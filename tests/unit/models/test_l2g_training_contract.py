"""L2-G contracts: what may be learned from, how it is split, and what must never leak in.

The tests worth writing here are the ones that try to smuggle something in — a TEST row, a
fabricated score for a crashed run, the dataset identity as a predictor, or the same BAM on both
sides of a fold.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from minos_engine.gates.required_checks import required_checks_for
from minos_engine.models.config_encoder import (
    ConfigEncoderError,
    build_config_encoding,
)
from minos_engine.models.contract import (
    CV_FOLD_CHROMOSOMES,
    FAILURE_UTILITY,
    FORBIDDEN_AT_INFERENCE,
    SAFE_BASELINE_CONFIG_HASH,
    TARGET_FORMULATION,
    compute_training_contract_hash,
    training_contract_content,
)
from minos_engine.models.dataset import (
    CvManifest,
    TrainingDataset,
    TrainingDatasetError,
    TrainingRow,
)
from minos_engine.models.metrics import (
    REGRET_ORIENTATION,
    MetricsError,
    bam_grouped_regret,
    calibration_error,
    downside_summary,
    spearman,
)
from minos_engine.models.spec import (
    MODEL_FAMILIES,
    SELECTION_ORDER,
    ArtifactRef,
    ModelBundle,
    ModelSpec,
    ModelSpecError,
)

_H = "a" * 64


def _repo() -> Path:
    return Path(__file__).resolve().parents[3]


def _manifest(n: int = 50) -> CvManifest:
    return CvManifest(
        bam_chromosome={
            f"minos-{CV_FOLD_CHROMOSOMES[i % 5]}-{i:02d}": CV_FOLD_CHROMOSOMES[i % 5]
            for i in range(n)
        }
    )


def _row(**over: Any) -> TrainingRow:
    fields: dict[str, Any] = {
        "dataset_id": "minos-chr18-00",
        "chromosome": "chr18",
        "config_hash": "c" * 64,
        "job_key": "j" * 64,
        "partition": "train",
        "succeeded": True,
        "minos_score": 0.7,
    }
    fields.update(over)
    return TrainingRow(**fields)


def _dataset(rows: tuple[TrainingRow, ...], **over: Any) -> TrainingDataset:
    fields: dict[str, Any] = {
        "baseline_qualified_gate_hash": _H,
        "baseline_selected_hash": _H,
        "feature_registry_hash": _H,
        "config_encoding_identity": _H,
        "parameter_space_hash": _H,
        "scoring_contract_hash": _H,
        "execution_environment_hash": _H,
        "train_plan_hashes": ("p" * 64,),
        "feature_names": ("alignment.nm_per_aligned_base", "coverage.mean_depth"),
        "config_feature_names": ("cfg.min_base_quality_score",),
        "rows": rows,
        "cv_manifest": _manifest(),
    }
    fields.update(over)
    return TrainingDataset(**fields)


# --------------------------------------------------------------------------------------------
# the frozen contract
# --------------------------------------------------------------------------------------------
def test_the_target_formulation_is_joint_expected_utility() -> None:
    assert TARGET_FORMULATION == "B_JOINT_EXPECTED_UTILITY"
    content = training_contract_content()
    assert content["failure_utility"] == 0.0
    assert content["infrastructure_incidents_are_labels"] is False
    assert content["cv_protocol"] == "BAM_GROUPED_CHROMOSOME_HELD_OUT"
    assert content["validation_use"] == "MODEL_SELECTION_ONLY_AFTER_CANDIDATES_FROZEN"
    assert content["test_use"] == "SEALED_UNTIL_L2_I"


def test_the_contract_hash_is_deterministic_and_covers_the_decisions() -> None:
    from minos_engine.models import contract as mod

    baseline = compute_training_contract_hash()
    assert baseline == compute_training_contract_hash()
    for field in (
        "target_formulation",
        "failure_utility",
        "cv_protocol",
        "validation_use",
        "safe_baseline_config_hash",
        "test_use",
    ):
        original = mod.training_contract_content
        perturbed = dict(original())
        perturbed[field] = "PERTURBED" if isinstance(perturbed[field], str) else 1.0
        assert perturbed != original()


def test_inference_may_never_require_truth_or_outcome() -> None:
    for forbidden in (
        "truth_vcf",
        "mutations_vcf",
        "minos_score",
        "admitted",
        "evaluation_hash",
        "winner_config",
        "dataset_id",
        "partition",
    ):
        assert forbidden in FORBIDDEN_AT_INFERENCE, forbidden


def test_the_safe_baseline_is_the_qualified_selection() -> None:
    assert SAFE_BASELINE_CONFIG_HASH == (
        "157d88d1587c13be395c62d60e27d1becdada78fad45e65d883bc1190e51acea"
    )


# --------------------------------------------------------------------------------------------
# labels: a crashed run has no score
# --------------------------------------------------------------------------------------------
def test_a_failed_candidate_is_never_given_a_fabricated_score() -> None:
    """The decisive target test. Utility 0.0 is an AGGREGATION rule, not a biological score."""
    with pytest.raises(TrainingDatasetError, match="inventing 0.0"):
        _row(succeeded=False, minos_score=0.0, failure_code="GATK_NONZERO_EXIT")


def test_a_bounded_candidate_failure_is_a_valid_negative_label() -> None:
    row = _row(succeeded=False, minos_score=None, failure_code="GATK_NONZERO_EXIT")
    assert row.succeeded is False
    assert row.minos_score is None


@pytest.mark.parametrize(
    "code", ["HAPPY_TIMEOUT", "EVALUATION_ERROR", "ARTIFACT_PUBLISH_FAILED", "TRUTH_BYTES_MISMATCH"]
)
def test_an_infrastructure_incident_is_never_a_training_label(code: str) -> None:
    """Our defect. A model trained on it learns about our infrastructure, not about genomics."""
    with pytest.raises(TrainingDatasetError, match="never a training label"):
        _row(succeeded=False, minos_score=None, failure_code=code)


def test_a_succeeded_row_must_carry_a_score() -> None:
    with pytest.raises(TrainingDatasetError, match="must carry a minos_score"):
        _row(succeeded=True, minos_score=None)


def test_the_score_model_sees_only_scored_rows(_: None = None) -> None:
    rows = (
        _row(config_hash="1" * 64),
        _row(
            config_hash="2" * 64,
            succeeded=False,
            minos_score=None,
            failure_code="GATK_NONZERO_EXIT",
        ),
    )
    dataset = _dataset(rows)
    assert len(dataset.scored_rows) == 1
    assert len(dataset.decided_rows) == 2
    assert FAILURE_UTILITY == 0.0


# --------------------------------------------------------------------------------------------
# partition isolation
# --------------------------------------------------------------------------------------------
@pytest.mark.parametrize("partition", ["test", "validation"])
def test_a_non_train_row_is_refused_rather_than_filtered(partition: str) -> None:
    with pytest.raises(TrainingDatasetError, match="TRAIN only"):
        _row(partition=partition)


# --------------------------------------------------------------------------------------------
# predictor hygiene
# --------------------------------------------------------------------------------------------
@pytest.mark.parametrize(
    "leaked",
    ["minos_score", "admitted", "truth_vcf", "evaluation_hash", "winner_config", "round_id"],
)
def test_a_forbidden_field_cannot_enter_the_predictor_matrix(leaked: str) -> None:
    with pytest.raises(TrainingDatasetError, match="never see at inference"):
        _dataset((_row(),), feature_names=("coverage.mean_depth", leaked))


@pytest.mark.parametrize("identity", ["dataset_id", "round_id", "partition", "chromosome"])
def test_identity_and_grouping_metadata_are_not_predictors(identity: str) -> None:
    """chromosome is a CV grouping key. As a feature it is an invitation to memorise."""
    with pytest.raises(TrainingDatasetError, match="metadata, not a predictor|never see"):
        _dataset((_row(),), feature_names=("coverage.mean_depth", identity))


# --------------------------------------------------------------------------------------------
# CV: the BAM is the atom
# --------------------------------------------------------------------------------------------
def test_there_are_exactly_five_chromosome_held_out_folds() -> None:
    folds = _manifest().folds()
    assert len(folds) == 5
    for train, held in folds:
        assert not (train & held), "a BAM appeared on both sides of a fold"
        assert len(train) + len(held) == 50


def test_every_bam_is_held_out_exactly_once() -> None:
    manifest = _manifest()
    seen: dict[str, int] = {}
    for _train, held in manifest.folds():
        for bam in held:
            seen[bam] = seen.get(bam, 0) + 1
    assert set(seen.values()) == {1}
    assert len(seen) == 50


def test_all_rows_of_one_bam_share_a_fold() -> None:
    """One BAM contributes 10-97 config rows here; splitting them would leak its features."""
    manifest = _manifest()
    bam = "minos-chr18-00"
    folds = {manifest.fold_of(bam) for _ in range(20)}
    assert folds == {0}


def test_the_manifest_is_deterministic_and_order_independent() -> None:
    a = _manifest()
    b = CvManifest(bam_chromosome=dict(reversed(list(a.bam_chromosome.items()))))
    assert a.identity() == b.identity()


def test_a_manifest_of_the_wrong_size_is_refused() -> None:
    with pytest.raises(TrainingDatasetError, match="expected 50"):
        _manifest(49)


def test_an_unknown_fold_chromosome_is_refused() -> None:
    bad = dict(_manifest().bam_chromosome)
    bad["minos-chr18-00"] = "chrX"
    with pytest.raises(TrainingDatasetError, match="unknown fold chromosomes"):
        CvManifest(bam_chromosome=bad)


def test_a_row_referencing_an_unknown_bam_is_refused() -> None:
    with pytest.raises(TrainingDatasetError, match="absent from the CV manifest"):
        _dataset((_row(dataset_id="minos-chr18-99"),))


# --------------------------------------------------------------------------------------------
# dataset identity
# --------------------------------------------------------------------------------------------
def test_the_dataset_identity_is_row_order_independent() -> None:
    rows = tuple(_row(config_hash=f"{i:064d}") for i in range(6))
    assert _dataset(rows).identity() == _dataset(tuple(reversed(rows))).identity()


def test_the_dataset_identity_moves_when_evidence_moves() -> None:
    rows = tuple(_row(config_hash=f"{i:064d}") for i in range(4))
    baseline = _dataset(rows).identity()
    changed = (*rows[:3], _row(config_hash=f"{3:064d}", minos_score=0.9))
    assert _dataset(changed).identity() != baseline


# --------------------------------------------------------------------------------------------
# config encoding
# --------------------------------------------------------------------------------------------
def test_the_encoder_matches_the_frozen_parameter_space() -> None:
    encoding = build_config_encoding(_repo())
    assert len(encoding.feature_names) == 28
    assert encoding.fixed_names == ("cfg.sample_ploidy", "cfg.dont_use_soft_clipped_bases")
    assert encoding.identity() == encoding.identity()


def test_enums_are_one_hot_rather_than_ordinal() -> None:
    """CONSERVATIVE is not "greater than" AGGRESSIVE; an ordinal code would assert that."""
    names = build_config_encoding(_repo()).feature_names
    assert "cfg.pcr_indel_model=CONSERVATIVE" in names
    assert "cfg.pcr_indel_model=HOSTILE" in names
    assert "cfg.pcr_indel_model" not in names


def _valid_config(repo: Path) -> dict[str, Any]:
    document = json.loads(
        (repo / "manifests/l2f_gatk_parameter_space_v1.json").read_text(encoding="utf-8")
    )
    parameters = document.get("content", document)["parameters"]
    return {p["name"]: p["default"] for p in parameters}


def test_a_valid_configuration_encodes_into_the_unit_interval() -> None:
    encoding = build_config_encoding(_repo())
    vector = encoding.encode(_valid_config(_repo()))
    assert len(vector) == len(encoding.feature_names)
    assert all(0.0 <= v <= 1.0 for v in vector)


def test_encoding_is_deterministic() -> None:
    encoding = build_config_encoding(_repo())
    config = _valid_config(_repo())
    assert encoding.encode(config) == encoding.encode(config)


def test_an_unknown_parameter_is_refused() -> None:
    encoding = build_config_encoding(_repo())
    config = {**_valid_config(_repo()), "invented_knob": 3}
    with pytest.raises(ConfigEncoderError, match="outside the frozen space"):
        encoding.encode(config)


def test_a_missing_parameter_is_refused() -> None:
    encoding = build_config_encoding(_repo())
    config = _valid_config(_repo())
    config.pop("min_base_quality_score")
    with pytest.raises(ConfigEncoderError, match="missing frozen parameters"):
        encoding.encode(config)


def test_an_out_of_range_value_is_refused() -> None:
    encoding = build_config_encoding(_repo())
    config = {**_valid_config(_repo()), "min_base_quality_score": 9999}
    with pytest.raises(ConfigEncoderError, match="outside its frozen range"):
        encoding.encode(config)


def test_an_enum_value_outside_its_vocabulary_is_refused() -> None:
    encoding = build_config_encoding(_repo())
    config = {**_valid_config(_repo()), "pcr_indel_model": "MADE_UP"}
    with pytest.raises(ConfigEncoderError, match="frozen vocabulary"):
        encoding.encode(config)


def test_a_fixed_parameter_may_not_be_varied() -> None:
    encoding = build_config_encoding(_repo())
    config = {**_valid_config(_repo()), "sample_ploidy": 3}
    with pytest.raises(ConfigEncoderError, match="is fixed at"):
        encoding.encode(config)


# --------------------------------------------------------------------------------------------
# model spec, bundle, metrics
# --------------------------------------------------------------------------------------------
def _spec(**over: Any) -> ModelSpec:
    fields: dict[str, Any] = {
        "family": "LINEAR_REGULARIZED",
        "implementation": "sklearn.linear_model.Ridge",
        "target_formulation": TARGET_FORMULATION,
        "feature_schema_hash": _H,
        "config_schema_hash": _H,
        "transform_specification": {"standardize": True},
        "hyperparameters": {"alpha": 1.0},
        "random_seed": 20260904,
        "loss": "squared_error",
        "failure_risk_formulation": "LOGISTIC_P_SUCCESS",
        "calibration_method": "ISOTONIC_ON_OOF",
        "ood_method": "STANDARDIZED_FEATURE_DISTANCE",
        "training_dataset_hash": _H,
        "cv_manifest_hash": _H,
    }
    fields.update(over)
    return ModelSpec(**fields)


def test_the_candidate_families_start_from_trivial_references() -> None:
    assert MODEL_FAMILIES[:4] == (
        "CONSTANT_SAFE_BASELINE",
        "GLOBAL_MEAN",
        "CONFIG_ONLY",
        "BAM_FEATURES_ONLY",
    )
    assert MODEL_FAMILIES[-1] == "COMPACT_MLP", "highest capacity must be last, not first"


def test_an_unfrozen_model_family_is_refused() -> None:
    with pytest.raises(ModelSpecError, match="not a frozen model family"):
        _spec(family="MY_NEW_TRANSFORMER")


def test_the_spec_hash_is_deterministic_and_covers_the_seed() -> None:
    assert _spec().identity() == _spec().identity()
    assert _spec(random_seed=1).identity() != _spec(random_seed=2).identity()
    assert _spec(hyperparameters={"alpha": 2.0}).identity() != _spec().identity()


def test_the_selection_order_puts_leakage_and_downside_before_accuracy() -> None:
    assert SELECTION_ORDER[0] == "no_leakage_and_complete_folds"
    assert SELECTION_ORDER.index("downside_not_worse_than_safe_baseline") < SELECTION_ORDER.index(
        "lowest_bam_grouped_regret"
    )
    assert SELECTION_ORDER[-1] == "deterministic_spec_hash_tie_break"


def _artifact(name: str) -> ArtifactRef:
    return ArtifactRef(
        path=f"/tmp/{name}", sha256=_H, media_type="application/octet-stream", size_bytes=1
    )


def test_the_bundle_binds_every_artifact_by_content() -> None:
    bundle = ModelBundle(
        spec_hash=_H,
        baseline_qualified_gate_hash=_H,
        safe_baseline_config_hash=_H,
        training_dataset_hash=_H,
        cv_manifest_hash=_H,
        model_artifact=_artifact("model"),
        transform_artifact=_artifact("transform"),
        oof_prediction_artifact=_artifact("oof"),
        cv_metric_artifact=_artifact("cv"),
        calibration_artifact=_artifact("cal"),
        runtime={"python": "3.12"},
    )
    assert bundle.identity() == bundle.identity()
    tampered = bundle.model_copy(update={"model_artifact": _artifact("other")})
    assert tampered.identity() != bundle.identity()


def test_regret_is_oracle_minus_selected_and_lower_is_better() -> None:
    assert REGRET_ORIENTATION == "ORACLE_MINUS_SELECTED_LOWER_IS_BETTER"
    actual = {("b1", "c1"): 0.9, ("b1", "c2"): 0.4, ("b2", "c1"): 0.5, ("b2", "c2"): 0.8}
    perfect = dict(actual)
    assert bam_grouped_regret(actual, perfect) == {"b1": 0.0, "b2": 0.0}
    inverted = {k: -v for k, v in actual.items()}
    assert bam_grouped_regret(actual, inverted) == {
        "b1": pytest.approx(0.5),
        "b2": pytest.approx(0.3),
    }


def test_regret_only_considers_configs_actually_run_for_that_bam() -> None:
    """The matrix is 70.6% sparse; a config never run for a BAM has no measured outcome."""
    actual = {("b1", "c1"): 0.9, ("b1", "c2"): 0.4}
    predicted = {("b1", "c1"): 0.1, ("b1", "c2"): 0.2, ("b1", "never_run"): 99.0}
    assert bam_grouped_regret(actual, predicted) == {"b1": pytest.approx(0.5)}


def test_downside_reports_the_tail_not_only_the_mean() -> None:
    summary = downside_summary({"a": 0.0, "b": 0.0, "c": 0.0, "d": 0.8})
    assert summary["mean_regret"] == pytest.approx(0.2)
    assert summary["max_regret"] == pytest.approx(0.8)
    assert summary["cvar_regret"] == pytest.approx(0.8)
    assert summary["zero_regret_fraction"] == pytest.approx(0.75)


def test_spearman_measures_ranking() -> None:
    assert spearman([1.0, 2.0, 3.0], [10.0, 20.0, 30.0]) == pytest.approx(1.0)
    assert spearman([1.0, 2.0, 3.0], [30.0, 20.0, 10.0]) == pytest.approx(-1.0)
    with pytest.raises(MetricsError):
        spearman([1.0, 1.0, 1.0], [1.0, 2.0, 3.0])


def test_calibration_error_is_computed_over_bins() -> None:
    perfect = calibration_error([0.1, 0.5, 0.9], [0.1, 0.5, 0.9])
    assert perfect["absolute_calibration_error"] == pytest.approx(0.0)
    biased = calibration_error([0.1, 0.5, 0.9], [0.3, 0.7, 1.0])
    assert biased["absolute_calibration_error"] > 0.0


# --------------------------------------------------------------------------------------------
# gate design and stage locks
# --------------------------------------------------------------------------------------------
def test_the_models_qualified_gate_is_designed_but_not_issued() -> None:
    required = required_checks_for("MODELS-QUALIFIED")
    assert len(required) == 34
    for expected in (
        "no_test_row_present",
        "every_oof_prediction_out_of_group",
        "transforms_fitted_fold_local",
        "select_config_still_blocked",
        "no_catastrophic_regression_vs_safe_baseline",
        "candidate_specs_frozen_before_validation",
    ):
        assert expected in required, expected
    assert not (_repo() / "gates/models-qualified.json").exists()
    assert not list((_repo() / "gates").glob("*MODELS-QUALIFIED*"))


def test_select_config_remains_blocked() -> None:
    from minos_engine.layer2.service import Layer2Service

    with pytest.raises(Exception) as excinfo:
        Layer2Service().select_config(None)  # type: ignore[arg-type]
    assert (
        "StageNotReady" in type(excinfo.value).__name__ or "not ready" in str(excinfo.value).lower()
    )


def test_baseline_qualified_remains_the_entry_gate() -> None:
    gate = json.loads((_repo() / "gates/baseline-qualified.json").read_text(encoding="utf-8"))
    assert gate["status"] == "PASS"
    assert gate["gate_hash"] == ("b9436bf3263925ebe187ed5550c7214cfa92bc75a0dd2607a7766103bfa6befa")
