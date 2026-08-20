"""E4 operational feature-matrix materialization (owner-run, source-bound).

The single production orchestration for E4: materialize ONLY the accepted epoch-1
``train`` and ``validation`` feature matrices into the canonical operational database,
through the pinned accepted trust boundary
(:func:`build_accepted_epoch1_feature_matrix`) and the mandatory owner-side
:class:`PartitionArtifactPublisher`. It:

  * consumes only the accepted epoch-1 member manifest (pinned trust anchors) — the
    caller passes the committed manifest bytes; identities are never caller-selected;
  * verifies the canonical operational database on the EXACT read and transaction
    connections (via the accepted builder's ``require_operational_identity=True`` path);
  * takes EXPLICITLY configured ``train`` and ``validation`` artifact roots — nothing is
    invented; missing / symlinked / mis-owned / mis-permissioned roots are rejected by
    the publisher, and the credential-proof fixture root is rejected outright;
  * materializes ONLY ``train`` and ``validation`` — ``test`` is never built (it is not
    in ``MATRIX_PARTITIONS`` and the accepted builder rejects it before any DB access);
  * derives every row count from frozen snapshot membership, uses the frozen 129
    BAM-only feature columns and the Parquet v2 contract, and performs logical,
    payload-byte, DB-row and credential verification inside one transaction per matrix;
  * is idempotent (equal input → verified replay of the stored artifact) and
    conflict-detecting (a divergent logical identity fails typed);
  * emits HASHES-ONLY metadata (:func:`matrix_metadata`) — no plaintext feature vectors,
    no credentials, and no retrievable artifact URI.

Provisioning production roots (mode ``0o2750``, distinct partition gids) is a deliberate
deployment step (:func:`minos_engine.storage.matrix_access.provision_partition_root`);
this module never creates roots and never falls back to a temporary or proof directory.
"""

from __future__ import annotations

import os
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.engine import Engine

from minos_engine.common.errors import MatrixAccessError
from minos_engine.layer2.features.contracts import build_feature_set_manifest
from minos_engine.layer2.features.extraction import MATRIX_PARTITIONS
from minos_engine.layer2.features.matrix_parquet import (
    ARTIFACT_FILE_MODE,
    MATRIX_ARTIFACT_KIND,
    MATRIX_ARTIFACT_MEDIA_TYPE,
    PARQUET_SCHEMA_VERSION,
)
from minos_engine.storage.feature_matrix import (
    PersistedFeatureMatrix,
    build_accepted_epoch1_feature_matrix,
)
from minos_engine.storage.matrix_access import PartitionArtifactPublisher

__all__ = [
    "CREDENTIAL_PROOF_ROOT",
    "PRODUCTION_PARTITIONS",
    "E4_MATRIX_METADATA_SCHEMA",
    "build_operational_feature_matrices",
    "matrix_metadata",
]

#: The credential-proof harness fixture directory. It is NOT production storage and is
#: refused as a matrix root (it is provisioned only for the cross-identity PASS proof).
CREDENTIAL_PROOF_ROOT = Path("/var/lib/minos/l2e_cred_proof")

#: The ONLY partitions E4 materializes. ``test`` is never built.
PRODUCTION_PARTITIONS: tuple[str, ...] = ("train", "validation")

#: Canonical schema id for the hashes-only per-partition metadata manifest.
E4_MATRIX_METADATA_SCHEMA = "l2e-e4-matrix-metadata-v1"


def _canonical_abs(path: Path) -> Path:
    """Absolute, symlink-resolved (as far as it exists) path — WITHOUT requiring the
    path to exist (so the proof-root check works before provisioning)."""
    return Path(os.path.abspath(os.path.realpath(str(path))))


def _reject_proof_root(partition: str, root: Path) -> None:
    """Refuse the credential-proof fixture directory (or anything inside it) as a
    production matrix root."""
    resolved = _canonical_abs(root)
    proof = _canonical_abs(CREDENTIAL_PROOF_ROOT)
    if resolved == proof or proof in resolved.parents:
        raise MatrixAccessError(
            f"{partition} root {root} is the credential-proof fixture "
            f"{CREDENTIAL_PROOF_ROOT} (or inside it); that directory is NOT production "
            "storage — configure explicit production artifact roots"
        )


