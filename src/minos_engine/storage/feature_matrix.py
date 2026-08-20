"""L2-E feature-matrix build + persistence (owner-run, schema-owner writes).

Storage-side counterpart of the pure ``layer2.features`` machinery: this module owns
every database write for L2-E matrices. No application role holds INSERT on any 0005
table; writes run as the schema owner (``minos_admin``).

Production API surface (the ONLY exported write entry points):
  * :func:`build_accepted_epoch1_feature_matrix` — THE production builder. Begins from
    ``load_accepted_epoch1_member_manifest(manifest_bytes)`` (pinned trust anchors; no
    caller FrozenSnapshot / ManifestTrustBundle / expected hashes / caller-selected
    identities), then builds + persists one partition matrix.
  * :func:`build_feature_matrix_with_trust` — explicitly TEST-ONLY generic builder for
    synthetic snapshots under a complete trust bundle. Production imports only the
    accepted boundary above.
  * :class:`PersistedFeatureMatrix` — the verified result.

The low-level persistence step (:func:`_persist_feature_matrix`) is PRIVATE and is not
an admissible external write path: it re-derives and re-verifies EVERYTHING from the
snapshot + matrix + vectors it is handed and from the artifact bytes it reads itself,
so no caller-claimed hash/size/path is ever trusted.

Persistence semantics (one transaction, verified before any insert):
  * a forged vector/matrix (``model_copy`` with recomputed vector_hash + matrix_hash but
    a stale ``feature_values_hash``) is rejected by the full re-verification BEFORE any
    row reaches ``feature_matrices`` / ``feature_matrix_members`` / ``catalog.artifacts``;
  * equal logical identity + verified-equal artifact → idempotent return of the STORED
    URI after fully re-verifying the stored artifact bytes + rows;
  * equal logical identity + different matrix_hash/artifact → :class:`MatrixConflictError`;
  * equal logical identity persisted under a different artifact root/URI →
    :class:`MatrixConflictError` (relocation is not an implicit operation);
  * equal artifact sha with different size/media/kind/uri → ArtifactMetadataConflictError;
  * content-addressed publication is immutable + no-clobber and carries the partition
    credential (gid + mode 0o640 on the inode before it is linked into place, verified
    after);
  * three distinct failure states are handled: (a) PRE-COMMIT failure (verification /
    insert / publish itself) → confirmed-uncommitted: rollback and unlink only an inode
    THIS attempt created; (b) AMBIGUOUS commit (``commit()`` raised — the server may have
    committed but the ack was lost) → the immutable content-addressed artifact is
    RETAINED and :class:`AmbiguousMatrixCommitError` is raised, so a committed row can
    never reference a missing file; (c) POST-COMMIT wrapper failure → the committed row +
    artifact are left intact;
  * concurrent unique violations are classified by constraint name.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import struct
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.exc import DBAPIError

from minos_engine.common.errors import (
    ArtifactMetadataConflictError,
    ContractValidationError,
    MatrixAccessError,
    MatrixConflictError,
)
from minos_engine.layer2.features.contracts import (
    FeatureMatrix,
    FeatureSetManifest,
    FeatureVector,
    build_feature_set_manifest,
)
from minos_engine.layer2.features.errors import (
    AmbiguousMatrixCommitError,
    ForbiddenPartitionError,
    MatrixArtifactIntegrityError,
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
    verify_matrix,
)
from minos_engine.layer2.features.matrix_parquet import (
    MATRIX_ARTIFACT_KIND,
    MATRIX_ARTIFACT_MEDIA_TYPE,
    PublishedArtifact,
    publish_matrix_artifact,
    serialize_matrix,
    verify_matrix_artifact,
)
from minos_engine.storage.database import verify_operational_database_identity
from minos_engine.storage.matrix_access import PartitionArtifactPublisher

__all__ = [
    "PersistedFeatureMatrix",
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
    #: The STORED, verified canonical artifact URI (never a caller-supplied path).
    artifact_path: str
    row_count: int
    idempotent: bool


# --------------------------------------------------------------------------- #
# constraint-name classification for concurrent unique violations
# (a test may exercise this, but it is NOT an admissible external write path)
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


def _advisory_key(sha256: str) -> int:
    """Deterministic signed int64 advisory-lock key from a content hash."""
    return int(struct.unpack("<q", bytes.fromhex(sha256)[:8])[0])


def _after_commit_hook() -> None:
    """Post-commit seam (no-op in production). Tests patch this to simulate a wrapper
    raising AFTER a successful commit, proving the committed row + artifact are retained
    (never unlinked)."""


def _commit_or_ambiguous(trans: Any, *, published: Any, artifact_sha256: str) -> None:
    """Commit; if commit RAISES the status is ambiguous (committed-but-ack-lost). The
    published immutable artifact is RETAINED and :class:`AmbiguousMatrixCommitError` is
    raised so no code path can unlink it — a committed DB row can never point at a
    missing file."""
    try:
        trans.commit()
    except BaseException as exc:  # noqa: BLE001 - re-raised as a typed ambiguity signal
        retained = "" if published is None else f" (retaining artifact {artifact_sha256[:12]}…)"
        raise AmbiguousMatrixCommitError(
            "commit raised; commit status is ambiguous (server may have committed but "
            f"the acknowledgement was lost){retained}"
        ) from exc


# --------------------------------------------------------------------------- #
# feature-set persistence (idempotent by content hash) — PRIVATE
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


def _persist_feature_set(conn: Connection, manifest: FeatureSetManifest) -> str:
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
# full re-verification (logical + payload) — trusts NOTHING from the caller
# --------------------------------------------------------------------------- #
def _reverify_or_raise(
    matrix: FeatureMatrix, snapshot: FrozenSnapshot, vectors: tuple[FeatureVector, ...]
) -> None:
    """Rerun the complete logical verification (membership, ordering, counts, bindings,
    per-vector value-kind checks, canonical feature_values_hash recomputed from the
    vector values, member/vector-hash binding, matrix_hash) and fail closed."""
    result = verify_matrix(matrix, snapshot, vectors)
    if not result.ok:
        raise MatrixArtifactIntegrityError(
            "matrix failed logical re-verification before persistence: "
            + ", ".join(result.failed())
        )


def _confine_within(path: Path, root: Path) -> Path:
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise MatrixArtifactIntegrityError(
            f"artifact path {path} cannot be resolved (missing or inaccessible): {exc}"
        ) from exc
    if not resolved.is_relative_to(root.resolve()):
        raise MatrixArtifactIntegrityError(
            f"artifact path {path} is not confined to the partition root {root}"
        )
    if not resolved.is_file():
        raise MatrixArtifactIntegrityError(f"artifact path {path} is not a regular file")
    return resolved


def _verify_stored_artifact(
    conn: Connection,
    *,
    artifact_id: str,
    expected_sha256: str,
    expected_size: int,
    expected_uri: str,
    partition_root: Path,
    matrix: FeatureMatrix,
    vectors: tuple[FeatureVector, ...],
) -> str:
    """Idempotent-replay artifact verification: the stored ``catalog.artifacts`` row and
    the physical bytes at its URI must fully verify. Returns the STORED URI."""
    row = (
        conn.execute(
            text(
                "SELECT id, uri, sha256, size_bytes, media_type, provenance "
                "FROM catalog.artifacts WHERE id = :i"
            ),
            {"i": artifact_id},
        )
        .mappings()
        .first()
    )
    if row is None:
        raise MatrixConflictError("matrix references a missing catalog.artifacts row")
    if (
        row["sha256"] != expected_sha256
        or int(row["size_bytes"]) != expected_size
        or row["media_type"] != MATRIX_ARTIFACT_MEDIA_TYPE
        or row["provenance"] != MATRIX_ARTIFACT_KIND
    ):
        raise ArtifactMetadataConflictError(
            "stored matrix artifact row has conflicting sha/size/media/provenance"
        )
    # relocation is not an implicit operation: a replay under a different root/URI
    # fails closed rather than silently rebinding the stored artifact.
    if row["uri"] != expected_uri:
        raise MatrixConflictError(
            "matrix already persisted under a different artifact URI/root "
            "(relocation requires an explicit, separately designed operation)"
        )
    stored_path = _confine_within(Path(str(row["uri"])), partition_root)
    stored_bytes = stored_path.read_bytes()
    if hashlib.sha256(stored_bytes).hexdigest() != expected_sha256:
        raise MatrixArtifactIntegrityError(
            "stored artifact bytes do not match the registered artifact_sha256"
        )
    checks = verify_matrix_artifact(stored_bytes, matrix, vectors, expected_sha256)
    if not all(checks.values()):
        raise MatrixArtifactIntegrityError(
            "stored artifact failed canonical Parquet verification: "
            + ", ".join(sorted(k for k, v in checks.items() if not v))
        )
    return str(row["uri"])


def _register_matrix_artifact(
    conn: Connection, *, uri: str, sha256: str, size_bytes: int
) -> tuple[str, bool]:
    """Race-safe content-addressed artifact registration (exact metadata match only).

    Returns ``(artifact_id, created)`` where ``created`` is True iff this call inserted
    the row (used to decide whether cleanup is permitted on rollback)."""
    params = {"h": sha256}
    select = text(
        "SELECT id, uri, size_bytes, media_type, provenance "
        "FROM catalog.artifacts WHERE sha256 = :h"
    )
    existing = conn.execute(select, params).mappings().first()
    created = False
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
        row = conn.execute(select, params).mappings().one()
        created = row["uri"] == uri
        existing = row
    if (
        existing["size_bytes"] != size_bytes
        or existing["media_type"] != MATRIX_ARTIFACT_MEDIA_TYPE
        or existing["provenance"] != MATRIX_ARTIFACT_KIND
        or existing["uri"] != uri
    ):
        raise ArtifactMetadataConflictError(
            f"artifact {sha256[:12]}… exists with conflicting metadata/provenance"
        )
    return str(existing["id"]), created


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


# --------------------------------------------------------------------------- #
# the single private persistence boundary — requires ALL verification material
# --------------------------------------------------------------------------- #
def _persist_feature_matrix(
    engine: Engine,
    *,
    snapshot: FrozenSnapshot,
    matrix: FeatureMatrix,
    vectors: tuple[FeatureVector, ...],
    publisher: PartitionArtifactPublisher,
    require_operational_identity: bool = False,
) -> PersistedFeatureMatrix:
    """Serialize, verify, publish (immutable no-clobber), and persist in ONE
    transaction. Trusts NO caller-supplied hash/size/path: bytes come from
    :func:`serialize_matrix` here, and the artifact is read back and re-verified.

    ``require_operational_identity`` defaults to ``False`` (the name-independent
    behaviour used by low-level persistence-mechanics tests on scratch databases). The
    production path never relies on that default: :func:`_build_feature_matrix` always
    threads an explicit value, and the accepted builder passes ``True``. When set (the
    accepted production path), the
    EXACT transactional connection is identity-verified immediately after the
    transaction begins — before any advisory lock, read-back, insert, artifact
    registration, or publication — so a mutation can only ever land on the canonical
    operational database. A failure here happens before ``published`` is ever assigned,
    so rollback leaves zero matrix/member/artifact rows and no published artifact."""
    manifest = build_feature_set_manifest()
    if matrix.feature_set_hash != manifest.feature_set_hash:
        raise ContractValidationError("matrix does not bind the canonical feature set")

    # (1) full logical re-verification BEFORE any bytes or rows — kills forged matrices.
    _reverify_or_raise(matrix, snapshot, vectors)

    # (2) identity-bound canonical bytes are produced HERE, not accepted from the caller.
    payload = serialize_matrix(matrix, vectors)
    artifact_sha256 = hashlib.sha256(payload).hexdigest()
    artifact_size = len(payload)

    # (3) payload-level re-verification over the bytes this function will publish.
    payload_checks = verify_matrix_artifact(payload, matrix, vectors, artifact_sha256)
    if not all(payload_checks.values()):
        raise MatrixArtifactIntegrityError(
            "candidate artifact failed canonical Parquet verification: "
            + ", ".join(sorted(k for k, v in payload_checks.items() if not v))
        )

    # (4) RE-VALIDATE the COMPLETE two-partition credential set at USE time — BEFORE any
    # DB mutation. Both roots must still exist as non-symlink directories with their
    # original (dev, inode), writer uid, exact mode 0o2750 and unchanged gids, remain
    # disjoint and carry distinct gids. This ``pre`` snapshot is re-checked for exact
    # equality immediately before publication; if either root changes during the
    # transaction the whole thing fails closed (rollback, no artifact).
    pre_credentials = publisher.credential_snapshot()
    root = Path(getattr(pre_credentials, matrix.partition).root)
    gid = getattr(pre_credentials, matrix.partition).gid
    final_path = root / f"{artifact_sha256}.parquet"

    conn = engine.connect()
    trans = conn.begin()
    published: PublishedArtifact | None = None
    committed = False
    try:
        # (0) identity-verify the EXACT transactional connection FIRST — before advisory
        # locks, reads, inserts, artifact registration, or publication. A prior check on
        # a separate engine connection is NOT sufficient; this binds the guarantee to the
        # connection the writes actually run on.
        if require_operational_identity:
            verify_operational_database_identity(conn)

        # serialize concurrent publishers of the same content across processes.
        conn.execute(
            text("SELECT pg_advisory_xact_lock(:k)"), {"k": _advisory_key(artifact_sha256)}
        )

        feature_set_id = _persist_feature_set(conn, manifest)
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
            stored_uri = _verify_stored_artifact(
                conn,
                artifact_id=str(existing["matrix_artifact_id"]),
                expected_sha256=artifact_sha256,
                expected_size=artifact_size,
                expected_uri=str(final_path),
                partition_root=root,
                matrix=matrix,
                vectors=vectors,
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
            _commit_or_ambiguous(trans, published=None, artifact_sha256=artifact_sha256)
            committed = True
            return PersistedFeatureMatrix(
                feature_matrix_id=str(existing["id"]),
                feature_set_id=feature_set_id,
                matrix_hash=matrix.matrix_hash,
                artifact_sha256=artifact_sha256,
                matrix_artifact_id=str(existing["matrix_artifact_id"]),
                artifact_path=stored_uri,
                row_count=matrix.row_count,
                idempotent=True,
            )

        artifact_id, _ = _register_matrix_artifact(
            conn, uri=str(final_path), sha256=artifact_sha256, size_bytes=artifact_size
        )
        matrix_id = str(
            conn.execute(
                text(
                    "INSERT INTO profiling.feature_matrices "
                    "(profile_snapshot_id, partition, feature_set_id, matrix_hash, "
                    " artifact_sha256, matrix_artifact_id, row_count, column_count) "
                    "VALUES (:s, :p, :f, :mh, :ah, :aid, :rc, :cc) RETURNING id"
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
            ).scalar_one()
        )
        for index, member in enumerate(matrix.members):
            conn.execute(
                text(
                    "INSERT INTO profiling.feature_matrix_members "
                    "(feature_matrix_id, dataset_registry_id, member_index, "
                    " vector_hash, feature_values_hash) VALUES (:m, :d, :i, :vh, :fh)"
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

        # (5) RE-VALIDATE the full two-partition credential set AGAIN, immediately before
        # publication, and require it to EXACTLY equal the pre-DB snapshot. If either
        # root was replaced/chmod'd/chgrp'd during the transaction this raises → the
        # transaction rolls back and NO artifact is published.
        post_credentials = publisher.credential_snapshot()
        if post_credentials != pre_credentials:
            raise MatrixAccessError(
                "partition credential set changed during the transaction — rolling back, "
                "no artifact published"
            )

        # (6) publish LAST, immediately before the commit boundary — with the partition
        # credential (gid + 0640) applied to the inode before it is linked into place.
        published = publish_matrix_artifact(payload, partition_root=root, gid=gid)
        # confirm the published file is a confined regular file of exactly our bytes.
        confined = _confine_within(published.path, root)
        if confined.read_bytes() != payload:
            raise MatrixArtifactIntegrityError(
                "published artifact bytes do not match the verified canonical payload"
            )

        # ---- COMMIT-AMBIGUITY BOUNDARY --------------------------------------------- #
        # A commit that RAISES is ambiguous (the server may have committed but the ack
        # was lost). On ambiguity the immutable content-addressed artifact is RETAINED,
        # never unlinked — a committed DB row can never reference a missing file.
        _commit_or_ambiguous(trans, published=published, artifact_sha256=artifact_sha256)
        committed = True
        _after_commit_hook()  # post-commit seam; a failure here must NOT unlink
        return PersistedFeatureMatrix(
            feature_matrix_id=matrix_id,
            feature_set_id=feature_set_id,
            matrix_hash=matrix.matrix_hash,
            artifact_sha256=artifact_sha256,
            matrix_artifact_id=artifact_id,
            artifact_path=str(final_path),
            row_count=matrix.row_count,
            idempotent=False,
        )
    except AmbiguousMatrixCommitError:
        # ambiguous commit status: retain the immutable orphan; do not touch the DB.
        raise
    except DBAPIError as exc:
        # confirmed-uncommitted: the failure is strictly before the commit boundary.
        if not committed:
            trans.rollback()
            if published is not None and published.created:
                with contextlib.suppress(FileNotFoundError):
                    final_path.unlink()
        classified = _classify_unique_violation(exc)
        if classified is exc:
            raise
        raise classified from exc
    except BaseException:
        # pre-commit failure → confirmed-uncommitted cleanup; post-commit wrapper
        # failure (committed=True) → leave the committed row + artifact intact.
        if not committed:
            trans.rollback()
            if published is not None and published.created:
                with contextlib.suppress(FileNotFoundError):
                    final_path.unlink()
        raise
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# public builders
# --------------------------------------------------------------------------- #
def build_accepted_epoch1_feature_matrix(
    engine: Engine,
    member_manifest_bytes: bytes,
    partition: str,
    *,
    publisher: PartitionArtifactPublisher,
) -> PersistedFeatureMatrix:
    """THE production E3 builder: accepted epoch-1 boundary only.

    Loads the accepted epoch-1 member manifest (pinned trust anchors; no caller-supplied
    identities) BEFORE touching the database, the payload provider, or the filesystem,
    and writes through the owner-side :class:`PartitionArtifactPublisher` (both frozen
    partition credentials validated at construction) — never a bare caller-supplied
    ``artifact_root``.

    Fail-closed ordering (defense in depth):
      1. the accepted epoch-1 manifest is loaded + identity-verified (pinned trust
         anchors) — a pure check that precedes ANY database access;
      2. a forbidden partition is rejected — also before any database access;
      3. ``require_operational_identity=True`` is threaded through so the canonical
         operational-store identity guard runs on the EXACT connections that actually
         read and write — the read connection inside :func:`_build_feature_matrix`
         (before the snapshot read or any payload resolution) and the transactional
         connection inside :func:`_persist_feature_matrix` (immediately after the
         transaction begins, before any advisory lock, insert, artifact registration, or
         publication). Each connection is verified once (a PostgreSQL session cannot
         switch databases); a check on any other/earlier connection is never treated as
         sufficient. The E4 production write path can therefore only ever mutate the real
         operational database (``current_database()`` == ``minos_engine_db``), never a
         scratch/staging/mistargeted one.
    """
    snapshot = load_accepted_epoch1_member_manifest(member_manifest_bytes)
    if partition not in MATRIX_PARTITIONS:
        raise ForbiddenPartitionError(
            f"partition {partition!r} is forbidden: matrices exist only for {MATRIX_PARTITIONS}"
        )
    return _build_feature_matrix(
        engine, snapshot, partition, publisher=publisher, require_operational_identity=True
    )


def build_feature_matrix_with_trust(
    engine: Engine,
    member_manifest_bytes: bytes,
    trust: ManifestTrustBundle,
    partition: str,
    *,
    artifact_root: Path,
) -> PersistedFeatureMatrix:
    """TEST-ONLY generic builder for synthetic snapshots under an explicit complete trust
    bundle. It constructs a :class:`PartitionArtifactPublisher` from the (test-provisioned)
    ``artifact_root`` — which itself validates the roots. Production imports and calls ONLY
    :func:`build_accepted_epoch1_feature_matrix` with an explicit publisher. It runs with
    ``require_operational_identity=False`` so synthetic snapshots on arbitrarily named
    scratch databases stay usable — the canonical-name guard is never imposed here."""
    snapshot = load_member_manifest_with_trust(member_manifest_bytes, trust)
    publisher = PartitionArtifactPublisher(
        train_root=artifact_root / "l2e" / "train",
        validation_root=artifact_root / "l2e" / "validation",
    )
    return _build_feature_matrix(
        engine, snapshot, partition, publisher=publisher, require_operational_identity=False
    )


def _build_feature_matrix(
    engine: Engine,
    snapshot: FrozenSnapshot,
    partition: str,
    *,
    publisher: PartitionArtifactPublisher,
    require_operational_identity: bool,
) -> PersistedFeatureMatrix:
    # test is rejected BEFORE any payload read, artifact write, or DB access.
    if partition not in MATRIX_PARTITIONS:
        raise ForbiddenPartitionError(
            f"partition {partition!r} is forbidden: matrices exist only for {MATRIX_PARTITIONS}"
        )
    members = snapshot.members_for(partition)  # verbatim membership; counts derive here
    with engine.connect() as conn:
        # identity-verify the EXACT read connection FIRST — before the snapshot identity
        # read or ANY payload-source resolution — on the accepted production path.
        if require_operational_identity:
            verify_operational_database_identity(conn)
        _verify_operational_snapshot(conn, snapshot)
        payload_paths = {m.dataset_id: _payload_source(conn, m) for m in members}

    def provider(member: SnapshotMember) -> bytes:
        return payload_paths[member.dataset_id].read_bytes()

    build: MatrixBuild = build_partition_matrix(snapshot, partition, provider)
    return _persist_feature_matrix(
        engine,
        snapshot=snapshot,
        matrix=build.matrix,
        vectors=build.vectors,
        publisher=publisher,
        require_operational_identity=require_operational_identity,
    )
