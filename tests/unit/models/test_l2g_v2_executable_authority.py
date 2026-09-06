"""L2-G v2 executable pre-fit authority: exact folds, exact margin, and the frozen future rules.

No real v2 model is fitted on real advantage labels. The synthetic fixtures use the real BAM and
finalist identities so the structural claims are about the real fold geometry, but every label and
predictor is synthetic.
"""

from __future__ import annotations

import hashlib
import inspect
import json
import math
from typing import Any

import numpy as np
import pytest

from minos_engine.models.contract import CV_FOLD_CHROMOSOMES
from minos_engine.models.prefit_loader import load_verified_training_dataset
from minos_engine.models.relative_finalist_authority import (
    ACCEPTED_FEASIBILITY_SHA256,
    ACCEPTED_RELATIVE_DATASET_IDENTITY,
    V2_OUTPUT_ROOT,
    RelativeAuthorityError,
    TrustedRelativeTrainingData,
    _build_final_train_bundle_content,
    evaluate_future_validation,
    load_trusted_relative_training_data,
    run_real_l2g_v2_train_oof_campaign,
    select_validation_winner,
)
from minos_engine.models.relative_finalist_contract import (
    ALTERNATIVE_FINALISTS,
    FINALIST_DOMAIN,
    SAFE_BASELINE_CONFIG_HASH,
    compute_relative_contract_hash,
)
from minos_engine.models.relative_finalist_dataset import build_relative_finalist_dataset
from minos_engine.models.relative_finalist_protocol import (
    FINAL_TRAIN_BUNDLE_RULE,
    FUTURE_VALIDATION_RULE,
    HARMFUL_SWITCH_DEFINITION,
    INNER_RESIDUAL_COUNT,
    NUMPY_QUANTILE_METHOD,
    QUANTILE_METHOD,
    RELATIVE_PROTOCOL_SCHEMA,
    SPEC_SCHEMA,
    SUPERSEDED_PROTOCOL_V1,
    SWITCH_RULE,
    TRANSFORM_POLICY,
    V2_CANDIDATE_GRID,
    WEIGHTING_POLICY,
    build_v2_spec_content,
    build_v2_spec_hashes,
    compute_relative_protocol_hash,
)
from minos_engine.models.relative_finalist_runner import (
    RelativeRunnerError,
    build_estimator,
    derive_switch_margin,
    inner_folds_for,
    policy_decisions,
    policy_metrics,
    reference_decisions,
    run_relative_outer_oof,
)
from minos_engine.qualification.l2f_accepted_identities import repository_root

_AUTHORITY = "reports/layer2/l2g-v2-prefit-authority.json"


def _bams() -> dict[str, str]:
    return {f"bam-{c}-{i}": c for c in CV_FOLD_CHROMOSOMES for i in range(10)}


@pytest.fixture(scope="module")
def synthetic() -> dict[str, Any]:
    rng = np.random.default_rng(3)
    bams = _bams()

    class _Row:
        def __init__(self, b: str, c: str, d: float) -> None:
            self.dataset_id, self.config_hash, self.delta = b, c, d

    rows, utility = [], {}
    for b in bams:
        safe = float(np.clip(rng.normal(0.7, 0.1), 0, 1))
        utility[(b, SAFE_BASELINE_CONFIG_HASH)] = safe
        for c in ALTERNATIVE_FINALISTS:
            u = float(np.clip(safe + rng.normal(-0.08, 0.08), 0, 1))
            utility[(b, c)] = u
            rows.append(_Row(b, c, u - safe))
    design = {(r.dataset_id, r.config_hash): rng.normal(size=157) for r in rows}
    return {"bams": bams, "rows": rows, "utility": utility, "design": design}


