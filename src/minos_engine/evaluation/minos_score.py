"""Minimal, faithful reimplementation of the Minos SN107 score, through to the validator.

**NOT PRODUCTION SCORE AUTHORITY.** This module is retained for historical tests, independent
diagnostics and parity audits only. Production scoring executes the pinned MINOS_SUBNET
implementation through :mod:`minos_engine.evaluation.minos_subnet_oracle`; nothing on the
production evaluation path imports this module, and a regression test enforces that. Do not
reintroduce it into the orchestrator, the evaluator or any selection decision.

Scope discipline: this is a *compatibility layer*, not a copy of the upstream repository. It
implements exactly the path a miner's VCF takes after hap.py has produced metrics —

    parsed metrics -> AdvancedScorer components -> score_100 -> /100 -> admission

— and nothing else. Parity against the real pinned upstream ``AdvancedScorer`` is proven by a
committed golden fixture generated from the audited upstream checkout; CI never needs that
checkout.

Two upstream behaviours are easy to get wrong and are reproduced deliberately:

* ``AdvancedScorer`` returns **0-100**; the validator divides by 100 and only then admits.
* a result the validator would SKIP is not a zero score. ``0.0`` itself is skipped, and an
  all-zero-metrics dict lands on ~0.25 through the non-core components, which upstream rejects
  by fingerprint. Both are distinct admission outcomes here, never silently scored.
"""

from __future__ import annotations

import math
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from minos_engine.common.errors import MinosEngineError
from minos_engine.evaluation.scoring_contract import AdmissionCode

__all__ = [
    "AdvancedScoreBreakdown",
    "ScoreComputationError",
    "compute_advanced_score",
    "decide_admission",
    "emphasis",
    "ratio_penalty",
]

_STRICT = ConfigDict(extra="forbid", frozen=True, strict=True)


class ScoreComputationError(MinosEngineError):
    """The scorer produced something the validator could not have used at all.

    This is an EVALUATION FAILURE, not a low score: non-finite output means the pipeline
    malfunctioned, and recording it as a scientific result would corrupt the baseline.
    """


class AdvancedScoreBreakdown(BaseModel):
    """The four weighted components, the penalty, and both score scales."""

    model_config = _STRICT

    core_score: float = Field(ge=0.0, le=1.0)
    completeness_score: float = Field(ge=0.0, le=1.0)
    fp_score: float = Field(ge=0.0, le=1.0)
    quality_score: float = Field(ge=0.0, le=1.0)
    overcall_penalty: float = Field(ge=0.0)
    minos_score_100: float = Field(ge=0.0, le=100.0)
    minos_score: float = Field(ge=0.0, le=1.0)


def emphasis(metric: float, gamma: float = 3.0) -> float:
    """Upstream nonlinear emphasis, including its exact 0.999999 clamp."""
    metric = max(0.0, min(metric, 0.999999))
    return float(1.0 - (1.0 - metric) ** gamma)


def ratio_penalty(delta: float, tolerance: float) -> float:
    """Upstream exponential penalty for a ratio deviation."""
    return math.exp(-abs(delta) / tolerance)


def _number(metrics: dict[str, Any], key: str) -> float:
    raw = metrics.get(key, 0)
    if raw is None:
        return 0.0
    try:
        value = float(raw)
    except (TypeError, ValueError) as exc:
        raise ScoreComputationError(f"metric {key!r} is not numeric: {raw!r}") from exc
    if not math.isfinite(value):
        raise ScoreComputationError(f"metric {key!r} is not finite: {raw!r}")
    return value


