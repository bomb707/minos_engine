"""The L2-G v2 out-of-fold runner: inner margins, the switch policy, and decision metrics.

The margin is the part that has to be exact. It is a safety threshold under a loss asymmetry the
feasibility audit measured — switching is right on 19 of 150 opportunities, and the average loss
when wrong is several times the average gain when right — so a margin that drifted between
implementations would silently change how often the policy deviates from the baseline.

So everything about it is pinned: four inner folds (one per remaining chromosome), each of the 40
outer-training BAMs predicted exactly once by an inner model that never saw it, exactly 120
residuals pooled across all BAMs and all three alternatives, and ``numpy.quantile(..., method=
"higher")`` so the threshold is an observed error rather than an interpolation between two.

The switch comparison is strict. At ``predicted == margin`` the evidence does not distinguish the
actions, and with these losses the fallback is the right side of a tie.
"""

from __future__ import annotations

from typing import Any, Final

from minos_engine.common.errors import MinosEngineError
from minos_engine.models.contract import CV_FOLD_CHROMOSOMES
from minos_engine.models.relative_finalist_contract import (
    ALTERNATIVE_FINALISTS,
    FINALIST_DOMAIN,
    SAFE_BASELINE_CONFIG_HASH,
)
from minos_engine.models.relative_finalist_protocol import (
    INNER_FOLD_COUNT,
    INNER_RESIDUAL_COUNT,
    NUMPY_QUANTILE_METHOD,
    OUTER_HELD_BAMS,
    OUTER_TRAINING_BAMS,
    TRANSFORM_POLICY,
    WEIGHTING_POLICY,
)

__all__ = [
    "OofDeltaRecord",
    "PolicyDecision",
    "RelativeRunnerError",
    "build_estimator",
    "derive_switch_margin",
    "inner_folds_for",
    "policy_decisions",
    "policy_metrics",
    "reference_decisions",
    "run_relative_outer_oof",
]

CVAR_ALPHA: Final = 0.25
_SUPPORTED: Final = {
    "sklearn.linear_model.Ridge",
    "sklearn.ensemble.HistGradientBoostingRegressor",
}


class RelativeRunnerError(MinosEngineError):
    """The v2 out-of-fold campaign could not be run honestly."""


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise RelativeRunnerError(message)


class OofDeltaRecord:
    """One held-out advantage prediction."""

    __slots__ = (
        "actual_delta",
        "alternative_config",
        "chromosome",
        "dataset_id",
        "outer_fold",
        "predicted_delta",
        "spec_hash",
    )

    actual_delta: float
    alternative_config: str
    chromosome: str
    dataset_id: str
    outer_fold: str
    predicted_delta: float
    spec_hash: str

    def __init__(self, **fields: Any) -> None:
        for name in self.__slots__:
            setattr(self, name, fields[name])

    def content(self) -> dict[str, Any]:
        return {name: getattr(self, name) for name in sorted(self.__slots__)}


class PolicyDecision:
    """One held-out BAM's decision, with everything needed to check it."""

    __slots__ = (
        "actual_selected_delta",
        "chromosome",
        "dataset_id",
        "margin",
        "oracle4_utility",
        "outer_fold",
        "predictions",
        "regret",
        "safe_utility",
        "selected_config",
        "selected_utility",
        "spec_hash",
        "switched",
    )

    actual_selected_delta: float
    chromosome: str
    dataset_id: str
    margin: float
    oracle4_utility: float
    outer_fold: str
    predictions: dict[str, float]
    regret: float
    safe_utility: float
    selected_config: str
    selected_utility: float
    spec_hash: str
    switched: bool

    def __init__(self, **fields: Any) -> None:
        for name in self.__slots__:
            setattr(self, name, fields[name])

    def content(self) -> dict[str, Any]:
        return {name: getattr(self, name) for name in sorted(self.__slots__)}


def inner_folds_for(
    training_bams: frozenset[str], chromosome_of: dict[str, str]
) -> list[tuple[frozenset[str], frozenset[str]]]:
    """Leave one remaining chromosome out, over the four left in the outer training side."""
    groups: dict[str, set[str]] = {}
    for bam in training_bams:
        groups.setdefault(chromosome_of[bam], set()).add(bam)
    _require(
        len(groups) == INNER_FOLD_COUNT,
        f"the inner split has {len(groups)} chromosome groups, expected {INNER_FOLD_COUNT}",
    )
    folds = []
    for chromosome in sorted(groups):
        held = frozenset(groups[chromosome])
        folds.append((frozenset(training_bams) - held, held))
    return folds


