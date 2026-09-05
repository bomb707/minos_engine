"""The ACCEPTED candidate- and reference-spec factory. Callers do not author scientific specs.

``ModelSpec`` is a faithful container, and that is exactly the problem: it accepts
``weighting_policy="PER_ROW"`` or ``dedup_policy="NONE"`` and merely produces a different hash. A
spec that silently abandons EQUAL_BAM_TOTAL is not a variant of this campaign, it is a different
experiment wearing the same type.

So the scientific fields are derived here from the frozen protocol and the real dataset, and the
caller supplies neither. The recipe grid is the one already frozen in
``l2g-model-training-protocol-v1``; this module only turns each recipe into a complete, hashed
spec bound to the real training dataset.

The REFERENCES are frozen with the same care as the candidates. "Beat the best reference" is the
promotion rule, so an under-specified reference is a promotion threshold nobody can reproduce.
"""

from __future__ import annotations

from typing import Any, Final

from minos_engine.common.errors import MinosEngineError
from minos_engine.models.config_encoder import build_config_encoding
from minos_engine.models.contract import (
    DEDUP_POLICY,
    FROZEN_FEATURE_SET_HASH,
    TARGET_FORMULATION,
    WEIGHTING_POLICY,
)
from minos_engine.models.dataset import TrainingDataset
from minos_engine.models.protocol import (
    CALIBRATION_POLICY,
    CANDIDATE_GRID,
    RANDOM_SEED,
    compute_training_protocol_hash,
)
from minos_engine.models.runtime import verify_training_runtime
from minos_engine.models.spec import (
    ADMISSION_TRAINING_POPULATION,
    PROMOTABLE_FAMILIES,
    REFERENCE_FAMILIES,
    SCORE_OUTPUT_POSTPROCESS,
    SCORE_TRAINING_POPULATION,
    ModelSpec,
)

__all__ = [
    "REFERENCE_RECIPES",
    "ModelSpecFactoryError",
    "build_accepted_l2g_model_specs",
    "build_accepted_l2g_reference_specs",
]

_OOD_METHOD: Final = "STANDARDIZED_FEATURE_DISTANCE"

#: The frozen grid names a classifier PER FAMILY. v2 recorded "LOGISTIC_P_ADMISSION" for all six,
#: which was simply untrue for the tree and MLP candidates. The scientific event is still
#: P(ADMITTED | X, theta); only the estimator family is now recorded truthfully.
_IMPLEMENTATIONS: Final[dict[str, str]] = {
    "Ridge": "sklearn.linear_model.Ridge",
    "LogisticRegression": "sklearn.linear_model.LogisticRegression",
    "HistGradientBoostingRegressor": "sklearn.ensemble.HistGradientBoostingRegressor",
    "HistGradientBoostingClassifier": "sklearn.ensemble.HistGradientBoostingClassifier",
    "MLPRegressor": "sklearn.neural_network.MLPRegressor",
    "MLPClassifier": "sklearn.neural_network.MLPClassifier",
}

#: which frozen grid hyperparameters belong to which head. ``alpha`` means Ridge penalty for the
#: linear family and MLP weight decay for the neural one; ``C`` is the logistic penalty. Splitting
#: them is what lets each head be constructed without guessing.
_LINEAR_SCORE_KEYS: Final = frozenset({"alpha"})
_LINEAR_ADMISSION_KEYS: Final = frozenset({"C"})


def _split_hyperparameters(
    family: str, hyperparameters: dict[str, Any]
) -> tuple[dict[str, Any], dict[str, Any]]:
    if family == "LINEAR_REGULARIZED":
        score = {k: v for k, v in hyperparameters.items() if k in _LINEAR_SCORE_KEYS}
        admission = {k: v for k, v in hyperparameters.items() if k in _LINEAR_ADMISSION_KEYS}
        unknown = set(hyperparameters) - _LINEAR_SCORE_KEYS - _LINEAR_ADMISSION_KEYS
        if unknown:
            raise ModelSpecFactoryError(f"unassignable linear hyperparameters {sorted(unknown)}")
        return dict(sorted(score.items())), dict(sorted(admission.items()))
    # tree and MLP recipes describe both heads with the same structural parameters
    shared = dict(sorted(hyperparameters.items()))
    return shared, dict(shared)


