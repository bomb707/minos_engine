"""L2-G pre-fit authority: the caller may not author science, and the science must be exact.

Every negative here answers one question: can a caller — or a silent upstream change — produce a
scientifically foreign training table that still validates? Under the previous contract the answer
was yes for most of them, because a strict type is not an authority.

No model is fitted anywhere in this module.
"""

from __future__ import annotations

import re
from typing import Any

import pytest

from minos_engine.layer2.features.contracts import AUTHORITATIVE_COLUMNS
from minos_engine.models.contract import (
    DEDUP_POLICY,
    FEATURE_COLUMN_COUNT,
    FROZEN_FEATURE_SET_HASH,
    OUTCOME_ADMITTED,
    OUTCOME_EXECUTION_FAILURE,
    OUTCOME_NON_ADMISSION,
    WEIGHTING_POLICY,
    compute_training_contract_hash,
)
from minos_engine.models.dataset import (
    TRAINING_DATASET_SCHEMA,
    BamFeatureBinding,
    CvManifest,
    TrainingDataset,
    TrainingDatasetError,
    TrainingRow,
)
from minos_engine.models.protocol import (
    CALIBRATION_POLICY,
    CANDIDATE_GRID,
    compute_training_protocol_hash,
    training_protocol_content,
)
from minos_engine.models.runtime import (
    PINNED_RUNTIME,
    TrainingRuntimeError,
    compute_training_runtime_hash,
    verify_training_runtime,
)
from minos_engine.models.spec import (
    MODEL_BUNDLE_SCHEMA,
    MODEL_SPEC_SCHEMA,
    PROMOTABLE_FAMILIES,
    REFERENCE_FAMILIES,
    ModelSpecError,
)
from minos_engine.models.spec_factory import (
    REFERENCE_RECIPES,
    build_accepted_l2g_model_specs,
    build_accepted_l2g_reference_specs,
)
from minos_engine.models.training_data_authority import (
    EXECUTION_ENVIRONMENT_HASH,
    PARAMETER_SPACE_HASH,
    SCORING_CONTRACT_HASH,
    TrainingDataAuthorityError,
    _classify,
    _collapse,
)

_H = "a" * 64
_SCHEDULE_HASH = "ffdd31955a24147430156aff003248f8acb51c68514ca95c6fdbe75525328773"


def _frozen_bams() -> dict[str, str]:
    from minos_engine.baseline.schedule import build_train_schedule

    return {m.dataset_id: m.chromosome for m in build_train_schedule().members}


def _hash_for(seed: int) -> str:
    return f"{seed:064x}"


def _features(bams: dict[str, str]) -> tuple[BamFeatureBinding, ...]:
    return tuple(
        BamFeatureBinding(
            dataset_id=b, vector_hash=_hash_for(1000 + i), feature_values_hash=_hash_for(2000 + i)
        )
        for i, b in enumerate(sorted(bams))
    )


def _rows(bams: dict[str, str]) -> tuple[TrainingRow, ...]:
    return tuple(
        TrainingRow(
            dataset_id=b,
            chromosome=bams[b],
            config_hash=_hash_for(i),
            partition="train",
            outcome=OUTCOME_ADMITTED,
            admitted_score=0.7,
            admission_code="ADMITTED",
            source_job_keys=(_hash_for(9000 + i),),
            source_plan_hashes=(_hash_for(8000 + i),),
        )
        for i, b in enumerate(sorted(bams))
    )


def _dataset(**over: Any) -> TrainingDataset:
    bams = _frozen_bams()
    fields: dict[str, Any] = {
        "baseline_qualified_gate_hash": _H,
        "baseline_selected_hash": _H,
        "feature_registry_hash": _H,
        "config_encoding_identity": _H,
        "parameter_space_hash": _H,
        "scoring_contract_hash": _H,
        "execution_environment_hash": _H,
        "training_contract_hash": compute_training_contract_hash(),
        "training_protocol_hash": compute_training_protocol_hash(),
        "train_schedule_hash": _SCHEDULE_HASH,
        "train_plan_hashes": (_hash_for(7001), _hash_for(7002), _hash_for(7003)),
        "feature_set_hash": FROZEN_FEATURE_SET_HASH,
        "feature_matrix_hash": _H,
        "feature_matrix_artifact_sha256": _H,
        "bam_features": _features(bams),
        "feature_names": tuple(AUTHORITATIVE_COLUMNS),
        "config_feature_names": ("cfg.min_base_quality_score",),
        "rows": _rows(bams),
        "cv_manifest": CvManifest(bam_chromosome=dict(bams)),
    }
    fields.update(over)
    return TrainingDataset(**fields)


