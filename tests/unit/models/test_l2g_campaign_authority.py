"""The sealed production campaign boundary and the canonical campaign result.

The question behind every test here is the same: can an operator make the real campaign run a
different experiment than the frozen one, or make the result assert something the campaign did
not establish? Dependency injection is what makes the core testable, and it is exactly what the
production entry must not expose.
"""

from __future__ import annotations

import copy
import inspect
from typing import Any

import numpy as np
import pytest

from minos_engine.models.campaign import (
    ACCEPTED_CANDIDATE_FAMILIES,
    ACCEPTED_CANDIDATE_SPEC_HASHES,
    ACCEPTED_REFERENCE_SPECS,
    REQUIRED_THREAD_POLICY,
    CampaignError,
    _run_l2g_train_oof_core,
    _verify_exact_specs,
    assess_completeness,
    run_real_l2g_train_oof_campaign,
)
from minos_engine.models.contract import CV_FOLD_CHROMOSOMES, SAFE_BASELINE_CONFIG_HASH
from minos_engine.models.design_matrix import DesignMatrix
from minos_engine.models.fit_driver import fit_fold_estimators, fit_reference_fold
from minos_engine.models.oof_runner import metric_artifact_identity
from minos_engine.models.shortlist import (
    SHORTLIST_RESULT_SCHEMA,
    ShortlistError,
    build_campaign_result,
    campaign_result_identity,
    verify_campaign_result,
)

_SCIENTIFIC_PARAMETERS = {
    "dataset",
    "design",
    "candidate_specs",
    "reference_specs",
    "fit_estimators",
    "fit_reference",
    "metrics",
    "shortlist",
    "thread_report",
    "folds",
    "specs",
    "spec_subset",
    "fold_subset",
}


# ------------------------------------------------------------------------------------------ #
# §2 / §5 -- the production boundary accepts no science
# ------------------------------------------------------------------------------------------ #
def test_the_production_entry_accepts_only_operational_handles() -> None:
    parameters = set(inspect.signature(run_real_l2g_train_oof_campaign).parameters)
    assert parameters == {
        "feature_matrix_artifact_path",
        "workspace",
        "config_payload_root",
        "root",
    }
    assert not (parameters & _SCIENTIFIC_PARAMETERS)


def test_the_injectable_core_is_private() -> None:
    """Injection is kept, but not reachable as the production API."""
    import minos_engine.models.campaign as module

    assert "run_l2g_train_oof_campaign" not in module.__all__
    assert not hasattr(module, "run_l2g_train_oof_campaign")
    assert "run_real_l2g_train_oof_campaign" in module.__all__
    core = set(inspect.signature(_run_l2g_train_oof_core).parameters)
    assert {"dataset", "design", "fit_estimators", "fit_reference"} <= core


def test_a_fake_fit_function_cannot_be_injected_through_the_production_api() -> None:
    def fake(**_: Any) -> dict[str, Any]:  # pragma: no cover - never invoked
        raise AssertionError("a caller-supplied fit ran")

    with pytest.raises(TypeError, match="unexpected keyword argument"):
        run_real_l2g_train_oof_campaign(  # type: ignore[call-arg]
            feature_matrix_artifact_path="/nonexistent", fit_estimators=fake
        )


@pytest.mark.parametrize(
    "argument",
    [
        "dataset",
        "design",
        "candidate_specs",
        "reference_specs",
        "shortlist",
        "thread_report",
        "metrics",
    ],
)
def test_no_scientific_object_can_cross_the_production_boundary(argument: str) -> None:
    with pytest.raises(TypeError, match="unexpected keyword argument"):
        run_real_l2g_train_oof_campaign(  # type: ignore[call-arg]
            feature_matrix_artifact_path="/nonexistent", **{argument: object()}
        )


def test_the_production_entry_uses_the_committed_fit_implementations() -> None:
    source = inspect.getsource(run_real_l2g_train_oof_campaign)
    assert "fit_estimators=fit_fold_estimators" in source
    assert "fit_reference=fit_reference_fold" in source
    assert "importlib" not in source