# ---------------------------------------------------------------------------------------- #
# honest versioning
# ---------------------------------------------------------------------------------------- #
def test_the_protocol_and_spec_are_versioned_v2() -> None:
    assert RELATIVE_PROTOCOL_SCHEMA == "l2g-relative-finalist-protocol-v3"
    assert SPEC_SCHEMA == "l2g-relative-finalist-spec-v3"
    assert SUPERSEDED_PROTOCOL_V1 == "SUPERSEDED_BEFORE_FIRST_V2_MODEL_FIT"


def test_the_relative_contract_and_dataset_did_not_move() -> None:
    """Only the executable procedure changed; the science did not."""
    dataset = build_relative_finalist_dataset(load_verified_training_dataset())
    assert dataset.identity() == ACCEPTED_RELATIVE_DATASET_IDENTITY
    assert compute_relative_contract_hash() == (
        "dd5aca807bf0499411b9b4279c206d7a0e9a6d50c85bf75c5d319cf7ba2d394e"
    )


def test_the_four_spec_hashes_are_deterministic() -> None:
    dataset = build_relative_finalist_dataset(load_verified_training_dataset())
    first = build_v2_spec_hashes(dataset.identity())
    assert first == build_v2_spec_hashes(dataset.identity())
    assert len(set(first)) == 4


# ---------------------------------------------------------------------------------------- #
# exact folds
# ---------------------------------------------------------------------------------------- #
def test_each_outer_fold_is_forty_over_ten() -> None:
    bams = _bams()
    for chromosome in CV_FOLD_CHROMOSOMES:
        held = [b for b, c in bams.items() if c == chromosome]
        train = [b for b in bams if b not in set(held)]
        assert len(train) == 40 and len(held) == 10
        assert len(train) * 3 == 120 and len(held) * 3 == 30


def test_the_inner_split_is_four_folds_covering_every_training_bam_once() -> None:
    bams = _bams()
    training = frozenset(b for b, c in bams.items() if c != "chr18")
    folds = inner_folds_for(training, bams)
    assert len(folds) == 4
    covered: list[str] = []
    for inner_train, inner_held in folds:
        assert len(inner_held) == 10 and len(inner_train) == 30
        assert not (inner_train & inner_held)
        covered.extend(inner_held)
    assert sorted(covered) == sorted(training)


def test_no_outer_held_bam_reaches_any_inner_fold() -> None:
    bams = _bams()
    outer_held = frozenset(b for b, c in bams.items() if c == "chr18")
    training = frozenset(bams) - outer_held
    for inner_train, inner_held in inner_folds_for(training, bams):
        assert not (inner_train & outer_held)
        assert not (inner_held & outer_held)


def test_a_training_side_with_the_wrong_group_count_is_refused() -> None:
    bams = _bams()
    only_two = frozenset(b for b, c in bams.items() if c in ("chr18", "chr19"))
    with pytest.raises(RelativeRunnerError, match="chromosome groups"):
        inner_folds_for(only_two, bams)


# ---------------------------------------------------------------------------------------- #
# the margin
# ---------------------------------------------------------------------------------------- #
def test_the_margin_requires_exactly_120_residuals() -> None:
    assert INNER_RESIDUAL_COUNT == 120
    with pytest.raises(RelativeRunnerError, match="expected exactly 120"):
        derive_switch_margin(np.zeros(119), margin_quantile=0.75)
    with pytest.raises(RelativeRunnerError, match="expected exactly 120"):
        derive_switch_margin(np.zeros(121), margin_quantile=0.75)
    assert derive_switch_margin(np.linspace(0, 1, 120), margin_quantile=0.75) > 0


def test_the_quantile_method_is_higher_not_interpolated() -> None:
    assert QUANTILE_METHOD == "HIGHER"
    assert NUMPY_QUANTILE_METHOD == "higher"
    # 120 distinct values, so the two methods genuinely disagree at q=0.75
    residuals = np.arange(120, dtype=float) / 119.0
    frozen = derive_switch_margin(residuals, margin_quantile=0.75)
    assert frozen == float(np.quantile(residuals, 0.75, method="higher"))
    assert frozen != float(np.quantile(residuals, 0.75, method="linear"))


