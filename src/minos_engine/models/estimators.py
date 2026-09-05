"""The CLOSED estimator factory: ModelSpec v3 -> concrete scikit-learn objects.

Nothing here imports from a caller-supplied string. ``importlib`` on a spec field would make the
spec an instruction to execute arbitrary code, and a "model family" would then mean whatever
happened to be importable. The mapping is a literal dictionary of the six estimators the frozen
protocol names, and an implementation outside it is refused rather than resolved.

Every estimator is also checked for the two properties this campaign depends on: it must accept
``sample_weight`` (EQUAL_BAM_TOTAL is not advisory) and it must accept a ``random_state`` where
one exists (a seed that is not passed is not a seed).
"""

from __future__ import annotations

import inspect
from typing import Any, Final

from minos_engine.common.errors import MinosEngineError
from minos_engine.models.spec import ModelSpec

__all__ = [
    "SUPPORTED_ESTIMATORS",
    "EstimatorFactoryError",
    "build_admission_estimator",
    "build_score_estimator",
]


class EstimatorFactoryError(MinosEngineError):
    """The specification does not map to a supported estimator."""


#: the ONLY estimators this campaign may fit. Names are matched exactly.
SUPPORTED_ESTIMATORS: Final[tuple[str, ...]] = (
    "sklearn.linear_model.Ridge",
    "sklearn.linear_model.LogisticRegression",
    "sklearn.ensemble.HistGradientBoostingRegressor",
    "sklearn.ensemble.HistGradientBoostingClassifier",
    "sklearn.neural_network.MLPRegressor",
    "sklearn.neural_network.MLPClassifier",
)

_SCORE_ESTIMATORS: Final = frozenset(
    {
        "sklearn.linear_model.Ridge",
        "sklearn.ensemble.HistGradientBoostingRegressor",
        "sklearn.neural_network.MLPRegressor",
    }
)
_ADMISSION_ESTIMATORS: Final = frozenset(
    {
        "sklearn.linear_model.LogisticRegression",
        "sklearn.ensemble.HistGradientBoostingClassifier",
        "sklearn.neural_network.MLPClassifier",
    }
)


def _construct(name: str, hyperparameters: dict[str, Any], seed: int) -> Any:
    if name not in SUPPORTED_ESTIMATORS:
        raise EstimatorFactoryError(
            f"{name!r} is not a supported estimator; the factory resolves a fixed table, never a "
            f"caller-supplied import path. Supported: {list(SUPPORTED_ESTIMATORS)}"
        )
    from sklearn.ensemble import (
        HistGradientBoostingClassifier,
        HistGradientBoostingRegressor,
    )
    from sklearn.linear_model import LogisticRegression, Ridge
    from sklearn.neural_network import MLPClassifier, MLPRegressor

    table = {
        "sklearn.linear_model.Ridge": Ridge,
        "sklearn.linear_model.LogisticRegression": LogisticRegression,
        "sklearn.ensemble.HistGradientBoostingRegressor": HistGradientBoostingRegressor,
        "sklearn.ensemble.HistGradientBoostingClassifier": HistGradientBoostingClassifier,
        "sklearn.neural_network.MLPRegressor": MLPRegressor,
        "sklearn.neural_network.MLPClassifier": MLPClassifier,
    }
    cls = table[name]
    params = dict(hyperparameters)
    if "hidden_layer_sizes" in params and isinstance(params["hidden_layer_sizes"], list):
        params["hidden_layer_sizes"] = tuple(params["hidden_layer_sizes"])

    accepted = set(inspect.signature(cls.__init__).parameters)
    unknown = sorted(set(params) - accepted)
    if unknown:
        raise EstimatorFactoryError(f"{name} does not accept hyperparameters {unknown}")
    if "random_state" in accepted:
        params["random_state"] = seed
    estimator = cls(**params)

    # EQUAL_BAM_TOTAL is load-bearing: an estimator that ignores it would silently fit whichever
    # BAMs the campaign scheduled most, which is the thing the weighting exists to prevent.
    if "sample_weight" not in set(inspect.signature(estimator.fit).parameters):
        raise EstimatorFactoryError(
            f"{name} does not accept sample_weight and cannot honour EQUAL_BAM_TOTAL"
        )
    return estimator


def build_score_estimator(spec: ModelSpec) -> Any:
    """The E[S | ADMITTED] head."""
    name = spec.score_model_implementation
    if name not in _SCORE_ESTIMATORS:
        raise EstimatorFactoryError(f"{name!r} is not a score (regression) estimator")
    return _construct(name, spec.score_hyperparameters, spec.random_seed)


def build_admission_estimator(spec: ModelSpec) -> Any:
    """The P(ADMITTED) head. Its family follows the spec, not a single hard-coded logistic."""
    name = spec.admission_model_implementation
    if name not in _ADMISSION_ESTIMATORS:
        raise EstimatorFactoryError(f"{name!r} is not an admission (classification) estimator")
    return _construct(name, spec.admission_hyperparameters, spec.random_seed)
