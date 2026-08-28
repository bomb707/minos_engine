"""L2-F F3-C2 bounded, deterministic, idempotent experiment-job enqueue (no claiming).

Inserts a **bounded** contiguous slice ``[start, start + count)`` of the accepted plan's logical
jobs (member-major then config-index order — the frozen F3-B order, never a second ordering
rule) into ``experiments.l2f_experiment_jobs``. It performs no claiming, execution, result
storage, scoring, optimization, gating or service activation (those are F4+), and there is **no**
enqueue-all / implicit full-corpus API — the maximum batch is ``MAX_ENQUEUE_BATCH`` jobs.

``enqueue_accepted_experiment_jobs(*, start, count)`` is the sole production entry point: it takes
no caller-supplied plan/hashes/snapshot/partition/candidate-set/member-id/config-id/job-key/trust,
obtains the database only through ``MINOS_DATABASE_URL``, verifies (as the FIRST access on the
exact transaction connection) that it is the canonical operational store at revision ``0006``
(never running Alembic) BEFORE constructing the accepted plan or issuing any plan query, requires
the complete accepted F3-C1 graph to already exist and verifies it (it never persists or repairs
a missing graph), and then — under a plan_hash-scoped advisory lock — inserts only the selected
jobs idempotently. ``_enqueue_experiment_jobs_with_trust`` is a PRIVATE explicit-trust boundary
for synthetic / non-75 tests only (no operational-identity check); it is not exported.
"""

from __future__ import annotations

import contextlib
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from sqlalchemy import Connection, Engine, text

from minos_engine.experiments.candidates import (
    generate_accepted_candidate_set,
    verify_accepted_candidate_set,
)
from minos_engine.experiments.plan import compute_job_key, iter_logical_jobs
from minos_engine.storage.database import create_db_engine, verify_operational_database_identity
from minos_engine.storage.l2f_plan_store import (
    L2FPersistenceError,
    _advisory_key,
    _build_accepted_plan,
    _insert_or_verify,
    _require_live_revision,
    _resolve_plan_upstream,
    _UpstreamResolver,
    _verify_persisted_graph,
)
from minos_engine.storage.roles import SCHEMA_OWNER

if TYPE_CHECKING:
    from minos_engine.experiments.candidates import CandidateSet
    from minos_engine.experiments.plan import ExperimentPlan

__all__ = [
    "MAX_ENQUEUE_BATCH",
    "JobEnqueueError",
    "JobEnqueueRangeError",
    "JobGraphMissingError",
    "JobKeyMismatchError",
    "JobEnqueueResult",
    "enqueue_accepted_experiment_jobs",
]

#: hard maximum number of jobs a single bounded enqueue may create (no enqueue-all exists).
MAX_ENQUEUE_BATCH = 64

_PENDING = "PENDING"

# The two alternate UNIQUE constraints of experiments.l2f_experiment_jobs that can collide on a
# fresh insert (constraints carrying the freshly generated ``id`` can never collide). Only the
# IMMUTABLE scientific-identity columns are inserted, so mutable status/claim metadata is never
# compared, reset, or written by an idempotent replay.
_JOB_UNIQUE_KEYS: dict[str, list[str]] = {
    "uq_l2f_jobs_job_key": ["job_key"],
    "uq_l2f_jobs_logical_identity": ["plan_id", "plan_member_id", "plan_config_id"],
}


class JobEnqueueError(L2FPersistenceError):
    """Base error for F3-C2 bounded job enqueue."""


class JobEnqueueRangeError(JobEnqueueError):
    """The requested ``[start, start + count)`` slice is outside the permitted bounds."""


class JobGraphMissingError(JobEnqueueError):
    """The complete accepted F3-C1 plan graph does not exist (enqueue never persists/repairs it)."""


class JobKeyMismatchError(JobEnqueueError):
    """An independently recomputed job_key disagreed with the frozen logical job ordering."""


@dataclass(frozen=True)
class JobEnqueueResult:
    """Outcome of one bounded enqueue call."""

    plan_hash: str
    requested_start: int
    requested_count: int
    created_count: int
    existing_count: int
    total_jobs_for_plan: int


