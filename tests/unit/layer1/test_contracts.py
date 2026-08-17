"""Group A — Layer 1 contract and schema validation."""

from __future__ import annotations

import math

import pytest
from pydantic import ValidationError

from minos_engine.layer1.contracts import (
    AlignmentMetrics,
    ContextFingerprint,
    FieldStatus,
    FilterCounts,
    MappingQualityMetrics,
    ProfileStatus,
    Region,
)
from minos_engine.schema_registry import validate_against


def _alignment(**over):
    base = {
        "total_observed_alignments": 100,
        "included_primary_alignments": 90,
        "mapped_fraction": 0.99,
        "unmapped_fraction": 0.01,
        "duplicate_fraction": 0.05,
        "secondary_fraction": 0.0,
        "supplementary_fraction": 0.0,
        "qcfail_fraction": 0.0,
        "paired_fraction": 1.0,
        "proper_pair_fraction": 0.98,
        "reverse_strand_fraction": 0.5,
        "mate_unmapped_fraction": 0.0,
    }
    base.update(over)
    return base


def test_valid_region_roundtrips_schema():
    r = Region(
        source="chr1:1-100",
        contig="chr1",
        start0=0,
        end0_exclusive=100,
        length_bp=100,
        source_coordinate_system="one_based_inclusive",
        verified=True,
    )
    validate_against("layer1-fingerprint-v1", _fingerprint(r).model_dump(mode="json"))


def _fingerprint(region: Region) -> ContextFingerprint:
    return ContextFingerprint(
        profile_schema_version="bam-profile-v1",
        profiler_algorithm_version="layer1-profiler-v1",
        profiler_config_hash="h",
        bam_sha256="a" * 64,
        index_status=FieldStatus.AVAILABLE,
        reference_status=FieldStatus.AVAILABLE,
        region=region,
        sampling_plan_hash="s",
        read_filter_policy_hash="f",
        completed_families=("reads",),
        degradation_status=ProfileStatus.COMPLETE,
        feature_values_hash="v",
    )


def test_region_rejects_inverted():
    with pytest.raises(ValidationError):
        Region(
            source="x",
            contig="c",
            start0=100,
            end0_exclusive=50,
            length_bp=-50,
            source_coordinate_system="zero_based_half_open",
            verified=True,
        )


def test_region_length_must_match():
    with pytest.raises(ValidationError):
        Region(
            source="x",
            contig="c",
            start0=0,
            end0_exclusive=100,
            length_bp=99,
            source_coordinate_system="zero_based_half_open",
            verified=True,
        )


def test_fraction_out_of_range_rejected():
    with pytest.raises(ValidationError):
        AlignmentMetrics(**_alignment(duplicate_fraction=1.5))


def test_negative_count_rejected():
    with pytest.raises(ValidationError):
        AlignmentMetrics(**_alignment(total_observed_alignments=-1))


def test_nan_rejected_in_distribution():
    with pytest.raises(ValidationError):
        MappingQualityMetrics(
            count=1,
            mean=math.nan,
            stddev=0.0,
            minimum=0.0,
            maximum=1.0,
            quantiles={"P50": 1.0},
            mean_mapping_quality_phred=1.0,
            mq0_fraction=0.0,
            mq_lt20_fraction=0.0,
        )


def test_unordered_quantiles_rejected():
    with pytest.raises(ValidationError):
        MappingQualityMetrics(
            count=2,
            mean=1.0,
            stddev=0.0,
            minimum=0.0,
            maximum=1.0,
            quantiles={"P10": 5.0, "P90": 1.0},
            mean_mapping_quality_phred=1.0,
            mq0_fraction=0.0,
            mq_lt20_fraction=0.0,
        )


def test_unknown_field_rejected():
    with pytest.raises(ValidationError):
        AlignmentMetrics(**_alignment(surprise=1))


def test_filter_counts_consistency_enforced():
    with pytest.raises(ValidationError):
        FilterCounts(
            observed=100,
            included=90,
            excluded_unmapped=1,
            excluded_secondary=1,
            excluded_supplementary=1,
            excluded_duplicate=1,
            excluded_qcfail=1,
            excluded_below_mapq=1,
        )


def test_filter_counts_consistent_accepts():
    fc = FilterCounts(
        observed=100,
        included=94,
        excluded_unmapped=1,
        excluded_secondary=1,
        excluded_supplementary=1,
        excluded_duplicate=1,
        excluded_qcfail=1,
        excluded_below_mapq=1,
    )
    assert fc.included + 6 == fc.observed


_UNIT_REGION = Region(
    source="x",
    contig="c",
    start0=0,
    end0_exclusive=1,
    length_bp=1,
    source_coordinate_system="zero_based_half_open",
    verified=True,
)


def test_malformed_bam_hash_rejected():
    with pytest.raises(ValidationError):
        ContextFingerprint(
            profile_schema_version="bam-profile-v1",
            profiler_algorithm_version="layer1-profiler-v1",
            profiler_config_hash="h",
            bam_sha256="short",
            index_status=FieldStatus.AVAILABLE,
            reference_status=FieldStatus.AVAILABLE,
            region=_UNIT_REGION,
            sampling_plan_hash="s",
            read_filter_policy_hash="f",
            completed_families=("reads",),
            degradation_status=ProfileStatus.COMPLETE,
            feature_values_hash="v",
        )


def test_fingerprint_hash_binds_content():
    fp = _fingerprint(_UNIT_REGION)
    assert fp.fingerprint_hash == fp.compute_hash()
    with pytest.raises(ValidationError):
        ContextFingerprint(
            profile_schema_version="bam-profile-v1",
            profiler_algorithm_version="layer1-profiler-v1",
            profiler_config_hash="h",
            bam_sha256="a" * 64,
            index_status=FieldStatus.AVAILABLE,
            reference_status=FieldStatus.AVAILABLE,
            region=_UNIT_REGION,
            sampling_plan_hash="s",
            read_filter_policy_hash="f",
            completed_families=("reads",),
            degradation_status=ProfileStatus.COMPLETE,
            feature_values_hash="v",
            fingerprint_hash="deadbeef",
        )