# ---------------------------------------------------------------------------------------- #
# exact feature authority
# ---------------------------------------------------------------------------------------- #
def test_the_baseline_dataset_is_valid() -> None:
    assert len(_dataset().rows) == 50
    assert TRAINING_DATASET_SCHEMA == "l2g-training-dataset-v3"


def test_129_invented_names_with_the_correct_feature_set_hash_are_refused() -> None:
    """THE hole: the count matched, so any 129 names instantiated a dataset."""
    invented = tuple(f"feat.{i:03d}" for i in range(FEATURE_COLUMN_COUNT))
    with pytest.raises(TrainingDatasetError, match="not the qualified production columns"):
        _dataset(feature_names=invented)


def test_the_qualified_columns_in_the_wrong_order_are_refused() -> None:
    shuffled = (AUTHORITATIVE_COLUMNS[1], AUTHORITATIVE_COLUMNS[0], *AUTHORITATIVE_COLUMNS[2:])
    with pytest.raises(TrainingDatasetError, match="qualified order"):
        _dataset(feature_names=tuple(shuffled))


def test_the_wider_141_registry_set_is_refused() -> None:
    with pytest.raises(TrainingDatasetError, match="not the qualified"):
        _dataset(feature_names=tuple(f"c{i}" for i in range(141)))


# ---------------------------------------------------------------------------------------- #
# exact members and folds
# ---------------------------------------------------------------------------------------- #
def test_synthetic_bams_in_a_valid_ten_per_chromosome_shape_are_refused() -> None:
    """50 ids, 10 per chromosome, perfectly shaped — and none of them the frozen TRAIN BAMs."""
    from minos_engine.baseline.schedule import CHROMOSOMES

    synthetic = {f"minos-{c}-{i:02d}": c for c in CHROMOSOMES for i in range(10)}
    assert len(synthetic) == 50
    manifest = CvManifest(bam_chromosome=synthetic)
    with pytest.raises(TrainingDatasetError):
        _dataset(cv_manifest=manifest)


def test_a_row_whose_chromosome_disagrees_with_the_manifest_is_refused() -> None:
    bams = _frozen_bams()
    rows = list(_rows(bams))
    wrong = "chr22" if rows[0].chromosome != "chr22" else "chr18"
    rows[0] = rows[0].model_copy(update={"chromosome": wrong})
    with pytest.raises(TrainingDatasetError, match="the row claims"):
        _dataset(rows=tuple(rows))


def test_the_manifest_requires_exactly_ten_per_chromosome() -> None:
    bams = _frozen_bams()
    lopsided = dict(bams)
    donor = next(b for b, c in bams.items() if c == "chr22")
    lopsided[donor] = "chr18"  # 11 / 10 / 10 / 10 / 9 -- still fifty BAMs
    assert len(lopsided) == 50
    with pytest.raises(TrainingDatasetError, match="five folds in name only"):
        CvManifest(bam_chromosome=lopsided)


# ---------------------------------------------------------------------------------------- #
# strict hash types
# ---------------------------------------------------------------------------------------- #
@pytest.mark.parametrize("bad", ["A" * 64, "z" * 64, "v7", "CHANGED", "-" * 64])
def test_a_loose_binding_hash_is_refused(bad: str) -> None:
    with pytest.raises((TrainingDatasetError, ValueError)):
        BamFeatureBinding(dataset_id="b", vector_hash=bad, feature_values_hash=_H)


