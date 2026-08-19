"""L2-E pure feature contracts + the frozen domain-separated hash functions.

Everything here is a pure function of the frozen L2-A registry and its inputs — no
database, no file I/O, no extraction pipeline (E1 scope). The hash formulas MUST match
``docs/layer2/FEATURE_VIEW.md`` byte-for-byte in their canonical-JSON preimages.

Fail-closed rules: strict ``extra=forbid`` contracts; train/validation partitions ONLY
(no test contract exists); a FeatureSetManifest must equal the COMPLETE authoritative
feature set (``AUTHORITATIVE_COLUMNS``, exactly 129 paths) — an internally consistent
subset with a recomputed hash is still rejected; FeatureVector construction validates
every value against the canonical column kind (bool/string/coercion rejected before
parsing, non-finite rejected, FRACTION in [0, 1]) so direct construction can never
produce a hash for an invalid vector; vectors and matrices bind the accepted
``REGISTRY_HASH``, the frozen ``FROZEN_FEATURE_SET_HASH`` and the exact 129 count; every
hash field is strict lowercase 64-hex at runtime. Matrix members MUST arrive strictly
ordered by ``dataset_id`` — unordered or duplicate members are REJECTED (the frozen
contract chooses rejection over silent normalization). ``artifact_sha256`` (exact
Parquet bytes) is NOT a field of the logical matrix and never enters ``matrix_hash``.
"""

from __future__ import annotations

import hashlib
import math
import re
from functools import lru_cache
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
    "AUTHORITATIVE_COLUMNS",
    "EXPECTED_COLUMN_COUNT",
    "FROZEN_FEATURE_SET_HASH",
    "FeatureColumn",
    "FeatureSetManifest",
    "FeatureVector",
    "FeatureMatrix",
    "MatrixMember",
    "build_feature_set_manifest",
    "canonical_feature_set",
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

#: FEATURE-READY-v1 selects exactly the ELIGIBLE bam-profile-v1 scalars.
EXPECTED_COLUMN_COUNT = 129

#: Frozen v1 feature-set identity (derived from the accepted registry; see FEATURE_VIEW.md).
FROZEN_FEATURE_SET_HASH = "7e867dfa5633044b69869be8a87fac564431a73a183aa0ab0b1b13158a7c176f"

#: The complete authoritative column set, derived ONCE from the frozen registry.
AUTHORITATIVE_COLUMNS: tuple[str, ...] = tuple(
    sorted(
        p
        for p in production_eligible_fields()
        if (rec := record_for(p)) is not None and rec.source_schema == _SOURCE_SCHEMA
    )
)
if len(AUTHORITATIVE_COLUMNS) != EXPECTED_COLUMN_COUNT:  # pragma: no cover - registry drift
    raise RuntimeError(
        f"feature registry drift: {len(AUTHORITATIVE_COLUMNS)} ELIGIBLE bam-profile-v1 "
        f"fields, expected exactly {EXPECTED_COLUMN_COUNT}"
    )

Partition = Literal["train", "validation"]  # NO test partition contract exists.


def _domain_hash(domain: str, content: dict[str, Any]) -> str:
    return hashlib.sha256((domain + "\n" + canonical_json_str(content)).encode()).hexdigest()


def _require_hex64(name: str, value: str) -> None:
    if not _HEX64.match(value):
        raise ValueError(f"{name} must be strict lowercase 64-hex")


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


