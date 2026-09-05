"""The four execution-level defects, and the completeness rules that gate promotion.

Each of these could change the TRAIN shortlist while every individual component still looked
correct in isolation, which is why they are tested at the campaign boundary rather than inside
the pieces.
"""

from __future__ import annotations

import math
from typing import Any

import numpy as np
import pytest

from minos_engine.models.campaign import (
    EXPECTED_BAM_COUNT,
    EXPECTED_OOF_RECORDS_PER_SPEC,
    EXPECTED_OUTER_FOLDS,
    STATUS_COMPLETE,
    STATUS_TRAINING_FAILURE,
    CampaignError,
    ReferenceThresholdUnavailable,
    assess_completeness,
    run_l2g_train_oof_campaign,
)
from minos_engine.models.contract import CV_FOLD_CHROMOSOMES, SAFE_BASELINE_CONFIG_HASH
from minos_engine.models.design_matrix import DesignMatrix
from minos_engine.models.fit_driver import fit_fold_estimators, fit_reference_fold
from minos_engine.models.oof_metrics import (
    SELECTION_POLICY,
    ReferenceSelectionUnavailable,
    _cvar,
    bam_selection_regret,
)
from minos_engine.models.oof_runner import TrainingFailure
from minos_engine.models.shortlist import (
    SHORTLIST_RESULT_SCHEMA,
    ShortlistError,
    derive_verified_train_shortlist,
)

_OTHER = [f"{i:064x}" for i in range(5)]
_CONFIGS = [SAFE_BASELINE_CONFIG_HASH, *_OTHER]


# ------------------------------------------------------------------------------------------ #
# DEFECT D -- the CVaR finite-sample rule
# ------------------------------------------------------------------------------------------ #
def test_cvar_takes_ceil_not_round_of_the_tail() -> None:
    """round(0.25 * 50) is 12 under banker's rounding; the frozen convention is ceil -> 13."""
    assert math.ceil(0.25 * 50) == 13
    assert round(0.25 * 50) == 12
    values = [float(i) for i in range(50)]
    # the 13 worst are 49..37
    assert _cvar(values, 0.25) == pytest.approx(sum(range(37, 50)) / 13)
    assert _cvar(values, 0.25) != pytest.approx(sum(range(38, 50)) / 12)


def test_the_cvar_tail_is_the_worst_regrets_not_the_best() -> None:
    values = [0.0] * 45 + [1.0] * 5
    assert _cvar(values, 0.25) > 0.0


def test_cvar_matches_the_frozen_baseline_objective_convention() -> None:
    import inspect

    from minos_engine.baseline import objective

    source = inspect.getsource(objective._cvar)
    assert "math.ceil" in source
    from minos_engine.models import oof_metrics

    assert "math.ceil" in inspect.getsource(oof_metrics._cvar)


@pytest.mark.parametrize(("n", "expected"), [(50, 13), (40, 10), (4, 1), (1, 1), (7, 2)])
def test_the_tail_size_is_ceil_for_any_population(n: int, expected: int) -> None:
    values = [float(i) for i in range(n)]
    tail = sorted(values, reverse=True)[:expected]
    assert _cvar(values, 0.25) == pytest.approx(sum(tail) / expected)


# ------------------------------------------------------------------------------------------ #
# DEFECT B -- safe-baseline selection
# ------------------------------------------------------------------------------------------ #
class _Rec:
    def __init__(self, bam: str, config: str, predicted: float, actual: float) -> None:
        self.dataset_id, self.config_hash = bam, config
        self.expected_utility_prediction, self.actual_utility = predicted, actual
        self.outer_fold = "chr18"
        self.actual_outcome = "ADMITTED"
        self.actual_admitted_score = actual
        self.clipped_score_prediction = predicted
        self.calibrated_admission_probability = 1.0