@pytest.mark.parametrize(
    "field",
    ["baseline_qualified_gate_hash", "feature_matrix_hash", "feature_matrix_artifact_sha256"],
)
def test_an_uppercase_authority_hash_is_refused(field: str) -> None:
    with pytest.raises(TrainingDatasetError, match="lowercase 64-hex"):
        _dataset(**{field: "A" * 64})


def test_a_wrong_length_plan_hash_is_refused() -> None:
    with pytest.raises((TrainingDatasetError, ValueError)):
        _dataset(train_plan_hashes=("abc",))


# ---------------------------------------------------------------------------------------- #
# the contract is part of the identity
# ---------------------------------------------------------------------------------------- #
def test_a_foreign_training_contract_hash_is_refused() -> None:
    with pytest.raises(TrainingDatasetError, match="not the frozen l2g-training-contract-v2"):
        _dataset(training_contract_hash=_H)


def test_a_foreign_training_protocol_hash_is_refused() -> None:
    with pytest.raises(TrainingDatasetError, match="not the frozen l2g-model-training-protocol"):
        _dataset(training_protocol_hash=_H)


def test_the_contract_and_protocol_move_the_dataset_identity() -> None:
    content = _dataset().content()
    assert content["training_contract_hash"] == compute_training_contract_hash()
    assert content["training_protocol_hash"] == compute_training_protocol_hash()
    assert content["train_schedule_hash"] == _SCHEDULE_HASH


def test_a_changed_feature_value_moves_the_dataset_identity() -> None:
    baseline = _dataset().identity()
    moved = list(_features(_frozen_bams()))
    moved[3] = BamFeatureBinding(
        dataset_id=moved[3].dataset_id,
        vector_hash=moved[3].vector_hash,
        feature_values_hash=_hash_for(999999),
    )
    assert _dataset(bam_features=tuple(moved)).identity() != baseline


# ---------------------------------------------------------------------------------------- #
# outcome derivation — the caller nominates nothing
# ---------------------------------------------------------------------------------------- #
def _cell(**over: Any) -> dict[str, Any]:
    cell = {
        "job_key": _hash_for(1),
        "plan_hash": _hash_for(2),
        "phase": "PHASE_A",
        "dataset_id": "b1",
        "config_hash": _hash_for(3),
        "parameter_space_hash": PARAMETER_SPACE_HASH,
        "execution_environment_hash": EXECUTION_ENVIRONMENT_HASH,
        "execution_failure_code": None,
        "has_execution_result": True,
        "scoring_contract_hash": SCORING_CONTRACT_HASH,
        "admitted": True,
        "admission_code": "ADMITTED",
        "minos_score": 0.7,
        "has_evaluation_failure": False,
    }
    cell.update(over)
    return cell


def test_an_admitted_cell_yields_its_persisted_score() -> None:
    assert _classify(_cell()) == {
        "outcome": OUTCOME_ADMITTED,
        "admitted_score": 0.7,
        "admission_code": "ADMITTED",
        "execution_failure_code": None,
    }


def test_a_non_admission_never_carries_its_score_forward() -> None:
    """The persisted minos_score exists; the frozen objective refuses to consume it."""
    out = _classify(
        _cell(admitted=False, admission_code="ZERO_INPUT_FINGERPRINT", minos_score=0.42)
    )
    assert out["outcome"] == OUTCOME_NON_ADMISSION
    assert out["admitted_score"] is None
    assert out["admission_code"] == "ZERO_INPUT_FINGERPRINT"


def test_a_bounded_execution_failure_is_a_candidate_failure() -> None:
    out = _classify(
        _cell(
            has_execution_result=False,
            execution_failure_code="GATK_NONZERO_EXIT",
            scoring_contract_hash=None,
            admitted=None,
            admission_code=None,
            minos_score=None,
        )
    )
    assert out["outcome"] == OUTCOME_EXECUTION_FAILURE
    assert out["admitted_score"] is None


def test_an_evaluation_failure_refuses_the_freeze() -> None:
    with pytest.raises(TrainingDataAuthorityError, match="infrastructure incident"):
        _classify(_cell(has_evaluation_failure=True))


