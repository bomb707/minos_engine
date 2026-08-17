"""Layer 1 immutable contracts (Layer 1 spec §1, §2, §16, §17).

Every model is frozen, forbids extra fields, rejects NaN/Infinity and impossible
negatives, uses explicit units in field names, validates rate/fraction fields in
[0,1] and quantile ordering, and records schema versions. Ambiguous names
(``quality``, ``coverage``, ``ratio``, ``score``, ``value``) are avoided in favor
of unit-bearing names (``mean_mapping_quality_phred``, ``callable_base_fraction``,
``mean_depth_reads_per_base``, ...). The measurements are descriptive; nothing
here encodes a GATK recommendation, a truth label, or a score.
"""

from __future__ import annotations

import math
from enum import Enum
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

__all__ = [
    "ProfileStatus",
    "DegradationStage",
    "PileupMode",
    "VerificationStrength",
    "FieldStatus",
    "ProfileRequest",
    "ProfileResult",
    "Region",
    "BamIdentity",
    "HeaderSummary",
    "FilterCounts",
    "AlignmentMetrics",
    "MappingQualityMetrics",
    "BaseQualityMetrics",
    "ReadLengthMetrics",
    "FragmentMetrics",
    "CigarMetrics",
    "CoverageView",
    "CoverageMetrics",
    "VariantEvidenceMetrics",
    "ReferenceContextMetrics",
    "SpatialMetrics",
    "DifficultyVector",
    "RuntimeComplexity",
    "ConfidenceReport",
    "CompletionReport",
    "StageTiming",
    "DegradationRecord",
    "ProfilerProvenance",
    "Unavailable",
    "WindowRow",
    "BamProfile",
    "ContextFingerprint",
    "ProfileManifest",
]

Fraction = Annotated[float, Field(ge=0.0, le=1.0)]
Count = Annotated[int, Field(ge=0)]
NonNegFloat = Annotated[float, Field(ge=0.0)]


def _finite(v: float) -> float:
    if not math.isfinite(v):
        raise ValueError("value must be finite (no NaN/Infinity)")
    return v


def _validate_quantiles_ordered(v: dict[str, float]) -> dict[str, float]:
    """Quantile map keyed by ``P01``..``P99`` must be non-decreasing by percentile."""
    items = sorted((float(k.strip("pP")), val) for k, val in v.items())
    prev = -math.inf
    for _, val in items:
        _finite(val)
        if val < prev:
            raise ValueError("quantiles must be non-decreasing by percentile")
        prev = val
    return v


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class ProfileStatus(str, Enum):
    COMPLETE = "COMPLETE"
    PARTIAL = "PARTIAL"
    FAILED = "FAILED"


class DegradationStage(str, Enum):
    FULL = "FULL"
    REDUCED_PILEUP = "REDUCED_PILEUP"
    REDUCED_WINDOWS = "REDUCED_WINDOWS"
    CORE_ONLY = "CORE_ONLY"


class PileupMode(str, Enum):
    FULL = "FULL"
    ADAPTIVE = "ADAPTIVE"
    SKIPPED = "SKIPPED"


class VerificationStrength(str, Enum):
    CONTENT_HASH_AND_FETCH = "content_hash_and_fetch"
    FETCH_ONLY = "fetch_only"
    DECLARED_DIGEST = "declared_digest"


class FieldStatus(str, Enum):
    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"


class Unavailable(_Frozen):
    """Typed unavailable measurement: ``{value:null, status:'unavailable', reason}``."""

    value: None = None
    status: FieldStatus = FieldStatus.UNAVAILABLE
    reason: str = Field(min_length=1)


# --------------------------------------------------------------------------- #
# Request / result (Layer 1 spec §1)
# --------------------------------------------------------------------------- #
class ProfileRequest(_Frozen):
    round_id: str = Field(min_length=1)
    bam_path: str = Field(min_length=1)
    bai_path: str = Field(min_length=1)
    reference_path: str = Field(min_length=1)
    fai_path: str = Field(min_length=1)
    region_source: str = Field(min_length=1)
    region_coordinate_convention: str = Field(min_length=1)
    budget_seconds: float = Field(gt=0)
    expected_hashes: dict[str, str] | None = None
    cpu_limit: int = Field(ge=1)
    memory_limit_bytes: int = Field(gt=0)
    profiler_config_version: str = Field(min_length=1)
    profiler_config_hash: str = Field(min_length=1)


