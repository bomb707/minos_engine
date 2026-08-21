"""L2-F F3-D non-mutating accepted experiment-harness verifier.

Verifies an **already persisted** F3-C1 plan graph plus any F3-C2 enqueued jobs against the
accepted, repository-owned contracts. It is strictly read-only: it never inserts, updates,
deletes, publishes, repairs, normalizes, migrates, get-or-inserts, takes an advisory lock, or
commits — its read transaction is always rolled back. It is **not** a second persistence path,
and it re-implements neither the plan-hash nor the job-key formula (both are imported from the
frozen F3-B contracts).

``verify_accepted_experiment_harness()`` is the sole production entry point: it takes no
caller-provided plan / candidate set / hashes / database / partition / job keys / trust bundle,
obtains the database only through ``MINOS_DATABASE_URL``, verifies (as the FIRST access on the
exact transaction connection) that it is the canonical operational store at revision ``0006``
before constructing accepted inputs or querying any stage table, builds the plan only through
``build_accepted_experiment_plan()``, and generates + independently verifies the accepted
``CandidateSet`` internally. ``_verify_experiment_harness_with_trust`` is a PRIVATE
explicit-trust boundary for synthetic / non-75 tests only; it is not exported.

Rather than a bare boolean, verification returns a deterministic ordered map of **named checks**
(:data:`CHECK_NAMES`) plus the ordered failure names. Partial enqueue is valid: the result
reports ``missing_job_count`` but never fails merely because fewer than ``logical_job_count``
jobs have been enqueued. Verification fails **closed** when the accepted plan graph is absent or
ambiguous (a typed :class:`HarnessGraphError`).

The comparison core (:func:`_evaluate_checks`) is **pure**: it consumes an immutable
:class:`PersistedGraph` snapshot and the accepted contracts, so constraint-impossible corruptions
can be verified against a controlled immutable representation without ever weakening a production
database constraint.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from sqlalchemy import Connection, Engine, text

from minos_engine.common.canonical_json import canonical_json_bytes
from minos_engine.common.errors import MinosEngineError
from minos_engine.experiments.accepted_plan import build_accepted_experiment_plan
from minos_engine.experiments.candidates import (
    generate_accepted_candidate_set,
    verify_accepted_candidate_set,
)
from minos_engine.experiments.plan import compute_job_key, compute_plan_hash, iter_logical_jobs
from minos_engine.storage.database import create_db_engine, verify_operational_database_identity
from minos_engine.storage.l2f_config_publisher import (
    CONFIG_ARTIFACT_EXTENSION,
    CONFIG_ARTIFACT_KIND,
    CONFIG_ARTIFACT_MEDIA_TYPE,
)
from minos_engine.storage.l2f_migration_contract import L2F_CONFIG_PAYLOAD_SCHEMA
from minos_engine.storage.l2f_plan_store import (
    _file_path_from_uri,
    _norm,
    _plan_member_identity_tuple,
    _require_live_revision,
)

if TYPE_CHECKING:
    from minos_engine.experiments.candidates import CandidateSet
    from minos_engine.experiments.plan import ExperimentPlan

__all__ = [
    "CHECK_NAMES",
    "STATUS_PASS",
    "STATUS_FAIL",
    "HarnessVerificationError",
    "HarnessGraphError",
    "HarnessVerificationResult",
    "verify_accepted_experiment_harness",
]

STATUS_PASS = "PASS"
STATUS_FAIL = "FAIL"

_TRAIN = "train"
_PENDING = "PENDING"
_CLAIMED = "CLAIMED"
_RUNNING = "RUNNING"
#: the only job statuses reachable in F4; SUCCEEDED/FAILED/CANCELLED arrive in F5.
_F4_STATUSES = (_PENDING, _CLAIMED, _RUNNING)
_F4_CLAIMED_STATUSES = (_CLAIMED, _RUNNING)

#: tokens that must never appear in an accepted-boundary artifact's uri/media/provenance —
#: truth, mutation and score-bearing material may not enter the plan/job graph.
_FORBIDDEN_ARTIFACT_TOKENS = (
    "truth",
    "mutation",
    "mutated",
    "score",
    "label",
    "happy",
    "vcf",
    "evaluation",
)

#: the complete, ordered named-check inventory (deterministic; the result map's key order).
CHECK_NAMES: tuple[str, ...] = (
    "plan_identity_self_binding",
    "plan_row_identity_hashes",
    "plan_upstream_uuid_binding",
    "derived_counts",
    "member_inventory_exact",
    "config_inventory_exact",
    "config_payload_bytes_canonical",
    "upstream_membership_exact",
    "no_nontrain_or_truth_data",
    "legacy_tables_excluded",
    "jobs_within_logical_universe",
    "job_keys_recompute",
    "job_member_config_binding",
    "job_uniqueness",
    "job_indices_valid_subset",
    "job_status_claim_consistency",
    "verification_non_mutating",
)


class HarnessVerificationError(MinosEngineError):
    """Base error for F3-D accepted-harness verification."""


class HarnessGraphError(HarnessVerificationError):
    """The accepted plan graph is absent or ambiguous — verification fails closed."""


# --------------------------------------------------------------------------- #
# immutable persisted-state snapshot (the pure evaluator's only DB input)
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class PersistedMember:
    """One persisted plan member, rejoined to its upstream identity."""

    member_index: int
    plan_member_id: str
    profile_snapshot_member_id: str
    feature_matrix_member_id: str
    bam_profile_id: str
    dataset_registry_id: str
    partition: str
    feature_values_hash: str
    dataset_id: str
    profile_id: str
    content_hash: str
    vector_hash: str


@dataclass(frozen=True)
class PersistedConfig:
    """One persisted plan config."""

    config_index: int
    plan_config_id: str
    config_payload_id: str
    config_hash: str
    parameter_space_hash: str


@dataclass(frozen=True)
class PersistedPayload:
    """One persisted CONFIG payload joined to its content-addressed artifact + file bytes."""

    config_payload_id: str
    config_hash: str
    parameter_space_hash: str
    schema_version: str
    media_type: str
    artifact_id: str
    artifact_sha256: str
    artifact_uri: str
    artifact_size_bytes: int | None
    artifact_provenance: str | None
    file_sha256: str | None
    file_size_bytes: int | None


@dataclass(frozen=True)
class PersistedJob:
    """One persisted logical job (F3-C2), joined to its bound member/config indices."""

    job_id: str
    job_key: str
    status: str
    claimed_by: str | None
    claimed_at_is_null: bool
    plan_member_id: str
    plan_config_id: str
    member_index: int | None
    config_index: int | None


@dataclass(frozen=True)
class UpstreamMember:
    """One live upstream train member (snapshot ⋈ dataset_registry ⋈ bam_profiles ⋈ matrix)."""

    dataset_id: str
    profile_id: str
    content_hash: str
    snapshot_feature_values_hash: str
    matrix_feature_values_hash: str
    vector_hash: str
    member_index: int


@dataclass(frozen=True)
class PersistedGraph:
    """An immutable snapshot of everything the pure evaluator compares against.

    Constructed from the database by :func:`_read_persisted_graph`; tests may construct it
    directly as a *controlled immutable representation* to exercise corruptions that production
    database constraints make unconstructable.
    """

    plan_id: str
    plan_row: Mapping[str, Any]
    upstream_ids: Mapping[str, str]
    members: tuple[PersistedMember, ...]
    configs: tuple[PersistedConfig, ...]
    payloads: tuple[PersistedPayload, ...]
    jobs: tuple[PersistedJob, ...]
    upstream_train: tuple[UpstreamMember, ...]
    upstream_nontrain_dataset_ids: tuple[str, ...]
    matrix_row_count: int
    legacy_profile_overlap: int
    legacy_gatk_config_overlap: int
    fingerprint_before: str
    fingerprint_after: str


@dataclass(frozen=True)
class HarnessVerificationResult:
    """Deterministic named-check verification outcome (never a bare boolean)."""

    status: str
    plan_hash: str
    candidate_set_hash: str
    logical_job_count: int
    persisted_job_count: int
    missing_job_count: int
    checks: Mapping[str, bool]
    failures: tuple[str, ...]

    @property
    def ok(self) -> bool:
        return self.status == STATUS_PASS


# --------------------------------------------------------------------------- #
# pure evaluation core
# --------------------------------------------------------------------------- #
def _payload_by_id(graph: PersistedGraph) -> dict[str, PersistedPayload]:
    return {p.config_payload_id: p for p in graph.payloads}


def _check_plan_identity_self_binding(plan: Any) -> bool:
    """The accepted plan's ``plan_hash`` must bind its own content under the frozen formula."""
    return bool(
        plan.plan_hash
        == compute_plan_hash(
            schema_version=plan.schema_version,
            epoch=plan.epoch,
            snapshot_hash=plan.snapshot_hash,
            split_manifest_hash=plan.split_manifest_hash,
            registry_snapshot_hash=plan.registry_snapshot_hash,
            train_matrix_hash=plan.train_matrix_hash,
            train_feature_view_hash=plan.train_feature_view_hash,
            feature_set_hash=plan.feature_set_hash,
            feature_registry_hash=plan.feature_registry_hash,
            gatk_registry_hash=plan.gatk_registry_hash,
            parameter_space_hash=plan.parameter_space_hash,
            experiment_parameter_policy_hash=plan.experiment_parameter_policy_hash,
            candidate_set_hash=plan.candidate_set_hash,
            members=plan.members,
            configs=plan.configs,
            train_member_count=plan.train_member_count,
            candidate_count=plan.candidate_count,
            logical_job_count=plan.logical_job_count,
        )
    )


