"""L2-F F4 safe job claiming + pre-execution state transitions (no execution, no results).

Exposes the three F4 operational boundaries — claim / start / release — over the accepted L2-F
job queue. Every mutation goes through the narrowly scoped ``SECURITY DEFINER`` functions added
by migration ``0007_l2f_job_claiming``; this module issues **no** direct INSERT/UPDATE/DELETE on
``experiments.l2f_experiment_jobs`` and ``minos_runner`` holds no table mutation grant. It never
executes GATK, never writes a result artifact, never touches legacy ``experiments.jobs``, and
implements no leases, stale-claim reclamation, heartbeats, retries or terminal-status handling
(those are F5+).

Each production entry point takes no caller-provided plan / plan hash / candidate set / database
/ partition / trust bundle. It obtains the database only through ``MINOS_DATABASE_URL``, verifies
(as the FIRST access on the exact transaction connection) that it is the canonical operational
store at revision ``0007_l2f_job_claiming`` — never running Alembic — then constructs the accepted
plan internally and verifies the persisted plan identity before claiming. The
``_*_with_trust`` counterparts are PRIVATE scratch/non-75 test boundaries and are not exported.

F4 state machine (enforced by BOTH the database trigger and these functions):

    PENDING  --claim(worker)-->   CLAIMED    (claimed_by=worker, claimed_at=now)
    CLAIMED  --start(worker)-->   RUNNING    (same worker; claim metadata preserved)
    CLAIMED  --release(worker)--> PENDING    (both claim fields cleared)

No other transition is reachable; SUCCEEDED / FAILED / CANCELLED remain unavailable until F5.
"""

from __future__ import annotations

import contextlib
import re
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from uuid import UUID

from sqlalchemy import Connection, Engine, text
from sqlalchemy.exc import DBAPIError

from minos_engine.common.errors import MinosEngineError
from minos_engine.storage.database import create_db_engine, verify_operational_database_identity
from minos_engine.storage.l2f_harness_verifier import _resolve_accepted_plan_row
from minos_engine.storage.l2f_plan_store import PlanRevisionError, _build_accepted_plan, _sqlstate

if TYPE_CHECKING:
    from minos_engine.experiments.plan import ExperimentPlan

__all__ = [
    "F4_MIGRATION_REVISION",
    "F4_COMPATIBLE_REVISIONS",
    "JOB_STATUS_PENDING",
    "JOB_STATUS_CLAIMED",
    "JOB_STATUS_RUNNING",
    "F4_TRANSITIONS",
    "WORKER_ID_MAX_LENGTH",
    "JobClaimError",
    "InvalidWorkerIdError",
    "JobPlanMissingError",
    "InvalidJobTransitionError",
    "AmbiguousClaimCommitError",
    "ClaimedJob",
    "JobStateResult",
    "claim_next_accepted_job",
    "start_accepted_job",
    "release_accepted_job",
]

#: the exact live revision this boundary requires (it NEVER runs Alembic).
F4_MIGRATION_REVISION = "0007_l2f_job_claiming"
#: F4 claim/start/release run unchanged under 0007 and under the additive 0008.
F4_COMPATIBLE_REVISIONS: frozenset[str] = frozenset(
    {F4_MIGRATION_REVISION, "0008_l2f_execution_results"}
)

JOB_STATUS_PENDING = "PENDING"
JOB_STATUS_CLAIMED = "CLAIMED"
JOB_STATUS_RUNNING = "RUNNING"

#: the complete, frozen F4 transition table (terminal states are F5 and are absent here).
F4_TRANSITIONS: tuple[tuple[str, str], ...] = (
    (JOB_STATUS_PENDING, JOB_STATUS_CLAIMED),
    (JOB_STATUS_CLAIMED, JOB_STATUS_PENDING),
    (JOB_STATUS_CLAIMED, JOB_STATUS_RUNNING),
)

#: conservative frozen worker-identity contract, validated BEFORE any database access. Bound
#: parameters are used regardless — this is defence in depth, never the only defence.
WORKER_ID_MAX_LENGTH = 64
_WORKER_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$")