def compute_advanced_score(metrics: dict[str, Any]) -> AdvancedScoreBreakdown:
    """Reproduce ``AdvancedScorer.compute_advanced_score`` and the validator's ``/100``.

    Weights are 60/15/15/10 over core / completeness / FP-rate / quality, then the overcall
    penalty is subtracted and the result floored at zero.
    """
    f1_snp = _number(metrics, "f1_snp")
    f1_indel = _number(metrics, "f1_indel")
    recall_snp = _number(metrics, "recall_snp")
    recall_indel = _number(metrics, "recall_indel")
    truth_total_snp = _number(metrics, "truth_total_snp")
    truth_total_indel = _number(metrics, "truth_total_indel")
    query_total_snp = _number(metrics, "query_total_snp")
    query_total_indel = _number(metrics, "query_total_indel")
    fp_snp = _number(metrics, "fp_snp")
    fp_indel = _number(metrics, "fp_indel")
    frac_na_snp = _number(metrics, "frac_na_snp")
    frac_na_indel = _number(metrics, "frac_na_indel")
    titv_truth_snp = _number(metrics, "titv_truth_snp")
    titv_query_snp = _number(metrics, "titv_query_snp")
    hethom_truth_snp = _number(metrics, "hethom_truth_snp")
    hethom_query_snp = _number(metrics, "hethom_query_snp")
    hethom_truth_indel = _number(metrics, "hethom_truth_indel")
    hethom_query_indel = _number(metrics, "hethom_query_indel")
    overcall_penalty = _number(metrics, "overcall_penalty")

    total_truth = truth_total_snp + truth_total_indel
    if total_truth <= 0:
        # upstream logs an error and returns 0.0 outright; there is nothing to score against.
        return AdvancedScoreBreakdown(
            core_score=0.0,
            completeness_score=0.0,
            fp_score=0.0,
            quality_score=0.0,
            overcall_penalty=overcall_penalty,
            minos_score_100=0.0,
            minos_score=0.0,
        )

    weighted_f1 = (f1_snp * truth_total_snp + f1_indel * truth_total_indel) / total_truth
    core_component = emphasis(weighted_f1, gamma=0.5)

    avg_recall = (recall_snp + recall_indel) / 2
    coverage = 1.0 - max(frac_na_snp, frac_na_indel)
    completeness_component = (emphasis(avg_recall, gamma=3.0) + emphasis(coverage, gamma=2.0)) / 2.0

    total_fp = fp_snp + fp_indel
    total_calls = query_total_snp + query_total_indel
    fp_rate = total_fp / max(total_calls, 1.0)
    size_ratio = total_calls / max(total_truth, 1.0)
    target_fp = max(0.002, 1.0 / max(total_truth, 1.0))
    fp_pen = math.exp(-max(0.0, fp_rate - target_fp) / target_fp)
    size_pen = math.exp(-abs(size_ratio - 1.0) / 0.10)
    fp_component = (fp_pen + size_pen) / 2.0

    titv_penalties: list[float] = []
    hethom_penalties: list[float] = []
    if titv_truth_snp > 0 and titv_query_snp > 0:
        titv_penalties.append(ratio_penalty(titv_query_snp - titv_truth_snp, 0.1))
    if hethom_truth_snp > 0 and hethom_query_snp > 0:
        hethom_penalties.append(ratio_penalty(hethom_query_snp - hethom_truth_snp, 0.15))
    if hethom_truth_indel > 0 and hethom_query_indel > 0:
        hethom_penalties.append(ratio_penalty(hethom_query_indel - hethom_truth_indel, 0.15))
    titv_component = sum(titv_penalties) / len(titv_penalties) if titv_penalties else 1.0
    hethom_component = sum(hethom_penalties) / len(hethom_penalties) if hethom_penalties else 1.0
    quality_component = (titv_component + hethom_component) / 2.0

    raw = 100.0 * (
        0.60 * core_component
        + 0.15 * completeness_component
        + 0.15 * fp_component
        + 0.10 * quality_component
    )
    score_100 = max(0.0, raw - overcall_penalty)
    if not math.isfinite(score_100):  # pragma: no cover - guarded by _number above
        raise ScoreComputationError(f"scorer produced a non-finite score: {score_100!r}")

    return AdvancedScoreBreakdown(
        core_score=core_component,
        completeness_score=completeness_component,
        fp_score=fp_component,
        quality_score=quality_component,
        overcall_penalty=overcall_penalty,
        minos_score_100=min(100.0, score_100),
        minos_score=min(1.0, score_100 / 100.0),
    )


def decide_admission(metrics: dict[str, Any], breakdown: AdvancedScoreBreakdown) -> AdmissionCode:
    """Reproduce what the validator does with the score — which is not always "record it".

    ``_valid_round_score`` requires a finite value in ``(0, 1]``; anything else is skipped, and
    an all-zero-metrics fingerprint near 0.25 is rejected even though it is numerically in range.
    """
    score = breakdown.minos_score
    if not math.isfinite(score):  # pragma: no cover - unreachable via the model bounds
        raise ScoreComputationError(f"non-finite normalized score: {score!r}")
    if score <= 0.0:
        return "NONPOSITIVE_SCORE"
    if score > 1.0:  # pragma: no cover - unreachable via the model bounds
        return "OUT_OF_RANGE_SCORE"
    if (
        _number(metrics, "f1_snp") == 0.0
        and _number(metrics, "f1_indel") == 0.0
        and 0.24999 <= score <= 0.25001
    ):
        return "ZERO_INPUT_FINGERPRINT"
    return "ADMITTED"
