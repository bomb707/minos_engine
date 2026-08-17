"""Descriptive difficulty vector, confidence, and completion (Layer 1 spec §16).

The difficulty transforms are versioned and monotonic in their inputs and remain
purely descriptive — they never encode a GATK recommendation or a truth signal.
Confidence combines integrity, completeness, availability and consistency as a
weighted geometric mean; completion reports the variant-evidence fraction.
"""

from __future__ import annotations

from .contracts import (
    AlignmentMetrics,
    BaseQualityMetrics,
    CigarMetrics,
    CompletionReport,
    ConfidenceReport,
    CoverageView,
    DifficultyVector,
    MappingQualityMetrics,
    ProfileStatus,
    ReferenceContextMetrics,
)

__all__ = ["difficulty_vector", "confidence_report", "completion_report", "TRANSFORM_VERSION"]

TRANSFORM_VERSION = "layer1-difficulty-v1"


def _clamp(x: float) -> float:
    return 0.0 if x < 0 else 1.0 if x > 1 else x


def difficulty_vector(
    *,
    alignment: AlignmentMetrics,
    mapping_quality: MappingQualityMetrics,
    base_quality: BaseQualityMetrics,
    cigar: CigarMetrics,
    coverage: CoverageView,
    reference: ReferenceContextMetrics,
) -> DifficultyVector:
    mapping_risk = _clamp(
        mapping_quality.mq0_fraction * 2.0
        + mapping_quality.mq_lt20_fraction
        + alignment.supplementary_fraction
    )
    coverage_risk = _clamp(coverage.zero_depth_fraction + 0.5 * coverage.depth_lt10_fraction)
    base_quality_risk = _clamp(
        base_quality.bq_lt20_fraction + base_quality.missing_quality_fraction
    )
    complexity_risk = _clamp(
        cigar.soft_clipped_base_fraction
        + cigar.indel_bearing_read_fraction
        + min(1.0, cigar.nm_per_aligned_base * 10.0)
    )
    reference_context_risk = _clamp(
        reference.n_fraction
        + 0.5 * reference.homopolymer_base_fraction
        + 0.5 * max(0.0, 1.0 - reference.entropy_bits / 2.0)
    )
    return DifficultyVector(
        transform_version=TRANSFORM_VERSION,
        mapping_risk=mapping_risk,
        coverage_risk=coverage_risk,
        base_quality_risk=base_quality_risk,
        complexity_risk=complexity_risk,
        reference_context_risk=reference_context_risk,
    )


def confidence_report(
    *,
    integrity: float,
    completeness: float,
    availability: float,
    consistency: float,
    high_min: float,
    medium_min: float,
) -> ConfidenceReport:
    sections = [_clamp(integrity), _clamp(completeness), _clamp(availability), _clamp(consistency)]
    product = 1.0
    for s in sections:
        product *= s
    overall = product ** (1.0 / len(sections))
    if overall >= high_min:
        band = "HIGH"
    elif overall >= medium_min:
        band = "MEDIUM"
    else:
        band = "LOW"
    return ConfidenceReport(
        integrity=_clamp(integrity),
        completeness=_clamp(completeness),
        availability=_clamp(availability),
        consistency=_clamp(consistency),
        overall=_clamp(overall),
        band=band,
    )


def completion_report(
    completed_families: tuple[str, ...],
    variant_evidence_completion: float,
    status: ProfileStatus,
) -> CompletionReport:
    return CompletionReport(
        completed_families=completed_families,
        variant_evidence_completion=_clamp(variant_evidence_completion),
        status=status,
    )