_CLAIM_FN = "experiments.minos_l2f_claim_next_job"
_START_FN = "experiments.minos_l2f_start_job"
_RELEASE_FN = "experiments.minos_l2f_release_job"

# stable SQLSTATEs raised by the 0007 functions/trigger -> typed Python errors (never strings).
_SQLSTATE_INVALID_WORKER = "MN001"
_SQLSTATE_PLAN_ABSENT = "MN002"
_SQLSTATE_NOT_OWNED = "MN003"
_SQLSTATE_CLAIM_INVARIANT = "MN010"
_SQLSTATE_CLAIM_METADATA = "MN011"
_SQLSTATE_TRANSITION = "MN012"
_TRANSITION_SQLSTATES = frozenset(
    {_SQLSTATE_NOT_OWNED, _SQLSTATE_CLAIM_INVARIANT, _SQLSTATE_CLAIM_METADATA, _SQLSTATE_TRANSITION}
)


class JobClaimError(MinosEngineError):
    """Base error for F4 job claiming / state transitions."""


class InvalidWorkerIdError(JobClaimError):
    """The supplied worker identity violates the frozen worker-id contract."""


class JobPlanMissingError(JobClaimError):
    """The accepted plan graph is not persisted (claiming never persists or repairs it)."""


class InvalidJobTransitionError(JobClaimError):
    """The requested transition is not permitted, or the job is not claimed by this worker."""


class AmbiguousClaimCommitError(JobClaimError):
    """The COMMIT itself raised: the outcome is unknown. NEVER retried automatically."""


@dataclass(frozen=True)
class ClaimedJob:
    """A job successfully claimed by this worker (identity + claim state only)."""

    job_id: str
    job_key: str
    plan_id: str
    plan_member_id: str
    plan_config_id: str
    status: str
    claimed_by: str
    claimed_at: Any


@dataclass(frozen=True)
class JobStateResult:
    """The post-transition state of one job."""

    job_id: str
    job_key: str
    status: str
    claimed_by: str | None
    claimed_at: Any


def validate_worker_id(worker_id: str) -> str:
    """Validate the frozen worker-identity contract BEFORE any database or filesystem access."""
    if not isinstance(worker_id, str):
        raise InvalidWorkerIdError("worker_id must be a string")
    if not worker_id or len(worker_id) > WORKER_ID_MAX_LENGTH:
        raise InvalidWorkerIdError(
            f"worker_id must be 1..{WORKER_ID_MAX_LENGTH} characters, got {len(worker_id)}"
        )
    if not _WORKER_ID_RE.fullmatch(worker_id):
        raise InvalidWorkerIdError(
            "worker_id must start alphanumeric and contain only [A-Za-z0-9._:-]"
        )
    return worker_id


def _coerce_job_id(job_id: UUID) -> str:
    if not isinstance(job_id, UUID):
        raise InvalidJobTransitionError("job_id must be a uuid.UUID")
    return str(job_id)


def _require_f4_revision(conn: Connection) -> None:
    rev = conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one_or_none()
    if rev not in F4_COMPATIBLE_REVISIONS:
        raise PlanRevisionError(
            f"live database revision is {rev!r}, which is not one of the F4-compatible "
            f"revisions {sorted(F4_COMPATIBLE_REVISIONS)!r}; refusing to claim (this boundary "
            "NEVER runs Alembic)"
        )


def _translate(exc: DBAPIError) -> JobClaimError | None:
    """Map a 0007 function/trigger SQLSTATE onto a typed F4 error (no message matching)."""
    state = _sqlstate(exc)  # type: ignore[arg-type]
    if state == _SQLSTATE_INVALID_WORKER:
        return InvalidWorkerIdError("worker_id rejected by the database contract")
    if state == _SQLSTATE_PLAN_ABSENT:
        return JobPlanMissingError("the accepted L2-F plan graph is not persisted")
    if state in _TRANSITION_SQLSTATES:
        return InvalidJobTransitionError(
            "the requested F4 transition is not permitted for this job/worker"
        )
    return None


