"""Parser hardening — every rejection raises the typed ComparisonError."""

from __future__ import annotations

import math

import pytest

from minos_engine.common.errors import ComparisonError
from minos_engine.tools.happy import (
    SUPPLIED_METRIC_ALLOWLIST,
    parse_raw_result,
)

_OK = {"snp": {"tp": 90, "fp": 10, "fn": 10}, "indel": {"tp": 40, "fp": 8, "fn": 12}}


def test_valid_parses():
    rc = parse_raw_result(dict(_OK))
    assert rc.snp["tp"] == 90


@pytest.mark.parametrize(
    "raw",
    [
        {"snp": {"tp": float("nan"), "fp": 0, "fn": 0}, "indel": {"tp": 0, "fp": 0, "fn": 0}},
        {"snp": {"tp": "90", "fp": 0, "fn": 0}, "indel": {"tp": 0, "fp": 0, "fn": 0}},
        {"snp": {"tp": True, "fp": 0, "fn": 0}, "indel": {"tp": 0, "fp": 0, "fn": 0}},
        {"snp": {"tp": 1.5, "fp": 0, "fn": 0}, "indel": {"tp": 0, "fp": 0, "fn": 0}},
        {"snp": {"tp": -1, "fp": 0, "fn": 0}, "indel": {"tp": 0, "fp": 0, "fn": 0}},
        {"snp": {"tp": 1, "fp": 0}, "indel": {"tp": 0, "fp": 0, "fn": 0}},  # missing fn
        {"snp": {"tp": 1, "fp": 0, "fn": 0, "x": 1}, "indel": {"tp": 0, "fp": 0, "fn": 0}},  # extra
        {"snp": {"tp": 1, "fp": 0, "fn": 0}},  # missing indel
    ],
)
def test_count_block_rejections(raw):
    with pytest.raises(ComparisonError):
        parse_raw_result(raw)


def test_unknown_top_level_key_rejected():
    with pytest.raises(ComparisonError):
        parse_raw_result({**_OK, "surprise": 1})


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), -float("inf"), -0.5, "1.0", True])
def test_ti_tv_rejections(bad):
    with pytest.raises(ComparisonError):
        parse_raw_result({**_OK, "ti_tv": bad})


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), -1.0, "x", False])
def test_het_hom_rejections(bad):
    with pytest.raises(ComparisonError):
        parse_raw_result({**_OK, "het_hom": bad})


def test_ti_tv_het_hom_null_ok():
    rc = parse_raw_result({**_OK, "ti_tv": None, "het_hom": None})
    assert rc.ti_tv is None and rc.het_hom is None


def test_unknown_supplied_metric_rejected():
    with pytest.raises(ComparisonError):
        parse_raw_result({**_OK, "supplied": {"weighted_f1": 0.9}})


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), 1.5, -0.1, "0.9", True])
def test_supplied_value_rejections(bad):
    with pytest.raises(ComparisonError):
        parse_raw_result({**_OK, "supplied": {"snp_precision": bad}})


def test_supplied_not_object_rejected():
    with pytest.raises(ComparisonError):
        parse_raw_result({**_OK, "supplied": [1, 2]})


def test_supplied_allowlist_values_accepted():
    supplied = dict.fromkeys(SUPPLIED_METRIC_ALLOWLIST, 0.9)
    # snp_precision recomputes to exactly 0.9 for 90/(90+10); align others loosely
    rc = parse_raw_result({**_OK, "supplied": {"snp_precision": 0.9}})
    assert rc.supplied["snp_precision"] == 0.9
    assert (
        frozenset(
            {"snp_precision", "snp_recall", "snp_f1", "indel_precision", "indel_recall", "indel_f1"}
        )
        == SUPPLIED_METRIC_ALLOWLIST
    )
    assert all(math.isfinite(v) for v in supplied.values())


def test_non_object_input_rejected():
    with pytest.raises(ComparisonError):
        parse_raw_result([1, 2, 3])  # type: ignore[arg-type]


def test_inconsistent_supplied_rejected_by_normalization():
    from minos_engine.intake.contracts import Region
    from minos_engine.twin.comparison import build_comparison_metrics
    from minos_engine.twin.identities import ToolIdentity

    region = Region.from_source("chr19:13000000-23000000", "one_based_inclusive")
    payload = {**_OK, "supplied": {"snp_precision": 0.5}}  # true precision is 0.9
    with pytest.raises(ComparisonError):
        build_comparison_metrics(
            round_id="R1",
            region=region,
            reference_sha256="c" * 64,
            raw=parse_raw_result(payload),
            truth_vcf_sha256="e" * 64,
            query_vcf_sha256="f" * 64,
            tool=ToolIdentity(name="hap.py", version="0.3.14"),
            raw_payload=payload,
        )