_PLAN_IDENTITY_COLUMNS = (
    "snapshot_hash",
    "split_manifest_hash",
    "registry_snapshot_hash",
    "train_matrix_hash",
    "train_feature_view_hash",
    "feature_set_hash",
    "feature_registry_hash",
    "gatk_registry_hash",
    "parameter_space_hash",
    "experiment_parameter_policy_hash",
    "candidate_set_hash",
)


def _check_plan_row_identity_hashes(plan: Any, graph: PersistedGraph) -> bool:
    row = graph.plan_row
    if _norm(row.get("plan_hash")) != _norm(plan.plan_hash):
        return False
    if _norm(row.get("partition")) != _TRAIN:
        return False
    return all(_norm(row.get(col)) == _norm(getattr(plan, col)) for col in _PLAN_IDENTITY_COLUMNS)


def _check_plan_upstream_uuid_binding(graph: PersistedGraph) -> bool:
    row = graph.plan_row
    ids = graph.upstream_ids
    return all(
        _norm(row.get(col)) == _norm(ids.get(col))
        for col in ("profile_snapshot_id", "train_feature_matrix_id", "feature_set_id")
    )


def _check_derived_counts(plan: Any, graph: PersistedGraph) -> bool:
    row = graph.plan_row
    if int(row.get("train_member_count", -1)) != plan.train_member_count:
        return False
    if int(row.get("candidate_count", -1)) != plan.candidate_count:
        return False
    if int(row.get("logical_job_count", -1)) != plan.logical_job_count:
        return False
    if plan.logical_job_count != plan.train_member_count * plan.candidate_count:
        return False
    return bool(
        len(graph.members) == plan.train_member_count
        and len(graph.configs) == plan.candidate_count
        and len({c.config_payload_id for c in graph.configs}) == plan.candidate_count
    )