def test_an_unbounded_execution_failure_refuses_the_freeze() -> None:
    with pytest.raises(TrainingDataAuthorityError, match="not a bounded candidate failure"):
        _classify(
            _cell(
                has_execution_result=False,
                execution_failure_code="ARTIFACT_PUBLISH_FAILED",
                scoring_contract_hash=None,
                admitted=None,
                admission_code=None,
                minos_score=None,
            )
        )


def test_a_foreign_scoring_contract_refuses_the_freeze() -> None:
    with pytest.raises(TrainingDataAuthorityError, match="not the\n?\\s*frozen scoring contract"):
        _classify(_cell(scoring_contract_hash=_H))


def test_an_admitted_cell_without_a_score_refuses() -> None:
    with pytest.raises(TrainingDataAuthorityError, match="no persisted score"):
        _classify(_cell(minos_score=None))


# ---------------------------------------------------------------------------------------- #
# dedup is DERIVED, and conflicts stop the freeze
# ---------------------------------------------------------------------------------------- #
def _pair(**over: Any) -> dict[str, Any]:
    return _cell(dataset_id="bam-a", config_hash=_hash_for(11), **over)


def test_identical_repeats_collapse_to_one_example_with_sorted_provenance() -> None:
    cells = [
        _pair(job_key=_hash_for(20), plan_hash=_hash_for(31)),
        _pair(job_key=_hash_for(21), plan_hash=_hash_for(30)),
    ]
    rows = _collapse(cells, chromosome_of={"bam-a": "chr18"})
    assert len(rows) == 1
    assert rows[0].source_job_keys == (_hash_for(20), _hash_for(21))
    assert rows[0].source_plan_hashes == (_hash_for(30), _hash_for(31))


def test_a_repeat_conflicting_on_admitted_score_refuses() -> None:
    with pytest.raises(TrainingDataAuthorityError, match="disagrees on the admitted score"):
        _collapse(
            [_pair(job_key=_hash_for(20)), _pair(job_key=_hash_for(21), minos_score=0.55)],
            chromosome_of={"bam-a": "chr18"},
        )


def test_a_repeat_conflicting_on_outcome_refuses() -> None:
    with pytest.raises(TrainingDataAuthorityError, match="disagrees on outcome"):
        _collapse(
            [
                _pair(job_key=_hash_for(20)),
                _pair(
                    job_key=_hash_for(21), admitted=False, admission_code="ZERO_INPUT_FINGERPRINT"
                ),
            ],
            chromosome_of={"bam-a": "chr18"},
        )


def test_a_repeat_conflicting_on_execution_environment_refuses() -> None:
    with pytest.raises(TrainingDataAuthorityError, match="two execution\n?\\s*environments"):
        _collapse(
            [
                _pair(job_key=_hash_for(20)),
                _pair(job_key=_hash_for(21), execution_environment_hash=_H),
            ],
            chromosome_of={"bam-a": "chr18"},
        )


def test_there_is_no_newest_row_rule_and_no_averaging() -> None:
    import inspect

    from minos_engine.models import training_data_authority as mod

    source = inspect.getsource(mod._collapse)
    assert "mean" not in source and "sum(" not in source
    assert "created_at" not in source and "ORDER BY" not in source


# ---------------------------------------------------------------------------------------- #
# weighting
# ---------------------------------------------------------------------------------------- #
def test_each_bam_carries_equal_total_admission_weight() -> None:
    bams = _frozen_bams()
    first = sorted(bams)[0]
    rows = list(_rows(bams))
    rows += [
        TrainingRow(
            dataset_id=first,
            chromosome=bams[first],
            config_hash=_hash_for(500 + k),
            partition="train",
            outcome=OUTCOME_ADMITTED,
            admitted_score=0.6,
            admission_code="ADMITTED",
            source_job_keys=(_hash_for(600 + k),),
            source_plan_hashes=(_hash_for(700 + k),),
        )
        for k in range(4)
    ]
    dataset = _dataset(rows=tuple(rows))
    weights = dataset.admission_weights()
    totals: dict[str, float] = {}
    for row in dataset.rows:
        totals[row.dataset_id] = totals.get(row.dataset_id, 0.0) + weights[row.identity()]
    assert all(abs(t - 1.0) < 1e-9 for t in totals.values())