def _validate_range_prearg(start: int, count: int) -> None:
    """Validate the caller-independent range bounds BEFORE any database or filesystem access."""
    if not isinstance(start, int) or isinstance(start, bool):
        raise JobEnqueueRangeError("start must be an int")
    if not isinstance(count, int) or isinstance(count, bool):
        raise JobEnqueueRangeError("count must be an int")
    if start < 0:
        raise JobEnqueueRangeError(f"start must be >= 0, got {start}")
    if count < 1 or count > MAX_ENQUEUE_BATCH:
        raise JobEnqueueRangeError(f"count must be in [1, {MAX_ENQUEUE_BATCH}], got {count}")


def _validate_range_against_plan(start: int, count: int, plan: ExperimentPlan) -> None:
    if start + count > plan.logical_job_count:
        raise JobEnqueueRangeError(
            f"start + count ({start + count}) exceeds the accepted plan's logical_job_count "
            f"({plan.logical_job_count})"
        )


def _build_accepted_candidate_set() -> CandidateSet:
    candidate_set = generate_accepted_candidate_set()
    verify_accepted_candidate_set(candidate_set)
    return candidate_set


def _resolve_plan_id(conn: Connection, plan: ExperimentPlan) -> str:
    plan_id = conn.execute(
        text("SELECT id FROM experiments.l2f_experiment_plans WHERE plan_hash = :h"),
        {"h": plan.plan_hash},
    ).scalar_one_or_none()
    if plan_id is None:
        raise JobGraphMissingError(
            "the accepted F3-C1 plan graph is not persisted; enqueue never persists or repairs it"
        )
    return str(plan_id)


def _member_index_map(conn: Connection, plan_id: str) -> dict[int, str]:
    rows = conn.execute(
        text(
            "SELECT member_index, id FROM experiments.l2f_experiment_plan_members WHERE plan_id = :p"
        ),
        {"p": plan_id},
    ).all()
    return {int(mi): str(pid) for mi, pid in rows}


def _config_index_map(conn: Connection, plan_id: str) -> dict[int, str]:
    rows = conn.execute(
        text(
            "SELECT config_index, id FROM experiments.l2f_experiment_plan_configs WHERE plan_id = :p"
        ),
        {"p": plan_id},
    ).all()
    return {int(ci): str(pid) for ci, pid in rows}


def _enqueue_slice(
    conn: Connection,
    plan: ExperimentPlan,
    candidate_set: CandidateSet,
    *,
    start: int,
    count: int,
    upstream_resolver: _UpstreamResolver = _resolve_plan_upstream,
) -> JobEnqueueResult:
    """Insert the selected bounded slice idempotently (given an open, verified transaction).

    The role elevation is ``SET LOCAL`` — TRANSACTION-scoped, so PostgreSQL itself restores the
    original session role on both COMMIT and ROLLBACK, and a pooled connection never returns to
    the pool still holding ``minos_admin``.
    """
    conn.execute(text(f"SET LOCAL ROLE {SCHEMA_OWNER}"))

    # require the complete accepted F3-C1 graph to exist and verify it (jobs-tolerant) BEFORE
    # enqueueing; never persist or repair a missing/invalid graph.
    plan_id = _resolve_plan_id(conn, plan)
    # the same INTERNAL strategy the persistence boundary uses: the full-closure resolver for
    # every historical path, and only the dedicated frozen-Phase-A projection substitutes it. The
    # pre-enqueue gate must resolve the plan the same way it was persisted, or it would re-derive
    # the very index conflation the projection exists to avoid.
    upstream = upstream_resolver(conn, plan)
    _verify_persisted_graph(conn, plan, candidate_set, plan_id, upstream, require_zero_jobs=False)

    # serialize concurrent enqueues on the plan via a deterministic plan_hash advisory lock.
    conn.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": _advisory_key(plan.plan_hash)})

    member_ids = _member_index_map(conn, plan_id)
    config_ids = _config_index_map(conn, plan_id)

    # the canonical order IS the frozen F3-B logical-job order (no second ordering rule).
    logical_jobs = list(iter_logical_jobs(plan))
    created = 0
    existing = 0
    for k in range(start, start + count):
        lj = logical_jobs[k]
        # independently recompute the job_key with the frozen formula and require equality.
        recomputed = compute_job_key(
            plan_hash=plan.plan_hash,
            member_index=lj.member_index,
            dataset_id=lj.dataset_id,
            profile_id=lj.profile_id,
            content_hash=lj.content_hash,
            feature_values_hash=lj.feature_values_hash,
            config_index=lj.config_index,
            config_hash=lj.config_hash,
        )
        if recomputed != lj.job_key:
            raise JobKeyMismatchError(
                f"recomputed job_key disagrees with the logical job at index {k}"
            )
        member_id = member_ids.get(lj.member_index)
        config_id = config_ids.get(lj.config_index)
        if member_id is None or config_id is None:
            raise JobGraphMissingError(
                f"persisted plan is missing member_index {lj.member_index} or "
                f"config_index {lj.config_index}"
            )
        # insert ONLY the immutable identity columns; status defaults to PENDING and claim
        # metadata to NULL. On any unique collision the existing row is re-read and its immutable
        # identity compared (never its mutable status/claim), so a replay resets nothing.
        _, was_created = _insert_or_verify(
            conn,
            table="l2f_experiment_jobs",
            row={
                "plan_id": plan_id,
                "plan_member_id": member_id,
                "plan_config_id": config_id,
                "job_key": lj.job_key,
            },
            unique_keys=_JOB_UNIQUE_KEYS,
        )
        if was_created:
            created += 1
        else:
            existing += 1

    total = int(
        conn.execute(
            text("SELECT count(*) FROM experiments.l2f_experiment_jobs WHERE plan_id = :p"),
            {"p": plan_id},
        ).scalar_one()
    )
    return JobEnqueueResult(
        plan_hash=plan.plan_hash,
        requested_start=start,
        requested_count=count,
        created_count=created,
        existing_count=existing,
        total_jobs_for_plan=total,
    )