def _check_member_inventory_exact(plan: Any, graph: PersistedGraph) -> bool:
    if len(graph.members) != len(plan.members):
        return False
    for i, (stored, expected) in enumerate(zip(graph.members, plan.members, strict=True)):
        if stored.member_index != i or expected.member_index != i:
            return False
        live = (
            stored.dataset_id,
            stored.profile_id,
            stored.content_hash,
            stored.feature_values_hash,
            stored.vector_hash,
            stored.member_index,
            stored.partition,
        )
        if live != _plan_member_identity_tuple(expected):
            return False
    return True


def _check_config_inventory_exact(plan: Any, candidate_set: Any, graph: PersistedGraph) -> bool:
    if len(graph.configs) != len(plan.configs) or len(graph.configs) != len(candidate_set.configs):
        return False
    triples = zip(graph.configs, plan.configs, candidate_set.configs, strict=True)
    for i, (stored, expected, cand) in enumerate(triples):
        if stored.config_index != i or expected.config_index != i:
            return False
        if _norm(stored.config_hash) != _norm(expected.config_hash):
            return False
        if expected.config_hash != cand.config_hash:  # accepted candidate order binding
            return False
        if _norm(stored.parameter_space_hash) != _norm(plan.parameter_space_hash):
            return False
    return True


def _check_config_payload_bytes_canonical(
    plan: Any, candidate_set: Any, graph: PersistedGraph
) -> bool:
    payloads = _payload_by_id(graph)
    if len(payloads) != len(candidate_set.configs):
        return False
    for stored, cand in zip(graph.configs, candidate_set.configs, strict=True):
        payload = payloads.get(stored.config_payload_id)
        if payload is None:
            return False
        canonical = canonical_json_bytes(cand.effective_config)
        expected_sha = hashlib.sha256(canonical).hexdigest()
        if expected_sha != cand.config_hash:  # the candidate's own byte↔hash binding
            return False
        if _norm(payload.config_hash) != _norm(cand.config_hash):
            return False
        if _norm(payload.parameter_space_hash) != _norm(plan.parameter_space_hash):
            return False
        if payload.schema_version != L2F_CONFIG_PAYLOAD_SCHEMA:
            return False
        if payload.media_type != CONFIG_ARTIFACT_MEDIA_TYPE:
            return False
        if _norm(payload.artifact_sha256) != _norm(cand.config_hash):
            return False
        if payload.artifact_provenance != CONFIG_ARTIFACT_KIND:
            return False
        # the artifact URI must be the content-addressed <config_hash>.json path.
        if not payload.artifact_uri.endswith(f"/{cand.config_hash}{CONFIG_ARTIFACT_EXTENSION}"):
            return False
        if payload.artifact_size_bytes is None or payload.artifact_size_bytes != len(canonical):
            return False
        if payload.file_sha256 != cand.config_hash:  # exact canonical bytes on disk
            return False
        if payload.file_size_bytes != len(canonical):
            return False
    return True


def _check_upstream_membership_exact(plan: Any, graph: PersistedGraph) -> bool:
    """The live upstream train inventory must equal the accepted plan inventory exactly."""
    n = plan.train_member_count
    if len(graph.upstream_train) != n or graph.matrix_row_count != n:
        return False
    by_ds = {u.dataset_id: u for u in graph.upstream_train}
    if len(by_ds) != n:  # duplicated logical identity upstream
        return False
    if set(by_ds) != {m.dataset_id for m in plan.members}:
        return False
    if {u.member_index for u in graph.upstream_train} != {m.member_index for m in plan.members}:
        return False
    for m in plan.members:
        u = by_ds[m.dataset_id]
        live = (
            u.dataset_id,
            u.profile_id,
            u.content_hash,
            u.snapshot_feature_values_hash,
            u.vector_hash,
            u.member_index,
            _TRAIN,
        )
        if live != _plan_member_identity_tuple(m):
            return False
        if _norm(u.snapshot_feature_values_hash) != _norm(u.matrix_feature_values_hash):
            return False
    return True