def _reject_int_coercion(data: Any, fields: tuple[str, ...]) -> None:
    """Reject bools and numeric strings for integer fields BEFORE pydantic coercion."""
    if isinstance(data, dict):
        for name in fields:
            v = data.get(name)
            if v is not None and (isinstance(v, bool) or not isinstance(v, int)):
                raise ValueError(f"{name} must be a non-bool integer (no coercion)")


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
    """The canonical column manifest: the COMPLETE authoritative feature set (exactly
    the 129 ELIGIBLE bam-profile-v1 scalars), sorted by path, contiguous indices,
    bound to the accepted registry. Any subset — even internally consistent with a
    recomputed hash — is rejected."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: str = FEATURE_SET_SCHEMA_VERSION
    registry_hash: str = Field(min_length=64, max_length=64)
    column_count: int = Field(gt=0)
    columns: tuple[FeatureColumn, ...]
    feature_set_hash: str = Field(default="")

    @model_validator(mode="before")
    @classmethod
    def _no_coercion(cls, data: Any) -> Any:
        _reject_int_coercion(data, ("column_count",))
        return data

    @model_validator(mode="after")
    def _bind(self) -> FeatureSetManifest:
        if self.schema_version != FEATURE_SET_SCHEMA_VERSION:
            raise ValueError(f"unsupported schema_version {self.schema_version!r}")
        _require_hex64("registry_hash", self.registry_hash)
        if self.registry_hash != REGISTRY_HASH:
            raise ValueError("registry_hash does not match the accepted feature registry")
        # the manifest must equal the COMPLETE authoritative set — checked FIRST so an
        # incomplete set is rejected for incompleteness, not for a count mismatch.
        paths = [c.path for c in self.columns]
        missing = set(AUTHORITATIVE_COLUMNS) - set(paths)
        extra = set(paths) - set(AUTHORITATIVE_COLUMNS)
        if missing or extra:
            raise ValueError(
                "columns must equal the complete authoritative feature set "
                f"({len(missing)} missing, {len(extra)} extra)"
            )
        if paths != list(AUTHORITATIVE_COLUMNS):
            raise ValueError(
                "columns must be the authoritative set in sorted canonical path order "
                "without duplicates"
            )
        if self.column_count != EXPECTED_COLUMN_COUNT:
            raise ValueError(f"column_count must be exactly {EXPECTED_COLUMN_COUNT}")
        if self.column_count != len(self.columns):
            raise ValueError("column_count does not match columns")
        if [c.index for c in self.columns] != list(range(len(self.columns))):
            raise ValueError("column indices must be contiguous 0..N-1 in order")
        # every column must carry the EXACT registry metadata for its path.
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
        else:
            _require_hex64("feature_set_hash", self.feature_set_hash)
            if self.feature_set_hash != expected:
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
    columns = []
    for i, path in enumerate(AUTHORITATIVE_COLUMNS):
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


@lru_cache(maxsize=1)
def canonical_feature_set() -> FeatureSetManifest:
    """The single canonical manifest, verified against the frozen identity (cached)."""
    manifest = build_feature_set_manifest()
    if manifest.feature_set_hash != FROZEN_FEATURE_SET_HASH:  # pragma: no cover - drift
        raise RuntimeError("derived canonical manifest does not match FROZEN_FEATURE_SET_HASH")
    return manifest


class FeatureVector(BaseModel):
    """One member's ordered feature values, bound to its snapshot/profile identity,
    the accepted registry and the frozen feature set. Construction is fail-closed:
    every value is validated against its canonical column kind — an invalid vector
    can never produce a vector_hash."""

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

    @model_validator(mode="before")
    @classmethod
    def _no_coercion(cls, data: Any) -> Any:
        """Reject bools, numeric strings and any other coercion BEFORE parsing."""
        _reject_int_coercion(data, ("epoch", "value_count"))
        if isinstance(data, dict):
            vals = data.get("values")
            if isinstance(vals, list | tuple):
                for i, v in enumerate(vals):
                    if isinstance(v, bool) or not isinstance(v, int | float):
                        raise ValueError(
                            f"values[{i}]: expected a non-bool number "
                            f"(no coercion), got {type(v).__name__}"
                        )
        return data

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
            _require_hex64(name, getattr(self, name))
        if self.registry_hash != REGISTRY_HASH:
            raise ValueError("registry_hash does not match the accepted feature registry")
        if self.feature_set_hash != FROZEN_FEATURE_SET_HASH:
            raise ValueError("feature_set_hash does not match the frozen FEATURE-READY-v1 set")
        if self.value_count != EXPECTED_COLUMN_COUNT:
            raise ValueError(f"value_count must be exactly {EXPECTED_COLUMN_COUNT}")
        if self.value_count != len(self.values):
            raise ValueError("value_count does not match values length (wrong-length)")
        # every value validated against the canonical column kind at CONSTRUCTION
        # (finiteness, FRACTION in [0,1]) — never through an optional later call.
        for column, value in zip(canonical_feature_set().columns, self.values, strict=True):
            validate_value_for_kind(column.value_kind, value, column.path)
        expected = vector_hash(self)
        if self.vector_hash == "":
            object.__setattr__(self, "vector_hash", expected)
        else:
            _require_hex64("vector_hash", self.vector_hash)
            if self.vector_hash != expected:
                raise ValueError("vector_hash does not match canonical content")
        return self


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
    vector_hash: str = Field(pattern=r"^[0-9a-f]{64}$")


class FeatureMatrix(BaseModel):
    """The LOGICAL matrix identity: ordered members only, bound to the accepted
    registry and the frozen feature set — artifact_sha256 (exact Parquet bytes) is
    NOT a field here and never enters matrix_hash."""

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

    @model_validator(mode="before")
    @classmethod
    def _no_coercion(cls, data: Any) -> Any:
        _reject_int_coercion(data, ("epoch", "row_count", "column_count"))
        return data

    @model_validator(mode="after")
    def _bind(self) -> FeatureMatrix:
        if self.schema_version != FEATURE_MATRIX_SCHEMA_VERSION:
            raise ValueError(f"unsupported schema_version {self.schema_version!r}")
        for name in ("snapshot_hash", "registry_hash", "feature_set_hash"):
            _require_hex64(name, getattr(self, name))
        if self.registry_hash != REGISTRY_HASH:
            raise ValueError("registry_hash does not match the accepted feature registry")
        if self.feature_set_hash != FROZEN_FEATURE_SET_HASH:
            raise ValueError("feature_set_hash does not match the frozen FEATURE-READY-v1 set")
        if self.column_count != EXPECTED_COLUMN_COUNT:
            raise ValueError(f"column_count must be exactly {EXPECTED_COLUMN_COUNT}")
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
        else:
            _require_hex64("matrix_hash", self.matrix_hash)
            if self.matrix_hash != expected:
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
