"""E3 persistence semantics on real PostgreSQL: idempotency, conflicts, atomicity."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.exc import DBAPIError

from minos_engine.common.errors import (
    ArtifactMetadataConflictError,
    MatrixConflictError,
)
from minos_engine.layer2.features.contracts import (
    FeatureMatrix,
    MatrixMember,
    build_feature_set_manifest,
    vector_hash,
)
from minos_engine.layer2.features.extraction import (
    build_partition_matrix,
    load_member_manifest_with_trust,
)
from minos_engine.layer2.features.matrix_parquet import serialize_matrix, write_matrix_artifact
from minos_engine.storage.feature_matrix import (
    build_feature_matrix_with_trust,
    persist_feature_matrix,
    persist_feature_set,
)


def _forged_build(snap, artifact_root):
    """A consistently re-hashed forgery of the snapshot's train matrix (valid kinds)."""
    snapshot = load_member_manifest_with_trust(snap.manifest_bytes, snap.trust)
    build = build_partition_matrix(
        snapshot, "train", lambda m: snap.payload_paths[m.dataset_id].read_bytes()
    )
    forged_values = list(build.vectors[0].values)
    forged_values[0] = 0.111111  # REAL column mutation, kinds stay valid
    forged_vector = build.vectors[0].model_copy(update={"values": tuple(forged_values)})
    forged_vector = forged_vector.model_copy(update={"vector_hash": vector_hash(forged_vector)})
    vectors = sorted(
        [forged_vector] + [v for v in build.vectors if v.dataset_id != forged_vector.dataset_id],
        key=lambda v: v.dataset_id,
    )
    matrix = FeatureMatrix(
        epoch=snapshot.epoch,
        snapshot_hash=snapshot.snapshot_hash,
        partition="train",
        registry_hash=build.matrix.registry_hash,
        feature_set_hash=build.matrix.feature_set_hash,
        row_count=len(vectors),
        column_count=129,
        members=tuple(
            MatrixMember(dataset_id=v.dataset_id, vector_hash=v.vector_hash) for v in vectors
        ),
    )
    payload = serialize_matrix(vectors)
    sha, path = write_matrix_artifact(payload, partition_root=artifact_root / "l2e" / "train")
    return snapshot, matrix, sha, path, len(payload)


def test_feature_set_idempotent_and_content_checked(l2e_engine: Engine, built) -> None:
    manifest = build_feature_set_manifest()
    with l2e_engine.begin() as conn:
        first = persist_feature_set(conn, manifest)
        second = persist_feature_set(conn, manifest)
    assert first == second
    with l2e_engine.connect() as conn:
        count = conn.execute(text("SELECT count(*) FROM profiling.feature_sets")).scalar()
    assert count == 1  # one canonical set row, no matter how many builds ran


def test_matrix_rebuild_is_idempotent(l2e_engine: Engine, snap_a, artifact_root, built) -> None:
    replay = build_feature_matrix_with_trust(
        l2e_engine, snap_a.manifest_bytes, snap_a.trust, "train", artifact_root=artifact_root
    )
    original = built[("a", "train")]
    assert replay.idempotent is True
    assert replay.feature_matrix_id == original.feature_matrix_id
    assert replay.matrix_hash == original.matrix_hash
    assert replay.artifact_sha256 == original.artifact_sha256
    with l2e_engine.connect() as conn:
        count = conn.execute(
            text(
                "SELECT count(*) FROM profiling.feature_matrices fm "
                "JOIN profiling.profile_snapshots ps ON ps.id = fm.profile_snapshot_id "
                "WHERE ps.epoch = 1 AND fm.partition = 'train'"
            )
        ).scalar()
    assert count == 1


def test_conflicting_logical_identity_is_typed(
    l2e_engine: Engine, snap_a, artifact_root, built
) -> None:
    snapshot, forged_matrix, sha, path, size = _forged_build(snap_a, artifact_root)
    assert forged_matrix.matrix_hash != built[("a", "train")].matrix_hash
    with pytest.raises(MatrixConflictError):
        persist_feature_matrix(
            l2e_engine,
            snapshot=snapshot,
            matrix=forged_matrix,
            artifact_sha256=sha,
            artifact_path=path,
            artifact_size=size,
        )


def test_artifact_metadata_conflict_is_typed(
    l2e_engine: Engine, extra_snaps, artifact_root
) -> None:
    snap = extra_snaps[3]
    snapshot = load_member_manifest_with_trust(snap.manifest_bytes, snap.trust)
    build = build_partition_matrix(
        snapshot, "train", lambda m: snap.payload_paths[m.dataset_id].read_bytes()
    )
    payload = serialize_matrix(build.vectors)
    sha, path = write_matrix_artifact(payload, partition_root=artifact_root / "l2e" / "train")
    # a pre-existing artifact row with the SAME sha but conflicting provenance.
    with l2e_engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO catalog.artifacts (uri, sha256, size_bytes, media_type, "
                " provenance) VALUES (:u, :h, :s, 'application/other', 'other:kind')"
            ),
            {"u": str(path), "h": sha, "s": len(payload)},
        )
    with pytest.raises(ArtifactMetadataConflictError):
        persist_feature_matrix(
            l2e_engine,
            snapshot=snapshot,
            matrix=build.matrix,
            artifact_sha256=sha,
            artifact_path=path,
            artifact_size=len(payload),
        )
    # the failed persistence left NO matrix or member rows (single transaction).
    with l2e_engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT count(*) FROM profiling.feature_matrices fm "
                "JOIN profiling.profile_snapshots ps ON ps.id = fm.profile_snapshot_id "
                "WHERE ps.epoch = 3"
            )
        ).scalar()
    assert rows == 0


