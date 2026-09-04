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
    PROMOTABLE_FAMILIES,
    REFERENCE_FAMILIES,
    ModelSpec,
)

__all__ = [
    "REFERENCE_RECIPES",
    "ModelSpecFactoryError",
    "build_accepted_l2g_model_specs",
    "build_accepted_l2g_reference_specs",
]

_OOD_METHOD: Final = "STANDARDIZED_FEATURE_DISTANCE"
_FAILURE_RISK: Final = "LOGISTIC_P_ADMISSION"


class ModelSpecFactoryError(MinosEngineError):
    """A candidate specification was not produced by the accepted factory."""


#: Each reference is fully executable: what it predicts, what it fits on, whether it has an
#: admission component, and how it breaks ties. "Predict the safe baseline" is otherwise three
#: different models depending on who implements it.
REFERENCE_RECIPES: Final[tuple[dict[str, Any], ...]] = (
    {
        "family": "CONSTANT_SAFE_BASELINE",
        "implementation": "minos_engine.models.references.ConstantSafeBaseline",
        "predicts": "THE_SAFE_BASELINE_CONFIG_FOR_EVERY_BAM",
        "score_fit_data": "NONE_IT_IS_CONSTANT",
        "admission_component": "CONSTANT_EMPIRICAL_ADMISSION_RATE_OF_THE_SAFE_BASELINE_ON_TRAIN",
        "tie_break": "NOT_APPLICABLE_SINGLE_CONFIG",
        "transform_specification": {},
        "loss": "NONE",
    },
    {
        "family": "GLOBAL_MEAN",
        "implementation": "minos_engine.models.references.GlobalMean",
        "predicts": "THE_EQUAL_BAM_WEIGHTED_MEAN_ADMITTED_SCORE_FOR_EVERY_CELL",
        "score_fit_data": "ADMITTED_EXAMPLES_IN_THE_FOLD_TRAINING_BAMS",
        "admission_component": "EQUAL_BAM_WEIGHTED_ADMISSION_RATE_IN_THE_FOLD_TRAINING_BAMS",
        # every config scores identically, so the choice must not depend on dict ordering
        "tie_break": "LOWEST_CONFIG_HASH_LEXICOGRAPHIC",
        "transform_specification": {},
        "loss": "NONE",
    },
    {
        "family": "CONFIG_ONLY",
        "implementation": "minos_engine.models.references.ConfigOnlyRidge",
        "predicts": "E[S|A] AND P(A) FROM THE 28 CONFIG COLUMNS ALONE",
        "score_fit_data": "ADMITTED_EXAMPLES_IN_THE_FOLD_TRAINING_BAMS",
        "admission_component": "LOGISTIC_ON_CONFIG_COLUMNS_ONLY",
        "tie_break": "LOWEST_CONFIG_HASH_LEXICOGRAPHIC",
        "transform_specification": {"standardize": True, "columns": "CONFIG_ONLY"},
        "loss": "squared_error",
    },
    {
        "family": "BAM_FEATURES_ONLY",
        "implementation": "minos_engine.models.references.BamFeaturesOnlyRidge",
        "predicts": "E[S|A] AND P(A) FROM THE 129 BAM COLUMNS ALONE",
        # deliberately blind to the config: it measures how much of the score is just "which BAM"
        "score_fit_data": "ADMITTED_EXAMPLES_IN_THE_FOLD_TRAINING_BAMS",
        "admission_component": "LOGISTIC_ON_BAM_COLUMNS_ONLY",
        "tie_break": "LOWEST_CONFIG_HASH_LEXICOGRAPHIC",
        "transform_specification": {"standardize": True, "columns": "BAM_FEATURES_ONLY"},
        "loss": "squared_error",
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
        "failure_risk_formulation": _FAILURE_RISK,
        "calibration_method": CALIBRATION_POLICY["scheme"],
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
        if recipe["family"] not in PROMOTABLE_FAMILIES:
            raise ModelSpecFactoryError(
                f"{recipe['family']!r} is not promotable; a reference cannot be a candidate"
            )
        specs.append(
            ModelSpec(
                family=str(recipe["family"]),
                implementation=f"sklearn:{recipe['score']}+{recipe['admission']}",
                transform_specification={"standardize": True, "columns": "ALL"},
                hyperparameters=dict(sorted(dict(recipe["hyperparameters"]).items())),
                loss="squared_error",
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
        if recipe["family"] not in REFERENCE_FAMILIES:
            raise ModelSpecFactoryError(f"{recipe['family']!r} is not a reference family")
        overrides = dict(fixed)
        overrides["failure_risk_formulation"] = str(recipe["admission_component"])
        # a constant reference has nothing to calibrate; saying ISOTONIC would be a fiction
        if recipe["family"] == "CONSTANT_SAFE_BASELINE":
            overrides["calibration_method"] = "NONE_CONSTANT_PREDICTOR"
        specs.append(
            ModelSpec(
                family=str(recipe["family"]),
                implementation=str(recipe["implementation"]),
                transform_specification={
                    **dict(recipe["transform_specification"]),
                    "predicts": recipe["predicts"],
                    "score_fit_data": recipe["score_fit_data"],
                    "tie_break": recipe["tie_break"],
                },
                hyperparameters={},
                loss=str(recipe["loss"]),
                **overrides,
            )
        )
    if len({s.identity() for s in specs}) != len(specs):
        raise ModelSpecFactoryError("two reference recipes produced the same spec identity")
    return tuple(specs)