def _artifact_carries_forbidden_token(payload: PersistedPayload) -> bool:
    haystack = " ".join(
        str(v or "").lower()
        for v in (payload.artifact_uri, payload.artifact_provenance, payload.media_type)
    )
    return any(token in haystack for token in _FORBIDDEN_ARTIFACT_TOKENS)


def _check_no_nontrain_or_truth_data(plan: Any, graph: PersistedGraph) -> bool:
    """No validation/test partition membership and no truth/mutation/score material may enter."""
    if any(m.partition != _TRAIN for m in graph.members):
        return False
    plan_datasets = {m.dataset_id for m in plan.members}
    if plan_datasets & set(graph.upstream_nontrain_dataset_ids):
        return False
    return not any(_artifact_carries_forbidden_token(p) for p in graph.payloads)


def _check_legacy_tables_excluded(graph: PersistedGraph) -> bool:
    return graph.legacy_profile_overlap == 0 and graph.legacy_gatk_config_overlap == 0


def _check_jobs_within_logical_universe(graph: PersistedGraph, universe: Mapping[str, int]) -> bool:
    return all(job.job_key in universe for job in graph.jobs)


def _check_job_keys_recompute(plan: Any, graph: PersistedGraph) -> bool:
    """Every stored job_key must recompute from the plan identity + its BOUND member/config."""
    members_by_index = {m.member_index: m for m in plan.members}
    configs_by_index = {c.config_index: c for c in plan.configs}
    for job in graph.jobs:
        if job.member_index is None or job.config_index is None:
            return False
        member = members_by_index.get(job.member_index)
        config = configs_by_index.get(job.config_index)
        if member is None or config is None:
            return False
        recomputed = compute_job_key(
            plan_hash=plan.plan_hash,
            member_index=member.member_index,
            dataset_id=member.dataset_id,
            profile_id=member.profile_id,
            content_hash=member.content_hash,
            feature_values_hash=member.feature_values_hash,
            config_index=config.config_index,
            config_hash=config.config_hash,
        )
        if recomputed != job.job_key:
            return False
    return True


def _check_job_member_config_binding(
    graph: PersistedGraph, universe_positions: Mapping[str, tuple[int, int]]
) -> bool:
    """A job's plan_member_id/plan_config_id must be the member/config encoded by its job_key."""
    member_id_by_index = {m.member_index: m.plan_member_id for m in graph.members}
    config_id_by_index = {c.config_index: c.plan_config_id for c in graph.configs}
    for job in graph.jobs:
        encoded = universe_positions.get(job.job_key)
        if encoded is None:
            return False
        member_index, config_index = encoded
        if job.member_index != member_index or job.config_index != config_index:
            return False
        if _norm(job.plan_member_id) != _norm(member_id_by_index.get(member_index)):
            return False
        if _norm(job.plan_config_id) != _norm(config_id_by_index.get(config_index)):
            return False
    return True


def _check_job_uniqueness(graph: PersistedGraph) -> bool:
    keys = [job.job_key for job in graph.jobs]
    logical = [(job.plan_member_id, job.plan_config_id) for job in graph.jobs]
    return len(set(keys)) == len(keys) and len(set(logical)) == len(logical)


def _check_job_indices_valid_subset(
    plan: Any, graph: PersistedGraph, universe: Mapping[str, int]
) -> bool:
    positions = set()
    for job in graph.jobs:
        pos = universe.get(job.job_key)
        if pos is None or not (0 <= pos < plan.logical_job_count):
            return False
        positions.add(pos)
    if len(positions) != len(graph.jobs):
        return False
    for job in graph.jobs:
        if job.member_index is None or job.config_index is None:
            return False
        if not (0 <= job.member_index < plan.train_member_count):
            return False
        if not (0 <= job.config_index < plan.candidate_count):
            return False
    return True


def _check_job_status_claim_consistency(graph: PersistedGraph) -> bool:
    """Every job's status must be an F4-reachable state whose claim metadata is consistent.

    * ``PENDING``  — no claim metadata at all (``claimed_by`` and ``claimed_at`` both absent).
    * ``CLAIMED``  — a non-empty worker identity AND a ``claimed_at``.
    * ``RUNNING``  — a non-empty worker identity AND a ``claimed_at``.
    * ``SUCCEEDED`` / ``FAILED`` / ``CANCELLED`` — terminal states are unreachable during F4
      (they arrive with F5 execution/results), so any job in one is invalid here.
    """
    for job in graph.jobs:
        if job.status not in _F4_STATUSES:
            return False
        if job.status == _PENDING:
            if job.claimed_by is not None or not job.claimed_at_is_null:
                return False
        elif job.status in _F4_CLAIMED_STATUSES:
            if job.claimed_by is None or not job.claimed_by.strip():
                return False
            if job.claimed_at_is_null:
                return False
    return True


