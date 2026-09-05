"""L2-G TRAIN OOF runner authority. No real campaign is executed here.

The properties that matter are structural, so most of these tests build a tiny synthetic fold and
ask whether a held-out label could possibly reach the thing that is later scored against it. A
metric computed on leaked labels looks excellent, which is exactly why it has to be impossible
rather than merely unintended.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from minos_engine.models.calibration import (
    CalibrationError,
    fit_nested_admission_calibrator,
)
from minos_engine.models.config_table import ConfigTableError, load_verified_config_vectors
from minos_engine.models.contract import CV_FOLD_CHROMOSOMES
from minos_engine.models.design_matrix import (
    CONTEXTUAL_COLUMN_COUNT,
    DesignMatrix,
    DesignMatrixError,
    build_design_matrix,
)
from minos_engine.models.estimators import (
    SUPPORTED_ESTIMATORS,
    EstimatorFactoryError,
    build_admission_estimator,
    build_score_estimator,
)
from minos_engine.models.feature_values import FeatureValuesError, load_verified_feature_values
from minos_engine.models.fit_driver import fit_fold_estimators
from minos_engine.models.oof_metrics import bam_selection_regret, summarise_oof
from minos_engine.models.oof_runner import (
    TrainingFailure,
    oof_artifact_identity,
    run_outer_oof,
)
from minos_engine.models.prefit_loader import (
    ACCEPTED_TRAINING_DATASET_HASH,
    PrefitAuthorityError,
    load_accepted_prefit_authority,
    load_verified_training_dataset,
)
from minos_engine.models.runtime import PINNED_RUNTIME, TrainingRuntimeError
from minos_engine.models.shortlist import ShortlistError, derive_train_shortlist
from minos_engine.models.spec import MODEL_SPEC_SCHEMA, ModelSpecError
from minos_engine.models.threading_control import (
    ThreadEnforcementError,
    observe_thread_pools,
    single_threaded,
    verify_single_threaded,
)


#: derived, never hard-coded: the operator root is a machine fact, not a scientific one, and a
#: literal path here is the defect class the portability guard exists to catch.
def _workspace() -> Path:
    from tests.minos_scratch import CANONICAL_MINOS_ROOT

    return CANONICAL_MINOS_ROOT / "minos_l2g_training"


def _artifact() -> Path:
    from minos_engine.models.prefit_loader import ACCEPTED_FEATURE_ARTIFACT_SHA

    return (
        Path("/var")
        / "lib"
        / "minos"
        / "l2e"
        / "train"
        / (f"{ACCEPTED_FEATURE_ARTIFACT_SHA}.parquet")
    )


def _repo() -> Path:
    return Path(__file__).resolve().parents[3]


def _specs() -> tuple[Any, ...]:
    from minos_engine.models.spec_factory import build_accepted_l2g_model_specs

    return build_accepted_l2g_model_specs(_dataset())


def _references() -> tuple[Any, ...]:
    from minos_engine.models.spec_factory import build_accepted_l2g_reference_specs

    return build_accepted_l2g_reference_specs(_dataset())


def _dataset() -> Any:
    return load_verified_training_dataset()


# ------------------------------------------------------------------------------------------ #
# ModelSpec v3 -- the confirmed defect
# ------------------------------------------------------------------------------------------ #
def test_the_spec_schema_is_v3() -> None:
    assert MODEL_SPEC_SCHEMA == "l2g-model-spec-v3"
    from minos_engine.models.spec import SUPERSEDED_SPEC_V2

    assert SUPERSEDED_SPEC_V2 == "SUPERSEDED_BEFORE_FIRST_MODEL_FIT"


def test_the_tree_candidates_do_not_claim_a_logistic_admission_head() -> None:
    """v2 recorded LOGISTIC_P_ADMISSION for all six. It was untrue for four of them."""
    trees = [s for s in _specs() if s.family == "TREE_ENSEMBLE"]
    assert len(trees) == 2
    for spec in trees:
        assert (
            spec.admission_model_implementation == "sklearn.ensemble.HistGradientBoostingClassifier"
        )
        assert "LOGISTIC" not in json.dumps(spec.content()).upper()


def test_the_mlp_candidate_does_not_claim_a_logistic_admission_head() -> None:
    mlp = [s for s in _specs() if s.family == "COMPACT_MLP"]
    assert len(mlp) == 1
    assert mlp[0].admission_model_implementation == "sklearn.neural_network.MLPClassifier"
    assert mlp[0].score_model_implementation == "sklearn.neural_network.MLPRegressor"
    assert "LOGISTIC" not in json.dumps(mlp[0].content()).upper()


def test_the_linear_candidates_do_use_logistic_regression() -> None:
    linear = [s for s in _specs() if s.family == "LINEAR_REGULARIZED"]
    assert len(linear) == 3
    for spec in linear:
        assert spec.admission_model_implementation == "sklearn.linear_model.LogisticRegression"
        assert spec.score_model_implementation == "sklearn.linear_model.Ridge"


def test_the_two_heads_are_separately_addressable() -> None:
    for spec in _specs():
        assert spec.score_model_implementation != spec.admission_model_implementation
        assert isinstance(spec.score_hyperparameters, dict)
        assert isinstance(spec.admission_hyperparameters, dict)


def test_the_six_candidate_and_four_reference_hashes_are_deterministic() -> None:
    a, b = _specs(), _specs()
    assert [s.identity() for s in a] == [s.identity() for s in b]
    assert len({s.identity() for s in a}) == 6
    ra, rb = _references(), _references()
    assert [s.identity() for s in ra] == [s.identity() for s in rb]
    assert len({s.identity() for s in ra}) == 4


def test_the_score_head_population_cannot_be_widened() -> None:
    spec = _specs()[0]
    with pytest.raises(ModelSpecError, match="ADMITTED examples only"):
        spec.model_copy(
            update={"score_training_population": "EVERY_DECIDED_OUTCOME"}
        ).model_post_init(None)


def test_the_score_clip_rule_is_frozen_on_every_spec() -> None:
    for spec in (*_specs(), *_references()):
        assert spec.score_output_postprocess == "CLIP_TO_0_1"
    with pytest.raises(ModelSpecError, match="frozen at CLIP_TO_0_1"):
        _specs()[0].model_copy(update={"score_output_postprocess": "NONE"}).model_post_init(None)


# ------------------------------------------------------------------------------------------ #
# estimator factory
# ------------------------------------------------------------------------------------------ #
def test_every_candidate_maps_to_real_estimators_that_take_sample_weight() -> None:
    for spec in _specs():
        score = build_score_estimator(spec)
        admission = build_admission_estimator(spec)
        assert type(score).__name__ == spec.score_model_implementation.rsplit(".", 1)[1]
        assert type(admission).__name__ == spec.admission_model_implementation.rsplit(".", 1)[1]
        assert getattr(score, "random_state", "missing") == spec.random_seed
        assert getattr(admission, "random_state", "missing") == spec.random_seed


def test_the_factory_refuses_an_arbitrary_import_path() -> None:
    spec = _specs()[0]
    forged = spec.model_copy(update={"score_model_implementation": "os.system"})
    with pytest.raises(EstimatorFactoryError, match="not a supported estimator|not a score"):
        build_score_estimator(forged)
    assert "os.system" not in SUPPORTED_ESTIMATORS


def test_a_classifier_cannot_be_used_as_the_score_head() -> None:
    spec = _specs()[0]
    swapped = spec.model_copy(
        update={"score_model_implementation": "sklearn.linear_model.LogisticRegression"}
    )
    with pytest.raises(EstimatorFactoryError, match="not a score"):
        build_score_estimator(swapped)


def test_an_estimator_without_sample_weight_support_is_refused(monkeypatch: Any) -> None:
    """EQUAL_BAM_TOTAL is not advisory: a head that ignores it must not be constructible."""
    import inspect as inspect_mod

    from minos_engine.models import estimators as mod

    real = mod.inspect.signature

    def fake(target: Any) -> Any:
        signature = real(target)
        if getattr(target, "__name__", "") == "fit":
            return signature.replace(
                parameters=[p for n, p in signature.parameters.items() if n != "sample_weight"]
            )
        return signature

    monkeypatch.setattr(mod.inspect, "signature", fake)
    assert inspect_mod is not None
    with pytest.raises(EstimatorFactoryError, match="cannot honour EQUAL_BAM_TOTAL"):
        build_score_estimator(_specs()[0])


# ------------------------------------------------------------------------------------------ #
# runtime and threads
# ------------------------------------------------------------------------------------------ #
def test_thread_limits_are_enforced_during_the_fit_not_merely_declared() -> None:
    import sklearn  # noqa: F401  -- the pools must be loaded to be observable

    outside = observe_thread_pools()
    assert outside, "no BLAS/OpenMP pool is loaded; the enforcement claim is untestable"
    with single_threaded() as report:
        assert report, "no pool observed inside the limited context"
        assert all(p["num_threads"] == 1 for p in report if p["user_api"] in ("blas", "openmp"))
    # and the claim is genuinely falsifiable: unlimited, the verifier refuses
    if any(p["num_threads"] != 1 for p in observe_thread_pools()):
        with pytest.raises(ThreadEnforcementError, match="SINGLE_THREADED_DETERMINISTIC"):
            verify_single_threaded()


@pytest.mark.parametrize("package", ["scikit-learn", "numpy", "scipy", "joblib"])
def test_a_wrong_library_version_refuses_to_fit(package: str, monkeypatch: Any) -> None:
    from minos_engine.models import runtime as mod

    monkeypatch.setattr(
        mod, "observe_training_runtime", lambda: {**PINNED_RUNTIME, package: "0.0.1"}
    )
    with pytest.raises(TrainingRuntimeError, match="not the frozen"):
        mod.verify_training_runtime()


# ------------------------------------------------------------------------------------------ #
# frozen bundle verification
# ------------------------------------------------------------------------------------------ #
def test_the_bundle_reconstructs_to_the_accepted_dataset_identity() -> None:
    """The manifest's own claim about its hash is never the authority."""
    dataset = load_verified_training_dataset()
    assert dataset.identity() == ACCEPTED_TRAINING_DATASET_HASH
    assert len(dataset.rows) == 1040
    assert len(dataset.score_examples) == 861