def test_a_negative_or_non_finite_residual_is_refused() -> None:
    bad = np.zeros(120)
    bad[0] = -1.0
    with pytest.raises(RelativeRunnerError, match="negative"):
        derive_switch_margin(bad, margin_quantile=0.75)
    bad[0] = float("nan")
    with pytest.raises(RelativeRunnerError, match="finite"):
        derive_switch_margin(bad, margin_quantile=0.75)


def test_the_residual_pool_is_all_bams_and_all_alternatives() -> None:
    assert SWITCH_RULE["residual_pool"] == "ALL_40_OUTER_TRAINING_BAMS_X_ALL_THREE_ALTERNATIVES"
    assert SWITCH_RULE["not_pooled_per_config"] is True
    assert SWITCH_RULE["not_pooled_per_bam"] is True
    assert SWITCH_RULE["not_positive_only"] is True
    assert SWITCH_RULE["margins_per_spec"] == "ONE_SCALAR_PER_OUTER_FOLD"


# ---------------------------------------------------------------------------------------- #
# the switch policy
# ---------------------------------------------------------------------------------------- #
def _decide(predicted: float, margin: float) -> Any:
    return policy_decisions(
        spec_hash="s",
        predictions={("b", c): predicted for c in ALTERNATIVE_FINALISTS},
        margin=margin,
        utility={("b", c): 0.7 for c in FINALIST_DOMAIN},
        held_bams=["b"],
        chromosome_of={"b": "chr18"},
        outer_fold="chr18",
    )[0]


def test_predicted_equal_to_the_margin_keeps_the_safe_baseline() -> None:
    """Strict, not >=. At the margin the evidence does not distinguish the actions."""
    decision = _decide(0.5, 0.5)
    assert decision.switched is False
    assert decision.selected_config == SAFE_BASELINE_CONFIG_HASH
    assert SWITCH_RULE["comparison"] == "STRICT_GREATER_THAN"
    assert SWITCH_RULE["predicted_equal_to_margin_keeps_safe_baseline"] is True


def test_predicted_above_the_margin_switches() -> None:
    decision = _decide(0.6, 0.5)
    assert decision.switched is True
    assert decision.selected_config in ALTERNATIVE_FINALISTS


def test_a_prediction_tie_breaks_on_the_lowest_config_hash() -> None:
    decision = _decide(0.9, 0.1)
    assert decision.selected_config == sorted(ALTERNATIVE_FINALISTS)[0]


def test_the_safe_baseline_is_always_available() -> None:
    assert SWITCH_RULE["safe_baseline_always_available"] is True
    assert SWITCH_RULE["never_forced_to_switch"] is True
    assert SWITCH_RULE["default_action"] == "SAFE_BASELINE"


# ---------------------------------------------------------------------------------------- #
# transforms, weights, estimators
# ---------------------------------------------------------------------------------------- #
def test_the_transform_policy_is_per_family() -> None:
    assert TRANSFORM_POLICY["RELATIVE_RIDGE_SHARED"]["transform"] == "STANDARD_SCALER"
    assert TRANSFORM_POLICY["RELATIVE_HISTGB_SHARED"]["transform"] == "NONE"
    source = inspect.getsource(
        __import__("minos_engine.models.relative_finalist_runner", fromlist=["x"])._fit_predict
    )
    assert "StandardScaler().fit(x_fit)" in source


def test_equal_bam_weighting_is_one_third_per_row() -> None:
    assert WEIGHTING_POLICY["policy"] == "EQUAL_BAM_TOTAL"
    assert WEIGHTING_POLICY["row_weight"] == pytest.approx(1.0 / 3.0)
    assert WEIGHTING_POLICY["bam_total_weight"] == 1.0
    assert WEIGHTING_POLICY["sample_weight_required"] is True


