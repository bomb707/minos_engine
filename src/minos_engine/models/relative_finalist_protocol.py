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
    "FINAL_TRAIN_BUNDLE_RULE",
    "FUTURE_VALIDATION_RULE",
    "HARMFUL_SWITCH_DEFINITION",
    "NUMPY_QUANTILE_METHOD",
    "QUANTILE_METHOD",
    "TRANSFORM_POLICY",
    "WEIGHTING_POLICY",
    "RELATIVE_PROTOCOL_DOMAIN",
    "RELATIVE_PROTOCOL_SCHEMA",
    "SWITCH_RULE",
    "V2_CANDIDATE_GRID",
    "V2_REFERENCES",
    "compute_relative_protocol_hash",
    "relative_protocol_content",
]

RELATIVE_PROTOCOL_SCHEMA: Final = "l2g-relative-finalist-protocol-v2"
RELATIVE_PROTOCOL_DOMAIN: Final = "minos:l2g-relative-finalist-protocol:v2\n"
SPEC_SCHEMA: Final = "l2g-relative-finalist-spec-v2"
SPEC_DOMAIN: Final = "minos:l2g-relative-finalist-spec:v2\n"
#: v1 named INNER_OOF_RESIDUAL_MARGIN but did not pin the inner folds, the residual pool, the
#: quantile algorithm, the per-family transforms or the strictness of the switch comparison --
#: enough freedom that two faithful implementations could produce different margins. Nothing was
#: ever fitted under it.
SUPERSEDED_PROTOCOL_V1: Final = "SUPERSEDED_BEFORE_FIRST_V2_MODEL_FIT"
SUPERSEDED_SPEC_V1: Final = "SUPERSEDED_BEFORE_FIRST_V2_MODEL_FIT"

RANDOM_SEED: Final = 20260904
CVAR_ALPHA: Final = 0.25
#: ceil(0.25 * 50) = 13, the same finite-sample convention as the frozen baseline objective
CVAR_TAIL_RULE: Final = "CEIL_ALPHA_TIMES_N"

#: ONE policy family, chosen before any v2 fit. The margin is learned inside outer-training data
#: and applied untouched to the held-out chromosome; the quantile is predeclared, not tuned.
#: ``higher``, not the default interpolation. The margin is a safety threshold under strongly
#: asymmetric switch losses, so it takes the next OBSERVED residual rather than interpolating
#: downward between two of them. (layer1/coverage.py uses ``linear`` for descriptive coverage
#: percentiles; that is a summary statistic, not a threshold, so the two do not conflict.)
QUANTILE_METHOD: Final = "HIGHER"
NUMPY_QUANTILE_METHOD: Final = "higher"

INNER_FOLD_COUNT: Final = 4
OUTER_TRAINING_BAMS: Final = 40
OUTER_HELD_BAMS: Final = 10
INNER_RESIDUAL_COUNT: Final = OUTER_TRAINING_BAMS * 3
OUTER_TRAINING_ROWS: Final = OUTER_TRAINING_BAMS * 3
OUTER_HELD_ROWS: Final = OUTER_HELD_BAMS * 3

SWITCH_RULE: Final[dict[str, Any]] = {
    "family": "INNER_OOF_RESIDUAL_MARGIN",
    "inner_cv": (
        "leave-one-remaining-chromosome-out over the four chromosomes left in the outer "
        "training side; every one of the 40 outer-training BAMs is predicted exactly once by an "
        "inner model that did not train on it"
    ),
    "inner_fold_count": INNER_FOLD_COUNT,
    "inner_residual_count": INNER_RESIDUAL_COUNT,
    "residual": "abs(predicted_delta - actual_delta)",
    "residual_pool": "ALL_40_OUTER_TRAINING_BAMS_X_ALL_THREE_ALTERNATIVES",
    "not_pooled_per_config": True,
    "not_pooled_per_bam": True,
    "not_positive_only": True,
    "margins_per_spec": "ONE_SCALAR_PER_OUTER_FOLD",
    "quantile_method": QUANTILE_METHOD,
    "comparison": "STRICT_GREATER_THAN",
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
    # equality keeps the baseline: at the margin the evidence does not distinguish the actions,
    # and the asymmetry of switch losses makes the fallback the right side of a tie
    "predicted_equal_to_margin_keeps_safe_baseline": True,
}

#: per-family, because standardisation helps a linear model and does nothing for a tree
TRANSFORM_POLICY: Final[dict[str, Any]] = {
    "RELATIVE_RIDGE_SHARED": {
        "transform": "STANDARD_SCALER",
        "columns": "ALL_157",
        "fitted_on": "THE_CURRENT_TRAINING_SIDE_ONLY",
        "inner_fits_on": "INNER_TRAINING_ROWS_ONLY",
        "outer_fits_on": "OUTER_TRAINING_ROWS_ONLY",
        "deployment_fits_on": "ALL_150_TRAIN_ROWS",
    },
    "RELATIVE_HISTGB_SHARED": {
        "transform": "NONE",
        "columns": "ALL_157",
        "rationale": "a histogram-binned tree is invariant to monotone rescaling",
    },
}

WEIGHTING_POLICY: Final[dict[str, Any]] = {
    "policy": "EQUAL_BAM_TOTAL",
    "row_weight": 1.0 / 3.0,
    "bam_total_weight": 1.0,
    "applies_to": "EVERY_INNER_FIT_EVERY_OUTER_FIT_AND_THE_DEPLOYMENT_FIT",
    "sample_weight_required": True,
}

