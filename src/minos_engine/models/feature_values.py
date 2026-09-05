"""Load the ACTUAL 129 numeric feature values, and make the bytes earn their identity.

Database metadata claiming the expected SHA-256 is not evidence about the file scikit-learn will
read. This module hashes the artifact itself, parses it, and requires the columns, order, member
set and per-BAM value identities to match the frozen authorities before a single value reaches an
estimator.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Final

from minos_engine.common.errors import MinosEngineError
from minos_engine.layer2.features.contracts import (
    AUTHORITATIVE_COLUMNS,
    EXPECTED_COLUMN_COUNT,
)
from minos_engine.models.dataset import TrainingDataset

__all__ = [
    "FeatureValuesError",
    "load_verified_feature_values",
]

_MEMBER_COLUMN: Final = "dataset_id"
_EXPECTED_MEMBERS: Final = 50


class FeatureValuesError(MinosEngineError):
    """The numeric feature matrix does not verify against the frozen authority."""


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise FeatureValuesError(message)


def load_verified_feature_values(
    *, artifact_path: Path, dataset: TrainingDataset
) -> dict[str, tuple[float, ...]]:
    """Return ``dataset_id -> 129 ordered values`` after proving the bytes are the qualified ones.

    The artifact SHA is recomputed from the file, not read from a database row: the trainer is
    about to consume these exact bytes, so these exact bytes are what must carry the identity.
    """
    import pyarrow.parquet as pq

    _require(artifact_path.is_file(), f"the feature matrix artifact is missing: {artifact_path}")
    _require(not artifact_path.is_symlink(), f"{artifact_path} is a symlink")
    raw = artifact_path.read_bytes()
    actual = hashlib.sha256(raw).hexdigest()
    _require(
        actual == dataset.feature_matrix_artifact_sha256,
        f"the artifact hashes to {actual}, but the frozen dataset binds "
        f"{dataset.feature_matrix_artifact_sha256}",
    )

    table = pq.read_table(artifact_path)
    metadata = {
        k.decode("utf-8"): v.decode("utf-8") for k, v in (table.schema.metadata or {}).items()
    }
    _require(
        metadata.get("matrix_hash") == dataset.feature_matrix_hash,
        f"the artifact declares matrix {metadata.get('matrix_hash')}, frozen dataset binds "
        f"{dataset.feature_matrix_hash}",
    )
    _require(
        metadata.get("feature_set_hash") == dataset.feature_set_hash,
        "the artifact was built under a different feature set",
    )
    _require(
        metadata.get("partition") == "train",
        f"the artifact is partition {metadata.get('partition')!r}, not train",
    )
    _require(
        table.num_rows == _EXPECTED_MEMBERS,
        f"the matrix holds {table.num_rows} rows, expected {_EXPECTED_MEMBERS}",
    )

    columns = list(table.column_names)
    _require(
        columns[0] == _MEMBER_COLUMN,
        f"the first column is {columns[0]!r}, expected {_MEMBER_COLUMN!r}",
    )
    value_columns = tuple(columns[1:])
    _require(
        len(value_columns) == EXPECTED_COLUMN_COUNT,
        f"the matrix carries {len(value_columns)} value columns, expected {EXPECTED_COLUMN_COUNT}",
    )
    # order, not merely membership: a permuted matrix would train every model on shuffled
    # predictors while every count and hash of the column SET still matched
    _require(
        value_columns == tuple(AUTHORITATIVE_COLUMNS),
        "the matrix columns are not the qualified columns in their qualified order",
    )
    _require(
        tuple(dataset.feature_names) == tuple(AUTHORITATIVE_COLUMNS),
        "the frozen dataset does not carry the qualified column order",
    )

    frame = table.to_pydict()
    members = [str(v) for v in frame[_MEMBER_COLUMN]]
    _require(len(set(members)) == len(members), "a BAM appears twice in the matrix")
    bound = {b.dataset_id for b in dataset.bam_features}
    _require(
        set(members) == bound,
        f"the matrix and the frozen bindings describe different BAMs: "
        f"{sorted(set(members) ^ bound)}",
    )

    values: dict[str, tuple[float, ...]] = {}
    for index, dataset_id in enumerate(members):
        row = tuple(float(frame[column][index]) for column in value_columns)
        _require(
            all(v == v and v not in (float("inf"), float("-inf")) for v in row),
            f"{dataset_id} carries a non-finite feature value",
        )
        values[dataset_id] = row
    return values