# ---------------------------------------------------------------------------------------- #
# the accepted spec factory
# ---------------------------------------------------------------------------------------- #
def test_the_factory_produces_the_finite_candidate_set() -> None:
    specs = build_accepted_l2g_model_specs(_dataset())
    assert len(specs) == len(CANDIDATE_GRID)
    assert all(s.family in PROMOTABLE_FAMILIES for s in specs)
    assert len({s.identity() for s in specs}) == len(specs)


def test_the_factory_cannot_be_asked_for_per_row_weighting() -> None:
    """ModelSpec ACCEPTS PER_ROW; the accepted factory never emits it."""
    for spec in build_accepted_l2g_model_specs(_dataset()):
        assert spec.weighting_policy == WEIGHTING_POLICY
        assert spec.dedup_policy == DEDUP_POLICY
    import inspect

    from minos_engine.models import spec_factory

    source = inspect.getsource(spec_factory)
    # the module docstring names PER_ROW as the thing it refuses; check the CODE
    code = source.split(chr(34) * 3, 2)[-1]
    assert "PER_ROW" not in code
    assert re.search(r'"weighting_policy":\s*WEIGHTING_POLICY', code)


def test_every_candidate_spec_binds_the_real_dataset_and_manifest() -> None:
    dataset = _dataset()
    for spec in build_accepted_l2g_model_specs(dataset):
        assert spec.training_dataset_hash == dataset.identity()
        assert spec.cv_manifest_hash == dataset.cv_manifest.identity()
        assert spec.target_formulation == "B_JOINT_EXPECTED_UTILITY_OVER_ADMISSION"


def test_the_factory_refuses_a_dataset_from_a_foreign_protocol() -> None:
    from minos_engine.models.spec_factory import ModelSpecFactoryError

    dataset = _dataset()
    foreign = dataset.model_copy(update={"training_protocol_hash": _H})
    with pytest.raises(ModelSpecFactoryError, match="frozen training protocol"):
        build_accepted_l2g_model_specs(foreign)


def test_the_reference_specs_are_frozen_and_hashed() -> None:
    refs = build_accepted_l2g_reference_specs(_dataset())
    assert len(refs) == len(REFERENCE_RECIPES) == 4
    assert {r.family for r in refs} == set(REFERENCE_FAMILIES)
    assert len({r.identity() for r in refs}) == 4
    for recipe in REFERENCE_RECIPES:
        assert recipe["tie_break"], "a reference with no tie-break is not reproducible"
        assert recipe["score_fit_data"]
        assert recipe["score_implementation"] and recipe["admission_implementation"]


def test_a_reference_is_never_produced_as_a_candidate() -> None:
    candidates = build_accepted_l2g_model_specs(_dataset())
    assert not ({c.family for c in candidates} & set(REFERENCE_FAMILIES))


def test_the_spec_is_v3_and_the_bundle_v2() -> None:
    """The spec moved again when the admission heads were made truthful; nothing was fitted."""
    assert MODEL_SPEC_SCHEMA == "l2g-model-spec-v3"
    assert MODEL_BUNDLE_SCHEMA == "l2g-model-bundle-v2"
    from minos_engine.models.spec import (
        SUPERSEDED_BUNDLE_V1,
        SUPERSEDED_SPEC_V1,
        SUPERSEDED_SPEC_V2,
    )

    assert SUPERSEDED_SPEC_V1 == SUPERSEDED_BUNDLE_V1 == "SUPERSEDED_BEFORE_FIRST_MODEL_FIT"
    assert SUPERSEDED_SPEC_V2 == "SUPERSEDED_BEFORE_FIRST_MODEL_FIT"