def test_atomic_rollback_leaves_no_partial_rows(
    l2e_engine: Engine, extra_snaps, artifact_root, monkeypatch
) -> None:
    import minos_engine.storage.feature_matrix as fm

    snap = extra_snaps[4]

    def _explode(*args, **kwargs):
        raise RuntimeError("injected failure after member insert")

    monkeypatch.setattr(fm, "_read_back_and_verify", _explode)
    with pytest.raises(RuntimeError, match="injected"):
        build_feature_matrix_with_trust(
            l2e_engine, snap.manifest_bytes, snap.trust, "train", artifact_root=artifact_root
        )
    monkeypatch.undo()
    with l2e_engine.connect() as conn:
        matrices = conn.execute(
            text(
                "SELECT count(*) FROM profiling.feature_matrices fm "
                "JOIN profiling.profile_snapshots ps ON ps.id = fm.profile_snapshot_id "
                "WHERE ps.epoch = 4"
            )
        ).scalar()
        members = conn.execute(
            text(
                "SELECT count(*) FROM profiling.feature_matrix_members mm "
                "JOIN profiling.feature_matrices fm ON fm.id = mm.feature_matrix_id "
                "JOIN profiling.profile_snapshots ps ON ps.id = fm.profile_snapshot_id "
                "WHERE ps.epoch = 4"
            )
        ).scalar()
    assert matrices == 0 and members == 0


def test_update_and_delete_rejected_on_all_three_tables(l2e_engine: Engine, built) -> None:
    statements = (
        "UPDATE profiling.feature_sets SET column_count = column_count",
        "DELETE FROM profiling.feature_sets",
        "UPDATE profiling.feature_matrices SET row_count = row_count",
        "DELETE FROM profiling.feature_matrices",
        "UPDATE profiling.feature_matrix_members SET member_index = member_index",
        "DELETE FROM profiling.feature_matrix_members",
    )
    for statement in statements:
        with pytest.raises(DBAPIError), l2e_engine.begin() as conn:
            conn.execute(text(statement))


def test_no_plaintext_feature_values_in_database(l2e_engine: Engine, built) -> None:
    with l2e_engine.connect() as conn:
        columns = {
            r[0]
            for r in conn.execute(
                text(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_schema = 'profiling' "
                    "AND table_name = 'feature_matrix_members'"
                )
            )
        }
    assert columns == {
        "id",
        "feature_matrix_id",
        "dataset_registry_id",
        "member_index",
        "vector_hash",
        "feature_values_hash",
        "created_at",
    }  # hashes + indices only — no value storage exists


def test_persisted_rows_match_verified_content_field_for_field(
    l2e_engine: Engine, snap_a, built
) -> None:
    result = built[("a", "train")]
    snapshot = load_member_manifest_with_trust(snap_a.manifest_bytes, snap_a.trust)
    expected_members = snapshot.members_for("train")
    with l2e_engine.connect() as conn:
        row = conn.execute(
            text(
                "SELECT fm.matrix_hash, fm.artifact_sha256, fm.row_count, fm.column_count, "
                " fs.feature_set_hash, fs.registry_hash, ps.snapshot_hash, art.sha256, art.uri "
                "FROM profiling.feature_matrices fm "
                "JOIN profiling.feature_sets fs ON fs.id = fm.feature_set_id "
                "JOIN profiling.profile_snapshots ps ON ps.id = fm.profile_snapshot_id "
                "JOIN catalog.artifacts art ON art.id = fm.matrix_artifact_id "
                "WHERE fm.id = :i"
            ),
            {"i": result.feature_matrix_id},
        ).one()
        db_members = conn.execute(
            text(
                "SELECT dr.dataset_id, mm.member_index, mm.vector_hash, mm.feature_values_hash "
                "FROM profiling.feature_matrix_members mm "
                "JOIN catalog.dataset_registry dr ON dr.id = mm.dataset_registry_id "
                "WHERE mm.feature_matrix_id = :i ORDER BY mm.member_index"
            ),
            {"i": result.feature_matrix_id},
        ).all()
    assert row.matrix_hash == result.matrix_hash
    assert row.artifact_sha256 == result.artifact_sha256 == row.sha256
    assert int(row.row_count) == 4 and int(row.column_count) == 129  # membership-derived
    assert row.feature_set_hash == build_feature_set_manifest().feature_set_hash
    assert row.snapshot_hash == snapshot.snapshot_hash
    assert [m.dataset_id for m in db_members] == [m.dataset_id for m in expected_members]
    assert [int(m.member_index) for m in db_members] == list(range(4))
    for db_member, member in zip(db_members, expected_members, strict=True):
        assert db_member.feature_values_hash == member.feature_values_hash
    payload = Path(row.uri).read_bytes()
    assert hashlib.sha256(payload).hexdigest() == result.artifact_sha256
