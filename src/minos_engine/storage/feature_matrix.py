"""L2-E feature-matrix build + persistence (owner-run, schema-owner writes).

Storage-side counterpart of the pure ``layer2.features`` machinery: this module owns
every database write for L2-E matrices. No application role holds INSERT on any 0005
table; writes run as the schema owner (``minos_admin``).

Trust boundary (owner ruling):
  * The PRODUCTION builder is :func:`build_accepted_epoch1_feature_matrix` — it begins
    from ``load_accepted_epoch1_member_manifest(manifest_bytes)`` and accepts NO
    caller-created FrozenSnapshot, no ManifestTrustBundle, no expected hashes, and no
    caller-selected epoch/snapshot identities. The E3+ operational path consumes ONLY
    this boundary.
  * :func:`build_feature_matrix_with_trust` is the explicitly TEST-ONLY generic helper
    (synthetic complete trust bundles for integration tests); production code never
    imports or calls it.
  * Before any payload read, artifact write, or DB mutation the builder rejects
    ``partition="test"``, verifies the member manifest, selects exact snapshot
    membership verbatim (row counts derive from that membership — no percentages,
    reassignment, or re-rounding), and confirms the operational profile snapshot and
    its membership reproduce the accepted snapshot identity.
  * Profile payloads are the EXACT accepted artifact bytes referenced by the
    operational store (``catalog.artifacts`` URIs) — never reconstructed from JSONB.

Persistence semantics (one transaction after full verification):
  * equal logical identity + equal matrix/artifact hashes → idempotent return;
  * equal logical identity + different matrix_hash → :class:`MatrixConflictError`;
  * equal artifact sha with different size/media/kind → ArtifactMetadataConflictError;
  * race-safe artifact registration via ON CONFLICT DO NOTHING + re-read;
  * concurrent unique violations classified by constraint name;
  * failures leave no partial feature set/matrix/member rows (single transaction);
  * every persisted row is re-read and checked field-for-field before returning.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.exc import DBAPIError

from minos_engine.common.errors import (
    ArtifactMetadataConflictError,
    ContractValidationError,
    MatrixConflictError,
)
from minos_engine.layer2.features.contracts import (
    FeatureMatrix,
    FeatureSetManifest,
    build_feature_set_manifest,
)
from minos_engine.layer2.features.errors import (
    ForbiddenPartitionError,
    ProfileArtifactHashMismatchError,
    SnapshotIdentityMismatchError,
)
from minos_engine.layer2.features.extraction import (
    MATRIX_PARTITIONS,
    FrozenSnapshot,
    ManifestTrustBundle,
    MatrixBuild,
    SnapshotMember,
    build_partition_matrix,
    load_accepted_epoch1_member_manifest,
    load_member_manifest_with_trust,
)
from minos_engine.layer2.features.matrix_parquet import (
    MATRIX_ARTIFACT_KIND,
    MATRIX_ARTIFACT_MEDIA_TYPE,
    serialize_matrix,
    write_matrix_artifact,
)

__all__ = [
    "PersistedFeatureMatrix",
    "persist_feature_set",
    "persist_feature_matrix",
    "build_accepted_epoch1_feature_matrix",
    "build_feature_matrix_with_trust",
]


@dataclass(frozen=True)
class PersistedFeatureMatrix:
    """The verified result of one matrix persistence (or idempotent replay)."""

    feature_matrix_id: str
    feature_set_id: str
    matrix_hash: str
    artifact_sha256: str
    matrix_artifact_id: str
    artifact_path: str
    row_count: int
    idempotent: bool


# --------------------------------------------------------------------------- #
# constraint-name classification for concurrent unique violations
# --------------------------------------------------------------------------- #
_CONSTRAINT_ERRORS: dict[str, type[Exception]] = {
    "uq_feature_matrices_logical_identity": MatrixConflictError,
    "uq_feature_matrices_matrix_hash": MatrixConflictError,
    "uq_feature_matrix_members_matrix_dataset": MatrixConflictError,
    "uq_feature_matrix_members_matrix_index": MatrixConflictError,
    "uq_feature_sets_feature_set_hash": ContractValidationError,
    "uq_artifacts_sha256": ArtifactMetadataConflictError,
}


def _classify_unique_violation(exc: DBAPIError) -> Exception:
    message = str(exc.orig) if exc.orig is not None else str(exc)
    for constraint, error_type in _CONSTRAINT_ERRORS.items():
        if constraint in message:
            return error_type(f"concurrent unique violation on {constraint}")
    return exc


# --------------------------------------------------------------------------- #
# feature-set persistence (idempotent by content hash)
# --------------------------------------------------------------------------- #
def _feature_set_row(conn: Connection, feature_set_hash: str) -> dict[str, Any] | None:
    row = (
        conn.execute(
            text(
                "SELECT id, registry_hash, column_count, column_manifest "
                "FROM profiling.feature_sets WHERE feature_set_hash = :h"
            ),
            {"h": feature_set_hash},
        )
        .mappings()
        .first()
    )
    return dict(row) if row is not None else None


def persist_feature_set(conn: Connection, manifest: FeatureSetManifest) -> str:
    """Persist (or idempotently resolve) one frozen feature-set manifest row."""
    column_manifest = [
        {
            "index": c.index,
            "path": c.path,
            "source_schema": c.source_schema,
            "state": c.state,
            "value_kind": c.value_kind,
        }
        for c in manifest.columns
    ]
    existing = _feature_set_row(conn, manifest.feature_set_hash)
    if existing is None:
        conn.execute(
            text(
                "INSERT INTO profiling.feature_sets "
                "(feature_set_hash, registry_hash, column_count, column_manifest) "
                "VALUES (:h, :r, :n, CAST(:m AS jsonb)) "
                "ON CONFLICT (feature_set_hash) DO NOTHING"
            ),
            {
                "h": manifest.feature_set_hash,
                "r": manifest.registry_hash,
                "n": manifest.column_count,
                "m": json.dumps(column_manifest),
            },
        )
        existing = _feature_set_row(conn, manifest.feature_set_hash)
        if existing is None:  # pragma: no cover - insert+reread cannot both miss
            raise ContractValidationError("feature_set row missing after insert")
    if (
        existing["registry_hash"] != manifest.registry_hash
        or int(existing["column_count"]) != manifest.column_count
        or existing["column_manifest"] != column_manifest
    ):
        raise ContractValidationError(
            f"feature_set {manifest.feature_set_hash[:12]}… exists with conflicting content"
        )
    return str(existing["id"])


# --------------------------------------------------------------------------- #
# operational-snapshot identity verification
# --------------------------------------------------------------------------- #
def _verify_operational_snapshot(conn: Connection, snapshot: FrozenSnapshot) -> str:
    """The DB profile snapshot + membership must reproduce the accepted identity."""
    row = (
        conn.execute(
            text(
                "SELECT id, snapshot_hash, split_manifest_hash, registry_snapshot_hash, "
                " member_count FROM profiling.profile_snapshots WHERE epoch = :e"
            ),
            {"e": snapshot.epoch},
        )
        .mappings()
        .first()
    )
    if row is None:
        raise SnapshotIdentityMismatchError(
            f"no operational profile snapshot for epoch {snapshot.epoch}"
        )
    if (
        row["snapshot_hash"] != snapshot.snapshot_hash
        or row["split_manifest_hash"] != snapshot.split_manifest_hash
        or row["registry_snapshot_hash"] != snapshot.registry_snapshot_hash
        or int(row["member_count"]) != len(snapshot.members)
    ):
        raise SnapshotIdentityMismatchError(
            "operational profile snapshot does not reproduce the accepted snapshot identity"
        )
    db_members = conn.execute(
        text(
            "SELECT dr.dataset_id, m.partition, bp.content_hash, m.feature_values_hash "
            "FROM profiling.profile_snapshot_members m "
            "JOIN profiling.bam_profiles bp ON bp.id = m.bam_profile_id "
            "JOIN catalog.dataset_registry dr ON dr.id = m.dataset_registry_id "
            "WHERE m.profile_snapshot_id = :s"
        ),
        {"s": row["id"]},
    ).all()
    operational = {
        r.dataset_id: (r.partition, r.content_hash, r.feature_values_hash) for r in db_members
    }
    accepted = {
        m.dataset_id: (m.partition, m.content_hash, m.feature_values_hash) for m in snapshot.members
    }
    if operational != accepted:
        raise SnapshotIdentityMismatchError(
            "operational snapshot membership does not reproduce the accepted membership"
        )
    return str(row["id"])


def _payload_source(conn: Connection, member: SnapshotMember) -> Path:
    """Resolve the EXACT accepted profile artifact for one member (never JSONB)."""
    row = (
        conn.execute(
            text(
                "SELECT bp.profile_sha256, art.uri "
                "FROM profiling.bam_profiles bp "
                "JOIN catalog.dataset_registry dr ON dr.id = bp.dataset_registry_id "
                "JOIN catalog.artifacts art ON art.id = bp.profile_artifact_id "
                "WHERE dr.dataset_id = :d AND bp.content_hash = :c"
            ),
            {"d": member.dataset_id, "c": member.content_hash},
        )
        .mappings()
        .first()
    )
    if row is None:
        raise SnapshotIdentityMismatchError(
            f"{member.dataset_id}: no accepted profile row/artifact for the selected version"
        )
    if row["profile_sha256"] != member.profile_sha256:
        raise ProfileArtifactHashMismatchError(
            f"{member.dataset_id}: operational profile_sha256 does not match the "
            "manifest-bound value"
        )
    return Path(str(row["uri"]))


# --------------------------------------------------------------------------- #
# matrix persistence (one transaction, verified field-for-field)
# --------------------------------------------------------------------------- #
def _matrix_row_by_identity(
    conn: Connection, profile_snapshot_id: str, partition: str, feature_set_id: str
) -> dict[str, Any] | None:
    row = (
        conn.execute(
            text(
                "SELECT id, matrix_hash, artifact_sha256, matrix_artifact_id, row_count, "
                " column_count FROM profiling.feature_matrices "
                "WHERE profile_snapshot_id = :s AND partition = :p AND feature_set_id = :f"
            ),
            {"s": profile_snapshot_id, "p": partition, "f": feature_set_id},
        )
        .mappings()
        .first()
    )
    return dict(row) if row is not None else None


def _register_matrix_artifact(conn: Connection, *, uri: str, sha256: str, size_bytes: int) -> str:
    """Race-safe content-addressed artifact registration (exact metadata match only)."""
    params = {"h": sha256}
    select = text(
        "SELECT id, uri, size_bytes, media_type, provenance "
        "FROM catalog.artifacts WHERE sha256 = :h"
    )
    existing = conn.execute(select, params).mappings().first()
    if existing is None:
        conn.execute(
            text(
                "INSERT INTO catalog.artifacts (uri, sha256, size_bytes, media_type, "
                " provenance) VALUES (:u, :h, :s, :m, :k) ON CONFLICT (sha256) DO NOTHING"
            ),
            {
                "u": uri,
                "h": sha256,
                "s": size_bytes,
                "m": MATRIX_ARTIFACT_MEDIA_TYPE,
                "k": MATRIX_ARTIFACT_KIND,
            },
        )
        existing = conn.execute(select, params).mappings().one()
    if (
        existing["size_bytes"] != size_bytes
        or existing["media_type"] != MATRIX_ARTIFACT_MEDIA_TYPE
        or existing["provenance"] != MATRIX_ARTIFACT_KIND
        or existing["uri"] != uri
    ):
        raise ArtifactMetadataConflictError(
            f"artifact {sha256[:12]}… exists with conflicting metadata/provenance"
        )
    return str(existing["id"])


def _read_back_and_verify(
    conn: Connection,
    matrix_id: str,
    *,
    matrix: FeatureMatrix,
    feature_set_id: str,
    profile_snapshot_id: str,
    artifact_sha256: str,
    artifact_id: str,
    member_registry_ids: dict[str, str],
    member_feature_hashes: dict[str, str],
) -> None:
    """Field-for-field verification of the committed (or replayed) rows."""
    row = (
        conn.execute(
            text(
                "SELECT profile_snapshot_id, partition, feature_set_id, matrix_hash, "
                " artifact_sha256, matrix_artifact_id, row_count, column_count "
                "FROM profiling.feature_matrices WHERE id = :i"
            ),
            {"i": matrix_id},
        )
        .mappings()
        .one()
    )
    if (
        str(row["profile_snapshot_id"]) != profile_snapshot_id
        or row["partition"] != matrix.partition
        or str(row["feature_set_id"]) != feature_set_id
        or row["matrix_hash"] != matrix.matrix_hash
        or row["artifact_sha256"] != artifact_sha256
        or str(row["matrix_artifact_id"]) != artifact_id
        or int(row["row_count"]) != matrix.row_count
        or int(row["column_count"]) != matrix.column_count
    ):
        raise MatrixConflictError("persisted matrix row does not match the verified content")
    members = conn.execute(
        text(
            "SELECT dr.dataset_id, mm.dataset_registry_id, mm.member_index, mm.vector_hash, "
            " mm.feature_values_hash FROM profiling.feature_matrix_members mm "
            "JOIN catalog.dataset_registry dr ON dr.id = mm.dataset_registry_id "
            "WHERE mm.feature_matrix_id = :i ORDER BY mm.member_index"
        ),
        {"i": matrix_id},
    ).all()
    if len(members) != len(matrix.members):
        raise MatrixConflictError("persisted member count does not match the matrix")
    for index, (db_row, member) in enumerate(zip(members, matrix.members, strict=True)):
        if (
            db_row.dataset_id != member.dataset_id
            or int(db_row.member_index) != index
            or db_row.vector_hash != member.vector_hash
            or str(db_row.dataset_registry_id) != member_registry_ids[member.dataset_id]
            or db_row.feature_values_hash != member_feature_hashes[member.dataset_id]
        ):
            raise MatrixConflictError(
                f"persisted member row {member.dataset_id} does not match the verified content"
            )


def persist_feature_matrix(
    engine: Engine,
    *,
    snapshot: FrozenSnapshot,
    matrix: FeatureMatrix,
    artifact_sha256: str,
    artifact_path: Path,
    artifact_size: int,
) -> PersistedFeatureMatrix:
    """Persist feature set + matrix + members + artifact registration in ONE
    transaction after full verification (see module docstring for semantics)."""
    manifest = build_feature_set_manifest()
    if matrix.feature_set_hash != manifest.feature_set_hash:
        raise ContractValidationError("matrix does not bind the canonical feature set")
    try:
        with engine.begin() as conn:
            feature_set_id = persist_feature_set(conn, manifest)
            profile_snapshot_id = _verify_operational_snapshot(conn, snapshot)
            member_rows = conn.execute(
                text(
                    "SELECT dr.dataset_id, dr.id AS registry_id, m.feature_values_hash "
                    "FROM profiling.profile_snapshot_members m "
                    "JOIN catalog.dataset_registry dr ON dr.id = m.dataset_registry_id "
                    "WHERE m.profile_snapshot_id = :s AND m.partition = :p"
                ),
                {"s": profile_snapshot_id, "p": matrix.partition},
            ).all()
            member_registry_ids = {r.dataset_id: str(r.registry_id) for r in member_rows}
            member_feature_hashes = {r.dataset_id: r.feature_values_hash for r in member_rows}
            if set(member_registry_ids) != {m.dataset_id for m in matrix.members}:
                raise SnapshotIdentityMismatchError(
                    "matrix membership does not equal the snapshot partition membership"
                )

            existing = _matrix_row_by_identity(
                conn, profile_snapshot_id, matrix.partition, feature_set_id
            )
            if existing is not None:
                if (
                    existing["matrix_hash"] != matrix.matrix_hash
                    or existing["artifact_sha256"] != artifact_sha256
                ):
                    raise MatrixConflictError(
                        "a matrix with the same logical identity exists with a different "
                        "matrix_hash/artifact binding"
                    )
                _read_back_and_verify(
                    conn,
                    str(existing["id"]),
                    matrix=matrix,
                    feature_set_id=feature_set_id,
                    profile_snapshot_id=profile_snapshot_id,
                    artifact_sha256=artifact_sha256,
                    artifact_id=str(existing["matrix_artifact_id"]),
                    member_registry_ids=member_registry_ids,
                    member_feature_hashes=member_feature_hashes,
                )
                return PersistedFeatureMatrix(
                    feature_matrix_id=str(existing["id"]),
                    feature_set_id=feature_set_id,
                    matrix_hash=matrix.matrix_hash,
                    artifact_sha256=artifact_sha256,
                    matrix_artifact_id=str(existing["matrix_artifact_id"]),
                    artifact_path=str(artifact_path),
                    row_count=matrix.row_count,
                    idempotent=True,
                )

            artifact_id = _register_matrix_artifact(
                conn, uri=str(artifact_path), sha256=artifact_sha256, size_bytes=artifact_size
            )
            inserted = conn.execute(
                text(
                    "INSERT INTO profiling.feature_matrices "
                    "(profile_snapshot_id, partition, feature_set_id, matrix_hash, "
                    " artifact_sha256, matrix_artifact_id, row_count, column_count) "
                    "VALUES (:s, :p, :f, :mh, :ah, :aid, :rc, :cc) "
                    "ON CONFLICT ON CONSTRAINT uq_feature_matrices_logical_identity "
                    "DO NOTHING RETURNING id"
                ),
                {
                    "s": profile_snapshot_id,
                    "p": matrix.partition,
                    "f": feature_set_id,
                    "mh": matrix.matrix_hash,
                    "ah": artifact_sha256,
                    "aid": artifact_id,
                    "rc": matrix.row_count,
                    "cc": matrix.column_count,
                },
            ).first()
            if inserted is None:
                # a concurrent equal-or-conflicting build won the race; re-read + classify.
                replay = _matrix_row_by_identity(
                    conn, profile_snapshot_id, matrix.partition, feature_set_id
                )
                if replay is None:  # pragma: no cover - conflict row must exist
                    raise MatrixConflictError("concurrent matrix insert could not be resolved")
                if (
                    replay["matrix_hash"] != matrix.matrix_hash
                    or replay["artifact_sha256"] != artifact_sha256
                ):
                    raise MatrixConflictError(
                        "a concurrent build persisted the same logical identity with a "
                        "different matrix_hash/artifact binding"
                    )
                matrix_id = str(replay["id"])
                idempotent = True
            else:
                matrix_id = str(inserted.id)
                idempotent = False
                for index, member in enumerate(matrix.members):
                    conn.execute(
                        text(
                            "INSERT INTO profiling.feature_matrix_members "
                            "(feature_matrix_id, dataset_registry_id, member_index, "
                            " vector_hash, feature_values_hash) "
                            "VALUES (:m, :d, :i, :vh, :fh)"
                        ),
                        {
                            "m": matrix_id,
                            "d": member_registry_ids[member.dataset_id],
                            "i": index,
                            "vh": member.vector_hash,
                            "fh": member_feature_hashes[member.dataset_id],
                        },
                    )
            _read_back_and_verify(
                conn,
                matrix_id,
                matrix=matrix,
                feature_set_id=feature_set_id,
                profile_snapshot_id=profile_snapshot_id,
                artifact_sha256=artifact_sha256,
                artifact_id=artifact_id,
                member_registry_ids=member_registry_ids,
                member_feature_hashes=member_feature_hashes,
            )
            return PersistedFeatureMatrix(
                feature_matrix_id=matrix_id,
                feature_set_id=feature_set_id,
                matrix_hash=matrix.matrix_hash,
                artifact_sha256=artifact_sha256,
                matrix_artifact_id=artifact_id,
                artifact_path=str(artifact_path),
                row_count=matrix.row_count,
                idempotent=idempotent,
            )
    except DBAPIError as exc:
        classified = _classify_unique_violation(exc)
        if classified is exc:
            raise
        raise classified from exc


# --------------------------------------------------------------------------- #
# builders
# --------------------------------------------------------------------------- #
def build_accepted_epoch1_feature_matrix(
    engine: Engine,
    member_manifest_bytes: bytes,
    partition: str,
    *,
    artifact_root: Path,
) -> PersistedFeatureMatrix:
    """THE production E3 builder: accepted epoch-1 boundary only.

    Loads the accepted epoch-1 member manifest (pinned trust anchors; no
    caller-supplied identities of any kind) BEFORE touching the database, the payload
    provider, or the filesystem, then builds + persists one partition matrix.
    """
    snapshot = load_accepted_epoch1_member_manifest(member_manifest_bytes)
    return _build_feature_matrix(engine, snapshot, partition, artifact_root=artifact_root)


def build_feature_matrix_with_trust(
    engine: Engine,
    member_manifest_bytes: bytes,
    trust: ManifestTrustBundle,
    partition: str,
    *,
    artifact_root: Path,
) -> PersistedFeatureMatrix:
    """TEST-ONLY generic builder for synthetic snapshots under an explicit complete
    trust bundle. Production code imports and calls ONLY
    :func:`build_accepted_epoch1_feature_matrix`."""
    snapshot = load_member_manifest_with_trust(member_manifest_bytes, trust)
    return _build_feature_matrix(engine, snapshot, partition, artifact_root=artifact_root)


def _build_feature_matrix(
    engine: Engine,
    snapshot: FrozenSnapshot,
    partition: str,
    *,
    artifact_root: Path,
) -> PersistedFeatureMatrix:
    # test is rejected BEFORE any payload read, artifact write, or DB access.
    if partition not in MATRIX_PARTITIONS:
        raise ForbiddenPartitionError(
            f"partition {partition!r} is forbidden: matrices exist only for {MATRIX_PARTITIONS}"
        )
    members = snapshot.members_for(partition)  # verbatim membership; counts derive here
    with engine.connect() as conn:
        _verify_operational_snapshot(conn, snapshot)
        payload_paths = {m.dataset_id: _payload_source(conn, m) for m in members}

    def provider(member: SnapshotMember) -> bytes:
        return payload_paths[member.dataset_id].read_bytes()

    build: MatrixBuild = build_partition_matrix(snapshot, partition, provider)
    payload = serialize_matrix(build.vectors)
    artifact_sha256, artifact_path = write_matrix_artifact(
        payload, partition_root=artifact_root / "l2e" / partition
    )
    if hashlib.sha256(artifact_path.read_bytes()).hexdigest() != artifact_sha256:
        raise ProfileArtifactHashMismatchError(  # pragma: no cover - atomic write verified
            "final matrix artifact bytes do not match artifact_sha256"
        )
    return persist_feature_matrix(
        engine,
        snapshot=snapshot,
        matrix=build.matrix,
        artifact_sha256=artifact_sha256,
        artifact_path=artifact_path,
        artifact_size=len(payload),
    )