@contextlib.contextmanager
def _typed_errors() -> Iterator[None]:
    try:
        yield
    except DBAPIError as exc:
        typed = _translate(exc)
        if typed is not None:
            raise typed from exc
        raise


def _commit_or_ambiguous(trans: Any) -> None:
    try:
        trans.commit()
    except BaseException as exc:  # a raising COMMIT -> unknown outcome; never auto-retried
        raise AmbiguousClaimCommitError(
            "COMMIT raised; the claim/transition outcome is ambiguous and is NOT retried"
        ) from exc


def _verify_persisted_plan(conn: Connection, plan: ExperimentPlan) -> None:
    """Resolve the persisted plan by its COMPLETE logical identity and require the recorded
    ``plan_hash`` to bind it (fails closed when absent or ambiguous)."""
    row = _resolve_accepted_plan_row(conn, plan)
    if str(row["plan_hash"]) != plan.plan_hash:
        raise JobPlanMissingError(
            "the persisted plan's recorded plan_hash does not bind the accepted plan identity"
        )


def _row_to_claimed(row: Any) -> ClaimedJob:
    return ClaimedJob(
        job_id=str(row["job_id"]),
        job_key=str(row["job_key"]),
        plan_id=str(row["plan_id"]),
        plan_member_id=str(row["plan_member_id"]),
        plan_config_id=str(row["plan_config_id"]),
        status=str(row["status"]),
        claimed_by=str(row["claimed_by"]),
        claimed_at=row["claimed_at"],
    )


def _row_to_state(row: Any) -> JobStateResult:
    return JobStateResult(
        job_id=str(row["job_id"]),
        job_key=str(row["job_key"]),
        status=str(row["status"]),
        claimed_by=None if row["claimed_by"] is None else str(row["claimed_by"]),
        claimed_at=row["claimed_at"],
    )


# --------------------------------------------------------------------------- #
# transaction-scoped primitives (used by both the accepted and the trust boundaries)
# --------------------------------------------------------------------------- #
def _claim_in_transaction(
    conn: Connection, plan: ExperimentPlan, worker_id: str
) -> ClaimedJob | None:
    _verify_persisted_plan(conn, plan)
    with _typed_errors():
        row = (
            conn.execute(
                text(f"SELECT * FROM {_CLAIM_FN}(:plan_hash, :worker_id)"),  # noqa: S608
                {"plan_hash": plan.plan_hash, "worker_id": worker_id},
            )
            .mappings()
            .first()
        )
    return None if row is None else _row_to_claimed(row)


def _transition_in_transaction(
    conn: Connection, function: str, plan: ExperimentPlan, job_id: str, worker_id: str
) -> JobStateResult:
    """Transition one job that MUST belong to the verified accepted plan.

    The persisted accepted-plan identity is resolved and verified first, and the plan hash is
    passed to the database function, which updates only when the job's ``plan_id`` resolves from
    that same hash. A job belonging to any other plan is therefore rejected with a typed error
    and leaves every row untouched.
    """
    _verify_persisted_plan(conn, plan)
    with _typed_errors():
        row = (
            conn.execute(
                text(f"SELECT * FROM {function}(:plan_hash, :job_id, :worker_id)"),  # noqa: S608
                {"plan_hash": plan.plan_hash, "job_id": job_id, "worker_id": worker_id},
            )
            .mappings()
            .first()
        )
    if row is None:  # pragma: no cover - the function raises MN003 instead of returning empty
        raise InvalidJobTransitionError("the requested F4 transition affected no job")
    return _row_to_state(row)