def test_the_safe_baseline_always_selects_its_own_config_not_the_lowest_hash() -> None:
    """A constant predictor ties everywhere; lexicographic order would pick a different model."""
    records = [
        _Rec("b1", "00" * 32, 0.5, 0.9),
        _Rec("b1", SAFE_BASELINE_CONFIG_HASH, 0.5, 0.6),
    ]
    result = bam_selection_regret(records, family="CONSTANT_SAFE_BASELINE")
    assert result["selected_config"]["b1"] == SAFE_BASELINE_CONFIG_HASH
    assert result["per_bam_regret"]["b1"] == pytest.approx(0.9 - 0.6)
    assert result["selected_actual_utility"]["b1"] == pytest.approx(0.6)


def test_the_safe_baseline_refuses_a_bam_where_its_config_was_never_observed() -> None:
    records = [_Rec("b1", "00" * 32, 0.5, 0.9)]
    with pytest.raises(ReferenceSelectionUnavailable, match="never observed"):
        bam_selection_regret(records, family="CONSTANT_SAFE_BASELINE")


def test_the_blind_references_keep_the_lexicographic_tie_break() -> None:
    records = [_Rec("b1", "ff" * 32, 0.5, 0.9), _Rec("b1", "00" * 32, 0.5, 0.1)]
    for family in ("GLOBAL_MEAN", "BAM_FEATURES_ONLY"):
        assert bam_selection_regret(records, family=family)["selected_config"]["b1"] == "00" * 32


def test_contextual_families_select_on_predicted_utility() -> None:
    records = [_Rec("b1", "ff" * 32, 0.9, 0.8), _Rec("b1", "00" * 32, 0.1, 0.2)]
    for family in ("CONFIG_ONLY", "LINEAR_REGULARIZED", "TREE_ENSEMBLE", "COMPACT_MLP"):
        result = bam_selection_regret(records, family=family)
        assert result["selected_config"]["b1"] == "ff" * 32


def test_selection_policy_is_bound_per_family_not_inferred() -> None:
    assert SELECTION_POLICY["CONSTANT_SAFE_BASELINE"] == (
        "ALWAYS_THE_QUALIFIED_SAFE_BASELINE_CONFIG"
    )
    assert SELECTION_POLICY["GLOBAL_MEAN"] == "LOWEST_CONFIG_HASH_LEXICOGRAPHIC"
    with pytest.raises(Exception, match="no frozen selection policy"):
        bam_selection_regret([_Rec("b1", "00" * 32, 0.5, 0.5)], family="INVENTED")


# ------------------------------------------------------------------------------------------ #
# DEFECT A / §3 -- reference spec vs implementation
# ------------------------------------------------------------------------------------------ #
def _dataset() -> Any:
    from minos_engine.models.prefit_loader import load_verified_training_dataset

    return load_verified_training_dataset()


def _reference_specs() -> tuple[Any, ...]:
    from minos_engine.models.spec_factory import build_accepted_l2g_reference_specs

    return build_accepted_l2g_reference_specs(_dataset())


def test_the_block_references_declare_nested_calibration_and_now_perform_it() -> None:
    specs = {s.family: s for s in _reference_specs()}
    for family in ("CONFIG_ONLY", "BAM_FEATURES_ONLY"):
        assert specs[family].admission_probability_calibration == (
            "NESTED_CROSS_FITTED_WITHIN_EACH_OUTER_FOLD"
        )
    import inspect

    from minos_engine.models import references

    source = inspect.getsource(references._LinearBlockReference)
    assert "fit_nested_admission_calibrator" in source
    assert "self._calibrator.apply(raw)" in source


def test_the_constant_references_declare_no_calibration_and_perform_none() -> None:
    specs = {s.family: s for s in _reference_specs()}
    for family in ("CONSTANT_SAFE_BASELINE", "GLOBAL_MEAN"):
        assert specs[family].admission_probability_calibration == "NONE_CONSTANT_PREDICTOR"