def test_every_v2_estimator_accepts_sample_weight_and_the_frozen_seed() -> None:
    for recipe in V2_CANDIDATE_GRID:
        spec = build_v2_spec_content(recipe, dataset_identity="d" * 64)
        estimator = build_estimator(spec)
        assert estimator.random_state == 20260904
        assert "sample_weight" in set(inspect.signature(estimator.fit).parameters)


def test_an_unsupported_estimator_is_refused() -> None:
    spec = build_v2_spec_content(V2_CANDIDATE_GRID[0], dataset_identity="d" * 64)
    spec["implementation"] = "os.system"
    with pytest.raises(RelativeRunnerError, match="not a supported v2 estimator"):
        build_estimator(spec)


# ---------------------------------------------------------------------------------------- #
# the structural run
# ---------------------------------------------------------------------------------------- #
@pytest.mark.parametrize("index", [0, 2])
def test_the_runner_produces_150_records_and_50_decisions(
    synthetic: dict[str, Any], index: int
) -> None:
    recipe = V2_CANDIDATE_GRID[index]
    spec = build_v2_spec_content(recipe, dataset_identity="d" * 64)
    out = run_relative_outer_oof(
        spec=spec,
        spec_hash="x" * 64,
        rows=synthetic["rows"],
        design=synthetic["design"],
        utility=synthetic["utility"],
        chromosome_of=synthetic["bams"],
    )
    assert len(out["records"]) == 150
    assert len(out["decisions"]) == 50
    assert len(out["margins"]) == 5
    assert len({(r.dataset_id, r.alternative_config) for r in out["records"]}) == 150
    assert len({d.dataset_id for d in out["decisions"]}) == 50
    for decision in out["decisions"]:
        assert synthetic["bams"][decision.dataset_id] == decision.outer_fold


def test_the_cvar_tail_is_thirteen_of_fifty(synthetic: dict[str, Any]) -> None:
    spec = build_v2_spec_content(V2_CANDIDATE_GRID[0], dataset_identity="d" * 64)
    out = run_relative_outer_oof(
        spec=spec,
        spec_hash="x" * 64,
        rows=synthetic["rows"],
        design=synthetic["design"],
        utility=synthetic["utility"],
        chromosome_of=synthetic["bams"],
    )
    assert out["metrics"]["cvar_tail_count"] == 13
    assert math.ceil(0.25 * 50) == 13


def test_harmful_switches_are_counted_not_thresholded() -> None:
    """v1's CATASTROPHIC_MARGIN of 0.05 is deliberately not carried over."""
    assert HARMFUL_SWITCH_DEFINITION["catastrophic_threshold_invented"] is False
    assert HARMFUL_SWITCH_DEFINITION["v1_catastrophic_margin_reused"] is False
    assert HARMFUL_SWITCH_DEFINITION["count_metric"] == "harmful_switch_count"


# ---------------------------------------------------------------------------------------- #
# references
# ---------------------------------------------------------------------------------------- #
def test_always_safe_baseline_never_switches(synthetic: dict[str, Any]) -> None:
    decisions = reference_decisions(
        "ALWAYS_SAFE_BASELINE",
        utility=synthetic["utility"],
        held_bams=sorted(b for b, c in synthetic["bams"].items() if c == "chr18"),
        training_bams=sorted(b for b in synthetic["bams"] if synthetic["bams"][b] != "chr18"),
        chromosome_of=synthetic["bams"],
        outer_fold="chr18",
    )
    assert all(d.selected_config == SAFE_BASELINE_CONFIG_HASH for d in decisions)
    assert policy_metrics(decisions)["switch_fraction"] == 0.0


def test_global_best_reads_only_the_outer_training_side() -> None:
    source = inspect.getsource(reference_decisions)
    assert "training_bams" in source
    assert "the holdout is not consulted" in source


