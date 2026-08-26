"""CONTROL-PLANE preparation of the L2-F2 Phase-B execution authority.

Recording an execution authority is an administrative act, so it lives here rather than in
:mod:`~minos_engine.storage.l2f2_runner`, whose whole purpose is to need no administrative
authority at all. This is the Phase-B counterpart of
:mod:`~minos_engine.storage.l2f2_canary_prepare`, and deliberately narrower: Phase B's plan is
persisted by its own boundary and its jobs are materialized by the control plane, so the ONLY
thing left for this module is the one authority row that lets the runner resolve them.

Nothing about that row is chosen. Every value is derived from the completed Phase-A ledger by
:func:`~minos_engine.baseline.phase_b.build_l2f2_phase_b_authority` — the plan, the candidate set,
the counts, the schedule and the protocol — and the function accepts no override for any of them.
A caller supplies an engine.

Two Phase-B specifics are worth stating:

* **There is no Phase-B canary.** The canary is a Phase-A concept: a structurally chosen first job
  proving the chain end-to-end before a screen is expanded. Phase B inherits a proven chain and a
  proven runtime, and inventing a canary for it would be fabricating scientific structure to
  satisfy a column. ``0016`` makes the column nullable and requires it to be NULL for Phase B.
* **The plan must already be persisted.** This module never persists a plan as a side effect of
  authorizing one; an authority for a plan that does not exist would be an authority over nothing.

Preparation is idempotent, and an existing authority must agree EXACTLY. The table is append-only
scientific lineage: a disagreement is a conflict to be raised, never a row to be repaired.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from sqlalchemy import text

from minos_engine.common.errors import MinosEngineError
from minos_engine.storage.l2f2_runner import BASELINE_DATABASE_NAME, BASELINE_REVISION

if TYPE_CHECKING:
    from sqlalchemy import Engine

    from minos_engine.baseline.phase_b import PhaseBAuthority

__all__ = [
    "PhaseBAuthorityConflictError",
    "PhaseBAuthorityPreparationError",
    "PhaseBAuthorityResult",
    "prepare_l2f2_phase_b_execution_authority",
]

_AUTHORITIES = "experiments.l2f2_execution_authorities"
_PHASE = "PHASE_B"


class PhaseBAuthorityPreparationError(MinosEngineError):
    """The baseline store is not in a state a Phase-B execution authority may be recorded into."""


class PhaseBAuthorityConflictError(PhaseBAuthorityPreparationError):
    """A Phase-B authority already exists for this plan and disagrees with the derived one."""


@dataclass(frozen=True)
class PhaseBAuthorityResult:
    """What preparation established (or verified already present)."""

    authority_id: str
    plan_id: str
    plan_hash: str
    phase: str
    candidate_set_hash: str
    member_count: int
    candidate_count: int
    logical_job_count: int
    canary_job_key: None
    created: bool


def _require_baseline_connection(conn: Any) -> None:
    """The active baseline store at the exact revision that admits Phase B. Fail closed."""
    database = conn.execute(text("SELECT current_database()")).scalar_one()
    if database != BASELINE_DATABASE_NAME:
        raise PhaseBAuthorityPreparationError(
            f"connected to database {database!r}, not the baseline store {BASELINE_DATABASE_NAME!r}"
        )
    revision = conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one_or_none()
    if revision != BASELINE_REVISION:
        raise PhaseBAuthorityPreparationError(
            f"baseline database revision is {revision!r}, expected {BASELINE_REVISION!r}; a "
            "Phase-B execution authority is representable only from that revision onward"
        )


def _plan_id(conn: Any, authority: PhaseBAuthority) -> str:
    plan_id = conn.execute(
        text("SELECT id FROM experiments.l2f_experiment_plans WHERE plan_hash = :h"),
        {"h": authority.plan_hash},
    ).scalar_one_or_none()
    if plan_id is None:
        raise PhaseBAuthorityPreparationError(
            f"the derived Phase-B plan {authority.plan_hash} is not persisted in this store; "
            "an execution authority over a plan that does not exist authorizes nothing"
        )
    return str(plan_id)


def _verify_existing(row: dict[str, Any], authority: PhaseBAuthority) -> None:
    """An existing authority must agree on every immutable field. Never repaired, never updated."""
    plan = authority.plan
    expected: dict[str, tuple[Any, Any]] = {
        "baseline_protocol_hash": (
            row["baseline_protocol_hash"],
            authority.baseline_protocol_hash,
        ),
        "train_schedule_sha256": (
            row["train_schedule_sha256"],
            authority.train_schedule_manifest_sha256,
        ),
        "candidate_set_hash": (row["candidate_set_hash"], authority.phase_b_candidate_set_hash),
        "parameter_space_hash": (row["parameter_space_hash"], plan.parameter_space_hash),
        "member_count": (row["member_count"], plan.train_member_count),
        "candidate_count": (row["candidate_count"], plan.candidate_count),
        "logical_job_count": (row["logical_job_count"], plan.logical_job_count),
        "canary_job_key": (row["canary_job_key"], None),
    }
    differing = sorted(key for key, (found, wanted) in expected.items() if found != wanted)
    if differing:
        raise PhaseBAuthorityConflictError(
            f"the persisted Phase-B execution authority disagrees with the derived authority on "
            f"{differing}; it is append-only scientific lineage and is never repaired"
        )


def prepare_l2f2_phase_b_execution_authority(engine: Engine) -> PhaseBAuthorityResult:
    """Record THE one Phase-B execution authority for the derived Phase-B plan. Idempotent.

    The authority is derived here, from the immutable ledger — no plan hash, candidate set,
    member list, config list, dimension, anchor, count, phase or schedule identity crosses this
    boundary from a caller.
    """
    from minos_engine.baseline.phase_b import build_l2f2_phase_b_authority

    with engine.connect() as conn:
        _require_baseline_connection(conn)

    authority = build_l2f2_phase_b_authority(engine)
    plan = authority.plan
    if authority.phase != _PHASE:  # pragma: no cover - structural guard
        raise PhaseBAuthorityPreparationError(
            f"the derived authority is {authority.phase}, not {_PHASE}"
        )

    with engine.connect() as conn, conn.begin():
        plan_id = _plan_id(conn, authority)
        conn.execute(text("SET LOCAL ROLE minos_admin"))
        row = (
            conn.execute(
                text(
                    "SELECT id, plan_id, baseline_protocol_hash, train_schedule_sha256, "  # noqa: S608
                    "       candidate_set_hash, parameter_space_hash, member_count, "
                    "       candidate_count, logical_job_count, canary_job_key "
                    f"  FROM {_AUTHORITIES} WHERE plan_hash = :h AND phase = :p"
                ),
                {"h": authority.plan_hash, "p": _PHASE},
            )
            .mappings()
            .one_or_none()
        )
        created = row is None
        if row is None:
            # canary_job_key is deliberately absent from the INSERT: Phase B has no canary, and
            # 0016's phase-semantic CHECK requires it to be NULL.
            authority_id = str(
                conn.execute(
                    text(
                        f"INSERT INTO {_AUTHORITIES} ("  # noqa: S608
                        "  baseline_protocol_hash, phase, plan_id, plan_hash, "
                        "  train_schedule_sha256, candidate_set_hash, parameter_space_hash, "
                        "  member_count, candidate_count, logical_job_count) "
                        "VALUES (:proto, :phase, :plan_id, :plan_hash, :sched, :cand, :space, "
                        "        :members, :configs, :jobs) RETURNING id"
                    ),
                    {
                        "proto": authority.baseline_protocol_hash,
                        "phase": _PHASE,
                        "plan_id": plan_id,
                        "plan_hash": authority.plan_hash,
                        "sched": authority.train_schedule_manifest_sha256,
                        "cand": authority.phase_b_candidate_set_hash,
                        "space": plan.parameter_space_hash,
                        "members": plan.train_member_count,
                        "configs": plan.candidate_count,
                        "jobs": plan.logical_job_count,
                    },
                ).scalar_one()
            )
        else:
            existing = dict(row)
            if str(existing["plan_id"]) != plan_id:  # pragma: no cover - FK makes this unreachable
                raise PhaseBAuthorityConflictError(
                    f"the persisted Phase-B authority binds plan {existing['plan_id']}, not the "
                    f"derived plan {plan_id}"
                )
            _verify_existing(existing, authority)
            authority_id = str(existing["id"])

    return PhaseBAuthorityResult(
        authority_id=authority_id,
        plan_id=plan_id,
        plan_hash=authority.plan_hash,
        phase=_PHASE,
        candidate_set_hash=authority.phase_b_candidate_set_hash,
        member_count=plan.train_member_count,
        candidate_count=plan.candidate_count,
        logical_job_count=plan.logical_job_count,
        canary_job_key=None,
        created=created,
    )