def test_the_block_reference_calibrator_never_sees_the_outer_fold() -> None:
    from minos_engine.models.references import ConfigOnlyRidge

    rng = np.random.default_rng(4)
    meta = [
        {
            "dataset_id": f"b{i % 8}",
            "config_hash": f"{i:064x}",
            "admission_label": int(i % 3 > 0),
            "admitted_score": 0.7 if i % 3 > 0 else None,
        }
        for i in range(64)
    ]
    train_bams = frozenset(f"b{i}" for i in range(6))
    held = frozenset({"b6", "b7"})
    rows = [m for m in meta if m["dataset_id"] in train_bams]
    x = rng.normal(size=(len(rows), 28))
    inner = [(train_bams - frozenset({f"b{i}"}), frozenset({f"b{i}"})) for i in range(6)]
    model = ConfigOnlyRidge.fit(x, rows, inner_folds=inner, outer_heldout_bams=held)
    assert model.calibration_bams
    assert not (model.calibration_bams & held)


def test_the_references_use_the_frozen_campaign_seed() -> None:
    import inspect

    from minos_engine.models import references
    from minos_engine.models.protocol import RANDOM_SEED

    source = inspect.getsource(references)
    assert "random_state=0" not in source
    assert "random_state=RANDOM_SEED" in source
    assert RANDOM_SEED == 20260904
    for spec in _reference_specs():
        assert spec.random_seed == RANDOM_SEED


# ------------------------------------------------------------------------------------------ #
# §9 -- inner single-class must fail
# ------------------------------------------------------------------------------------------ #
def test_an_inner_single_class_fold_is_a_training_failure_not_a_skip() -> None:
    import inspect

    from minos_engine.models import fit_driver

    source = inspect.getsource(fit_driver.fit_fold_estimators)
    assert "continue" not in source, "an inner fold is still being skipped"
    assert "single class" in source

    rng = np.random.default_rng(2)
    meta = [
        {
            "dataset_id": f"b{i % 4}",
            "config_hash": f"{i:064x}",
            "identity": f"i{i}",
            # b0 is entirely one class, so its inner fold cannot calibrate
            "admission_label": 1 if i % 4 == 0 else int(i % 2),
            "admitted_score": 0.7,
        }
        for i in range(40)
    ]
    for m in meta:
        if m["admission_label"] == 0:
            m["admitted_score"] = None
    x = rng.normal(size=(len(meta), 20))
    weights = {m["identity"]: 1.0 for m in meta}
    inner = [
        (frozenset({"b0", "b1", "b2"}), frozenset({"b3"})),
        (frozenset({"b1", "b2", "b3"}), frozenset({"b0"})),
    ]

    class S:
        family = "LINEAR_REGULARIZED"
        score_model_implementation = "sklearn.linear_model.Ridge"
        admission_model_implementation = "sklearn.linear_model.LogisticRegression"
        score_hyperparameters = {"alpha": 1.0}
        admission_hyperparameters = {"C": 1.0, "max_iter": 1000}
        random_seed = 20260904

    with pytest.raises(TrainingFailure):
        fit_fold_estimators(
            spec=S(),
            x_train=x,
            meta_train=[dict(m) for m in meta],
            weights=weights,
            score_weights=weights,
            inner_folds=[(frozenset({"b0"}), frozenset({"b1"}))],
            train_bams=frozenset({"b0", "b1", "b2", "b3"}),
            held_bams=frozenset({"b9"}),
        )
    assert inner


def test_an_empty_inner_fold_is_a_training_failure() -> None:
    import inspect

    from minos_engine.models import fit_driver

    source = inspect.getsource(fit_driver.fit_fold_estimators)
    assert "no training rows" in source and "no held-out rows" in source


# ------------------------------------------------------------------------------------------ #
# §10 -- prediction runs single-threaded too
# ------------------------------------------------------------------------------------------ #
def test_prediction_not_just_fitting_runs_under_the_thread_limit() -> None:
    import inspect

    from minos_engine.models import fit_driver

    source = inspect.getsource(fit_driver)
    for callable_name in ("_raw_admission", "_raw_score", "_calibrate"):
        block = source.split(f"def {callable_name}")[1].split("return")[0]
        assert "single_threaded()" in block, f"{callable_name} predicts outside the limit"


