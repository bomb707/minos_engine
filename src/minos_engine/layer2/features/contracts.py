"""L2-E pure feature contracts + the frozen domain-separated hash functions.

Everything here is a pure function of the frozen L2-A registry and its inputs — no
database, no file I/O, no extraction pipeline (E1 scope). The hash formulas MUST match
``docs/layer2/FEATURE_VIEW.md`` byte-for-byte in their canonical-JSON preimages.

Fail-closed rules: strict ``extra=forbid`` contracts; train/validation partitions ONLY
(no test contract exists); REAL values must be finite non-bool numbers; FRACTION values
additionally in [0, 1]; COUNT structural validation (non-bool int, 0 ≤ v ≤ 2**53) is
retained for future feature sets even though FEATURE-READY-v1 has zero COUNT columns;
missing/null/extra/bool-as-number/wrong-length values are rejected. Matrix members MUST
arrive strictly ordered by ``dataset_id`` — unordered or duplicate members are REJECTED
(the frozen contract chooses rejection over silent normalization).
"""

from __future__ import annotations

import hashlib
import math
import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from minos_engine.common.canonical_json import canonical_json_str
from minos_engine.layer2.feature_registry import (
    REGISTRY_HASH,
    production_eligible_fields,
    record_for,
)

__all__ = [
    "FEATURE_SET_SCHEMA_VERSION",
    "FEATURE_VECTOR_SCHEMA_VERSION",
    "FEATURE_MATRIX_SCHEMA_VERSION",
    "FEATURE_SET_DOMAIN",
    "FEATURE_VECTOR_DOMAIN",
    "FEATURE_MATRIX_DOMAIN",
    "FeatureColumn",
    "FeatureSetManifest",
    "FeatureVector",
    "FeatureMatrix",
    "MatrixMember",
    "build_feature_set_manifest",
    "feature_set_hash",
    "vector_hash",
    "matrix_hash",
    "validate_value_for_kind",
]

FEATURE_SET_SCHEMA_VERSION = "feature-set-v1"
FEATURE_VECTOR_SCHEMA_VERSION = "feature-vector-v1"
FEATURE_MATRIX_SCHEMA_VERSION = "feature-matrix-v1"

FEATURE_SET_DOMAIN = "minos:feature-set:v1"
FEATURE_VECTOR_DOMAIN = "minos:feature-vector:v1"
FEATURE_MATRIX_DOMAIN = "minos:feature-matrix:v1"

_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_SOURCE_SCHEMA = "bam-profile-v1"
_MAX_COUNT = 2**53

Partition = Literal["train", "validation"]  # NO test partition contract exists.


def _domain_hash(domain: str, content: dict[str, Any]) -> str:
    return hashlib.sha256((domain + "\n" + canonical_json_str(content)).encode()).hexdigest()


def validate_value_for_kind(kind: str, value: Any, path: str) -> float:
    """Fail-closed scalar validation per registry value kind.

    Rejects bool-as-number, non-finite REAL/FRACTION, out-of-range FRACTION, and
    non-integral / out-of-range COUNT (COUNT retained for future feature sets).
    """
    if isinstance(value, bool):
        raise ValueError(f"{path}: bool is not a numeric feature value")
    if kind in ("REAL", "FRACTION"):
        if not isinstance(value, int | float):
            raise ValueError(f"{path}: expected number, got {type(value).__name__}")
        v = float(value)
        if not math.isfinite(v):
            raise ValueError(f"{path}: non-finite value")
        if kind == "FRACTION" and not (0.0 <= v <= 1.0):
            raise ValueError(f"{path}: FRACTION outside [0, 1]")
        return v
    if kind == "COUNT":
        if not isinstance(value, int):
            raise ValueError(f"{path}: COUNT must be an integer")
        if not (0 <= value <= _MAX_COUNT):
            raise ValueError(f"{path}: COUNT outside [0, 2**53]")
        return float(value)
    raise ValueError(f"{path}: unsupported value kind {kind!r}")


