"""Test group E — scoring inputs (authoritative) and honest unavailable score."""

from __future__ import annotations

import pytest

from minos_engine.intake.contracts import Region
from minos_engine.tools.happy import parse_raw_result
from minos_engine.twin.comparison import build_comparison_metrics
from minos_engine.twin.identities import ToolIdentity
from minos_engine.twin.scoring import build_score_inputs, compute_score
from minos_engine.twin.unavailable import AvailabilityStatus, ReasonCode

REGION = Region.from_source("chr19:13000000-23000000", "one_based_inclusive")


def _metrics(snp=(90, 10, 10), indel=(40, 8, 12)):
    payload = {
        "snp": {"tp": snp[0], "fp": snp[1], "fn": snp[2]},
        "indel": {"tp": indel[0], "fp": indel[1], "fn": indel[2]},
    }
    return build_comparison_metrics(
        round_id="R1",
        region=REGION,
        reference_sha256="c" * 64,
        raw=parse_raw_result(payload),
        truth_vcf_sha256="e" * 64,
        query_vcf_sha256="f" * 64,
        tool=ToolIdentity(name="hap.py", version="0.3.14"),
        raw_payload=payload,
    )


def test_score_inputs_authoritative():
    si = build_score_inputs(_metrics())
    assert si.mean_recall == pytest.approx((0.9 + 40 / 52) / 2)
    assert si.total_truth == (90 + 10) + (40 + 12)
    assert si.fp_total == 10 + 8
    assert si.total_calls == 100 + 48


def test_score_inputs_bounds():
    si = build_score_inputs(_metrics(snp=(0, 0, 0), indel=(0, 0, 0)))
    assert 0.0 <= si.snp_f1 <= 1.0
    assert si.total_truth == 0


def test_composite_score_is_unavailable_and_honest():
    result = compute_score(build_score_inputs(_metrics()))
    assert result.status is AvailabilityStatus.UNAVAILABLE
    assert result.reason_code is ReasonCode.AUTHORITATIVE_SCORER_NOT_AVAILABLE
    assert result.final_score is None
    assert result.components is None
    assert result.scorer_identity is None


def test_no_invented_fallback_score():
    # Even with a perfect comparison, no numeric score is fabricated.
    result = compute_score(build_score_inputs(_metrics(snp=(100, 0, 0), indel=(100, 0, 0))))
    assert result.final_score is None