def test_the_thread_limit_actually_binds_during_a_prediction() -> None:
    import sklearn  # noqa: F401

    from minos_engine.models.threading_control import observe_thread_pools, single_threaded

    observed: list[int] = []
    with single_threaded():
        observed = [
            p["num_threads"] for p in observe_thread_pools() if p["user_api"] in ("blas", "openmp")
        ]
    assert observed and all(n == 1 for n in observed)


# ------------------------------------------------------------------------------------------ #
# DEFECT C -- completeness gates promotion
# ------------------------------------------------------------------------------------------ #
class _R:
    def __init__(self, bam: str, config: str, fold: str) -> None:
        self.dataset_id, self.config_hash, self.outer_fold = bam, config, fold


def _full_records(bams: dict[str, str], configs: list[str]) -> list[_R]:
    return [_R(b, c, bams[b]) for b in bams for c in configs]


def _bams() -> dict[str, str]:
    return {f"bam-{c}-{i}": c for c in CV_FOLD_CHROMOSOMES for i in range(10)}


def test_the_real_expectation_is_1040_records_over_50_bams_and_5_folds() -> None:
    assert EXPECTED_OOF_RECORDS_PER_SPEC == 1040
    assert EXPECTED_BAM_COUNT == 50
    assert EXPECTED_OUTER_FOLDS == 5


def test_a_complete_spec_predicts_every_cell_exactly_once() -> None:
    bams, configs = _bams(), _CONFIGS
    records = _full_records(bams, configs)
    result = assess_completeness(
        records=records, failures=[], expected_cells=len(records), expected_bams=50
    )
    assert result["status"] == STATUS_COMPLETE
    assert result["successful_outer_fold_count"] == 5
    assert result["duplicate_cell_count"] == 0
    assert result["reasons"] == []


def test_one_missing_record_makes_a_spec_incomplete() -> None:
    bams, configs = _bams(), _CONFIGS
    records = _full_records(bams, configs)
    result = assess_completeness(
        records=records[:-1], failures=[], expected_cells=len(records), expected_bams=50
    )
    assert result["status"] == STATUS_TRAINING_FAILURE
    assert "scientific cells predicted" in result["reasons"][0]


def test_a_duplicate_prediction_makes_a_spec_incomplete() -> None:
    bams, configs = _bams(), _CONFIGS
    records = _full_records(bams, configs)
    duplicated = [*records, records[0]]
    result = assess_completeness(
        records=duplicated, failures=[], expected_cells=len(records), expected_bams=50
    )
    assert result["status"] == STATUS_TRAINING_FAILURE
    assert result["duplicate_cell_count"] == 1


def test_four_of_five_folds_is_incomplete_however_good_the_metrics() -> None:
    bams, configs = _bams(), _CONFIGS
    records = [r for r in _full_records(bams, configs) if r.outer_fold != "chr22"]
    result = assess_completeness(
        records=records,
        failures=[{"fold": "chr22"}],
        expected_cells=len(_full_records(bams, configs)),
        expected_bams=50,
    )
    assert result["status"] == STATUS_TRAINING_FAILURE
    assert result["successful_outer_fold_count"] == 4
    assert result["failed_folds"] == ["chr22"]


def test_a_missing_bam_makes_a_spec_incomplete() -> None:
    bams, configs = _bams(), _CONFIGS
    dropped = next(iter(bams))
    records = [r for r in _full_records(bams, configs) if r.dataset_id != dropped]
    result = assess_completeness(
        records=records,
        failures=[],
        expected_cells=len(_full_records(bams, configs)),
        expected_bams=50,
    )
    assert result["status"] == STATUS_TRAINING_FAILURE
    assert result["unique_bam_count"] == 49


