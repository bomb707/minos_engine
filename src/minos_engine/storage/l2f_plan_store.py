"""L2-F F3-C1 durable persistence of the accepted experiment plan (no jobs).

Persists the accepted ``ExperimentPlan`` graph into the operational store: the plan, its
complete train-member inventory, every accepted canonical CONFIG payload (published as an
immutable content-addressed artifact + registered in ``catalog.artifacts``), and the complete
plan-config inventory. It inserts **no** ``experiments.l2f_experiment_jobs`` rows (that is
F3-C2), and performs no claiming, execution, scoring, gating or service activation.

Trust boundaries:
* ``persist_accepted_experiment_plan()`` is the sole production entry point — no caller-provided
  plan/snapshot/candidate-set/hashes/partition/trust/paths. It opens the transaction
  connection, verifies (as the FIRST access on that exact connection, before anything is built,
  any candidate generated, any publisher/root touched, any upstream read or file published) that
  the connection is the canonical operational store AND is at revision ``0006`` (no
  auto-upgrade), and only THEN builds the plan via ``build_accepted_experiment_plan()``,
  independently regenerates + verifies the accepted ``CandidateSet``, constructs the provisioned
  publisher, resolves upstream, publishes, and writes — all on that same verified connection.
* ``_persist_experiment_plan_with_trust`` is a PRIVATE explicit-trust boundary for scratch /
  non-75 tests only (no operational-identity check); it is not exported.

Every upstream UUID is resolved by complete immutable identity (exactly one row, else a typed
``UpstreamIdentityError``) before any artifact publication — including the ``bam_profile`` the
snapshot member points to (its ``profile_id`` / ``content_hash`` / ``feature_values_hash`` /
``dataset_registry_id`` / COMPLETE status) and the matrix member's ``vector_hash``. Persistence
is idempotent under a plan_hash-scoped transaction advisory lock: every immutable table is
insert-or-verify against **every** immutable column and **every** unique constraint; a differing
existing row is a typed conflict, never a silently swallowed uniqueness violation.
"""

from __future__ import annotations

import contextlib
import os
import struct
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from sqlalchemy import Connection, Engine, text
from sqlalchemy.exc import IntegrityError

from minos_engine.common.canonical_json import canonical_json_bytes
from minos_engine.common.errors import MinosEngineError
from minos_engine.experiments.accepted_plan import build_accepted_experiment_plan
from minos_engine.experiments.candidates import (
    generate_accepted_candidate_set,
    verify_accepted_candidate_set,
)
from minos_engine.storage.database import create_db_engine, verify_operational_database_identity
from minos_engine.storage.l2f_config_publisher import (
    CONFIG_ARTIFACT_KIND,
    CONFIG_ARTIFACT_MEDIA_TYPE,
    CONFIG_ARTIFACT_ROOT_MODE,
    ConfigArtifactRootError,
    ConfigPayloadPublisher,
    PublishedConfigArtifact,
)
from minos_engine.storage.l2f_migration_contract import (
    L2F_CONFIG_PAYLOAD_SCHEMA,
    L2F_MIGRATION_REVISION,
)
from minos_engine.storage.roles import SCHEMA_OWNER

if TYPE_CHECKING:
    from minos_engine.experiments.candidates import CandidateSet
    from minos_engine.experiments.plan import ExperimentPlan

__all__ = [
    "L2FPersistenceError",
    "UpstreamIdentityError",
    "ImmutableMetadataConflictError",
    "ArtifactMetadataConflictError",
    "PlanRevisionError",
    "AmbiguousPlanCommitError",
    "PlanPersistResult",
    "persist_accepted_experiment_plan",
]

#: env var naming the provisioned CONFIG-payload artifact root (config, not a caller arg).
ENV_CONFIG_ARTIFACT_ROOT = "MINOS_L2F_CONFIG_ARTIFACT_ROOT"
_TRAIN = "train"
_COMPLETE = "COMPLETE"
_UNIQUE_VIOLATION = "23505"

