"""Code-owned Layer 1 → Layer 2 feature-eligibility registry (L2-A, remediated).

Every Layer 1 v2 analytical field maps to exactly one registry record. Dynamic
config-bound maps (quantiles, support/allele-fraction site counts) are expanded to
concrete scalar leaves (e.g. ``mapping_quality.quantiles.P50``); data-dependent
maps (homopolymer histogram, stratum window counts) remain documentation
containers (``model_feature = False``) and are never scalar features. Window-profile
scalar columns live in the ``window.*`` namespace.

Production selection admits **ELIGIBLE scalar leaves only**:

  * containers, identifiers, operational, categorical, and unknown paths are rejected;
  * CONDITIONAL and RESEARCH_ONLY are rejected (no promotion exists in L2-A);
  * FORBIDDEN is always rejected.

A caller-supplied object can never authorize a CONDITIONAL/RESEARCH_ONLY feature —
there is no promotion parameter. A real promotion is a future stage requiring a
repository-owned, hash-bound, git-bound accepted promotion artifact (see
``PromotionRecord`` docstring and ``reports/LAYER2_DATASET_SPLIT_POLICY.md``).

The registry is deterministic and canonically hashable. Its hash binds every
scalar path, state, family, value kind, model-feature flag, config-bound flag,
truth-derived status, source-schema identity, plus the accepted profiler-config and
Layer 1 schema hashes. This module owns classification only — it opens no files,
runs no extraction, and imports no Layer 1 profiler code.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping

from minos_engine.common.hashing import canonical_hash

from .contracts import (
    CanonicalFeatureVector,
    FeatureEligibilityState,
    FeatureRegistryRecord,
    FeatureValueKind,
)
from .prerequisites import LAYER1_SCHEMA_HASH, PROFILER_CONFIG_HASH

__all__ = [
    "FEATURE_REGISTRY",
    "REGISTRY_HASH",
    "CONFIG_BOUND_KEYS",
    "SOURCE_SCHEMA_HASHES",
    "canonical_registry",
    "registry_hash",
    "record_for",
    "state_for",
    "counts_by_state",
    "counts_by_value_kind",
    "production_eligible_fields",
    "scalar_model_feature_paths",
    "container_paths",
    "assert_production_feature_vector",
    "validate_scalar_value",
    "validate_production_feature_mapping",
]

ELIGIBLE = FeatureEligibilityState.ELIGIBLE
CONDITIONAL = FeatureEligibilityState.CONDITIONAL
RESEARCH_ONLY = FeatureEligibilityState.RESEARCH_ONLY
FORBIDDEN = FeatureEligibilityState.FORBIDDEN

FRACTION = FeatureValueKind.FRACTION
REAL = FeatureValueKind.REAL
COUNT = FeatureValueKind.COUNT
BOOL = FeatureValueKind.BOOL
CATEGORICAL = FeatureValueKind.CATEGORICAL
IDENTIFIER = FeatureValueKind.IDENTIFIER
OPERATIONAL = FeatureValueKind.OPERATIONAL
CONTAINER = FeatureValueKind.CONTAINER

# Pinned content hashes of the source schemas (drift-checked by a test that
# re-hashes the schema files) plus the external-sentinel identity.
SOURCE_SCHEMA_HASHES: dict[str, str] = {
    "bam-profile-v1": "e127d10459068d12d61753f4ddcd4a503a481767aec24c2e5f9ee48655018df6",
    "window-profile-v1": "2a3ac682e8bc2617359a411dcd6952fc972bf93d642bfc3c23d5e90d1cd06c6c",
    "external": "8cb6bad9898af86464656143795596417b30cecd868328aad1f0440d578730b0",
}

# Config-bound dynamic-map keys, derived from configs/layer1/default.yaml and bound
# to the accepted profiler-config hash. A test re-derives these from the config.
_QUANTILE_KEYS = ("P01", "P05", "P10", "P25", "P50", "P75", "P90", "P95", "P99")
_SUPPORT_KEYS = ("support_ge_2", "support_ge_3", "support_ge_5", "support_ge_8", "support_ge_10")
_AF_KEYS = ("af_ge_05", "af_ge_10", "af_ge_20", "af_ge_30", "af_ge_40")
CONFIG_BOUND_KEYS: dict[str, tuple[str, ...]] = {
    "mapping_quality.quantiles": _QUANTILE_KEYS,
    "base_quality.quantiles_phred": _QUANTILE_KEYS,
    "read_length.quantiles": _QUANTILE_KEYS,
    "pairing.quantiles_bp": _QUANTILE_KEYS,
    "coverage.fragment_primary.depth_quantiles": _QUANTILE_KEYS,
    "coverage.duplicate_including.depth_quantiles": _QUANTILE_KEYS,
    "variant_evidence.support_threshold_site_counts": _SUPPORT_KEYS,
    "variant_evidence.allele_fraction_threshold_site_counts": _AF_KEYS,
}

# External FORBIDDEN sentinels that are genuinely truth-derived.
_TRUTH_DERIVED = frozenset(
    {
        "external.truth_vcf",
        "external.mutation_calls",
        "external.happy_results",
        "external.tp_fp_fn_counts",
        "external.hidden_labels",
        "external.round_final_score",
    }
)

# (path, state, family, value_kind, model_feature, config_bound, source_schema)
_RAW: tuple[tuple[str, FeatureEligibilityState, str, FeatureValueKind, bool, bool, str], ...] = (
    (
        "alignment.aligned_query_bases",
        CONDITIONAL,
        "alignment",
        COUNT,
        True,
        False,
        "bam-profile-v1",
    ),
    ("alignment.cigar_ins_del_burden", ELIGIBLE, "alignment", REAL, True, False, "bam-profile-v1"),
    ("alignment.deleted_bases", CONDITIONAL, "alignment", COUNT, True, False, "bam-profile-v1"),
    (
        "alignment.hard_clipped_bases",
        CONDITIONAL,
        "alignment",
        COUNT,
        True,
        False,
        "bam-profile-v1",
    ),
    (
        "alignment.indel_bearing_read_fraction",
        ELIGIBLE,
        "alignment",
        FRACTION,
        True,
        False,
        "bam-profile-v1",
    ),
    ("alignment.inserted_bases", CONDITIONAL, "alignment", COUNT, True, False, "bam-profile-v1"),
    (
        "alignment.nm_availability_fraction",
        ELIGIBLE,
        "alignment",
        FRACTION,
        True,
        False,
        "bam-profile-v1",
    ),
    ("alignment.nm_per_aligned_base", ELIGIBLE, "alignment", REAL, True, False, "bam-profile-v1"),
    (
        "alignment.query_consuming_bases",
        CONDITIONAL,
        "alignment",
        COUNT,
        True,
        False,
        "bam-profile-v1",
    ),
    ("alignment.skipped_bases", CONDITIONAL, "alignment", COUNT, True, False, "bam-profile-v1"),
    (
        "alignment.soft_clipped_base_fraction",
        ELIGIBLE,
        "alignment",
        FRACTION,
        True,
        False,
        "bam-profile-v1",
    ),
    (
        "alignment.soft_clipped_bases",
        CONDITIONAL,
        "alignment",
        COUNT,
        True,
        False,
        "bam-profile-v1",
    ),
    (
        "alignment.soft_clipped_read_fraction",
        ELIGIBLE,
        "alignment",
        FRACTION,
        True,
        False,
        "bam-profile-v1",
    ),
    (
        "base_quality.bases_observed",
        CONDITIONAL,
        "base_quality",
        COUNT,
        True,
        False,
        "bam-profile-v1",
    ),
    (
        "base_quality.bases_with_quality",
        CONDITIONAL,
        "base_quality",
        COUNT,
        True,
        False,
        "bam-profile-v1",
    ),
    (
        "base_quality.bq_lt20_fraction",
        ELIGIBLE,
        "base_quality",
        FRACTION,
        True,
        False,
        "bam-profile-v1",
    ),
    (
        "base_quality.mean_base_quality_phred",
        ELIGIBLE,
        "base_quality",
        REAL,
        True,
        False,
        "bam-profile-v1",
    ),
    (
        "base_quality.missing_quality_fraction",
        ELIGIBLE,
        "base_quality",
        FRACTION,
        True,
        False,
        "bam-profile-v1",
    ),
    (
        "base_quality.quantiles_phred",
        ELIGIBLE,
        "base_quality",
        CONTAINER,
        False,
        True,
        "bam-profile-v1",
    ),
    (
        "base_quality.quantiles_phred.P01",
        ELIGIBLE,
        "base_quality",
        REAL,
        True,
        True,
        "bam-profile-v1",
    ),
    (
        "base_quality.quantiles_phred.P05",
        ELIGIBLE,
        "base_quality",
        REAL,
        True,
        True,
        "bam-profile-v1",
    ),
    (
        "base_quality.quantiles_phred.P10",
        ELIGIBLE,
        "base_quality",
        REAL,
        True,
        True,
        "bam-profile-v1",
    ),
    (
        "base_quality.quantiles_phred.P25",
        ELIGIBLE,
        "base_quality",
        REAL,
        True,
        True,
        "bam-profile-v1",
    ),
    (
        "base_quality.quantiles_phred.P50",
        ELIGIBLE,
        "base_quality",
        REAL,
        True,
        True,
        "bam-profile-v1",
    ),
    (
        "base_quality.quantiles_phred.P75",
        ELIGIBLE,
        "base_quality",
        REAL,
        True,
        True,
        "bam-profile-v1",
    ),
    (
        "base_quality.quantiles_phred.P90",
        ELIGIBLE,
        "base_quality",
        REAL,
        True,
        True,
        "bam-profile-v1",
    ),
    (
        "base_quality.quantiles_phred.P95",
        ELIGIBLE,
        "base_quality",
        REAL,
        True,
        True,
        "bam-profile-v1",
    ),
    (
        "base_quality.quantiles_phred.P99",
        ELIGIBLE,
        "base_quality",
        REAL,
        True,
        True,
        "bam-profile-v1",
    ),
    (
        "base_quality.stddev_base_quality_phred",
        ELIGIBLE,
        "base_quality",
        REAL,
        True,
        False,
        "bam-profile-v1",
    ),
    (
        "completion.completed_families",
        FORBIDDEN,
        "completion",
        CONTAINER,
        False,
        False,
        "bam-profile-v1",
    ),
    ("completion.status", FORBIDDEN, "completion", OPERATIONAL, False, False, "bam-profile-v1"),
    (
        "completion.variant_evidence_completion",
        ELIGIBLE,
        "completion",
        REAL,
        True,
        False,
        "bam-profile-v1",
    ),
    ("confidence.availability", CONDITIONAL, "confidence", REAL, True, False, "bam-profile-v1"),
    ("confidence.band", FORBIDDEN, "confidence", CATEGORICAL, False, False, "bam-profile-v1"),
    ("confidence.completeness", CONDITIONAL, "confidence", REAL, True, False, "bam-profile-v1"),
    ("confidence.consistency", CONDITIONAL, "confidence", REAL, True, False, "bam-profile-v1"),
    ("confidence.integrity", CONDITIONAL, "confidence", REAL, True, False, "bam-profile-v1"),
    ("confidence.overall", CONDITIONAL, "confidence", REAL, True, False, "bam-profile-v1"),
    (
        "coverage.deletion_aware_depth_recorded_separately",
        FORBIDDEN,
        "coverage",
        IDENTIFIER,
        False,
        False,
        "bam-profile-v1",
    ),
    (
        "coverage.duplicate_including.callable_base_fraction",
        ELIGIBLE,
        "coverage",
        FRACTION,
        True,
        False,
        "bam-profile-v1",
    ),
    (
        "coverage.duplicate_including.coefficient_of_variation",
        ELIGIBLE,
        "coverage",
        REAL,
        True,
        False,
        "bam-profile-v1",
    ),
    (
        "coverage.duplicate_including.depth_gt100_fraction",
        ELIGIBLE,
        "coverage",
        FRACTION,
        True,
        False,
        "bam-profile-v1",
    ),
    (
        "coverage.duplicate_including.depth_gt200_fraction",
        ELIGIBLE,
        "coverage",
        FRACTION,
        True,
        False,
        "bam-profile-v1",
    ),
    (
        "coverage.duplicate_including.depth_gt50_fraction",
        ELIGIBLE,
        "coverage",
        FRACTION,
        True,
        False,
        "bam-profile-v1",
    ),
    (
        "coverage.duplicate_including.depth_lt10_fraction",
        ELIGIBLE,
        "coverage",
        FRACTION,
        True,
        False,
        "bam-profile-v1",
    ),
    (
        "coverage.duplicate_including.depth_lt20_fraction",
        ELIGIBLE,
        "coverage",
        FRACTION,
        True,
        False,
        "bam-profile-v1",
    ),
    (
        "coverage.duplicate_including.depth_lt5_fraction",
        ELIGIBLE,
        "coverage",
        FRACTION,
        True,
        False,
        "bam-profile-v1",
    ),
    (
        "coverage.duplicate_including.depth_mad",
        ELIGIBLE,
        "coverage",
        REAL,
        True,
        False,
        "bam-profile-v1",
    ),
    (
        "coverage.duplicate_including.depth_quantiles",
        ELIGIBLE,
        "coverage",
        CONTAINER,
        False,
        True,
        "bam-profile-v1",
    ),
    (
        "coverage.duplicate_including.depth_quantiles.P01",
        ELIGIBLE,
        "coverage",
        REAL,
        True,
        True,
        "bam-profile-v1",
    ),
    (
        "coverage.duplicate_including.depth_quantiles.P05",
        ELIGIBLE,
        "coverage",
        REAL,
        True,
        True,
        "bam-profile-v1",
    ),
    (
        "coverage.duplicate_including.depth_quantiles.P10",
        ELIGIBLE,
        "coverage",
        REAL,
        True,
        True,
        "bam-profile-v1",
    ),
    (
        "coverage.duplicate_including.depth_quantiles.P25",
        ELIGIBLE,
        "coverage",
        REAL,
        True,
        True,
        "bam-profile-v1",
    ),
    (
        "coverage.duplicate_including.depth_quantiles.P50",
        ELIGIBLE,
        "coverage",
        REAL,
        True,
        True,
        "bam-profile-v1",
    ),
    (
        "coverage.duplicate_including.depth_quantiles.P75",
        ELIGIBLE,
        "coverage",
        REAL,
        True,
        True,
        "bam-profile-v1",
    ),
    (
        "coverage.duplicate_including.depth_quantiles.P90",
        ELIGIBLE,
        "coverage",
        REAL,
        True,
        True,
        "bam-profile-v1",
    ),
    (
        "coverage.duplicate_including.depth_quantiles.P95",
        ELIGIBLE,
        "coverage",
        REAL,
        True,
        True,
        "bam-profile-v1",
    ),
    (
        "coverage.duplicate_including.depth_quantiles.P99",
        ELIGIBLE,
        "coverage",
        REAL,
        True,
        True,
        "bam-profile-v1",
    ),
    (
        "coverage.duplicate_including.depth_semantics",
        FORBIDDEN,
        "coverage",
        IDENTIFIER,
        False,
        False,
        "bam-profile-v1",
    ),
    (
        "coverage.duplicate_including.max_depth",
        CONDITIONAL,
        "coverage",
        COUNT,
        True,
        False,
        "bam-profile-v1",
    ),
    (
        "coverage.duplicate_including.mean_depth_reads_per_base",
        ELIGIBLE,
        "coverage",
        REAL,
        True,
        False,
        "bam-profile-v1",
    ),
    (
        "coverage.duplicate_including.median_depth_reads_per_base",
        ELIGIBLE,
        "coverage",
        REAL,
        True,
        False,
        "bam-profile-v1",
    ),
    (
        "coverage.duplicate_including.stddev_depth",
        ELIGIBLE,
        "coverage",
        REAL,
        True,
        False,
        "bam-profile-v1",
    ),
    (
        "coverage.duplicate_including.view_name",
        FORBIDDEN,
        "coverage",
        IDENTIFIER,
        False,
        False,
        "bam-profile-v1",
    ),
    (
        "coverage.duplicate_including.zero_depth_fraction",
        ELIGIBLE,
        "coverage",
        FRACTION,
        True,
        False,
        "bam-profile-v1",
    ),
    (
        "coverage.eligible_region_bases",
        CONDITIONAL,
        "coverage",
        COUNT,
        True,
        False,
        "bam-profile-v1",
    ),
    (
        "coverage.fragment_primary.callable_base_fraction",
        ELIGIBLE,
        "coverage",
        FRACTION,
        True,
        False,
        "bam-profile-v1",
    ),
    (
        "coverage.fragment_primary.coefficient_of_variation",
        ELIGIBLE,
        "coverage",
        REAL,
        True,
        False,
        "bam-profile-v1",
    ),
    (
        "coverage.fragment_primary.depth_gt100_fraction",
        ELIGIBLE,
        "coverage",
        FRACTION,
        True,
        False,
        "bam-profile-v1",
    ),
    (
        "coverage.fragment_primary.depth_gt200_fraction",
        ELIGIBLE,
        "coverage",
        FRACTION,
        True,
        False,
        "bam-profile-v1",
    ),
    (
        "coverage.fragment_primary.depth_gt50_fraction",
        ELIGIBLE,
        "coverage",
        FRACTION,
        True,
        False,
        "bam-profile-v1",
    ),
    (
        "coverage.fragment_primary.depth_lt10_fraction",
        ELIGIBLE,
        "coverage",
        FRACTION,
        True,
        False,
        "bam-profile-v1",
    ),
    (
        "coverage.fragment_primary.depth_lt20_fraction",
        ELIGIBLE,
        "coverage",
        FRACTION,
        True,
        False,
        "bam-profile-v1",
    ),
    (
        "coverage.fragment_primary.depth_lt5_fraction",
        ELIGIBLE,
        "coverage",
        FRACTION,
        True,
        False,
        "bam-profile-v1",
    ),
    (
        "coverage.fragment_primary.depth_mad",
        ELIGIBLE,
        "coverage",
        REAL,
        True,
        False,
        "bam-profile-v1",
    ),
    (
        "coverage.fragment_primary.depth_quantiles",
        ELIGIBLE,
        "coverage",
        CONTAINER,
        False,
        True,
        "bam-profile-v1",
    ),
    (
        "coverage.fragment_primary.depth_quantiles.P01",
        ELIGIBLE,
        "coverage",
        REAL,
        True,
        True,
        "bam-profile-v1",
    ),
    (
        "coverage.fragment_primary.depth_quantiles.P05",
        ELIGIBLE,
        "coverage",
        REAL,
        True,
        True,
        "bam-profile-v1",
    ),
    (
        "coverage.fragment_primary.depth_quantiles.P10",
        ELIGIBLE,
        "coverage",
        REAL,
        True,
        True,
        "bam-profile-v1",
    ),
    (
        "coverage.fragment_primary.depth_quantiles.P25",
        ELIGIBLE,
        "coverage",
        REAL,
        True,
        True,
        "bam-profile-v1",
    ),
    (
        "coverage.fragment_primary.depth_quantiles.P50",
        ELIGIBLE,
        "coverage",
        REAL,
        True,
        True,
        "bam-profile-v1",
    ),
    (
        "coverage.fragment_primary.depth_quantiles.P75",
        ELIGIBLE,
        "coverage",
        REAL,
        True,
        True,
        "bam-profile-v1",
    ),
    (
        "coverage.fragment_primary.depth_quantiles.P90",
        ELIGIBLE,
        "coverage",
        REAL,
        True,
        True,
        "bam-profile-v1",
    ),
    (
        "coverage.fragment_primary.depth_quantiles.P95",
        ELIGIBLE,
        "coverage",
        REAL,
        True,
        True,
        "bam-profile-v1",
    ),
    (
        "coverage.fragment_primary.depth_quantiles.P99",
        ELIGIBLE,
        "coverage",
        REAL,
        True,
        True,
        "bam-profile-v1",
    ),
    (
        "coverage.fragment_primary.depth_semantics",
        FORBIDDEN,
        "coverage",
        IDENTIFIER,
        False,
        False,
        "bam-profile-v1",
    ),
    (
        "coverage.fragment_primary.max_depth",
        CONDITIONAL,
        "coverage",
        COUNT,
        True,
        False,
        "bam-profile-v1",
    ),
    (
        "coverage.fragment_primary.mean_depth_reads_per_base",
        ELIGIBLE,
        "coverage",
        REAL,
        True,
        False,
        "bam-profile-v1",
    ),
    (
        "coverage.fragment_primary.median_depth_reads_per_base",
        ELIGIBLE,
        "coverage",
        REAL,
        True,
        False,
        "bam-profile-v1",
    ),
    (
        "coverage.fragment_primary.stddev_depth",
        ELIGIBLE,
        "coverage",
        REAL,
        True,
        False,
        "bam-profile-v1",
    ),
    (
        "coverage.fragment_primary.view_name",
        FORBIDDEN,
        "coverage",
        IDENTIFIER,
        False,
        False,
        "bam-profile-v1",
    ),
    (
        "coverage.fragment_primary.zero_depth_fraction",
        ELIGIBLE,
        "coverage",
        FRACTION,
        True,
        False,
        "bam-profile-v1",
    ),
    ("degradation", FORBIDDEN, "degradation", CONTAINER, False, False, "bam-profile-v1"),
    (
        "difficulty.base_quality_risk",
        CONDITIONAL,
        "difficulty",
        FRACTION,
        True,
        False,
        "bam-profile-v1",
    ),
    (
        "difficulty.complexity_risk",
        CONDITIONAL,
        "difficulty",
        FRACTION,
        True,
        False,
        "bam-profile-v1",
    ),
    (
        "difficulty.coverage_risk",
        CONDITIONAL,
        "difficulty",
        FRACTION,
        True,
        False,
        "bam-profile-v1",
    ),
    ("difficulty.mapping_risk", CONDITIONAL, "difficulty", FRACTION, True, False, "bam-profile-v1"),
    (
        "difficulty.reference_context_risk",
        CONDITIONAL,
        "difficulty",
        FRACTION,
        True,
        False,
        "bam-profile-v1",
    ),
    (
        "difficulty.transform_version",
        FORBIDDEN,
        "difficulty",
        IDENTIFIER,
        False,
        False,
        "bam-profile-v1",
    ),
    ("external.dataset_id", FORBIDDEN, "external", IDENTIFIER, False, False, "external"),
    (
        "external.evaluation_credentials",
        FORBIDDEN,
        "external",
        IDENTIFIER,
        False,
        False,
        "external",
    ),
    ("external.happy_results", FORBIDDEN, "external", IDENTIFIER, False, False, "external"),
    ("external.hidden_labels", FORBIDDEN, "external", IDENTIFIER, False, False, "external"),
    ("external.mutation_calls", FORBIDDEN, "external", IDENTIFIER, False, False, "external"),
    ("external.operator_identity", FORBIDDEN, "external", IDENTIFIER, False, False, "external"),
    (
        "external.operator_rank_identity",
        FORBIDDEN,
        "external",
        IDENTIFIER,
        False,
        False,
        "external",
    ),
    (
        "external.previous_winning_config",
        FORBIDDEN,
        "external",
        IDENTIFIER,
        False,
        False,
        "external",
    ),
    ("external.round_final_score", FORBIDDEN, "external", IDENTIFIER, False, False, "external"),
    ("external.round_id", FORBIDDEN, "external", IDENTIFIER, False, False, "external"),
    ("external.tp_fp_fn_counts", FORBIDDEN, "external", IDENTIFIER, False, False, "external"),
    ("external.truth_vcf", FORBIDDEN, "external", IDENTIFIER, False, False, "external"),
    (
        "filter_counts.excluded_below_mapq",
        CONDITIONAL,
        "filter_counts",
        COUNT,
        True,
        False,
        "bam-profile-v1",
    ),
    (
        "filter_counts.excluded_duplicate",
        CONDITIONAL,
        "filter_counts",
        COUNT,
        True,
        False,
        "bam-profile-v1",
    ),
    (
        "filter_counts.excluded_qcfail",
        CONDITIONAL,
        "filter_counts",
        COUNT,
        True,
        False,
        "bam-profile-v1",
    ),
    (
        "filter_counts.excluded_secondary",
        CONDITIONAL,
        "filter_counts",
        COUNT,
        True,
        False,
        "bam-profile-v1",
    ),
    (
        "filter_counts.excluded_supplementary",
        CONDITIONAL,
        "filter_counts",
        COUNT,
        True,
        False,
        "bam-profile-v1",
    ),
    (
        "filter_counts.excluded_unmapped",
        CONDITIONAL,
        "filter_counts",
        COUNT,
        True,
        False,
        "bam-profile-v1",
    ),
    ("filter_counts.included", CONDITIONAL, "filter_counts", COUNT, True, False, "bam-profile-v1"),
    ("filter_counts.observed", CONDITIONAL, "filter_counts", COUNT, True, False, "bam-profile-v1"),
    ("header.contigs", FORBIDDEN, "header", CONTAINER, False, False, "bam-profile-v1"),
    ("header.coordinate_sorted", FORBIDDEN, "header", IDENTIFIER, False, False, "bam-profile-v1"),
    ("header.has_md_tag_observed", FORBIDDEN, "header", IDENTIFIER, False, False, "bam-profile-v1"),
    ("header.has_nm_tag_observed", FORBIDDEN, "header", IDENTIFIER, False, False, "bam-profile-v1"),
    ("header.hd_version", FORBIDDEN, "header", IDENTIFIER, False, False, "bam-profile-v1"),
    ("header.library_names", FORBIDDEN, "header", CONTAINER, False, False, "bam-profile-v1"),
    ("header.platform_values", FORBIDDEN, "header", CONTAINER, False, False, "bam-profile-v1"),
    ("header.program_ids", FORBIDDEN, "header", CONTAINER, False, False, "bam-profile-v1"),
    ("header.read_group_ids", FORBIDDEN, "header", CONTAINER, False, False, "bam-profile-v1"),
    ("header.sample_names", FORBIDDEN, "header", CONTAINER, False, False, "bam-profile-v1"),
    ("header.sort_order", FORBIDDEN, "header", IDENTIFIER, False, False, "bam-profile-v1"),
    ("identity.bam_sha256", FORBIDDEN, "identity", IDENTIFIER, False, False, "bam-profile-v1"),
    ("identity.bam_size_bytes", FORBIDDEN, "identity", IDENTIFIER, False, False, "bam-profile-v1"),
    ("identity.fai_sha256", FORBIDDEN, "identity", IDENTIFIER, False, False, "bam-profile-v1"),
    ("identity.header_sha256", FORBIDDEN, "identity", IDENTIFIER, False, False, "bam-profile-v1"),
    ("identity.index_sha256", FORBIDDEN, "identity", IDENTIFIER, False, False, "bam-profile-v1"),
    ("identity.index_status", FORBIDDEN, "identity", IDENTIFIER, False, False, "bam-profile-v1"),
    (
        "identity.reference_sha256",
        FORBIDDEN,
        "identity",
        IDENTIFIER,
        False,
        False,
        "bam-profile-v1",
    ),
    (
        "identity.reference_status",
        FORBIDDEN,
        "identity",
        IDENTIFIER,
        False,
        False,
        "bam-profile-v1",
    ),
    (
        "identity.verification_strength",
        FORBIDDEN,
        "identity",
        IDENTIFIER,
        False,
        False,
        "bam-profile-v1",
    ),
    ("mapping_quality.count", CONDITIONAL, "mapping_quality", COUNT, True, False, "bam-profile-v1"),
    ("mapping_quality.maximum", ELIGIBLE, "mapping_quality", REAL, True, False, "bam-profile-v1"),
    ("mapping_quality.mean", ELIGIBLE, "mapping_quality", REAL, True, False, "bam-profile-v1"),
    (
        "mapping_quality.mean_mapping_quality_phred",
        ELIGIBLE,
        "mapping_quality",
        REAL,
        True,
        False,
        "bam-profile-v1",
    ),
    ("mapping_quality.minimum", ELIGIBLE, "mapping_quality", REAL, True, False, "bam-profile-v1"),
    (
        "mapping_quality.mq0_fraction",
        ELIGIBLE,
        "mapping_quality",
        FRACTION,
        True,
        False,
        "bam-profile-v1",
    ),
    (
        "mapping_quality.mq_lt20_fraction",
        ELIGIBLE,
        "mapping_quality",
        FRACTION,
        True,
        False,
        "bam-profile-v1",
    ),
    (
        "mapping_quality.quantiles",
        ELIGIBLE,
        "mapping_quality",
        CONTAINER,
        False,
        True,
        "bam-profile-v1",
    ),
    (
        "mapping_quality.quantiles.P01",
        ELIGIBLE,
        "mapping_quality",
        REAL,
        True,
        True,
        "bam-profile-v1",
    ),
    (
        "mapping_quality.quantiles.P05",
        ELIGIBLE,
        "mapping_quality",
        REAL,
        True,
        True,
        "bam-profile-v1",
    ),
    (
        "mapping_quality.quantiles.P10",
        ELIGIBLE,
        "mapping_quality",
        REAL,
        True,
        True,
        "bam-profile-v1",
    ),
    (
        "mapping_quality.quantiles.P25",
        ELIGIBLE,
        "mapping_quality",
        REAL,
        True,
        True,
        "bam-profile-v1",
    ),
    (
        "mapping_quality.quantiles.P50",
        ELIGIBLE,
        "mapping_quality",
        REAL,
        True,
        True,
        "bam-profile-v1",
    ),
    (
        "mapping_quality.quantiles.P75",
        ELIGIBLE,
        "mapping_quality",
        REAL,
        True,
        True,
        "bam-profile-v1",
    ),
    (
        "mapping_quality.quantiles.P90",
        ELIGIBLE,
        "mapping_quality",
        REAL,
        True,
        True,
        "bam-profile-v1",
    ),
    (
        "mapping_quality.quantiles.P95",
        ELIGIBLE,
        "mapping_quality",
        REAL,
        True,
        True,
        "bam-profile-v1",
    ),
    (
        "mapping_quality.quantiles.P99",
        ELIGIBLE,
        "mapping_quality",
        REAL,
        True,
        True,
        "bam-profile-v1",
    ),
    ("mapping_quality.stddev", ELIGIBLE, "mapping_quality", REAL, True, False, "bam-profile-v1"),
    (
        "pairing.abnormal_pair_fraction",
        ELIGIBLE,
        "pairing",
        FRACTION,
        True,
        False,
        "bam-profile-v1",
    ),
    ("pairing.eligible_pair_count", CONDITIONAL, "pairing", COUNT, True, False, "bam-profile-v1"),
    ("pairing.insert_size_mad_bp", ELIGIBLE, "pairing", REAL, True, False, "bam-profile-v1"),
    ("pairing.mean_insert_size_bp", ELIGIBLE, "pairing", REAL, True, False, "bam-profile-v1"),
    (
        "pairing.overlapping_mate_fraction",
        ELIGIBLE,
        "pairing",
        FRACTION,
        True,
        False,
        "bam-profile-v1",
    ),
    ("pairing.quantiles_bp", ELIGIBLE, "pairing", CONTAINER, False, True, "bam-profile-v1"),
    ("pairing.quantiles_bp.P01", ELIGIBLE, "pairing", REAL, True, True, "bam-profile-v1"),
    ("pairing.quantiles_bp.P05", ELIGIBLE, "pairing", REAL, True, True, "bam-profile-v1"),
    ("pairing.quantiles_bp.P10", ELIGIBLE, "pairing", REAL, True, True, "bam-profile-v1"),
    ("pairing.quantiles_bp.P25", ELIGIBLE, "pairing", REAL, True, True, "bam-profile-v1"),
    ("pairing.quantiles_bp.P50", ELIGIBLE, "pairing", REAL, True, True, "bam-profile-v1"),
    ("pairing.quantiles_bp.P75", ELIGIBLE, "pairing", REAL, True, True, "bam-profile-v1"),
    ("pairing.quantiles_bp.P90", ELIGIBLE, "pairing", REAL, True, True, "bam-profile-v1"),
    ("pairing.quantiles_bp.P95", ELIGIBLE, "pairing", REAL, True, True, "bam-profile-v1"),
    ("pairing.quantiles_bp.P99", ELIGIBLE, "pairing", REAL, True, True, "bam-profile-v1"),
    ("pairing.stddev_insert_size_bp", ELIGIBLE, "pairing", REAL, True, False, "bam-profile-v1"),
    (
        "pairing.template_length_policy",
        FORBIDDEN,
        "pairing",
        IDENTIFIER,
        False,
        False,
        "bam-profile-v1",
    ),
    ("profile_id", FORBIDDEN, "profile_id", IDENTIFIER, False, False, "bam-profile-v1"),
    ("provenance.config_hash", FORBIDDEN, "provenance", IDENTIFIER, False, False, "bam-profile-v1"),
    (
        "provenance.config_version",
        FORBIDDEN,
        "provenance",
        IDENTIFIER,
        False,
        False,
        "bam-profile-v1",
    ),
    (
        "provenance.profiler_version",
        FORBIDDEN,
        "provenance",
        IDENTIFIER,
        False,
        False,
        "bam-profile-v1",
    ),
    (
        "provenance.pysam_version",
        FORBIDDEN,
        "provenance",
        IDENTIFIER,
        False,
        False,
        "bam-profile-v1",
    ),
    (
        "provenance.schema_version",
        FORBIDDEN,
        "provenance",
        IDENTIFIER,
        False,
        False,
        "bam-profile-v1",
    ),
    ("read_length.count", CONDITIONAL, "read_length", COUNT, True, False, "bam-profile-v1"),
    ("read_length.maximum", ELIGIBLE, "read_length", REAL, True, False, "bam-profile-v1"),
    ("read_length.mean", ELIGIBLE, "read_length", REAL, True, False, "bam-profile-v1"),
    ("read_length.minimum", ELIGIBLE, "read_length", REAL, True, False, "bam-profile-v1"),
    ("read_length.quantiles", ELIGIBLE, "read_length", CONTAINER, False, True, "bam-profile-v1"),
    ("read_length.quantiles.P01", ELIGIBLE, "read_length", REAL, True, True, "bam-profile-v1"),
    ("read_length.quantiles.P05", ELIGIBLE, "read_length", REAL, True, True, "bam-profile-v1"),
    ("read_length.quantiles.P10", ELIGIBLE, "read_length", REAL, True, True, "bam-profile-v1"),
    ("read_length.quantiles.P25", ELIGIBLE, "read_length", REAL, True, True, "bam-profile-v1"),
    ("read_length.quantiles.P50", ELIGIBLE, "read_length", REAL, True, True, "bam-profile-v1"),
    ("read_length.quantiles.P75", ELIGIBLE, "read_length", REAL, True, True, "bam-profile-v1"),
    ("read_length.quantiles.P90", ELIGIBLE, "read_length", REAL, True, True, "bam-profile-v1"),
    ("read_length.quantiles.P95", ELIGIBLE, "read_length", REAL, True, True, "bam-profile-v1"),
    ("read_length.quantiles.P99", ELIGIBLE, "read_length", REAL, True, True, "bam-profile-v1"),
    ("read_length.stddev", ELIGIBLE, "read_length", REAL, True, False, "bam-profile-v1"),
    (
        "read_length.variable_read_length",
        CONDITIONAL,
        "read_length",
        BOOL,
        False,
        False,
        "bam-profile-v1",
    ),
    ("reads.duplicate_fraction", ELIGIBLE, "reads", FRACTION, True, False, "bam-profile-v1"),
    (
        "reads.included_primary_alignments",
        CONDITIONAL,
        "reads",
        COUNT,
        True,
        False,
        "bam-profile-v1",
    ),
    ("reads.mapped_fraction", ELIGIBLE, "reads", FRACTION, True, False, "bam-profile-v1"),
    ("reads.mate_unmapped_fraction", ELIGIBLE, "reads", FRACTION, True, False, "bam-profile-v1"),
    ("reads.paired_fraction", ELIGIBLE, "reads", FRACTION, True, False, "bam-profile-v1"),
    ("reads.proper_pair_fraction", ELIGIBLE, "reads", FRACTION, True, False, "bam-profile-v1"),
    ("reads.qcfail_fraction", ELIGIBLE, "reads", FRACTION, True, False, "bam-profile-v1"),
    ("reads.reverse_strand_fraction", ELIGIBLE, "reads", FRACTION, True, False, "bam-profile-v1"),
    ("reads.secondary_fraction", ELIGIBLE, "reads", FRACTION, True, False, "bam-profile-v1"),
    ("reads.supplementary_fraction", ELIGIBLE, "reads", FRACTION, True, False, "bam-profile-v1"),
    ("reads.total_observed_alignments", CONDITIONAL, "reads", COUNT, True, False, "bam-profile-v1"),
    ("reads.unmapped_fraction", ELIGIBLE, "reads", FRACTION, True, False, "bam-profile-v1"),
    (
        "reference_context.ambiguous_reference_excluded",
        FORBIDDEN,
        "reference_context",
        IDENTIFIER,
        False,
        False,
        "bam-profile-v1",
    ),
    (
        "reference_context.dinucleotide_repeat_fraction",
        ELIGIBLE,
        "reference_context",
        FRACTION,
        True,
        False,
        "bam-profile-v1",
    ),
    (
        "reference_context.entropy_bits",
        ELIGIBLE,
        "reference_context",
        REAL,
        True,
        False,
        "bam-profile-v1",
    ),
    (
        "reference_context.gc_fraction",
        ELIGIBLE,
        "reference_context",
        FRACTION,
        True,
        False,
        "bam-profile-v1",
    ),
    (
        "reference_context.homopolymer_base_fraction",
        ELIGIBLE,
        "reference_context",
        FRACTION,
        True,
        False,
        "bam-profile-v1",
    ),
    (
        "reference_context.homopolymer_length_histogram",
        CONDITIONAL,
        "reference_context",
        CONTAINER,
        False,
        False,
        "bam-profile-v1",
    ),
    (
        "reference_context.n_fraction",
        ELIGIBLE,
        "reference_context",
        FRACTION,
        True,
        False,
        "bam-profile-v1",
    ),
    (
        "reference_context.reference_available",
        FORBIDDEN,
        "reference_context",
        IDENTIFIER,
        False,
        False,
        "bam-profile-v1",
    ),
    ("region.contig", FORBIDDEN, "region", IDENTIFIER, False, False, "bam-profile-v1"),
    ("region.end0_exclusive", FORBIDDEN, "region", REAL, False, False, "bam-profile-v1"),
    ("region.length_bp", FORBIDDEN, "region", REAL, False, False, "bam-profile-v1"),
    ("region.source", FORBIDDEN, "region", IDENTIFIER, False, False, "bam-profile-v1"),
    (
        "region.source_coordinate_system",
        FORBIDDEN,
        "region",
        IDENTIFIER,
        False,
        False,
        "bam-profile-v1",
    ),
    ("region.start0", FORBIDDEN, "region", REAL, False, False, "bam-profile-v1"),
    ("region.verified", FORBIDDEN, "region", IDENTIFIER, False, False, "bam-profile-v1"),
    (
        "runtime_complexity.actual_pileup_seconds",
        FORBIDDEN,
        "runtime_complexity",
        OPERATIONAL,
        False,
        False,
        "bam-profile-v1",
    ),
    (
        "runtime_complexity.chosen_pileup_mode",
        FORBIDDEN,
        "runtime_complexity",
        OPERATIONAL,
        False,
        False,
        "bam-profile-v1",
    ),
    (
        "runtime_complexity.predicted_pileup_seconds",
        FORBIDDEN,
        "runtime_complexity",
        OPERATIONAL,
        False,
        False,
        "bam-profile-v1",
    ),
    ("schema_version", FORBIDDEN, "schema_version", IDENTIFIER, False, False, "bam-profile-v1"),
    ("spatial.analyzed_bases", CONDITIONAL, "spatial", COUNT, True, False, "bam-profile-v1"),
    (
        "spatial.interval_fraction_analyzed",
        CONDITIONAL,
        "spatial",
        FRACTION,
        True,
        False,
        "bam-profile-v1",
    ),
    ("spatial.primary_window_count", CONDITIONAL, "spatial", COUNT, True, False, "bam-profile-v1"),
    ("spatial.refined_window_count", CONDITIONAL, "spatial", COUNT, True, False, "bam-profile-v1"),
    ("spatial.sampled_window_count", CONDITIONAL, "spatial", COUNT, True, False, "bam-profile-v1"),
    ("spatial.sampling_uncertainty", CONDITIONAL, "spatial", REAL, True, False, "bam-profile-v1"),
    (
        "spatial.stratum_window_counts",
        CONDITIONAL,
        "spatial",
        CONTAINER,
        False,
        False,
        "bam-profile-v1",
    ),
    ("stage_timings", FORBIDDEN, "stage_timings", CONTAINER, False, False, "bam-profile-v1"),
    ("status", FORBIDDEN, "status", OPERATIONAL, False, False, "bam-profile-v1"),
    (
        "variant_evidence.allele_fraction_threshold_site_counts",
        CONDITIONAL,
        "variant_evidence",
        CONTAINER,
        False,
        True,
        "bam-profile-v1",
    ),
    (
        "variant_evidence.allele_fraction_threshold_site_counts.af_ge_05",
        CONDITIONAL,
        "variant_evidence",
        COUNT,
        True,
        True,
        "bam-profile-v1",
    ),
    (
        "variant_evidence.allele_fraction_threshold_site_counts.af_ge_10",
        CONDITIONAL,
        "variant_evidence",
        COUNT,
        True,
        True,
        "bam-profile-v1",
    ),
    (
        "variant_evidence.allele_fraction_threshold_site_counts.af_ge_20",
        CONDITIONAL,
        "variant_evidence",
        COUNT,
        True,
        True,
        "bam-profile-v1",
    ),
    (
        "variant_evidence.allele_fraction_threshold_site_counts.af_ge_30",
        CONDITIONAL,
        "variant_evidence",
        COUNT,
        True,
        True,
        "bam-profile-v1",
    ),
    (
        "variant_evidence.allele_fraction_threshold_site_counts.af_ge_40",
        CONDITIONAL,
        "variant_evidence",
        COUNT,
        True,
        True,
        "bam-profile-v1",
    ),
    (
        "variant_evidence.analyzed_callable_bases",
        CONDITIONAL,
        "variant_evidence",
        COUNT,
        True,
        False,
        "bam-profile-v1",
    ),
    (
        "variant_evidence.candidate_deletion_density_per_base",
        ELIGIBLE,
        "variant_evidence",
        REAL,
        True,
        False,
        "bam-profile-v1",
    ),
    (
        "variant_evidence.candidate_insertion_density_per_base",
        ELIGIBLE,
        "variant_evidence",
        REAL,
        True,
        False,
        "bam-profile-v1",
    ),
    (
        "variant_evidence.candidate_snp_density_per_base",
        RESEARCH_ONLY,
        "variant_evidence",
        REAL,
        True,
        False,
        "bam-profile-v1",
    ),
    (
        "variant_evidence.columns_reaching_max_depth",
        CONDITIONAL,
        "variant_evidence",
        COUNT,
        True,
        False,
        "bam-profile-v1",
    ),
    (
        "variant_evidence.eligible_region_bases",
        CONDITIONAL,
        "variant_evidence",
        COUNT,
        True,
        False,
        "bam-profile-v1",
    ),
    (
        "variant_evidence.forward_alt_fraction",
        ELIGIBLE,
        "variant_evidence",
        FRACTION,
        True,
        False,
        "bam-profile-v1",
    ),
    (
        "variant_evidence.low_quality_alt_fraction",
        ELIGIBLE,
        "variant_evidence",
        FRACTION,
        True,
        False,
        "bam-profile-v1",
    ),
    (
        "variant_evidence.max_depth_capped_fraction",
        ELIGIBLE,
        "variant_evidence",
        FRACTION,
        True,
        False,
        "bam-profile-v1",
    ),
    (
        "variant_evidence.mismatch_fraction",
        ELIGIBLE,
        "variant_evidence",
        FRACTION,
        True,
        False,
        "bam-profile-v1",
    ),
    (
        "variant_evidence.reverse_alt_fraction",
        ELIGIBLE,
        "variant_evidence",
        FRACTION,
        True,
        False,
        "bam-profile-v1",
    ),
    (
        "variant_evidence.support_threshold_site_counts",
        CONDITIONAL,
        "variant_evidence",
        CONTAINER,
        False,
        True,
        "bam-profile-v1",
    ),
    (
        "variant_evidence.support_threshold_site_counts.support_ge_10",
        CONDITIONAL,
        "variant_evidence",
        COUNT,
        True,
        True,
        "bam-profile-v1",
    ),
    (
        "variant_evidence.support_threshold_site_counts.support_ge_2",
        CONDITIONAL,
        "variant_evidence",
        COUNT,
        True,
        True,
        "bam-profile-v1",
    ),
    (
        "variant_evidence.support_threshold_site_counts.support_ge_3",
        CONDITIONAL,
        "variant_evidence",
        COUNT,
        True,
        True,
        "bam-profile-v1",
    ),
    (
        "variant_evidence.support_threshold_site_counts.support_ge_5",
        CONDITIONAL,
        "variant_evidence",
        COUNT,
        True,
        True,
        "bam-profile-v1",
    ),
    (
        "variant_evidence.support_threshold_site_counts.support_ge_8",
        CONDITIONAL,
        "variant_evidence",
        COUNT,
        True,
        True,
        "bam-profile-v1",
    ),
    ("warnings", FORBIDDEN, "warnings", CONTAINER, False, False, "bam-profile-v1"),
    ("window.analysis_weight", FORBIDDEN, "window", IDENTIFIER, False, False, "window-profile-v1"),
    ("window.bq_mean_phred", ELIGIBLE, "window", REAL, True, False, "window-profile-v1"),
    (
        "window.candidate_indel_density_per_base",
        ELIGIBLE,
        "window",
        REAL,
        True,
        False,
        "window-profile-v1",
    ),
    (
        "window.candidate_snp_density_per_base",
        RESEARCH_ONLY,
        "window",
        REAL,
        True,
        False,
        "window-profile-v1",
    ),
    ("window.cigar_ins_del_burden", ELIGIBLE, "window", REAL, True, False, "window-profile-v1"),
    ("window.contig", FORBIDDEN, "window", IDENTIFIER, False, False, "window-profile-v1"),
    (
        "window.depth_mean_reads_per_base",
        ELIGIBLE,
        "window",
        REAL,
        True,
        False,
        "window-profile-v1",
    ),
    (
        "window.depth_median_reads_per_base",
        ELIGIBLE,
        "window",
        REAL,
        True,
        False,
        "window-profile-v1",
    ),
    ("window.difficult_flags", FORBIDDEN, "window", CONTAINER, False, False, "window-profile-v1"),
    ("window.duplicate_fraction", ELIGIBLE, "window", FRACTION, True, False, "window-profile-v1"),
    ("window.end0", FORBIDDEN, "window", IDENTIFIER, False, False, "window-profile-v1"),
    ("window.entropy_bits", ELIGIBLE, "window", REAL, True, False, "window-profile-v1"),
    ("window.gc_fraction", ELIGIBLE, "window", FRACTION, True, False, "window-profile-v1"),
    (
        "window.homopolymer_base_fraction",
        ELIGIBLE,
        "window",
        FRACTION,
        True,
        False,
        "window-profile-v1",
    ),
    ("window.length_bp", FORBIDDEN, "window", IDENTIFIER, False, False, "window-profile-v1"),
    ("window.mq_mean_phred", ELIGIBLE, "window", REAL, True, False, "window-profile-v1"),
    ("window.nm_per_aligned_base", ELIGIBLE, "window", REAL, True, False, "window-profile-v1"),
    ("window.profile_id", FORBIDDEN, "window", IDENTIFIER, False, False, "window-profile-v1"),
    ("window.read_count", CONDITIONAL, "window", COUNT, True, False, "window-profile-v1"),
    ("window.sampled", FORBIDDEN, "window", BOOL, False, False, "window-profile-v1"),
    (
        "window.selection_probability",
        FORBIDDEN,
        "window",
        IDENTIFIER,
        False,
        False,
        "window-profile-v1",
    ),
    (
        "window.soft_clipped_read_fraction",
        ELIGIBLE,
        "window",
        FRACTION,
        True,
        False,
        "window-profile-v1",
    ),
    ("window.start0", FORBIDDEN, "window", IDENTIFIER, False, False, "window-profile-v1"),
    ("window.stratum", FORBIDDEN, "window", IDENTIFIER, False, False, "window-profile-v1"),
    ("window.window_id", FORBIDDEN, "window", IDENTIFIER, False, False, "window-profile-v1"),
)


def _build() -> tuple[FeatureRegistryRecord, ...]:
    records: list[FeatureRegistryRecord] = []
    seen: set[str] = set()
    for path, state, family, vk, mf, cb, schema in _RAW:
        if path in seen:
            raise ValueError(f"duplicate feature field path in registry: {path}")
        seen.add(path)
        records.append(
            FeatureRegistryRecord(
                field_path=path,
                state=state,
                family=family,
                value_kind=vk,
                model_feature=mf,
                source_schema=schema,
                source_schema_hash=SOURCE_SCHEMA_HASHES[schema],
                config_bound=cb,
                truth_derived=path in _TRUTH_DERIVED,
            )
        )
    return tuple(sorted(records, key=lambda r: r.field_path))


FEATURE_REGISTRY: tuple[FeatureRegistryRecord, ...] = _build()
_BY_PATH: dict[str, FeatureRegistryRecord] = {r.field_path: r for r in FEATURE_REGISTRY}


def canonical_registry() -> dict[str, object]:
    """Canonical, order-stable serialization bound to the accepted config/schema."""
    return {
        "profiler_config_hash": PROFILER_CONFIG_HASH,
        "layer1_schema_hash": LAYER1_SCHEMA_HASH,
        "records": [
            {
                "field_path": r.field_path,
                "state": r.state.value,
                "family": r.family,
                "value_kind": r.value_kind.value,
                "model_feature": r.model_feature,
                "config_bound": r.config_bound,
                "truth_derived": r.truth_derived,
                "source_schema": r.source_schema,
                "source_schema_hash": r.source_schema_hash,
            }
            for r in FEATURE_REGISTRY
        ],
    }


def registry_hash() -> str:
    return canonical_hash(canonical_registry())


REGISTRY_HASH: str = registry_hash()


def record_for(field_path: str) -> FeatureRegistryRecord | None:
    return _BY_PATH.get(field_path)


def state_for(field_path: str) -> FeatureEligibilityState:
    rec = _BY_PATH.get(field_path)
    if rec is None:
        raise ValueError(f"unknown feature field path: {field_path}")
    return rec.state


def counts_by_state() -> dict[str, int]:
    out = dict.fromkeys((s.value for s in FeatureEligibilityState), 0)
    for r in FEATURE_REGISTRY:
        out[r.state.value] += 1
    return out


def counts_by_value_kind() -> dict[str, int]:
    out = dict.fromkeys((k.value for k in FeatureValueKind), 0)
    for r in FEATURE_REGISTRY:
        out[r.value_kind.value] += 1
    return out


def production_eligible_fields() -> tuple[str, ...]:
    return tuple(r.field_path for r in FEATURE_REGISTRY if r.production_allowed)


def scalar_model_feature_paths() -> tuple[str, ...]:
    return tuple(r.field_path for r in FEATURE_REGISTRY if r.model_feature)


def container_paths() -> tuple[str, ...]:
    return tuple(r.field_path for r in FEATURE_REGISTRY if r.value_kind is CONTAINER)


def assert_production_feature_vector(field_paths: Iterable[str]) -> None:
    """Raise ``ValueError`` unless every path is an ELIGIBLE scalar leaf.

    There is no promotion parameter: CONDITIONAL and RESEARCH_ONLY are always
    rejected in L2-A, so a caller can never authorize them. Containers, unknown
    paths, and duplicates are rejected.
    """
    seen: set[str] = set()
    for fp in field_paths:
        if fp in seen:
            raise ValueError(f"duplicate feature field path: {fp}")
        seen.add(fp)
        rec = _BY_PATH.get(fp)
        if rec is None:
            raise ValueError(f"unknown feature field path rejected: {fp}")
        if rec.value_kind is CONTAINER:
            raise ValueError(f"container path is not a scalar feature: {fp}")
        if rec.state is FORBIDDEN:
            raise ValueError(f"FORBIDDEN field cannot enter a production feature vector: {fp}")
        if rec.state is RESEARCH_ONLY:
            raise ValueError(f"RESEARCH_ONLY field cannot enter production: {fp}")
        if rec.state is CONDITIONAL:
            raise ValueError(f"CONDITIONAL field cannot enter production in L2-A: {fp}")
        if not rec.production_allowed:
            raise ValueError(f"field is not an ELIGIBLE scalar model feature: {fp}")


# Largest integer exactly representable as an IEEE-754 double; COUNT (and any
# integer input) must stay within this range so float storage is lossless.
_MAX_EXACT_INT = 2**53
_SCALAR_KINDS = (FRACTION, REAL, COUNT)


def validate_scalar_value(record: FeatureRegistryRecord, raw: object) -> float:
    """Validate one scalar feature value against its ``value_kind``; return an exact float.

    Policy (documented contract):

      * COUNT input must be a built-in Python ``int``, must not be ``bool``, must be
        non-negative, and must be exactly representable in the
        ``CanonicalFeatureVector`` representation (``0 <= value <= 2**53``). Floats
        (including integral floats like ``1.0``), strings, ``Decimal``, NumPy
        scalars, and ``None`` are rejected.
      * REAL/FRACTION input must be a built-in ``int`` or ``float`` (never ``bool``),
        finite (no NaN/Infinity), and — for integers — exactly representable
        (``abs(value) <= 2**53``). FRACTION must additionally lie in ``[0.0, 1.0]``.

    Non-scalar value kinds (CONTAINER/IDENTIFIER/OPERATIONAL/BOOL/CATEGORICAL) are
    rejected: they are never model features.
    """
    kind = record.value_kind
    fp = record.field_path
    if kind not in _SCALAR_KINDS:
        raise ValueError(f"{kind.value} is not a scalar model feature: {fp}")
    if isinstance(raw, bool):
        raise ValueError(f"bool is not a valid numeric feature value: {fp}")
    if kind is COUNT:
        # Exact built-in int only — reject float/str/Decimal/NumPy/None.
        if type(raw) is not int:
            raise ValueError(f"COUNT feature must be a built-in int (no float/other): {fp}")
        if raw < 0:
            raise ValueError(f"COUNT feature must be non-negative: {fp}")
        if raw > _MAX_EXACT_INT:
            raise ValueError(f"COUNT feature exceeds exactly-representable range (2**53): {fp}")
        return float(raw)
    # REAL or FRACTION — exact built-in int or float only.
    if type(raw) is not int and type(raw) is not float:
        raise ValueError(f"feature value must be a built-in int or float: {fp}")
    if type(raw) is int and abs(raw) > _MAX_EXACT_INT:
        raise ValueError(f"integer feature exceeds exactly-representable range (2**53): {fp}")
    val = float(raw)
    if not math.isfinite(val):
        raise ValueError(f"feature value must be finite (no NaN/Infinity): {fp}")
    if kind is FRACTION and not (0.0 <= val <= 1.0):
        raise ValueError(f"fraction feature out of [0,1]: {fp}")
    return val


def validate_production_feature_mapping(
    features: Mapping[str, object],
) -> CanonicalFeatureVector:
    """Validate a complete production feature mapping and return a canonical vector.

    Accepts ELIGIBLE scalar leaves only. Rejects unknown/container/non-ELIGIBLE
    paths (via :func:`assert_production_feature_vector`) and, per
    :func:`validate_scalar_value`, bool values, NaN/Infinity, wrong types,
    lossy/oversized integers, fractional COUNT values, and out-of-range fractions.
    """
    paths = list(features.keys())
    assert_production_feature_vector(paths)
    ordered = sorted(paths)
    values = [validate_scalar_value(_BY_PATH[fp], features[fp]) for fp in ordered]
    return CanonicalFeatureVector(
        fields=tuple(ordered), values=tuple(values), registry_hash=REGISTRY_HASH
    )