class ProfileResult(_Frozen):
    status: ProfileStatus
    profile_path: str | None = None
    windows_path: str | None = None
    manifest_path: str | None = None
    failure_code: str | None = None
    fallback_required: bool = False
    warnings: tuple[str, ...] = ()


# --------------------------------------------------------------------------- #
# Region + identity + header
# --------------------------------------------------------------------------- #
class Region(_Frozen):
    source: str = Field(min_length=1)
    contig: str = Field(min_length=1)
    start0: Count
    end0_exclusive: int = Field(gt=0)
    length_bp: int = Field(gt=0)
    source_coordinate_system: str = Field(min_length=1)
    verified: bool

    @model_validator(mode="after")
    def _coords(self) -> Region:
        if not self.start0 < self.end0_exclusive:
            raise ValueError("require start0 < end0_exclusive")
        if self.length_bp != self.end0_exclusive - self.start0:
            raise ValueError("length_bp must equal end0_exclusive - start0")
        return self


class BamIdentity(_Frozen):
    bam_sha256: str = Field(min_length=64, max_length=64)
    bam_size_bytes: int = Field(gt=0)
    header_sha256: str = Field(min_length=64, max_length=64)
    index_status: FieldStatus
    index_sha256: str | None = None
    reference_status: FieldStatus
    reference_sha256: str | None = None
    fai_sha256: str | None = None
    verification_strength: VerificationStrength


class HeaderSummary(_Frozen):
    hd_version: str | None = None
    sort_order: str | None = None
    contigs: tuple[tuple[str, int], ...]
    read_group_ids: tuple[str, ...]
    sample_names: tuple[str, ...]
    library_names: tuple[str, ...]
    platform_values: tuple[str, ...]
    program_ids: tuple[str, ...]
    coordinate_sorted: bool
    has_nm_tag_observed: bool
    has_md_tag_observed: bool


# --------------------------------------------------------------------------- #
# Filtering + alignment families (Layer 1 spec §8–§9)
# --------------------------------------------------------------------------- #
class FilterCounts(_Frozen):
    """Mutually-exclusive read accounting: observed = included + Σ excluded."""

    observed: Count
    included: Count
    excluded_unmapped: Count
    excluded_secondary: Count
    excluded_supplementary: Count
    excluded_duplicate: Count
    excluded_qcfail: Count
    excluded_below_mapq: Count

    @model_validator(mode="after")
    def _consistent(self) -> FilterCounts:
        total_excluded = (
            self.excluded_unmapped
            + self.excluded_secondary
            + self.excluded_supplementary
            + self.excluded_duplicate
            + self.excluded_qcfail
            + self.excluded_below_mapq
        )
        if self.included + total_excluded != self.observed:
            raise ValueError("filter counts inconsistent: observed != included + Σ excluded")
        return self


class AlignmentMetrics(_Frozen):
    total_observed_alignments: Count
    included_primary_alignments: Count
    mapped_fraction: Fraction
    unmapped_fraction: Fraction
    duplicate_fraction: Fraction
    secondary_fraction: Fraction
    supplementary_fraction: Fraction
    qcfail_fraction: Fraction
    paired_fraction: Fraction
    proper_pair_fraction: Fraction
    reverse_strand_fraction: Fraction
    mate_unmapped_fraction: Fraction


class _Distribution(_Frozen):
    """A bounded numeric distribution summary with ordered quantiles."""

    count: Count
    mean: NonNegFloat
    stddev: NonNegFloat
    minimum: NonNegFloat
    maximum: NonNegFloat
    quantiles: dict[str, float]

    @field_validator("mean", "stddev", "minimum", "maximum")
    @classmethod
    def _f(cls, v: float) -> float:
        return _finite(v)

    @field_validator("quantiles")
    @classmethod
    def _q_ordered(cls, v: dict[str, float]) -> dict[str, float]:
        return _validate_quantiles_ordered(v)


