"""L2-G v2 contracts: the admitted target, scientific dedup, equal-BAM weight, and the leaks.

The v1 contract froze ``P(GATK success)`` as the probability term and let the score regressor
consume all 1140 evaluations. The real campaign has 986 admitted and 154 non-admitted, so that
would have trained the regressor on 154 rows the frozen objective refuses to treat as utility.
Most of what follows exists to make that class of error impossible to reintroduce.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from minos_engine.gates.required_checks import required_checks_for
from minos_engine.layer2.features.contracts import AUTHORITATIVE_COLUMNS
from minos_engine.models.config_encoder import ConfigEncoderError, build_config_encoding
from minos_engine.models.contract import (
    BAMS_PER_CHROMOSOME,
    CV_FOLD_CHROMOSOMES,
    DEDUP_POLICY,
    FAILURE_UTILITY,
    FEATURE_COLUMN_COUNT,
    FORBIDDEN_AT_INFERENCE,
    FROZEN_FEATURE_SET_HASH,
    OUTCOME_ADMITTED,
    OUTCOME_EXECUTION_FAILURE,
    OUTCOME_NON_ADMISSION,
    SAFE_BASELINE_CONFIG_HASH,
    TARGET_FORMULATION,
    TRAINING_CONTRACT_SCHEMA,
    WEIGHTING_POLICY,
    compute_training_contract_hash,
    training_contract_content,
)
from minos_engine.models.dataset import (
    BamFeatureBinding,
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
from minos_engine.models.protocol import (
    CANDIDATE_GRID,
    MODEL_BACKEND,
    compute_training_protocol_hash,
    training_protocol_content,
)
from minos_engine.models.spec import (
    MODEL_FAMILIES,
    PROMOTABLE_FAMILIES,
    REFERENCE_FAMILIES,
    SELECTION_ORDER,
    ArtifactRef,
    ModelBundle,
    ModelSpec,
    ModelSpecError,
)

_H = "a" * 64


def _repo() -> Path:
    return Path(__file__).resolve().parents[3]


def _frozen() -> tuple[tuple[str, str], ...]:
    """The real frozen fifty. Synthetic ids no longer instantiate a dataset, by design."""
    from minos_engine.baseline.schedule import build_train_schedule

    return tuple((m.dataset_id, m.chromosome) for m in build_train_schedule().members)


def _bam(i: int) -> str:
    return _frozen()[i][0]


def _chromosome(i: int) -> str:
    return _frozen()[i][1]


def _hex(seed: int) -> str:
    return f"{seed:064x}"


def _manifest(counts: dict[str, int] | None = None) -> CvManifest:
    if counts is None:
        return CvManifest(bam_chromosome=dict(_frozen()))
    mapping: dict[str, str] = {}
    index = 0
    for chromosome, count in counts.items():
        for _ in range(count):
            mapping[f"bam-{index:02d}"] = chromosome
            index += 1
    return CvManifest(bam_chromosome=mapping)


def _features(n: int = 50) -> tuple[BamFeatureBinding, ...]:
    return tuple(
        BamFeatureBinding(
            dataset_id=_bam(i), vector_hash=_hex(1000 + i), feature_values_hash=_hex(2000 + i)
        )
        for i in range(n)
    )


def _row(**over: Any) -> TrainingRow:
    fields: dict[str, Any] = {
        "dataset_id": _bam(0),
        "chromosome": _chromosome(0),
        "config_hash": _hex(31337),
        "partition": "train",
        "outcome": OUTCOME_ADMITTED,
        "admitted_score": 0.7,
        "admission_code": "ADMITTED",
        "source_job_keys": (_hex(9001),),
        "source_plan_hashes": (_hex(8001),),
    }
    fields.update(over)
    return TrainingRow(**fields)


def _cover_all_bams() -> tuple[TrainingRow, ...]:
    return tuple(
        _row(dataset_id=_bam(i), chromosome=_chromosome(i), config_hash=_hex(i)) for i in range(50)
    )


def _dataset(rows: tuple[TrainingRow, ...] | None = None, **over: Any) -> TrainingDataset:
    fields: dict[str, Any] = {
        "baseline_qualified_gate_hash": _H,
        "baseline_selected_hash": _H,
        "feature_registry_hash": _H,
        "config_encoding_identity": _H,
        "parameter_space_hash": _H,
        "scoring_contract_hash": _H,
        "execution_environment_hash": _H,
        "train_plan_hashes": (_hex(7001),),
        "training_contract_hash": compute_training_contract_hash(),
        "training_protocol_hash": compute_training_protocol_hash(),
        "train_schedule_hash": _hex(4242),
        "feature_set_hash": FROZEN_FEATURE_SET_HASH,
        "feature_matrix_hash": _H,
        "feature_matrix_artifact_sha256": _H,
        "bam_features": _features(),
        "feature_names": tuple(AUTHORITATIVE_COLUMNS),
        "config_feature_names": ("cfg.min_base_quality_score",),
        "rows": rows if rows is not None else _cover_all_bams(),
        "cv_manifest": _manifest(),
    }
    fields.update(over)
    return TrainingDataset(**fields)


# --------------------------------------------------------------------------------------------
# v2 target semantics -- the corrected defect
# --------------------------------------------------------------------------------------------
def test_the_contract_is_v2_and_factorises_over_ADMISSION_not_execution() -> None:
    assert TRAINING_CONTRACT_SCHEMA == "l2g-training-contract-v2"
    assert TARGET_FORMULATION == "B_JOINT_EXPECTED_UTILITY_OVER_ADMISSION"
    content = training_contract_content()
    assert content["score_model_examples"] == "ADMITTED_ONLY"
    assert content["admission_model_examples"] == "EVERY_DECIDED_OUTCOME"
    assert content["superseded"]["l2g-training-contract-v1"] == "SUPERSEDED_BEFORE_FIRST_MODEL_FIT"
    assert content["failure_utility"] == 0.0
    assert content["feature_column_count"] == 129
    assert content["feature_set_hash"] == FROZEN_FEATURE_SET_HASH


def test_a_non_admitted_evaluation_is_never_a_score_label() -> None:
    """THE v1 defect. The frozen objective refuses to consume that number as utility."""
    with pytest.raises(TrainingDatasetError, match="NOT utility evidence"):
        _row(
            outcome=OUTCOME_NON_ADMISSION,
            admitted_score=0.42,
            admission_code="ZERO_INPUT_FINGERPRINT",
        )


def test_a_non_admission_contributes_admission_target_zero() -> None:
    row = _row(
        outcome=OUTCOME_NON_ADMISSION, admitted_score=None, admission_code="ZERO_INPUT_FINGERPRINT"
    )
    assert row.admission_label == 0
    assert row.is_score_example is False
    assert row.admission_code == "ZERO_INPUT_FINGERPRINT"


def test_a_non_admission_may_not_be_disguised_as_a_gatk_crash() -> None:
    with pytest.raises(TrainingDatasetError, match="not a GATK crash"):
        _row(
            outcome=OUTCOME_NON_ADMISSION,
            admitted_score=None,
            admission_code="NONPOSITIVE_SCORE",
            execution_failure_code="GATK_NONZERO_EXIT",
        )


def test_a_non_admission_must_keep_the_code_that_explains_it() -> None:
    with pytest.raises(TrainingDatasetError, match="admission_code"):
        _row(outcome=OUTCOME_NON_ADMISSION, admitted_score=None, admission_code=None)


def test_an_execution_failure_contributes_zero_without_a_fabricated_score() -> None:
    row = _row(
        outcome=OUTCOME_EXECUTION_FAILURE,
        admitted_score=None,
        admission_code=None,
        execution_failure_code="GATK_NONZERO_EXIT",
    )
    assert row.admission_label == 0
    assert row.is_score_example is False
    assert row.admitted_score is None
    assert FAILURE_UTILITY == 0.0


def test_a_crashed_run_may_not_be_given_a_score() -> None:
    with pytest.raises(TrainingDatasetError, match="produced no score"):
        _row(
            outcome=OUTCOME_EXECUTION_FAILURE,
            admitted_score=0.0,
            execution_failure_code="GATK_NONZERO_EXIT",
        )


@pytest.mark.parametrize(
    "code", ["HAPPY_TIMEOUT", "EVALUATION_ERROR", "ARTIFACT_PUBLISH_FAILED", "TRUTH_BYTES_MISMATCH"]
)
def test_an_infrastructure_incident_is_never_a_training_label(code: str) -> None:
    with pytest.raises(TrainingDatasetError, match="never a training label"):
        _row(outcome=OUTCOME_EXECUTION_FAILURE, admitted_score=None, execution_failure_code=code)


def test_an_unknown_outcome_class_is_refused() -> None:
    with pytest.raises(TrainingDatasetError, match="not a decided outcome class"):
        _row(outcome="PROBABLY_FINE")


def test_an_admitted_example_requires_its_score() -> None:
    with pytest.raises(TrainingDatasetError, match="must carry its persisted score"):
        _row(outcome=OUTCOME_ADMITTED, admitted_score=None)


def test_the_two_components_see_different_example_sets() -> None:
    rows = (
        *_cover_all_bams()[:48],
        _row(
            dataset_id=_bam(48),
            chromosome=CV_FOLD_CHROMOSOMES[3],
            config_hash=f"{48:064d}",
            outcome=OUTCOME_NON_ADMISSION,
            admitted_score=None,
            admission_code="ZERO_INPUT_FINGERPRINT",
        ),
        _row(
            dataset_id=_bam(49),
            chromosome=CV_FOLD_CHROMOSOMES[4],
            config_hash=f"{49:064d}",
            outcome=OUTCOME_EXECUTION_FAILURE,
            admitted_score=None,
            admission_code=None,
            execution_failure_code="GATK_NONZERO_EXIT",
        ),
    )
    dataset = _dataset(rows)
    assert len(dataset.admission_examples) == 50
    assert len(dataset.score_examples) == 48
    assert dataset.bams_without_score_examples() == (_bam(48), _bam(49))


# --------------------------------------------------------------------------------------------
# feature authority -- 129, not 141
# --------------------------------------------------------------------------------------------
def test_the_qualified_129_column_feature_set_is_required() -> None:
    assert FEATURE_COLUMN_COUNT == 129
    assert FROZEN_FEATURE_SET_HASH == (
        "7e867dfa5633044b69869be8a87fac564431a73a183aa0ab0b1b13158a7c176f"
    )
    from minos_engine.layer2.features.contracts import (
        AUTHORITATIVE_COLUMNS,
        EXPECTED_COLUMN_COUNT,
    )

    assert EXPECTED_COLUMN_COUNT == FEATURE_COLUMN_COUNT
    assert len(AUTHORITATIVE_COLUMNS) == FEATURE_COLUMN_COUNT


def test_the_wider_141_field_registry_result_is_refused() -> None:
    """The registry's eligible set is wider than the qualified production matrix."""
    with pytest.raises(TrainingDatasetError, match="not the qualified"):
        _dataset(feature_names=tuple(f"feat.{i:03d}" for i in range(141)))