# ------------------------------------------------------------------------------------------ #
# §12 -- the shortlist fails closed
# ------------------------------------------------------------------------------------------ #
_REFS = tuple(f"{100 + i:064x}" for i in range(4))
_CANDS = tuple(f"{i:064x}" for i in range(6))
_GOOD = {"mean_regret": 0.01, "cvar_regret": 0.02}
_BAD = {"mean_regret": 0.90, "cvar_regret": 0.90}
_WORSE_THAN_BAR = {"mean_regret": 0.95, "cvar_regret": 0.95}


def test_the_verified_shortlist_requires_all_four_references() -> None:
    with pytest.raises(ShortlistError, match="exactly the frozen four"):
        derive_verified_train_shortlist(
            reference_metrics=dict.fromkeys(_REFS[:3], _BAD),
            candidate_metrics={_CANDS[0]: _GOOD},
            reference_spec_hashes=_REFS[:3],
            candidate_spec_hashes=_CANDS,
        )


def test_the_verified_shortlist_refuses_a_missing_reference_metric() -> None:
    with pytest.raises(ShortlistError, match="never fully observed"):
        derive_verified_train_shortlist(
            reference_metrics=dict.fromkeys(_REFS[:3], _BAD),
            candidate_metrics={_CANDS[0]: _GOOD},
            reference_spec_hashes=_REFS,
            candidate_spec_hashes=_CANDS,
        )


def test_the_verified_shortlist_refuses_an_unknown_candidate() -> None:
    with pytest.raises(ShortlistError, match="unknown candidate"):
        derive_verified_train_shortlist(
            reference_metrics=dict.fromkeys(_REFS, _BAD),
            candidate_metrics={"f" * 64: _GOOD},
            reference_spec_hashes=_REFS,
            candidate_spec_hashes=_CANDS,
        )


def test_an_incomplete_candidate_cannot_be_shortlisted() -> None:
    with pytest.raises(ShortlistError, match="did not complete"):
        derive_verified_train_shortlist(
            reference_metrics=dict.fromkeys(_REFS, _BAD),
            candidate_metrics={_CANDS[0]: _GOOD},
            reference_spec_hashes=_REFS,
            candidate_spec_hashes=_CANDS,
            ineligible_candidate_hashes=(_CANDS[0],),
        )


def test_a_complete_campaign_can_shortlist() -> None:
    result = derive_verified_train_shortlist(
        reference_metrics=dict.fromkeys(_REFS, _BAD),
        candidate_metrics={_CANDS[0]: _GOOD, _CANDS[1]: _WORSE_THAN_BAR},
        reference_spec_hashes=_REFS,
        candidate_spec_hashes=_CANDS,
        ineligible_candidate_hashes=tuple(_CANDS[2:]),
    )
    # the bar is the BEST reference (0.90 / 0.90): 0.01 clears it, 0.95 does not
    assert result["shortlist"] == [_CANDS[0]]
    assert result["ineligible_candidate_count"] == 4
    assert result["reference_threshold_available"] is True


def test_an_empty_eligible_set_yields_an_empty_shortlist_not_a_fallback_promotion() -> None:
    result = derive_verified_train_shortlist(
        reference_metrics=dict.fromkeys(_REFS, _GOOD),
        candidate_metrics={},
        reference_spec_hashes=_REFS,
        candidate_spec_hashes=_CANDS,
        ineligible_candidate_hashes=_CANDS,
    )
    assert result["shortlist"] == []
    assert result["shortlist_empty"] is True
    assert "SAFE_BASELINE" in result["fallback_if_empty"]


def test_the_campaign_result_schema_was_versioned_for_the_new_semantics() -> None:
    assert SHORTLIST_RESULT_SCHEMA == "l2g-train-oof-campaign-result-v2"
    from minos_engine.models.shortlist import SUPERSEDED_RESULT_V1

    assert SUPERSEDED_RESULT_V1 == "SUPERSEDED_BEFORE_FIRST_CAMPAIGN"