class MappingQualityMetrics(_Distribution):
    mean_mapping_quality_phred: NonNegFloat
    mq0_fraction: Fraction
    mq_lt20_fraction: Fraction


class BaseQualityMetrics(_Frozen):
    bases_observed: Count
    bases_with_quality: Count
    mean_base_quality_phred: NonNegFloat
    stddev_base_quality_phred: NonNegFloat
    quantiles_phred: dict[str, float]
    bq_lt20_fraction: Fraction
    missing_quality_fraction: Fraction

    @field_validator("quantiles_phred")
    @classmethod
    def _q(cls, v: dict[str, float]) -> dict[str, float]:
        return _validate_quantiles_ordered(v)


class ReadLengthMetrics(_Distribution):
    variable_read_length: bool


class FragmentMetrics(_Frozen):
    eligible_pair_count: Count
    template_length_policy: str = Field(min_length=1)
    mean_insert_size_bp: NonNegFloat
    stddev_insert_size_bp: NonNegFloat
    insert_size_mad_bp: NonNegFloat
    quantiles_bp: dict[str, float]
    overlapping_mate_fraction: Fraction
    abnormal_pair_fraction: Fraction

    @field_validator("quantiles_bp")
    @classmethod
    def _q(cls, v: dict[str, float]) -> dict[str, float]:
        return _validate_quantiles_ordered(v)


class CigarMetrics(_Frozen):
    aligned_query_bases: Count
    soft_clipped_bases: Count
    hard_clipped_bases: Count
    inserted_bases: Count
    deleted_bases: Count
    skipped_bases: Count
    query_consuming_bases: Count
    soft_clipped_read_fraction: Fraction
    soft_clipped_base_fraction: Fraction
    indel_bearing_read_fraction: Fraction
    nm_per_aligned_base: NonNegFloat
    nm_availability_fraction: Fraction
    cigar_ins_del_burden: NonNegFloat


# --------------------------------------------------------------------------- #
# Coverage (Layer 1 spec §10)
# --------------------------------------------------------------------------- #
class CoverageView(_Frozen):
    view_name: str = Field(min_length=1)
    depth_semantics: str = Field(min_length=1)
    mean_depth_reads_per_base: NonNegFloat
    median_depth_reads_per_base: NonNegFloat
    stddev_depth: NonNegFloat
    coefficient_of_variation: NonNegFloat
    depth_mad: NonNegFloat
    max_depth: Count
    zero_depth_fraction: Fraction
    depth_lt5_fraction: Fraction
    depth_lt10_fraction: Fraction
    depth_lt20_fraction: Fraction
    depth_gt50_fraction: Fraction
    depth_gt100_fraction: Fraction
    depth_gt200_fraction: Fraction
    callable_base_fraction: Fraction
    depth_quantiles: dict[str, float]

    @field_validator(
        "mean_depth_reads_per_base",
        "median_depth_reads_per_base",
        "stddev_depth",
        "coefficient_of_variation",
        "depth_mad",
    )
    @classmethod
    def _f(cls, v: float) -> float:
        return _finite(v)

    @field_validator("depth_quantiles")
    @classmethod
    def _q(cls, v: dict[str, float]) -> dict[str, float]:
        return _validate_quantiles_ordered(v)


class CoverageMetrics(_Frozen):
    fragment_primary: CoverageView
    duplicate_including: CoverageView
    deletion_aware_depth_recorded_separately: bool = True
    eligible_region_bases: Count


# --------------------------------------------------------------------------- #
# Variant evidence (truth-free), reference context, spatial (Layer 1 §11–§13, §15)
# --------------------------------------------------------------------------- #
class VariantEvidenceMetrics(_Frozen):
    analyzed_callable_bases: Count
    eligible_region_bases: Count
    mismatch_fraction: Fraction
    candidate_snp_density_per_base: NonNegFloat
    candidate_insertion_density_per_base: NonNegFloat
    candidate_deletion_density_per_base: NonNegFloat
    support_threshold_site_counts: dict[str, int]
    allele_fraction_threshold_site_counts: dict[str, int]
    forward_alt_fraction: Fraction
    reverse_alt_fraction: Fraction
    low_quality_alt_fraction: Fraction
    columns_reaching_max_depth: Count
    max_depth_capped_fraction: Fraction

    @field_validator(
        "candidate_snp_density_per_base",
        "candidate_insertion_density_per_base",
        "candidate_deletion_density_per_base",
    )
    @classmethod
    def _f(cls, v: float) -> float:
        return _finite(v)