def test_a_foreign_feature_set_hash_is_refused() -> None:
    with pytest.raises(TrainingDatasetError, match="feature promotion"):
        _dataset(feature_set_hash="b" * 64)


def test_one_feature_value_change_moves_the_dataset_identity() -> None:
    """Binding names alone would let a predictor VALUE change invisibly."""
    baseline = _dataset().identity()
    moved = list(_features())
    moved[7] = BamFeatureBinding(
        dataset_id=moved[7].dataset_id,
        vector_hash=_hex(555555),
        feature_values_hash=moved[7].feature_values_hash,
    )
    assert _dataset(bam_features=tuple(moved)).identity() != baseline


def test_a_missing_or_duplicated_feature_member_is_refused() -> None:
    with pytest.raises(TrainingDatasetError, match="different BAM sets"):
        _dataset(bam_features=_features(49))
    duplicated = (*_features(49), _features(1)[0])
    with pytest.raises(TrainingDatasetError, match="appears twice"):
        _dataset(bam_features=duplicated)


# --------------------------------------------------------------------------------------------
# dedup and weighting
# --------------------------------------------------------------------------------------------
def test_a_repeated_scientific_pair_is_refused_in_the_learning_table() -> None:
    """The campaign scheduled 115 pairs more than once; a cell must not gain weight for that."""
    rows = (*_cover_all_bams(), _row(dataset_id=_bam(0), config_hash=f"{0:064d}"))
    with pytest.raises(TrainingDatasetError, match="more than once"):
        _dataset(rows)