# ------------------------------------------------------------------------------------------ #
# §3 / §6 -- everything is derived inside the boundary
# ------------------------------------------------------------------------------------------ #
def test_the_production_entry_derives_the_whole_authority_internally() -> None:
    source = inspect.getsource(run_real_l2g_train_oof_campaign)
    for derivation in (
        "load_accepted_prefit_authority(",
        "load_verified_training_dataset(",
        "load_verified_feature_values(",
        "load_verified_config_vectors(",
        "build_design_matrix(",
        "build_accepted_l2g_model_specs(dataset)",
        "build_accepted_l2g_reference_specs(dataset)",
        "verify_training_runtime()",
    ):
        assert derivation in source, f"the boundary does not derive: {derivation}"


def test_the_production_entry_requires_the_accepted_dataset_and_cv_identities() -> None:
    source = inspect.getsource(run_real_l2g_train_oof_campaign)
    assert "ACCEPTED_TRAINING_DATASET_HASH" in source
    assert "ACCEPTED_CV_MANIFEST_HASH" in source
    assert "ACCEPTED_FEATURE_MATRIX_HASH" in source
    assert "ACCEPTED_CONFIG_ENCODING_IDENTITY" in source


def test_the_production_entry_proves_the_exact_cell_set_of_its_design_matrix() -> None:
    source = inspect.getsource(run_real_l2g_train_oof_campaign)
    assert "design_cells != dataset_cells" in source
    assert "repeats a scientific cell" in source


def test_a_wrong_artifact_path_fails_rather_than_substitutes() -> None:
    from minos_engine.models.feature_values import FeatureValuesError

    with pytest.raises((FeatureValuesError, CampaignError)):
        run_real_l2g_train_oof_campaign(feature_matrix_artifact_path="/nonexistent/matrix.parquet")


# ------------------------------------------------------------------------------------------ #
# §4 -- exact spec identities, not counts
# ------------------------------------------------------------------------------------------ #
class _Spec:
    def __init__(self, family: str, spec_hash: str) -> None:
        self.family = family
        self._hash = spec_hash
        self.score_model_implementation = "sklearn.linear_model.Ridge"
        self.admission_model_implementation = "sklearn.linear_model.LogisticRegression"
        self.score_hyperparameters: dict[str, Any] = {"alpha": 1.0}
        self.admission_hyperparameters: dict[str, Any] = {"C": 1.0, "max_iter": 1000}
        self.random_seed = 20260904

    def identity(self) -> str:
        return self._hash


def _accepted_candidates() -> tuple[_Spec, ...]:
    return tuple(
        _Spec(f, h)
        for f, h in zip(ACCEPTED_CANDIDATE_FAMILIES, ACCEPTED_CANDIDATE_SPEC_HASHES, strict=True)
    )


def _accepted_references() -> tuple[_Spec, ...]:
    return tuple(_Spec(f, h) for f, h in ACCEPTED_REFERENCE_SPECS)


def test_the_exact_accepted_identities_are_required() -> None:
    _verify_exact_specs(
        _accepted_candidates(),
        expected=ACCEPTED_CANDIDATE_SPEC_HASHES,
        families=ACCEPTED_CANDIDATE_FAMILIES,
        role="candidate",
    )


def test_six_specs_with_one_foreign_hash_are_refused() -> None:
    specs = list(_accepted_candidates())
    specs[3] = _Spec("TREE_ENSEMBLE", "f" * 64)
    with pytest.raises(CampaignError, match="not the accepted frozen set"):
        _verify_exact_specs(
            tuple(specs),
            expected=ACCEPTED_CANDIDATE_SPEC_HASHES,
            families=ACCEPTED_CANDIDATE_FAMILIES,
            role="candidate",
        )