#: v1's oof_metrics carries CATASTROPHIC_MARGIN = 0.05. It is NOT reused here: that number was
#: chosen without independent justification, and inventing a severity threshold merely to have a
#: metric would be worse than counting the thing that is actually defined. A harmful switch is a
#: switch whose realised advantage is negative; severity stays continuous.
HARMFUL_SWITCH_DEFINITION: Final[dict[str, Any]] = {
    "harmful_switch": "a switch whose ACTUAL delta for the selected finalist is < 0",
    "count_metric": "harmful_switch_count",
    "severity_metrics": ["mean_loss_on_harmful_switch", "worst_switch_delta"],
    "catastrophic_threshold_invented": False,
    "v1_catastrophic_margin_reused": False,
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
    "mean_loss_on_harmful_switch",
    "harmful_switch_count",
    "worst_switch_delta",
)
V2_DIAGNOSTICS: Final[tuple[str, ...]] = ("delta_mae", "delta_rmse", "delta_r2", "delta_spearman")

#: how a TRAIN-shortlisted spec becomes a deployable model. Frozen BEFORE any OOF result, so the
#: deployment margin cannot be chosen once the numbers are visible.
FINAL_TRAIN_BUNDLE_RULE: Final[dict[str, Any]] = {
    "applies_to": "EVERY_SPEC_IN_THE_FROZEN_TRAIN_SHORTLIST",
    "step_1": "generate the full five-fold TRAIN OOF delta predictions over all 150 rows",
    "step_2": (
        "derive ONE deployment margin from those 150 OOF absolute residuals using the spec's "
        "frozen margin_quantile and the frozen quantile method"
    ),
    "step_3": "fit the final estimator on all 50 BAMs / 150 relative rows",
    "step_4": "fit the final transform, where the family has one, on all 150 TRAIN rows",
    "step_5": "persist estimator, transform, margin, domain, schemas, runtime and identities",
    "validation_labels_in_bundle": False,
}

#: frozen now, while VALIDATION is still unread, so the bar cannot be set to fit the result.
FUTURE_VALIDATION_RULE: Final[dict[str, Any]] = {
    "precondition": "A_NON_EMPTY_FROZEN_TRAIN_SHORTLIST",
    "action_domain": "THE_SAME_FOUR_FINALISTS",
    "labels": "EXISTING_PHASE_D_OUTCOMES_10_BAMS_X_4_FINALISTS",
    "new_gatk_execution_authorized": False,
    "inference": "USE_THE_FINAL_TRAIN_BUNDLE_UNCHANGED",
    "refit_on_validation": False,
    "recalibrate_margin_on_validation": False,
    "cvar_alpha": CVAR_ALPHA,
    "cvar_tail_count_n10": 3,
    "bar": (
        "validation mean_regret <= validation ALWAYS_SAFE_BASELINE mean_regret AND validation "
        "cvar_regret <= validation ALWAYS_SAFE_BASELINE cvar_regret"
    ),
    "ties_admitted": True,
    "no_rescue_by_secondary_diagnostics": True,
    "tie_break_order": [
        "LOWER_VALIDATION_MEAN_REGRET",
        "LOWER_VALIDATION_CVAR_REGRET",
        "LOWER_TRAIN_MEAN_REGRET",
        "LOWER_TRAIN_CVAR_REGRET",
        "LEXICAL_MODEL_SPEC_HASH",
    ],
    "if_none_clear_both_bars": "MODELS_QUALIFIED_REMAINS_HOLD",
}


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
        "quantile_method": QUANTILE_METHOD,
        "inner_fold_count": INNER_FOLD_COUNT,
        "inner_residual_count": INNER_RESIDUAL_COUNT,
        "outer_training_bams": OUTER_TRAINING_BAMS,
        "outer_held_bams": OUTER_HELD_BAMS,
        "outer_training_rows": OUTER_TRAINING_ROWS,
        "outer_held_rows": OUTER_HELD_ROWS,
        "transform_policy": {
            k: dict(sorted(v.items())) for k, v in sorted(TRANSFORM_POLICY.items())
        },
        "weighting_policy": dict(sorted(WEIGHTING_POLICY.items())),
        "harmful_switch_definition": dict(sorted(HARMFUL_SWITCH_DEFINITION.items())),
        "final_train_bundle_rule": dict(sorted(FINAL_TRAIN_BUNDLE_RULE.items())),
        "future_validation_rule": dict(sorted(FUTURE_VALIDATION_RULE.items())),
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
    family = str(recipe["family"])
    return {
        "schema_version": SPEC_SCHEMA,
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
        "quantile_method": QUANTILE_METHOD,
        "inner_cv": SWITCH_RULE["inner_cv"],
        "inner_fold_count": INNER_FOLD_COUNT,
        "inner_residual_count": INNER_RESIDUAL_COUNT,
        "residual_pool": SWITCH_RULE["residual_pool"],
        "comparison": SWITCH_RULE["comparison"],
        "transform_policy": dict(sorted(TRANSFORM_POLICY[family].items())),
        "random_seed": RANDOM_SEED,
        "weighting_policy": dict(sorted(WEIGHTING_POLICY.items())),
        "cv_protocol": "BAM_GROUPED_CHROMOSOME_HELD_OUT_FIVE_FOLDS",
        "training_runtime_hash": compute_training_runtime_hash(),
        "training_dataset_hash": dataset_identity,
        "relative_protocol_hash": compute_relative_protocol_hash(),
    }


def build_v2_spec_hashes(dataset_identity: str) -> tuple[str, ...]:
    """The four candidate identities, all of which exist BEFORE any v2 fit."""
    return tuple(
        sha256_hex(
            SPEC_DOMAIN.encode("utf-8")
            + canonical_json_bytes(build_v2_spec_content(r, dataset_identity=dataset_identity))
        )
        for r in V2_CANDIDATE_GRID
    )