def test_phase_order_does_not_change_a_learning_example_identity() -> None:
    a = _row(source_job_keys=("j1", "j2"), source_plan_hashes=("pA", "pB"))
    b = _row(source_job_keys=("j2", "j1"), source_plan_hashes=("pB", "pA"))
    assert a.identity() == b.identity()


def test_each_bam_carries_equal_total_admission_weight() -> None:
    """The unbalanced campaign gives some BAMs 10 examples and others 80."""
    rows = [
        _row(dataset_id=_bam(i), chromosome=_chromosome(i), config_hash=_hex(i)) for i in range(50)
    ]
    # give BAM 0 four extra examples, as Phase-B BAMs really do have
    rows += [
        _row(dataset_id=_bam(0), chromosome="chr18", config_hash=f"{900 + k:064d}")
        for k in range(4)
    ]
    dataset = _dataset(tuple(rows))
    weights = dataset.admission_weights()
    totals: dict[str, float] = {}
    for row in dataset.rows:
        totals[row.dataset_id] = totals.get(row.dataset_id, 0.0) + weights[row.identity()]
    assert all(abs(t - 1.0) < 1e-9 for t in totals.values()), totals
    assert len([r for r in dataset.rows if r.dataset_id == _bam(0)]) == 5


def test_each_bam_with_scores_carries_equal_total_score_weight() -> None:
    rows = [
        _row(dataset_id=_bam(i), chromosome=_chromosome(i), config_hash=_hex(i)) for i in range(50)
    ]
    rows += [
        _row(dataset_id=_bam(1), chromosome="chr19", config_hash=f"{800 + k:064d}")
        for k in range(3)
    ]
    dataset = _dataset(tuple(rows))
    weights = dataset.score_weights()
    totals: dict[str, float] = {}
    for row in dataset.score_examples:
        totals[row.dataset_id] = totals.get(row.dataset_id, 0.0) + weights[row.identity()]
    assert all(abs(t - 1.0) < 1e-9 for t in totals.values()), totals


