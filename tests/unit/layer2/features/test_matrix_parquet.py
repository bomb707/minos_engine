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
    serialize_matrix,
    verify_matrix_artifact,
    write_matrix_artifact,
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


def test_writer_is_the_qualified_version() -> None:
    # pyproject pins the same version; determinism is never claimed on any other.
    assert pa.__version__ == QUALIFIED_PYARROW_VERSION


def test_serialization_is_byte_deterministic(vectors) -> None:
    first = serialize_matrix(vectors)
    second = serialize_matrix(vectors)
    assert first == second
    assert hashlib.sha256(first).hexdigest() == hashlib.sha256(second).hexdigest()


def test_exact_schema_metadata_order_and_values(vectors) -> None:
    payload = serialize_matrix(vectors)
    table = pq.read_table(pa.BufferReader(payload))
    columns = canonical_feature_set().columns
    assert table.schema.names == ["dataset_id"] + [c.path for c in columns]
    assert table.schema.field(0).type == pa.string()
    assert all(table.schema.field(i).type == pa.float64() for i in range(1, 130))
    metadata = {
        k.decode(): v.decode() for k, v in table.schema.metadata.items() if k != b"ARROW:schema"
    }
    assert metadata == {
        "schema_version": PARQUET_SCHEMA_VERSION,
        "feature_set_hash": _MANIFEST.feature_set_hash,
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


def test_arrow_schema_echo_matches_canonical(vectors) -> None:
    payload = serialize_matrix(vectors)
    table = pq.read_table(pa.BufferReader(payload))
    assert table.schema.remove_metadata() == canonical_matrix_schema().remove_metadata()
    assert all(not table.schema.field(i).nullable for i in range(table.num_columns))


def test_unordered_and_duplicate_rows_rejected(vectors) -> None:
    with pytest.raises(MatrixArtifactIntegrityError, match="ordered"):
        serialize_matrix(list(reversed(vectors)))
    with pytest.raises(MatrixArtifactIntegrityError, match="duplicate"):
        serialize_matrix([vectors[0], vectors[0]])


def test_invalid_values_rejected_before_write(vectors) -> None:
    # model_copy bypasses contract validation; the serializer must re-check.
    fraction_index = next(
        i for i, c in enumerate(canonical_feature_set().columns) if c.value_kind == "FRACTION"
    )
    bad_values = list(vectors[0].values)
    bad_values[fraction_index] = 1.5
    forged = vectors[0].model_copy(update={"values": tuple(bad_values)})
    with pytest.raises(MissingFeatureError, match="FRACTION"):
        serialize_matrix([forged] + vectors[1:])
    short = vectors[0].model_copy(update={"values": vectors[0].values[:128]})
    with pytest.raises(MissingFeatureError, match="129"):
        serialize_matrix([short] + vectors[1:])


def test_verify_passes_on_good_payload(vectors) -> None:
    payload = serialize_matrix(vectors)
    checks = verify_matrix_artifact(
        payload, _matrix(vectors), vectors, hashlib.sha256(payload).hexdigest()
    )
    assert checks and all(checks.values()), [k for k, v in checks.items() if not v]


@pytest.mark.parametrize(
    "case",
    ["byte_change", "wrong_hash", "value_change", "row_reorder", "column_reorder", "null_value"],
)
def test_verify_detects_tampering(vectors, case) -> None:
    payload = serialize_matrix(vectors)
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
    payload = serialize_matrix(vectors)
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


def test_atomic_write_and_round_trip(tmp_path, vectors) -> None:
    payload = serialize_matrix(vectors)
    sha, path = write_matrix_artifact(payload, partition_root=tmp_path / "l2e" / "train")
    assert path.name == f"{sha}.parquet"
    assert path.read_bytes() == payload
    assert hashlib.sha256(path.read_bytes()).hexdigest() == sha
    # idempotent re-write of identical content lands on the same path.
    sha2, path2 = write_matrix_artifact(payload, partition_root=tmp_path / "l2e" / "train")
    assert (sha2, path2) == (sha, path)
    # no temporary or partial files remain.
    leftovers = [p for p in path.parent.iterdir() if p.name.startswith(".tmp-")]
    assert leftovers == []


def test_matrix_hash_and_artifact_sha_stay_separate(vectors) -> None:
    payload = serialize_matrix(vectors)
    matrix = _matrix(vectors)
    artifact_sha = hashlib.sha256(payload).hexdigest()
    assert matrix.matrix_hash != artifact_sha
    assert "artifact_sha256" not in matrix.model_dump()