def test_four_references_with_right_families_but_a_foreign_hash_are_refused() -> None:
    specs = list(_accepted_references())
    specs[2] = _Spec("CONFIG_ONLY", "e" * 64)
    with pytest.raises(CampaignError, match="not the accepted frozen set"):
        _verify_exact_specs(
            tuple(specs),
            expected=tuple(h for _, h in ACCEPTED_REFERENCE_SPECS),
            families=tuple(f for f, _ in ACCEPTED_REFERENCE_SPECS),
            role="reference",
        )


def test_a_duplicate_spec_hash_is_refused() -> None:
    specs = list(_accepted_candidates())
    specs[1] = _Spec("LINEAR_REGULARIZED", ACCEPTED_CANDIDATE_SPEC_HASHES[0])
    with pytest.raises(CampaignError, match="appears twice"):
        _verify_exact_specs(
            tuple(specs),
            expected=ACCEPTED_CANDIDATE_SPEC_HASHES,
            families=ACCEPTED_CANDIDATE_FAMILIES,
            role="candidate",
        )


def test_reordering_the_accepted_specs_is_refused() -> None:
    specs = list(_accepted_candidates())
    specs[0], specs[1] = specs[1], specs[0]
    with pytest.raises(CampaignError, match="not the accepted frozen set"):
        _verify_exact_specs(
            tuple(specs),
            expected=ACCEPTED_CANDIDATE_SPEC_HASHES,
            families=ACCEPTED_CANDIDATE_FAMILIES,
            role="candidate",
        )


def test_a_correct_hash_under_the_wrong_family_is_refused() -> None:
    specs = list(_accepted_candidates())
    specs[0] = _Spec("COMPACT_MLP", ACCEPTED_CANDIDATE_SPEC_HASHES[0])
    with pytest.raises(CampaignError, match="families are"):
        _verify_exact_specs(
            tuple(specs),
            expected=ACCEPTED_CANDIDATE_SPEC_HASHES,
            families=ACCEPTED_CANDIDATE_FAMILIES,
            role="candidate",
        )


def test_the_frozen_hashes_here_are_the_ones_the_factory_derives() -> None:
    from minos_engine.models.prefit_loader import load_verified_training_dataset
    from minos_engine.models.spec_factory import (
        build_accepted_l2g_model_specs,
        build_accepted_l2g_reference_specs,
    )

    dataset = load_verified_training_dataset()
    assert (
        tuple(s.identity() for s in build_accepted_l2g_model_specs(dataset))
        == ACCEPTED_CANDIDATE_SPEC_HASHES
    )
    assert tuple(s.identity() for s in build_accepted_l2g_reference_specs(dataset)) == tuple(
        h for _, h in ACCEPTED_REFERENCE_SPECS
    )


# ------------------------------------------------------------------------------------------ #
# §7 -- exact cell-set closure
# ------------------------------------------------------------------------------------------ #
class _Rec:
    def __init__(self, bam: str, config: str, fold: str) -> None:
        self.dataset_id, self.config_hash, self.outer_fold = bam, config, fold


def _cells() -> tuple[dict[str, str], list[str]]:
    bams = {f"bam-{c}-{i}": c for c in CV_FOLD_CHROMOSOMES for i in range(10)}
    configs = [SAFE_BASELINE_CONFIG_HASH, *[f"{i:064x}" for i in range(5)]]
    return bams, configs


def test_an_exact_cell_set_is_verified_when_supplied() -> None:
    bams, configs = _cells()
    records = [_Rec(b, c, bams[b]) for b in bams for c in configs]
    expected = frozenset((r.dataset_id, r.config_hash) for r in records)
    result = assess_completeness(
        records=records,
        failures=[],
        expected_cells=len(records),
        expected_bams=50,
        expected_cell_set=expected,
    )
    assert result["status"] == "COMPLETE"
    assert result["exact_cell_set_verified"] is True