def test_a_bam_without_admitted_examples_is_declared_not_divided_by_zero() -> None:
    rows = list(_cover_all_bams()[:49])
    rows.append(
        _row(
            dataset_id=_bam(49),
            chromosome=CV_FOLD_CHROMOSOMES[4],
            config_hash=f"{49:064d}",
            outcome=OUTCOME_EXECUTION_FAILURE,
            admitted_score=None,
            admission_code=None,
            execution_failure_code="GATK_NONZERO_EXIT",
        )
    )
    dataset = _dataset(tuple(rows))
    assert dataset.bams_without_score_examples() == (_bam(49),)
    assert dataset.score_weights()  # no ZeroDivisionError


def test_the_policies_are_bound_into_the_dataset_identity() -> None:
    content = _dataset().content()
    assert content["dedup_policy"] == DEDUP_POLICY == "ONE_EXAMPLE_PER_BAM_CONFIG_PAIR"
    assert content["weighting_policy"] == WEIGHTING_POLICY == "EQUAL_BAM_TOTAL"


# --------------------------------------------------------------------------------------------
# partition, predictors, CV
# --------------------------------------------------------------------------------------------
@pytest.mark.parametrize("partition", ["test", "validation"])
def test_a_non_train_row_is_refused_rather_than_filtered(partition: str) -> None:
    with pytest.raises(TrainingDatasetError, match="TRAIN only"):
        _row(partition=partition)


@pytest.mark.parametrize("leaked", ["minos_score", "admitted", "truth_vcf", "evaluation_hash"])
def test_a_forbidden_field_cannot_enter_the_predictor_matrix(leaked: str) -> None:
    names = (*[f"feat.{i:03d}" for i in range(FEATURE_COLUMN_COUNT - 1)], leaked)
    with pytest.raises(TrainingDatasetError, match="never see at inference"):
        _dataset(feature_names=names)


@pytest.mark.parametrize("identity", ["dataset_id", "round_id", "partition", "chromosome"])
def test_identity_and_grouping_metadata_are_not_predictors(identity: str) -> None:
    names = (*[f"feat.{i:03d}" for i in range(FEATURE_COLUMN_COUNT - 1)], identity)
    with pytest.raises(TrainingDatasetError, match="metadata, not a predictor|never see"):
        _dataset(feature_names=names)


