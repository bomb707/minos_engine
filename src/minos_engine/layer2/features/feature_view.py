"""L2-E canonical feature-view contract (E5) — deterministic, immutable identity.

A :class:`FeatureViewManifest` is the frozen, corpus-independent-in-shape but
epoch-bound identity of ONE production feature matrix (train or validation). It binds —
so nothing downstream can silently change — the feature-set/registry identity, the exact
129 ordered feature columns, the upstream split + snapshot identities, the matrix +
artifact hashes, the exact ordered member identities with their per-member vector /
feature-value hashes, and the row count. Its ``feature_view_hash`` is a pure function of
that content: **no timestamp, UUID, path, credential, database URL, or machine-specific
value participates**, so the same accepted inputs always produce the same identity.

This module is CONTRACT ONLY — it constructs and hashes the identity. It performs no
database access, no filesystem access, and no verification against operational state
(that is :mod:`minos_engine.storage.feature_view_verify`). Building a manifest from a
tampered set of inputs simply yields a different ``feature_view_hash``; proving the
manifest matches the *accepted* operational reality is the verifier's job.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from minos_engine.common.hashing import canonical_hash
from minos_engine.layer2.features.contracts import (
    EXPECTED_COLUMN_COUNT,
    FeatureSetManifest,
    build_feature_set_manifest,
)
from minos_engine.layer2.features.extraction import MATRIX_PARTITIONS

__all__ = [
    "FEATURE_VIEW_VERSION",
    "FeatureViewColumn",
    "FeatureViewMember",
    "FeatureViewManifest",
    "feature_view_hash",
    "build_feature_view_manifest",
]

#: Frozen version tag of the feature-view identity contract. A breaking change to what
#: the identity binds (or how it is canonicalized) requires an explicit bump here.
FEATURE_VIEW_VERSION = "l2e-feature-view-v1"

_HEX64 = r"^[0-9a-f]{64}$"


class FeatureViewColumn(BaseModel):
    """One frozen feature column's full identity (order + semantics)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    index: int = Field(ge=0)
    path: str = Field(min_length=1)
    source_schema: str = Field(min_length=1)
    state: str = Field(min_length=1)
    value_kind: str = Field(min_length=1)


class FeatureViewMember(BaseModel):
    """One frozen row identity: the member, its position, and its content hashes."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    dataset_id: str = Field(min_length=1)
    member_index: int = Field(ge=0)
    vector_hash: str = Field(pattern=_HEX64)
    feature_values_hash: str = Field(pattern=_HEX64)


class FeatureViewManifest(BaseModel):
    """The deterministic, immutable identity of one production feature matrix."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    feature_view_version: str = FEATURE_VIEW_VERSION
    epoch: int = Field(ge=1)
    partition: str

    # feature schema identity
    feature_set_hash: str = Field(pattern=_HEX64)
    feature_registry_hash: str = Field(pattern=_HEX64)
    column_count: int
    columns: tuple[FeatureViewColumn, ...]

    # upstream split + snapshot identity
    snapshot_hash: str = Field(pattern=_HEX64)
    split_manifest_hash: str = Field(pattern=_HEX64)
    registry_snapshot_hash: str = Field(pattern=_HEX64)

    # matrix + artifact identity
    matrix_hash: str = Field(pattern=_HEX64)
    artifact_sha256: str = Field(pattern=_HEX64)
    row_count: int = Field(ge=0)

    # ordered member identity
    members: tuple[FeatureViewMember, ...]

    #: sha256 over the canonical content EXCLUDING this field (set on construction).
    feature_view_hash: str = Field(default="")

    @field_validator("feature_view_version")
    @classmethod
    def _version(cls, v: str) -> str:
        if v != FEATURE_VIEW_VERSION:
            raise ValueError(f"feature_view_version must be {FEATURE_VIEW_VERSION!r}")
        return v

    @field_validator("partition")
    @classmethod
    def _partition(cls, v: str) -> str:
        # ``test`` is structurally excluded: a feature view exists only for train /
        # validation. There is no way to name the sealed test partition here.
        if v not in MATRIX_PARTITIONS:
            raise ValueError(f"partition must be one of {MATRIX_PARTITIONS}, not {v!r}")
        return v

    @model_validator(mode="after")
    def _bind(self) -> FeatureViewManifest:
        if self.column_count != EXPECTED_COLUMN_COUNT:
            raise ValueError(f"column_count must be exactly {EXPECTED_COLUMN_COUNT}")
        if len(self.columns) != EXPECTED_COLUMN_COUNT:
            raise ValueError(f"columns must contain exactly {EXPECTED_COLUMN_COUNT} entries")
        # exact 0..128 order, no gaps, no duplicates, no reordering
        if [c.index for c in self.columns] != list(range(EXPECTED_COLUMN_COUNT)):
            raise ValueError("columns must be ordered by contiguous index 0..128")
        if len({c.path for c in self.columns}) != EXPECTED_COLUMN_COUNT:
            raise ValueError("duplicate feature column path")
        # members carry contiguous 0..row_count-1 indices, unique dataset_ids
        if len(self.members) != self.row_count:
            raise ValueError("members length does not equal row_count")
        if [m.member_index for m in self.members] != list(range(self.row_count)):
            raise ValueError("members must be ordered by contiguous member_index")
        if len({m.dataset_id for m in self.members}) != self.row_count:
            raise ValueError("duplicate member dataset_id")

        expected = feature_view_hash(self)
        if self.feature_view_hash == "":
            object.__setattr__(self, "feature_view_hash", expected)
        elif self.feature_view_hash != expected:
            raise ValueError("feature_view_hash does not match canonical content")
        return self