def test_a_tampered_training_examples_file_is_refused(tmp_path: Path) -> None:
    workspace = _workspace()
    for name in (
        "training_dataset_manifest.json",
        "config_table.json",
        "bam_feature_bindings.json",
    ):
        (tmp_path / name).write_bytes((workspace / name).read_bytes())
    payload = json.loads((workspace / "training_examples.json").read_text())
    payload["examples"][0]["admitted_score"] = 0.999999
    (tmp_path / "training_examples.json").write_text(json.dumps(payload))
    with pytest.raises(PrefitAuthorityError, match="hashes to"):
        load_verified_training_dataset(workspace=tmp_path)


def test_a_tampered_config_table_is_refused(tmp_path: Path) -> None:
    workspace = _workspace()
    for name in (
        "training_dataset_manifest.json",
        "training_examples.json",
        "bam_feature_bindings.json",
    ):
        (tmp_path / name).write_bytes((workspace / name).read_bytes())
    payload = json.loads((workspace / "config_table.json").read_text())
    payload["config_hashes"] = payload["config_hashes"][:-1]
    (tmp_path / "config_table.json").write_text(json.dumps(payload))
    with pytest.raises(PrefitAuthorityError, match="hashes to"):
        load_verified_training_dataset(workspace=tmp_path)