def test_exactly_ten_bams_per_chromosome() -> None:
    assert BAMS_PER_CHROMOSOME == 10
    manifest = _manifest()
    for chromosome in CV_FOLD_CHROMOSOMES:
        assert sum(1 for c in manifest.bam_chromosome.values() if c == chromosome) == 10


def test_a_lopsided_fifty_bam_split_is_refused() -> None:
    """46/1/1/1/1 totals fifty and is five folds in name only."""
    with pytest.raises(TrainingDatasetError, match="five folds in name only"):
        _manifest({"chr18": 46, "chr19": 1, "chr20": 1, "chr21": 1, "chr22": 1})


def test_five_folds_hold_out_ten_bams_each_and_never_overlap() -> None:
    folds = _manifest().folds()
    assert len(folds) == 5
    for train, held in folds:
        assert len(held) == 10 and len(train) == 40
        assert not (train & held)


def test_a_dataset_covering_only_some_manifest_bams_is_refused() -> None:
    with pytest.raises(TrainingDatasetError, match="contribute no learning example"):
        _dataset(_cover_all_bams()[:40])


def test_the_dataset_identity_is_row_order_independent() -> None:
    rows = _cover_all_bams()
    assert _dataset(rows).identity() == _dataset(tuple(reversed(rows))).identity()


# --------------------------------------------------------------------------------------------
# config encoder authority
# --------------------------------------------------------------------------------------------
def test_the_encoder_comes_from_the_accepted_authority() -> None:
    import inspect

    from minos_engine.models import config_encoder as mod

    source = inspect.getsource(mod.build_config_encoding)
    assert "load_committed_live_gatk_parameter_space" in source
    assert "json.loads" not in source, "the encoder must not re-parse the manifest itself"


def test_the_encoding_shape_and_identity_are_unchanged() -> None:
    encoding = build_config_encoding()
    assert len(encoding.feature_names) == 28
    assert encoding.fixed_names == ("cfg.sample_ploidy", "cfg.dont_use_soft_clipped_bases")
    assert encoding.identity() == (
        "3053fed09a1a7fdc9462a963871564275c88e4eca5fe3a898d2d6821c36b1fe4"
    )