def _canonical_content(manifest: FeatureViewManifest) -> dict[str, Any]:
    """The exact identity-bearing content, in a deterministic, hash-stable shape. Only
    frozen identities participate — no timestamp, path, credential, or runtime value."""
    return {
        "feature_view_version": manifest.feature_view_version,
        "epoch": manifest.epoch,
        "partition": manifest.partition,
        "feature_set_hash": manifest.feature_set_hash,
        "feature_registry_hash": manifest.feature_registry_hash,
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
        "snapshot_hash": manifest.snapshot_hash,
        "split_manifest_hash": manifest.split_manifest_hash,
        "registry_snapshot_hash": manifest.registry_snapshot_hash,
        "matrix_hash": manifest.matrix_hash,
        "artifact_sha256": manifest.artifact_sha256,
        "row_count": manifest.row_count,
        "members": [
            {
                "dataset_id": m.dataset_id,
                "member_index": m.member_index,
                "vector_hash": m.vector_hash,
                "feature_values_hash": m.feature_values_hash,
            }
            for m in manifest.members
        ],
    }


def feature_view_hash(manifest: FeatureViewManifest) -> str:
    """SHA-256 over the canonical identity content (excludes ``feature_view_hash``)."""
    return canonical_hash(_canonical_content(manifest))


def _columns_from_feature_set(feature_set: FeatureSetManifest) -> tuple[FeatureViewColumn, ...]:
    return tuple(
        FeatureViewColumn(
            index=c.index,
            path=c.path,
            source_schema=c.source_schema,
            state=c.state,
            value_kind=c.value_kind,
        )
        for c in feature_set.columns
    )


def build_feature_view_manifest(
    *,
    epoch: int,
    partition: str,
    snapshot_hash: str,
    split_manifest_hash: str,
    registry_snapshot_hash: str,
    matrix_hash: str,
    artifact_sha256: str,
    row_count: int,
    members: tuple[FeatureViewMember, ...],
    feature_set: FeatureSetManifest | None = None,
) -> FeatureViewManifest:
    """Construct the canonical feature-view manifest from accepted identities.

    The feature schema (129 ordered columns, set + registry hash) is taken from the
    code-owned :func:`build_feature_set_manifest` unless an explicit (already-verified)
    ``feature_set`` is provided. Everything else is bound verbatim from the caller's
    accepted upstream/operational identities; the manifest validators enforce shape and
    compute the deterministic ``feature_view_hash``.
    """
    fs = feature_set if feature_set is not None else build_feature_set_manifest()
    return FeatureViewManifest(
        epoch=epoch,
        partition=partition,
        feature_set_hash=fs.feature_set_hash,
        feature_registry_hash=fs.registry_hash,
        column_count=fs.column_count,
        columns=_columns_from_feature_set(fs),
        snapshot_hash=snapshot_hash,
        split_manifest_hash=split_manifest_hash,
        registry_snapshot_hash=registry_snapshot_hash,
        matrix_hash=matrix_hash,
        artifact_sha256=artifact_sha256,
        row_count=row_count,
        members=members,
    )