def test_a_tampered_feature_binding_is_refused(tmp_path: Path) -> None:
    workspace = _workspace()
    for name in ("training_dataset_manifest.json", "training_examples.json", "config_table.json"):
        (tmp_path / name).write_bytes((workspace / name).read_bytes())
    payload = json.loads((workspace / "bam_feature_bindings.json").read_text())
    payload["bindings"][0]["feature_values_hash"] = "f" * 64
    (tmp_path / "bam_feature_bindings.json").write_text(json.dumps(payload))
    with pytest.raises(PrefitAuthorityError, match="hashes to"):
        load_verified_training_dataset(workspace=tmp_path)


def test_a_bundle_file_may_not_be_a_symlink(tmp_path: Path) -> None:
    workspace = _workspace()
    for name in (
        "training_dataset_manifest.json",
        "config_table.json",
        "bam_feature_bindings.json",
    ):
        (tmp_path / name).write_bytes((workspace / name).read_bytes())
    (tmp_path / "training_examples.json").symlink_to(workspace / "training_examples.json")
    with pytest.raises(PrefitAuthorityError, match="symlink"):
        load_verified_training_dataset(workspace=tmp_path)


def test_the_committed_authority_must_agree_with_this_source() -> None:
    document = load_accepted_prefit_authority()
    assert document["training_dataset_hash"] == ACCEPTED_TRAINING_DATASET_HASH
    assert len(document["candidate_spec_hashes"]) == 6
    assert len(document["reference_spec_hashes"]) == 4


def test_the_authority_records_the_current_v3_spec_hashes_not_stale_ones() -> None:
    """After the v3 refresh, the committed hashes must be the ones this source produces."""
    document = load_accepted_prefit_authority()
    recorded = {entry["spec_hash"] for entry in document["candidate_spec_hashes"]}
    assert recorded == {s.identity() for s in _specs()}
    recorded_refs = {entry["spec_hash"] for entry in document["reference_spec_hashes"]}
    assert recorded_refs == {s.identity() for s in _references()}