def _evaluate_checks(
    plan: Any, candidate_set: Any, graph: PersistedGraph
) -> tuple[dict[str, bool], tuple[str, ...]]:
    """PURE evaluation of every named check against an immutable persisted snapshot.

    Every check that can safely be evaluated IS evaluated (no early exit), so the caller receives
    the complete ordered set of failures rather than only the first.
    """
    jobs = list(iter_logical_jobs(plan))
    universe = {job.job_key: i for i, job in enumerate(jobs)}
    universe_positions = {job.job_key: (job.member_index, job.config_index) for job in jobs}

    results: dict[str, bool] = {
        "plan_identity_self_binding": _check_plan_identity_self_binding(plan),
        "plan_row_identity_hashes": _check_plan_row_identity_hashes(plan, graph),
        "plan_upstream_uuid_binding": _check_plan_upstream_uuid_binding(graph),
        "derived_counts": _check_derived_counts(plan, graph),
        "member_inventory_exact": _check_member_inventory_exact(plan, graph),
        "config_inventory_exact": _check_config_inventory_exact(plan, candidate_set, graph),
        "config_payload_bytes_canonical": _check_config_payload_bytes_canonical(
            plan, candidate_set, graph
        ),
        "upstream_membership_exact": _check_upstream_membership_exact(plan, graph),
        "no_nontrain_or_truth_data": _check_no_nontrain_or_truth_data(plan, graph),
        "legacy_tables_excluded": _check_legacy_tables_excluded(graph),
        "jobs_within_logical_universe": _check_jobs_within_logical_universe(graph, universe),
        "job_keys_recompute": _check_job_keys_recompute(plan, graph),
        "job_member_config_binding": _check_job_member_config_binding(graph, universe_positions),
        "job_uniqueness": _check_job_uniqueness(graph),
        "job_indices_valid_subset": _check_job_indices_valid_subset(plan, graph, universe),
        "job_status_claim_consistency": _check_job_status_claim_consistency(graph),
        "verification_non_mutating": graph.fingerprint_before == graph.fingerprint_after,
    }
    checks = {name: results[name] for name in CHECK_NAMES}  # deterministic order
    failures = tuple(name for name in CHECK_NAMES if not checks[name])
    return checks, failures


def _build_result(
    plan: Any, candidate_set: Any, graph: PersistedGraph
) -> HarnessVerificationResult:
    checks, failures = _evaluate_checks(plan, candidate_set, graph)
    universe = {job.job_key for job in iter_logical_jobs(plan)}
    # partial enqueue is VALID: missing jobs are reported, never a failure by themselves.
    persisted_valid = len({job.job_key for job in graph.jobs} & universe)
    return HarnessVerificationResult(
        status=STATUS_PASS if not failures else STATUS_FAIL,
        plan_hash=plan.plan_hash,
        candidate_set_hash=plan.candidate_set_hash,
        logical_job_count=plan.logical_job_count,
        persisted_job_count=len(graph.jobs),
        missing_job_count=plan.logical_job_count - persisted_valid,
        checks=checks,
        failures=failures,
    )


# --------------------------------------------------------------------------- #
# read-only database snapshot
# --------------------------------------------------------------------------- #
def _state_fingerprint(conn: Connection, plan_id: str, uris: tuple[str, ...]) -> str:
    """A digest over every row count, timestamp, status/claim field and artifact file that
    verification must leave untouched (the non-mutation self-check's evidence)."""
    parts: list[str] = []
    for table in (
        "l2f_experiment_plans",
        "l2f_experiment_plan_members",
        "l2f_config_payloads",
        "l2f_experiment_plan_configs",
        "l2f_experiment_jobs",
    ):
        n = conn.execute(text(f"SELECT count(*) FROM experiments.{table}")).scalar_one()  # noqa: S608
        parts.append(f"{table}={n}")
    parts.append(
        "artifacts="
        + str(conn.execute(text("SELECT count(*) FROM catalog.artifacts")).scalar_one())
    )
    jobs = conn.execute(
        text(
            "SELECT id, job_key, status, claimed_by, claimed_at, created_at, updated_at "
            "FROM experiments.l2f_experiment_jobs WHERE plan_id = :p ORDER BY id"
        ),
        {"p": plan_id},
    ).all()
    parts.extend("|".join(str(v) for v in row) for row in jobs)
    stamps = conn.execute(
        text(
            "SELECT 'p'||id||created_at FROM experiments.l2f_experiment_plans "
            "UNION ALL SELECT 'm'||id||created_at FROM experiments.l2f_experiment_plan_members "
            "UNION ALL SELECT 'c'||id||created_at FROM experiments.l2f_experiment_plan_configs "
            "UNION ALL SELECT 'y'||id||created_at FROM experiments.l2f_config_payloads "
            "ORDER BY 1"
        )
    ).all()
    parts.extend(str(row[0]) for row in stamps)
    for uri in uris:
        try:
            data = _file_path_from_uri(uri).read_bytes()
            parts.append(f"{uri}={hashlib.sha256(data).hexdigest()}")
        except (OSError, MinosEngineError):
            parts.append(f"{uri}=<unreadable>")
    return hashlib.sha256("\n".join(parts).encode()).hexdigest()