# ------------------------------------------------------------------------------------------ #
# §7 -- the orchestrator, on a synthetic grid
# ------------------------------------------------------------------------------------------ #
class _Row:
    def __init__(self, bam: str, config: str, chromosome: str, admitted: bool, score: Any) -> None:
        self.dataset_id, self.config_hash, self.chromosome = bam, config, chromosome
        self.outcome = "ADMITTED" if admitted else "CANDIDATE_NON_ADMISSION"
        self.admitted_score = score
        self.admission_label = 1 if admitted else 0

    def identity(self) -> str:
        return f"{self.dataset_id}|{self.config_hash}"


def _synthetic_campaign() -> tuple[Any, Any, tuple[Any, ...], tuple[Any, ...]]:
    rng = np.random.default_rng(5)
    bams = _bams()
    rows = []
    for bam, chromosome in bams.items():
        for config in _CONFIGS:
            admitted = bool(rng.random() > 0.25)
            rows.append(
                _Row(
                    bam,
                    config,
                    chromosome,
                    admitted,
                    float(np.clip(rng.normal(0.7, 0.1), 0, 1)) if admitted else None,
                )
            )
    meta = tuple(
        {
            "dataset_id": r.dataset_id,
            "chromosome": r.chromosome,
            "config_hash": r.config_hash,
            "outcome": r.outcome,
            "admitted_score": r.admitted_score,
            "admission_label": r.admission_label,
            "identity": r.identity(),
        }
        for r in rows
    )
    design = DesignMatrix(
        x_bam=rng.normal(size=(len(rows), 129)),
        x_config=rng.normal(size=(len(rows), 28)),
        bam_columns=tuple(f"b{i}" for i in range(129)),
        config_columns=tuple(f"c{i}" for i in range(28)),
        meta=meta,
    )

    class _CV:
        bam_chromosome = bams

        def identity(self) -> str:
            return "c" * 64

    class _DS:
        cv_manifest = _CV()

        def __init__(self) -> None:
            self.rows = tuple(rows)

        def admission_weights(self) -> dict[str, float]:
            per: dict[str, int] = {}
            for r in rows:
                per[r.dataset_id] = per.get(r.dataset_id, 0) + 1
            return {r.identity(): 1 / per[r.dataset_id] for r in rows}

        def score_weights(self) -> dict[str, float]:
            admitted = [r for r in rows if r.admitted_score is not None]
            per: dict[str, int] = {}
            for r in admitted:
                per[r.dataset_id] = per.get(r.dataset_id, 0) + 1
            return {r.identity(): 1 / per[r.dataset_id] for r in admitted}

    def _spec(family: str, spec_hash: str) -> Any:
        class _S:
            def __init__(self) -> None:
                self.family = family
                self.score_model_implementation = "sklearn.linear_model.Ridge"
                self.admission_model_implementation = "sklearn.linear_model.LogisticRegression"
                self.score_hyperparameters = {"alpha": 1.0}
                self.admission_hyperparameters = {"C": 1.0, "max_iter": 1000}
                self.random_seed = 20260904

            def identity(self) -> str:
                return spec_hash

        return _S()

    candidates = tuple(_spec("LINEAR_REGULARIZED", h) for h in _CANDS)
    references = tuple(
        _spec(f, h)
        for f, h in zip(
            ["CONSTANT_SAFE_BASELINE", "GLOBAL_MEAN", "CONFIG_ONLY", "BAM_FEATURES_ONLY"],
            _REFS,
            strict=True,
        )
    )
    return _DS(), design, candidates, references