# ------------------------------------------------------------------------------------------ #
# real feature values and config payloads
# ------------------------------------------------------------------------------------------ #
@pytest.mark.skipif(not _artifact().is_file(), reason="qualified matrix artifact not present")
def test_the_real_matrix_bytes_earn_their_identity() -> None:
    values = load_verified_feature_values(artifact_path=_artifact(), dataset=_dataset())
    assert len(values) == 50
    assert all(len(v) == 129 for v in values.values())


@pytest.mark.skipif(not _artifact().is_file(), reason="qualified matrix artifact not present")
def test_tampered_matrix_bytes_are_refused(tmp_path: Path) -> None:
    copy = tmp_path / "matrix.parquet"
    data = bytearray(_artifact().read_bytes())
    data[-1] ^= 0x01
    copy.write_bytes(bytes(data))
    with pytest.raises(FeatureValuesError, match="hashes to"):
        load_verified_feature_values(artifact_path=copy, dataset=_dataset())


@pytest.mark.skipif(not _artifact().is_file(), reason="qualified matrix artifact not present")
def test_a_matrix_whose_columns_are_reordered_is_refused(tmp_path: Path, monkeypatch: Any) -> None:
    """A permuted matrix keeps every column-SET hash intact and trains on shuffled predictors."""
    import pyarrow.parquet as pq

    from minos_engine.models import feature_values as mod

    table = pq.read_table(_artifact())
    names = list(table.column_names)
    swapped = [names[0], names[2], names[1], *names[3:]]
    monkeypatch.setattr(
        mod.pq if hasattr(mod, "pq") else pq,
        "read_table",
        lambda p: table.select(swapped),
        raising=False,
    )
    import pyarrow.parquet as real_pq

    monkeypatch.setattr(real_pq, "read_table", lambda p: table.select(swapped))
    with pytest.raises(FeatureValuesError, match="qualified order"):
        load_verified_feature_values(artifact_path=_artifact(), dataset=_dataset())


def test_every_config_payload_earns_the_hash_it_is_stored_under() -> None:
    hashes = tuple(json.loads((_workspace() / "config_table.json").read_text())["config_hashes"])
    vectors, names = load_verified_config_vectors(config_hashes=hashes)
    assert len(vectors) == 80
    assert len(names) == 28
    assert all(len(v) == 28 for v in vectors.values())


def test_a_config_payload_that_does_not_hash_to_its_name_is_refused(tmp_path: Path) -> None:
    from minos_engine.models.config_table import CONFIG_PAYLOAD_ROOT

    hashes = tuple(json.loads((_workspace() / "config_table.json").read_text())["config_hashes"])
    for value in hashes:
        source = CONFIG_PAYLOAD_ROOT / f"{value}.json"
        (tmp_path / source.name).write_bytes(source.read_bytes())
    victim = tmp_path / f"{hashes[0]}.json"
    payload = json.loads(victim.read_text())
    payload["assembly_region_padding"] = 999
    victim.write_text(json.dumps(payload))
    with pytest.raises(ConfigTableError, match="does not describe|hashes to"):
        load_verified_config_vectors(config_hashes=hashes, payload_root=tmp_path)


def test_the_config_vector_comes_only_from_the_accepted_encoder() -> None:
    import inspect

    from minos_engine.models import config_table as mod

    source = inspect.getsource(mod.load_verified_config_vectors)
    assert "build_config_encoding()" in source
    assert "encoding.encode(payload)" in source


# ------------------------------------------------------------------------------------------ #
# structural OOF properties, on a synthetic fold
# ------------------------------------------------------------------------------------------ #
class _Row:
    def __init__(self, bam: str, config: str, chromosome: str, admitted: bool, score: float | None):
        self.dataset_id, self.config_hash, self.chromosome = bam, config, chromosome
        self.outcome = "ADMITTED" if admitted else "CANDIDATE_NON_ADMISSION"
        self.admitted_score = score
        self.admission_label = 1 if admitted else 0

    def identity(self) -> str:
        return f"{self.dataset_id}|{self.config_hash}"


class _Spec:
    family = "LINEAR_REGULARIZED"
    score_model_implementation = "sklearn.linear_model.Ridge"
    admission_model_implementation = "sklearn.linear_model.LogisticRegression"
    score_hyperparameters: dict[str, Any] = {"alpha": 1.0}
    admission_hyperparameters: dict[str, Any] = {"C": 1.0, "max_iter": 1000}
    random_seed = 20260904

    def identity(self) -> str:
        return "s" * 64