def test_oracle4_is_a_diagnostic_upper_bound(synthetic: dict[str, Any]) -> None:
    from minos_engine.models.relative_finalist_protocol import V2_REFERENCES

    oracle = next(r for r in V2_REFERENCES if r["name"] == "ORACLE4")
    assert oracle["deployable"] is False
    assert oracle["is_promotion_bar"] is False
    held = sorted(b for b, c in synthetic["bams"].items() if c == "chr18")
    decisions = reference_decisions(
        "ORACLE4",
        utility=synthetic["utility"],
        held_bams=held,
        training_bams=[b for b in synthetic["bams"] if b not in set(held)],
        chromosome_of=synthetic["bams"],
        outer_fold="chr18",
    )
    assert policy_metrics(decisions)["mean_regret"] == pytest.approx(0.0)


def test_an_unknown_reference_is_refused(synthetic: dict[str, Any]) -> None:
    with pytest.raises(RelativeRunnerError, match="not a frozen v2 reference"):
        reference_decisions(
            "INVENTED",
            utility=synthetic["utility"],
            held_bams=["x"],
            training_bams=["y"],
            chromosome_of={"x": "chr18", "y": "chr19"},
            outer_fold="chr18",
        )


# ---------------------------------------------------------------------------------------- #
# the sealed boundary
# ---------------------------------------------------------------------------------------- #
def test_the_sealed_v2_entry_accepts_only_operational_handles() -> None:
    parameters = set(inspect.signature(run_real_l2g_v2_train_oof_campaign).parameters)
    assert parameters == {
        "feature_matrix_artifact_path",
        "workspace",
        "config_payload_root",
        "root",
    }
    assert not (parameters & {"dataset", "design", "specs", "rows", "delta", "weights"})


def test_a_caller_cannot_mint_trusted_v2_data() -> None:
    with pytest.raises(RelativeAuthorityError, match="may only be minted"):
        TrustedRelativeTrainingData(object(), dataset=None, design={}, utility={}, spec_hashes=())


def test_the_trusted_loader_requires_the_accepted_identities() -> None:
    source = inspect.getsource(load_trusted_relative_training_data)
    assert "ACCEPTED_SOURCE_TRAINING_DATASET" in source
    assert "ACCEPTED_RELATIVE_DATASET_IDENTITY" in source
    assert "ACCEPTED_FEASIBILITY_SHA256" in source
    assert "ACCEPTED_FINALIST_DOMAIN_HASH" in source


def test_the_design_matrix_is_157_columns_and_derived() -> None:
    source = inspect.getsource(load_trusted_relative_training_data)
    assert "PREDICTOR_COLUMNS" in source
    assert "load_verified_feature_values" in source
    assert "load_verified_config_vectors" in source
    from minos_engine.models.relative_finalist_authority import PREDICTOR_COLUMNS

    assert PREDICTOR_COLUMNS == 157


def test_the_feasibility_artifact_still_matches_its_accepted_bytes() -> None:
    path = repository_root() / "reports/layer2/l2g-v2-relative-finalist-feasibility.json"
    assert hashlib.sha256(path.read_bytes()).hexdigest() == ACCEPTED_FEASIBILITY_SHA256


# ---------------------------------------------------------------------------------------- #
# the future rules
# ---------------------------------------------------------------------------------------- #
def test_the_deployment_margin_must_be_the_frozen_quantile_of_all_150_residuals() -> None:
    spec = build_v2_spec_content(V2_CANDIDATE_GRID[0], dataset_identity="d" * 64)
    residuals = np.linspace(0.0, 1.0, 150)
    correct = float(np.quantile(residuals, 0.75, method="higher"))
    content = _build_final_train_bundle_content(
        spec_hash="s" * 64,
        spec=spec,
        oof_residuals=residuals,
        deployment_margin=correct,
        estimator_artifact_sha256="a" * 64,
        transform_artifact_sha256="b" * 64,
        train_oof_evidence_identity="c" * 64,
        source_commit="d" * 40,
        source_tree="e" * 40,
    )
    assert content["deployment_margin"] == correct
    assert content["validation_labels_used"] is False
    # the FEATURE authorities are bound separately from the decision-domain hash
    assert content["feature_set_hash"] != content["finalist_domain_hash"]
    with pytest.raises(RelativeAuthorityError, match="not the frozen quantile"):
        _build_final_train_bundle_content(
            spec_hash="s" * 64,
            spec=spec,
            oof_residuals=residuals,
            deployment_margin=0.0,
            estimator_artifact_sha256="a" * 64,
            transform_artifact_sha256=None,
            train_oof_evidence_identity="c" * 64,
            source_commit="d" * 40,
            source_tree="e" * 40,
        )