class ModelSpecFactoryError(MinosEngineError):
    """A candidate specification was not produced by the accepted factory."""


#: Each reference is fully executable: what it predicts, what it fits on, whether it has an
#: admission component, and how it breaks ties. "Predict the safe baseline" is otherwise three
#: different models depending on who implements it.
REFERENCE_RECIPES: Final[tuple[dict[str, Any], ...]] = (
    {
        "family": "CONSTANT_SAFE_BASELINE",
        "score_implementation": "minos_engine.models.references.ConstantSafeBaseline",
        "admission_implementation": "minos_engine.models.references.ConstantSafeBaseline",
        "score_hyperparameters": {},
        "admission_hyperparameters": {},
        "score_loss": "NONE_CONSTANT",
        "admission_loss": "NONE_CONSTANT",
        "predicts": "THE_SAFE_BASELINE_CONFIG_FOR_EVERY_BAM",
        "score_fit_data": "THE_SAFE_BASELINE_CONFIG_ROWS_IN_THE_FOLD_TRAINING_BAMS",
        "tie_break": "NOT_APPLICABLE_SINGLE_CONFIG",
        "transform_specification": {},
    },
    {
        "family": "GLOBAL_MEAN",
        "score_implementation": "minos_engine.models.references.GlobalMean",
        "admission_implementation": "minos_engine.models.references.GlobalMean",
        "score_hyperparameters": {},
        "admission_hyperparameters": {},
        "score_loss": "NONE_CONSTANT",
        "admission_loss": "NONE_CONSTANT",
        "predicts": "THE_EQUAL_BAM_WEIGHTED_MEAN_ADMITTED_SCORE_FOR_EVERY_CELL",
        "score_fit_data": "ADMITTED_EXAMPLES_IN_THE_FOLD_TRAINING_BAMS",
        # every config scores identically, so selection must not depend on iteration order
        "tie_break": "LOWEST_CONFIG_HASH_LEXICOGRAPHIC",
        "transform_specification": {},
    },
    {
        "family": "CONFIG_ONLY",
        "score_implementation": "minos_engine.models.references.ConfigOnlyRidge",
        "admission_implementation": "minos_engine.models.references.ConfigOnlyRidge",
        "score_hyperparameters": {"alpha": 1.0},
        "admission_hyperparameters": {"max_iter": 1000},
        "score_loss": "squared_error",
        "admission_loss": "log_loss",
        "predicts": "E[S|A] AND P(A) FROM THE 28 CONFIG COLUMNS ALONE",
        "score_fit_data": "ADMITTED_EXAMPLES_IN_THE_FOLD_TRAINING_BAMS",
        "tie_break": "LOWEST_CONFIG_HASH_LEXICOGRAPHIC",
        "transform_specification": {"standardize": True, "columns": "CONFIG_ONLY_28"},
    },
    {
        "family": "BAM_FEATURES_ONLY",
        "score_implementation": "minos_engine.models.references.BamFeaturesOnlyRidge",
        "admission_implementation": "minos_engine.models.references.BamFeaturesOnlyRidge",
        "score_hyperparameters": {"alpha": 1.0},
        "admission_hyperparameters": {"max_iter": 1000},
        "score_loss": "squared_error",
        "admission_loss": "log_loss",
        # blind to the config on purpose: it measures how much of the score is just "which BAM"
        "predicts": "E[S|A] AND P(A) FROM THE 129 BAM COLUMNS ALONE",
        "score_fit_data": "ADMITTED_EXAMPLES_IN_THE_FOLD_TRAINING_BAMS",
        "tie_break": "LOWEST_CONFIG_HASH_LEXICOGRAPHIC",
        "transform_specification": {"standardize": True, "columns": "BAM_FEATURES_ONLY_129"},
    },
)


