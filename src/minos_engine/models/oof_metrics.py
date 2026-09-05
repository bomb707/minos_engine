"""Metrics over out-of-fold predictions. The unit of selection is the BAM, never the cell.

Cell-weighted aggregates would let the ten Phase-A BAMs, which carry up to 80 examples each,
outvote the forty that carry ten. Regret is therefore computed per BAM and then aggregated, and
the oracle is always the best config ACTUALLY OBSERVED for that BAM — a config nobody ran for it
is not a missed opportunity, it is an unknown.
"""

from __future__ import annotations

import math
from typing import Any, Final

from minos_engine.common.errors import MinosEngineError
from minos_engine.models.contract import SAFE_BASELINE_CONFIG_HASH

__all__ = [
    "REGRET_ORIENTATION",
    "SELECTION_POLICY",
    "ReferenceSelectionUnavailable",
    "OofMetricsError",
    "admission_metrics",
    "bam_selection_regret",
    "score_metrics",
    "summarise_oof",
]

REGRET_ORIENTATION: Final = "ORACLE_MINUS_SELECTED_LOWER_IS_BETTER"
CVAR_ALPHA: Final = 0.25
#: a selected config that is worse than the safe baseline by more than this is a regression a
#: controller would actually be blamed for, so it is counted rather than averaged away
CATASTROPHIC_MARGIN: Final = 0.05


#: selection is bound to the FAMILY, never inferred from prediction equality. A constant
#: predictor whose configs all score alike would otherwise "choose" the lexicographically lowest
#: config, which is not what the safe-baseline reference does or means.
SELECT_SAFE_BASELINE_CONFIG: Final = "ALWAYS_THE_QUALIFIED_SAFE_BASELINE_CONFIG"
SELECT_LEXICOGRAPHIC: Final = "LOWEST_CONFIG_HASH_LEXICOGRAPHIC"
SELECT_PREDICTED_UTILITY: Final = "HIGHEST_PREDICTED_UTILITY_THEN_LOWEST_CONFIG_HASH"

SELECTION_POLICY: Final[dict[str, str]] = {
    "CONSTANT_SAFE_BASELINE": SELECT_SAFE_BASELINE_CONFIG,
    # blind to the config by construction, so a ranking would be noise dressed as a choice
    "GLOBAL_MEAN": SELECT_LEXICOGRAPHIC,
    "BAM_FEATURES_ONLY": SELECT_LEXICOGRAPHIC,
    "CONFIG_ONLY": SELECT_PREDICTED_UTILITY,
    "LINEAR_REGULARIZED": SELECT_PREDICTED_UTILITY,
    "TREE_ENSEMBLE": SELECT_PREDICTED_UTILITY,
    "COMPACT_MLP": SELECT_PREDICTED_UTILITY,
}


class OofMetricsError(MinosEngineError):
    """The out-of-fold predictions do not support a metric."""


class ReferenceSelectionUnavailable(OofMetricsError):
    """A reference cannot be scored because the config it always selects was never observed."""


def _tie_broken_best(candidates: list[tuple[str, float]]) -> str:
    """Highest predicted utility; ties broken by the LOWEST config hash, never by order."""
    if not candidates:
        raise OofMetricsError("no candidate configs for this BAM")
    best = max(value for _, value in candidates)
    return sorted(config for config, value in candidates if value == best)[0]


def _select_for(
    family: str, *, observed: dict[str, float], predicted: list[tuple[str, float]]
) -> str:
    """Apply the FROZEN selection policy for this family."""
    try:
        policy = SELECTION_POLICY[family]
    except KeyError:
        raise OofMetricsError(f"{family!r} has no frozen selection policy") from None
    if policy == SELECT_SAFE_BASELINE_CONFIG:
        if SAFE_BASELINE_CONFIG_HASH not in observed:
            raise ReferenceSelectionUnavailable(
                "the safe baseline config was never observed for this BAM, so the reference has "
                "no measurable held-out utility and the campaign cannot use it as a bar"
            )
        return SAFE_BASELINE_CONFIG_HASH
    if policy == SELECT_LEXICOGRAPHIC:
        return sorted(observed)[0]
    return _tie_broken_best(predicted)