_BuildPlan = Callable[[Connection], "tuple[ExperimentPlan, CandidateSet]"]


def _enqueue_in_new_transaction(
    engine: Engine,
    *,
    start: int,
    count: int,
    verify_identity: bool,
    build_plan: _BuildPlan,
    upstream_resolver: _UpstreamResolver = _resolve_plan_upstream,
) -> JobEnqueueResult:
    conn = engine.connect()
    trans = conn.begin()
    committed = False
    try:
        if verify_identity:
            # identity + revision are the FIRST accesses on this connection, before the accepted
            # plan is constructed and before any plan query.
            verify_operational_database_identity(conn)
            _require_live_revision(conn)
        plan, candidate_set = build_plan(conn)
        _validate_range_against_plan(start, count, plan)
        result = _enqueue_slice(
            conn,
            plan,
            candidate_set,
            start=start,
            count=count,
            upstream_resolver=upstream_resolver,
        )
        trans.commit()
        committed = True
        return result
    except BaseException:
        if not committed:
            with contextlib.suppress(Exception):
                trans.rollback()  # roll back every job inserted by this call
        raise
    finally:
        conn.close()


def enqueue_accepted_experiment_jobs(*, start: int, count: int) -> JobEnqueueResult:
    """Enqueue the bounded logical-job slice ``[start, start + count)`` of the accepted plan.

    Requires ``start >= 0``, ``1 <= count <= MAX_ENQUEUE_BATCH`` (validated before any database or
    filesystem access) and ``start + count <= plan.logical_job_count`` (validated after the
    identity + revision checks and accepted-plan construction). Idempotent: an exact existing job
    is a no-op; a conflicting identity/key is a typed error; a replay resets no status/claim
    metadata. There is no enqueue-all API.
    """
    _validate_range_prearg(start, count)
    engine = create_db_engine()
    try:

        def _build(_conn: Connection) -> tuple[ExperimentPlan, CandidateSet]:
            return _build_accepted_plan(), _build_accepted_candidate_set()

        return _enqueue_in_new_transaction(
            engine, start=start, count=count, verify_identity=True, build_plan=_build
        )
    finally:
        engine.dispose()


def _enqueue_experiment_jobs_with_trust(
    engine: Engine,
    plan: ExperimentPlan,
    candidate_set: CandidateSet,
    *,
    start: int,
    count: int,
) -> JobEnqueueResult:
    """PRIVATE explicit-trust enqueue for scratch / non-75 tests ONLY (no operational-identity
    check). Never exported; the accepted production path is
    :func:`enqueue_accepted_experiment_jobs`."""
    _validate_range_prearg(start, count)
    return _enqueue_in_new_transaction(
        engine,
        start=start,
        count=count,
        verify_identity=False,
        build_plan=lambda _conn: (plan, candidate_set),
    )