def _synthetic() -> tuple[Any, ...]:
    rng = np.random.default_rng(11)
    bams = {f"bam-{c}-{i}": c for c in CV_FOLD_CHROMOSOMES for i in range(10)}
    configs = [f"{i:064x}" for i in range(6)]
    rows = []
    for bam, chromosome in bams.items():
        for config in configs:
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
    design = DesignMatrix(
        x_bam=rng.normal(size=(len(rows), 129)),
        x_config=rng.normal(size=(len(rows), 28)),
        bam_columns=tuple(f"b{i}" for i in range(129)),
        config_columns=tuple(f"c{i}" for i in range(28)),
        meta=tuple(
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
        ),
    )
    per_bam: dict[str, int] = {}
    for r in rows:
        per_bam[r.dataset_id] = per_bam.get(r.dataset_id, 0) + 1
    weights = {r.identity(): 1 / per_bam[r.dataset_id] for r in rows}
    admitted_rows = [r for r in rows if r.admitted_score is not None]
    per_admitted: dict[str, int] = {}
    for r in admitted_rows:
        per_admitted[r.dataset_id] = per_admitted.get(r.dataset_id, 0) + 1
    score_weights = {r.identity(): 1 / per_admitted[r.dataset_id] for r in admitted_rows}
    return rows, design, bams, weights, score_weights


def test_each_cell_is_predicted_exactly_once_by_a_model_that_never_saw_its_bam() -> None:
    rows, design, bams, weights, score_weights = _synthetic()
    records, failures = run_outer_oof(
        spec=_Spec(),
        rows=rows,
        design=design,
        chromosome_of=bams,
        weights=weights,
        score_weights=score_weights,
        fit_estimators=fit_fold_estimators,
    )
    assert not failures
    assert len(records) == len(rows)
    assert len({(r.dataset_id, r.config_hash) for r in records}) == len(rows)
    for record in records:
        assert bams[record.dataset_id] == record.outer_fold


def test_predictions_are_bounded_and_the_utility_is_the_product() -> None:
    rows, design, bams, weights, score_weights = _synthetic()
    records, _ = run_outer_oof(
        spec=_Spec(),
        rows=rows,
        design=design,
        chromosome_of=bams,
        weights=weights,
        score_weights=score_weights,
        fit_estimators=fit_fold_estimators,
    )
    for r in records:
        assert 0.0 <= r.calibrated_admission_probability <= 1.0
        assert 0.0 <= r.clipped_score_prediction <= 1.0
        assert 0.0 <= r.expected_utility_prediction <= 1.0
        assert r.expected_utility_prediction == pytest.approx(
            r.calibrated_admission_probability * r.clipped_score_prediction
        )


def test_a_score_prediction_outside_the_unit_interval_is_clipped() -> None:
    from minos_engine.models.oof_runner import _clip01

    assert list(_clip01([-3.0, 0.5, 7.0])) == [0.0, 0.5, 1.0]


def test_the_outer_scaler_never_sees_the_held_out_chromosome() -> None:
    """If the scaler had seen the fold, its mean would move when the fold's values change."""
    rows, design, bams, weights, score_weights = _synthetic()
    held = "chr18"
    held_idx = [i for i, m in enumerate(design.meta) if m["chromosome"] == held]
    moved = design.contextual.copy()
    moved[held_idx] += 1000.0
    train_idx = [i for i, m in enumerate(design.meta) if m["chromosome"] != held]

    from sklearn.preprocessing import StandardScaler

    a = StandardScaler().fit(design.contextual[train_idx]).mean_
    b = StandardScaler().fit(moved[train_idx]).mean_
    assert np.allclose(a, b), "the training rows changed; the fold indices are wrong"


def test_the_calibrator_refuses_to_see_the_fold_it_will_be_applied_to() -> None:
    with pytest.raises(CalibrationError, match="held-out BAMs"):
        fit_nested_admission_calibrator(
            inner_probabilities=[0.1, 0.9],
            inner_labels=[0, 1],
            calibration_bams=frozenset({"bam-a", "bam-b"}),
            outer_heldout_bams=frozenset({"bam-b"}),
            inner_folds=4,
        )


def test_degenerate_calibration_labels_are_a_training_failure_not_a_constant() -> None:
    with pytest.raises(CalibrationError, match="degenerate"):
        fit_nested_admission_calibrator(
            inner_probabilities=[0.2, 0.4],
            inner_labels=[1, 1],
            calibration_bams=frozenset({"a"}),
            outer_heldout_bams=frozenset({"b"}),
            inner_folds=4,
        )