@pytest.mark.parametrize("bad", ["A" * 64, "nope"])
def test_a_malformed_spec_binding_hash_is_refused(bad: str) -> None:
    from minos_engine.models.spec import ModelSpec

    with pytest.raises((ModelSpecError, ValueError)):
        ModelSpec(
            family="LINEAR_REGULARIZED",
            implementation="x",
            target_formulation="t",
            feature_schema_hash=bad,
            config_schema_hash=_H,
            transform_specification={},
            hyperparameters={},
            random_seed=1,
            loss="squared_error",
            weighting_policy=WEIGHTING_POLICY,
            dedup_policy=DEDUP_POLICY,
            failure_risk_formulation="f",
            calibration_method="c",
            ood_method="o",
            training_dataset_hash=_H,
            cv_manifest_hash=_H,
        )


# ---------------------------------------------------------------------------------------- #
# runtime and calibration
# ---------------------------------------------------------------------------------------- #
def test_the_runtime_is_exact_not_a_range() -> None:
    assert PINNED_RUNTIME["scikit-learn"] == "1.9.0"
    verified = verify_training_runtime()
    assert verified["observed"]["scikit-learn"] == "1.9.0"
    assert verified["runtime_hash"] == compute_training_runtime_hash()
    from pathlib import Path

    pyproject = (Path(__file__).resolve().parents[3] / "pyproject.toml").read_text()
    assert "scikit-learn==1.9.0" in pyproject
    assert "scikit-learn>=1.5,<2" not in pyproject


def test_a_runtime_mismatch_refuses_to_fit(monkeypatch: Any) -> None:
    from minos_engine.models import runtime as mod

    monkeypatch.setattr(
        mod, "observe_training_runtime", lambda: {**PINNED_RUNTIME, "scikit-learn": "1.5.0"}
    )
    with pytest.raises(TrainingRuntimeError, match="not the frozen"):
        mod.verify_training_runtime()


def test_calibration_is_nested_and_cannot_fit_on_the_labels_it_is_scored_against() -> None:
    assert CALIBRATION_POLICY["scheme"] == "NESTED_CROSS_FITTED_WITHIN_EACH_OUTER_FOLD"
    assert "INNER" in CALIBRATION_POLICY["fitted_on"]
    assert CALIBRATION_POLICY["applied_to"] == "THE_HELD_OUT_CHROMOSOME"
    assert "FORBIDDEN" not in CALIBRATION_POLICY["scheme"]
    assert CALIBRATION_POLICY["forbidden"].startswith("FITTING_CALIBRATION_ON_THE_OUTER_OOF")
    assert training_protocol_content()["calibration"] == CALIBRATION_POLICY


def test_the_calibration_fold_partition_never_shares_a_bam() -> None:
    """The structural property the policy relies on: the inner fitting set excludes the fold."""
    manifest = CvManifest(bam_chromosome=_frozen_bams())
    for train_bams, held in manifest.folds():
        assert len(train_bams) == 40 and len(held) == 10
        assert not (train_bams & held)
        # the isotonic mapping is fitted on inner pairs drawn from `train_bams` only, so no
        # label from `held` can reach the mapping that is later applied to `held`
        inner_pool = {b: manifest.bam_chromosome[b] for b in train_bams}
        assert set(inner_pool).isdisjoint(held)


# ---------------------------------------------------------------------------------------- #
# stage locks
# ---------------------------------------------------------------------------------------- #
def test_no_real_campaign_was_executed_in_this_task() -> None:
    """The trainer source now legitimately fits; what must not exist is a REAL result."""

    from tests.minos_scratch import CANONICAL_MINOS_ROOT

    workspace = CANONICAL_MINOS_ROOT / "minos_l2g_training"
    for forbidden in (
        "oof_predictions.json",
        "train_oof_campaign_result.json",
        "metrics.json",
        "model_bundle.joblib",
    ):
        assert not (workspace / forbidden).exists(), f"a real campaign artifact exists: {forbidden}"
    authority = _authority()
    assert authority["no_model_fitted"] is True
    assert authority["train_oof_campaign_executed"] is False


def test_no_models_qualified_gate_is_issued() -> None:
    from pathlib import Path

    root = Path(__file__).resolve().parents[3]
    assert not (root / "gates/models-qualified.json").exists()


def test_select_config_remains_blocked() -> None:
    from minos_engine.layer2.service import Layer2Service

    with pytest.raises(Exception) as excinfo:
        Layer2Service().select_config(None)  # type: ignore[arg-type]
    assert "StageNotReady" in type(excinfo.value).__name__