def _run_in_new_transaction[T](
    engine: Engine, *, verify_identity: bool, action: Callable[[Connection], T]
) -> T:
    conn = engine.connect()
    trans = conn.begin()
    committed = False
    try:
        if verify_identity:
            # identity + revision are the FIRST accesses on this exact connection, before the
            # accepted plan is constructed and before any stage query.
            verify_operational_database_identity(conn)
            _require_f4_revision(conn)
        result = action(conn)
        _commit_or_ambiguous(trans)
        committed = True
        return result
    except AmbiguousClaimCommitError:
        raise  # outcome unknown: do NOT roll back, do NOT retry
    except BaseException:
        if not committed:
            with contextlib.suppress(Exception):
                trans.rollback()
        raise
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# accepted production boundaries
# --------------------------------------------------------------------------- #
def claim_next_accepted_job(*, worker_id: str) -> ClaimedJob | None:
    """Claim the next PENDING job of the accepted plan for ``worker_id``, or ``None`` if the
    queue is empty. Deterministic order ``(created_at, id)``; rows locked by another worker are
    skipped (``FOR UPDATE SKIP LOCKED``), never waited on."""
    validate_worker_id(worker_id)
    engine = create_db_engine()
    try:
        return _run_in_new_transaction(
            engine,
            verify_identity=True,
            action=lambda conn: _claim_in_transaction(conn, _build_accepted_plan(), worker_id),
        )
    finally:
        engine.dispose()


def start_accepted_job(*, job_id: UUID, worker_id: str) -> JobStateResult:
    """Transition ``CLAIMED -> RUNNING`` for a job of the ACCEPTED plan this worker holds.

    The accepted plan is built internally and its complete persisted identity verified; the job
    must belong to that plan. A job from any other plan is rejected with a typed error and no
    mutation. Claim metadata is preserved across the transition."""
    validate_worker_id(worker_id)
    jid = _coerce_job_id(job_id)
    engine = create_db_engine()
    try:
        return _run_in_new_transaction(
            engine,
            verify_identity=True,
            action=lambda conn: _transition_in_transaction(
                conn, _START_FN, _build_accepted_plan(), jid, worker_id
            ),
        )
    finally:
        engine.dispose()


def release_accepted_job(*, job_id: UUID, worker_id: str) -> JobStateResult:
    """Transition ``CLAIMED -> PENDING`` for a job of the ACCEPTED plan this worker holds.

    The accepted plan is built internally and its complete persisted identity verified; the job
    must belong to that plan. A job from any other plan is rejected with a typed error and no
    mutation. Both claim fields are cleared."""
    validate_worker_id(worker_id)
    jid = _coerce_job_id(job_id)
    engine = create_db_engine()
    try:
        return _run_in_new_transaction(
            engine,
            verify_identity=True,
            action=lambda conn: _transition_in_transaction(
                conn, _RELEASE_FN, _build_accepted_plan(), jid, worker_id
            ),
        )
    finally:
        engine.dispose()


# --------------------------------------------------------------------------- #
# PRIVATE explicit-trust boundaries (scratch / non-75 tests ONLY; never exported)
# --------------------------------------------------------------------------- #
def _claim_next_job_with_trust(
    engine: Engine, plan: ExperimentPlan, *, worker_id: str
) -> ClaimedJob | None:
    validate_worker_id(worker_id)
    return _run_in_new_transaction(
        engine,
        verify_identity=False,
        action=lambda conn: _claim_in_transaction(conn, plan, worker_id),
    )


def _start_job_with_trust(
    engine: Engine, plan: ExperimentPlan, *, job_id: UUID, worker_id: str
) -> JobStateResult:
    validate_worker_id(worker_id)
    jid = _coerce_job_id(job_id)
    return _run_in_new_transaction(
        engine,
        verify_identity=False,
        action=lambda conn: _transition_in_transaction(conn, _START_FN, plan, jid, worker_id),
    )


def _release_job_with_trust(
    engine: Engine, plan: ExperimentPlan, *, job_id: UUID, worker_id: str
) -> JobStateResult:
    validate_worker_id(worker_id)
    jid = _coerce_job_id(job_id)
    return _run_in_new_transaction(
        engine,
        verify_identity=False,
        action=lambda conn: _transition_in_transaction(conn, _RELEASE_FN, plan, jid, worker_id),
    )
