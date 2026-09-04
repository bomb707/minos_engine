"""``l2g-model-training-protocol-v1`` — the rules, frozen before any TRAIN result decides anything.

Two properties matter more than the contents.

**The candidate set is finite and named before fitting.** With 50 independent BAMs an open-ended
search would find whatever the folds happened to reward; a small grid, hashed in advance, cannot.

**VALIDATION never chooses the rules used to judge it.** Numeric performance thresholds cannot
honestly be fixed before any out-of-fold number exists, so this protocol freezes a two-stage rule
instead: TRAIN OOF may derive the shortlist and thresholds using only the predeclared formula
below, and a SEPARATE source freeze must then bind the exact shortlisted specs and the exact
resulting thresholds BEFORE the first VALIDATION score is read. That keeps the decision procedure
fixed while letting its parameters come from evidence that is legitimately available.
"""

from __future__ import annotations

from typing import Any, Final

from minos_engine.common.canonical_json import canonical_json_bytes
from minos_engine.common.hashing import sha256_hex
from minos_engine.models.contract import (
    DEDUP_POLICY,
    FEATURE_COLUMN_COUNT,
    FROZEN_FEATURE_SET_HASH,
    TARGET_FORMULATION,
    WEIGHTING_POLICY,
    compute_training_contract_hash,
)
from minos_engine.models.spec import (
    PROMOTABLE_FAMILIES,
    REFERENCE_FAMILIES,
    SELECTION_ORDER,
)

__all__ = [
    "CANDIDATE_GRID",
    "CVAR_ALPHA",
    "MODEL_BACKEND",
    "TRAINING_PROTOCOL_DOMAIN",
    "TRAINING_PROTOCOL_SCHEMA",
    "compute_training_protocol_hash",
    "training_protocol_content",
]

TRAINING_PROTOCOL_SCHEMA: Final = "l2g-model-training-protocol-v1"
TRAINING_PROTOCOL_DOMAIN: Final = "minos:l2g-model-training-protocol:v1\n"

#: The minimum reproducible CPU backend. Verified locally on Python 3.12: Ridge, LogisticRegression
#: and the HistGradientBoosting pair all accept ``sample_weight`` and ``random_state``, which the
#: EQUAL_BAM_TOTAL weighting requires. No GPU stack is introduced for 50 independent BAMs.
MODEL_BACKEND: Final[dict[str, str]] = {
    "library": "scikit-learn",
    "constraint": ">=1.5,<2",
    "verified_version": "1.9.0",
    "python": "3.12",
    "serialization": "joblib",
}

CVAR_ALPHA: Final = 0.25

#: The finite grid. Every entry becomes a hashed ModelSpec before it is fitted; there is no
#: adaptive search. ``COMPACT_MLP`` is present because the backend was verified to honour sample
#: weights empirically, not merely to accept the argument -- but it stays last, and the data
#: density argues against it.
CANDIDATE_GRID: Final[tuple[dict[str, Any], ...]] = (
    {
        "family": "LINEAR_REGULARIZED",
        "score": "Ridge",
        "admission": "LogisticRegression",
        "hyperparameters": {"alpha": 0.1, "C": 1.0},
    },
    {
        "family": "LINEAR_REGULARIZED",
        "score": "Ridge",
        "admission": "LogisticRegression",
        "hyperparameters": {"alpha": 1.0, "C": 1.0},
    },
    {
        "family": "LINEAR_REGULARIZED",
        "score": "Ridge",
        "admission": "LogisticRegression",
        "hyperparameters": {"alpha": 10.0, "C": 0.1},
    },
    {
        "family": "TREE_ENSEMBLE",
        "score": "HistGradientBoostingRegressor",
        "admission": "HistGradientBoostingClassifier",
        "hyperparameters": {"max_depth": 3, "max_iter": 200, "learning_rate": 0.05},
    },
    {
        "family": "TREE_ENSEMBLE",
        "score": "HistGradientBoostingRegressor",
        "admission": "HistGradientBoostingClassifier",
        "hyperparameters": {"max_depth": 2, "max_iter": 100, "learning_rate": 0.1},
    },
    {
        "family": "COMPACT_MLP",
        "score": "MLPRegressor",
        "admission": "MLPClassifier",
        "hyperparameters": {"hidden_layer_sizes": [16], "max_iter": 500, "alpha": 1.0},
    },
)

RANDOM_SEED: Final = 20260904


def training_protocol_content() -> dict[str, Any]:
    """Exactly what ``training_protocol_hash`` covers."""
    return {
        "backend": dict(sorted(MODEL_BACKEND.items())),
        "calibration": "ISOTONIC_ON_TRAIN_OOF_ONLY",
        "candidate_grid": [dict(sorted(c.items())) for c in CANDIDATE_GRID],
        "cvar_alpha": CVAR_ALPHA,
        "dedup_policy": DEDUP_POLICY,
        "downside_metrics": ["mean_regret", "max_regret", "cvar_regret", "zero_regret_fraction"],
        "feature_column_count": FEATURE_COLUMN_COUNT,
        "feature_set_hash": FROZEN_FEATURE_SET_HASH,
        "hpo": "FINITE_PREDECLARED_GRID_NO_ADAPTIVE_SEARCH",
        "ood_method": "STANDARDIZED_FEATURE_DISTANCE",
        "promotable_families": list(PROMOTABLE_FAMILIES),
        "random_seed": RANDOM_SEED,
        "reference_families": list(REFERENCE_FAMILIES),
        "regret_orientation": "ORACLE_MINUS_SELECTED_LOWER_IS_BETTER",
        "schema_version": TRAINING_PROTOCOL_SCHEMA,
        "selection_order": list(SELECTION_ORDER),
        "target_formulation": TARGET_FORMULATION,
        "threshold_rule": {
            "stage_1": "TRAIN_OOF_MAY_DERIVE_SHORTLIST_AND_THRESHOLDS_BY_THIS_FORMULA_ONLY",
            "formula": (
                "shortlist = promotable specs whose OOF mean regret <= best reference mean "
                "regret AND whose OOF CVaR-0.25 regret <= best reference CVaR regret"
            ),
            "stage_2": "SEPARATE_SOURCE_FREEZE_BINDS_SHORTLIST_AND_THRESHOLDS",
            "invariant": "VALIDATION_NEVER_CHOOSES_THE_RULES_USED_TO_JUDGE_IT",
        },
        "training_contract_hash": compute_training_contract_hash(),
        "validation_label_domain": (
            "FOUR_FROZEN_FINALISTS_ON_TEN_BAMS_ONLY -- cannot validate unseen configs"
        ),
        "validation_use": "SELECTION_ONLY_AFTER_STAGE_2_FREEZE",
        "weighting_policy": WEIGHTING_POLICY,
        "test_lock": "SEALED_UNTIL_L2_I",
    }


def compute_training_protocol_hash() -> str:
    """The domain-separated identity of the frozen training protocol."""
    return sha256_hex(
        TRAINING_PROTOCOL_DOMAIN.encode("utf-8") + canonical_json_bytes(training_protocol_content())
    )