def test_the_orchestrator_runs_all_ten_specs_and_reports_completeness() -> None:
    dataset, design, candidates, references = _synthetic_campaign()
    result = run_l2g_train_oof_campaign(
        dataset=dataset,
        design=design,
        candidate_specs=candidates,
        reference_specs=references,
        fit_estimators=fit_fold_estimators,
        fit_reference=fit_reference_fold,
    )
    assert result["all_required_references_complete"] is True
    assert result["reference_threshold_available"] is True
    assert len(result["per_spec"]) == 10
    for entry in result["per_spec"].values():
        assert entry["status"] == STATUS_COMPLETE
        assert entry["successful_outer_fold_count"] == 5
        assert entry["observed_oof_record_count"] == len(dataset.rows)
        assert entry["oof_artifact_hash"]
    assert result["validation_read"] is False
    assert result["test_accessed"] is False


def test_a_failed_reference_holds_the_whole_campaign() -> None:
    dataset, design, candidates, references = _synthetic_campaign()

    def broken(**_: Any) -> dict[str, Any]:
        raise RuntimeError("deliberate reference failure")

    with pytest.raises(ReferenceThresholdUnavailable, match="never fully observed"):
        run_l2g_train_oof_campaign(
            dataset=dataset,
            design=design,
            candidate_specs=candidates,
            reference_specs=references,
            fit_estimators=fit_fold_estimators,
            fit_reference=broken,
        )


def test_a_failed_candidate_is_recorded_but_never_shortlisted() -> None:
    dataset, design, candidates, references = _synthetic_campaign()
    seen = {"n": 0}

    def flaky(**kwargs: Any) -> dict[str, Any]:
        seen["n"] += 1
        if seen["n"] == 1:
            raise TrainingFailure("deliberate candidate fold failure")
        return fit_fold_estimators(**kwargs)

    result = run_l2g_train_oof_campaign(
        dataset=dataset,
        design=design,
        candidate_specs=candidates,
        reference_specs=references,
        fit_estimators=flaky,
        fit_reference=fit_reference_fold,
    )
    failed = [e for e in result["per_spec"].values() if e["status"] == STATUS_TRAINING_FAILURE]
    assert len(failed) == 1
    assert failed[0]["role"] == "CANDIDATE"
    assert failed[0]["spec_hash"] in result["ineligible_candidates"]
    assert failed[0]["spec_hash"] not in result["shortlist"]
    assert "metrics" not in failed[0]


def test_the_orchestrator_refuses_a_wrong_sized_spec_set() -> None:
    dataset, design, candidates, references = _synthetic_campaign()
    with pytest.raises(CampaignError, match="expected the frozen 6"):
        run_l2g_train_oof_campaign(
            dataset=dataset,
            design=design,
            candidate_specs=candidates[:3],
            reference_specs=references,
            fit_estimators=fit_fold_estimators,
            fit_reference=fit_reference_fold,
        )
    with pytest.raises(CampaignError, match="expected the frozen 4"):
        run_l2g_train_oof_campaign(
            dataset=dataset,
            design=design,
            candidate_specs=candidates,
            reference_specs=references[:2],
            fit_estimators=fit_fold_estimators,
            fit_reference=fit_reference_fold,
        )


def test_the_safe_baseline_reference_selects_its_config_in_a_real_campaign_run() -> None:
    dataset, design, candidates, references = _synthetic_campaign()
    result = run_l2g_train_oof_campaign(
        dataset=dataset,
        design=design,
        candidate_specs=candidates,
        reference_specs=references,
        fit_estimators=fit_fold_estimators,
        fit_reference=fit_reference_fold,
    )
    entry = next(e for e in result["per_spec"].values() if e["family"] == "CONSTANT_SAFE_BASELINE")
    assert entry["metrics"]["selection_policy"] == "ALWAYS_THE_QUALIFIED_SAFE_BASELINE_CONFIG"


def test_no_real_campaign_artifact_exists() -> None:
    from tests.minos_scratch import CANONICAL_MINOS_ROOT

    workspace = CANONICAL_MINOS_ROOT / "minos_l2g_training"
    for forbidden in (
        "oof_predictions.json",
        "train_oof_campaign_result.json",
        "metrics.json",
    ):
        assert not (workspace / forbidden).exists()