def _resolve_accepted_plan_row(conn: Connection, plan: Any) -> Mapping[str, Any]:
    """Resolve the persisted plan by its COMPLETE logical identity (never by plan_hash alone, so
    a forged plan_hash surfaces as a named check rather than a silent miss). Absent or ambiguous
    fails closed."""
    where = " AND ".join(f"{col} = :{col}" for col in _PLAN_IDENTITY_COLUMNS)
    params = {col: getattr(plan, col) for col in _PLAN_IDENTITY_COLUMNS}
    rows = (
        conn.execute(
            text(f"SELECT * FROM experiments.l2f_experiment_plans WHERE {where}"),  # noqa: S608
            params,
        )
        .mappings()
        .all()
    )
    if len(rows) != 1:
        raise HarnessGraphError(
            f"accepted plan graph is absent or ambiguous: {len(rows)} rows match the complete "
            "logical identity (verification fails closed and never persists or repairs)"
        )
    return dict(rows[0])


def _read_upstream_ids(conn: Connection, plan: Any) -> dict[str, str]:
    fs = conn.execute(
        text(
            "SELECT id FROM profiling.feature_sets WHERE feature_set_hash = :f AND registry_hash = :r"
        ),
        {"f": plan.feature_set_hash, "r": plan.feature_registry_hash},
    ).scalar_one_or_none()
    snap = conn.execute(
        text(
            "SELECT id FROM profiling.profile_snapshots WHERE snapshot_hash = :s "
            "AND split_manifest_hash = :sm AND registry_snapshot_hash = :rs"
        ),
        {
            "s": plan.snapshot_hash,
            "sm": plan.split_manifest_hash,
            "rs": plan.registry_snapshot_hash,
        },
    ).scalar_one_or_none()
    if fs is None or snap is None:
        raise HarnessGraphError("accepted upstream feature set / profile snapshot is absent")
    mat = conn.execute(
        text(
            "SELECT id FROM profiling.feature_matrices WHERE profile_snapshot_id = :s "
            "AND partition = :p AND matrix_hash = :m AND feature_set_id = :f"
        ),
        {"s": snap, "p": _TRAIN, "m": plan.train_matrix_hash, "f": fs},
    ).scalar_one_or_none()
    if mat is None:
        raise HarnessGraphError("accepted upstream train feature matrix is absent")
    return {
        "feature_set_id": str(fs),
        "profile_snapshot_id": str(snap),
        "train_feature_matrix_id": str(mat),
    }


def _read_members(conn: Connection, plan_id: str) -> tuple[PersistedMember, ...]:
    rows = (
        conn.execute(
            text(
                "SELECT pm.member_index, pm.id AS plan_member_id, pm.profile_snapshot_member_id, "
                "pm.feature_matrix_member_id, pm.bam_profile_id, pm.dataset_registry_id, "
                "pm.partition, pm.feature_values_hash, dr.dataset_id, bp.profile_id, "
                "bp.content_hash, fmm.vector_hash "
                "FROM experiments.l2f_experiment_plan_members pm "
                "JOIN catalog.dataset_registry dr ON dr.id = pm.dataset_registry_id "
                "JOIN profiling.bam_profiles bp ON bp.id = pm.bam_profile_id "
                "JOIN profiling.feature_matrix_members fmm ON fmm.id = pm.feature_matrix_member_id "
                "WHERE pm.plan_id = :p ORDER BY pm.member_index"
            ),
            {"p": plan_id},
        )
        .mappings()
        .all()
    )
    return tuple(
        PersistedMember(
            member_index=int(r["member_index"]),
            plan_member_id=str(r["plan_member_id"]),
            profile_snapshot_member_id=str(r["profile_snapshot_member_id"]),
            feature_matrix_member_id=str(r["feature_matrix_member_id"]),
            bam_profile_id=str(r["bam_profile_id"]),
            dataset_registry_id=str(r["dataset_registry_id"]),
            partition=str(r["partition"]),
            feature_values_hash=str(r["feature_values_hash"]),
            dataset_id=str(r["dataset_id"]),
            profile_id=str(r["profile_id"]),
            content_hash=str(r["content_hash"]),
            vector_hash=str(r["vector_hash"]),
        )
        for r in rows
    )


def _read_configs(conn: Connection, plan_id: str) -> tuple[PersistedConfig, ...]:
    rows = (
        conn.execute(
            text(
                "SELECT config_index, id AS plan_config_id, config_payload_id, config_hash, "
                "parameter_space_hash FROM experiments.l2f_experiment_plan_configs "
                "WHERE plan_id = :p ORDER BY config_index"
            ),
            {"p": plan_id},
        )
        .mappings()
        .all()
    )
    return tuple(
        PersistedConfig(
            config_index=int(r["config_index"]),
            plan_config_id=str(r["plan_config_id"]),
            config_payload_id=str(r["config_payload_id"]),
            config_hash=str(r["config_hash"]),
            parameter_space_hash=str(r["parameter_space_hash"]),
        )
        for r in rows
    )


