"""Decision metrics for L2-G. Frozen orientation, defined before any number is produced.

Accuracy is not the question. The controller will pick ONE config per BAM, so what matters is how
much utility that choice leaves on the table against the best available option — and how bad the
worst case gets. A model with better mean MAE that occasionally picks a catastrophic config is
worse than a duller one that never does.

``regret = oracle_utility - selected_utility``. Lower is better. Fixed here so it cannot be
reoriented after results are seen.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from typing import Final

from minos_engine.common.errors import MinosEngineError

__all__ = [
    "REGRET_ORIENTATION",
    "MetricsError",
    "bam_grouped_regret",
    "calibration_error",
    "downside_summary",
    "spearman",
]

REGRET_ORIENTATION: Final = "ORACLE_MINUS_SELECTED_LOWER_IS_BETTER"


class MetricsError(MinosEngineError):
    """A metric cannot be computed from the evidence supplied."""


def bam_grouped_regret(
    actual: Mapping[tuple[str, str], float], predicted: Mapping[tuple[str, str], float]
) -> dict[str, float]:
    """Per-BAM regret from choosing the model's argmax instead of the oracle's.

    Keys are ``(dataset_id, config_hash)``. Only configs actually observed for a BAM are
    candidates: the campaign's matrix is 70.6% sparse, so scoring a model on a config that was
    never run for that BAM would compare it against a number nobody measured.
    """
    if not actual:
        raise MetricsError("no observations to compute regret over")
    by_bam: dict[str, list[tuple[str, float]]] = {}
    for (dataset_id, config_hash), value in actual.items():
        by_bam.setdefault(dataset_id, []).append((config_hash, value))

    regrets: dict[str, float] = {}
    for dataset_id, options in by_bam.items():
        available = [c for c, _ in options]
        scored = dict(options)
        candidates = [
            (predicted[(dataset_id, c)], c) for c in available if (dataset_id, c) in predicted
        ]
        if not candidates:
            raise MetricsError(f"no prediction for any observed config of {dataset_id}")
        # deterministic tie-break by config hash, so equal predictions do not vary by dict order
        chosen = min(candidates, key=lambda pair: (-pair[0], pair[1]))[1]
        regrets[dataset_id] = max(scored.values()) - scored[chosen]
    return regrets


def downside_summary(regrets: Mapping[str, float], *, alpha: float = 0.25) -> dict[str, float]:
    """Tail behaviour, not just the average. A model is judged on its worst BAMs."""
    if not regrets:
        raise MetricsError("no regrets to summarise")
    values = sorted(regrets.values(), reverse=True)  # worst first
    take = max(1, math.ceil(alpha * len(values)))
    return {
        "mean_regret": sum(values) / len(values),
        "max_regret": values[0],
        "cvar_regret": sum(values[:take]) / take,
        "zero_regret_fraction": sum(1 for v in values if v <= 0.0) / len(values),
    }


def spearman(x: Sequence[float], y: Sequence[float]) -> float:
    """Rank correlation. Ranking is what a selector needs; absolute error is diagnostic."""
    if len(x) != len(y) or len(x) < 2:
        raise MetricsError("spearman needs two equal-length sequences of at least two points")

    def _ranks(values: Sequence[float]) -> list[float]:
        order = sorted(range(len(values)), key=lambda i: values[i])
        ranks = [0.0] * len(values)
        index = 0
        while index < len(order):
            stop = index
            while stop + 1 < len(order) and values[order[stop + 1]] == values[order[index]]:
                stop += 1
            shared = (index + stop) / 2.0 + 1.0
            for position in range(index, stop + 1):
                ranks[order[position]] = shared
            index = stop + 1
        return ranks

    rx, ry = _ranks(x), _ranks(y)
    n = len(rx)
    mx, my = sum(rx) / n, sum(ry) / n
    num = sum((a - mx) * (b - my) for a, b in zip(rx, ry, strict=True))
    dx = math.sqrt(sum((a - mx) ** 2 for a in rx))
    dy = math.sqrt(sum((b - my) ** 2 for b in ry))
    if dx == 0.0 or dy == 0.0:
        raise MetricsError("spearman is undefined when a sequence has no rank variance")
    return num / (dx * dy)


def calibration_error(
    predicted: Sequence[float], actual: Sequence[float], *, bins: int = 10
) -> dict[str, float]:
    """Reliability over OOF predictions. Calibrating on in-fold fits would flatter the model."""
    if len(predicted) != len(actual) or not predicted:
        raise MetricsError("calibration needs equal-length non-empty sequences")
    edges = [i / bins for i in range(bins + 1)]
    total, absolute = 0, 0.0
    for lo, hi in zip(edges[:-1], edges[1:], strict=True):
        members = [
            (p, a)
            for p, a in zip(predicted, actual, strict=True)
            if lo <= p < hi or (hi == 1.0 and p == 1.0)
        ]
        if not members:
            continue
        mean_p = sum(p for p, _ in members) / len(members)
        mean_a = sum(a for _, a in members) / len(members)
        absolute += abs(mean_p - mean_a) * len(members)
        total += len(members)
    if total == 0:  # pragma: no cover - guarded by the emptiness check above
        raise MetricsError("no predictions fell inside the calibration bins")
    return {"absolute_calibration_error": absolute / total, "binned_points": float(total)}