def test_a_content_tampered_manifest_with_an_untouched_hash_is_refused(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """The old direct-parse path trusted the document's own hash field. This one recomputes."""
    from minos_engine.experiments import gatk_live_space as space_mod

    def tampered() -> Any:
        raise space_mod.LiveSpaceError("recomputed parameter-space hash does not match")

    monkeypatch.setattr(space_mod, "load_committed_live_gatk_parameter_space", tampered)
    with pytest.raises(Exception, match="recomputed"):
        build_config_encoding()


def _valid_config() -> dict[str, Any]:
    from minos_engine.experiments.gatk_live_space import (
        load_committed_live_gatk_parameter_space,
    )

    return {p.name: p.default for p in load_committed_live_gatk_parameter_space().parameters}


def test_a_valid_configuration_encodes_into_the_unit_interval() -> None:
    encoding = build_config_encoding()
    vector = encoding.encode(_valid_config())
    assert len(vector) == 28
    assert all(0.0 <= v <= 1.0 for v in vector)


def test_enums_are_one_hot_rather_than_ordinal() -> None:
    names = build_config_encoding().feature_names
    assert "cfg.pcr_indel_model=CONSERVATIVE" in names
    assert "cfg.pcr_indel_model" not in names


@pytest.mark.parametrize(
    ("mutate", "match"),
    [
        pytest.param(lambda c: {**c, "invented": 1}, "outside the frozen space", id="unknown"),
        pytest.param(
            lambda c: {k: v for k, v in c.items() if k != "sample_ploidy"},
            "missing frozen parameters",
            id="missing",
        ),
        pytest.param(
            lambda c: {**c, "min_base_quality_score": 9999}, "outside its frozen range", id="range"
        ),
        pytest.param(lambda c: {**c, "pcr_indel_model": "MADE_UP"}, "frozen vocabulary", id="enum"),
        pytest.param(lambda c: {**c, "sample_ploidy": 3}, "is fixed at", id="fixed"),
    ],
)
def test_an_invalid_configuration_is_refused(mutate: Any, match: str) -> None:
    encoding = build_config_encoding()
    with pytest.raises(ConfigEncoderError, match=match):
        encoding.encode(mutate(_valid_config()))


# --------------------------------------------------------------------------------------------
# protocol, spec, bundle
# --------------------------------------------------------------------------------------------
def test_references_and_promotable_families_are_distinct() -> None:
    assert set(REFERENCE_FAMILIES).isdisjoint(PROMOTABLE_FAMILIES)
    assert "CONSTANT_SAFE_BASELINE" in REFERENCE_FAMILIES
    assert "LINEAR_REGULARIZED" in PROMOTABLE_FAMILIES
    assert (*REFERENCE_FAMILIES, *PROMOTABLE_FAMILIES) == MODEL_FAMILIES


def test_the_backend_is_pinned_and_declared() -> None:
    assert MODEL_BACKEND["library"] == "scikit-learn"
    assert MODEL_BACKEND["constraint"] == "==1.9.0"
    pyproject = (_repo() / "pyproject.toml").read_text(encoding="utf-8")
    assert "scikit-learn==1.9.0" in pyproject


def test_the_candidate_grid_is_finite_and_predeclared() -> None:
    assert 1 <= len(CANDIDATE_GRID) <= 12
    assert all(c["family"] in PROMOTABLE_FAMILIES for c in CANDIDATE_GRID)
    content = training_protocol_content()
    assert content["hpo"] == "FINITE_PREDECLARED_GRID_NO_ADAPTIVE_SEARCH"


def test_validation_never_chooses_the_rules_used_to_judge_it() -> None:
    rule = training_protocol_content()["threshold_rule"]
    assert rule["invariant"] == "VALIDATION_NEVER_CHOOSES_THE_RULES_USED_TO_JUDGE_IT"
    assert "SEPARATE_SOURCE_FREEZE" in rule["stage_2"]
    assert training_protocol_content()["validation_use"] == "SELECTION_ONLY_AFTER_STAGE_2_FREEZE"


def test_the_protocol_hash_is_deterministic() -> None:
    assert compute_training_protocol_hash() == compute_training_protocol_hash()


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
        "weighting_policy": WEIGHTING_POLICY,
        "dedup_policy": DEDUP_POLICY,
        "failure_risk_formulation": "LOGISTIC_P_ADMISSION",
        "calibration_method": "ISOTONIC_ON_OOF",
        "ood_method": "STANDARDIZED_FEATURE_DISTANCE",
        "training_dataset_hash": _H,
        "cv_manifest_hash": _H,
    }
    fields.update(over)
    return ModelSpec(**fields)


def test_an_unfrozen_model_family_is_refused() -> None:
    with pytest.raises(ModelSpecError, match="not a frozen model family"):
        _spec(family="MY_NEW_TRANSFORMER")


def test_the_spec_hash_covers_seed_hyperparameters_and_policies() -> None:
    baseline = _spec().identity()
    assert _spec(random_seed=1).identity() != baseline
    assert _spec(hyperparameters={"alpha": 2.0}).identity() != baseline
    assert _spec(weighting_policy="PER_ROW").identity() != baseline
    assert _spec(dedup_policy="NONE").identity() != baseline


def test_the_selection_order_puts_leakage_and_downside_before_accuracy() -> None:
    assert SELECTION_ORDER[0] == "no_leakage_and_complete_folds"
    assert SELECTION_ORDER.index("downside_not_worse_than_safe_baseline") < SELECTION_ORDER.index(
        "lowest_bam_grouped_regret"
    )


def _artifact(role: str, sha: str = _H, path: str = "/tmp/a") -> ArtifactRef:
    return ArtifactRef(
        role=role, path=path, sha256=sha, media_type="application/octet-stream", size_bytes=1
    )


