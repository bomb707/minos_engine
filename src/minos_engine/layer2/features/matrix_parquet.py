"""Canonical feature-matrix Parquet serialization — frozen in FEATURE_VIEW.md.

Format (frozen): ``dataset_id`` UTF-8 string first, then the 129 non-nullable float64
feature columns named by field path in canonical column-manifest index order; rows
ordered by ``dataset_id`` ascending; nulls forbidden; ``compression=NONE``;
dictionaries disabled; statistics disabled; Parquet format version and data-page
version pinned; the ONLY application schema metadata keys are ``schema_version`` and
``feature_set_hash`` — no timestamps or runtime-generated application metadata. The
pinned writer additionally stores its deterministic ``ARROW:schema`` echo of exactly
that schema; verification requires the metadata to be exactly these and nothing else.

Byte determinism is claimed ONLY on the exact qualified writer version: pyproject pins
``pyarrow==QUALIFIED_PYARROW_VERSION`` and every serialize/verify call re-checks the
installed writer identity and fails closed on any other version.

Writes are atomic: bytes go to a temporary file in the destination directory, are
fsynced, re-read and hash-verified, then atomically renamed into the content-addressed
final location. A failure never leaves a partial final artifact.

``matrix_hash`` (logical identity) and ``artifact_sha256`` (exact Parquet bytes) are
never conflated: this module produces/verifies ``artifact_sha256``; the logical hash
lives in :mod:`minos_engine.layer2.features.contracts`.
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from .contracts import (
    EXPECTED_COLUMN_COUNT,
    FROZEN_FEATURE_SET_HASH,
    FeatureMatrix,
    FeatureVector,
    canonical_feature_set,
    matrix_hash,
    validate_value_for_kind,
    vector_hash,
)
from .errors import (
    MatrixArtifactIntegrityError,
    MissingFeatureError,
    UnqualifiedWriterError,
)

__all__ = [
    "PARQUET_SCHEMA_VERSION",
    "MATRIX_ARTIFACT_KIND",
    "MATRIX_ARTIFACT_MEDIA_TYPE",
    "QUALIFIED_PYARROW_VERSION",
    "PARQUET_FORMAT_VERSION",
    "PARQUET_DATA_PAGE_VERSION",
    "PublishedArtifact",
    "canonical_matrix_schema",
    "serialize_matrix",
    "publish_matrix_artifact",
    "fsync_directory",
    "verify_matrix_artifact",
]

PARQUET_SCHEMA_VERSION = "feature-matrix-parquet-v1"
MATRIX_ARTIFACT_KIND = "l2e:feature-matrix-parquet"
MATRIX_ARTIFACT_MEDIA_TYPE = "application/vnd.apache.parquet"

#: The exact qualified writer identity for the frozen byte format. pyproject pins this
#: same version; any other installed pyarrow fails closed here — cross-installation
#: byte determinism is never claimed across writer versions.
QUALIFIED_PYARROW_VERSION = "17.0.0"

#: Pinned Parquet container settings (frozen with the format).
PARQUET_FORMAT_VERSION = "2.6"
PARQUET_DATA_PAGE_VERSION = "1.0"

#: The application metadata keys, in canonical order. Beyond schema_version +
#: feature_set_hash, the artifact self-identifies its LOGICAL matrix (matrix_hash,
#: snapshot_hash, partition, epoch). This binds artifact bytes to exactly one logical
#: matrix, so two DISTINCT matrices never share ``artifact_sha256`` — in particular two
#: zero-row matrices in different partitions get distinct content-addressed artifacts
#: under their own partition roots. No timestamp or runtime-generated value is included;
#: every key is a frozen logical identity. ``matrix_hash`` (logical) and
#: ``artifact_sha256`` (exact bytes) remain strictly separate VALUES.
_APP_METADATA_KEYS = (
    "schema_version",
    "feature_set_hash",
    "matrix_hash",
    "snapshot_hash",
    "partition",
    "epoch",
)


def _metadata_for(matrix: FeatureMatrix) -> dict[bytes, bytes]:
    return {
        b"schema_version": PARQUET_SCHEMA_VERSION.encode(),
        b"feature_set_hash": FROZEN_FEATURE_SET_HASH.encode(),
        b"matrix_hash": matrix.matrix_hash.encode(),
        b"snapshot_hash": matrix.snapshot_hash.encode(),
        b"partition": matrix.partition.encode(),
        b"epoch": str(matrix.epoch).encode(),
    }


def _require_qualified_writer() -> None:
    if pa.__version__ != QUALIFIED_PYARROW_VERSION:
        raise UnqualifiedWriterError(
            f"installed pyarrow {pa.__version__} is not the qualified writer "
            f"{QUALIFIED_PYARROW_VERSION}; canonical byte determinism is not claimed "
            "on any other version"
        )


def canonical_matrix_schema(matrix: FeatureMatrix) -> pa.Schema:
    """The frozen Arrow schema: dataset_id + 129 non-nullable float64 columns, carrying
    the identity-bound application metadata for ``matrix``."""
    fields = [pa.field("dataset_id", pa.string(), nullable=False)]
    fields += [
        pa.field(column.path, pa.float64(), nullable=False)
        for column in canonical_feature_set().columns
    ]
    return pa.schema(fields, metadata=_metadata_for(matrix))


def serialize_matrix(matrix: FeatureMatrix, vectors: Sequence[FeatureVector]) -> bytes:
    """Serialize ``matrix``'s ordered vectors to the identity-bound canonical Parquet
    bytes.

    Fail-closed: requires the qualified writer; the vectors must exactly cover the
    matrix members (same dataset_ids, strictly ordered by ``dataset_id``, no
    duplicates), exactly 129 values each, per-kind valid (FRACTION in [0,1]); the
    artifact metadata binds the matrix logical identity.
    """
    _require_qualified_writer()
    ids = [v.dataset_id for v in vectors]
    if ids != [m.dataset_id for m in matrix.members]:
        raise MatrixArtifactIntegrityError("vectors do not match the matrix members/order")
    if len(set(ids)) != len(ids):
        raise MatrixArtifactIntegrityError("duplicate dataset_id rows are rejected")
    if ids != sorted(ids):
        raise MatrixArtifactIntegrityError("rows must be strictly ordered by dataset_id")
    columns = canonical_feature_set().columns
    for vector in vectors:
        if len(vector.values) != EXPECTED_COLUMN_COUNT:
            raise MissingFeatureError(
                f"{vector.dataset_id}: vector does not carry exactly {EXPECTED_COLUMN_COUNT} values"
            )
        for column, value in zip(columns, vector.values, strict=True):
            try:
                validate_value_for_kind(column.value_kind, value, column.path)
            except ValueError as exc:
                raise MissingFeatureError(f"{vector.dataset_id}: {exc}") from exc
    schema = canonical_matrix_schema(matrix)
    arrays: list[pa.Array] = [pa.array(ids, type=pa.string())]
    for index in range(EXPECTED_COLUMN_COUNT):
        arrays.append(pa.array([v.values[index] for v in vectors], type=pa.float64()))
    for array in arrays:
        if array.null_count != 0:  # pragma: no cover - upstream contracts forbid nulls
            raise MatrixArtifactIntegrityError("null values are forbidden in the payload")
    table = pa.Table.from_arrays(arrays, schema=schema)
    sink = pa.BufferOutputStream()
    pq.write_table(
        table,
        sink,
        compression="NONE",
        use_dictionary=False,
        write_statistics=False,
        version=PARQUET_FORMAT_VERSION,
        data_page_version=PARQUET_DATA_PAGE_VERSION,
        store_schema=True,
        write_page_index=False,
    )
    return bytes(sink.getvalue().to_pybytes())


@dataclass(frozen=True)
class PublishedArtifact:
    """Result of a content-addressed publication."""

    artifact_sha256: str
    path: Path
    size_bytes: int
    #: True iff THIS call created the final inode (False = an identical file already
    #: existed and was verified + reused). Rollback cleanup may unlink only when True.
    created: bool


def fsync_directory(directory: Path) -> None:
    """fsync a directory so a rename/link into it is durable."""
    fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def publish_matrix_artifact(payload: bytes, *, partition_root: Path) -> PublishedArtifact:
    """Publish the payload to its content-addressed location with IMMUTABLE, no-clobber
    semantics (never ``os.replace`` over an existing target).

    * If ``<partition_root>/<sha256>.parquet`` already exists it is verified byte-for-byte
      and reused (``created=False``); a mismatch raises :class:`MatrixArtifactIntegrityError`
      and leaves the existing file UNCHANGED.
    * Otherwise the bytes are written to a unique temp file, fsynced, then hard-linked
      into place atomically (``os.link`` fails closed if the target already exists — a
      concurrent equal-content publisher does not replace the winner's inode). The temp
      link is removed and the directory fsynced. ``created=True``.

    A partial final artifact can never appear: the final name only ever names a fully
    written, fsynced inode.
    """
    artifact_sha256 = hashlib.sha256(payload).hexdigest()
    partition_root.mkdir(parents=True, exist_ok=True)
    final_path = partition_root / f"{artifact_sha256}.parquet"

    if final_path.exists():
        _verify_existing_bytes(final_path, artifact_sha256)
        return PublishedArtifact(artifact_sha256, final_path, len(payload), created=False)

    tmp_fd, tmp_name = tempfile.mkstemp(prefix=f".tmp-{artifact_sha256}-", dir=partition_root)
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(tmp_fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        if hashlib.sha256(tmp_path.read_bytes()).hexdigest() != artifact_sha256:
            raise MatrixArtifactIntegrityError(  # pragma: no cover - local write is stable
                "written artifact bytes do not hash to the expected artifact_sha256"
            )
        try:
            os.link(tmp_path, final_path)  # atomic, no-clobber
            created = True
        except FileExistsError:
            # a concurrent publisher won; verify + reuse its inode, never replace it.
            _verify_existing_bytes(final_path, artifact_sha256)
            created = False
    finally:
        with contextlib.suppress(FileNotFoundError):
            tmp_path.unlink()
    fsync_directory(partition_root)
    return PublishedArtifact(artifact_sha256, final_path, len(payload), created=created)


def _verify_existing_bytes(path: Path, expected_sha256: str) -> None:
    if not path.is_file():
        raise MatrixArtifactIntegrityError(f"artifact path is not a regular file: {path}")
    if hashlib.sha256(path.read_bytes()).hexdigest() != expected_sha256:
        raise MatrixArtifactIntegrityError(
            f"pre-existing artifact at {path} does not match the expected content hash "
            "(left unchanged)"
        )


def verify_matrix_artifact(
    payload: bytes,
    matrix: FeatureMatrix,
    vectors: Sequence[FeatureVector],
    expected_artifact_sha256: str,
) -> dict[str, bool]:
    """Named, recomputed payload-level checks for one matrix artifact (never repairs).

    Proves: exact artifact bytes, byte determinism vs re-serialization, exact schema
    and metadata, column order, row order, zero nulls, exact values vs the vectors,
    recomputed vector hashes, and the recomputed LOGICAL matrix_hash — with
    ``artifact_sha256`` and ``matrix_hash`` kept strictly separate.
    """
    _require_qualified_writer()
    checks: dict[str, bool] = {}
    checks["artifact_sha256_matches"] = (
        hashlib.sha256(payload).hexdigest() == expected_artifact_sha256
    )
    ordered = sorted(vectors, key=lambda v: v.dataset_id)
    try:
        reserialized = serialize_matrix(matrix, ordered)
        checks["reserialization_byte_identical"] = reserialized == payload
    except Exception:  # noqa: BLE001 - verification never raises, it reports
        checks["reserialization_byte_identical"] = False

    columns = canonical_feature_set().columns
    expected_names = ["dataset_id"] + [c.path for c in columns]
    try:
        table = pq.read_table(pa.BufferReader(payload))
    except Exception:  # noqa: BLE001 - unreadable payloads fail every content check
        for name in (
            "schema_metadata_exact",
            "column_names_and_order_exact",
            "column_types_exact",
            "row_order_by_dataset_id",
            "no_nulls",
            "row_and_column_counts_match",
            "values_match_vectors",
        ):
            checks[name] = False
        checks["matrix_hash_recomputed_match"] = matrix_hash(matrix) == matrix.matrix_hash
        return checks

    raw_metadata = dict(table.schema.metadata or {})
    file_keys = {
        k.decode() for k in (pq.ParquetFile(pa.BufferReader(payload)).metadata.metadata or {})
    }
    metadata = {k.decode(): v.decode() for k, v in raw_metadata.items() if k != b"ARROW:schema"}
    # exactly the identity-bound application keys, plus ONLY the writer's ARROW:schema echo.
    expected_metadata = {k.decode(): v.decode() for k, v in _metadata_for(matrix).items()}
    checks["schema_metadata_exact"] = metadata == expected_metadata and file_keys == (
        set(_APP_METADATA_KEYS) | {"ARROW:schema"}
    )
    checks["column_names_and_order_exact"] = table.schema.names == expected_names
    checks["column_types_exact"] = table.schema.names == expected_names and (
        table.schema.field(0).type == pa.string()
        and all(table.schema.field(i).type == pa.float64() for i in range(1, len(expected_names)))
    )
    row_ids = table.column("dataset_id").to_pylist() if "dataset_id" in table.schema.names else []
    checks["row_order_by_dataset_id"] = bool(row_ids) == bool(len(table)) and row_ids == sorted(
        row_ids
    )
    checks["no_nulls"] = all(table.column(i).null_count == 0 for i in range(table.num_columns))
    checks["row_and_column_counts_match"] = (
        len(table) == matrix.row_count == len(ordered)
        and table.num_columns == matrix.column_count + 1 == EXPECTED_COLUMN_COUNT + 1
    )
    values_ok = row_ids == [v.dataset_id for v in ordered]
    if values_ok and checks["column_names_and_order_exact"]:
        for column_index, column in enumerate(columns):
            stored = table.column(column.path).to_pylist()
            expected_values = [v.values[column_index] for v in ordered]
            if stored != expected_values:
                values_ok = False
                break
    else:
        values_ok = False
    checks["values_match_vectors"] = values_ok
    checks["vector_hashes_recomputed_match"] = all(vector_hash(v) == v.vector_hash for v in ordered)
    # LOGICAL identity recompute — strictly separate from artifact_sha256.
    checks["matrix_hash_recomputed_match"] = matrix_hash(matrix) == matrix.matrix_hash
    return checks
