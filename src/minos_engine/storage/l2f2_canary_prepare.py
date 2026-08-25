"""CONTROL-PLANE preparation of the L2-F2 Phase-A canary.

This is deliberately *not* a runner operation. Persisting a plan graph, publishing config
payloads, recording an execution authority and enqueuing a job are administrative acts, so this
module runs with administrative authority — which is exactly why it is a separate module from
:mod:`~minos_engine.storage.l2f2_runner`, whose entire purpose is to need none of that.

What it prepares is fixed, not chosen: the frozen Phase-A plan (five chromosome-balanced TRAIN
members × the accepted 39 candidates = 195 logical jobs), its execution authority bound to the
frozen L2-F2-B protocol hash, and **exactly one** enqueued job — logical index 0, the canary.
There is no parameter by which a caller could enqueue a different job, a different count or a
different plan.

Two safety properties matter more than convenience. Preparation is **idempotent**: replaying it
converges on the same plan, the same authority and the same single job rather than duplicating
anything. And it **fails closed on unexplained state**: if the database already holds jobs,
results or an authority this function did not put there, it refuses rather than quietly adding
the canary beside them. Deleting or resetting state it did not create is never an option.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import Engine, text

from minos_engine.baseline.phase_a import PhaseAAuthority, build_phase_a_authority
from minos_engine.common.errors import MinosEngineError
from minos_engine.experiments.candidates import generate_accepted_candidate_set
from minos_engine.experiments.plan import iter_logical_jobs
from minos_engine.storage.l2f2_runner import BASELINE_REVISION

__all__ = [
    "CanaryPreparationError",
    "CanaryPreparationResult",
    "prepare_l2f2_phase_a_canary",
]

_AUTHORITIES = "experiments.l2f2_execution_authorities"
_PHASE = "PHASE_A"


class CanaryPreparationError(MinosEngineError):
    """The baseline store is not in a state the canary may be prepared into."""


@dataclass(frozen=True)
class CanaryPreparationResult:
    """What preparation established (or verified already present)."""

    plan_hash: str
    plan_id: str
    authority_id: str
    canary_job_key: str
    member_count: int
    candidate_count: int
    logical_job_count: int
    enqueued_job_count: int
    plan_created: bool
    authority_created: bool
    job_created: bool


def _require_clean_or_matching(engine: Engine, authority: PhaseAAuthority) -> None:
    """Refuse unexplained prior state instead of enqueuing the canary beside it."""
    plan_hash = authority.plan_hash
    with engine.connect() as conn:
        revision = conn.execute(
            text("SELECT version_num FROM alembic_version")
        ).scalar_one_or_none()
        if revision != BASELINE_REVISION:
            raise CanaryPreparationError(
                f"baseline database revision is {revision!r}, expected {BASELINE_REVISION!r}"
            )
        foreign_plans = conn.execute(
            text("SELECT count(*) FROM experiments.l2f_experiment_plans WHERE plan_hash <> :h"),
            {"h": plan_hash},
        ).scalar_one()
        if foreign_plans:
            raise CanaryPreparationError(
                f"{foreign_plans} unexplained experiment plan(s) are already persisted; "
                "preparation refuses to add the canary beside state it did not create"
            )
        foreign_authorities = conn.execute(
            text(f"SELECT count(*) FROM {_AUTHORITIES} WHERE plan_hash <> :h"),  # noqa: S608
            {"h": plan_hash},
        ).scalar_one()
        if foreign_authorities:
            raise CanaryPreparationError(
                f"{foreign_authorities} unexplained execution authority row(s) already exist"
            )
        results = conn.execute(
            text("SELECT count(*) FROM experiments.l2f_execution_results")
        ).scalar_one()
        failures = conn.execute(
            text("SELECT count(*) FROM experiments.l2f_execution_failures")
        ).scalar_one()
        if results or failures:
            raise CanaryPreparationError(
                f"the baseline store already holds {results} execution result(s) and {failures} "
                "failure(s); preparation never runs over existing execution history"
            )
        unexpected_jobs = conn.execute(
            text(
                "SELECT count(*) FROM experiments.l2f_experiment_jobs j "
                "  JOIN experiments.l2f_experiment_plans p ON p.id = j.plan_id "
                " WHERE p.plan_hash <> :h OR j.job_key <> :k"
            ),
            {"h": plan_hash, "k": authority.canary.job_key},
        ).scalar_one()
        if unexpected_jobs:
            raise CanaryPreparationError(
                f"{unexpected_jobs} job(s) other than the frozen canary are already enqueued"
            )


def prepare_l2f2_phase_a_canary(
    engine: Engine, *, config_artifact_root: Path
) -> CanaryPreparationResult:
    """Persist the frozen Phase-A plan, record its authority and enqueue ONLY the canary.

    Accepts no plan, candidate set, job key, start or count: everything is derived from committed
    authorities. Idempotent, and fail-closed on any state it did not create.
    """
    from minos_engine.storage.l2f_config_publisher import ConfigPayloadPublisher
    from minos_engine.storage.l2f_job_enqueue import _enqueue_experiment_jobs_with_trust
    from minos_engine.storage.l2f_plan_store import _persist_experiment_plan_with_trust

    authority = build_phase_a_authority()
    plan = authority.plan
    candidate_set = generate_accepted_candidate_set()
    _require_clean_or_matching(engine, authority)

    with engine.connect() as conn:
        existing_plan = conn.execute(
            text("SELECT id FROM experiments.l2f_experiment_plans WHERE plan_hash = :h"),
            {"h": plan.plan_hash},
        ).scalar_one_or_none()

    plan_created = existing_plan is None
    if plan_created:
        _persist_experiment_plan_with_trust(
            engine,
            plan,
            candidate_set,
            publisher=ConfigPayloadPublisher(config_artifact_root),
        )

    with engine.connect() as conn:
        plan_id = str(
            conn.execute(
                text("SELECT id FROM experiments.l2f_experiment_plans WHERE plan_hash = :h"),
                {"h": plan.plan_hash},
            ).scalar_one()
        )

    # ---- the immutable L2-F2 execution authority ------------------------------------------
    with engine.connect() as conn, conn.begin():
        conn.execute(text("SET LOCAL ROLE minos_admin"))
        row = (
            conn.execute(
                text(
                    f"SELECT id, baseline_protocol_hash, member_count, candidate_count, "  # noqa: S608
                    f"       logical_job_count, canary_job_key, train_schedule_sha256, "
                    f"       candidate_set_hash, parameter_space_hash "
                    f"  FROM {_AUTHORITIES} WHERE plan_hash = :h AND phase = :p"
                ),
                {"h": plan.plan_hash, "p": _PHASE},
            )
            .mappings()
            .one_or_none()
        )
        authority_created = row is None
        if row is None:
            authority_id = str(
                conn.execute(
                    text(
                        f"INSERT INTO {_AUTHORITIES} ("  # noqa: S608
                        "  baseline_protocol_hash, phase, plan_id, plan_hash, "
                        "  train_schedule_sha256, candidate_set_hash, parameter_space_hash, "
                        "  member_count, candidate_count, logical_job_count, canary_job_key) "
                        "VALUES (:proto, :phase, :plan_id, :plan_hash, :sched, :cand, :space, "
                        "        :members, :configs, :jobs, :canary) RETURNING id"
                    ),
                    {
                        "proto": authority.baseline_protocol_hash,
                        "phase": _PHASE,
                        "plan_id": plan_id,
                        "plan_hash": plan.plan_hash,
                        "sched": authority.train_schedule_manifest_sha256,
                        "cand": plan.candidate_set_hash,
                        "space": plan.parameter_space_hash,
                        "members": plan.train_member_count,
                        "configs": plan.candidate_count,
                        "jobs": plan.logical_job_count,
                        "canary": authority.canary.job_key,
                    },
                ).scalar_one()
            )
        else:
            # an existing authority must agree EXACTLY; it is append-only and never repaired.
            existing = dict(row)
            mismatched = {
                "baseline_protocol_hash": (
                    existing["baseline_protocol_hash"],
                    authority.baseline_protocol_hash,
                ),
                "train_schedule_sha256": (
                    existing["train_schedule_sha256"],
                    authority.train_schedule_manifest_sha256,
                ),
                "candidate_set_hash": (existing["candidate_set_hash"], plan.candidate_set_hash),
                "parameter_space_hash": (
                    existing["parameter_space_hash"],
                    plan.parameter_space_hash,
                ),
                "member_count": (existing["member_count"], plan.train_member_count),
                "candidate_count": (existing["candidate_count"], plan.candidate_count),
                "logical_job_count": (existing["logical_job_count"], plan.logical_job_count),
                "canary_job_key": (existing["canary_job_key"], authority.canary.job_key),
            }
            differing = sorted(k for k, (a, b) in mismatched.items() if a != b)
            if differing:
                raise CanaryPreparationError(
                    f"the persisted execution authority disagrees with the frozen authority on "
                    f"{differing}; it is append-only and is never repaired"
                )
            authority_id = str(existing["id"])

    # ---- exactly ONE job: logical index 0 ---------------------------------------------------
    canary = next(iter_logical_jobs(plan))
    if canary.job_key != authority.canary.job_key:  # pragma: no cover - structural guard
        raise CanaryPreparationError("logical job 0 does not match the frozen canary job key")

    with engine.connect() as conn:
        enqueued = conn.execute(
            text(
                "SELECT count(*) FROM experiments.l2f_experiment_jobs j "
                "  JOIN experiments.l2f_experiment_plans p ON p.id = j.plan_id "
                " WHERE p.plan_hash = :h"
            ),
            {"h": plan.plan_hash},
        ).scalar_one()
    job_created = enqueued == 0
    if job_created:
        _enqueue_experiment_jobs_with_trust(engine, plan, candidate_set, start=0, count=1)

    with engine.connect() as conn:
        total_jobs = conn.execute(
            text(
                "SELECT count(*) FROM experiments.l2f_experiment_jobs j "
                "  JOIN experiments.l2f_experiment_plans p ON p.id = j.plan_id "
                " WHERE p.plan_hash = :h"
            ),
            {"h": plan.plan_hash},
        ).scalar_one()
    if total_jobs != 1:
        raise CanaryPreparationError(
            f"exactly one canary job must be enqueued; the store holds {total_jobs}"
        )

    return CanaryPreparationResult(
        plan_hash=plan.plan_hash,
        plan_id=plan_id,
        authority_id=authority_id,
        canary_job_key=authority.canary.job_key,
        member_count=plan.train_member_count,
        candidate_count=plan.candidate_count,
        logical_job_count=plan.logical_job_count,
        enqueued_job_count=int(total_jobs),
        plan_created=plan_created,
        authority_created=authority_created,
        job_created=job_created,
    )
