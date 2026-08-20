"""E3 persistence semantics on real PostgreSQL: no-bypass verification, replay, atomicity."""

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
from minos_engine.layer2.features.errors import MatrixArtifactIntegrityError
from minos_engine.layer2.features.extraction import (
    build_partition_matrix,
    load_member_manifest_with_trust,
)
from minos_engine.layer2.features.matrix_parquet import serialize_matrix
from minos_engine.storage import feature_matrix as fm
from minos_engine.storage.feature_matrix import (
    _classify_unique_violation,
    _persist_feature_matrix,
    build_feature_matrix_with_trust,
)


def _forge(snap, *, index: int = 0, value: float = 0.123456):
    """Honest build of a snapshot's train partition, then a consistently rehashed
    forgery: value change → vector_hash recomputed → MatrixMember rebound → matrix_hash
    recomputed → Parquet reserializable, but the OLD feature_values_hash is retained."""
    snapshot = load_member_manifest_with_trust(snap.manifest_bytes, snap.trust)
    build = build_partition_matrix(
        snapshot, "train", lambda m: snap.payload_paths[m.dataset_id].read_bytes()
    )
    values = list(build.vectors[0].values)
    values[index] = value  # REAL column: kinds stay valid, only fvh goes stale
    forged_vec = build.vectors[0].model_copy(update={"values": tuple(values)})
    forged_vec = forged_vec.model_copy(update={"vector_hash": vector_hash(forged_vec)})
    vectors = tuple(
        sorted(
            [forged_vec] + [v for v in build.vectors if v.dataset_id != forged_vec.dataset_id],
            key=lambda v: v.dataset_id,
        )
    )
    forged_matrix = FeatureMatrix(
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
    return snapshot, forged_matrix, vectors, build.matrix.matrix_hash


def _count(engine: Engine, epoch: int, partition: str = "train") -> int:
    with engine.connect() as conn:
        return int(
            conn.execute(
                text(
                    "SELECT count(*) FROM profiling.feature_matrices fm "
                    "JOIN profiling.profile_snapshots ps ON ps.id = fm.profile_snapshot_id "
                    "WHERE ps.epoch = :e AND fm.partition = :p"
                ),
                {"e": epoch, "p": partition},
            ).scalar_one()
        )


# --------------------------------------------------------------------------- #
# item 1 — the persistence boundary re-verifies from vectors+bytes (no bypass)
# --------------------------------------------------------------------------- #
def test_forged_matrix_rejected_before_any_insert(l2e_engine, extra_snaps, artifact_root) -> None:
    snap = extra_snaps[3]  # fresh logical identity (epoch 3 train, never built)
    snapshot, forged_matrix, forged_vectors, honest_hash = _forge(snap)
    assert forged_matrix.matrix_hash != honest_hash
    with pytest.raises(MatrixArtifactIntegrityError, match="feature_values_hashes_recomputed"):
        _persist_feature_matrix(
            l2e_engine,
            snapshot=snapshot,
            matrix=forged_matrix,
            vectors=forged_vectors,
            partition_root=artifact_root,
        )
    # nothing reached feature_matrices / members / catalog.artifacts for this identity.
    assert _count(l2e_engine, 3, "train") == 0
    with l2e_engine.connect() as conn:
        members = conn.execute(
            text(
                "SELECT count(*) FROM profiling.feature_matrix_members mm "
                "JOIN profiling.feature_matrices m ON m.id = mm.feature_matrix_id "
                "JOIN profiling.profile_snapshots ps ON ps.id = m.profile_snapshot_id "
                "WHERE ps.epoch = 3"
            )
        ).scalar_one()
        forged_payload_sha = hashlib.sha256(
            serialize_matrix(forged_matrix, forged_vectors)
        ).hexdigest()
        art = conn.execute(
            text("SELECT count(*) FROM catalog.artifacts WHERE sha256 = :h"),
            {"h": forged_payload_sha},
        ).scalar_one()
    assert members == 0 and art == 0


def test_persistence_recomputes_hash_and_size_from_own_bytes(l2e_engine, snap_a, built) -> None:
    result = built[("a", "train")]
    snapshot = load_member_manifest_with_trust(snap_a.manifest_bytes, snap_a.trust)
    build = build_partition_matrix(
        snapshot, "train", lambda m: snap_a.payload_paths[m.dataset_id].read_bytes()
    )
    payload = serialize_matrix(build.matrix, build.vectors)
    assert result.artifact_sha256 == hashlib.sha256(payload).hexdigest()
    stored = Path(result.artifact_path)
    assert stored.read_bytes() == payload
    assert stored.stat().st_size == len(payload)


# --------------------------------------------------------------------------- #
# item 2 — idempotent replay fully verifies the stored artifact
# --------------------------------------------------------------------------- #
def _rebuild(engine, snap, partition="train", *, artifact_root):
    return build_feature_matrix_with_trust(
        engine, snap.manifest_bytes, snap.trust, partition, artifact_root=artifact_root
    )


def test_replay_same_bytes_returns_stored_uri(l2e_engine, snap_a, built, artifact_root) -> None:
    original = built[("a", "train")]
    replay = _rebuild(l2e_engine, snap_a, artifact_root=artifact_root)
    assert replay.idempotent is True
    assert replay.feature_matrix_id == original.feature_matrix_id
    assert replay.artifact_path == original.artifact_path  # the STORED uri, verified
    assert _count(l2e_engine, 1, "train") == 1


def test_replay_under_different_root_is_typed_conflict(
    l2e_engine, snap_a, built, tmp_path_factory
) -> None:
    other_root = tmp_path_factory.mktemp("l2e_other_root")
    for part in ("train", "validation"):
        (other_root / "l2e" / part).mkdir(parents=True, exist_ok=True)
    with pytest.raises(MatrixConflictError, match="different artifact URI/root"):
        _rebuild(l2e_engine, snap_a, artifact_root=other_root)


def test_replay_detects_tampered_catalog_uri(l2e_engine, snap_a, built, artifact_root) -> None:
    art_id = built[("a", "train")].matrix_artifact_id
    with l2e_engine.begin() as conn:
        conn.execute(
            text("UPDATE catalog.artifacts SET uri = :u WHERE id = :i"),
            {"u": "/tmp/relocated.parquet", "i": art_id},
        )
    try:
        with pytest.raises(MatrixConflictError, match="different artifact URI/root"):
            _rebuild(l2e_engine, snap_a, artifact_root=artifact_root)
    finally:
        with l2e_engine.begin() as conn:
            conn.execute(
                text("UPDATE catalog.artifacts SET uri = :u WHERE id = :i"),
                {"u": built[("a", "train")].artifact_path, "i": art_id},
            )


@pytest.mark.parametrize(
    ("field", "value"),
    [("size_bytes", 999999), ("media_type", "application/other"), ("provenance", "other:kind")],
)
def test_replay_detects_wrong_stored_metadata(
    l2e_engine, snap_a, built, artifact_root, field, value
) -> None:
    art_id = built[("a", "train")].matrix_artifact_id
    with l2e_engine.begin() as conn:
        original = conn.execute(
            text(f"SELECT {field} AS v FROM catalog.artifacts WHERE id = :i"),  # noqa: S608
            {"i": art_id},
        ).scalar_one()
        conn.execute(
            text(f"UPDATE catalog.artifacts SET {field} = :v WHERE id = :i"),  # noqa: S608
            {"v": value, "i": art_id},
        )
    try:
        with pytest.raises(ArtifactMetadataConflictError):
            _rebuild(l2e_engine, snap_a, artifact_root=artifact_root)
    finally:
        with l2e_engine.begin() as conn:
            conn.execute(
                text(f"UPDATE catalog.artifacts SET {field} = :v WHERE id = :i"),  # noqa: S608
                {"v": original, "i": art_id},
            )


def test_replay_detects_missing_stored_file(l2e_engine, snap_a, built, artifact_root) -> None:
    path = Path(built[("a", "train")].artifact_path)
    backup = path.read_bytes()
    path.unlink()
    try:
        with pytest.raises(MatrixArtifactIntegrityError, match="cannot be resolved"):
            _rebuild(l2e_engine, snap_a, artifact_root=artifact_root)
    finally:
        path.write_bytes(backup)


def test_replay_detects_modified_stored_bytes(l2e_engine, snap_a, built, artifact_root) -> None:
    path = Path(built[("a", "train")].artifact_path)
    original = path.read_bytes()
    path.write_bytes(original[:-1] + bytes([original[-1] ^ 0xFF]))
    try:
        with pytest.raises(MatrixArtifactIntegrityError):
            _rebuild(l2e_engine, snap_a, artifact_root=artifact_root)
    finally:
        path.write_bytes(original)


# --------------------------------------------------------------------------- #
# atomicity + append-only + no plaintext + constraint classification
# --------------------------------------------------------------------------- #
def test_atomic_rollback_leaves_no_partial_rows_or_orphan(
    l2e_engine, extra_snaps, artifact_root, monkeypatch
) -> None:
    snap = extra_snaps[4]
    snapshot = load_member_manifest_with_trust(snap.manifest_bytes, snap.trust)
    build = build_partition_matrix(
        snapshot, "train", lambda m: snap.payload_paths[m.dataset_id].read_bytes()
    )
    payload = serialize_matrix(build.matrix, build.vectors)
    sha = hashlib.sha256(payload).hexdigest()
    final_path = artifact_root / "l2e" / "train" / f"{sha}.parquet"

    def _boom(*a, **k):
        raise DBAPIError("stmt", {}, Exception("injected commit-time failure"))

    monkeypatch.setattr(fm, "_read_back_and_verify", _boom)
    with pytest.raises(DBAPIError):
        _persist_feature_matrix(
            l2e_engine,
            snapshot=snapshot,
            matrix=build.matrix,
            vectors=build.vectors,
            partition_root=artifact_root,
        )
    monkeypatch.undo()
    assert _count(l2e_engine, 4, "train") == 0
    assert not final_path.exists()
    with l2e_engine.connect() as conn:
        art = conn.execute(
            text("SELECT count(*) FROM catalog.artifacts WHERE sha256 = :h"), {"h": sha}
        ).scalar_one()
    assert art == 0


# --------------------------------------------------------------------------- #
# item 3 — commit ambiguity: never orphan a committed row from its artifact
# --------------------------------------------------------------------------- #
def test_committed_then_wrapper_raises_keeps_row_and_artifact(
    l2e_engine, extra_snaps, artifact_root, monkeypatch
) -> None:
    """The REAL commit succeeds; a post-commit wrapper step then raises. The committed
    row must still reference an existing, valid artifact (never unlinked)."""
    from minos_engine.layer2.features.matrix_parquet import serialize_matrix as _ser

    snap = extra_snaps[11]
    snapshot = load_member_manifest_with_trust(snap.manifest_bytes, snap.trust)
    build = build_partition_matrix(
        snapshot, "train", lambda m: snap.payload_paths[m.dataset_id].read_bytes()
    )
    sha = hashlib.sha256(_ser(build.matrix, build.vectors)).hexdigest()
    final_path = artifact_root / "l2e" / "train" / f"{sha}.parquet"

    monkeypatch.setattr(
        fm, "_after_commit_hook", lambda: (_ for _ in ()).throw(RuntimeError("boom"))
    )
    with pytest.raises(RuntimeError, match="boom"):
        _persist_feature_matrix(
            l2e_engine,
            snapshot=snapshot,
            matrix=build.matrix,
            vectors=build.vectors,
            partition_root=artifact_root,
        )
    monkeypatch.undo()
    # the commit really happened: the row exists AND references the existing artifact.
    assert _count(l2e_engine, 11, "train") == 1
    assert final_path.exists()
    with l2e_engine.connect() as conn:
        uri = conn.execute(
            text(
                "SELECT art.uri FROM profiling.feature_matrices m "
                "JOIN catalog.artifacts art ON art.id = m.matrix_artifact_id "
                "JOIN profiling.profile_snapshots ps ON ps.id = m.profile_snapshot_id "
                "WHERE ps.epoch = 11 AND m.partition = 'train'"
            )
        ).scalar_one()
    assert Path(uri).exists()
    assert hashlib.sha256(Path(uri).read_bytes()).hexdigest() == sha


def test_ambiguous_commit_retains_immutable_orphan(
    l2e_engine, extra_snaps, artifact_root, monkeypatch
) -> None:
    """commit() raises (ambiguous status): the immutable content-addressed artifact is
    RETAINED (never unlinked) so a possibly-committed row can never reference a missing
    file; the caller sees a typed AmbiguousMatrixCommitError."""
    from minos_engine.layer2.features.errors import AmbiguousMatrixCommitError
    from minos_engine.layer2.features.matrix_parquet import serialize_matrix as _ser

    snap = extra_snaps[12]
    snapshot = load_member_manifest_with_trust(snap.manifest_bytes, snap.trust)
    build = build_partition_matrix(
        snapshot, "train", lambda m: snap.payload_paths[m.dataset_id].read_bytes()
    )
    sha = hashlib.sha256(_ser(build.matrix, build.vectors)).hexdigest()
    final_path = artifact_root / "l2e" / "train" / f"{sha}.parquet"

    # simulate the ambiguity boundary raising (commit reached the wire; ack lost).
    def _ambiguous(trans, *, published, artifact_sha256):
        raise AmbiguousMatrixCommitError("simulated ambiguous commit (ack lost)")

    monkeypatch.setattr(fm, "_commit_or_ambiguous", _ambiguous)
    with pytest.raises(AmbiguousMatrixCommitError):
        _persist_feature_matrix(
            l2e_engine,
            snapshot=snapshot,
            matrix=build.matrix,
            vectors=build.vectors,
            partition_root=artifact_root,
        )
    monkeypatch.undo()
    # the immutable artifact is RETAINED (not unlinked) for later reconciliation.
    assert final_path.exists()
    assert hashlib.sha256(final_path.read_bytes()).hexdigest() == sha


def test_update_and_delete_rejected_on_all_three_tables(l2e_engine, built) -> None:
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


def test_no_plaintext_feature_values_column_exists(l2e_engine, built) -> None:
    with l2e_engine.connect() as conn:
        columns = {
            r[0]
            for r in conn.execute(
                text(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_schema = 'profiling' AND table_name = 'feature_matrix_members'"
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
    }


def test_constraint_name_classification_is_test_only(l2e_engine, built) -> None:
    """A RAW duplicate insert (test-only, NOT a persistence path) is classified by
    constraint name to a typed MatrixConflictError."""
    with l2e_engine.connect() as conn:
        existing = (
            conn.execute(
                text(
                    "SELECT profile_snapshot_id, partition, feature_set_id, matrix_hash, "
                    " artifact_sha256, matrix_artifact_id, row_count, column_count "
                    "FROM profiling.feature_matrices LIMIT 1"
                )
            )
            .mappings()
            .one()
        )
    captured: DBAPIError | None = None
    try:
        with l2e_engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO profiling.feature_matrices (profile_snapshot_id, partition, "
                    " feature_set_id, matrix_hash, artifact_sha256, matrix_artifact_id, "
                    " row_count, column_count) VALUES (:s, :p, :f, :mh, :ah, :aid, :rc, :cc)"
                ),
                {
                    "s": str(existing["profile_snapshot_id"]),
                    "p": existing["partition"],
                    "f": str(existing["feature_set_id"]),
                    "mh": existing["matrix_hash"],  # duplicate matrix_hash → unique violation
                    "ah": existing["artifact_sha256"],
                    "aid": str(existing["matrix_artifact_id"]),
                    "rc": existing["row_count"],
                    "cc": existing["column_count"],
                },
            )
    except DBAPIError as exc:
        captured = exc
    assert captured is not None
    assert isinstance(_classify_unique_violation(captured), MatrixConflictError)


def test_persisted_rows_match_verified_content_field_for_field(l2e_engine, snap_a, built) -> None:
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
    assert int(row.row_count) == 4 and int(row.column_count) == 129
    assert row.feature_set_hash == build_feature_set_manifest().feature_set_hash
    assert row.snapshot_hash == snapshot.snapshot_hash
    assert [m.dataset_id for m in db_members] == [m.dataset_id for m in expected_members]
    assert [int(m.member_index) for m in db_members] == list(range(4))
    for db_member, member in zip(db_members, expected_members, strict=True):
        assert db_member.feature_values_hash == member.feature_values_hash
    assert hashlib.sha256(Path(row.uri).read_bytes()).hexdigest() == result.artifact_sha256
