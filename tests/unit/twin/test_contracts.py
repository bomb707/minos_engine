"""Test group A — Twin contracts and schema/model parity."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from minos_engine.intake.contracts import Region
from minos_engine.schema_registry import validate_against
from minos_engine.twin.contracts import (
    ComparisonMetrics,
    ScoreInputs,
    TwinExecutionRequest,
    TwinScoreResult,
    VariantClassCounts,
)
from minos_engine.twin.identities import ToolIdentity
from minos_engine.twin.unavailable import AvailabilityStatus, ReasonCode

_H = "a" * 64
REGION = Region.from_source("chr19:13000000-23000000", "one_based_inclusive")


def _request(**over):
    base = {
        "round_id": "R1",
        "region": REGION,
        "requested_config": {"min_pruning": 3},
        "parameter_space_hash": _H,
        "protocol_snapshot_hash": "b" * 64,
        "reference_sha256": "c" * 64,
        "output_uri": "s3://x/out.vcf.gz",
        "budget_seconds": 300.0,
        "gatk_tool": ToolIdentity(name="gatk", version="4.5.0.0"),
        "engine_git_sha": "stage1",
    }
    base.update(over)
    return TwinExecutionRequest(**base)


def test_valid_request_and_schema_parity():
    req = _request()
    validate_against("twin-execution-request-v1", req.model_dump(mode="json"))


def test_unknown_field_rejected():
    with pytest.raises(ValidationError):
        _request(surprise=1)


def test_missing_required_identity_rejected():
    with pytest.raises(ValidationError):
        _request(parameter_space_hash="")


def test_malformed_hash_rejected():
    with pytest.raises(ValidationError):
        _request(reference_sha256="not-a-hash")


def test_budget_must_be_positive():
    with pytest.raises(ValidationError):
        _request(budget_seconds=0)


def _metrics(**over):
    base = {
        "round_id": "R1",
        "region": REGION,
        "reference_sha256": "c" * 64,
        "snp": VariantClassCounts(tp=90, fp=5, fn=10),
        "indel": VariantClassCounts(tp=40, fp=8, fn=12),
        "snp_precision": 0.9,
        "snp_recall": 0.9,
        "snp_f1": 0.9,
        "indel_precision": 0.8,
        "indel_recall": 0.7,
        "indel_f1": 0.75,
        "total_calls": 143,
        "truth_vcf_sha256": "e" * 64,
        "query_vcf_sha256": "f" * 64,
        "tool": ToolIdentity(name="hap.py", version="0.3.14"),
        "raw_result_hash": "d" * 64,
    }
    base.update(over)
    return ComparisonMetrics(**base)


def test_metrics_inconsistent_totals_rejected():
    with pytest.raises(ValidationError):
        _metrics(total_calls=999)


def test_metrics_non_finite_rejected():
    with pytest.raises(ValidationError):
        _metrics(snp_precision=float("nan"))


def test_metrics_rate_out_of_range_rejected():
    with pytest.raises(ValidationError):
        _metrics(snp_recall=1.5)


def test_impossible_negative_count_rejected():
    with pytest.raises(ValidationError):
        VariantClassCounts(tp=-1, fp=0, fn=0)


def _inputs():
    return ScoreInputs(
        round_id="R1",
        snp_f1=0.9,
        indel_f1=0.8,
        mean_recall=0.85,
        total_truth=152,
        total_calls=143,
        fp_total=13,
    )


def test_available_score_requires_components():
    with pytest.raises(ValidationError):
        TwinScoreResult(round_id="R1", status=AvailabilityStatus.AVAILABLE, score_inputs=_inputs())


def test_unavailable_score_requires_reason_and_no_components():
    with pytest.raises(ValidationError):
        TwinScoreResult(
            round_id="R1", status=AvailabilityStatus.UNAVAILABLE, score_inputs=_inputs()
        )
    ok = TwinScoreResult(
        round_id="R1",
        status=AvailabilityStatus.UNAVAILABLE,
        reason_code=ReasonCode.AUTHORITATIVE_SCORER_NOT_AVAILABLE,
        score_inputs=_inputs(),
    )
    assert ok.final_score is None