def _read_payloads(conn: Connection, payload_ids: tuple[str, ...]) -> tuple[PersistedPayload, ...]:
    if not payload_ids:
        return ()
    rows = (
        conn.execute(
            text(
                "SELECT cp.id AS config_payload_id, cp.config_hash, cp.parameter_space_hash, "
                "cp.schema_version, cp.media_type, cp.artifact_id, a.sha256 AS artifact_sha256, "
                "a.uri AS artifact_uri, a.size_bytes AS artifact_size_bytes, "
                "a.provenance AS artifact_provenance "
                "FROM experiments.l2f_config_payloads cp "
                "JOIN catalog.artifacts a ON a.id = cp.artifact_id "
                "WHERE cp.id = ANY(:ids)"
            ),
            {"ids": list(payload_ids)},
        )
        .mappings()
        .all()
    )
    payloads: list[PersistedPayload] = []
    for r in rows:
        file_sha: str | None = None
        file_size: int | None = None
        try:
            data = _file_path_from_uri(str(r["artifact_uri"])).read_bytes()
            file_sha = hashlib.sha256(data).hexdigest()
            file_size = len(data)
        except (OSError, MinosEngineError):
            file_sha = None
            file_size = None
        size = r["artifact_size_bytes"]
        payloads.append(
            PersistedPayload(
                config_payload_id=str(r["config_payload_id"]),
                config_hash=str(r["config_hash"]),
                parameter_space_hash=str(r["parameter_space_hash"]),
                schema_version=str(r["schema_version"]),
                media_type=str(r["media_type"]),
                artifact_id=str(r["artifact_id"]),
                artifact_sha256=str(r["artifact_sha256"]),
                artifact_uri=str(r["artifact_uri"]),
                artifact_size_bytes=None if size is None else int(size),
                artifact_provenance=None
                if r["artifact_provenance"] is None
                else str(r["artifact_provenance"]),
                file_sha256=file_sha,
                file_size_bytes=file_size,
            )
        )
    return tuple(payloads)


def _read_jobs(conn: Connection, plan_id: str) -> tuple[PersistedJob, ...]:
    rows = (
        conn.execute(
            text(
                "SELECT j.id AS job_id, j.job_key, j.status, j.claimed_by, "
                "(j.claimed_at IS NULL) AS claimed_at_is_null, j.plan_member_id, j.plan_config_id, "
                "pm.member_index, pc.config_index "
                "FROM experiments.l2f_experiment_jobs j "
                "LEFT JOIN experiments.l2f_experiment_plan_members pm ON pm.id = j.plan_member_id "
                "LEFT JOIN experiments.l2f_experiment_plan_configs pc ON pc.id = j.plan_config_id "
                "WHERE j.plan_id = :p ORDER BY j.job_key"
            ),
            {"p": plan_id},
        )
        .mappings()
        .all()
    )
    return tuple(
        PersistedJob(
            job_id=str(r["job_id"]),
            job_key=str(r["job_key"]),
            status=str(r["status"]),
            claimed_by=None if r["claimed_by"] is None else str(r["claimed_by"]),
            claimed_at_is_null=bool(r["claimed_at_is_null"]),
            plan_member_id=str(r["plan_member_id"]),
            plan_config_id=str(r["plan_config_id"]),
            member_index=None if r["member_index"] is None else int(r["member_index"]),
            config_index=None if r["config_index"] is None else int(r["config_index"]),
        )
        for r in rows
    )


def _read_upstream_inventory(
    conn: Connection, snapshot_id: str, matrix_id: str
) -> tuple[tuple[UpstreamMember, ...], tuple[str, ...], int]:
    rows = (
        conn.execute(
            text(
                "SELECT dr.dataset_id, bp.profile_id, bp.content_hash, "
                "psm.feature_values_hash AS snapshot_fvh, fmm.feature_values_hash AS matrix_fvh, "
                "fmm.vector_hash, fmm.member_index "
                "FROM profiling.profile_snapshot_members psm "
                "JOIN catalog.dataset_registry dr ON dr.id = psm.dataset_registry_id "
                "JOIN profiling.bam_profiles bp ON bp.id = psm.bam_profile_id "
                "JOIN profiling.feature_matrix_members fmm "
                "  ON fmm.dataset_registry_id = psm.dataset_registry_id "
                " AND fmm.feature_matrix_id = :mid "
                "WHERE psm.profile_snapshot_id = :sid AND psm.partition = :p"
            ),
            {"sid": snapshot_id, "mid": matrix_id, "p": _TRAIN},
        )
        .mappings()
        .all()
    )
    train = tuple(
        UpstreamMember(
            dataset_id=str(r["dataset_id"]),
            profile_id=str(r["profile_id"]),
            content_hash=str(r["content_hash"]),
            snapshot_feature_values_hash=str(r["snapshot_fvh"]),
            matrix_feature_values_hash=str(r["matrix_fvh"]),
            vector_hash=str(r["vector_hash"]),
            member_index=int(r["member_index"]),
        )
        for r in rows
    )
    nontrain = tuple(
        str(r[0])
        for r in conn.execute(
            text(
                "SELECT dr.dataset_id FROM profiling.profile_snapshot_members psm "
                "JOIN catalog.dataset_registry dr ON dr.id = psm.dataset_registry_id "
                "WHERE psm.profile_snapshot_id = :sid AND psm.partition <> :p"
            ),
            {"sid": snapshot_id, "p": _TRAIN},
        ).all()
    )
    row_count = int(
        conn.execute(
            text("SELECT row_count FROM profiling.feature_matrices WHERE id = :m"),
            {"m": matrix_id},
        ).scalar_one()
    )
    return train, nontrain, row_count


