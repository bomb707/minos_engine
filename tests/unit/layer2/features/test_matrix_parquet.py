"""E3 canonical Parquet: byte determinism, exact format, atomic writes, tamper cases."""

from __future__ import annotations

import hashlib

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from minos_engine.layer2.features.contracts import (
    FeatureMatrix,
    FeatureVector,
    MatrixMember,
    build_feature_set_manifest,
    canonical_feature_set,
)
from minos_engine.layer2.features.errors import (
    MatrixArtifactIntegrityError,
    MissingFeatureError,
)
from minos_engine.layer2.features.matrix_parquet import (
    PARQUET_SCHEMA_VERSION,
    QUALIFIED_PYARROW_VERSION,
    canonical_matrix_schema,
    publish_matrix_artifact,
    serialize_matrix,
    verify_matrix_artifact,
)

_MANIFEST = build_feature_set_manifest()


def _vector(i: int, *, partition: str = "train") -> FeatureVector:
    return FeatureVector(
        epoch=1,
        dataset_id=f"ds-{i:02d}",
        profile_id=f"p-{i:02d}",
        content_hash="b" * 64,
        feature_values_hash="c" * 64,
        partition=partition,  # type: ignore[arg-type]
        snapshot_hash="d" * 64,
        registry_hash=_MANIFEST.registry_hash,
        feature_set_hash=_MANIFEST.feature_set_hash,
        value_count=129,
        values=tuple(0.001 * j + 0.0001 * i for j in range(129)),
    )


def _matrix(vectors: list[FeatureVector]) -> FeatureMatrix:
    return FeatureMatrix(
        epoch=1,
        snapshot_hash="d" * 64,
        partition="train",
        registry_hash=_MANIFEST.registry_hash,
        feature_set_hash=_MANIFEST.feature_set_hash,
        row_count=len(vectors),
        column_count=129,
        members=tuple(
            MatrixMember(dataset_id=v.dataset_id, vector_hash=v.vector_hash) for v in vectors
        ),
    )


@pytest.fixture(scope="module")
def vectors() -> list[FeatureVector]:
    return [_vector(i) for i in range(4)]


@pytest.fixture(scope="module")
def mtx(vectors) -> FeatureMatrix:
    return _matrix(vectors)


def test_writer_is_the_qualified_version() -> None:
    # pyproject pins the same version; determinism is never claimed on any other.
    assert pa.__version__ == QUALIFIED_PYARROW_VERSION


def test_serialization_is_byte_deterministic(mtx, vectors) -> None:
    first = serialize_matrix(mtx, vectors)
    second = serialize_matrix(mtx, vectors)
    assert first == second
    assert hashlib.sha256(first).hexdigest() == hashlib.sha256(second).hexdigest()


def test_exact_schema_metadata_order_and_values(mtx, vectors) -> None:
    payload = serialize_matrix(mtx, vectors)
    table = pq.read_table(pa.BufferReader(payload))
    columns = canonical_feature_set().columns
    assert table.schema.names == ["dataset_id"] + [c.path for c in columns]
    assert table.schema.field(0).type == pa.string()
    assert all(table.schema.field(i).type == pa.float64() for i in range(1, 130))
    metadata = {
        k.decode(): v.decode() for k, v in table.schema.metadata.items() if k != b"ARROW:schema"
    }
    # identity-bound application metadata: the frozen keys + this matrix's logical id.
    assert metadata == {
        "schema_version": PARQUET_SCHEMA_VERSION,
        "feature_set_hash": _MANIFEST.feature_set_hash,
        "matrix_hash": mtx.matrix_hash,
        "snapshot_hash": mtx.snapshot_hash,
        "partition": mtx.partition,
        "epoch": str(mtx.epoch),
    }
    assert table.column("dataset_id").to_pylist() == [v.dataset_id for v in vectors]
    assert all(table.column(i).null_count == 0 for i in range(table.num_columns))
    for j, column in enumerate(columns):
        assert table.column(column.path).to_pylist() == [v.values[j] for v in vectors]
    # container settings pinned: no compression, no statistics, no dictionaries.
    parquet_file = pq.ParquetFile(pa.BufferReader(payload))
    for rg in range(parquet_file.metadata.num_row_groups):
        for col in range(parquet_file.metadata.num_columns):
            column_meta = parquet_file.metadata.row_group(rg).column(col)
            assert column_meta.compression == "UNCOMPRESSED"
            assert not column_meta.is_stats_set
            assert "PLAIN_DICTIONARY" not in str(column_meta.encodings)
            assert "RLE_DICTIONARY" not in str(column_meta.encodings)