# Every alternate UNIQUE constraint that can collide on a fresh insert (constraints that carry
# the freshly generated primary-key ``id`` can never collide and are intentionally omitted).
_PLAN_UNIQUE_KEYS: dict[str, list[str]] = {
    "uq_l2f_plans_plan_hash": ["plan_hash"],
    "uq_l2f_plans_logical_identity": [
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
    ],
}
_MEMBER_UNIQUE_KEYS: dict[str, list[str]] = {
    "uq_l2f_pm_plan_snapshot_member": ["plan_id", "profile_snapshot_member_id"],
    "uq_l2f_pm_plan_matrix_member": ["plan_id", "feature_matrix_member_id"],
    "uq_l2f_pm_plan_member_index": ["plan_id", "member_index"],
}
_CONFIG_PAYLOAD_UNIQUE_KEYS: dict[str, list[str]] = {
    "uq_l2f_config_payloads_config_hash": ["config_hash"],
}
_PLAN_CONFIG_UNIQUE_KEYS: dict[str, list[str]] = {
    "uq_l2f_pc_plan_payload": ["plan_id", "config_payload_id"],
    "uq_l2f_pc_plan_index": ["plan_id", "config_index"],
}


class L2FPersistenceError(MinosEngineError):
    """Base error for L2-F plan persistence."""


class UpstreamIdentityError(L2FPersistenceError):
    """A required upstream row was missing or ambiguous under its complete identity."""


class ImmutableMetadataConflictError(L2FPersistenceError):
    """An existing immutable row shares a unique key but differs on immutable metadata."""


class ArtifactMetadataConflictError(L2FPersistenceError):
    """An existing catalog.artifacts row shares the sha256 but differs on / has invalid metadata."""


class PlanRevisionError(L2FPersistenceError):
    """The live database is not at the required migration revision (no auto-upgrade)."""


class AmbiguousPlanCommitError(L2FPersistenceError):
    """The COMMIT itself raised; the commit outcome is unknown — artifacts are retained."""


@dataclass(frozen=True)
class PlanPersistResult:
    """Outcome of persisting an accepted plan graph (never contains job rows)."""

    plan_id: str
    plan_hash: str
    plan_created: bool
    member_count: int
    config_count: int
    payload_count: int
    artifacts_created: int
    jobs_count: int
    replay: bool


def _advisory_key(sha256: str) -> int:
    """Deterministic transaction advisory-lock key: first 8 bytes as little-endian int64."""
    return int(struct.unpack("<q", bytes.fromhex(sha256)[:8])[0])


def _require_live_revision(conn: Connection) -> None:
    rev = conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one_or_none()
    if rev != L2F_MIGRATION_REVISION:
        raise PlanRevisionError(
            f"live database revision is {rev!r}, not the required {L2F_MIGRATION_REVISION!r}; "
            "refusing to persist (this boundary NEVER runs Alembic)"
        )


def _norm(value: Any) -> str | None:
    """Normalize a scalar for immutable-column comparison (UUID/CHAR/Text/BigInteger → str)."""
    return None if value is None else str(value)


def _sqlstate(exc: IntegrityError) -> str | None:
    return getattr(getattr(exc, "orig", None), "sqlstate", None)


def _constraint_name(exc: IntegrityError) -> str | None:
    diag = getattr(getattr(exc, "orig", None), "diag", None)
    return getattr(diag, "constraint_name", None)


def _resolve_one(conn: Connection, sql: str, params: dict[str, Any], what: str) -> dict[str, Any]:
    rows = conn.execute(text(sql), params).mappings().all()
    if len(rows) != 1:
        raise UpstreamIdentityError(
            f"{what}: expected exactly 1 upstream row by complete identity, found {len(rows)}"
        )
    return dict(rows[0])


def _artifact_metadata_matches(
    row: Any, *, uri: str, size_bytes: int, media_type: str, provenance: str
) -> bool:
    """True iff the stored catalog.artifacts row matches the expected metadata exactly.

    Any NULL or malformed stored value (e.g. a NULL ``size_bytes``, or a non-integer stored
    size) is treated as a mismatch — never a raised ``TypeError``/``ValueError``.
    """
    stored_size = row["size_bytes"]
    if stored_size is None:
        return False
    try:
        stored_size_int = int(stored_size)
    except (TypeError, ValueError):
        return False
    return (
        row["uri"] == uri
        and stored_size_int == size_bytes
        and row["media_type"] == media_type
        and row["provenance"] == provenance
    )