def _enqueue_l2f2_phase_a_slice_with_trust(
    engine: Engine, *, start: int, count: int
) -> JobEnqueueResult:
    """PRIVATE dedicated enqueue of a BOUNDED slice of the frozen L2-F2 Phase-A logical jobs.

    The counterpart of the dedicated Phase-A persistence boundary, and for the same reason: the
    pre-enqueue integrity gate re-resolves upstream, and the historical full-inventory resolver
    would look for the Phase-A members at matrix ordinals 0..4 instead of 0/10/20/30/40.

    It takes no plan, candidate set, member, config or job key — every one is recomputed here
    from committed authority, so the ONLY thing a caller chooses is which contiguous slice of the
    frozen 195-job order to insert. There is deliberately still no enqueue-all: ``count`` is
    bounded by :data:`MAX_ENQUEUE_BATCH` exactly as the historical path is.
    """
    from minos_engine.baseline.phase_a import build_phase_a_plan
    from minos_engine.storage.l2f_plan_store import _resolve_phase_a_upstream

    _validate_range_prearg(start, count)
    plan = build_phase_a_plan()
    candidate_set = _build_accepted_candidate_set()
    return _enqueue_in_new_transaction(
        engine,
        start=start,
        count=count,
        verify_identity=False,
        build_plan=lambda _conn: (plan, candidate_set),
        upstream_resolver=_resolve_phase_a_upstream,
    )


def _enqueue_l2f2_phase_b_slice_with_trust(
    engine: Engine, *, start: int, count: int
) -> JobEnqueueResult:
    """PRIVATE dedicated enqueue of a BOUNDED slice of the derived L2-F2 Phase-B logical jobs.

    The Phase-B counterpart of the Phase-A seam, and for the same reason: the pre-enqueue
    integrity gate re-resolves upstream, and Phase-B members are a projection of the accepted
    closure at source ordinals 0/10/20/30/40/1/11/21/31/41 rather than 0..9.

    It takes no plan, candidate set, member, config or job key: the authority is derived from the
    completed Phase-A ledger here, so the ONLY caller choice is which contiguous slice of the
    frozen logical order to insert, bounded by :data:`MAX_ENQUEUE_BATCH` exactly as every other
    enqueue path is.
    """
    from minos_engine.baseline.phase_b import build_l2f2_phase_b_authority
    from minos_engine.storage.l2f_plan_store import (
        _phase_b_candidate_set_for,
        _resolve_phase_b_upstream,
    )

    _validate_range_prearg(start, count)
    authority = build_l2f2_phase_b_authority(engine)
    candidate_set = _phase_b_candidate_set_for(authority)
    return _enqueue_in_new_transaction(
        engine,
        start=start,
        count=count,
        verify_identity=False,
        build_plan=lambda _conn: (authority.plan, candidate_set),
        upstream_resolver=_resolve_phase_b_upstream,
    )


def _enqueue_l2f2_phase_c_slice_with_trust(
    engine: Engine, *, start: int, count: int
) -> JobEnqueueResult:
    """PRIVATE dedicated enqueue of a BOUNDED slice of the derived L2-F2 Phase-C logical jobs.

    The Phase-C counterpart of the Phase-A and Phase-B seams: the authority is derived from the
    completed Phase-B ledger here, so the ONLY caller choice is which contiguous slice of the
    frozen logical order to insert, bounded by :data:`MAX_ENQUEUE_BATCH` exactly as every other
    enqueue path is.
    """
    from minos_engine.baseline.phase_c import build_l2f2_phase_c_authority
    from minos_engine.storage.l2f_plan_store import (
        _phase_c_candidate_set_for,
        _resolve_phase_c_upstream,
    )

    _validate_range_prearg(start, count)
    authority = build_l2f2_phase_c_authority(engine)
    candidate_set = _phase_c_candidate_set_for(authority)
    return _enqueue_in_new_transaction(
        engine,
        start=start,
        count=count,
        verify_identity=False,
        build_plan=lambda _conn: (authority.plan, candidate_set),
        upstream_resolver=_resolve_phase_c_upstream,
    )


def _enqueue_l2f2_phase_a_canary_with_trust(engine: Engine) -> JobEnqueueResult:
    """PRIVATE dedicated enqueue of EXACTLY the frozen L2-F2 Phase-A canary — logical job 0.

    A fixed slice of the boundary above: no start, no count, nothing a caller could widen.
    """
    return _enqueue_l2f2_phase_a_slice_with_trust(engine, start=0, count=1)