def bam_selection_regret(
    records: Any, *, family: str, safe_baseline_config: str | None = None
) -> dict[str, Any]:
    """Per-BAM regret of the FAMILY'S selected config against the observed oracle.

    The family is required rather than inferred: a constant predictor scores every config alike,
    and inferring "no preference, so take the lowest hash" would quietly replace the safe-baseline
    reference with a different model.
    """
    by_bam: dict[str, list[Any]] = {}
    for record in records:
        by_bam.setdefault(record.dataset_id, []).append(record)
    if not by_bam:
        raise OofMetricsError("no out-of-fold records to measure")

    regrets: dict[str, float] = {}
    selected: dict[str, str] = {}
    selected_utility: dict[str, float] = {}
    catastrophic = 0
    for dataset_id, rows in sorted(by_bam.items()):
        actual = {r.config_hash: float(r.actual_utility) for r in rows}
        if not actual:
            raise OofMetricsError(f"{dataset_id} has no observed config")
        predicted = [(r.config_hash, float(r.expected_utility_prediction)) for r in rows]
        choice = _select_for(family, observed=actual, predicted=predicted)
        selected[dataset_id] = choice
        selected_utility[dataset_id] = actual[choice]
        oracle = max(actual.values())
        regrets[dataset_id] = oracle - actual[choice]
        if (
            safe_baseline_config is not None
            and safe_baseline_config in actual
            and actual[choice] < actual[safe_baseline_config] - CATASTROPHIC_MARGIN
        ):
            catastrophic += 1
    return {
        "family": family,
        "selection_policy": SELECTION_POLICY[family],
        "orientation": REGRET_ORIENTATION,
        "per_bam_regret": regrets,
        "selected_config": selected,
        "selected_actual_utility": selected_utility,
        "catastrophic_regression_count": catastrophic,
    }


def _cvar(values: list[float], alpha: float) -> float:
    """Mean of the ``ceil(alpha * N)`` WORST regrets.

    ``ceil``, not ``round``: the frozen baseline objective's CVaR takes ``ceil(alpha * N)`` of the
    lower utility tail, and a regret metric that quietly took a smaller tail would be a different
    robustness measure wearing the same name. At N=50, alpha=0.25 that is 13 BAMs, not 12 --
    Python's banker's rounding turns 12.5 into 12.
    """
    if not values:
        raise OofMetricsError("no values to take a tail of")
    # descending: HIGH regret is bad, so the tail is the top of the sorted list
    ordered = sorted(values, reverse=True)
    take = max(1, math.ceil(alpha * len(ordered)))
    return sum(ordered[:take]) / take


def summarise_oof(regret: dict[str, Any]) -> dict[str, float]:
    values = list(regret["per_bam_regret"].values())
    if not values:
        raise OofMetricsError("no per-BAM regret to summarise")
    return {
        "mean_regret": sum(values) / len(values),
        "max_regret": max(values),
        "cvar_regret": _cvar(values, CVAR_ALPHA),
        "zero_regret_fraction": sum(1 for v in values if v <= 0.0) / len(values),
    }


def score_metrics(records: Any) -> dict[str, float]:
    """Accuracy of E[S | ADMITTED], measured only where an admitted score actually exists."""
    import numpy as np

    rows = [r for r in records if r.actual_admitted_score is not None]
    if not rows:
        raise OofMetricsError("no admitted example to score against")
    actual = np.asarray([float(r.actual_admitted_score) for r in rows], dtype=float)
    predicted = np.asarray([float(r.clipped_score_prediction) for r in rows], dtype=float)
    residual = predicted - actual
    total = float(np.sum((actual - actual.mean()) ** 2))
    metrics = {
        "score_mae": float(np.mean(np.abs(residual))),
        "score_rmse": float(np.sqrt(np.mean(residual**2))),
        "score_r2": float(1.0 - np.sum(residual**2) / total) if total > 0 else float("nan"),
    }
    try:
        from minos_engine.models.metrics import spearman

        metrics["score_spearman"] = spearman(list(actual), list(predicted))
    except Exception:
        # a diagnostic that cannot be computed (constant ranks) is reported absent, not faked
        metrics["score_spearman"] = float("nan")
    return metrics


def admission_metrics(records: Any) -> dict[str, float]:
    """Brier, log loss and absolute calibration error of the CALIBRATED probability."""
    import numpy as np

    rows = list(records)
    if not rows:
        raise OofMetricsError("no admission predictions")
    labels = np.asarray([1.0 if r.actual_outcome == "ADMITTED" else 0.0 for r in rows])
    probabilities = np.clip(
        np.asarray([float(r.calibrated_admission_probability) for r in rows], dtype=float),
        1e-12,
        1 - 1e-12,
    )
    return {
        "admission_brier": float(np.mean((probabilities - labels) ** 2)),
        "admission_log_loss": float(
            -np.mean(labels * np.log(probabilities) + (1 - labels) * np.log(1 - probabilities))
        ),
        "admission_calibration_error": float(abs(probabilities.mean() - labels.mean())),
    }