def build_estimator(spec: Any) -> Any:
    """A closed factory: two estimators, resolved from a table, never from an import path."""
    name = str(spec["implementation"])
    _require(name in _SUPPORTED, f"{name!r} is not a supported v2 estimator")
    from sklearn.ensemble import HistGradientBoostingRegressor
    from sklearn.linear_model import Ridge

    params = dict(spec["hyperparameters"])
    if name.endswith("Ridge"):
        estimator: Any = Ridge(**params, random_state=int(spec["random_seed"]))
    else:
        estimator = HistGradientBoostingRegressor(**params, random_state=int(spec["random_seed"]))
    import inspect

    _require(
        "sample_weight" in set(inspect.signature(estimator.fit).parameters),
        f"{name} cannot honour EQUAL_BAM_TOTAL",
    )
    return estimator


def _fit_predict(spec: Any, x_fit: Any, y_fit: Any, w_fit: Any, x_pred: Any) -> Any:
    """Fit and predict with the family's frozen transform policy, on the training side only."""
    import numpy as np
    from sklearn.preprocessing import StandardScaler

    policy = TRANSFORM_POLICY[str(spec["family"])]
    if policy["transform"] == "STANDARD_SCALER":
        scaler = StandardScaler().fit(x_fit)
        x_fit, x_pred = scaler.transform(x_fit), scaler.transform(x_pred)
    else:
        _require(policy["transform"] == "NONE", "unknown transform policy")
    estimator = build_estimator(spec)
    estimator.fit(x_fit, y_fit, sample_weight=w_fit)
    predicted = np.asarray(estimator.predict(x_pred), dtype=float)
    _require(bool(np.all(np.isfinite(predicted))), "an estimator produced a non-finite prediction")
    return predicted


def _weights(n: int) -> Any:
    """EQUAL_BAM_TOTAL: three rows per BAM, so each row carries one third."""
    import numpy as np

    return np.full(n, float(WEIGHTING_POLICY["row_weight"]), dtype=float)


def derive_switch_margin(residuals: Any, *, margin_quantile: float) -> float:
    """The frozen quantile of exactly 120 inner out-of-fold absolute residuals."""
    import numpy as np

    array = np.asarray(residuals, dtype=float)
    _require(
        array.size == INNER_RESIDUAL_COUNT,
        f"{array.size} inner residuals, expected exactly {INNER_RESIDUAL_COUNT}",
    )
    _require(bool(np.all(np.isfinite(array))), "an inner residual is not finite")
    _require(bool(np.all(array >= 0.0)), "an inner residual is negative; it must be an absolute")
    return float(np.quantile(array, margin_quantile, method=NUMPY_QUANTILE_METHOD))


def policy_decisions(
    *,
    spec_hash: str,
    predictions: dict[tuple[str, str], float],
    margin: float,
    utility: dict[tuple[str, str], float],
    held_bams: list[str],
    chromosome_of: dict[str, str],
    outer_fold: str,
) -> list[PolicyDecision]:
    """Apply the frozen switch rule. Strictly greater than the margin, or keep the baseline."""
    decisions = []
    for bam in sorted(held_bams):
        per_alt = {c: float(predictions[(bam, c)]) for c in ALTERNATIVE_FINALISTS}
        best = max(per_alt.values())
        # ties on the prediction go to the lowest config hash, never to iteration order
        choice = sorted(c for c, v in per_alt.items() if v == best)[0]
        switched = best > margin  # STRICT: equality keeps the safe baseline
        selected = choice if switched else SAFE_BASELINE_CONFIG_HASH
        safe_u = utility[(bam, SAFE_BASELINE_CONFIG_HASH)]
        selected_u = utility[(bam, selected)]
        oracle = max(utility[(bam, c)] for c in FINALIST_DOMAIN)
        decisions.append(
            PolicyDecision(
                spec_hash=spec_hash,
                dataset_id=bam,
                chromosome=chromosome_of[bam],
                outer_fold=outer_fold,
                predictions=dict(sorted(per_alt.items())),
                margin=float(margin),
                selected_config=selected,
                switched=bool(switched),
                safe_utility=safe_u,
                selected_utility=selected_u,
                oracle4_utility=oracle,
                regret=oracle - selected_u,
                actual_selected_delta=selected_u - safe_u,
            )
        )
    return decisions