def _register_config_artifact(
    conn: Connection, *, uri: str, sha256: str, size_bytes: int, media_type: str
) -> str:
    """Get-or-verify a catalog.artifacts row (sha256==config_hash, media_type==config media).

    Every mismatch — including NULL or malformed stored metadata — fails with a typed
    ``ArtifactMetadataConflictError``; no raw database/type exception escapes.
    """
    sel = (
        "SELECT id, uri, size_bytes, media_type, provenance FROM catalog.artifacts "
        "WHERE sha256 = :h"
    )
    row = conn.execute(text(sel), {"h": sha256}).mappings().first()
    if row is None:
        conn.execute(
            text(
                "INSERT INTO catalog.artifacts (uri, sha256, media_type, size_bytes, provenance) "
                "VALUES (:u, :h, :m, :s, :p) ON CONFLICT (sha256) DO NOTHING"
            ),
            {"u": uri, "h": sha256, "m": media_type, "s": size_bytes, "p": CONFIG_ARTIFACT_KIND},
        )
        row = conn.execute(text(sel), {"h": sha256}).mappings().first()
    if row is None:  # pragma: no cover - a row must exist after the get-or-insert
        raise ArtifactMetadataConflictError(
            f"catalog.artifacts row for sha256 {sha256} could not be registered"
        )
    if not _artifact_metadata_matches(
        row, uri=uri, size_bytes=size_bytes, media_type=media_type, provenance=CONFIG_ARTIFACT_KIND
    ):
        raise ArtifactMetadataConflictError(
            f"catalog.artifacts for sha256 {sha256} exists with differing or invalid metadata"
        )
    return str(row["id"])


def _insert_or_verify(
    conn: Connection,
    *,
    table: str,
    row: dict[str, Any],
    unique_keys: dict[str, list[str]],
) -> tuple[str, bool]:
    """Insert an immutable row idempotently. Returns (id, created).

    A plain INSERT is attempted inside a SAVEPOINT. On a uniqueness violation (SQLSTATE 23505)
    — on ANY of the table's unique constraints, not only one preselected conflict target — the
    savepoint is rolled back, the constraint is classified deterministically by name, the
    conflicting row is re-read by that constraint's logical key, and **every** immutable column
    of ``row`` is compared. An exact match is idempotent success; any difference is a typed
    :class:`ImmutableMetadataConflictError`. A raw uniqueness/integrity exception is never
    exposed. Non-uniqueness integrity errors (which indicate a genuine structural violation,
    not idempotency) propagate unchanged.
    """
    cols = list(row)
    placeholders = ", ".join(f":{c}" for c in cols)
    insert_sql = text(
        f"INSERT INTO experiments.{table} ({', '.join(cols)}) VALUES ({placeholders}) "  # noqa: S608
        "RETURNING id"
    )
    sp = conn.begin_nested()
    try:
        inserted = conn.execute(insert_sql, row).first()
        sp.commit()
        assert inserted is not None  # noqa: S101 - RETURNING id always yields a row on insert
        return str(inserted[0]), True
    except IntegrityError as exc:
        sp.rollback()
        if _sqlstate(exc) != _UNIQUE_VIOLATION:
            raise
        return _verify_existing_conflict(conn, table, row, unique_keys, exc), False


def _verify_existing_conflict(
    conn: Connection,
    table: str,
    row: dict[str, Any],
    unique_keys: dict[str, list[str]],
    exc: IntegrityError,
) -> str:
    constraint = _constraint_name(exc)
    key_cols = unique_keys.get(constraint) if constraint is not None else None
    if key_cols is None:
        raise ImmutableMetadataConflictError(
            f"experiments.{table}: uniqueness violation on unhandled constraint {constraint!r}"
        ) from exc
    where = " AND ".join(f"{c} = :{c}" for c in key_cols)
    existing = (
        conn.execute(
            text(f"SELECT * FROM experiments.{table} WHERE {where}"),  # noqa: S608
            {c: row[c] for c in key_cols},
        )
        .mappings()
        .one_or_none()
    )
    if existing is None:
        raise ImmutableMetadataConflictError(
            f"experiments.{table}: unique key {constraint!r} collided but no row re-read"
        ) from exc
    for col in row:
        if _norm(existing[col]) != _norm(row[col]):
            raise ImmutableMetadataConflictError(
                f"experiments.{table}: existing row for {constraint!r} differs on immutable "
                f"column {col!r}"
            )
    return str(existing["id"])