def test_no_validation_or_test_source_is_reachable_from_the_builder() -> None:
    import inspect

    from minos_engine.models import training_data_authority as mod

    source = inspect.getsource(mod)
    assert "validation" not in source.lower().replace("validation_finalists", "")
    assert "l2f2_validation" not in source
    assert "'test'" not in source and '"test"' not in source


# ---------------------------------------------------------------------------------------- #
# the frozen pre-fit authority artifact
# ---------------------------------------------------------------------------------------- #
def _authority() -> dict[str, Any]:
    import json
    from pathlib import Path

    root = Path(__file__).resolve().parents[3]
    return dict(
        json.loads((root / "reports/layer2/l2g-prefit-authority.json").read_text(encoding="utf-8"))
    )


def test_the_prefit_authority_records_the_real_frozen_dataset() -> None:
    """The REAL freeze, recorded before any fit. Counts re-derived from sealed TRAIN evidence."""
    a = _authority()
    assert a["training_dataset_schema"] == "l2g-training-dataset-v3"
    assert re.fullmatch(r"[0-9a-f]{64}", a["training_dataset_hash"])
    counts = a["counts"]
    assert counts["scientific_cells"] == 1040
    assert counts["admitted"] == 861
    assert counts["non_admission"] == 149
    assert counts["execution_failure"] == 30
    assert counts["unique_configs"] == 80
    assert counts["bams"] == 50
    assert counts["per_chromosome"] == dict.fromkeys(
        ("chr18", "chr19", "chr20", "chr21", "chr22"), 10
    )
    assert a["bams_without_admitted_examples"] == 0


def test_the_prefit_authority_binds_the_frozen_upstream_identities() -> None:
    a = _authority()["authorities"]
    assert a["training_contract_hash"] == compute_training_contract_hash()
    assert a["training_protocol_hash"] == compute_training_protocol_hash()
    assert a["training_runtime_hash"] == compute_training_runtime_hash()
    assert a["feature_set_hash"] == FROZEN_FEATURE_SET_HASH
    assert a["scoring_contract_hash"] == SCORING_CONTRACT_HASH
    assert a["execution_environment_hash"] == EXECUTION_ENVIRONMENT_HASH
    assert a["parameter_space_hash"] == PARAMETER_SPACE_HASH
    assert len(a["train_plan_hashes"]) == 3
    for value in a.values():
        for item in value if isinstance(value, list) else [value]:
            assert re.fullmatch(r"[0-9a-f]{64}", str(item)), item


def test_the_six_candidate_and_four_reference_spec_hashes_exist_before_any_fit() -> None:
    a = _authority()
    assert len(a["candidate_spec_hashes"]) == 6
    assert len(a["reference_spec_hashes"]) == 4
    families = {c["family"] for c in a["candidate_spec_hashes"]}
    assert families <= set(PROMOTABLE_FAMILIES)
    assert {r["family"] for r in a["reference_spec_hashes"]} == set(REFERENCE_FAMILIES)
    hashes = [s["spec_hash"] for s in a["candidate_spec_hashes"] + a["reference_spec_hashes"]]
    assert len(set(hashes)) == 10
    assert all(re.fullmatch(r"[0-9a-f]{64}", h) for h in hashes)
    assert a["no_model_fitted"] is True
    assert a["models_qualified_issued"] is False
    assert a["validation_consulted"] is False
    assert a["test_sealed_until"] == "L2-I"


def test_the_committed_authority_carries_no_learning_data() -> None:
    """Hashes and counts only: no score, no BAM identity, no feature value."""
    import json
    from pathlib import Path

    root = Path(__file__).resolve().parents[3]
    raw = (root / "reports/layer2/l2g-prefit-authority.json").read_text(encoding="utf-8")
    assert "minos_score" not in raw
    assert "admitted_score" not in raw
    assert "minos-chr" not in raw, "a BAM identity leaked into the committed authority"
    assert "truth" not in raw.lower()
    payload = json.loads(raw)
    assert "examples" not in payload and "rows" not in payload