def reference_decisions(
    name: str,
    *,
    utility: dict[tuple[str, str], float],
    held_bams: list[str],
    training_bams: list[str],
    chromosome_of: dict[str, str],
    outer_fold: str,
) -> list[PolicyDecision]:
    """The three frozen reference policies, with their executable semantics."""
    if name == "ALWAYS_SAFE_BASELINE":
        chosen = dict.fromkeys(held_bams, SAFE_BASELINE_CONFIG_HASH)
    elif name == "GLOBAL_BEST_FINALIST_FROM_OUTER_TRAIN":
        # equal-BAM mean utility over the OUTER-TRAINING BAMs only; the holdout is not consulted
        means = {
            c: sum(utility[(b, c)] for b in training_bams) / len(training_bams)
            for c in FINALIST_DOMAIN
        }
        best = max(means.values())
        pick = sorted(c for c, v in means.items() if v == best)[0]
        chosen = dict.fromkeys(held_bams, pick)
    elif name == "ORACLE4":
        chosen = {}
        for b in held_bams:
            best = max(utility[(b, c)] for c in FINALIST_DOMAIN)
            chosen[b] = sorted(c for c in FINALIST_DOMAIN if utility[(b, c)] == best)[0]
    else:
        raise RelativeRunnerError(f"{name!r} is not a frozen v2 reference")

    out = []
    for bam in sorted(held_bams):
        selected = chosen[bam]
        safe_u = utility[(bam, SAFE_BASELINE_CONFIG_HASH)]
        selected_u = utility[(bam, selected)]
        oracle = max(utility[(bam, c)] for c in FINALIST_DOMAIN)
        out.append(
            PolicyDecision(
                spec_hash=name,
                dataset_id=bam,
                chromosome=chromosome_of[bam],
                outer_fold=outer_fold,
                predictions={},
                margin=float("nan"),
                selected_config=selected,
                switched=selected != SAFE_BASELINE_CONFIG_HASH,
                safe_utility=safe_u,
                selected_utility=selected_u,
                oracle4_utility=oracle,
                regret=oracle - selected_u,
                actual_selected_delta=selected_u - safe_u,
            )
        )
    return out


def policy_metrics(decisions: list[PolicyDecision]) -> dict[str, Any]:
    """Decision metrics first. Prediction accuracy is a diagnostic, not a qualification."""
    import math

    _require(bool(decisions), "no decisions to measure")
    regrets = [float(d.regret) for d in decisions]
    n = len(regrets)
    tail = max(1, math.ceil(CVAR_ALPHA * n))
    switches = [d for d in decisions if d.switched]
    helpful = [d for d in switches if d.actual_selected_delta > 0]
    harmful = [d for d in switches if d.actual_selected_delta < 0]
    return {
        "decision_count": n,
        "mean_regret": sum(regrets) / n,
        "max_regret": max(regrets),
        "cvar_regret": sum(sorted(regrets, reverse=True)[:tail]) / tail,
        "cvar_tail_count": tail,
        "zero_regret_fraction": sum(1 for r in regrets if r <= 0.0) / n,
        "safe_baseline_kept_fraction": len([d for d in decisions if not d.switched]) / n,
        "switch_fraction": len(switches) / n,
        "switch_precision": (len(helpful) / len(switches)) if switches else None,
        "mean_gain_on_switch": (
            sum(d.actual_selected_delta for d in switches) / len(switches) if switches else None
        ),
        "mean_loss_on_harmful_switch": (
            sum(d.actual_selected_delta for d in harmful) / len(harmful) if harmful else None
        ),
        "harmful_switch_count": len(harmful),
        "worst_switch_delta": (
            min(d.actual_selected_delta for d in switches) if switches else None
        ),
    }


