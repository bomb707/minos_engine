"""Activate the prepared Phase-D campaign: ten truth identities, then forty jobs, then stop.

Preparation established WHAT the campaign is. Activation establishes two further things, and
they are deliberately separate decisions with separate entry points:

* **which truth the evaluator may use.** Ten VALIDATION identities, one per frozen member,
  content-hashed and registered through ``0022``'s ``SECURITY DEFINER`` registrar — which
  re-derives the partition from ``catalog.split_allocations`` rather than believing anything
  passed in, so TRAIN and TEST cannot enter through this door even by name;
* **which forty jobs may exist.** The exact Cartesian product of ten members and four
  configurations, and nothing else.

Neither function runs anything. Materialization ends with forty jobs in the canonical initial
state, unclaimed and untouched; spending GATK hours on them is the next authorization and is not
in this module.

Nothing scientific crosses either boundary. A caller supplies an engine and, for registration, the
root the truth bytes are READ from — never persisted. Which ten members, which four
configurations, which order, which partition, which plan: all resolved from the persisted Phase-D
authority, its binding, and the argument-free bootstrap that has to accept them first.

The job identity is not new. ``compute_job_key`` is the frozen domain-separated formula every
earlier phase used, and the enumeration order is the same member-major, config-index order, so a
Phase-D job is the same kind of object as a Phase-B or Phase-C one and lands in the same table.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from minos_engine.baseline.phase_d import (
    PHASE_D_CANDIDATE_COUNT,
    PHASE_D_LOGICAL_JOB_BUDGET,
    PhaseDAuthority,
)
from minos_engine.baseline.validation_members import VALIDATION_COUNT
from minos_engine.baseline.validation_plan import (
    VALIDATION_PLAN_PARTITION,
    ValidationPlan,
    ValidationPlanConfig,
    ValidationPlanMember,
)
from minos_engine.common.errors import MinosEngineError

if TYPE_CHECKING:
    from sqlalchemy import Connection, Engine

__all__ = [
    "TruthActivationResult",
    "ValidationActivationError",
    "ValidationJobMaterializationResult",
    "activate_l2f2_validation_truth",
    "materialize_l2f2_validation_jobs",
]

#: the canonical pre-execution state of the accepted job state machine (0006).
INITIAL_JOB_STATUS = "PENDING"

_JOBS = "experiments.l2f_experiment_jobs"


class ValidationActivationError(MinosEngineError):
    """The Phase-D campaign cannot be activated as the frozen protocol requires."""


@dataclass(frozen=True, slots=True)
class TruthActivationResult:
    """Identity counts only. Never a digest, never a path, never a payload."""

    plan_hash: str
    member_count: int
    truth_identity_count: int
    created_truth_count: int


@dataclass(frozen=True, slots=True)
class ValidationJobMaterializationResult:
    """What materialization established. ``pending_jobs`` is part of the contract."""

    plan_hash: str
    plan_id: str
    member_count: int
    config_count: int
    logical_job_count: int
    created_jobs: int
    existing_jobs: int
    pending_jobs: int


# ------------------------------------------------------------------------------------------- #
# the persisted campaign, loaded back out of the store that holds it
# ------------------------------------------------------------------------------------------- #
def _authorized_plan(conn: Connection, authority: PhaseDAuthority) -> tuple[str, ValidationPlan]:
    """Resolve the plan the DATABASE's own bootstrap authorizes, then load and verify its graph.

    The bootstrap runs first and its answer selects the plan. A caller-chosen row could be any
    row; the bootstrap's answer is the one the runner would also resolve, which is the only
    plan worth materializing jobs against.
    """
    from sqlalchemy import text

    row = conn.execute(
        text(
            "SELECT plan_hash, execution_environment_hash "
            "  FROM experiments.l2f2_resolve_phase_d_runner_bootstrap()"
        )
    ).one()
    plan_hash, environment = str(row[0]), str(row[1])
    if plan_hash != authority.plan_hash:
        raise ValidationActivationError(
            f"the Phase-D bootstrap resolved plan {plan_hash}, not the frozen {authority.plan_hash}"
        )
    if environment != authority.execution_environment_hash:
        raise ValidationActivationError(
            f"the Phase-D bootstrap resolved environment {environment}, not the frozen "
            f"{authority.execution_environment_hash}"
        )

    plan_id = str(
        conn.execute(
            text("SELECT id FROM experiments.l2f_experiment_plans WHERE plan_hash = :h"),
            {"h": plan_hash},
        ).scalar_one()
    )
    return plan_id, _load_validation_plan(conn, plan_id, authority)


def _load_validation_plan(
    conn: Connection, plan_id: str, authority: PhaseDAuthority
) -> ValidationPlan:
    """Read the persisted graph back and re-verify every part of it against the frozen campaign.

    The bootstrap already refused a graph that is not this campaign. This is the independent
    source-side re-check §15 requires: the two disagree only if something is very wrong, and a
    boundary that trusts one oracle has no way to notice when it is.
    """
    from sqlalchemy import text

    plan = (
        conn.execute(
            text(
                "SELECT id, partition, profile_snapshot_id, snapshot_hash, split_manifest_hash, "
                "       registry_snapshot_hash, gatk_registry_hash, parameter_space_hash, "
                "       experiment_parameter_policy_hash, candidate_set_hash, plan_hash, "
                "       train_member_count, candidate_count, logical_job_count "
                "  FROM experiments.l2f_experiment_plans WHERE id = :p"
            ),
            {"p": plan_id},
        )
        .mappings()
        .one()
    )
    if str(plan["partition"]) != VALIDATION_PLAN_PARTITION:
        raise ValidationActivationError(
            f"the Phase-D plan is partition {plan['partition']!r}, not validation"
        )
    if str(plan["parameter_space_hash"]) != authority.parameter_space_hash:
        raise ValidationActivationError("the Phase-D plan binds a different parameter space")
    if str(plan["candidate_set_hash"]) != authority.phase_c_candidate_set_hash:
        raise ValidationActivationError("the Phase-D plan binds a different candidate set")
    if int(plan["candidate_count"]) != PHASE_D_CANDIDATE_COUNT:
        raise ValidationActivationError(
            f"the Phase-D plan declares {plan['candidate_count']} configurations, "
            f"the frozen protocol fixes {PHASE_D_CANDIDATE_COUNT}"
        )
    if int(plan["train_member_count"]) != VALIDATION_COUNT:
        raise ValidationActivationError(
            f"the Phase-D plan declares {plan['train_member_count']} members, "
            f"the frozen protocol fixes {VALIDATION_COUNT}"
        )
    if int(plan["logical_job_count"]) != PHASE_D_LOGICAL_JOB_BUDGET:
        raise ValidationActivationError(
            f"the Phase-D plan declares {plan['logical_job_count']} logical jobs, "
            f"the frozen protocol fixes {PHASE_D_LOGICAL_JOB_BUDGET}"
        )

    members = _load_members(conn, plan_id, authority, snapshot_id=str(plan["profile_snapshot_id"]))
    configs = _load_configs(conn, plan_id, authority)
    return ValidationPlan(
        plan_hash=str(plan["plan_hash"]),
        profile_snapshot_id=str(plan["profile_snapshot_id"]),
        snapshot_hash=str(plan["snapshot_hash"]),
        split_manifest_hash=str(plan["split_manifest_hash"]),
        registry_snapshot_hash=str(plan["registry_snapshot_hash"]),
        gatk_registry_hash=str(plan["gatk_registry_hash"]),
        parameter_space_hash=str(plan["parameter_space_hash"]),
        experiment_parameter_policy_hash=str(plan["experiment_parameter_policy_hash"]),
        candidate_set_hash=str(plan["candidate_set_hash"]),
        members=members,
        configs=configs,
    )


def _load_members(
    conn: Connection, plan_id: str, authority: PhaseDAuthority, *, snapshot_id: str
) -> tuple[ValidationPlanMember, ...]:
    """The ten members, compared ROW BY ROW against the frozen schedule.

    Counting ten validation members at indices 0..9 says the plan has the right shape. It does
    not say the plan holds the right ten: a store seeded with ten other validation rounds
    satisfies every structural check and is a different experiment.

    So each persisted row is compared to ``authority.schedule.members[i]`` on the identity the
    split manifest froze — dataset, round, chromosome and the upstream identity tuple. The
    persisted ``plan_hash`` is not accepted as a substitute: the database stores that hash as a
    value, and a value cannot vouch for the rows that were supposed to have produced it. This is
    the member-side counterpart of the exact-four configuration check.
    """
    from sqlalchemy import text

    rows = (
        conn.execute(
            text(
                "SELECT pm.member_index, pm.partition, pm.profile_snapshot_id, "
                "       pm.dataset_registry_id, pm.profile_snapshot_member_id, "
                "       pm.bam_profile_id, pm.feature_values_hash, "
                "       dr.dataset_id, dr.round_id, dr.chromosome, dr.identity_tuple_hash, "
                "       sa.partition AS allocation, "
                "       bp.profile_id, bp.content_hash "
                "  FROM experiments.l2f_experiment_plan_members pm "
                "  JOIN catalog.dataset_registry dr ON dr.id = pm.dataset_registry_id "
                "  JOIN catalog.split_allocations sa ON sa.dataset_registry_id = dr.id "
                "  JOIN profiling.bam_profiles bp ON bp.id = pm.bam_profile_id "
                " WHERE pm.plan_id = :p ORDER BY pm.member_index"
            ),
            {"p": plan_id},
        )
        .mappings()
        .all()
    )
    if len(rows) != VALIDATION_COUNT:
        raise ValidationActivationError(
            f"the Phase-D plan holds {len(rows)} members, the frozen protocol fixes "
            f"{VALIDATION_COUNT}"
        )
    if [int(r["member_index"]) for r in rows] != list(range(VALIDATION_COUNT)):
        raise ValidationActivationError(
            "the Phase-D plan member_index inventory is not 0..9 exactly once"
        )
    frozen = {m.member_index: m for m in authority.schedule.members}
    if sorted(frozen) != list(range(VALIDATION_COUNT)):  # pragma: no cover - schedule guarantees
        raise ValidationActivationError("the frozen schedule is not indexed 0..9 exactly once")

    members: list[ValidationPlanMember] = []
    for row in rows:
        # BOTH the plan member's partition and the split's own allocation. The member row is what
        # the plan asserts; the allocation is what the accepted split says. Phase D runs only
        # where they agree, and only where they both say validation.
        if str(row["partition"]) != VALIDATION_PLAN_PARTITION:
            raise ValidationActivationError(
                f"Phase-D plan member {row['dataset_id']} is partition {row['partition']!r}; "
                "validation confirms on the VALIDATION partition only"
            )
        if str(row["allocation"]) != VALIDATION_PLAN_PARTITION:
            raise ValidationActivationError(
                f"Phase-D plan member {row['dataset_id']} is allocated to "
                f"{row['allocation']!r} by the accepted split"
            )
        if str(row["profile_snapshot_id"]) != snapshot_id:
            raise ValidationActivationError(
                f"Phase-D plan member {row['dataset_id']} belongs to a different profile snapshot "
                "than its plan"
            )
        # THE frozen identity check. Every field the split manifest fixes for this index.
        expected = frozen[int(row["member_index"])]
        persisted_identity = {
            "dataset_id": str(row["dataset_id"]),
            "round_id": str(row["round_id"]),
            "chromosome": str(row["chromosome"]),
            "identity_tuple_hash": str(row["identity_tuple_hash"]),
        }
        frozen_identity = {
            "dataset_id": expected.dataset_id,
            "round_id": expected.round_id,
            "chromosome": expected.chromosome,
            "identity_tuple_hash": expected.identity_tuple_hash,
        }
        differing = sorted(
            field for field, value in persisted_identity.items() if value != frozen_identity[field]
        )
        if differing:
            raise ValidationActivationError(
                f"Phase-D plan member at index {row['member_index']} is not the frozen validation "
                f"member: {differing} disagree with the split manifest "
                f"(persisted dataset {persisted_identity['dataset_id']}, frozen "
                f"{frozen_identity['dataset_id']})"
            )
        members.append(
            ValidationPlanMember(
                member_index=int(row["member_index"]),
                dataset_id=str(row["dataset_id"]),
                round_id=str(row["round_id"]),
                chromosome=str(row["chromosome"]),
                dataset_registry_id=str(row["dataset_registry_id"]),
                profile_snapshot_member_id=str(row["profile_snapshot_member_id"]),
                bam_profile_id=str(row["bam_profile_id"]),
                profile_id=str(row["profile_id"]),
                content_hash=str(row["content_hash"]),
                feature_values_hash=str(row["feature_values_hash"]),
            )
        )
    return tuple(members)


def _load_configs(
    conn: Connection, plan_id: str, authority: PhaseDAuthority
) -> tuple[ValidationPlanConfig, ...]:
    """The four configurations, in frozen order, and no others."""
    from sqlalchemy import text

    rows = (
        conn.execute(
            text(
                "SELECT pc.config_index, pc.config_hash, pc.parameter_space_hash, "
                "       cp.media_type, a.uri, a.sha256, a.size_bytes "
                "  FROM experiments.l2f_experiment_plan_configs pc "
                "  JOIN experiments.l2f_config_payloads cp ON cp.id = pc.config_payload_id "
                "  JOIN catalog.artifacts a ON a.id = cp.artifact_id "
                " WHERE pc.plan_id = :p ORDER BY pc.config_index"
            ),
            {"p": plan_id},
        )
        .mappings()
        .all()
    )
    if len(rows) != PHASE_D_CANDIDATE_COUNT:
        raise ValidationActivationError(
            f"the Phase-D plan holds {len(rows)} configurations, the frozen protocol fixes "
            f"{PHASE_D_CANDIDATE_COUNT}"
        )
    if [int(r["config_index"]) for r in rows] != list(range(PHASE_D_CANDIDATE_COUNT)):
        raise ValidationActivationError(
            "the Phase-D plan config_index inventory is not 0..3 exactly once"
        )
    persisted = tuple(str(r["config_hash"]) for r in rows)
    if persisted != tuple(authority.ordered_config_hashes):
        raise ValidationActivationError(
            "the Phase-D plan does not persist the frozen four in frozen order"
        )
    configs: list[ValidationPlanConfig] = []
    for row in rows:
        if str(row["parameter_space_hash"]) != authority.parameter_space_hash:
            raise ValidationActivationError(
                f"Phase-D configuration {row['config_hash']} binds a different parameter space"
            )
        config_hash = str(row["config_hash"])
        # the registered artifact digest for a CONFIG payload IS its config hash. 0006 binds this
        # declaratively; re-asserting it here costs nothing and makes the graph self-describing.
        if str(row["sha256"]) != config_hash:
            raise ValidationActivationError(
                f"the artifact for Phase-D configuration {config_hash} is registered under "
                f"digest {row['sha256']}"
            )
        configs.append(
            ValidationPlanConfig(
                config_index=int(row["config_index"]),
                config_hash=config_hash,
                parameter_space_hash=str(row["parameter_space_hash"]),
                payload_sha256=str(row["sha256"]),
                payload_media_type=str(row["media_type"]),
                payload_uri=str(row["uri"]),
                payload_size_bytes=int(row["size_bytes"]),
                inherited_candidate_index=authority.inherited_candidate_index[config_hash],
            )
        )
    return tuple(configs)


# ------------------------------------------------------------------------------------------- #
# connection authorization — the same shape preparation uses, and no wider
# ------------------------------------------------------------------------------------------- #
def _require_target(conn: Connection, *, database: str, revision: str) -> None:
    from sqlalchemy import text

    live_db = str(conn.execute(text("SELECT current_database()")).scalar_one())
    if live_db != database:
        raise ValidationActivationError(
            f"the validation target connection is attached to {live_db!r}; "
            f"L2-F2-F activation requires {database!r}"
        )
    live_rev = conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one_or_none()
    if live_rev != revision:
        raise ValidationActivationError(
            f"the validation target database is at revision {live_rev!r}, expected {revision!r}"
        )


# ------------------------------------------------------------------------------------------- #
# 1. truth: exactly ten VALIDATION identities, and no other partition
# ------------------------------------------------------------------------------------------- #
def activate_l2f2_validation_truth(
    *,
    target: Engine,
    finalist_freeze_path: str | Path,
    dataset_root: str | Path,
) -> TruthActivationResult:
    """Register and verify THE ten VALIDATION truth identities. Idempotent; conflicts fail closed.

    Registration is delegated to the accepted registrar, not reimplemented: it queries only the
    VALIDATION-only target projection, and persists through ``0022``'s ``SECURITY DEFINER``
    function which re-derives the partition from the accepted split itself. TRAIN and TEST are
    therefore not refused by this module's politeness — they are unreachable through the interface
    it calls.

    What this boundary adds is the Phase-D question the general registrar cannot ask: are these
    the ten members THIS campaign froze, is there exactly one identity each, and is every digest
    present? Truth that is registered but incomplete would let materialization authorize jobs the
    evaluator could never score.

    It also adds the question that must be answered FIRST. Before any truth file is opened or
    hashed, this store must be proven to hold the exact prepared Phase-D campaign: right database,
    right revision, a bootstrap that resolves the frozen plan and environment, and a persisted
    graph whose ten members and four configurations survive re-verification. An unprepared or
    malformed target is refused with the answer key still unread.

    ``dataset_root`` is where the bytes are READ from. No path is ever persisted.
    """
    from minos_engine.storage.l2f2_runner import VALIDATION_DATABASE_NAME, VALIDATION_REVISION

    return _activate_truth_with_trust(
        target=target,
        finalist_freeze_path=finalist_freeze_path,
        dataset_root=Path(dataset_root),
        expected_database=VALIDATION_DATABASE_NAME,
        expected_revision=VALIDATION_REVISION,
    )


def _activate_truth_with_trust(
    *,
    target: Engine,
    finalist_freeze_path: str | Path,
    dataset_root: Path,
    expected_database: str,
    expected_revision: str,
) -> TruthActivationResult:
    """The truth-activation core. Private; store identity is a parameter only here."""
    from minos_engine.evaluation.truth_registration import register_validation_truth_identities

    authority = _authority_from(finalist_freeze_path)

    # THE authorization gate, and it runs before a single truth byte is opened. Truth is the
    # answer key: pointing provisioning at a directory is not a reason to read it. The store must
    # already hold THIS exact prepared campaign — the database's own bootstrap must resolve it,
    # and the persisted graph must re-verify against the frozen ten and the frozen four — before
    # the registrar is allowed to touch the filesystem.
    with target.connect() as conn:
        _require_target(conn, database=expected_database, revision=expected_revision)
        _authorized_plan(conn, authority)

    result = register_validation_truth_identities(
        target, dataset_root=dataset_root, expected_count=VALIDATION_COUNT
    )
    if result.requested != VALIDATION_COUNT:  # pragma: no cover - expected_count enforces this
        raise ValidationActivationError(
            f"registered {result.requested} truth identities, the frozen protocol fixes "
            f"{VALIDATION_COUNT}"
        )

    with target.connect() as conn:
        ready = _truth_readiness(conn, authority)
    return TruthActivationResult(
        plan_hash=authority.plan_hash,
        member_count=VALIDATION_COUNT,
        truth_identity_count=ready,
        created_truth_count=result.created,
    )


def _authority_from(finalist_freeze_path: str | Path) -> PhaseDAuthority:
    from minos_engine.baseline.finalist_freeze import load_finalist_freeze
    from minos_engine.baseline.phase_d import build_l2f2_phase_d_authority
    from minos_engine.storage.l2f2_validation_prepare import (
        ACCEPTED_FINALIST_FREEZE_SHA256,
        ACCEPTED_PHASE_C_CLOSURE_SHA256,
    )

    return build_l2f2_phase_d_authority(
        load_finalist_freeze(
            finalist_freeze_path,
            expected_artifact_sha256=ACCEPTED_FINALIST_FREEZE_SHA256,
            expected_phase_c_closure_sha256=ACCEPTED_PHASE_C_CLOSURE_SHA256,
        )
    )


def _truth_readiness(conn: Connection, authority: PhaseDAuthority) -> int:
    """Exactly one complete truth identity for each EXACT frozen validation round, or a refusal.

    Matching on ``dataset_id`` alone would only prove that truth is attached to a row carrying a
    familiar name. What has to be true is stronger: that this is the truth for THIS exact frozen
    round. So every registered identity is joined back to its registry row and that row is
    compared to the frozen schedule on dataset, round, chromosome and the upstream identity tuple
    — the same identity the plan members are held to.

    Counted against the FROZEN schedule rather than against whatever the store happens to hold,
    so a store with ten identities that are not these ten is not ready.
    """
    from sqlalchemy import text

    frozen = {member.dataset_id: member for member in authority.schedule.members}
    rows = (
        conn.execute(
            text(
                "SELECT dr.dataset_id, dr.round_id, dr.chromosome, dr.identity_tuple_hash, "
                "       sa.partition, "
                "       d.truth_vcf_sha256, d.truth_tbi_sha256, "
                "       d.mutations_vcf_sha256, d.mutations_tbi_sha256 "
                "  FROM evaluation.dataset_evaluation_identity d "
                "  JOIN catalog.dataset_registry dr ON dr.id = d.dataset_registry_id "
                "  JOIN catalog.split_allocations sa ON sa.dataset_registry_id = dr.id "
                " WHERE dr.dataset_id = ANY(:ids)"
            ),
            {"ids": sorted(frozen)},
        )
        .mappings()
        .all()
    )
    seen: set[str] = set()
    for row in rows:
        dataset_id = str(row["dataset_id"])
        if dataset_id in seen:
            raise ValidationActivationError(
                f"validation member {dataset_id} carries more than one truth identity; "
                "activation refuses to choose between them"
            )
        seen.add(dataset_id)
        if str(row["partition"]) != VALIDATION_PLAN_PARTITION:
            raise ValidationActivationError(
                f"truth identity for {dataset_id} is allocated to {row['partition']!r}; "
                "Phase D evaluates the VALIDATION partition only"
            )
        member = frozen[dataset_id]
        differing = sorted(
            field
            for field, persisted, expected_value in (
                ("round_id", str(row["round_id"]), member.round_id),
                ("chromosome", str(row["chromosome"]), member.chromosome),
                (
                    "identity_tuple_hash",
                    str(row["identity_tuple_hash"]),
                    member.identity_tuple_hash,
                ),
            )
            if persisted != expected_value
        )
        if differing:
            raise ValidationActivationError(
                f"the truth identity registered for {dataset_id} belongs to a different round: "
                f"{differing} disagree with the frozen validation schedule"
            )
        missing = [
            column
            for column in (
                "truth_vcf_sha256",
                "truth_tbi_sha256",
                "mutations_vcf_sha256",
                "mutations_tbi_sha256",
            )
            if row[column] is None
        ]
        if missing:
            raise ValidationActivationError(
                f"the truth identity for {dataset_id} is incomplete: {sorted(missing)} absent"
            )
    absent = sorted(set(frozen) - seen)
    if absent:
        raise ValidationActivationError(
            f"{len(absent)} of {VALIDATION_COUNT} validation members have no truth identity "
            f"(first: {absent[0]}); Phase D authorizes no job it could not evaluate"
        )
    return len(seen)


# ------------------------------------------------------------------------------------------- #
# 2. the exact forty jobs
# ------------------------------------------------------------------------------------------- #
def materialize_l2f2_validation_jobs(
    *, target: Engine, finalist_freeze_path: str | Path
) -> ValidationJobMaterializationResult:
    """Materialize THE forty Phase-D jobs. Idempotent; creates nothing it cannot fully create.

    Verifies the store, asks the database's own bootstrap which plan is authorized, re-derives the
    prepared graph and re-checks it against the frozen campaign, requires all ten truth identities,
    and only then writes the exact Cartesian product of ten members and four configurations in one
    transaction.

    Every job is left in the canonical initial state. Nothing here claims, starts, executes,
    evaluates, scores or ranks — those are later authorizations and none of them lives here.
    """
    from minos_engine.storage.l2f2_runner import VALIDATION_DATABASE_NAME, VALIDATION_REVISION

    return _materialize_with_trust(
        target=target,
        finalist_freeze_path=finalist_freeze_path,
        expected_database=VALIDATION_DATABASE_NAME,
        expected_revision=VALIDATION_REVISION,
    )


def _materialize_with_trust(
    *,
    target: Engine,
    finalist_freeze_path: str | Path,
    expected_database: str,
    expected_revision: str,
    fail_after: int | None = None,
) -> ValidationJobMaterializationResult:
    """The materialization core. Private; store identity is a parameter only here.

    ``fail_after`` exists so the atomicity claim can be PROVEN rather than asserted: a proof that
    a partial product rolls back needs a way to interrupt one. It is reachable only through this
    private name and defaults to never failing.
    """
    from sqlalchemy import text

    from minos_engine.experiments.plan import compute_job_key

    authority = _authority_from(finalist_freeze_path)

    with target.connect() as conn, conn.begin():
        _require_target(conn, database=expected_database, revision=expected_revision)
        conn.execute(text("SET LOCAL ROLE minos_admin"))

        plan_id, plan = _authorized_plan(conn, authority)
        _truth_readiness(conn, authority)

        member_ids = _index_map(
            conn, plan_id, "l2f_experiment_plan_members", "member_index", VALIDATION_COUNT
        )
        config_ids = _index_map(
            conn,
            plan_id,
            "l2f_experiment_plan_configs",
            "config_index",
            PHASE_D_CANDIDATE_COUNT,
        )

        # member-major, then config-index: the same enumeration order every earlier phase used.
        wanted: dict[str, tuple[int, int]] = {}
        for member in plan.members:
            for config in plan.configs:
                job_key = compute_job_key(
                    plan_hash=plan.plan_hash,
                    member_index=member.member_index,
                    dataset_id=member.dataset_id,
                    profile_id=member.profile_id,
                    content_hash=member.content_hash,
                    feature_values_hash=member.feature_values_hash,
                    config_index=config.config_index,
                    config_hash=config.config_hash,
                )
                wanted[job_key] = (member.member_index, config.config_index)
        if len(wanted) != PHASE_D_LOGICAL_JOB_BUDGET:  # pragma: no cover - structural guard
            raise ValidationActivationError(
                f"the logical product is {len(wanted)} jobs, the frozen protocol fixes "
                f"{PHASE_D_LOGICAL_JOB_BUDGET}"
            )

        existing = _existing_jobs(conn, plan_id)
        _require_empty_or_complete(existing, wanted)

        created = 0
        for job_key, (member_index, config_index) in wanted.items():
            if job_key in existing:
                continue
            if fail_after is not None and created >= fail_after:
                raise ValidationActivationError(
                    f"deliberate mid-materialization failure after {created} job(s)"
                )
            conn.execute(
                text(
                    f"INSERT INTO {_JOBS} "  # noqa: S608
                    "  (plan_id, plan_member_id, plan_config_id, job_key, status) "
                    "VALUES (:p, :m, :c, :k, :s)"
                ),
                {
                    "p": plan_id,
                    "m": member_ids[member_index],
                    "c": config_ids[config_index],
                    "k": job_key,
                    "s": INITIAL_JOB_STATUS,
                },
            )
            created += 1

        final = _existing_jobs(conn, plan_id)
        if set(final) != set(wanted):  # pragma: no cover - the insert loop makes this unreachable
            raise ValidationActivationError("the materialized job set is not the logical product")
        pending = conn.execute(
            text(
                f"SELECT count(*) FROM {_JOBS} "  # noqa: S608
                " WHERE plan_id = :p AND status = :s AND claimed_by IS NULL "
                "   AND claimed_at IS NULL"
            ),
            {"p": plan_id, "s": INITIAL_JOB_STATUS},
        ).scalar_one()
        if int(pending) != PHASE_D_LOGICAL_JOB_BUDGET:
            raise ValidationActivationError(
                f"{pending} of {PHASE_D_LOGICAL_JOB_BUDGET} Phase-D jobs are unclaimed and "
                f"{INITIAL_JOB_STATUS}; activation ends before execution begins"
            )

        return ValidationJobMaterializationResult(
            plan_hash=plan.plan_hash,
            plan_id=plan_id,
            member_count=len(plan.members),
            config_count=len(plan.configs),
            logical_job_count=PHASE_D_LOGICAL_JOB_BUDGET,
            created_jobs=created,
            existing_jobs=len(existing),
            pending_jobs=int(pending),
        )


def _index_map(
    conn: Connection, plan_id: str, table: str, index_column: str, expected: int
) -> dict[int, str]:
    from sqlalchemy import text

    rows = conn.execute(
        text(
            f"SELECT {index_column}, id FROM experiments.{table} "  # noqa: S608
            " WHERE plan_id = :p"
        ),
        {"p": plan_id},
    ).all()
    resolved = {int(r[0]): str(r[1]) for r in rows}
    if len(resolved) != expected:  # pragma: no cover - the loaders already enforce this
        raise ValidationActivationError(f"expected {expected} rows in {table}, found {len(rows)}")
    return resolved


def _existing_jobs(conn: Connection, plan_id: str) -> dict[str, dict[str, Any]]:
    from sqlalchemy import text

    rows = (
        conn.execute(
            text(
                "SELECT j.job_key, j.status, j.claimed_by, j.claimed_at, "
                "       pm.member_index, pc.config_index "
                f"  FROM {_JOBS} j "  # noqa: S608
                "  JOIN experiments.l2f_experiment_plan_members pm ON pm.id = j.plan_member_id "
                "  JOIN experiments.l2f_experiment_plan_configs pc ON pc.id = j.plan_config_id "
                " WHERE j.plan_id = :p"
            ),
            {"p": plan_id},
        )
        .mappings()
        .all()
    )
    return {str(r["job_key"]): dict(r) for r in rows}


def _require_empty_or_complete(
    existing: dict[str, dict[str, Any]], wanted: dict[str, tuple[int, int]]
) -> None:
    """The persisted job graph must be EMPTY or COMPLETE. There is no third acceptable state.

    Not a prefix. A partial durable graph is not a materialization that got part of the way and
    may be finished later — it is evidence that a previous activation was interrupted or ran
    outside this contract, and completing it would silently convert that evidence into a campaign
    that looks like it was authorized in one piece. Between 1 and 39 jobs is therefore a typed
    refusal requiring an operator to look, not a gap to fill.

    Never repaired. A job is the durable authorization to spend GATK hours on one exact pair, and
    a graph that disagrees with the frozen product is a disagreement about which experiment is
    running.
    """
    unexpected = sorted(set(existing) - set(wanted))
    if unexpected:
        raise ValidationActivationError(
            f"{len(unexpected)} persisted Phase-D job(s) are not in the frozen logical product "
            f"(first job_key {unexpected[0]}); refusing to reconcile a conflicting job graph"
        )
    for job_key, row in existing.items():
        member_index, config_index = wanted[job_key]
        if int(row["member_index"]) != member_index or int(row["config_index"]) != config_index:
            raise ValidationActivationError(
                f"persisted Phase-D job {job_key} binds member {row['member_index']}/config "
                f"{row['config_index']}, the frozen product binds {member_index}/{config_index}"
            )
        if (
            str(row["status"]) != INITIAL_JOB_STATUS
            or row["claimed_by"] is not None
            or row["claimed_at"] is not None
        ):
            raise ValidationActivationError(
                f"persisted Phase-D job {job_key} is {row['status']} and not unclaimed; "
                "activation refuses to extend a campaign that has already begun executing"
            )

    # EMPTY or COMPLETE. Every row above was individually legitimate; the count is the last
    # question, and the one a top-up would answer by writing rather than by refusing.
    if existing and len(existing) != len(wanted):
        raise ValidationActivationError(
            f"the Phase-D plan already holds {len(existing)} of {len(wanted)} jobs; a partial "
            "durable job graph is evidence of an interrupted or out-of-contract activation and "
            "is never completed in place. Activation proceeds from zero jobs or verifies all "
            f"{len(wanted)}"
        )
