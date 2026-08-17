"""Test group D — comparison ingestion, recomputation, zero denominators."""

from __future__ import annotations

import pytest

from minos_engine.common.errors import ComparisonError
from minos_engine.intake.contracts import Region
from minos_engine.tools.happy import parse_raw_result
from minos_engine.twin.comparison import build_comparison_metrics, recompute_rates
from minos_engine.twin.identities import ToolIdentity

REGION = Region.from_source("chr19:13000000-23000000", "one_based_inclusive")
TOOL = ToolIdentity(name="hap.py", version="0.3.14")


def _build(raw_payload):
    return build_comparison_metrics(
        round_id="R1",
        region=REGION,
        reference_sha256="c" * 64,
        raw=parse_raw_result(raw_payload),
        truth_vcf_sha256="e" * 64,
        query_vcf_sha256="f" * 64,
        tool=TOOL,
        raw_payload=raw_payload,
    )


def test_valid_recomputation():
    cm = _build({"snp": {"tp": 90, "fp": 10, "fn": 10}, "indel": {"tp": 40, "fp": 8, "fn": 12}})
    assert cm.snp_precision == pytest.approx(0.9)
    assert cm.snp_recall == pytest.approx(0.9)
    assert cm.snp_f1 == pytest.approx(0.9)
    assert cm.total_calls == 100 + 48


def test_zero_denominator_deterministic():
    assert recompute_rates(0, 0, 0) == (0.0, 0.0, 0.0)
    cm = _build({"snp": {"tp": 0, "fp": 0, "fn": 0}, "indel": {"tp": 0, "fp": 0, "fn": 0}})
    assert (cm.snp_precision, cm.snp_recall, cm.snp_f1) == (0.0, 0.0, 0.0)
    assert cm.total_calls == 0


def test_missing_class_rejected():
    with pytest.raises(ComparisonError):
        parse_raw_result({"snp": {"tp": 1, "fp": 0, "fn": 0}})


def test_malformed_numeric_rejected():
    with pytest.raises(ComparisonError):
        parse_raw_result(
            {"snp": {"tp": "x", "fp": 0, "fn": 0}, "indel": {"tp": 0, "fp": 0, "fn": 0}}
        )


def test_negative_count_rejected():
    with pytest.raises(ComparisonError):
        parse_raw_result(
            {"snp": {"tp": -1, "fp": 0, "fn": 0}, "indel": {"tp": 0, "fp": 0, "fn": 0}}
        )


def test_bool_not_accepted_as_count():
    with pytest.raises(ComparisonError):
        parse_raw_result(
            {"snp": {"tp": True, "fp": 0, "fn": 0}, "indel": {"tp": 0, "fp": 0, "fn": 0}}
        )


def test_inconsistent_supplied_metric_rejected():
    with pytest.raises(ComparisonError):
        _build(
            {
                "snp": {"tp": 90, "fp": 10, "fn": 10},
                "indel": {"tp": 40, "fp": 8, "fn": 12},
                "supplied": {"snp_precision": 0.5},
            }
        )


def test_consistent_supplied_metric_accepted():
    cm = _build(
        {
            "snp": {"tp": 90, "fp": 10, "fn": 10},
            "indel": {"tp": 40, "fp": 8, "fn": 12},
            "supplied": {"snp_precision": 0.9},
        }
    )
    assert cm.snp_precision == pytest.approx(0.9)


def test_deterministic_normalization():
    payload = {"snp": {"tp": 3, "fp": 1, "fn": 2}, "indel": {"tp": 5, "fp": 5, "fn": 0}}
    a = _build(payload)
    b = _build(payload)
    assert a.content_hash() == b.content_hash()
