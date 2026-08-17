"""Feature-eligibility registry: exhaustive, deterministic, restrictive."""

from __future__ import annotations

import pydantic
import pytest
from pydantic import BaseModel

from minos_engine.layer1.contracts import BamProfile
from minos_engine.layer2 import feature_registry as FR
from minos_engine.layer2.contracts import (
    DatasetPartition,
    FeatureEligibilityState,
    PromotionRecord,
)


def _is_model(t) -> bool:
    try:
        return isinstance(t, type) and issubclass(t, BaseModel)
    except TypeError:
        return False


def _bamprofile_paths() -> set[str]:
    paths: set[str] = set()
    for name, f in BamProfile.model_fields.items():
        ann = f.annotation
        if _is_model(ann):
            for sub, sf in ann.model_fields.items():
                san = sf.annotation
                if _is_model(san):
                    for s2 in san.model_fields:
                        paths.add(f"{name}.{sub}.{s2}")
                else:
                    paths.add(f"{name}.{sub}")
        else:
            paths.add(name)
    return paths


def test_registry_is_exhaustive_over_bamprofile():
    registered = {
        r.field_path for r in FR.FEATURE_REGISTRY if not r.field_path.startswith("external.")
    }
    assert registered == _bamprofile_paths()


def test_external_forbidden_sentinels_present():
    external = {r.field_path for r in FR.FEATURE_REGISTRY if r.field_path.startswith("external.")}
    for expected in ("external.truth_vcf", "external.previous_winning_config", "external.round_id"):
        assert expected in external


def test_deterministic_order_and_stable_hash():
    paths = [r.field_path for r in FR.FEATURE_REGISTRY]
    assert paths == sorted(paths)
    assert FR.registry_hash() == FR.REGISTRY_HASH
    assert FR.registry_hash() == FR.registry_hash()


def test_counts_by_state():
    counts = FR.counts_by_state()
    assert counts["ELIGIBLE"] == 80
    assert counts["CONDITIONAL"] == 56
    assert counts["RESEARCH_ONLY"] == 1
    assert counts["FORBIDDEN"] == 59
    assert sum(counts.values()) == len(FR.FEATURE_REGISTRY)


def test_snp_density_is_research_only():
    assert (
        FR.state_for("variant_evidence.candidate_snp_density_per_base")
        is FeatureEligibilityState.RESEARCH_ONLY
    )


def test_no_eligible_or_conditional_is_truth_derived():
    for r in FR.FEATURE_REGISTRY:
        if r.state is not FeatureEligibilityState.FORBIDDEN:
            assert r.truth_derived is False


def test_unknown_field_rejected():
    with pytest.raises(ValueError):
        FR.state_for("does.not.exist")
    with pytest.raises(ValueError):
        FR.assert_production_feature_vector(["does.not.exist"])


def test_forbidden_field_rejected_in_production():
    with pytest.raises(ValueError):
        FR.assert_production_feature_vector(["region.contig"])


def test_research_only_rejected_in_production():
    with pytest.raises(ValueError):
        FR.assert_production_feature_vector(["variant_evidence.candidate_snp_density_per_base"])


def test_conditional_requires_promotion():
    with pytest.raises(ValueError):
        FR.assert_production_feature_vector(["filter_counts.observed"])
    promo = {
        "filter_counts.observed": PromotionRecord(
            field_path="filter_counts.observed",
            evidence_partition=DatasetPartition.TRAIN,
            justification="cv benefit",
            approved_by="owner",
        )
    }
    FR.assert_production_feature_vector(["filter_counts.observed"], promo)


def test_promotion_via_test_results_impossible():
    # The contract forbids constructing a test-partition promotion at all.
    with pytest.raises(pydantic.ValidationError):
        PromotionRecord(
            field_path="filter_counts.observed",
            evidence_partition=DatasetPartition.TEST,
            justification="x",
            approved_by="owner",
        )


def test_eligible_fields_pass_production():
    elig = FR.production_eligible_fields()
    assert "reads.mapped_fraction" in elig
    FR.assert_production_feature_vector(list(elig))  # no raise


def test_no_duplicate_paths():
    paths = [r.field_path for r in FR.FEATURE_REGISTRY]
    assert len(paths) == len(set(paths))


def test_record_for():
    assert FR.record_for("reads.mapped_fraction").state is FeatureEligibilityState.ELIGIBLE
    assert FR.record_for("nope.nope") is None


def test_promotion_field_path_must_match():
    promo = {
        "filter_counts.observed": PromotionRecord(
            field_path="filter_counts.included",  # mismatched key vs value
            evidence_partition=DatasetPartition.TRAIN,
            justification="j",
            approved_by="owner",
        )
    }
    with pytest.raises(ValueError):
        FR.assert_production_feature_vector(["filter_counts.observed"], promo)