def _resolve_plan_upstream(conn: Connection, plan: ExperimentPlan) -> dict[str, Any]:
    """Resolve every upstream UUID by COMPLETE immutable identity (exactly one row each).

    Raises :class:`UpstreamIdentityError` (before any publication) if any required upstream row
    is missing, ambiguous, or fails a complete-identity check — including the ``bam_profile``
    the snapshot member points to (dataset registry, profile id, content hash, feature-values
    hash, COMPLETE status) and the matrix member's ``vector_hash``.
    """
    feature_set = _resolve_one(
        conn,
        "SELECT id FROM profiling.feature_sets WHERE feature_set_hash = :fsh AND registry_hash = :frh",
        {"fsh": plan.feature_set_hash, "frh": plan.feature_registry_hash},
        "feature_set",
    )
    snapshot = _resolve_one(
        conn,
        "SELECT id FROM profiling.profile_snapshots WHERE snapshot_hash = :sh "
        "AND split_manifest_hash = :sm AND registry_snapshot_hash = :rs",
        {
            "sh": plan.snapshot_hash,
            "sm": plan.split_manifest_hash,
            "rs": plan.registry_snapshot_hash,
        },
        "profile_snapshot",
    )
    matrix = _resolve_one(
        conn,
        "SELECT id FROM profiling.feature_matrices WHERE profile_snapshot_id = :sid "
        "AND partition = :p AND matrix_hash = :mh AND feature_set_id = :fsid",
        {
            "sid": snapshot["id"],
            "p": _TRAIN,
            "mh": plan.train_matrix_hash,
            "fsid": feature_set["id"],
        },
        "train_feature_matrix",
    )
    resolved_members: list[dict[str, Any]] = []
    for m in plan.members:
        dsr = _resolve_one(
            conn,
            "SELECT id FROM catalog.dataset_registry WHERE dataset_id = :d",
            {"d": m.dataset_id},
            f"dataset_registry[{m.dataset_id}]",
        )
        sm = _resolve_one(
            conn,
            "SELECT id, bam_profile_id FROM profiling.profile_snapshot_members "
            "WHERE profile_snapshot_id = :sid AND dataset_registry_id = :drid "
            "AND partition = :p AND feature_values_hash = :fvh",
            {"sid": snapshot["id"], "drid": dsr["id"], "p": _TRAIN, "fvh": m.feature_values_hash},
            f"profile_snapshot_member[{m.dataset_id}]",
        )
        # the snapshot member MUST point at a bam_profile whose COMPLETE, of this dataset, with
        # the plan member's exact profile_id / content_hash / feature_values_hash.
        bam = _resolve_one(
            conn,
            "SELECT id FROM profiling.bam_profiles WHERE id = :bid AND dataset_registry_id = :drid "
            "AND profile_id = :pid AND content_hash = :ch AND feature_values_hash = :fvh "
            "AND profile_status = :st",
            {
                "bid": sm["bam_profile_id"],
                "drid": dsr["id"],
                "pid": m.profile_id,
                "ch": m.content_hash,
                "fvh": m.feature_values_hash,
                "st": _COMPLETE,
            },
            f"bam_profile[{m.dataset_id}]",
        )
        fmm = _resolve_one(
            conn,
            "SELECT id FROM profiling.feature_matrix_members WHERE feature_matrix_id = :mid "
            "AND dataset_registry_id = :drid AND member_index = :idx "
            "AND feature_values_hash = :fvh AND vector_hash = :vh",
            {
                "mid": matrix["id"],
                "drid": dsr["id"],
                "idx": m.member_index,
                "fvh": m.feature_values_hash,
                "vh": m.vector_hash,
            },
            f"feature_matrix_member[{m.dataset_id}]",
        )
        resolved_members.append(
            {
                "dataset_registry_id": dsr["id"],
                "profile_snapshot_member_id": sm["id"],
                "bam_profile_id": bam["id"],
                "feature_matrix_member_id": fmm["id"],
                "feature_values_hash": m.feature_values_hash,
                "member_index": m.member_index,
            }
        )
    return {
        "feature_set_id": feature_set["id"],
        "profile_snapshot_id": snapshot["id"],
        "train_feature_matrix_id": matrix["id"],
        "members": resolved_members,
    }