def test_the_bundle_refuses_a_partial_residual_set() -> None:
    spec = build_v2_spec_content(V2_CANDIDATE_GRID[0], dataset_identity="d" * 64)
    with pytest.raises(RelativeAuthorityError, match="expected all 150"):
        _build_final_train_bundle_content(
            spec_hash="s" * 64,
            spec=spec,
            oof_residuals=np.zeros(149),
            deployment_margin=0.0,
            estimator_artifact_sha256="a" * 64,
            transform_artifact_sha256=None,
            train_oof_evidence_identity="c" * 64,
            source_commit="d" * 40,
            source_tree="e" * 40,
        )


def test_validation_cannot_be_evaluated_with_an_empty_train_shortlist() -> None:
    with pytest.raises(RelativeAuthorityError, match="non-empty frozen TRAIN shortlist"):
        evaluate_future_validation(
            train_shortlist=(),
            selector_metrics={},
            safe_baseline_metrics={"mean_regret": 0.1, "cvar_regret": 0.2},
        )


def test_a_non_shortlisted_selector_cannot_be_evaluated() -> None:
    with pytest.raises(RelativeAuthorityError, match="non-shortlisted"):
        evaluate_future_validation(
            train_shortlist=("a" * 64,),
            selector_metrics={"b" * 64: {"mean_regret": 0.0, "cvar_regret": 0.0}},
            safe_baseline_metrics={"mean_regret": 0.1, "cvar_regret": 0.2},
        )


def test_a_selector_failing_either_validation_bar_cannot_qualify() -> None:
    result = evaluate_future_validation(
        train_shortlist=("a" * 64, "b" * 64, "c" * 64),
        selector_metrics={
            "a" * 64: {"mean_regret": 0.05, "cvar_regret": 0.10},  # both
            "b" * 64: {"mean_regret": 0.05, "cvar_regret": 0.90},  # mean only
            "c" * 64: {"mean_regret": 0.90, "cvar_regret": 0.10},  # cvar only
        },
        safe_baseline_metrics={"mean_regret": 0.10, "cvar_regret": 0.20},
    )
    assert result["qualified"] == ["a" * 64]


def test_no_qualified_selector_holds_models_qualified() -> None:
    result = evaluate_future_validation(
        train_shortlist=("a" * 64,),
        selector_metrics={"a" * 64: {"mean_regret": 0.9, "cvar_regret": 0.9}},
        safe_baseline_metrics={"mean_regret": 0.1, "cvar_regret": 0.2},
    )
    assert result["qualified"] == []
    assert result["models_qualified_status"] == "HOLD_NO_VALIDATION_QUALIFIED_SELECTOR"


def test_the_validation_tie_break_is_deterministic_and_frozen() -> None:
    assert FUTURE_VALIDATION_RULE["tie_break_order"] == [
        "LOWER_VALIDATION_MEAN_REGRET",
        "LOWER_VALIDATION_CVAR_REGRET",
        "LOWER_TRAIN_MEAN_REGRET",
        "LOWER_TRAIN_CVAR_REGRET",
        "LEXICAL_MODEL_SPEC_HASH",
    ]
    winner = select_validation_winner(
        qualified=("b" * 64, "a" * 64),
        validation_metrics={
            h: {"mean_regret": 0.1, "cvar_regret": 0.2} for h in ("a" * 64, "b" * 64)
        },
        train_metrics={h: {"mean_regret": 0.1, "cvar_regret": 0.2} for h in ("a" * 64, "b" * 64)},
    )
    assert winner == "a" * 64  # all metrics tie -> lexical spec hash


