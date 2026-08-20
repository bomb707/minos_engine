"""E3 concurrency on real PostgreSQL: equal-content races; a forgery can never win."""

from __future__ import annotations

import threading

from sqlalchemy import create_engine, text

from minos_engine.layer2.features.contracts import FeatureMatrix, MatrixMember, vector_hash
from minos_engine.layer2.features.errors import MatrixArtifactIntegrityError
from minos_engine.layer2.features.extraction import (
    build_partition_matrix,
    load_member_manifest_with_trust,
)
from minos_engine.storage.database import normalize_database_url
from minos_engine.storage.feature_matrix import (
    _persist_feature_matrix,
    build_feature_matrix_with_trust,
)
from tests.integration.layer2_features.conftest import make_publisher


def _count(engine, epoch: int) -> int:
    with engine.connect() as conn:
        return int(
            conn.execute(
                text(
                    "SELECT count(*) FROM profiling.feature_matrices fm "
                    "JOIN profiling.profile_snapshots ps ON ps.id = fm.profile_snapshot_id "
                    "WHERE ps.epoch = :e AND fm.partition = 'train'"
                ),
                {"e": epoch},
            ).scalar_one()
        )


def _stored_hash(engine, epoch: int) -> str:
    with engine.connect() as conn:
        return str(
            conn.execute(
                text(
                    "SELECT fm.matrix_hash FROM profiling.feature_matrices fm "
                    "JOIN profiling.profile_snapshots ps ON ps.id = fm.profile_snapshot_id "
                    "WHERE ps.epoch = :e AND fm.partition = 'train'"
                ),
                {"e": epoch},
            ).scalar_one()
        )


def test_two_engines_equal_content_build_one_row_both_succeed(
    l2e_db_url, l2e_engine, extra_snaps, artifact_root
) -> None:
    """Two independent engines build the SAME logical matrix concurrently: both succeed,
    exactly one accepted row, exactly one published inode (one created + one replay)."""
    snap = extra_snaps[5]
    engines = [create_engine(normalize_database_url(l2e_db_url)) for _ in range(2)]
    barrier = threading.Barrier(2)
    results: list = [None, None]
    errors: list = [None, None]

    def worker(slot: int) -> None:
        try:
            barrier.wait(timeout=30)
            results[slot] = build_feature_matrix_with_trust(
                engines[slot], snap.manifest_bytes, snap.trust, "train", artifact_root=artifact_root
            )
        except Exception as exc:  # noqa: BLE001 - collected for assertion
            errors[slot] = exc

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=120)
    for engine in engines:
        engine.dispose()

    assert errors == [None, None], errors
    assert results[0] is not None and results[1] is not None
    assert results[0].matrix_hash == results[1].matrix_hash
    assert results[0].feature_matrix_id == results[1].feature_matrix_id
    assert results[0].artifact_sha256 == results[1].artifact_sha256
    assert _count(l2e_engine, 5) == 1
    assert {results[0].idempotent, results[1].idempotent} == {True, False}
    # one single published inode for the shared content.
    from pathlib import Path

    p = Path(results[0].artifact_path)
    assert p == Path(results[1].artifact_path)
    assert len(list(p.parent.glob(f"{results[0].artifact_sha256}.parquet"))) == 1


def test_forged_candidate_cannot_win_concurrency(
    l2e_db_url, l2e_engine, extra_snaps, artifact_root
) -> None:
    """Honest and forged builds race on a fresh identity: the forgery is rejected by
    re-verification before any DB work, so the honest matrix is the sole accepted row."""
    snap = extra_snaps[6]
    snapshot = load_member_manifest_with_trust(snap.manifest_bytes, snap.trust)
    honest = build_partition_matrix(
        snapshot, "train", lambda m: snap.payload_paths[m.dataset_id].read_bytes()
    )
    values = list(honest.vectors[0].values)
    values[1] = 0.999888  # REAL column; only feature_values_hash goes stale
    forged_vec = honest.vectors[0].model_copy(update={"values": tuple(values)})
    forged_vec = forged_vec.model_copy(update={"vector_hash": vector_hash(forged_vec)})
    forged_vectors = tuple(
        sorted(
            [forged_vec] + [v for v in honest.vectors if v.dataset_id != forged_vec.dataset_id],
            key=lambda v: v.dataset_id,
        )
    )
    forged_matrix = FeatureMatrix(
        epoch=snapshot.epoch,
        snapshot_hash=snapshot.snapshot_hash,
        partition="train",
        registry_hash=honest.matrix.registry_hash,
        feature_set_hash=honest.matrix.feature_set_hash,
        row_count=len(forged_vectors),
        column_count=129,
        members=tuple(
            MatrixMember(dataset_id=v.dataset_id, vector_hash=v.vector_hash) for v in forged_vectors
        ),
    )
    assert forged_matrix.matrix_hash != honest.matrix.matrix_hash

    engines = [create_engine(normalize_database_url(l2e_db_url)) for _ in range(2)]
    barrier = threading.Barrier(2)
    outcomes: list = [None, None]

    def worker(slot: int) -> None:
        try:
            barrier.wait(timeout=30)
            if slot == 0:
                outcomes[0] = _persist_feature_matrix(
                    engines[0],
                    snapshot=snapshot,
                    matrix=honest.matrix,
                    vectors=honest.vectors,
                    publisher=make_publisher(artifact_root),
                    require_operational_identity=False,
                )
            else:
                outcomes[1] = _persist_feature_matrix(
                    engines[1],
                    snapshot=snapshot,
                    matrix=forged_matrix,
                    vectors=forged_vectors,
                    publisher=make_publisher(artifact_root),
                    require_operational_identity=False,
                )
        except Exception as exc:  # noqa: BLE001 - collected for assertion
            outcomes[slot] = exc

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=120)
    for engine in engines:
        engine.dispose()

    assert not isinstance(outcomes[0], Exception), outcomes[0]
    assert isinstance(outcomes[1], MatrixArtifactIntegrityError)
    assert _count(l2e_engine, 6) == 1
    assert _stored_hash(l2e_engine, 6) == honest.matrix.matrix_hash  # forgery never wins
