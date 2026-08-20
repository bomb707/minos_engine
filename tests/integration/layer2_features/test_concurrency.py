"""E3 concurrency on real PostgreSQL: equal-content races and conflicting identities."""

from __future__ import annotations

import threading

from sqlalchemy import create_engine, text

from minos_engine.common.errors import MatrixConflictError
from minos_engine.layer2.features.contracts import FeatureMatrix, MatrixMember, vector_hash
from minos_engine.layer2.features.extraction import (
    build_partition_matrix,
    load_member_manifest_with_trust,
)
from minos_engine.layer2.features.matrix_parquet import serialize_matrix, write_matrix_artifact
from minos_engine.storage.database import normalize_database_url
from minos_engine.storage.feature_matrix import (
    build_feature_matrix_with_trust,
    persist_feature_matrix,
)


def _count_matrices(engine, epoch: int) -> int:
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


def test_two_engines_concurrent_equal_content_build(
    l2e_db_url, l2e_engine, extra_snaps, artifact_root
) -> None:
    """Two independent engines build the SAME logical matrix concurrently: both
    succeed, exactly one row exists, identical identities on both sides."""
    snap = extra_snaps[5]
    engines = [
        create_engine(normalize_database_url(l2e_db_url)),
        create_engine(normalize_database_url(l2e_db_url)),
    ]
    barrier = threading.Barrier(2)
    results: list = [None, None]
    errors: list = [None, None]

    def worker(slot: int) -> None:
        try:
            barrier.wait(timeout=30)
            results[slot] = build_feature_matrix_with_trust(
                engines[slot],
                snap.manifest_bytes,
                snap.trust,
                "train",
                artifact_root=artifact_root,
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
    # both succeeded, but exactly ONE accepted row exists.
    assert _count_matrices(l2e_engine, 5) == 1
    assert results[0].idempotent != results[1].idempotent or any(r.idempotent for r in results)


def test_concurrent_conflicting_identity_one_row_one_typed_error(
    l2e_db_url, l2e_engine, extra_snaps, artifact_root
) -> None:
    """Two concurrent persists with the SAME logical identity but DIFFERENT content:
    exactly one accepted row remains and the loser gets a typed MatrixConflictError."""
    snap = extra_snaps[6]
    snapshot = load_member_manifest_with_trust(snap.manifest_bytes, snap.trust)
    honest = build_partition_matrix(
        snapshot, "train", lambda m: snap.payload_paths[m.dataset_id].read_bytes()
    )
    forged_values = list(honest.vectors[0].values)
    forged_values[1] = 0.222222
    forged_vector = honest.vectors[0].model_copy(update={"values": tuple(forged_values)})
    forged_vector = forged_vector.model_copy(update={"vector_hash": vector_hash(forged_vector)})
    forged_vectors = sorted(
        [forged_vector] + [v for v in honest.vectors if v.dataset_id != forged_vector.dataset_id],
        key=lambda v: v.dataset_id,
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

    honest_payload = serialize_matrix(honest.vectors)
    honest_sha, honest_path = write_matrix_artifact(
        honest_payload, partition_root=artifact_root / "l2e" / "train"
    )
    forged_payload = serialize_matrix(forged_vectors)
    forged_sha, forged_path = write_matrix_artifact(
        forged_payload, partition_root=artifact_root / "l2e" / "train"
    )

    engines = [
        create_engine(normalize_database_url(l2e_db_url)),
        create_engine(normalize_database_url(l2e_db_url)),
    ]
    jobs = (
        (honest.matrix, honest_sha, honest_path, len(honest_payload)),
        (forged_matrix, forged_sha, forged_path, len(forged_payload)),
    )
    barrier = threading.Barrier(2)
    outcomes: list = [None, None]

    def worker(slot: int) -> None:
        matrix, sha, path, size = jobs[slot]
        try:
            barrier.wait(timeout=30)
            outcomes[slot] = persist_feature_matrix(
                engines[slot],
                snapshot=snapshot,
                matrix=matrix,
                artifact_sha256=sha,
                artifact_path=path,
                artifact_size=size,
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

    conflicts = [o for o in outcomes if isinstance(o, MatrixConflictError)]
    successes = [o for o in outcomes if not isinstance(o, Exception) and o is not None]
    assert len(conflicts) == 1 and len(successes) == 1, outcomes
    assert _count_matrices(l2e_engine, 6) == 1
    with l2e_engine.connect() as conn:
        stored_hash = conn.execute(
            text(
                "SELECT fm.matrix_hash FROM profiling.feature_matrices fm "
                "JOIN profiling.profile_snapshots ps ON ps.id = fm.profile_snapshot_id "
                "WHERE ps.epoch = 6 AND fm.partition = 'train'"
            )
        ).scalar_one()
    assert stored_hash == successes[0].matrix_hash