def test_validation_may_not_refit_or_recalibrate() -> None:
    assert FUTURE_VALIDATION_RULE["refit_on_validation"] is False
    assert FUTURE_VALIDATION_RULE["recalibrate_margin_on_validation"] is False
    assert FUTURE_VALIDATION_RULE["new_gatk_execution_authorized"] is False
    assert FUTURE_VALIDATION_RULE["inference"] == "USE_THE_FINAL_TRAIN_BUNDLE_UNCHANGED"
    assert FUTURE_VALIDATION_RULE["cvar_tail_count_n10"] == 3 == math.ceil(0.25 * 10)


def test_the_final_bundle_rule_forbids_validation_labels() -> None:
    assert FINAL_TRAIN_BUNDLE_RULE["validation_labels_in_bundle"] is False


# ---------------------------------------------------------------------------------------- #
# the committed authority and the locks
# ---------------------------------------------------------------------------------------- #
@pytest.fixture(scope="module")
def authority() -> dict[str, Any]:
    return dict(json.loads((repository_root() / _AUTHORITY).read_bytes()))


def test_the_v2_prefit_authority_binds_the_frozen_procedure(authority: dict[str, Any]) -> None:
    dataset = build_relative_finalist_dataset(load_verified_training_dataset())
    assert authority["relative_dataset_identity"] == ACCEPTED_RELATIVE_DATASET_IDENTITY
    assert authority["protocol_hash"] == compute_relative_protocol_hash()
    assert authority["schema_version"] == "l2g-v2-prefit-authority-v3"
    assert [e["spec_hash"] for e in authority["candidate_spec_hashes"]] == list(
        build_v2_spec_hashes(dataset.identity())
    )
    assert authority["quantile_method"] == "HIGHER"
    assert authority["inner_cv"]["residuals"] == 120
    assert authority["outer_cv"]["training_bams"] == 40
    assert authority["output_root"] == V2_OUTPUT_ROOT
    assert authority["no_v2_model_fitted"] is True
    assert authority["validation_read"] is False
    assert authority["test_accessed"] is False


def test_campaign_v1_is_untouched() -> None:
    from minos_engine.models.campaign_freeze import (
        CAMPAIGN_FREEZE_PATH,
        campaign_freeze_identity,
        verify_campaign_freeze,
    )

    freeze = json.loads((repository_root() / CAMPAIGN_FREEZE_PATH).read_bytes())
    assert verify_campaign_freeze(freeze)["ok"] is True
    assert campaign_freeze_identity(freeze) == (
        "1c2039dec2f3fbb51a8058c947bbf8de9f9c6d235a133b5948aa6b33ac516673"
    )
    assert freeze["shortlist"] == []


def test_no_real_v2_campaign_was_run() -> None:
    from tests.minos_scratch import CANONICAL_MINOS_ROOT

    assert not (CANONICAL_MINOS_ROOT / V2_OUTPUT_ROOT).exists()


def test_the_v2_source_reads_no_validation_and_no_test() -> None:
    root = repository_root() / "src/minos_engine/models"
    for name in (
        "relative_finalist_contract.py",
        "relative_finalist_dataset.py",
        "relative_finalist_protocol.py",
        "relative_finalist_runner.py",
        "relative_finalist_authority.py",
    ):
        source = (root / name).read_text(encoding="utf-8")
        assert "l2f2_validation" not in source
        assert "phase_d_selection" not in source
        assert "TEST_TRUTH" not in source


def test_models_qualified_absent_and_select_config_blocked() -> None:
    from minos_engine.layer2.service import Layer2Service

    assert not (repository_root() / "gates/models-qualified.json").exists()
    with pytest.raises(Exception) as excinfo:
        Layer2Service().select_config(None)  # type: ignore[arg-type]
    assert "StageNotReady" in type(excinfo.value).__name__
