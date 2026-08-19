"""L2-E production feature view — pure contracts (no DB, no file I/O, no extraction).

E1 scope: the canonical feature-set column manifest (derived from the frozen L2-A
registry), the immutable FeatureSetManifest / FeatureVector / FeatureMatrix contracts,
and the domain-separated canonical hash functions frozen in
``docs/layer2/FEATURE_VIEW.md``. Matrices exist only for the ``train`` and
``validation`` partitions — there is NO test-partition contract.
"""

from __future__ import annotations

from .contracts import (
    AUTHORITATIVE_COLUMNS,
    EXPECTED_COLUMN_COUNT,
    FEATURE_MATRIX_SCHEMA_VERSION,
    FEATURE_SET_SCHEMA_VERSION,
    FEATURE_VECTOR_SCHEMA_VERSION,
    FROZEN_FEATURE_SET_HASH,
    FeatureColumn,
    FeatureMatrix,
    FeatureSetManifest,
    FeatureVector,
    build_feature_set_manifest,
    canonical_feature_set,
    feature_set_hash,
    matrix_hash,
    vector_hash,
)

__all__ = [
    "AUTHORITATIVE_COLUMNS",
    "EXPECTED_COLUMN_COUNT",
    "FROZEN_FEATURE_SET_HASH",
    "FEATURE_SET_SCHEMA_VERSION",
    "FEATURE_VECTOR_SCHEMA_VERSION",
    "FEATURE_MATRIX_SCHEMA_VERSION",
    "FeatureColumn",
    "FeatureSetManifest",
    "FeatureVector",
    "FeatureMatrix",
    "build_feature_set_manifest",
    "canonical_feature_set",
    "feature_set_hash",
    "vector_hash",
    "matrix_hash",
]