class ReferenceContextMetrics(_Frozen):
    gc_fraction: Fraction
    n_fraction: Fraction
    entropy_bits: NonNegFloat
    homopolymer_base_fraction: Fraction
    homopolymer_length_histogram: dict[str, int]
    dinucleotide_repeat_fraction: Fraction
    ambiguous_reference_excluded: bool
    reference_available: bool

    @field_validator("entropy_bits")
    @classmethod
    def _f(cls, v: float) -> float:
        return _finite(v)


class SpatialMetrics(_Frozen):
    primary_window_count: Count
    refined_window_count: Count
    sampled_window_count: Count
    analyzed_bases: Count
    interval_fraction_analyzed: Fraction
    stratum_window_counts: dict[str, int]
    sampling_uncertainty: NonNegFloat

    @field_validator("sampling_uncertainty")
    @classmethod
    def _f(cls, v: float) -> float:
        return _finite(v)


class DifficultyVector(_Frozen):
    """Descriptive, versioned, monotonic risk transforms. Never a GATK recommendation."""

    transform_version: str = Field(min_length=1)
    mapping_risk: Fraction
    coverage_risk: Fraction
    base_quality_risk: Fraction
    complexity_risk: Fraction
    reference_context_risk: Fraction


class RuntimeComplexity(_Frozen):
    predicted_pileup_seconds: NonNegFloat
    actual_pileup_seconds: NonNegFloat
    chosen_pileup_mode: PileupMode

    @field_validator("predicted_pileup_seconds", "actual_pileup_seconds")
    @classmethod
    def _f(cls, v: float) -> float:
        return _finite(v)


class ConfidenceReport(_Frozen):
    integrity: Fraction
    completeness: Fraction
    availability: Fraction
    consistency: Fraction
    overall: Fraction
    band: str = Field(min_length=1)


class CompletionReport(_Frozen):
    completed_families: tuple[str, ...]
    variant_evidence_completion: Fraction
    status: ProfileStatus


class StageTiming(_Frozen):
    """Operational timing — NOT part of content identity (excluded from fingerprint)."""

    stage: str = Field(min_length=1)
    elapsed_seconds: NonNegFloat

    @field_validator("elapsed_seconds")
    @classmethod
    def _f(cls, v: float) -> float:
        return _finite(v)


class DegradationRecord(_Frozen):
    stage: DegradationStage
    reason: str = Field(min_length=1)
    trigger: str = Field(min_length=1)
    omitted_features: tuple[str, ...]
    completed_features: tuple[str, ...]
    elapsed_seconds: NonNegFloat
    remaining_seconds: float
    usable: bool
    status: ProfileStatus


class ProfilerProvenance(_Frozen):
    profiler_version: str = Field(min_length=1)
    config_version: str = Field(min_length=1)
    config_hash: str = Field(min_length=1)
    pysam_version: str = Field(min_length=1)
    schema_version: str = Field(min_length=1)


# --------------------------------------------------------------------------- #
# Window rows (window-profile-v1) + top-level profile (bam-profile-v1)
# --------------------------------------------------------------------------- #
class WindowRow(_Frozen):
    profile_id: str = Field(min_length=1)
    window_id: int = Field(ge=0)
    contig: str = Field(min_length=1)
    start0: Count
    end0: int = Field(gt=0)
    length_bp: int = Field(gt=0)
    stratum: str = Field(min_length=1)
    read_count: Count
    depth_mean_reads_per_base: NonNegFloat
    depth_median_reads_per_base: NonNegFloat
    mq_mean_phred: NonNegFloat
    bq_mean_phred: NonNegFloat
    duplicate_fraction: Fraction
    soft_clipped_read_fraction: Fraction
    nm_per_aligned_base: NonNegFloat
    cigar_ins_del_burden: NonNegFloat
    gc_fraction: Fraction
    entropy_bits: NonNegFloat
    homopolymer_base_fraction: Fraction
    candidate_snp_density_per_base: NonNegFloat
    candidate_indel_density_per_base: NonNegFloat
    difficult_flags: tuple[str, ...]
    sampled: bool
    selection_probability: Fraction
    analysis_weight: NonNegFloat

    @model_validator(mode="after")
    def _coords(self) -> WindowRow:
        if not self.start0 < self.end0:
            raise ValueError("window requires start0 < end0")
        if self.length_bp != self.end0 - self.start0:
            raise ValueError("window length_bp must equal end0 - start0")
        return self


