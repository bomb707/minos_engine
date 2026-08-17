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
    Layer1ProfileReference,
    ParameterSpaceIdentity,
    PromotionRecord,
    RoundIdentity,
)

H = "a" * 64


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


def test_profile_reference_optional_hashes():
    ref = Layer1ProfileReference(
        profile_id="p",
        profile_manifest_hash=H,
        fingerprint_hash=H,
        region_hash=H,
        bam_sha256=H,
    )
    assert ref.bai_sha256 is None
    with pytest.raises(pydantic.ValidationError):
        Layer1ProfileReference(
            profile_id="p",
            profile_manifest_hash=H,
            fingerprint_hash=H,
            region_hash=H,
            bam_sha256=H,
            reference_sha256="bad",
        )


def test_feature_record_truth_derived_must_be_forbidden():
    FeatureRegistryRecord(
        field_path="external.truth_vcf",
        state=FeatureEligibilityState.FORBIDDEN,
        family="external",
        rationale="truth",
        truth_derived=True,
    )
    with pytest.raises(pydantic.ValidationError):
        FeatureRegistryRecord(
            field_path="x.y",
            state=FeatureEligibilityState.ELIGIBLE,
            family="x",
            rationale="r",
            truth_derived=True,
        )


def test_feature_record_derived_properties():
    elig = FeatureRegistryRecord(
        field_path="a", state=FeatureEligibilityState.ELIGIBLE, family="a", rationale="r"
    )
    cond = FeatureRegistryRecord(
        field_path="b", state=FeatureEligibilityState.CONDITIONAL, family="b", rationale="r"
    )
    assert elig.production_allowed and not elig.requires_promotion
    assert cond.requires_promotion and not cond.production_allowed


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