class FeatureColumn(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    index: int = Field(ge=0)
    path: str = Field(min_length=1)
    source_schema: str
    state: str
    value_kind: str

    @model_validator(mode="after")
    def _bind(self) -> FeatureColumn:
        if self.source_schema != _SOURCE_SCHEMA:
            raise ValueError(f"column {self.path}: source_schema must be {_SOURCE_SCHEMA}")
        if self.state != "ELIGIBLE":
            raise ValueError(f"column {self.path}: state must be ELIGIBLE")
        if self.value_kind not in ("REAL", "FRACTION", "COUNT"):
            raise ValueError(f"column {self.path}: invalid value_kind {self.value_kind!r}")
        return self


class FeatureSetManifest(BaseModel):
    """The canonical column manifest: exactly the selected ELIGIBLE bam-profile-v1
    scalars, sorted by path, contiguous indices, bound to the accepted registry."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: str = FEATURE_SET_SCHEMA_VERSION
    registry_hash: str = Field(min_length=64, max_length=64)
    column_count: int = Field(gt=0)
    columns: tuple[FeatureColumn, ...]
    feature_set_hash: str = Field(default="")

    @model_validator(mode="after")
    def _bind(self) -> FeatureSetManifest:
        if self.schema_version != FEATURE_SET_SCHEMA_VERSION:
            raise ValueError(f"unsupported schema_version {self.schema_version!r}")
        if not _HEX64.match(self.registry_hash):
            raise ValueError("registry_hash must be 64 lowercase hex")
        if self.registry_hash != REGISTRY_HASH:
            raise ValueError("registry_hash does not match the accepted feature registry")
        if self.column_count != len(self.columns):
            raise ValueError("column_count does not match columns")
        paths = [c.path for c in self.columns]
        if len(set(paths)) != len(paths):
            raise ValueError("duplicate column paths")
        if paths != sorted(paths):
            raise ValueError("columns must be in sorted canonical path order")
        if [c.index for c in self.columns] != list(range(len(self.columns))):
            raise ValueError("column indices must be contiguous 0..N-1 in order")
        # every column must be a genuine ELIGIBLE bam-profile record with matching kind.
        for c in self.columns:
            rec = record_for(c.path)
            if rec is None or rec.source_schema != _SOURCE_SCHEMA:
                raise ValueError(f"column {c.path}: not a registry bam-profile-v1 field")
            if rec.state.value != "ELIGIBLE":
                raise ValueError(f"column {c.path}: registry state is not ELIGIBLE")
            if rec.value_kind.value != c.value_kind:
                raise ValueError(f"column {c.path}: value_kind mismatch vs registry")
        expected = feature_set_hash(self)
        if self.feature_set_hash == "":
            object.__setattr__(self, "feature_set_hash", expected)
        elif self.feature_set_hash != expected:
            raise ValueError("feature_set_hash does not match canonical content")
        return self


def feature_set_hash(manifest: FeatureSetManifest) -> str:
    """Frozen: SHA256("minos:feature-set:v1\\n" + canonical_json({...}))."""
    return _domain_hash(
        FEATURE_SET_DOMAIN,
        {
            "schema_version": FEATURE_SET_SCHEMA_VERSION,
            "registry_hash": manifest.registry_hash,
            "column_count": manifest.column_count,
            "columns": [
                {
                    "index": c.index,
                    "path": c.path,
                    "source_schema": c.source_schema,
                    "state": c.state,
                    "value_kind": c.value_kind,
                }
                for c in manifest.columns
            ],
        },
    )


def build_feature_set_manifest() -> FeatureSetManifest:
    """Derive the canonical FEATURE-READY-v1 manifest from the frozen registry."""
    selected = sorted(
        p
        for p in production_eligible_fields()
        if (rec := record_for(p)) is not None and rec.source_schema == _SOURCE_SCHEMA
    )
    columns = []
    for i, path in enumerate(selected):
        rec = record_for(path)
        assert rec is not None  # noqa: S101 - selected from the registry above
        columns.append(
            FeatureColumn(
                index=i,
                path=path,
                source_schema=rec.source_schema,
                state=rec.state.value,
                value_kind=rec.value_kind.value,
            )
        )
    return FeatureSetManifest(
        registry_hash=REGISTRY_HASH,
        column_count=len(columns),
        columns=tuple(columns),
    )


class FeatureVector(BaseModel):
    """One member's ordered feature values, bound to its snapshot/profile identity."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: str = FEATURE_VECTOR_SCHEMA_VERSION
    epoch: int = Field(ge=1)
    dataset_id: str = Field(min_length=1)
    profile_id: str = Field(min_length=1)
    content_hash: str = Field(min_length=64, max_length=64)
    feature_values_hash: str = Field(min_length=64, max_length=64)
    partition: Partition
    snapshot_hash: str = Field(min_length=64, max_length=64)
    registry_hash: str = Field(min_length=64, max_length=64)
    feature_set_hash: str = Field(min_length=64, max_length=64)
    value_count: int = Field(gt=0)
    values: tuple[float, ...]
    vector_hash: str = Field(default="")

    @model_validator(mode="after")
    def _bind(self) -> FeatureVector:
        if self.schema_version != FEATURE_VECTOR_SCHEMA_VERSION:
            raise ValueError(f"unsupported schema_version {self.schema_version!r}")
        for name in (
            "content_hash",
            "feature_values_hash",
            "snapshot_hash",
            "registry_hash",
            "feature_set_hash",
        ):
            if not _HEX64.match(getattr(self, name)):
                raise ValueError(f"{name} must be 64 lowercase hex")
        if self.value_count != len(self.values):
            raise ValueError("value_count does not match values length (wrong-length)")
        for i, v in enumerate(self.values):
            if not math.isfinite(v):
                raise ValueError(f"values[{i}]: non-finite value")
        expected = vector_hash(self)
        if self.vector_hash == "":
            object.__setattr__(self, "vector_hash", expected)
        elif self.vector_hash != expected:
            raise ValueError("vector_hash does not match canonical content")
        return self

    def validate_against(self, manifest: FeatureSetManifest) -> None:
        """Column-order + kind validation against the frozen manifest (fail-closed)."""
        if self.feature_set_hash != manifest.feature_set_hash:
            raise ValueError("vector feature_set_hash does not match the manifest")
        if self.value_count != manifest.column_count:
            raise ValueError("value_count does not match the manifest column count")
        for column, value in zip(manifest.columns, self.values, strict=True):
            validate_value_for_kind(column.value_kind, value, column.path)


def vector_hash(vector: FeatureVector) -> str:
    """Frozen: SHA256("minos:feature-vector:v1\\n" + canonical_json({...}))."""
    return _domain_hash(
        FEATURE_VECTOR_DOMAIN,
        {
            "schema_version": FEATURE_VECTOR_SCHEMA_VERSION,
            "epoch": vector.epoch,
            "dataset_id": vector.dataset_id,
            "profile_id": vector.profile_id,
            "content_hash": vector.content_hash,
            "feature_values_hash": vector.feature_values_hash,
            "partition": vector.partition,
            "snapshot_hash": vector.snapshot_hash,
            "registry_hash": vector.registry_hash,
            "feature_set_hash": vector.feature_set_hash,
            "value_count": vector.value_count,
            "values": list(vector.values),
        },
    )


class MatrixMember(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    dataset_id: str = Field(min_length=1)
    vector_hash: str = Field(min_length=64, max_length=64)


class FeatureMatrix(BaseModel):
    """The LOGICAL matrix identity: ordered members only — artifact_sha256 (exact
    Parquet bytes) is recorded elsewhere and never enters matrix_hash."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: str = FEATURE_MATRIX_SCHEMA_VERSION
    epoch: int = Field(ge=1)
    snapshot_hash: str = Field(min_length=64, max_length=64)
    partition: Partition
    registry_hash: str = Field(min_length=64, max_length=64)
    feature_set_hash: str = Field(min_length=64, max_length=64)
    row_count: int = Field(ge=0)
    column_count: int = Field(gt=0)
    members: tuple[MatrixMember, ...]
    matrix_hash: str = Field(default="")

    @model_validator(mode="after")
    def _bind(self) -> FeatureMatrix:
        if self.schema_version != FEATURE_MATRIX_SCHEMA_VERSION:
            raise ValueError(f"unsupported schema_version {self.schema_version!r}")
        for name in ("snapshot_hash", "registry_hash", "feature_set_hash"):
            if not _HEX64.match(getattr(self, name)):
                raise ValueError(f"{name} must be 64 lowercase hex")
        if self.row_count != len(self.members):
            raise ValueError("row_count does not match members")
        ids = [m.dataset_id for m in self.members]
        if len(set(ids)) != len(ids):
            raise ValueError("duplicate dataset_id members are rejected")
        # frozen contract choice: unordered members are REJECTED, never normalized.
        if ids != sorted(ids):
            raise ValueError("members must be strictly ordered by dataset_id")
        expected = matrix_hash(self)
        if self.matrix_hash == "":
            object.__setattr__(self, "matrix_hash", expected)
        elif self.matrix_hash != expected:
            raise ValueError("matrix_hash does not match canonical content")
        return self


def matrix_hash(matrix: FeatureMatrix) -> str:
    """Frozen: SHA256("minos:feature-matrix:v1\\n" + canonical_json({...}))."""
    return _domain_hash(
        FEATURE_MATRIX_DOMAIN,
        {
            "schema_version": FEATURE_MATRIX_SCHEMA_VERSION,
            "epoch": matrix.epoch,
            "snapshot_hash": matrix.snapshot_hash,
            "partition": matrix.partition,
            "registry_hash": matrix.registry_hash,
            "feature_set_hash": matrix.feature_set_hash,
            "row_count": matrix.row_count,
            "column_count": matrix.column_count,
            "members": [
                {"dataset_id": m.dataset_id, "vector_hash": m.vector_hash} for m in matrix.members
            ],
        },
    )