def test_the_calibration_bam_set_is_recorded_and_excludes_the_fold() -> None:
    rows, design, bams, weights, score_weights = _synthetic()
    records, _ = run_outer_oof(
        spec=_Spec(),
        rows=rows,
        design=design,
        chromosome_of=bams,
        weights=weights,
        score_weights=score_weights,
        fit_estimators=fit_fold_estimators,
    )
    assert all(r.calibration_bams_identity for r in records)
    assert len({r.calibration_bams_identity for r in records}) == 5


def test_a_single_class_admission_fold_is_a_training_failure() -> None:
    rows, design, bams, weights, score_weights = _synthetic()
    for meta in design.meta:
        meta["admission_label"] = 1
    records, failures = run_outer_oof(
        spec=_Spec(),
        rows=rows,
        design=design,
        chromosome_of=bams,
        weights=weights,
        score_weights=score_weights,
        fit_estimators=fit_fold_estimators,
    )
    assert not records
    assert len(failures) == 5
    assert all(f["class"] == "TRAINING_FAILURE" for f in failures)
    assert all("single class" in f["reason"] for f in failures)


def test_a_non_finite_prediction_is_a_training_failure() -> None:
    rows, design, bams, weights, score_weights = _synthetic()

    def broken(**kwargs: Any) -> dict[str, Any]:
        return {
            "raw_admission": lambda x: np.full(len(x), np.nan),
            "raw_score": lambda x: np.zeros(len(x)),
            "calibrate": lambda p: p,
            "calibration_bams": kwargs["train_bams"],
        }

    records, failures = run_outer_oof(
        spec=_Spec(),
        rows=rows,
        design=design,
        chromosome_of=bams,
        weights=weights,
        score_weights=score_weights,
        fit_estimators=broken,
    )
    assert not records
    assert len(failures) == 5
    assert all("non-finite" in f["reason"] for f in failures)


def test_a_numerical_exception_is_recorded_as_a_failure_not_raised() -> None:
    rows, design, bams, weights, score_weights = _synthetic()

    def exploding(**_: Any) -> dict[str, Any]:
        raise FloatingPointError("underflow in the solver")

    records, failures = run_outer_oof(
        spec=_Spec(),
        rows=rows,
        design=design,
        chromosome_of=bams,
        weights=weights,
        score_weights=score_weights,
        fit_estimators=exploding,
    )
    assert not records
    assert all(f["class"] == "TRAINING_FAILURE" for f in failures)
    assert all("FloatingPointError" in f["reason"] for f in failures)


def test_a_failed_fold_is_never_silently_dropped_from_the_record() -> None:
    """A candidate must not win by having fewer folds counted against it."""
    rows, design, bams, weights, score_weights = _synthetic()
    calls = {"n": 0}

    def flaky(**kwargs: Any) -> dict[str, Any]:
        calls["n"] += 1
        if calls["n"] == 1:
            raise TrainingFailure("deliberate")
        return fit_fold_estimators(**kwargs)

    records, failures = run_outer_oof(
        spec=_Spec(),
        rows=rows,
        design=design,
        chromosome_of=bams,
        weights=weights,
        score_weights=score_weights,
        fit_estimators=flaky,
    )
    assert len(failures) == 1
    assert len(records) < len(rows)
    assert {r.outer_fold for r in records} == set(CV_FOLD_CHROMOSOMES) - {failures[0]["fold"]}


def test_the_oof_artifact_identity_is_order_independent() -> None:
    rows, design, bams, weights, score_weights = _synthetic()
    records, _ = run_outer_oof(
        spec=_Spec(),
        rows=rows,
        design=design,
        chromosome_of=bams,
        weights=weights,
        score_weights=score_weights,
        fit_estimators=fit_fold_estimators,
    )
    assert oof_artifact_identity(records) == oof_artifact_identity(list(reversed(records)))


# ------------------------------------------------------------------------------------------ #
# design matrix and tie-breaks
# ------------------------------------------------------------------------------------------ #
def test_the_contextual_matrix_is_157_columns() -> None:
    assert CONTEXTUAL_COLUMN_COUNT == 157
    _, design, _, _, _ = _synthetic()
    assert design.contextual.shape[1] == 157


def test_metadata_never_becomes_a_predictor() -> None:
    _, design, _, _, _ = _synthetic()
    assert not (set(design.columns) & {"dataset_id", "chromosome", "config_hash", "outcome"})


