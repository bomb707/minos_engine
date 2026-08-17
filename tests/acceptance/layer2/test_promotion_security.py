"""A caller-constructed object can never authorize a non-ELIGIBLE feature (L2-A)."""

from __future__ import annotations

import contextlib
import inspect

import pydantic
import pytest

from minos_engine.layer2 import feature_registry as FR
from minos_engine.layer2.contracts import DatasetPartition, FeatureEligibilityState, PromotionRecord

_CONDITIONAL = "filter_counts.observed"
_RESEARCH = "variant_evidence.candidate_snp_density_per_base"


def test_production_api_has_no_promotion_parameter():
    sig = inspect.signature(FR.assert_production_feature_vector)
    assert list(sig.parameters) == ["field_paths"]
    sig2 = inspect.signature(FR.validate_production_feature_mapping)
    assert list(sig2.parameters) == ["features"]


def test_conditional_always_rejected_regardless_of_intent():
    with pytest.raises(ValueError):
        FR.assert_production_feature_vector([_CONDITIONAL])
    with pytest.raises(ValueError):
        FR.validate_production_feature_mapping({_CONDITIONAL: 1})


def test_research_only_always_rejected():
    with pytest.raises(ValueError):
        FR.assert_production_feature_vector([_RESEARCH])


def test_arbitrary_promotion_record_cannot_authorize():
    # Even a well-formed PromotionRecord has no path into the authorization API.
    promo = PromotionRecord(
        field_path=_CONDITIONAL,
        evidence_partition=DatasetPartition.VALIDATION,
        justification="looks legit",
        approved_by="anybody",
    )
    assert promo.to_state is FeatureEligibilityState.ELIGIBLE
    # There is no argument to pass it to; the feature stays rejected.
    with pytest.raises(TypeError):
        FR.assert_production_feature_vector([_CONDITIONAL], {_CONDITIONAL: promo})  # type: ignore[call-arg]
    with pytest.raises(ValueError):
        FR.assert_production_feature_vector([_CONDITIONAL])


def test_arbitrary_approved_by_and_justification_are_inert():
    for who, why in (("root", "x"), ("owner", "cv benefit"), ("", "")):
        with contextlib.suppress(pydantic.ValidationError):
            PromotionRecord(
                field_path=_CONDITIONAL,
                evidence_partition=DatasetPartition.TRAIN,
                justification=why or "y",
                approved_by=who or "z",
            )
        # No constructed record changes production authorization.
        with pytest.raises(ValueError):
            FR.assert_production_feature_vector([_CONDITIONAL])


def test_test_partition_evidence_cannot_be_constructed():
    with pytest.raises(pydantic.ValidationError):
        PromotionRecord(
            field_path=_CONDITIONAL,
            evidence_partition=DatasetPartition.TEST,
            justification="j",
            approved_by="owner",
        )
