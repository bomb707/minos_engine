"""Minos scoring layer — inputs are authoritative; the composite score is not.

``build_score_inputs`` assembles normalized inputs a scorer would consume, using
only standard, authoritative definitions (F1, mean recall, truth/call/FP totals).

``compute_score`` returns a **typed unavailable** result: the exact pinned Minos
``AdvancedScorer`` formula, weights, chromosome weighting, clipping, and
normalization are NOT defined in the authoritative specifications or this
repository (Overall spec §7 references a "pinned AdvancedScorer" without defining
it). Per the Stage 1 mandate we do not invent it; we return
``AUTHORITATIVE_SCORER_NOT_AVAILABLE``. A structural/fixture-replay Twin still
qualifies at its declared level but must not claim numerical validator parity.
"""

from __future__ import annotations

from .contracts import ComparisonMetrics, ScoreInputs, TwinScoreResult
from .unavailable import AvailabilityStatus, ReasonCode

__all__ = ["build_score_inputs", "compute_score"]


def build_score_inputs(metrics: ComparisonMetrics) -> ScoreInputs:
    """Assemble normalized scoring inputs from comparison metrics (authoritative)."""
    total_truth = metrics.snp.truth_total + metrics.indel.truth_total
    mean_recall = (metrics.snp_recall + metrics.indel_recall) / 2.0
    return ScoreInputs(
        round_id=metrics.round_id,
        snp_f1=metrics.snp_f1,
        indel_f1=metrics.indel_f1,
        mean_recall=mean_recall,
        total_truth=total_truth,
        total_calls=metrics.total_calls,
        fp_total=metrics.snp.fp + metrics.indel.fp,
    )


def compute_score(inputs: ScoreInputs) -> TwinScoreResult:
    """Return the Minos score — typed UNAVAILABLE until the pinned scorer is known.

    Do not introduce an invented fallback score here. When an authoritative
    AdvancedScorer is provided (a later stage), populate components + final_score
    and set status AVAILABLE with the scorer identity.
    """
    return TwinScoreResult(
        round_id=inputs.round_id,
        status=AvailabilityStatus.UNAVAILABLE,
        reason_code=ReasonCode.AUTHORITATIVE_SCORER_NOT_AVAILABLE,
        score_inputs=inputs,
    )
