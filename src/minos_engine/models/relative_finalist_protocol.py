"""``l2g-relative-finalist-protocol-v1`` — the frozen L2-G v2 decision procedure.

The TRAIN feasibility audit, run before this protocol was written, says what v2 is up against:
across the 50 BAMs the safe baseline is strictly best on 34 and beaten on 16, and a PERFECT
four-finalist oracle would gain only 0.0150 mean utility. Each alternative loses far more often
than it wins (8/50, 5/50, 6/50), and its average loss when wrong is several times its average gain
when right.

Two consequences are built into this protocol rather than discovered later.

**Switching must be rare and right.** A rule of "switch whenever predicted advantage is positive"
would switch on prediction noise, and with this loss asymmetry that is worse than never switching
at all. The frozen rule therefore requires the predicted advantage to clear a margin learned from
the model's own inner out-of-fold residuals — inside outer-training data only.

**Capacity is not the answer.** Fifty independent BAMs and 150 advantage examples do not support a
larger model than v1 used, and v1's failure was a decision failure, not an accuracy failure. The
grid is two estimator families times two margin quantiles: four specs, all named here.
"""

from __future__ import annotations

from typing import Any, Final

from minos_engine.common.canonical_json import canonical_json_bytes
from minos_engine.common.hashing import sha256_hex
from minos_engine.models.contract import CV_FOLD_CHROMOSOMES
from minos_engine.models.relative_finalist_contract import (
    FINALIST_DOMAIN,
    RELATIVE_TARGET,
    RESEARCH_PROTOCOL_VERSION,
    SAFE_BASELINE_CONFIG_HASH,
    compute_finalist_domain_hash,
    compute_relative_contract_hash,
)
from minos_engine.models.runtime import compute_training_runtime_hash

__all__ = [
    "RELATIVE_PROTOCOL_DOMAIN",
    "RELATIVE_PROTOCOL_SCHEMA",
    "SWITCH_RULE",
    "V2_CANDIDATE_GRID",
    "V2_REFERENCES",
    "compute_relative_protocol_hash",
    "relative_protocol_content",
]

RELATIVE_PROTOCOL_SCHEMA: Final = "l2g-relative-finalist-protocol-v1"
RELATIVE_PROTOCOL_DOMAIN: Final = "minos:l2g-relative-finalist-protocol:v1\n"

RANDOM_SEED: Final = 20260904
CVAR_ALPHA: Final = 0.25
#: ceil(0.25 * 50) = 13, the same finite-sample convention as the frozen baseline objective
CVAR_TAIL_RULE: Final = "CEIL_ALPHA_TIMES_N"

#: ONE policy family, chosen before any v2 fit. The margin is learned inside outer-training data
#: and applied untouched to the held-out chromosome; the quantile is predeclared, not tuned.
SWITCH_RULE: Final[dict[str, Any]] = {
    "family": "INNER_OOF_RESIDUAL_MARGIN",
    "action_domain": "THE_FOUR_FROZEN_FINALISTS",
    "default_action": "SAFE_BASELINE",
    "rule": (
        "switch to argmax predicted advantage iff that predicted advantage > margin; otherwise "
        "keep SAFE_BASELINE"
    ),
    "margin_definition": (
        "the margin_quantile-th quantile of |predicted - actual| advantage residuals on INNER "
        "BAM-grouped out-of-fold predictions drawn from the outer-training BAMs only"
    ),
    "margin_fitted_on": "OUTER_TRAINING_BAMS_ONLY",
    "applied_to": "THE_HELD_OUT_CHROMOSOME",
    "tie_break": "LOWEST_CONFIG_HASH_LEXICOGRAPHIC",
    "safe_baseline_always_available": True,
    "never_forced_to_switch": True,
}