def _fixed_scientific_fields(dataset: TrainingDataset) -> dict[str, Any]:
    """Everything a caller must NOT be able to choose."""
    return {
        "target_formulation": TARGET_FORMULATION,
        "feature_schema_hash": FROZEN_FEATURE_SET_HASH,
        "config_schema_hash": build_config_encoding().identity(),
        "weighting_policy": WEIGHTING_POLICY,
        "dedup_policy": DEDUP_POLICY,
        "score_training_population": SCORE_TRAINING_POPULATION,
        "admission_training_population": ADMISSION_TRAINING_POPULATION,
        "score_output_postprocess": SCORE_OUTPUT_POSTPROCESS,
        "admission_probability_calibration": CALIBRATION_POLICY["scheme"],
        "ood_method": _OOD_METHOD,
        "random_seed": RANDOM_SEED,
        "training_dataset_hash": dataset.identity(),
        "cv_manifest_hash": dataset.cv_manifest.identity(),
    }


def _guard(dataset: TrainingDataset) -> None:
    if dataset.training_protocol_hash != compute_training_protocol_hash():
        raise ModelSpecFactoryError(
            "the dataset was not built under the frozen training protocol; specs derived from it "
            "would cite a protocol that no longer describes this campaign"
        )
    if dataset.weighting_policy_name() != WEIGHTING_POLICY:
        raise ModelSpecFactoryError("the dataset does not carry EQUAL_BAM_TOTAL weighting")
    if dataset.dedup_policy_name() != DEDUP_POLICY:
        raise ModelSpecFactoryError("the dataset does not carry the scientific dedup policy")
    verify_training_runtime()


def build_accepted_l2g_model_specs(dataset: TrainingDataset) -> tuple[ModelSpec, ...]:
    """The finite candidate set, bound to THIS dataset. No caller-authored science."""
    _guard(dataset)
    fixed = _fixed_scientific_fields(dataset)
    specs = []
    for recipe in CANDIDATE_GRID:
        family = str(recipe["family"])
        if family not in PROMOTABLE_FAMILIES:
            raise ModelSpecFactoryError(
                f"{family!r} is not promotable; a reference cannot be a candidate"
            )
        score_name = _IMPLEMENTATIONS[str(recipe["score"])]
        admission_name = _IMPLEMENTATIONS[str(recipe["admission"])]
        score_hp, admission_hp = _split_hyperparameters(family, dict(recipe["hyperparameters"]))
        specs.append(
            ModelSpec(
                family=family,
                score_model_implementation=score_name,
                admission_model_implementation=admission_name,
                score_hyperparameters=score_hp,
                admission_hyperparameters=admission_hp,
                score_loss="squared_error",
                admission_loss="log_loss",
                transform_specification={"standardize": True, "columns": "CONTEXTUAL_157"},
                **fixed,
            )
        )
    if len({s.identity() for s in specs}) != len(specs):
        raise ModelSpecFactoryError("two candidate recipes produced the same spec identity")
    return tuple(specs)


def build_accepted_l2g_reference_specs(dataset: TrainingDataset) -> tuple[ModelSpec, ...]:
    """The frozen reference set the promotion threshold is measured against."""
    _guard(dataset)
    fixed = _fixed_scientific_fields(dataset)
    specs = []
    for recipe in REFERENCE_RECIPES:
        family = str(recipe["family"])
        if family not in REFERENCE_FAMILIES:
            raise ModelSpecFactoryError(f"{family!r} is not a reference family")
        overrides = dict(fixed)
        # a constant predictor has nothing to calibrate; claiming ISOTONIC would be a fiction
        if family in ("CONSTANT_SAFE_BASELINE", "GLOBAL_MEAN"):
            overrides["admission_probability_calibration"] = "NONE_CONSTANT_PREDICTOR"
        specs.append(
            ModelSpec(
                family=family,
                score_model_implementation=str(recipe["score_implementation"]),
                admission_model_implementation=str(recipe["admission_implementation"]),
                score_hyperparameters=dict(recipe["score_hyperparameters"]),
                admission_hyperparameters=dict(recipe["admission_hyperparameters"]),
                score_loss=str(recipe["score_loss"]),
                admission_loss=str(recipe["admission_loss"]),
                transform_specification={
                    **dict(recipe["transform_specification"]),
                    "predicts": recipe["predicts"],
                    "score_fit_data": recipe["score_fit_data"],
                    "tie_break": recipe["tie_break"],
                },
                **overrides,
            )
        )
    if len({s.identity() for s in specs}) != len(specs):
        raise ModelSpecFactoryError("two reference recipes produced the same spec identity")
    return tuple(specs)