def test_arrow_schema_echo_matches_canonical(mtx, vectors) -> None:
    payload = serialize_matrix(mtx, vectors)
    table = pq.read_table(pa.BufferReader(payload))
    assert table.schema.remove_metadata() == canonical_matrix_schema(mtx).remove_metadata()
    assert all(not table.schema.field(i).nullable for i in range(table.num_columns))


def test_unordered_and_duplicate_rows_rejected(mtx, vectors) -> None:
    with pytest.raises(MatrixArtifactIntegrityError, match="members/order"):
        serialize_matrix(mtx, list(reversed(vectors)))
    with pytest.raises(MatrixArtifactIntegrityError, match="members/order"):
        serialize_matrix(mtx, [vectors[0], vectors[0]])


def test_invalid_values_rejected_before_write(mtx, vectors) -> None:
    # model_copy bypasses contract validation; the serializer must re-check.
    fraction_index = next(
        i for i, c in enumerate(canonical_feature_set().columns) if c.value_kind == "FRACTION"
    )
    bad_values = list(vectors[0].values)
    bad_values[fraction_index] = 1.5
    forged = vectors[0].model_copy(update={"values": tuple(bad_values)})
    with pytest.raises(MissingFeatureError, match="FRACTION"):
        serialize_matrix(mtx, [forged] + vectors[1:])
    short = vectors[0].model_copy(update={"values": vectors[0].values[:128]})
    with pytest.raises(MissingFeatureError, match="129"):
        serialize_matrix(mtx, [short] + vectors[1:])


def test_verify_passes_on_good_payload(vectors) -> None:
    payload = serialize_matrix(_matrix(vectors), vectors)
    checks = verify_matrix_artifact(
        payload, _matrix(vectors), vectors, hashlib.sha256(payload).hexdigest()
    )
    assert checks and all(checks.values()), [k for k, v in checks.items() if not v]


@pytest.mark.parametrize(
    "case",
    ["byte_change", "wrong_hash", "value_change", "row_reorder", "column_reorder", "null_value"],
)
def test_verify_detects_tampering(vectors, case) -> None:
    payload = serialize_matrix(_matrix(vectors), vectors)
    sha = hashlib.sha256(payload).hexdigest()
    matrix = _matrix(vectors)
    if case == "byte_change":
        tampered = payload[:-1] + bytes([payload[-1] ^ 0xFF])
        checks = verify_matrix_artifact(tampered, matrix, vectors, sha)
        assert not all(checks.values())
        assert not checks["artifact_sha256_matches"]
    elif case == "wrong_hash":
        checks = verify_matrix_artifact(payload, matrix, vectors, "0" * 64)
        assert not checks["artifact_sha256_matches"]
    elif case == "value_change":
        forged_values = list(vectors[0].values)
        forged_values[3] = 0.987654
        forged = [vectors[0].model_copy(update={"values": tuple(forged_values)})] + vectors[1:]
        checks = verify_matrix_artifact(payload, matrix, forged, sha)
        assert not checks["values_match_vectors"]
    elif case == "row_reorder":
        table = pq.read_table(pa.BufferReader(payload))
        reordered = table.take(list(reversed(range(len(table)))))
        sink = pa.BufferOutputStream()
        pq.write_table(reordered, sink, compression="NONE")
        tampered = bytes(sink.getvalue().to_pybytes())
        checks = verify_matrix_artifact(
            tampered, matrix, vectors, hashlib.sha256(tampered).hexdigest()
        )
        assert not checks["row_order_by_dataset_id"]
        assert not checks["reserialization_byte_identical"]
    elif case == "column_reorder":
        table = pq.read_table(pa.BufferReader(payload))
        names = table.schema.names
        swapped = table.select([names[0]] + [names[2], names[1]] + names[3:])
        sink = pa.BufferOutputStream()
        pq.write_table(swapped, sink, compression="NONE")
        tampered = bytes(sink.getvalue().to_pybytes())
        checks = verify_matrix_artifact(
            tampered, matrix, vectors, hashlib.sha256(tampered).hexdigest()
        )
        assert not checks["column_names_and_order_exact"]
    elif case == "null_value":
        table = pq.read_table(pa.BufferReader(payload))
        name = table.schema.names[1]
        nulled = table.set_column(
            1,
            pa.field(name, pa.float64(), nullable=True),
            pa.array([None] * len(table), type=pa.float64()),
        )
        sink = pa.BufferOutputStream()
        pq.write_table(nulled, sink, compression="NONE")
        tampered = bytes(sink.getvalue().to_pybytes())
        checks = verify_matrix_artifact(
            tampered, matrix, vectors, hashlib.sha256(tampered).hexdigest()
        )
        assert not checks["no_nulls"]