#: two low-capacity families x two predeclared margins. No adaptive search, no HPO library.
V2_CANDIDATE_GRID: Final[tuple[dict[str, Any], ...]] = (
    {
        "family": "RELATIVE_RIDGE_SHARED",
        "implementation": "sklearn.linear_model.Ridge",
        "hyperparameters": {"alpha": 1.0},
        "margin_quantile": 0.75,
    },
    {
        "family": "RELATIVE_RIDGE_SHARED",
        "implementation": "sklearn.linear_model.Ridge",
        "hyperparameters": {"alpha": 1.0},
        "margin_quantile": 0.90,
    },
    {
        "family": "RELATIVE_HISTGB_SHARED",
        "implementation": "sklearn.ensemble.HistGradientBoostingRegressor",
        "hyperparameters": {"max_depth": 2, "max_iter": 100, "learning_rate": 0.05},
        "margin_quantile": 0.75,
    },
    {
        "family": "RELATIVE_HISTGB_SHARED",
        "implementation": "sklearn.ensemble.HistGradientBoostingRegressor",
        "hyperparameters": {"max_depth": 2, "max_iter": 100, "learning_rate": 0.05},
        "margin_quantile": 0.90,
    },
)

#: ALWAYS_SAFE_BASELINE is the deployable reference and therefore the promotion bar. ORACLE4 is
#: recorded as an upper bound only -- it needs the held-out answer and can never be deployed, so
#: promoting against it would be measuring against something nobody can run.
V2_REFERENCES: Final[tuple[dict[str, Any], ...]] = (
    {
        "name": "ALWAYS_SAFE_BASELINE",
        "deployable": True,
        "is_promotion_bar": True,
        "policy": "select SAFE_BASELINE for every BAM",
    },
    {
        "name": "GLOBAL_BEST_FINALIST_FROM_OUTER_TRAIN",
        "deployable": True,
        "is_promotion_bar": False,
        "policy": "select the finalist with the best equal-BAM mean utility on outer-training BAMs",
    },
    {
        "name": "ORACLE4",
        "deployable": False,
        "is_promotion_bar": False,
        "policy": "select the best of the four using the held-out answer; UPPER BOUND ONLY",
    },
)

PROMOTION_RULE: Final[dict[str, Any]] = {
    "bar": "ALWAYS_SAFE_BASELINE",
    "rule": (
        "mean_regret <= ALWAYS_SAFE_BASELINE mean_regret AND cvar_regret <= "
        "ALWAYS_SAFE_BASELINE cvar_regret"
    ),
    "orientation": "ORACLE4_MINUS_SELECTED_LOWER_IS_BETTER",
    "ties_admitted": True,
    "not_weakened_because_v1_failed": True,
    "empty_shortlist_is_valid": True,
    "fallback_if_empty": "SAFE_BASELINE_REMAINS_AND_MODELS_QUALIFIED_HOLDS",
}

#: decision metrics first; prediction accuracy is a diagnostic. A model that predicts advantage
#: well and switches badly is not qualified -- that is precisely v1's lesson.
V2_METRICS: Final[tuple[str, ...]] = (
    "mean_regret",
    "max_regret",
    "cvar_regret",
    "zero_regret_fraction",
    "safe_baseline_kept_fraction",
    "switch_fraction",
    "switch_precision",
    "mean_gain_on_switch",
    "catastrophic_switch_count",
)
V2_DIAGNOSTICS: Final[tuple[str, ...]] = ("delta_mae", "delta_rmse", "delta_r2", "delta_spearman")