def build_operational_feature_matrices(
    engine: Engine,
    *,
    member_manifest_bytes: bytes,
    train_root: Path,
    validation_root: Path,
) -> dict[str, PersistedFeatureMatrix]:
    """Materialize the accepted epoch-1 ``train`` and ``validation`` matrices only.

    Rejects the credential-proof fixture root, constructs the owner-side publisher (which
    validates both roots — existence, non-symlink, exact ``0o2750``, writer ownership,
    disjoint, distinct gids), then builds each production partition through the pinned
    accepted boundary. Returns the two verified persistence results keyed by partition.
    ``test`` is never materialized.
    """
    for partition, root in (("train", train_root), ("validation", validation_root)):
        _reject_proof_root(partition, root)

    # The publisher is the mandatory write boundary: it validates both roots on the GIVEN
    # (unresolved) paths first and never creates missing roots.
    publisher = PartitionArtifactPublisher(train_root=train_root, validation_root=validation_root)

    results: dict[str, PersistedFeatureMatrix] = {}
    for partition in PRODUCTION_PARTITIONS:
        assert partition in MATRIX_PARTITIONS  # test is structurally excluded
        results[partition] = build_accepted_epoch1_feature_matrix(
            engine, member_manifest_bytes, partition, publisher=publisher
        )
    return results


def matrix_metadata(
    engine: Engine,
    persisted: PersistedFeatureMatrix,
    *,
    partition: str,
    publisher: PartitionArtifactPublisher,
) -> dict[str, object]:
    """Build the HASHES-ONLY metadata manifest for one persisted matrix.

    Contains only identities, hashes, counts, ordering, and permission/gid facts — never
    a plaintext feature vector, a credential secret, or a retrievable artifact URI. The
    member ordering carries ``vector_hash`` and ``feature_values_hash`` (hashes), never
    the underlying values.
    """
    if partition not in PRODUCTION_PARTITIONS:
        raise MatrixAccessError(f"{partition!r} is not a production partition")
    manifest = build_feature_set_manifest()
    with engine.connect() as conn:
        row = (
            conn.execute(
                text(
                    "SELECT fm.matrix_hash, fm.artifact_sha256, fm.row_count, fm.column_count, "
                    " ps.epoch, ps.snapshot_hash, ps.split_manifest_hash, "
                    " ps.registry_snapshot_hash, ps.member_count "
                    "FROM profiling.feature_matrices fm "
                    "JOIN profiling.profile_snapshots ps ON ps.id = fm.profile_snapshot_id "
                    "WHERE fm.id = :i"
                ),
                {"i": persisted.feature_matrix_id},
            )
            .mappings()
            .one()
        )
        members = conn.execute(
            text(
                "SELECT dr.dataset_id, mm.member_index, mm.vector_hash, mm.feature_values_hash "
                "FROM profiling.feature_matrix_members mm "
                "JOIN catalog.dataset_registry dr ON dr.id = mm.dataset_registry_id "
                "WHERE mm.feature_matrix_id = :i ORDER BY mm.member_index"
            ),
            {"i": persisted.feature_matrix_id},
        ).all()

    credential = getattr(publisher.credential_snapshot(), partition)
    return {
        "schema": E4_MATRIX_METADATA_SCHEMA,
        "partition": partition,
        "epoch": int(row["epoch"]),
        "snapshot_hash": row["snapshot_hash"],
        "split_manifest_hash": row["split_manifest_hash"],
        "registry_snapshot_hash": row["registry_snapshot_hash"],
        "snapshot_member_count": int(row["member_count"]),
        "feature_set_hash": manifest.feature_set_hash,
        "registry_hash": manifest.registry_hash,
        "column_count": int(row["column_count"]),
        "parquet_schema_version": PARQUET_SCHEMA_VERSION,
        "artifact_media_type": MATRIX_ARTIFACT_MEDIA_TYPE,
        "artifact_kind": MATRIX_ARTIFACT_KIND,
        "matrix_hash": row["matrix_hash"],
        "artifact_sha256": row["artifact_sha256"],
        "row_count": int(row["row_count"]),
        "idempotent_replay": bool(persisted.idempotent),
        "members": [
            {
                "dataset_id": m.dataset_id,
                "member_index": int(m.member_index),
                "vector_hash": m.vector_hash,
                "feature_values_hash": m.feature_values_hash,
            }
            for m in members
        ],
        "artifact_mode": oct(ARTIFACT_FILE_MODE),
        "root_mode": oct(credential.mode),
        "partition_gid": int(credential.gid),
    }