class BamProfile(_Frozen):
    schema_version: str = "bam-profile-v1"
    profile_id: str = Field(min_length=1)
    status: ProfileStatus
    provenance: ProfilerProvenance
    identity: BamIdentity
    region: Region
    header: HeaderSummary
    filter_counts: FilterCounts
    reads: AlignmentMetrics
    coverage: CoverageMetrics
    mapping_quality: MappingQualityMetrics
    base_quality: BaseQualityMetrics
    read_length: ReadLengthMetrics
    pairing: FragmentMetrics
    alignment: CigarMetrics
    variant_evidence: VariantEvidenceMetrics
    reference_context: ReferenceContextMetrics
    spatial: SpatialMetrics
    difficulty: DifficultyVector
    runtime_complexity: RuntimeComplexity
    confidence: ConfidenceReport
    completion: CompletionReport
    stage_timings: tuple[StageTiming, ...]
    degradation: tuple[DegradationRecord, ...]
    warnings: tuple[str, ...]

    @model_validator(mode="after")
    def _complete_requires_sections(self) -> BamProfile:
        # A COMPLETE status may not carry an unfinished required family set.
        if self.status is ProfileStatus.COMPLETE:
            required = {
                "reads",
                "coverage",
                "mapping_quality",
                "base_quality",
                "pairing",
                "alignment",
                "variant_evidence",
                "reference_context",
            }
            done = set(self.completion.completed_families)
            missing = sorted(required - done)
            if missing:
                raise ValueError(f"COMPLETE profile missing required families: {missing}")
        return self


class ContextFingerprint(_Frozen):
    """Canonical Layer 1 identity — no paths, timestamps, runtime, or host state."""

    schema_version: str = "layer1-fingerprint-v1"
    profile_schema_version: str = Field(min_length=1)
    profiler_algorithm_version: str = Field(min_length=1)
    profiler_config_hash: str = Field(min_length=1)
    bam_sha256: str = Field(min_length=64, max_length=64)
    index_status: FieldStatus
    index_sha256: str | None = None
    reference_status: FieldStatus
    reference_sha256: str | None = None
    region: Region
    sampling_plan_hash: str = Field(min_length=1)
    read_filter_policy_hash: str = Field(min_length=1)
    completed_families: tuple[str, ...]
    degradation_status: ProfileStatus
    feature_values_hash: str = Field(min_length=1)
    fingerprint_hash: str = Field(default="")

    def _content(self) -> dict[str, Any]:
        return self.model_dump(mode="json", exclude={"fingerprint_hash"})

    def compute_hash(self) -> str:
        from minos_engine.common.hashing import canonical_hash

        return canonical_hash(self._content())

    @model_validator(mode="after")
    def _bind(self) -> ContextFingerprint:
        expected = self.compute_hash()
        if self.fingerprint_hash == "":
            object.__setattr__(self, "fingerprint_hash", expected)
        elif self.fingerprint_hash != expected:
            raise ValueError("fingerprint_hash does not match canonical content")
        return self


class ProfileManifest(_Frozen):
    schema_version: str = "profile-manifest-v1"
    profile_id: str = Field(min_length=1)
    created_at: str = Field(min_length=1)
    profiler_version: str = Field(min_length=1)
    profiler_config_hash: str = Field(min_length=1)
    region_contig: str = Field(min_length=1)
    region_start0: Count
    region_end0: int = Field(gt=0)
    status: ProfileStatus
    profile_sha256: str = Field(min_length=64, max_length=64)
    windows_sha256: str = Field(min_length=64, max_length=64)
    windows_row_count: Count
    fingerprint_hash: str = Field(min_length=64, max_length=64)
