"""Full-versus-adaptive pileup cost model (Layer 1 spec §14).

Predicts pileup seconds from region size, read count, depth, clipping and CIGAR
complexity using conservative rule-based coefficients (calibration deferred — see
the audit). FULL pileup is chosen only when the prediction fits inside both the
pileup soft budget and the remaining time minus the serialization reserve.
"""

from __future__ import annotations

import math

from .contracts import PileupMode

__all__ = ["predict_pileup_seconds", "choose_mode"]


def predict_pileup_seconds(
    coeffs: dict[str, float],
    *,
    region_bp: int,
    read_count: int,
    mean_depth: float,
    max_depth_proxy: float,
    clipping_rate: float,
    cigar_complexity: float,
) -> float:
    exponent = (
        coeffs["b0"]
        + coeffs["b1"] * math.log(max(region_bp, 1))
        + coeffs["b2"] * math.log(read_count + 1)
        + coeffs["b3"] * mean_depth
        + coeffs["b4"] * max_depth_proxy
        + coeffs["b5"] * clipping_rate
        + coeffs["b6"] * cigar_complexity
    )
    # clamp to avoid overflow on pathological inputs
    exponent = min(exponent, 40.0)
    return math.exp(exponent)


def choose_mode(
    predicted_seconds: float,
    *,
    pileup_soft_seconds: float,
    remaining_seconds: float,
    serialization_reserve_seconds: float,
) -> PileupMode:
    budget = min(pileup_soft_seconds, remaining_seconds - serialization_reserve_seconds)
    if budget <= 0:
        return PileupMode.SKIPPED
    return PileupMode.FULL if predicted_seconds <= budget else PileupMode.ADAPTIVE