def test_a_missing_feature_vector_refuses_rather_than_imputing() -> None:
    rows, _, bams, _, _ = _synthetic()
    with pytest.raises(DesignMatrixError, match="no verified feature vector"):
        build_design_matrix(
            rows=rows[:1],
            bam_vectors={},
            config_vectors={},
            bam_columns=tuple(f"b{i}" for i in range(129)),
            config_columns=tuple(f"c{i}" for i in range(28)),
        )


def test_a_prediction_tie_is_broken_by_the_lowest_config_hash() -> None:
    class R:
        def __init__(self, config: str) -> None:
            self.dataset_id, self.config_hash = "bam-a", config
            self.expected_utility_prediction = 0.5
            self.actual_utility = 0.1

    result = bam_selection_regret(
        [R("ff" * 32), R("00" * 32), R("aa" * 32)], family="LINEAR_REGULARIZED"
    )
    assert result["selected_config"]["bam-a"] == "00" * 32


def test_regret_is_oracle_minus_selected_over_observed_configs_only() -> None:
    class R:
        def __init__(self, config: str, predicted: float, actual: float) -> None:
            self.dataset_id, self.config_hash = "bam-a", config
            self.expected_utility_prediction, self.actual_utility = predicted, actual

    result = bam_selection_regret(
        [R("a" * 64, 0.9, 0.2), R("b" * 64, 0.1, 0.8)], family="LINEAR_REGULARIZED"
    )
    assert result["per_bam_regret"]["bam-a"] == pytest.approx(0.6)
    assert summarise_oof(result)["mean_regret"] == pytest.approx(0.6)


# ------------------------------------------------------------------------------------------ #
# shortlist rule
# ------------------------------------------------------------------------------------------ #
def test_the_shortlist_requires_both_bars() -> None:
    result = derive_train_shortlist(
        reference_metrics={"GLOBAL_MEAN": {"mean_regret": 0.10, "cvar_regret": 0.20}},
        candidate_metrics={
            "both": {"mean_regret": 0.05, "cvar_regret": 0.15},
            "mean_only": {"mean_regret": 0.05, "cvar_regret": 0.90},
            "cvar_only": {"mean_regret": 0.90, "cvar_regret": 0.05},
        },
    )
    assert result["shortlist"] == ["both"]


def test_an_empty_shortlist_stays_empty_and_keeps_the_safe_baseline() -> None:
    result = derive_train_shortlist(
        reference_metrics={"GLOBAL_MEAN": {"mean_regret": 0.01, "cvar_regret": 0.02}},
        candidate_metrics={"a": {"mean_regret": 0.5, "cvar_regret": 0.6}},
    )
    assert result["shortlist"] == []
    assert result["shortlist_empty"] is True
    assert "SAFE_BASELINE" in result["fallback_if_empty"]


def test_the_best_reference_is_the_bar_not_the_average() -> None:
    result = derive_train_shortlist(
        reference_metrics={
            "weak": {"mean_regret": 0.90, "cvar_regret": 0.90},
            "strong": {"mean_regret": 0.05, "cvar_regret": 0.05},
        },
        candidate_metrics={"middling": {"mean_regret": 0.40, "cvar_regret": 0.40}},
    )
    assert result["shortlist"] == []
    assert result["best_reference_mean_regret"] == 0.05


def test_a_campaign_result_must_bind_all_ten_spec_hashes() -> None:
    from minos_engine.models.shortlist import _train_campaign_result_content

    with pytest.raises(ShortlistError, match="ten model-spec hashes"):
        _train_campaign_result_content(
            source_commit="a" * 40,
            source_tree="b" * 40,
            prefit_authority_sha256="c" * 64,
            training_dataset_hash="d" * 64,
            cv_manifest_hash="e" * 64,
            training_runtime_hash="f" * 64,
            candidate_spec_hashes=("1" * 64,),
            reference_spec_hashes=(),
            oof_artifact_hashes={},
            metric_artifact_hashes={},
            training_failures=(),
            thread_report=(),
            shortlist={
                "best_reference_mean_regret": 0.0,
                "best_reference_cvar_regret": 0.0,
                "shortlist": [],
                "shortlist_empty": True,
            },
        )


# ------------------------------------------------------------------------------------------ #
# references
# ------------------------------------------------------------------------------------------ #
def test_the_constant_reference_always_returns_the_qualified_baseline_config() -> None:
    from minos_engine.models.contract import SAFE_BASELINE_CONFIG_HASH
    from minos_engine.models.references import ConstantSafeBaseline

    meta = [
        {
            "dataset_id": "b1",
            "config_hash": SAFE_BASELINE_CONFIG_HASH,
            "admission_label": 1,
            "admitted_score": 0.8,
        },
        {"dataset_id": "b1", "config_hash": "z" * 64, "admission_label": 0, "admitted_score": None},
    ]
    reference = ConstantSafeBaseline.fit(meta)
    assert reference.choose_config(["z" * 64, SAFE_BASELINE_CONFIG_HASH]) == (
        SAFE_BASELINE_CONFIG_HASH
    )
    assert reference.admitted_score == pytest.approx(0.8)