def _bundle(**over: Any) -> ModelBundle:
    fields: dict[str, Any] = {
        "spec_hash": _H,
        "baseline_qualified_gate_hash": _H,
        "safe_baseline_config_hash": _H,
        "training_dataset_hash": _H,
        "cv_manifest_hash": _H,
        "model_artifact": _artifact("model"),
        "transform_artifact": _artifact("transform"),
        "oof_prediction_artifact": _artifact("oof"),
        "cv_metric_artifact": _artifact("cv"),
        "calibration_artifact": _artifact("calibration"),
        "runtime": {"python": "3.12", "scikit-learn": "1.9.0"},
    }
    fields.update(over)
    return ModelBundle(**fields)


def test_the_bundle_identity_is_host_independent() -> None:
    """The same bytes under a different absolute directory are the same artifact."""
    here = _bundle()
    elsewhere = _bundle(model_artifact=_artifact("model", path="/mnt/other/place/model.joblib"))
    assert here.identity() == elsewhere.identity()


def test_the_bundle_identity_moves_when_content_moves() -> None:
    assert (
        _bundle(model_artifact=_artifact("model", sha="b" * 64)).identity() != _bundle().identity()
    )


@pytest.mark.parametrize("bad", ["A" * 64, "z" * 64, "abc"])
def test_a_malformed_artifact_sha_is_refused(bad: str) -> None:
    with pytest.raises((ModelSpecError, ValueError)):
        _artifact("model", sha=bad)


# --------------------------------------------------------------------------------------------
# metrics
# --------------------------------------------------------------------------------------------
def test_regret_is_oracle_minus_selected_and_lower_is_better() -> None:
    assert REGRET_ORIENTATION == "ORACLE_MINUS_SELECTED_LOWER_IS_BETTER"
    actual = {("b1", "c1"): 0.9, ("b1", "c2"): 0.4}
    assert bam_grouped_regret(actual, dict(actual)) == {"b1": 0.0}
    assert bam_grouped_regret(actual, {k: -v for k, v in actual.items()}) == {
        "b1": pytest.approx(0.5)
    }


def test_regret_only_considers_configs_actually_run_for_that_bam() -> None:
    actual = {("b1", "c1"): 0.9, ("b1", "c2"): 0.4}
    predicted = {("b1", "c1"): 0.1, ("b1", "c2"): 0.2, ("b1", "never_run"): 99.0}
    assert bam_grouped_regret(actual, predicted) == {"b1": pytest.approx(0.5)}


def test_downside_reports_the_tail() -> None:
    summary = downside_summary({"a": 0.0, "b": 0.0, "c": 0.0, "d": 0.8})
    assert summary["max_regret"] == pytest.approx(0.8)
    assert summary["cvar_regret"] == pytest.approx(0.8)


def test_spearman_and_calibration_behave() -> None:
    assert spearman([1.0, 2.0, 3.0], [10.0, 20.0, 30.0]) == pytest.approx(1.0)
    with pytest.raises(MetricsError):
        spearman([1.0, 1.0, 1.0], [1.0, 2.0, 3.0])
    assert calibration_error([0.1, 0.5], [0.1, 0.5])["absolute_calibration_error"] == pytest.approx(
        0.0
    )


# --------------------------------------------------------------------------------------------
# gate and stage locks
# --------------------------------------------------------------------------------------------
def test_the_models_qualified_gate_covers_the_corrected_semantics() -> None:
    required = required_checks_for("MODELS-QUALIFIED")
    assert len(required) == 59
    for expected in (
        "training_contract_v2_hash_exact",
        "training_protocol_hash_exact",
        "feature_set_hash_is_the_qualified_129_column_set",
        "feature_matrix_value_identity_bound",
        "scientific_pair_dedup_exact",
        "equal_bam_weighting_exact",
        "admitted_target_semantics_exact",
        "non_admission_not_a_score_label",
        "ten_bams_per_chromosome_exact",
        "finite_candidate_grid_no_adaptive_search",
        "validation_not_consulted_during_train_development",
        "bundle_identity_host_independent",
        "promotable_family_selected_not_a_reference",
        "config_encoder_built_from_accepted_authority",
    ):
        assert expected in required, expected


def test_no_models_qualified_gate_is_issued_yet() -> None:
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


def test_the_contract_hashes_are_deterministic() -> None:
    assert compute_training_contract_hash() == compute_training_contract_hash()
    assert SAFE_BASELINE_CONFIG_HASH in training_contract_content().values()
    assert "minos_score" in FORBIDDEN_AT_INFERENCE