def _publish_config_payloads(
    conn: Connection,
    plan: ExperimentPlan,
    candidate_set: CandidateSet,
    *,
    publisher: ConfigPayloadPublisher,
    created_files: list[PublishedConfigArtifact],
) -> tuple[list[str], int]:
    """Publish each canonical CONFIG payload + register its artifact + insert its config_payload.

    Returns ``(payload_ids, artifacts_created)`` in candidate order.
    """
    payload_ids: list[str] = []
    artifacts_created = 0
    for cand in candidate_set.configs:
        payload = canonical_json_bytes(cand.effective_config)
        published = publisher.publish(payload, config_hash=cand.config_hash)
        created_files.append(published)
        if published.created:
            artifacts_created += 1
        artifact_id = _register_config_artifact(
            conn,
            uri=published.uri,
            sha256=cand.config_hash,
            size_bytes=published.size_bytes,
            media_type=published.media_type,
        )
        payload_id, _ = _insert_or_verify(
            conn,
            table="l2f_config_payloads",
            row={
                "config_hash": cand.config_hash,
                "parameter_space_hash": plan.parameter_space_hash,
                "schema_version": L2F_CONFIG_PAYLOAD_SCHEMA,
                "media_type": CONFIG_ARTIFACT_MEDIA_TYPE,
                "artifact_id": artifact_id,
            },
            unique_keys=_CONFIG_PAYLOAD_UNIQUE_KEYS,
        )
        payload_ids.append(payload_id)
    return payload_ids, artifacts_created