def test_one_missing_cell_substituted_by_a_foreign_one_is_refused() -> None:
    """The count still totals correctly, which is exactly why counting is not enough."""
    bams, configs = _cells()
    records = [_Rec(b, c, bams[b]) for b in bams for c in configs]
    expected = frozenset((r.dataset_id, r.config_hash) for r in records)
    substituted = [*records[:-1], _Rec(records[-1].dataset_id, "9" * 64, records[-1].outer_fold)]
    assert len(substituted) == len(records)
    result = assess_completeness(
        records=substituted,
        failures=[],
        expected_cells=len(records),
        expected_bams=50,
        expected_cell_set=expected,
    )
    assert result["status"] == "TRAINING_FAILURE"
    assert result["exact_cell_set_verified"] is False
    assert "1 missing, 1 foreign" in result["reasons"][0]


def test_completeness_without_a_cell_set_does_not_claim_the_proof() -> None:
    bams, configs = _cells()
    records = [_Rec(b, c, bams[b]) for b in bams for c in configs]
    result = assess_completeness(
        records=records, failures=[], expected_cells=len(records), expected_bams=50
    )
    assert result["exact_cell_set_verified"] is False


# ------------------------------------------------------------------------------------------ #
# §8 -- thread evidence is observed, never supplied
# ------------------------------------------------------------------------------------------ #
def test_the_production_entry_observes_its_own_thread_evidence() -> None:
    source = inspect.getsource(run_real_l2g_train_oof_campaign)
    assert "observe_thread_pools()" in source
    assert "single_threaded()" in source
    assert "thread_report=thread_report" in source
    assert REQUIRED_THREAD_POLICY == "SINGLE_THREADED_DETERMINISTIC"


def test_the_boundary_refuses_when_thread_enforcement_does_not_bind() -> None:
    source = inspect.getsource(run_real_l2g_train_oof_campaign)
    assert "thread enforcement did not bind" in source
    assert "cannot be evidenced" in source


# ------------------------------------------------------------------------------------------ #
# a full synthetic closure -> canonical result
# ------------------------------------------------------------------------------------------ #
def _synthetic_closure() -> dict[str, Any]:
    rng = np.random.default_rng(5)
    bams, configs = _cells()

    class _Row:
        def __init__(self, bam: str, config: str, chromosome: str, admitted: bool) -> None:
            self.dataset_id, self.config_hash, self.chromosome = bam, config, chromosome
            self.admission_label = 1 if admitted else 0
            self.outcome = "ADMITTED" if admitted else "CANDIDATE_NON_ADMISSION"
            self.admitted_score = float(np.clip(rng.normal(0.7, 0.1), 0, 1)) if admitted else None

        def identity(self) -> str:
            return f"{self.dataset_id}|{self.config_hash}"

    rows = [_Row(b, c, ch, bool(rng.random() > 0.25)) for b, ch in bams.items() for c in configs]
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

    campaign = _run_l2g_train_oof_core(
        dataset=_DS(),
        design=design,
        candidate_specs=_accepted_candidates(),
        reference_specs=_accepted_references(),
        fit_estimators=fit_fold_estimators,
        fit_reference=fit_reference_fold,
        thread_report=(
            {"user_api": "blas", "internal_api": "openblas", "num_threads": 1, "prefix": "x"},
        ),
    )
    # the sealed entry attaches these; the core deliberately does not invent them
    campaign["authority"] = dict.fromkeys(
        (
            "prefit_authority_sha256",
            "training_dataset_hash",
            "cv_manifest_hash",
            "feature_matrix_hash",
            "feature_matrix_artifact_sha256",
            "config_encoding_identity",
            "training_contract_hash",
            "training_protocol_hash",
            "training_runtime_hash",
        ),
        "a" * 64,
    )
    from minos_engine.models.shortlist import ACCEPTED_AUTHORITIES

    campaign["authority"] = {
        **ACCEPTED_AUTHORITIES,
        "prefit_authority_sha256": "61d8b33432202c1813a3d64d37bb727f8f1b8012ef1af23c7bf7af0ef8356000",
    }
    campaign["thread_policy"] = REQUIRED_THREAD_POLICY
    campaign["candidate_spec_hashes"] = list(ACCEPTED_CANDIDATE_SPEC_HASHES)
    campaign["reference_spec_hashes"] = [h for _, h in ACCEPTED_REFERENCE_SPECS]
    for entry in campaign["per_spec"].values():
        entry["expected_oof_record_count"] = 1040
        entry["observed_oof_record_count"] = 1040
        entry["unique_bam_count"] = 50
    return campaign