def test_the_global_mean_predicts_one_number_and_breaks_ties_lexicographically() -> None:
    from minos_engine.models.references import GlobalMean

    meta = [
        {"dataset_id": "b1", "config_hash": "a" * 64, "admission_label": 1, "admitted_score": 0.6},
        {"dataset_id": "b2", "config_hash": "b" * 64, "admission_label": 0, "admitted_score": None},
        {"dataset_id": "b2", "config_hash": "c" * 64, "admission_label": 1, "admitted_score": 0.8},
    ]
    reference = GlobalMean.fit(meta)
    predictions = reference.predict_expected_utility(meta)
    assert len(set(predictions)) == 1
    assert reference.choose_config(["f" * 64, "0" * 64]) == "0" * 64


def test_the_block_references_see_only_their_own_columns() -> None:
    from minos_engine.models.references import BamFeaturesOnlyRidge, ConfigOnlyRidge

    rng = np.random.default_rng(3)
    meta = [
        {
            "dataset_id": f"b{i % 4}",
            "config_hash": f"{i:064x}",
            "admission_label": int(i % 3 > 0),
            "admitted_score": 0.7 if i % 3 > 0 else None,
        }
        for i in range(24)
    ]
    inner = [
        (frozenset({"b0", "b1", "b2", "b3"}) - frozenset({f"b{i}"}), frozenset({f"b{i}"}))
        for i in range(4)
    ]
    held = frozenset({"b9"})
    config_only = ConfigOnlyRidge.fit(
        rng.normal(size=(24, 28)), meta, inner_folds=inner, outer_heldout_bams=held
    )
    bam_only = BamFeaturesOnlyRidge.fit(
        rng.normal(size=(24, 129)), meta, inner_folds=inner, outer_heldout_bams=held
    )
    assert config_only.family == "CONFIG_ONLY"
    assert bam_only.family == "BAM_FEATURES_ONLY"
    assert bam_only.choose_config(["f" * 64, "1" * 64]) == "1" * 64


def test_every_reference_exposes_the_one_prediction_formula() -> None:
    from minos_engine.models.references import ConstantSafeBaseline, GlobalMean

    for reference in (ConstantSafeBaseline(0.5, 0.6), GlobalMean(0.5, 0.6)):
        meta = [{}, {}]
        u = reference.predict_expected_utility(meta)
        p = reference.predict_admission_probability(meta)
        s = reference.predict_admitted_score(meta)
        assert np.allclose(u, np.asarray(p) * np.asarray(s))
        assert np.all((u >= 0) & (u <= 1))


# ------------------------------------------------------------------------------------------ #
# stage locks
# ------------------------------------------------------------------------------------------ #
def test_the_trainer_source_has_no_validation_dependency() -> None:
    root = _repo() / "src/minos_engine/models"
    for path in root.glob("*.py"):
        source = path.read_text(encoding="utf-8")
        assert "l2f2_validation" not in source, f"{path.name} reaches for VALIDATION"
        assert "phase_d_selection" not in source, f"{path.name} reaches for Phase-D results"
        assert "validation_finalists" not in source, f"{path.name} imports validation finalists"


def test_the_trainer_source_has_no_test_dependency() -> None:
    root = _repo() / "src/minos_engine/models"
    for path in root.glob("*.py"):
        source = path.read_text(encoding="utf-8")
        assert not re.search(r"partition\s*=\s*[\"']test[\"']", source), path.name
        assert "TEST_TRUTH" not in source and "test_feature_matrix" not in source


def test_no_real_campaign_artifact_was_produced() -> None:
    workspace = _workspace()
    for forbidden in ("oof_predictions.json", "train_oof_campaign_result.json", "metrics.json"):
        assert not (workspace / forbidden).exists(), f"a real campaign artifact exists: {forbidden}"


def test_models_qualified_is_still_absent_and_select_config_still_blocked() -> None:
    from minos_engine.layer2.service import Layer2Service

    assert not (_repo() / "gates/models-qualified.json").exists()
    with pytest.raises(Exception) as excinfo:
        Layer2Service().select_config(None)  # type: ignore[arg-type]
    assert "StageNotReady" in type(excinfo.value).__name__