def run_relative_outer_oof(
    *,
    spec: dict[str, Any],
    spec_hash: str,
    rows: list[Any],
    design: dict[tuple[str, str], Any],
    utility: dict[tuple[str, str], float],
    chromosome_of: dict[str, str],
) -> dict[str, Any]:
    """Five outer folds; per fold, four inner folds for the margin, then one decision per BAM."""
    import numpy as np

    by_cell = {(r.dataset_id, r.config_hash): r for r in rows}
    records: list[OofDeltaRecord] = []
    decisions: list[PolicyDecision] = []
    margins: dict[str, float] = {}
    predicted_cells: set[tuple[str, str]] = set()
    decided: set[str] = set()

    for chromosome in CV_FOLD_CHROMOSOMES:
        held_bams = sorted(b for b, c in chromosome_of.items() if c == chromosome)
        train_bams = sorted(b for b in chromosome_of if b not in set(held_bams))
        _require(
            len(train_bams) == OUTER_TRAINING_BAMS and len(held_bams) == OUTER_HELD_BAMS,
            f"outer fold {chromosome} is {len(train_bams)}/{len(held_bams)}, expected "
            f"{OUTER_TRAINING_BAMS}/{OUTER_HELD_BAMS}",
        )

        # --- inner folds -> exactly 120 residuals, none from the outer holdout -------------- #
        residuals: list[float] = []
        seen_inner: set[tuple[str, str]] = set()
        for inner_train, inner_held in inner_folds_for(frozenset(train_bams), chromosome_of):
            _require(
                not (inner_train & frozenset(held_bams))
                and not (inner_held & frozenset(held_bams)),
                "an inner fold reached into the outer holdout",
            )
            fit_cells = [(b, c) for b in sorted(inner_train) for c in ALTERNATIVE_FINALISTS]
            pred_cells = [(b, c) for b in sorted(inner_held) for c in ALTERNATIVE_FINALISTS]
            x_fit = np.asarray([design[k] for k in fit_cells], dtype=float)
            y_fit = np.asarray([by_cell[k].delta for k in fit_cells], dtype=float)
            x_pred = np.asarray([design[k] for k in pred_cells], dtype=float)
            predicted = _fit_predict(spec, x_fit, y_fit, _weights(len(fit_cells)), x_pred)
            for key, value in zip(pred_cells, predicted, strict=True):
                _require(key not in seen_inner, f"inner cell {key} predicted twice")
                seen_inner.add(key)
                residuals.append(abs(float(value) - float(by_cell[key].delta)))
        margin = derive_switch_margin(residuals, margin_quantile=float(spec["margin_quantile"]))
        margins[chromosome] = margin

        # --- outer fit on the 40, prediction on the untouched 10 --------------------------- #
        fit_cells = [(b, c) for b in train_bams for c in ALTERNATIVE_FINALISTS]
        pred_cells = [(b, c) for b in held_bams for c in ALTERNATIVE_FINALISTS]
        x_fit = np.asarray([design[k] for k in fit_cells], dtype=float)
        y_fit = np.asarray([by_cell[k].delta for k in fit_cells], dtype=float)
        x_pred = np.asarray([design[k] for k in pred_cells], dtype=float)
        predicted = _fit_predict(spec, x_fit, y_fit, _weights(len(fit_cells)), x_pred)

        per_cell = {}
        for key, value in zip(pred_cells, predicted, strict=True):
            _require(key not in predicted_cells, f"cell {key} predicted in two outer folds")
            predicted_cells.add(key)
            per_cell[key] = float(value)
            records.append(
                OofDeltaRecord(
                    spec_hash=spec_hash,
                    dataset_id=key[0],
                    chromosome=chromosome_of[key[0]],
                    alternative_config=key[1],
                    actual_delta=float(by_cell[key].delta),
                    predicted_delta=float(value),
                    outer_fold=chromosome,
                )
            )
        for decision in policy_decisions(
            spec_hash=spec_hash,
            predictions=per_cell,
            margin=margin,
            utility=utility,
            held_bams=held_bams,
            chromosome_of=chromosome_of,
            outer_fold=chromosome,
        ):
            _require(decision.dataset_id not in decided, "a BAM was decided twice")
            decided.add(decision.dataset_id)
            decisions.append(decision)

    return {
        "spec_hash": spec_hash,
        "records": records,
        "decisions": decisions,
        "margins": dict(sorted(margins.items())),
        "metrics": policy_metrics(decisions),
    }