@pytest.fixture(scope="module")
def closure() -> dict[str, Any]:
    return _synthetic_closure()


@pytest.fixture(scope="module")
def result(published_l2g_result: dict[str, Any]) -> dict[str, Any]:
    """The canonical result from the shared published campaign."""
    return published_l2g_result


def test_the_canonical_result_binds_per_spec_completeness(result: dict[str, Any]) -> None:
    assert (
        result["schema_version"] == SHORTLIST_RESULT_SCHEMA == ("l2g-train-oof-campaign-result-v2")
    )
    assert len(result["per_spec"]) == 10
    for entry in result["per_spec"]:
        for field in (
            "spec_hash",
            "family",
            "role",
            "status",
            "expected_outer_fold_count",
            "successful_outer_fold_count",
            "failed_folds",
            "expected_oof_record_count",
            "observed_oof_record_count",
            "unique_bam_count",
            "duplicate_cell_count",
            "exact_cell_set_verified",
            "training_failures",
        ):
            assert field in entry, f"v2 claims to bind {field} but does not"


def test_the_canonical_result_binds_the_authority_and_source(result: dict[str, Any]) -> None:
    for field in (
        "training_dataset_hash",
        "cv_manifest_hash",
        "training_protocol_hash",
        "training_runtime_hash",
        "feature_matrix_hash",
        "feature_matrix_artifact_sha256",
        "config_encoding_identity",
        "training_contract_hash",
        "prefit_authority_sha256",
        "source_commit",
        "source_tree",
    ):
        assert result[field]
    assert result["validation_read"] is False
    assert result["test_accessed"] is False
    assert result["thread_policy"] == REQUIRED_THREAD_POLICY


def test_the_source_provenance_comes_from_git_not_the_caller() -> None:
    source = inspect.getsource(build_campaign_result)
    parameters = set(inspect.signature(build_campaign_result).parameters)
    assert parameters == {"trusted", "published", "root"}
    assert not (parameters & {"source_commit", "source_tree", "shortlist", "per_spec"})
    # provenance now comes from the campaign, captured at EXECUTION
    assert "trusted.execution_source_commit" in source


def test_the_result_verifies_and_its_identity_is_stable(result: dict[str, Any]) -> None:
    assert verify_campaign_result(result)["ok"] is True
    assert campaign_result_identity(result) == campaign_result_identity(copy.deepcopy(result))


@pytest.mark.parametrize(
    ("label", "field", "value"),
    [
        ("folds", "successful_outer_fold_count", 4),
        ("records", "observed_oof_record_count", 1039),
        ("bams", "unique_bam_count", 49),
        ("duplicates", "duplicate_cell_count", 1),
        ("cell set", "exact_cell_set_verified", False),
    ],
)
def test_a_fabricated_complete_status_is_refused(
    result: dict[str, Any], label: str, field: str, value: Any
) -> None:
    tampered = copy.deepcopy(result)
    tampered["per_spec"][0][field] = value
    with pytest.raises(ShortlistError, match="COMPLETE"):
        verify_campaign_result(tampered)
    assert label


def test_a_failed_spec_carrying_a_scientific_artifact_is_refused(result: dict[str, Any]) -> None:
    tampered = copy.deepcopy(result)
    tampered["per_spec"][0]["status"] = "TRAINING_FAILURE"
    with pytest.raises(ShortlistError, match="failed but carries"):
        verify_campaign_result(tampered)


