"""E5 independent feature-view verifier (owner-side, read-only).

Proves that a production feature matrix (train or validation) matches ACCEPTED upstream
and operational reality — not merely that a manifest is internally self-consistent. It
INDEPENDENTLY reconstructs the matrix from immutable accepted evidence and cross-binds
every identity:

  * the accepted epoch-1 snapshot is loaded through the PINNED trust anchors
    (:func:`load_accepted_epoch1_member_manifest`), so its snapshot / split / registry
    identities are fixed by code, not read from a mutable row;
  * the operational profile snapshot must reproduce that pinned identity;
  * the feature set (129 ordered columns + set/registry hash) is derived INTERNALLY from
    :func:`build_feature_set_manifest` — never supplied by a caller;
  * the matrix + vectors are rebuilt from the accepted membership and the exact accepted
    profile-artifact bytes, then re-serialized to canonical Parquet;
  * the independently rebuilt ``matrix_hash`` / ``artifact_sha256`` must equal the DB row
    AND the physical artifact bytes on disk;
  * each member's ``vector_hash`` / ``feature_values_hash`` in the DB must equal the
    independently recomputed values, which are in turn bound to the pinned snapshot.

Because the anchor is the immutable accepted snapshot + the real profile bytes, a
consistently-rehashed tamper (substitute a member/vector/value and recompute vector,
value, matrix, artifact and view hashes to stay internally consistent) still fails: the
independently reconstructed identity no longer matches the tampered row/artifact.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Connection, Engine

from minos_engine.common.errors import MatrixAccessError
from minos_engine.layer2.feature_registry import REGISTRY_HASH
from minos_engine.layer2.features.contracts import (
    EXPECTED_COLUMN_COUNT,
    FROZEN_FEATURE_SET_HASH,
    build_feature_set_manifest,
)
from minos_engine.layer2.features.extraction import (
    MATRIX_PARTITIONS,
    FrozenSnapshot,
    MatrixBuild,
    SnapshotMember,
    build_partition_matrix,
    load_accepted_epoch1_member_manifest,
    verify_matrix,
)
from minos_engine.layer2.features.feature_view import (
    FeatureViewManifest,
    FeatureViewMember,
    build_feature_view_manifest,
)
from minos_engine.layer2.features.matrix_parquet import (
    MATRIX_ARTIFACT_KIND,
    serialize_matrix,
    verify_matrix_artifact,
)
from minos_engine.storage.database import verify_operational_database_identity
from minos_engine.storage.feature_matrix import (
    _artifact_uri_to_path,
    _confine_within,
    _payload_source,
    _verify_operational_snapshot,
)

__all__ = [
    "ACCEPTED_EPOCH1_MANIFEST",
    "VerifiedFeatureView",
    "FeatureViewVerificationError",
    "verify_feature_view",
    "feature_view_cross_binding_checks",
]

#: The committed accepted epoch-1 member manifest (pinned trust anchors resolve it).
ACCEPTED_EPOCH1_MANIFEST = (
    Path(__file__).resolve().parents[3] / "manifests" / "profile_snapshot_epoch1_members.json"
)


class FeatureViewVerificationError(MatrixAccessError):
    """A feature view does not match independently derived accepted/operational state."""


@dataclass(frozen=True)
class VerifiedFeatureView:
    """The immutable result of a successful, independently verified feature view."""

    manifest: FeatureViewManifest
    checks: dict[str, bool]

    @property
    def ok(self) -> bool:
        return all(self.checks.values())


def _accepted_snapshot() -> FrozenSnapshot:
    return load_accepted_epoch1_member_manifest(ACCEPTED_EPOCH1_MANIFEST.read_bytes())


def _partition_root(conn: Connection, artifact_sha256: str) -> Path:
    """Resolve the physical artifact directory from the registered (bare-path) URI."""
    uri = conn.execute(
        text("SELECT uri FROM catalog.artifacts WHERE sha256 = :h AND provenance = :k"),
        {"h": artifact_sha256, "k": MATRIX_ARTIFACT_KIND},
    ).scalar_one()
    return _artifact_uri_to_path(str(uri)).parent


def _db_matrix_row(conn: Connection, partition: str, epoch: int) -> dict[str, Any]:
    row = (
        conn.execute(
            text(
                "SELECT fm.id, fm.matrix_hash, fm.artifact_sha256, fm.row_count, fm.column_count "
                "FROM profiling.feature_matrices fm "
                "JOIN profiling.profile_snapshots ps ON ps.id = fm.profile_snapshot_id "
                "WHERE fm.partition = :p AND ps.epoch = :e"
            ),
            {"p": partition, "e": epoch},
        )
        .mappings()
        .first()
    )
    if row is None:
        raise FeatureViewVerificationError(
            f"no operational feature matrix for partition {partition!r} epoch {epoch}"
        )
    return dict(row)


def _db_members(conn: Connection, matrix_id: str) -> list[dict[str, Any]]:
    return [
        dict(r)
        for r in conn.execute(
            text(
                "SELECT dr.dataset_id, mm.member_index, mm.vector_hash, mm.feature_values_hash "
                "FROM profiling.feature_matrix_members mm "
                "JOIN catalog.dataset_registry dr ON dr.id = mm.dataset_registry_id "
                "WHERE mm.feature_matrix_id = :i ORDER BY mm.member_index"
            ),
            {"i": matrix_id},
        ).mappings()
    ]


def verify_feature_view(engine: Engine, partition: str, *, epoch: int = 1) -> VerifiedFeatureView:
    """Independently verify + derive the canonical feature view for one partition.

    Fails closed (:class:`FeatureViewVerificationError`) on any discrepancy between the
    independently reconstructed identity and the operational DB row + physical artifact.
    Returns the deterministic :class:`FeatureViewManifest` on success. ``test`` is not a
    valid partition here (only ``train`` / ``validation``)."""
    if partition not in MATRIX_PARTITIONS:
        raise FeatureViewVerificationError(
            f"partition must be one of {MATRIX_PARTITIONS}, not {partition!r}"
        )

    # (a) feature set derived INTERNALLY — never caller-supplied.
    feature_set = build_feature_set_manifest()

    # (b) accepted snapshot via PINNED trust anchors (immutable code-owned identity).
    snapshot = _accepted_snapshot()

    with engine.connect() as conn:
        # (c) canonical operational DB, and the DB snapshot reproduces the pinned identity.
        verify_operational_database_identity(conn)
        _verify_operational_snapshot(conn, snapshot)

        members: tuple[SnapshotMember, ...] = snapshot.members_for(partition)
        payload_paths = {m.dataset_id: _payload_source(conn, m) for m in members}

        # (d) INDEPENDENT reconstruction from accepted membership + real profile bytes.
        build: MatrixBuild = build_partition_matrix(
            snapshot, partition, lambda m: payload_paths[m.dataset_id].read_bytes()
        )
        payload = serialize_matrix(build.matrix, build.vectors)
        derived_sha = hashlib.sha256(payload).hexdigest()
        logical = verify_matrix(build.matrix, snapshot, build.vectors)

        # (e) operational DB row + members.
        db = _db_matrix_row(conn, partition, epoch)
        db_members = _db_members(conn, str(db["id"]))
        db_snap_hash = str(
            conn.execute(
                text("SELECT snapshot_hash FROM profiling.profile_snapshots WHERE epoch = :e"),
                {"e": epoch},
            ).scalar_one()
        )

        # (f) physical artifact bytes on disk.
        root = _partition_root(conn, str(db["artifact_sha256"]))
        artifact_path = _confine_within(root / f"{derived_sha}.parquet", root)
        artifact_bytes = artifact_path.read_bytes()

    # cross-binding checks (every one must hold).
    checks = feature_view_cross_binding_checks(
        feature_set=feature_set,
        snapshot=snapshot,
        build=build,
        derived_sha=derived_sha,
        logical_ok=logical.ok,
        db_row=db,
        db_members=db_members,
        db_snapshot_hash=db_snap_hash,
        artifact_bytes=artifact_bytes,
    )
    if not all(checks.values()):
        failed = ", ".join(k for k, v in checks.items() if not v)
        raise FeatureViewVerificationError(f"feature-view verification failed: {failed}")

    fv_members = tuple(
        FeatureViewMember(
            dataset_id=r["dataset_id"],
            member_index=int(r["member_index"]),
            vector_hash=str(r["vector_hash"]),
            feature_values_hash=str(r["feature_values_hash"]),
        )
        for r in db_members
    )
    manifest = build_feature_view_manifest(
        epoch=epoch,
        partition=partition,
        snapshot_hash=snapshot.snapshot_hash,
        split_manifest_hash=snapshot.split_manifest_hash,
        registry_snapshot_hash=snapshot.registry_snapshot_hash,
        matrix_hash=build.matrix.matrix_hash,
        artifact_sha256=derived_sha,
        row_count=len(members),
        members=fv_members,
        feature_set=feature_set,
    )
    return VerifiedFeatureView(manifest=manifest, checks=checks)


def feature_view_cross_binding_checks(
    *,
    feature_set: Any,
    snapshot: FrozenSnapshot,
    build: MatrixBuild,
    derived_sha: str,
    logical_ok: bool,
    db_row: dict[str, Any],
    db_members: list[dict[str, Any]],
    db_snapshot_hash: str,
    artifact_bytes: bytes,
) -> dict[str, bool]:
    """Pure cross-binding: every independently-derived identity vs the observed DB row,
    DB members, physical artifact bytes, and pinned snapshot. Any mismatch → a False
    entry (fail closed). Separated from I/O so tamper attacks can be exercised directly.

    A consistently-rehashed attack (substitute a member / vector / value and recompute
    all local hashes) still fails here: ``build`` is reconstructed from the ACCEPTED
    snapshot + real profile bytes, so tampered DB/artifact values no longer match."""
    payload_checks = verify_matrix_artifact(
        artifact_bytes, build.matrix, build.vectors, derived_sha
    )
    return {
        "canonical_feature_set_129": feature_set.column_count == EXPECTED_COLUMN_COUNT,
        "feature_set_hash_pinned": feature_set.feature_set_hash == FROZEN_FEATURE_SET_HASH,
        "feature_registry_hash_pinned": feature_set.registry_hash == REGISTRY_HASH,
        "snapshot_identity_pinned": snapshot.snapshot_hash == db_snapshot_hash,
        "logical_reverification_ok": logical_ok,
        "db_matrix_hash_matches_derived": db_row["matrix_hash"] == build.matrix.matrix_hash,
        "db_artifact_sha_matches_derived": db_row["artifact_sha256"] == derived_sha,
        "db_row_count_matches_membership": int(db_row["row_count"]) == len(build.matrix.members),
        "db_column_count_129": int(db_row["column_count"]) == EXPECTED_COLUMN_COUNT,
        "db_member_count_matches": len(db_members) == len(build.matrix.members),
        "physical_artifact_sha_matches": hashlib.sha256(artifact_bytes).hexdigest() == derived_sha,
        "physical_artifact_reverifies": all(payload_checks.values()),
        "member_order_and_hashes_bound": _members_bound(db_members, build),
    }


def _members_bound(db_members: list[dict[str, Any]], build: MatrixBuild) -> bool:
    """DB member rows must equal the independently reconstructed matrix members: same
    dataset_id + contiguous index + vector_hash, and each feature_values_hash equals the
    independently recomputed vector's value hash (bound to accepted payloads)."""
    if len(db_members) != len(build.matrix.members):
        return False
    vec_by_id = {v.dataset_id: v for v in build.vectors}
    for index, (row, member) in enumerate(zip(db_members, build.matrix.members, strict=True)):
        vec = vec_by_id.get(member.dataset_id)
        if vec is None:
            return False
        if (
            row["dataset_id"] != member.dataset_id
            or int(row["member_index"]) != index
            or row["vector_hash"] != member.vector_hash
            or row["vector_hash"] != vec.vector_hash
            or row["feature_values_hash"] != vec.feature_values_hash
        ):
            return False
    return True