def _persist_plan_with_trust(
    conn: Connection,
    plan: ExperimentPlan,
    candidate_set: CandidateSet,
    *,
    publisher: ConfigPayloadPublisher,
    created_files: list[PublishedConfigArtifact],
) -> PlanPersistResult:
    """Core persistence given an open connection in a transaction (no identity check).

    Runs as the schema owner, serialized on a plan_hash advisory lock. Resolves ALL upstream
    identities before publishing anything, then publishes payloads + registers artifacts +
    inserts the plan/member/payload/config graph idempotently, and read-back verifies.
    """
    conn.execute(text(f"SET ROLE {SCHEMA_OWNER}"))
    conn.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": _advisory_key(plan.plan_hash)})

    # cross-bind the plan's configs to the accepted candidate set at the same index.
    if len(plan.configs) != len(candidate_set.configs):
        raise L2FPersistenceError("plan config count != accepted candidate count")
    for cfg, cand in zip(plan.configs, candidate_set.configs, strict=True):
        if cfg.config_hash != cand.config_hash:
            raise L2FPersistenceError("plan config does not match the accepted candidate at index")
        if cfg.parameter_space_hash != plan.parameter_space_hash:
            raise L2FPersistenceError("plan config parameter_space_hash mismatch")

    # ---- resolve upstream identities (exactly one row each) BEFORE any publication ----
    upstream = _resolve_plan_upstream(conn, plan)
    snapshot_id = upstream["profile_snapshot_id"]
    matrix_id = upstream["train_feature_matrix_id"]
    feature_set_id = upstream["feature_set_id"]
    resolved_members = upstream["members"]

    # ---- publish CONFIG payloads + register catalog.artifacts + insert config_payloads ----
    payload_ids, artifacts_created = _publish_config_payloads(
        conn, plan, candidate_set, publisher=publisher, created_files=created_files
    )

    # ---- insert the plan (every immutable column; every unique key) ----
    plan_id, plan_created = _insert_or_verify(
        conn,
        table="l2f_experiment_plans",
        row={
            "profile_snapshot_id": snapshot_id,
            "train_feature_matrix_id": matrix_id,
            "feature_set_id": feature_set_id,
            "partition": _TRAIN,
            "snapshot_hash": plan.snapshot_hash,
            "split_manifest_hash": plan.split_manifest_hash,
            "registry_snapshot_hash": plan.registry_snapshot_hash,
            "train_matrix_hash": plan.train_matrix_hash,
            "train_feature_view_hash": plan.train_feature_view_hash,
            "feature_set_hash": plan.feature_set_hash,
            "feature_registry_hash": plan.feature_registry_hash,
            "gatk_registry_hash": plan.gatk_registry_hash,
            "parameter_space_hash": plan.parameter_space_hash,
            "experiment_parameter_policy_hash": plan.experiment_parameter_policy_hash,
            "candidate_set_hash": plan.candidate_set_hash,
            "train_member_count": plan.train_member_count,
            "candidate_count": plan.candidate_count,
            "logical_job_count": plan.logical_job_count,
            "plan_hash": plan.plan_hash,
        },
        unique_keys=_PLAN_UNIQUE_KEYS,
    )

    # ---- insert the complete member inventory ----
    for rm in resolved_members:
        _insert_or_verify(
            conn,
            table="l2f_experiment_plan_members",
            row={
                "plan_id": plan_id,
                "profile_snapshot_id": snapshot_id,
                "feature_matrix_id": matrix_id,
                "profile_snapshot_member_id": rm["profile_snapshot_member_id"],
                "feature_matrix_member_id": rm["feature_matrix_member_id"],
                "bam_profile_id": rm["bam_profile_id"],
                "dataset_registry_id": rm["dataset_registry_id"],
                "partition": _TRAIN,
                "feature_values_hash": rm["feature_values_hash"],
                "member_index": rm["member_index"],
            },
            unique_keys=_MEMBER_UNIQUE_KEYS,
        )

    # ---- insert the complete plan-config inventory ----
    for cfg, payload_id in zip(plan.configs, payload_ids, strict=True):
        _insert_or_verify(
            conn,
            table="l2f_experiment_plan_configs",
            row={
                "plan_id": plan_id,
                "config_payload_id": payload_id,
                "config_hash": cfg.config_hash,
                "parameter_space_hash": cfg.parameter_space_hash,
                "config_index": cfg.config_index,
            },
            unique_keys=_PLAN_CONFIG_UNIQUE_KEYS,
        )

    # ---- transaction-local read-back verification ----
    member_count = conn.execute(
        text("SELECT count(*) FROM experiments.l2f_experiment_plan_members WHERE plan_id = :p"),
        {"p": plan_id},
    ).scalar_one()
    config_count = conn.execute(
        text("SELECT count(*) FROM experiments.l2f_experiment_plan_configs WHERE plan_id = :p"),
        {"p": plan_id},
    ).scalar_one()
    payload_count = conn.execute(
        text(
            "SELECT count(DISTINCT config_payload_id) "
            "FROM experiments.l2f_experiment_plan_configs WHERE plan_id = :p"
        ),
        {"p": plan_id},
    ).scalar_one()
    jobs_count = conn.execute(
        text("SELECT count(*) FROM experiments.l2f_experiment_jobs WHERE plan_id = :p"),
        {"p": plan_id},
    ).scalar_one()
    stored = (
        conn.execute(
            text(
                "SELECT plan_hash, train_member_count, candidate_count, logical_job_count "
                "FROM experiments.l2f_experiment_plans WHERE id = :p"
            ),
            {"p": plan_id},
        )
        .mappings()
        .one()
    )
    if stored["plan_hash"] != plan.plan_hash:
        raise L2FPersistenceError("read-back plan_hash mismatch")
    if member_count != plan.train_member_count:
        raise L2FPersistenceError("read-back member count mismatch")
    if config_count != plan.candidate_count:
        raise L2FPersistenceError("read-back config count mismatch")
    if jobs_count != 0:
        raise L2FPersistenceError("F3-C1 must create zero jobs")

    return PlanPersistResult(
        plan_id=plan_id,
        plan_hash=plan.plan_hash,
        plan_created=plan_created,
        member_count=int(member_count),
        config_count=int(config_count),
        payload_count=int(payload_count),
        artifacts_created=artifacts_created,
        jobs_count=int(jobs_count),
        replay=not plan_created,
    )


def _commit_or_ambiguous(trans: Any) -> None:
    try:
        trans.commit()
    except BaseException as exc:  # a raising commit -> unknown outcome; retain artifacts
        raise AmbiguousPlanCommitError("COMMIT raised; commit outcome is ambiguous") from exc


def _post_commit_hook() -> None:
    """No-op seam invoked AFTER a successful commit, before returning. A failure here (a
    wrapper failure after a durable commit) must NOT roll back or remove the committed rows or
    the published immutable artifacts."""
    return None


# --------------------------------------------------------------------------- #
# accepted-input construction seams (monkeypatchable; the accepted production path invokes them
# ONLY after the exact-connection identity + revision checks have passed).
# --------------------------------------------------------------------------- #
def _build_accepted_plan() -> ExperimentPlan:
    return build_accepted_experiment_plan()