def test_a_reference_failure_with_threshold_available_true_is_refused(
    result: dict[str, Any],
) -> None:
    tampered = copy.deepcopy(result)
    reference = next(e for e in tampered["per_spec"] if e["role"] == "REFERENCE")
    reference["status"] = "TRAINING_FAILURE"
    for field in (
        "oof_scientific_hash",
        "metric_scientific_hash",
        "oof_file_sha256",
        "metric_file_sha256",
        "promotion_metrics",
    ):
        reference.pop(field, None)
    with pytest.raises(ShortlistError, match="reference|threshold"):
        verify_campaign_result(tampered)


def test_a_shortlisted_candidate_that_did_not_complete_is_refused(result: dict[str, Any]) -> None:
    tampered = copy.deepcopy(result)
    tampered["shortlist"] = ["f" * 64]
    tampered["shortlist_empty"] = False
    with pytest.raises(ShortlistError, match="not eligible and COMPLETE"):
        verify_campaign_result(tampered)


def test_a_duplicated_oof_scientific_hash_is_refused(result: dict[str, Any]) -> None:
    tampered = copy.deepcopy(result)
    tampered["per_spec"][0]["oof_scientific_hash"] = tampered["per_spec"][1]["oof_scientific_hash"]
    with pytest.raises(ShortlistError, match="share an OOF artifact hash"):
        verify_campaign_result(tampered)


def test_the_builder_recomputes_the_metric_identity_from_its_own_metrics() -> None:
    """An exchange preserves uniqueness, so only recomputation catches it."""
    source = inspect.getsource(build_campaign_result)
    assert "metric_artifact_identity(metrics, spec_hash=spec_hash)" in source
    assert "does not describe its own" in source


def test_the_metric_artifact_identity_is_bound_to_its_spec() -> None:
    metrics = {"mean_regret": 0.1, "cvar_regret": 0.2}
    assert metric_artifact_identity(metrics, spec_hash="a" * 64) != metric_artifact_identity(
        metrics, spec_hash="b" * 64
    )


def test_removing_per_spec_completeness_is_refused(result: dict[str, Any]) -> None:
    tampered = copy.deepcopy(result)
    for entry in tampered["per_spec"]:
        entry.pop("exact_cell_set_verified", None)
    with pytest.raises((ShortlistError, KeyError)):
        verify_campaign_result(tampered)


def test_a_tampered_source_commit_moves_the_result_identity(result: dict[str, Any]) -> None:
    tampered = copy.deepcopy(result)
    tampered["source_commit"] = "0" * 40
    assert campaign_result_identity(tampered) != campaign_result_identity(result)


def test_a_validation_read_is_refused(result: dict[str, Any]) -> None:
    tampered = copy.deepcopy(result)
    tampered["validation_read"] = True
    with pytest.raises(ShortlistError, match="VALIDATION read"):
        verify_campaign_result(tampered)


def test_a_test_access_is_refused(result: dict[str, Any]) -> None:
    tampered = copy.deepcopy(result)
    tampered["test_accessed"] = True
    with pytest.raises(ShortlistError, match="TEST access"):
        verify_campaign_result(tampered)


def test_the_builder_refuses_a_closure_missing_specs(closure: dict[str, Any]) -> None:
    source = inspect.getsource(build_campaign_result)
    assert "must describe all ten frozen specs" in source
    assert copy is not None


# ------------------------------------------------------------------------------------------ #
# stage locks
# ------------------------------------------------------------------------------------------ #
def test_no_real_campaign_artifact_exists() -> None:
    from tests.minos_scratch import CANONICAL_MINOS_ROOT

    workspace = CANONICAL_MINOS_ROOT / "minos_l2g_training"
    for forbidden in (
        "oof_predictions.json",
        "train_oof_campaign_result.json",
        "metrics.json",
    ):
        assert not (workspace / forbidden).exists()


def test_models_qualified_absent_and_select_config_blocked() -> None:
    from pathlib import Path

    from minos_engine.layer2.service import Layer2Service

    root = Path(__file__).resolve().parents[3]
    assert not (root / "gates/models-qualified.json").exists()
    with pytest.raises(Exception) as excinfo:
        Layer2Service().select_config(None)  # type: ignore[arg-type]
    assert "StageNotReady" in type(excinfo.value).__name__
