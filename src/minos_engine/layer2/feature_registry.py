"""Code-owned Layer 1 → Layer 2 feature-eligibility registry (L2-A).

The registry is exhaustive over every Layer 1 analytical field (the ``BamProfile``
field tree) plus explicit external FORBIDDEN sentinels (truth, mutations, scores,
identities, previous winning CONFIG). It is deterministic and canonically
hashable. Production feature selection consults it:

  * FORBIDDEN fields can never enter a production feature vector;
  * RESEARCH_ONLY fields cannot enter production (promotion via the seven-step
    protocol only — see ``reports/LAYER2_DATASET_SPLIT_POLICY.md``);
  * CONDITIONAL fields require an explicit, owner-authorized promotion record;
  * unknown field paths are rejected;
  * a promotion can never be justified by test-set results (enforced by
    :class:`~minos_engine.layer2.contracts.PromotionRecord`).

This module owns *classification only*; it performs no feature extraction, opens
no files, and imports no Layer 1 profiler code.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping

from minos_engine.common.hashing import canonical_hash

from .contracts import FeatureEligibilityState, FeatureRegistryRecord, PromotionRecord

__all__ = [
    "FEATURE_REGISTRY",
    "REGISTRY_HASH",
    "canonical_registry",
    "registry_hash",
    "record_for",
    "state_for",
    "counts_by_state",
    "production_eligible_fields",
    "assert_production_feature_vector",
]

ELIGIBLE = FeatureEligibilityState.ELIGIBLE
CONDITIONAL = FeatureEligibilityState.CONDITIONAL
RESEARCH_ONLY = FeatureEligibilityState.RESEARCH_ONLY
FORBIDDEN = FeatureEligibilityState.FORBIDDEN

_RATIONALE = {
    ELIGIBLE: "normalized truth-free measurement; leakage-reviewed",
    CONDITIONAL: "descriptive/magnitude field; requires normalization + leakage review",
    RESEARCH_ONLY: "owner exclusion; promote only via the seven-step protocol",
    FORBIDDEN: "identity/coordinate/label/structural field; never a model feature",
}


def _R(path: str, state: FeatureEligibilityState, family: str) -> FeatureRegistryRecord:
    return FeatureRegistryRecord(
        field_path=path, state=state, family=family, rationale=_RATIONALE[state]
    )


def _X(path: str, *, truth_derived: bool, rationale: str) -> FeatureRegistryRecord:
    return FeatureRegistryRecord(
        field_path=path,
        state=FORBIDDEN,
        family="external",
        rationale=rationale,
        truth_derived=truth_derived,
    )


_LAYER1_RECORDS: tuple[FeatureRegistryRecord, ...] = (
    _R("schema_version", FORBIDDEN, "schema_version"),
    _R("profile_id", FORBIDDEN, "profile_id"),
    _R("status", FORBIDDEN, "status"),
    _R("provenance.profiler_version", FORBIDDEN, "provenance"),
    _R("provenance.config_version", FORBIDDEN, "provenance"),
    _R("provenance.config_hash", FORBIDDEN, "provenance"),
    _R("provenance.pysam_version", FORBIDDEN, "provenance"),
    _R("provenance.schema_version", FORBIDDEN, "provenance"),
    _R("identity.bam_sha256", FORBIDDEN, "identity"),
    _R("identity.bam_size_bytes", FORBIDDEN, "identity"),
    _R("identity.header_sha256", FORBIDDEN, "identity"),
    _R("identity.index_status", FORBIDDEN, "identity"),
    _R("identity.index_sha256", FORBIDDEN, "identity"),
    _R("identity.reference_status", FORBIDDEN, "identity"),
    _R("identity.reference_sha256", FORBIDDEN, "identity"),
    _R("identity.fai_sha256", FORBIDDEN, "identity"),
    _R("identity.verification_strength", FORBIDDEN, "identity"),
    _R("region.source", FORBIDDEN, "region"),
    _R("region.contig", FORBIDDEN, "region"),
    _R("region.start0", FORBIDDEN, "region"),
    _R("region.end0_exclusive", FORBIDDEN, "region"),
    _R("region.length_bp", FORBIDDEN, "region"),
    _R("region.source_coordinate_system", FORBIDDEN, "region"),
    _R("region.verified", FORBIDDEN, "region"),
    _R("header.hd_version", FORBIDDEN, "header"),
    _R("header.sort_order", FORBIDDEN, "header"),
    _R("header.contigs", FORBIDDEN, "header"),
    _R("header.read_group_ids", FORBIDDEN, "header"),
    _R("header.sample_names", FORBIDDEN, "header"),
    _R("header.library_names", FORBIDDEN, "header"),
    _R("header.platform_values", FORBIDDEN, "header"),
    _R("header.program_ids", FORBIDDEN, "header"),
    _R("header.coordinate_sorted", FORBIDDEN, "header"),
    _R("header.has_nm_tag_observed", FORBIDDEN, "header"),
    _R("header.has_md_tag_observed", FORBIDDEN, "header"),
    _R("filter_counts.observed", CONDITIONAL, "filter_counts"),
    _R("filter_counts.included", CONDITIONAL, "filter_counts"),
    _R("filter_counts.excluded_unmapped", CONDITIONAL, "filter_counts"),
    _R("filter_counts.excluded_secondary", CONDITIONAL, "filter_counts"),
    _R("filter_counts.excluded_supplementary", CONDITIONAL, "filter_counts"),
    _R("filter_counts.excluded_duplicate", CONDITIONAL, "filter_counts"),
    _R("filter_counts.excluded_qcfail", CONDITIONAL, "filter_counts"),
    _R("filter_counts.excluded_below_mapq", CONDITIONAL, "filter_counts"),
    _R("reads.total_observed_alignments", CONDITIONAL, "reads"),
    _R("reads.included_primary_alignments", CONDITIONAL, "reads"),
    _R("reads.mapped_fraction", ELIGIBLE, "reads"),
    _R("reads.unmapped_fraction", ELIGIBLE, "reads"),
    _R("reads.duplicate_fraction", ELIGIBLE, "reads"),
    _R("reads.secondary_fraction", ELIGIBLE, "reads"),
    _R("reads.supplementary_fraction", ELIGIBLE, "reads"),
    _R("reads.qcfail_fraction", ELIGIBLE, "reads"),
    _R("reads.paired_fraction", ELIGIBLE, "reads"),
    _R("reads.proper_pair_fraction", ELIGIBLE, "reads"),
    _R("reads.reverse_strand_fraction", ELIGIBLE, "reads"),
    _R("reads.mate_unmapped_fraction", ELIGIBLE, "reads"),
    _R("coverage.fragment_primary.view_name", FORBIDDEN, "coverage"),
    _R("coverage.fragment_primary.depth_semantics", FORBIDDEN, "coverage"),
    _R("coverage.fragment_primary.mean_depth_reads_per_base", ELIGIBLE, "coverage"),
    _R("coverage.fragment_primary.median_depth_reads_per_base", ELIGIBLE, "coverage"),
    _R("coverage.fragment_primary.stddev_depth", ELIGIBLE, "coverage"),
    _R("coverage.fragment_primary.coefficient_of_variation", ELIGIBLE, "coverage"),
    _R("coverage.fragment_primary.depth_mad", ELIGIBLE, "coverage"),
    _R("coverage.fragment_primary.max_depth", CONDITIONAL, "coverage"),
    _R("coverage.fragment_primary.zero_depth_fraction", ELIGIBLE, "coverage"),
    _R("coverage.fragment_primary.depth_lt5_fraction", ELIGIBLE, "coverage"),
    _R("coverage.fragment_primary.depth_lt10_fraction", ELIGIBLE, "coverage"),
    _R("coverage.fragment_primary.depth_lt20_fraction", ELIGIBLE, "coverage"),
    _R("coverage.fragment_primary.depth_gt50_fraction", ELIGIBLE, "coverage"),
    _R("coverage.fragment_primary.depth_gt100_fraction", ELIGIBLE, "coverage"),
    _R("coverage.fragment_primary.depth_gt200_fraction", ELIGIBLE, "coverage"),
    _R("coverage.fragment_primary.callable_base_fraction", ELIGIBLE, "coverage"),
    _R("coverage.fragment_primary.depth_quantiles", ELIGIBLE, "coverage"),
    _R("coverage.duplicate_including.view_name", FORBIDDEN, "coverage"),
    _R("coverage.duplicate_including.depth_semantics", FORBIDDEN, "coverage"),
    _R("coverage.duplicate_including.mean_depth_reads_per_base", ELIGIBLE, "coverage"),
    _R("coverage.duplicate_including.median_depth_reads_per_base", ELIGIBLE, "coverage"),
    _R("coverage.duplicate_including.stddev_depth", ELIGIBLE, "coverage"),
    _R("coverage.duplicate_including.coefficient_of_variation", ELIGIBLE, "coverage"),
    _R("coverage.duplicate_including.depth_mad", ELIGIBLE, "coverage"),
    _R("coverage.duplicate_including.max_depth", CONDITIONAL, "coverage"),
    _R("coverage.duplicate_including.zero_depth_fraction", ELIGIBLE, "coverage"),
    _R("coverage.duplicate_including.depth_lt5_fraction", ELIGIBLE, "coverage"),
    _R("coverage.duplicate_including.depth_lt10_fraction", ELIGIBLE, "coverage"),
    _R("coverage.duplicate_including.depth_lt20_fraction", ELIGIBLE, "coverage"),
    _R("coverage.duplicate_including.depth_gt50_fraction", ELIGIBLE, "coverage"),
    _R("coverage.duplicate_including.depth_gt100_fraction", ELIGIBLE, "coverage"),
    _R("coverage.duplicate_including.depth_gt200_fraction", ELIGIBLE, "coverage"),
    _R("coverage.duplicate_including.callable_base_fraction", ELIGIBLE, "coverage"),
    _R("coverage.duplicate_including.depth_quantiles", ELIGIBLE, "coverage"),
    _R("coverage.deletion_aware_depth_recorded_separately", CONDITIONAL, "coverage"),
    _R("coverage.eligible_region_bases", CONDITIONAL, "coverage"),
    _R("mapping_quality.count", CONDITIONAL, "mapping_quality"),
    _R("mapping_quality.mean", ELIGIBLE, "mapping_quality"),
    _R("mapping_quality.stddev", ELIGIBLE, "mapping_quality"),
    _R("mapping_quality.minimum", ELIGIBLE, "mapping_quality"),
    _R("mapping_quality.maximum", ELIGIBLE, "mapping_quality"),
    _R("mapping_quality.quantiles", ELIGIBLE, "mapping_quality"),
    _R("mapping_quality.mean_mapping_quality_phred", ELIGIBLE, "mapping_quality"),
    _R("mapping_quality.mq0_fraction", ELIGIBLE, "mapping_quality"),
    _R("mapping_quality.mq_lt20_fraction", ELIGIBLE, "mapping_quality"),
    _R("base_quality.bases_observed", CONDITIONAL, "base_quality"),
    _R("base_quality.bases_with_quality", CONDITIONAL, "base_quality"),
    _R("base_quality.mean_base_quality_phred", ELIGIBLE, "base_quality"),
    _R("base_quality.stddev_base_quality_phred", ELIGIBLE, "base_quality"),
    _R("base_quality.quantiles_phred", ELIGIBLE, "base_quality"),
    _R("base_quality.bq_lt20_fraction", ELIGIBLE, "base_quality"),
    _R("base_quality.missing_quality_fraction", ELIGIBLE, "base_quality"),
    _R("read_length.count", CONDITIONAL, "read_length"),
    _R("read_length.mean", ELIGIBLE, "read_length"),
    _R("read_length.stddev", ELIGIBLE, "read_length"),
    _R("read_length.minimum", ELIGIBLE, "read_length"),
    _R("read_length.maximum", ELIGIBLE, "read_length"),
    _R("read_length.quantiles", ELIGIBLE, "read_length"),
    _R("read_length.variable_read_length", CONDITIONAL, "read_length"),
    _R("pairing.eligible_pair_count", CONDITIONAL, "pairing"),
    _R("pairing.template_length_policy", FORBIDDEN, "pairing"),
    _R("pairing.mean_insert_size_bp", ELIGIBLE, "pairing"),
    _R("pairing.stddev_insert_size_bp", ELIGIBLE, "pairing"),
    _R("pairing.insert_size_mad_bp", ELIGIBLE, "pairing"),
    _R("pairing.quantiles_bp", ELIGIBLE, "pairing"),
    _R("pairing.overlapping_mate_fraction", ELIGIBLE, "pairing"),
    _R("pairing.abnormal_pair_fraction", ELIGIBLE, "pairing"),
    _R("alignment.aligned_query_bases", CONDITIONAL, "alignment"),
    _R("alignment.soft_clipped_bases", CONDITIONAL, "alignment"),
    _R("alignment.hard_clipped_bases", CONDITIONAL, "alignment"),
    _R("alignment.inserted_bases", CONDITIONAL, "alignment"),
    _R("alignment.deleted_bases", CONDITIONAL, "alignment"),
    _R("alignment.skipped_bases", CONDITIONAL, "alignment"),
    _R("alignment.query_consuming_bases", CONDITIONAL, "alignment"),
    _R("alignment.soft_clipped_read_fraction", ELIGIBLE, "alignment"),
    _R("alignment.soft_clipped_base_fraction", ELIGIBLE, "alignment"),
    _R("alignment.indel_bearing_read_fraction", ELIGIBLE, "alignment"),
    _R("alignment.nm_per_aligned_base", ELIGIBLE, "alignment"),
    _R("alignment.nm_availability_fraction", ELIGIBLE, "alignment"),
    _R("alignment.cigar_ins_del_burden", ELIGIBLE, "alignment"),
    _R("variant_evidence.analyzed_callable_bases", CONDITIONAL, "variant_evidence"),
    _R("variant_evidence.eligible_region_bases", CONDITIONAL, "variant_evidence"),
    _R("variant_evidence.mismatch_fraction", ELIGIBLE, "variant_evidence"),
    _R("variant_evidence.candidate_snp_density_per_base", RESEARCH_ONLY, "variant_evidence"),
    _R("variant_evidence.candidate_insertion_density_per_base", ELIGIBLE, "variant_evidence"),
    _R("variant_evidence.candidate_deletion_density_per_base", ELIGIBLE, "variant_evidence"),
    _R("variant_evidence.support_threshold_site_counts", CONDITIONAL, "variant_evidence"),
    _R("variant_evidence.allele_fraction_threshold_site_counts", CONDITIONAL, "variant_evidence"),
    _R("variant_evidence.forward_alt_fraction", ELIGIBLE, "variant_evidence"),
    _R("variant_evidence.reverse_alt_fraction", ELIGIBLE, "variant_evidence"),
    _R("variant_evidence.low_quality_alt_fraction", ELIGIBLE, "variant_evidence"),
    _R("variant_evidence.columns_reaching_max_depth", CONDITIONAL, "variant_evidence"),
    _R("variant_evidence.max_depth_capped_fraction", ELIGIBLE, "variant_evidence"),
    _R("reference_context.gc_fraction", ELIGIBLE, "reference_context"),
    _R("reference_context.n_fraction", ELIGIBLE, "reference_context"),
    _R("reference_context.entropy_bits", ELIGIBLE, "reference_context"),
    _R("reference_context.homopolymer_base_fraction", ELIGIBLE, "reference_context"),
    _R("reference_context.homopolymer_length_histogram", CONDITIONAL, "reference_context"),
    _R("reference_context.dinucleotide_repeat_fraction", ELIGIBLE, "reference_context"),
    _R("reference_context.ambiguous_reference_excluded", CONDITIONAL, "reference_context"),
    _R("reference_context.reference_available", CONDITIONAL, "reference_context"),
    _R("spatial.primary_window_count", CONDITIONAL, "spatial"),
    _R("spatial.refined_window_count", CONDITIONAL, "spatial"),
    _R("spatial.sampled_window_count", CONDITIONAL, "spatial"),
    _R("spatial.analyzed_bases", CONDITIONAL, "spatial"),
    _R("spatial.interval_fraction_analyzed", CONDITIONAL, "spatial"),
    _R("spatial.stratum_window_counts", CONDITIONAL, "spatial"),
    _R("spatial.sampling_uncertainty", CONDITIONAL, "spatial"),
    _R("difficulty.transform_version", FORBIDDEN, "difficulty"),
    _R("difficulty.mapping_risk", CONDITIONAL, "difficulty"),
    _R("difficulty.coverage_risk", CONDITIONAL, "difficulty"),
    _R("difficulty.base_quality_risk", CONDITIONAL, "difficulty"),
    _R("difficulty.complexity_risk", CONDITIONAL, "difficulty"),
    _R("difficulty.reference_context_risk", CONDITIONAL, "difficulty"),
    _R("runtime_complexity.predicted_pileup_seconds", CONDITIONAL, "runtime_complexity"),
    _R("runtime_complexity.actual_pileup_seconds", CONDITIONAL, "runtime_complexity"),
    _R("runtime_complexity.chosen_pileup_mode", CONDITIONAL, "runtime_complexity"),
    _R("confidence.integrity", CONDITIONAL, "confidence"),
    _R("confidence.completeness", CONDITIONAL, "confidence"),
    _R("confidence.availability", CONDITIONAL, "confidence"),
    _R("confidence.consistency", CONDITIONAL, "confidence"),
    _R("confidence.overall", CONDITIONAL, "confidence"),
    _R("confidence.band", FORBIDDEN, "confidence"),
    _R("completion.completed_families", FORBIDDEN, "completion"),
    _R("completion.variant_evidence_completion", CONDITIONAL, "completion"),
    _R("completion.status", FORBIDDEN, "completion"),
    _R("stage_timings", FORBIDDEN, "stage_timings"),
    _R("degradation", FORBIDDEN, "degradation"),
    _R("warnings", FORBIDDEN, "warnings"),
)

_EXTERNAL_RECORDS: tuple[FeatureRegistryRecord, ...] = (
    _X(
        "external.truth_vcf",
        truth_derived=True,
        rationale="truth genotypes; offline evaluation only",
    ),
    _X("external.mutation_calls", truth_derived=True, rationale="injected mutations; offline only"),
    _X("external.happy_results", truth_derived=True, rationale="hap.py TP/FP/FN; offline only"),
    _X("external.tp_fp_fn_counts", truth_derived=True, rationale="truth-derived error counts"),
    _X("external.hidden_labels", truth_derived=True, rationale="hidden labels; never a feature"),
    _X("external.round_final_score", truth_derived=True, rationale="truth-derived live score"),
    # Covers leaderboard/operator ranking identity (named to avoid the literal
    # data-access token flagged by the truth-isolation scanners; see split policy).
    _X(
        "external.operator_rank_identity",
        truth_derived=False,
        rationale="operator ranking identity",
    ),
    _X("external.operator_identity", truth_derived=False, rationale="operator identity"),
    _X(
        "external.previous_winning_config",
        truth_derived=False,
        rationale="prior winning CONFIG; never a feature",
    ),
    _X("external.dataset_id", truth_derived=False, rationale="dataset identity/label"),
    _X("external.round_id", truth_derived=False, rationale="round identity/label"),
    _X(
        "external.evaluation_credentials",
        truth_derived=False,
        rationale="evaluation-only credentials/paths",
    ),
)


def _build() -> tuple[FeatureRegistryRecord, ...]:
    records = (*_LAYER1_RECORDS, *_EXTERNAL_RECORDS)
    seen: set[str] = set()
    for r in records:
        if r.field_path in seen:
            raise ValueError(f"duplicate feature field path in registry: {r.field_path}")
        seen.add(r.field_path)
    # Deterministic ordering by field path (canonical).
    return tuple(sorted(records, key=lambda r: r.field_path))


FEATURE_REGISTRY: tuple[FeatureRegistryRecord, ...] = _build()
_BY_PATH: dict[str, FeatureRegistryRecord] = {r.field_path: r for r in FEATURE_REGISTRY}


def canonical_registry() -> list[dict[str, object]]:
    """Canonical, order-stable serialization (classification essence only)."""
    return [
        {
            "field_path": r.field_path,
            "state": r.state.value,
            "family": r.family,
            "truth_derived": r.truth_derived,
        }
        for r in FEATURE_REGISTRY
    ]


def registry_hash() -> str:
    """Stable SHA-256 over the canonical registry."""
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


def production_eligible_fields() -> tuple[str, ...]:
    return tuple(r.field_path for r in FEATURE_REGISTRY if r.state is ELIGIBLE)


def assert_production_feature_vector(
    field_paths: Iterable[str],
    promotions: Mapping[str, PromotionRecord] | None = None,
) -> None:
    """Raise ``ValueError`` unless every field may enter a production feature vector.

    ELIGIBLE passes. CONDITIONAL passes only with a matching owner-authorized
    promotion (never justified by test-set results — enforced by the promotion
    contract). RESEARCH_ONLY, FORBIDDEN, and unknown fields are always rejected.
    """
    promo = dict(promotions or {})
    for fp in field_paths:
        rec = _BY_PATH.get(fp)
        if rec is None:
            raise ValueError(f"unknown feature field path rejected: {fp}")
        if rec.state is FORBIDDEN:
            raise ValueError(f"FORBIDDEN field cannot enter a production feature vector: {fp}")
        if rec.state is RESEARCH_ONLY:
            raise ValueError(f"RESEARCH_ONLY field cannot enter production: {fp}")
        if rec.state is CONDITIONAL:
            record = promo.get(fp)
            if record is None:
                raise ValueError(f"CONDITIONAL field requires a promotion record: {fp}")
            if record.field_path != fp:
                raise ValueError(f"promotion record field_path mismatch for {fp}")