def _build_accepted_candidate_set() -> CandidateSet:
    candidate_set = generate_accepted_candidate_set()
    verify_accepted_candidate_set(candidate_set)
    return candidate_set


def _config_artifact_root_from_env() -> Path:
    raw = os.environ.get(ENV_CONFIG_ARTIFACT_ROOT)
    if raw is None or not raw.strip():
        raise ConfigArtifactRootError(
            f"{ENV_CONFIG_ARTIFACT_ROOT} is not set; the provisioned CONFIG-payload artifact "
            f"root (mode {oct(CONFIG_ARTIFACT_ROOT_MODE)}) must be configured explicitly"
        )
    return Path(raw.strip())


def _build_publisher() -> ConfigPayloadPublisher:
    """Construct the provisioned publisher (this is the step that first touches the artifact
    root on the filesystem)."""
    return ConfigPayloadPublisher(_config_artifact_root_from_env())


_BuildInputs = Callable[[Connection], "tuple[ExperimentPlan, CandidateSet, ConfigPayloadPublisher]"]


def _execute_persistence_txn(
    engine: Engine,
    *,
    verify_identity: bool,
    build_inputs: _BuildInputs,
) -> PlanPersistResult:
    """Open one transaction, optionally verify the exact-connection identity + revision FIRST,
    then build the accepted inputs and persist — all on the same verified connection.

    When ``verify_identity`` is True the identity + revision checks are the FIRST accesses on the
    connection; ``build_inputs`` (which builds the plan, generates candidates, constructs the
    publisher and touches the artifact root) is invoked ONLY after they pass, so a wrong database
    or revision produces zero calls to the plan builder, candidate builder, publisher/root
    access, upstream resolver and publication.
    """
    conn = engine.connect()
    trans = conn.begin()
    committed = False
    created_files: list[PublishedConfigArtifact] = []
    publisher: ConfigPayloadPublisher | None = None
    try:
        if verify_identity:
            verify_operational_database_identity(conn)
            _require_live_revision(conn)
        plan, candidate_set, publisher = build_inputs(conn)
        result = _persist_plan_with_trust(
            conn, plan, candidate_set, publisher=publisher, created_files=created_files
        )
        _commit_or_ambiguous(trans)
        committed = True
        _post_commit_hook()  # a failure here is post-durable-commit: keep rows + artifacts
        return result
    except AmbiguousPlanCommitError:
        # commit outcome unknown: DO NOT roll back and DO NOT remove immutable artifacts.
        raise
    except BaseException:
        if not committed:
            with contextlib.suppress(Exception):
                trans.rollback()
            if publisher is not None:
                for art in created_files:
                    publisher.unpublish_if_created(art)
        raise
    finally:
        conn.close()


def persist_accepted_experiment_plan() -> PlanPersistResult:
    """THE accepted F3-C1 persistence entry point — no caller-provided trust or paths.

    Opens the transaction connection and, as the FIRST access on that exact connection, verifies
    it is the canonical operational store at revision ``0006`` (never auto-upgrading). Only then
    does it build the accepted plan, regenerate + independently verify the accepted candidate
    set, construct the provisioned publisher (first filesystem touch), resolve upstream, publish
    payloads, and persist the plan/member/payload/config graph (zero jobs) on the same verified
    connection. Idempotent on replay.
    """
    engine = create_db_engine()
    try:

        def _build(
            _conn: Connection,
        ) -> tuple[ExperimentPlan, CandidateSet, ConfigPayloadPublisher]:
            plan = _build_accepted_plan()
            candidate_set = _build_accepted_candidate_set()
            publisher = _build_publisher()
            return plan, candidate_set, publisher

        return _execute_persistence_txn(engine, verify_identity=True, build_inputs=_build)
    finally:
        engine.dispose()


def _persist_experiment_plan_with_trust(
    engine: Engine,
    plan: ExperimentPlan,
    candidate_set: CandidateSet,
    *,
    publisher: ConfigPayloadPublisher,
) -> PlanPersistResult:
    """PRIVATE explicit-trust persistence for scratch / non-75 tests ONLY (no operational
    identity check). Never exported; the accepted production path is
    :func:`persist_accepted_experiment_plan`."""
    return _execute_persistence_txn(
        engine,
        verify_identity=False,
        build_inputs=lambda _conn: (plan, candidate_set, publisher),
    )
