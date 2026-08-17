"""Foundational Layer 2 contract validation (L2-A)."""

from __future__ import annotations

import pydantic
import pytest

from minos_engine.layer2.contracts import (
    ArtifactIdentity,
    ComputeLimits,
    ControlMode,
    DatasetPartition,
    DecisionIdentity,
    DecisionRequest,
    DecisionResult,
    FallbackReason,
    FeatureEligibilityState,
    FeatureRegistryRecord,
    FeatureValueKind,
    Layer1ProfileReference,
    ParameterSpaceIdentity,
    PromotionRecord,
    RoundIdentity,
)

H = "a" * 64
SH = "e127d10459068d12d61753f4ddcd4a503a481767aec24c2e5f9ee48655018df6"


def _record(**over) -> FeatureRegistryRecord:
    kwargs = {
        "field_path": "reads.mapped_fraction",
        "state": FeatureEligibilityState.ELIGIBLE,
        "family": "reads",
        "value_kind": FeatureValueKind.FRACTION,
        "model_feature": True,
        "source_schema": "bam-profile-v1",
        "source_schema_hash": SH,
        "config_bound": False,
        "truth_derived": False,
    }
    kwargs.update(over)
    return FeatureRegistryRecord(**kwargs)


def test_artifact_identity_valid_and_frozen():
    a = ArtifactIdentity(uri="s3://bucket/x", sha256=H)
    assert a.sha256 == H
    with pytest.raises(pydantic.ValidationError):
        a.uri = "y"  # type: ignore[misc]


@pytest.mark.parametrize("bad", ["short", "A" * 64, "g" * 64, ""])
def test_artifact_identity_bad_sha_rejected(bad):
    with pytest.raises(pydantic.ValidationError):
        ArtifactIdentity(uri="u", sha256=bad)


def test_extra_fields_forbidden():
    with pytest.raises(pydantic.ValidationError):
        ArtifactIdentity(uri="u", sha256=H, extra=1)  # type: ignore[call-arg]


def test_empty_id_rejected():
    with pytest.raises(pydantic.ValidationError):
        RoundIdentity(round_id="")


def test_parameter_space_is_gatk_only():
    ParameterSpaceIdentity(parameter_space_hash=H)
    with pytest.raises(pydantic.ValidationError):
        ParameterSpaceIdentity(parameter_space_hash=H, caller="deepvariant")


def test_compute_limits_bounds():
    ComputeLimits(
        remaining_seconds=0.0, wall_clock_budget_seconds=1.0, cpu_limit=1, memory_limit_bytes=1
    )
    with pytest.raises(pydantic.ValidationError):
        ComputeLimits(
            remaining_seconds=-1.0,
            wall_clock_budget_seconds=1.0,
            cpu_limit=1,
            memory_limit_bytes=1,
        )
    with pytest.raises(pydantic.ValidationError):
        ComputeLimits(
            remaining_seconds=0.0,
            wall_clock_budget_seconds=1.0,
            cpu_limit=0,
            memory_limit_bytes=1,
        )


def test_compute_limits_rejects_non_finite():
    with pytest.raises(pydantic.ValidationError):
        ComputeLimits(
            remaining_seconds=float("nan"),
            wall_clock_budget_seconds=1.0,
            cpu_limit=1,
            memory_limit_bytes=1,
        )
    with pytest.raises(pydantic.ValidationError):
        ComputeLimits(
            remaining_seconds=float("inf"),
            wall_clock_budget_seconds=1.0,
            cpu_limit=1,
            memory_limit_bytes=1,
        )


def test_decision_identity_requires_sha():
    DecisionIdentity(decision_id="d", config_hash=H, decision_manifest_hash=H)
    with pytest.raises(pydantic.ValidationError):
        DecisionIdentity(decision_id="d", config_hash="x", decision_manifest_hash=H)


def test_profile_reference_all_identities_mandatory():
    with pytest.raises(pydantic.ValidationError):
        Layer1ProfileReference(
            profile_id="p",
            profile_manifest_hash=H,
            fingerprint_hash=H,
            region_hash=H,
            bam_sha256=H,
        )  # type: ignore[call-arg]  # missing bai/reference/fai


def test_feature_record_truth_derived_must_be_forbidden():
    _record(
        field_path="external.truth_vcf",
        state=FeatureEligibilityState.FORBIDDEN,
        family="external",
        value_kind=FeatureValueKind.IDENTIFIER,
        model_feature=False,
        source_schema="external",
        truth_derived=True,
    )
    with pytest.raises(pydantic.ValidationError):
        _record(truth_derived=True)  # ELIGIBLE + truth_derived -> rejected


def test_feature_record_model_feature_invariant():
    elig = _record()
    assert elig.production_allowed
    # A container can never be a model feature.
    with pytest.raises(pydantic.ValidationError):
        _record(value_kind=FeatureValueKind.CONTAINER, model_feature=True)
    # FORBIDDEN scalar is not a model feature.
    with pytest.raises(pydantic.ValidationError):
        _record(state=FeatureEligibilityState.FORBIDDEN, model_feature=True)


def test_feature_record_bad_schema_hash_rejected():
    with pytest.raises(pydantic.ValidationError):
        _record(source_schema_hash="short")


def test_promotion_rejects_test_partition():
    PromotionRecord(
        field_path="x",
        evidence_partition=DatasetPartition.VALIDATION,
        justification="j",
        approved_by="owner",
    )
    with pytest.raises(pydantic.ValidationError):
        PromotionRecord(
            field_path="x",
            evidence_partition=DatasetPartition.TEST,
            justification="j",
            approved_by="owner",
        )


def test_promotion_must_target_eligible():
    with pytest.raises(pydantic.ValidationError):
        PromotionRecord(
            field_path="x",
            to_state=FeatureEligibilityState.CONDITIONAL,
            evidence_partition=DatasetPartition.TRAIN,
            justification="j",
            approved_by="owner",
        )


def test_decision_request_and_result_typed():
    req = DecisionRequest(
        round=RoundIdentity(round_id="r1"),
        profile_ref=Layer1ProfileReference(
            profile_id="p",
            profile_manifest_hash=H,
            fingerprint_hash=H,
            region_hash=H,
            bam_sha256=H,
            bai_sha256=H,
            reference_sha256=H,
            fai_sha256=H,
        ),
        parameter_space=ParameterSpaceIdentity(parameter_space_hash=H),
        safe_baseline=ArtifactIdentity(uri="u", sha256=H),
        controller_version="c1",
        limits=ComputeLimits(
            remaining_seconds=10.0,
            wall_clock_budget_seconds=30.0,
            cpu_limit=2,
            memory_limit_bytes=1024,
        ),
    )
    assert req.requested_mode is ControlMode.SAFE_BASELINE
    res = DecisionResult(
        decision=DecisionIdentity(decision_id="d", config_hash=H, decision_manifest_hash=H),
        mode=ControlMode.SAFE_BASELINE,
        selected_config=ArtifactIdentity(uri="u", sha256=H),
    )
    assert res.fallback_reason is FallbackReason.NONE