def relative_protocol_content() -> dict[str, Any]:
    return {
        "schema_version": RELATIVE_PROTOCOL_SCHEMA,
        "research_protocol_version": RESEARCH_PROTOCOL_VERSION,
        "relative_contract_hash": compute_relative_contract_hash(),
        "finalist_domain": list(FINALIST_DOMAIN),
        "finalist_domain_hash": compute_finalist_domain_hash(),
        "safe_baseline_config_hash": SAFE_BASELINE_CONFIG_HASH,
        "target": RELATIVE_TARGET,
        "candidate_grid": [dict(sorted(c.items())) for c in V2_CANDIDATE_GRID],
        "references": [dict(sorted(r.items())) for r in V2_REFERENCES],
        "switch_rule": dict(sorted(SWITCH_RULE.items())),
        "promotion_rule": dict(sorted(PROMOTION_RULE.items())),
        "metrics": list(V2_METRICS),
        "diagnostics": list(V2_DIAGNOSTICS),
        "cv_outer_folds": list(CV_FOLD_CHROMOSOMES),
        "cv_grouping": "BAM_GROUPED_CHROMOSOME_HELD_OUT",
        "transforms_fitted_on": "OUTER_TRAINING_BAMS_ONLY",
        "cvar_alpha": CVAR_ALPHA,
        "cvar_tail_rule": CVAR_TAIL_RULE,
        "random_seed": RANDOM_SEED,
        "training_runtime_hash": compute_training_runtime_hash(),
        "hpo": "FINITE_PREDECLARED_GRID_NO_ADAPTIVE_SEARCH",
        # v2's design was informed by v1 TRAIN evidence, so its own TRAIN OOF is development
        # evidence for this protocol -- not an untouched estimate of how well the protocol
        # DESIGN generalises. Saying otherwise would overclaim.
        "train_oof_status": "DEVELOPMENT_EVIDENCE_FOR_THIS_PROTOCOL",
        "validation_rule": (
            "VALIDATION stays unread through the feasibility audit, protocol construction, "
            "implementation, the v2 TRAIN OOF campaign and the v2 shortlist freeze; it may be "
            "read only if v2 freezes at least one promotable selector, and then once, over the "
            "same four finalists already evaluated on all ten VALIDATION BAMs"
        ),
        "new_validation_gatk_authorized": False,
        "test_lock": "SEALED_UNTIL_L2_I",
    }


def compute_relative_protocol_hash() -> str:
    return sha256_hex(
        RELATIVE_PROTOCOL_DOMAIN.encode("utf-8") + canonical_json_bytes(relative_protocol_content())
    )


def build_v2_spec_content(recipe: dict[str, Any], *, dataset_identity: str) -> dict[str, Any]:
    """One frozen v2 candidate specification, bound to the v2 dataset."""
    return {
        "schema_version": "l2g-relative-finalist-spec-v1",
        "family": str(recipe["family"]),
        "implementation": str(recipe["implementation"]),
        "hyperparameters": dict(sorted(dict(recipe["hyperparameters"]).items())),
        "target": RELATIVE_TARGET,
        "finalist_domain_hash": compute_finalist_domain_hash(),
        "safe_baseline_config_hash": SAFE_BASELINE_CONFIG_HASH,
        "feature_representation": "BAM_129_PLUS_CONFIG_DELTA_28",
        "config_delta_representation": "ENCODE(theta) - ENCODE(theta_safe)",
        "switch_rule_family": SWITCH_RULE["family"],
        "margin_quantile": float(recipe["margin_quantile"]),
        "random_seed": RANDOM_SEED,
        "weighting_policy": "EQUAL_BAM_TOTAL",
        "cv_protocol": "BAM_GROUPED_CHROMOSOME_HELD_OUT_FIVE_FOLDS",
        "training_runtime_hash": compute_training_runtime_hash(),
        "training_dataset_hash": dataset_identity,
        "relative_protocol_hash": compute_relative_protocol_hash(),
    }


def build_v2_spec_hashes(dataset_identity: str) -> tuple[str, ...]:
    """The four candidate identities, all of which exist BEFORE any v2 fit."""
    return tuple(
        sha256_hex(
            b"minos:l2g-relative-finalist-spec:v1\n"
            + canonical_json_bytes(build_v2_spec_content(r, dataset_identity=dataset_identity))
        )
        for r in V2_CANDIDATE_GRID
    )