def _read_legacy_overlap(
    conn: Connection, graph_members: tuple[PersistedMember, ...], config_hashes: tuple[str, ...]
) -> tuple[int, int]:
    """Legacy L2-B tables must contribute nothing to the accepted graph."""
    profile_overlap = 0
    if graph_members:
        profile_overlap = int(
            conn.execute(
                text("SELECT count(*) FROM profiling.profiles WHERE id = ANY(:ids)"),
                {"ids": [m.bam_profile_id for m in graph_members]},
            ).scalar_one()
        )
    gatk_overlap = 0
    if config_hashes:
        gatk_overlap = int(
            conn.execute(
                text("SELECT count(*) FROM catalog.gatk_configs WHERE config_hash = ANY(:h)"),
                {"h": list(config_hashes)},
            ).scalar_one()
        )
    return profile_overlap, gatk_overlap


def _read_persisted_graph(conn: Connection, plan: Any) -> PersistedGraph:
    """Read the complete persisted graph. Strictly read-only (SELECT statements only)."""
    plan_row = _resolve_accepted_plan_row(conn, plan)
    plan_id = str(plan_row["id"])
    upstream_ids = _read_upstream_ids(conn, plan)
    members = _read_members(conn, plan_id)
    configs = _read_configs(conn, plan_id)
    payloads = _read_payloads(conn, tuple(c.config_payload_id for c in configs))
    uris = tuple(sorted(p.artifact_uri for p in payloads))

    fingerprint_before = _state_fingerprint(conn, plan_id, uris)
    jobs = _read_jobs(conn, plan_id)
    train, nontrain, row_count = _read_upstream_inventory(
        conn, upstream_ids["profile_snapshot_id"], upstream_ids["train_feature_matrix_id"]
    )
    legacy_profile, legacy_gatk = _read_legacy_overlap(
        conn, members, tuple(c.config_hash for c in configs)
    )
    fingerprint_after = _state_fingerprint(conn, plan_id, uris)

    return PersistedGraph(
        plan_id=plan_id,
        plan_row=plan_row,
        upstream_ids=upstream_ids,
        members=members,
        configs=configs,
        payloads=payloads,
        jobs=jobs,
        upstream_train=train,
        upstream_nontrain_dataset_ids=nontrain,
        matrix_row_count=row_count,
        legacy_profile_overlap=legacy_profile,
        legacy_gatk_config_overlap=legacy_gatk,
        fingerprint_before=fingerprint_before,
        fingerprint_after=fingerprint_after,
    )


def _verify_in_read_transaction(
    engine: Engine, plan: Any, candidate_set: Any, *, verify_identity: bool
) -> HarnessVerificationResult:
    """Open a read-only transaction, snapshot the graph, evaluate the pure checks, ROLL BACK.

    The transaction is always rolled back — verification never commits, so it cannot durably
    change any row, timestamp, status, claim field or file.
    """
    conn = engine.connect()
    trans = conn.begin()
    try:
        if verify_identity:
            verify_operational_database_identity(conn)
            _require_live_revision(conn)
        graph = _read_persisted_graph(conn, plan)
        return _build_result(plan, candidate_set, graph)
    finally:
        trans.rollback()  # never commit: verification is strictly non-mutating
        conn.close()


def _build_accepted_candidate_set() -> CandidateSet:
    candidate_set = generate_accepted_candidate_set()
    verify_accepted_candidate_set(candidate_set)
    return candidate_set


def verify_accepted_experiment_harness() -> HarnessVerificationResult:
    """THE accepted F3-D verification entry point — no caller-provided trust, inputs or database.

    Verifies the already-persisted accepted harness (F3-C1 graph + any F3-C2 jobs) against the
    repository-owned contracts and returns a deterministic named-check result. It never inserts,
    updates, deletes, publishes, repairs, normalizes or migrates anything, and partial job
    coverage is valid (reported via ``missing_job_count``, never a failure).
    """
    engine = create_db_engine()
    try:
        conn = engine.connect()
        trans = conn.begin()
        try:
            # identity + revision are the FIRST accesses on this exact connection, before the
            # accepted plan is constructed and before any stage table is queried.
            verify_operational_database_identity(conn)
            _require_live_revision(conn)
            plan = build_accepted_experiment_plan()
            candidate_set = _build_accepted_candidate_set()
            graph = _read_persisted_graph(conn, plan)
            return _build_result(plan, candidate_set, graph)
        finally:
            trans.rollback()
            conn.close()
    finally:
        engine.dispose()


def _verify_experiment_harness_with_trust(
    engine: Engine, plan: ExperimentPlan, candidate_set: CandidateSet
) -> HarnessVerificationResult:
    """PRIVATE explicit-trust verification for scratch / non-75 tests ONLY (no operational
    identity check). Never exported; the accepted production path is
    :func:`verify_accepted_experiment_harness`."""
    return _verify_in_read_transaction(engine, plan, candidate_set, verify_identity=False)
