"""DB-V2 D3-A phase B0: bootstrap the shadow artifact catalog and its locations.

B0 is the *only* transformation D3-A performs. It moves nothing but the artifact catalog:

    V1  catalog.artifacts
    ->  dbv2_catalog.storage_backends, dbv2_catalog.artifacts, dbv2_catalog.artifact_locations

No profile, matrix, plan, job, result, evaluation, model or runtime row is created. B0 exists
because ``0009`` creates the shadow tables empty and a complete R1 snapshot describes the
operational artifact inventory — R2 has nothing to be exact against until B0 has run.

Everything goes through the declared get-or-verify APIs, so B0 is idempotent, concurrency-safe and
fail-closed on any immutable conflict by construction rather than by convention. It never writes,
renames, deletes, chmods or repairs a payload file.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Final

from sqlalchemy import Connection, text

from .dbv2_recovery import (
    OPERATIONAL_RETENTION_CLASS,
    ArtifactRoots,
    R1Bundle,
    ScannedArtifact,
    hash_payload,
)

__all__ = ["B0Error", "B0Result", "SHADOW_REVISION", "bootstrap_artifacts"]

SHADOW_REVISION: Final = "0009_dbv2_shadow_schema"

#: the frozen schema version every bootstrapped operational artifact carries.
OPERATIONAL_SCHEMA_VERSION: Final = "minos-v1-artifact-v1"

#: the shadow business tables B0 must leave empty. If any of them holds a row, something other
#: than B0 has already transformed data and B0 refuses to add to it.
NON_ARTIFACT_SHADOW_TABLES: Final[tuple[str, ...]] = (
    "dbv2_catalog.datasets",
    "dbv2_catalog.releases",
    "dbv2_catalog.backup_sets",
    "dbv2_profiling.bam_profiles",
    "dbv2_profiling.profile_snapshots",
    "dbv2_profiling.profile_snapshot_members",
    "dbv2_profiling.feature_sets",
    "dbv2_profiling.feature_matrices",
    "dbv2_profiling.feature_matrix_members",
    "dbv2_experiments.parameter_spaces",
    "dbv2_experiments.candidate_configs",
    "dbv2_experiments.candidate_sets",
    "dbv2_experiments.candidate_set_configs",
    "dbv2_experiments.experiment_plans",
    "dbv2_experiments.experiment_plan_members",
    "dbv2_experiments.experiment_plan_configs",
    "dbv2_experiments.experiment_jobs",
    "dbv2_experiments.execution_attempts",
    "dbv2_experiments.execution_results",
    "dbv2_experiments.execution_failures",
    "dbv2_experiments.job_events",
    "dbv2_evaluation.truth_bindings",
    "dbv2_evaluation.evaluation_runs",
    "dbv2_evaluation.evaluation_metrics",
    "dbv2_evaluation.evaluation_scores",
    "dbv2_models.model_definitions",
    "dbv2_models.training_runs",
    "dbv2_models.model_versions",
    "dbv2_models.model_activations",
    "dbv2_runtime.service_instances",
    "dbv2_runtime.leases",
    "dbv2_runtime.active_selections",
)

#: deterministic in the recovery set's identity, so two B0 callers for the same R1 serialize.
_ADVISORY_LOCK_SQL: Final = "SELECT pg_advisory_xact_lock(hashtextextended(:key, 1))"


class B0Error(RuntimeError):
    """The artifact bootstrap failed closed."""


@dataclass(frozen=True, slots=True)
class B0Result:
    backends: int
    artifacts_registered: int
    locations_registered: int
    artifacts_verified: int
    already_present: int


def _require_revision(conn: Connection) -> None:
    revision = str(conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one())
    if revision != SHADOW_REVISION:
        raise B0Error(f"B0 requires {SHADOW_REVISION}, this database is at {revision}")


def _require_empty_business_tables(conn: Connection) -> None:
    for table in NON_ARTIFACT_SHADOW_TABLES:
        count = int(conn.execute(text(f"SELECT count(*) FROM {table}")).scalar_one())
        if count:
            raise B0Error(
                f"{table} already holds {count} rows; B0 transforms the artifact catalog only and "
                "refuses to run against a partially transformed shadow schema"
            )


def _get_or_create_backend(conn: Connection, backend_key: str, logical_root: str) -> str:
    """Get-or-verify one storage backend per declared artifact root."""
    existing = conn.execute(
        text(
            "SELECT id, backend_type, logical_root FROM dbv2_catalog.storage_backends "
            "WHERE backend_key = :k FOR UPDATE"
        ),
        {"k": backend_key},
    ).one_or_none()
    if existing is not None:
        if str(existing[1]) != "local_fs" or str(existing[2]) != logical_root:
            raise B0Error(
                f"storage backend {backend_key!r} is already registered with a different identity"
            )
        return str(existing[0])
    return str(
        conn.execute(
            text(
                "INSERT INTO dbv2_catalog.storage_backends "
                "(backend_key, backend_type, logical_root) VALUES (:k, 'local_fs', :r) "
                "RETURNING id"
            ),
            {"k": backend_key, "r": logical_root},
        ).scalar_one()
    )


def _provenance(artifact: ScannedArtifact) -> str:
    """Bind the V1 row identity and its immutable identity, and nothing host-specific.

    The absolute source path is deliberately absent: it is host state, and the backend key plus
    the relative object_key already reconstruct it exactly.
    """
    return json.dumps(
        {
            "backend_key": artifact.backend_key,
            "bootstrap_phase": "B0",
            "object_key": artifact.object_key,
            "source": "v1.catalog.artifacts",
            "v1_artifact_id": artifact.v1_id,
            "v1_content_sha256": artifact.sha256,
            "v1_size_bytes": artifact.size_bytes,
        },
        sort_keys=True,
        separators=(",", ":"),
    )


def bootstrap_artifacts(conn: Connection, *, bundle: R1Bundle, roots: ArtifactRoots) -> B0Result:
    """Transform the V1 artifact catalog into the shadow catalog, inside the caller's transaction.

    ``bundle`` must be a verified R1 set; every payload is re-read through the same
    descriptor-bound contract R1 used, so B0 never trusts R1's word about the bytes.
    """
    _require_revision(conn)
    conn.execute(text(_ADVISORY_LOCK_SQL), {"key": bundle.recovery_set_id})
    _require_empty_business_tables(conn)

    declared = {entry["content_sha256"]: entry for entry in _snapshot_entries(bundle)}
    if len(declared) != bundle.artifact_count:
        raise B0Error("the R1 snapshot repeats a content digest")

    backend_ids: dict[str, str] = {}
    for backend_key, root in roots.roots:
        backend_ids[backend_key] = _get_or_create_backend(conn, backend_key, str(root))

    registered = locations = verified = already = 0
    for artifact in bundle.artifacts:
        if artifact.backend_key not in backend_ids:
            raise B0Error(f"artifact {artifact.v1_id} names an undeclared backend")
        entry = declared.get(artifact.sha256)
        if entry is None:
            raise B0Error(f"artifact {artifact.sha256} is absent from the R1 snapshot")
        _, _, path = roots.resolve(artifact.locator)
        observation = hash_payload(path)
        if observation.sha256 != artifact.sha256 or observation.size_bytes != artifact.size_bytes:
            raise B0Error(
                f"artifact {artifact.v1_id} no longer matches R1: {observation.sha256} "
                f"({observation.size_bytes} bytes)"
            )
        before = _artifact_row(conn, artifact.sha256)
        artifact_id = str(
            conn.execute(
                text(
                    "SELECT dbv2_catalog.get_or_verify_external_artifact("
                    ":d, :s, :m, :k, 'operational', :rc, :sv, CAST(:p AS jsonb))"
                ),
                {
                    "d": artifact.sha256,
                    "s": artifact.size_bytes,
                    "m": artifact.media_type,
                    "k": artifact.artifact_kind,
                    "rc": OPERATIONAL_RETENTION_CLASS,
                    "sv": OPERATIONAL_SCHEMA_VERSION,
                    "p": _provenance(artifact),
                },
            ).scalar_one()
        )
        if before is None:
            registered += 1
        else:
            already += 1
        location_before = _location_row(conn, artifact_id)
        conn.execute(
            text("SELECT dbv2_catalog.get_or_verify_artifact_location(:a, :b, :k, true)"),
            {"a": artifact_id, "b": artifact.backend_key, "k": artifact.object_key},
        )
        if location_before is None:
            locations += 1
        outcome = str(
            conn.execute(
                text("SELECT dbv2_catalog.record_artifact_verification(:a, :d, :s, NULL)"),
                {"a": artifact_id, "d": observation.sha256, "s": observation.size_bytes},
            ).scalar_one()
        )
        if outcome != "verified":
            raise B0Error(f"artifact {artifact.sha256} verified as {outcome}")
        verified += 1

    _require_exact_graph(conn, bundle)
    return B0Result(
        backends=len(backend_ids),
        artifacts_registered=registered,
        locations_registered=locations,
        artifacts_verified=verified,
        already_present=already,
    )


def _snapshot_entries(bundle: R1Bundle) -> list[dict[str, Any]]:
    document = json.loads(bundle.snapshot_manifest_bytes.decode("utf-8"))
    entries = document["entries"]
    if not isinstance(entries, list):
        raise B0Error("the R1 snapshot entries are not a list")
    return [dict(entry) for entry in entries]


def _artifact_row(conn: Connection, digest: str) -> Any:
    return conn.execute(
        text("SELECT id FROM dbv2_catalog.artifacts WHERE content_sha256 = :d"), {"d": digest}
    ).one_or_none()


def _location_row(conn: Connection, artifact_id: str) -> Any:
    return conn.execute(
        text("SELECT id FROM dbv2_catalog.artifact_locations WHERE artifact_id = :a"),
        {"a": artifact_id},
    ).one_or_none()


def _require_exact_graph(conn: Connection, bundle: R1Bundle) -> None:
    """Re-read the whole graph and require exact equality with R1 - not a count, the set."""
    rows = conn.execute(
        text(
            "SELECT a.content_sha256, a.size_bytes, a.artifact_kind, a.verification_state, "
            "       b.backend_key, l.object_key, l.location_state, l.is_primary "
            "FROM dbv2_catalog.artifacts AS a "
            "JOIN dbv2_catalog.artifact_locations AS l ON l.artifact_id = a.id "
            "JOIN dbv2_catalog.storage_backends AS b ON b.id = l.backend_id "
            "WHERE a.backup_scope = 'operational' "
            "ORDER BY a.content_sha256"
        )
    ).all()
    observed = {(str(row[0]), int(row[1]), str(row[2]), str(row[4]), str(row[5])) for row in rows}
    expected = {
        (a.sha256, a.size_bytes, a.artifact_kind, a.backend_key, a.object_key)
        for a in bundle.artifacts
    }
    if observed != expected:
        missing = sorted(expected - observed)
        extra = sorted(observed - expected)
        raise B0Error(
            f"the bootstrapped graph differs from R1: {len(missing)} missing, {len(extra)} extra"
        )
    for row in rows:
        if str(row[3]) != "verified":
            raise B0Error(f"artifact {row[0]} is {row[3]}, not verified")
        if str(row[6]) != "present" or not bool(row[7]):
            raise B0Error(f"artifact {row[0]} has no present primary location")
    primaries = conn.execute(
        text(
            "SELECT a.content_sha256, count(*) FROM dbv2_catalog.artifacts AS a "
            "JOIN dbv2_catalog.artifact_locations AS l ON l.artifact_id = a.id "
            "WHERE a.backup_scope = 'operational' AND l.is_primary "
            "  AND l.location_state = 'present' "
            "GROUP BY 1 HAVING count(*) <> 1"
        )
    ).all()
    if primaries:
        raise B0Error(f"{len(primaries)} artifacts do not have exactly one present primary")