def test_verify_detects_wrong_metadata(vectors) -> None:
    payload = serialize_matrix(_matrix(vectors), vectors)
    table = pq.read_table(pa.BufferReader(payload))
    tampered_schema = table.schema.with_metadata(
        {b"schema_version": b"feature-matrix-parquet-v999", b"feature_set_hash": b"0" * 64}
    )
    sink = pa.BufferOutputStream()
    pq.write_table(table.cast(tampered_schema), sink, compression="NONE")
    tampered = bytes(sink.getvalue().to_pybytes())
    checks = verify_matrix_artifact(
        tampered, _matrix(vectors), vectors, hashlib.sha256(tampered).hexdigest()
    )
    assert not checks["schema_metadata_exact"]


def test_publish_is_immutable_no_clobber_and_round_trips(tmp_path, vectors) -> None:
    import os

    payload = serialize_matrix(_matrix(vectors), vectors)
    root = tmp_path / "l2e" / "train"
    root.mkdir(parents=True)
    gid = os.getgid()
    first = publish_matrix_artifact(payload, partition_root=root, gid=gid)
    assert first.path.name == f"{first.artifact_sha256}.parquet"
    assert first.path.read_bytes() == payload
    assert first.size_bytes == len(payload)
    assert first.created is True
    # the inode carries the partition gid + mode 0640.
    st = first.path.stat()
    assert st.st_gid == gid and (st.st_mode & 0o777) == 0o640
    inode = st.st_ino
    # equal-content republish reuses the SAME inode and does not create a new one.
    second = publish_matrix_artifact(payload, partition_root=root, gid=gid)
    assert second.path == first.path and second.created is False
    assert second.path.stat().st_ino == inode
    # no temporary or partial files remain.
    leftovers = [p for p in first.path.parent.iterdir() if p.name.startswith(".tmp-")]
    assert leftovers == []


def test_publish_unlinks_created_inode_if_fsync_fails_after_link(tmp_path, vectors, monkeypatch):
    """A failure AFTER os.link (here: fsync_directory) must unlink the just-created inode
    and leave no final or temporary artifact."""
    import os

    import minos_engine.layer2.features.matrix_parquet as mp

    payload = serialize_matrix(_matrix(vectors), vectors)
    root = tmp_path / "l2e" / "train"
    root.mkdir(parents=True)
    sha = hashlib.sha256(payload).hexdigest()

    calls = {"n": 0}

    def _flaky_fsync_dir(directory) -> None:
        calls["n"] += 1
        raise OSError("injected directory fsync failure after link")

    monkeypatch.setattr(mp, "fsync_directory", _flaky_fsync_dir)
    with pytest.raises(OSError, match="injected"):
        publish_matrix_artifact(payload, partition_root=root, gid=os.getgid())
    monkeypatch.undo()
    # neither the final content-addressed file nor any temp file remains.
    assert not (root / f"{sha}.parquet").exists()
    assert list(root.glob(".tmp-*")) == []


def test_publish_rejects_corrupt_preexisting_target_unchanged(tmp_path, vectors) -> None:
    import os

    payload = serialize_matrix(_matrix(vectors), vectors)
    root = tmp_path / "l2e" / "train"
    root.mkdir(parents=True)
    published = publish_matrix_artifact(payload, partition_root=root, gid=os.getgid())
    # corrupt the final content-addressed file in place.
    corrupt = payload[:-1] + bytes([payload[-1] ^ 0xFF])
    published.path.write_bytes(corrupt)
    with pytest.raises(MatrixArtifactIntegrityError, match="does not match"):
        publish_matrix_artifact(payload, partition_root=root, gid=os.getgid())
    # the corrupt file is left UNCHANGED (never silently repaired).
    assert published.path.read_bytes() == corrupt


def test_matrix_hash_and_artifact_sha_stay_separate(vectors) -> None:
    payload = serialize_matrix(_matrix(vectors), vectors)
    matrix = _matrix(vectors)
    artifact_sha = hashlib.sha256(payload).hexdigest()
    assert matrix.matrix_hash != artifact_sha
    assert "artifact_sha256" not in matrix.model_dump()
