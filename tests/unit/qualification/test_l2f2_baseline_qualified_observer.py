"""The production observer: what a caller may supply, and what it may never claim.

The point of this suite is negative. Every PASS-relevant fact must be MEASURED by an observer,
so the tests worth writing are the ones that try to supply one instead.
"""

from __future__ import annotations

import inspect
import json
from pathlib import Path

import pytest

from minos_engine.qualification import l2f2_baseline_qualified_qualifier as qualifier
from minos_engine.qualification.l2f2_baseline_qualified_contract import (
    ACCEPTED_BCFTOOLS_DIGEST,
    ACCEPTED_HAPPY_DIGEST,
    HARNESS_READY_GATE_HASH,
    HARNESS_READY_QUALIFICATION_HASH,
)
from minos_engine.qualification.l2f2_baseline_qualified_qualifier import (
    CLOSURE_AUTHORITY_SOURCE,
    BaselineQualificationObservationError,
    TrustedBaselineQualification,
    observe_closure_and_selected,
    observe_evidence_hashes,
    observe_harness_prerequisite,
    observe_protocol_identities,
    observe_scorer_authority,
    observe_source_provenance,
    observe_test_seal,
    observe_train_evidence,
    observe_train_validation_disjointness,
    run_baseline_qualified_qualification,
)
from tests.minos_scratch import CANONICAL_MINOS_ROOT

_REAL_CLOSURE = (
    CANONICAL_MINOS_ROOT / "minos_l2f2_validation" / "phase_d_real_closure_20260903T094127Z.json"
)


def _repo() -> Path:
    return Path(__file__).resolve().parents[3]


# --------------------------------------------------------------------------------------------
# §15/§16 — nothing PASS-relevant is caller-nominable
# --------------------------------------------------------------------------------------------
_FORBIDDEN_PARAMETERS = (
    "checks",
    "winner",
    "ranking",
    "scores",
    "statistics",
    "harness_ready_gate_verified",
    "scorer_source_identities_verified",
    "baseline_selected_manifest_verified",
    "closure_artifact_verified",
    "selected_statistics_verified",
    "test_untouched",
    "train_and_validation_identities_disjoint",
    "worktree_clean",
    "descends_closure_authority_source",
    "all_candidates_complete",
    "selected_config_hash",
    "selected_rank",
    "objective_identity",
    "candidate_design_identity",
    "evaluation_count",
    "logical_job_count",
    "evaluation_set_sha256",
    "execution_failure_set_sha256",
    "evidence_sha256",
    "baseline_selected_hash",
    "phase_d_closure_hash",
    "qualified_source_git_sha",
    "qualified_source_tree_sha",
    "result",
)


def test_the_production_entry_accepts_only_locations() -> None:
    """A caller says WHERE to look. It never says what will be found."""
    parameters = inspect.signature(run_baseline_qualified_qualification).parameters
    assert set(parameters) == {
        "root",
        "closure_artifact",
        "evidence_paths",
        "train_database_url",
    }, sorted(parameters)
    for forbidden in _FORBIDDEN_PARAMETERS:
        assert forbidden not in parameters, forbidden


def test_the_production_entry_takes_no_qualification_result() -> None:
    """It BUILDS the result. It must not accept one."""
    parameters = inspect.signature(run_baseline_qualified_qualification).parameters
    for name, parameter in parameters.items():
        annotation = str(parameter.annotation)
        assert "BaselineQualificationResult" not in annotation, name


def test_no_production_module_can_mint_from_an_arbitrary_result() -> None:
    """§16 — the decisive regression. A general mint helper is exactly the removed defect."""
    src = _repo() / "src"
    offenders: list[str] = []
    for path in sorted(src.rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        if "_mint_trusted" in text and "``_mint_trusted" not in text:
            offenders.append(str(path.relative_to(src)))
        # any production call site constructing the wrapper must be the qualifier's own mint
        if "TrustedBaselineQualification(" in text and path.name != (
            "l2f2_baseline_qualified_qualifier.py"
        ):
            offenders.append(f"{path.relative_to(src)} constructs the trusted wrapper")
    assert offenders == [], offenders


def test_the_mint_token_is_not_exported() -> None:
    assert "_MINT" not in (qualifier.__all__ or ())
    assert "_MintToken" not in (qualifier.__all__ or ())


def test_a_foreign_token_cannot_mint() -> None:
    from minos_engine.qualification.l2f2_baseline_qualified_contract import (
        BaselineQualificationResult,
    )

    with pytest.raises(BaselineQualificationObservationError, match="production qualifier alone"):
        TrustedBaselineQualification(object(), object())  # type: ignore[arg-type]
    assert BaselineQualificationResult is not None


# --------------------------------------------------------------------------------------------
# observers measure what they claim
# --------------------------------------------------------------------------------------------
def test_source_provenance_is_measured_from_git() -> None:
    import subprocess

    observed = observe_source_provenance(_repo())
    head = subprocess.run(
        ["git", "-C", str(_repo()), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert observed["qualified_source_git_sha"] == head
    assert len(observed["qualified_source_tree_sha"]) == 40
    assert observed["descends_closure_authority_source"] is True
    assert isinstance(observed["worktree_clean"], bool)


def test_source_provenance_refuses_a_non_repository(tmp_path: Path) -> None:
    with pytest.raises(BaselineQualificationObservationError, match="not a git repository"):
        observe_source_provenance(tmp_path)


def test_the_harness_prerequisite_is_verified_not_asserted() -> None:
    observed = observe_harness_prerequisite(_repo())
    assert observed["harness_ready_gate_hash"] == HARNESS_READY_GATE_HASH
    assert observed["harness_ready_qualification_hash"] == HARNESS_READY_QUALIFICATION_HASH
    assert observed["harness_ready_gate_verified"] is True


def test_the_scorer_authority_is_read_from_the_committed_manifest() -> None:
    observed = observe_scorer_authority(_repo())
    assert observed["scoring_contract_hash"] == (
        "b24a07e208ce8e2fff6672102ae4e61aed93c6f352a5af46ba81c4789adb76d6"
    )
    assert observed["minos_subnet_sha"] == "649bb92c6abccebde58a736a2b2af7fd77a701c1"
    assert observed["happy_resolved_digest"] == ACCEPTED_HAPPY_DIGEST
    assert observed["bcftools_resolved_digest"] == ACCEPTED_BCFTOOLS_DIGEST
    assert observed["scorer_source_identities_verified"] is True


def test_the_protocol_identities_are_derived_from_the_committed_protocol() -> None:
    observed = observe_protocol_identities(_repo())
    assert observed["objective_identity"] == (
        "a2d2bc85a49b4e09761a67a451b9bd9106b5c5180329282a6e1a86a13c7e0b62"
    )
    assert observed["candidate_design_identity"] == (
        "5695fe2c40104dda49260d6ba4ed790bf56b261a1e73c022ea99d518f4f3ead4"
    )


def test_the_test_seal_is_derived_from_an_accepted_gate_without_opening_test() -> None:
    """Gate metadata only. No TEST identity, path, truth or feature value is touched."""
    observed = observe_test_seal(_repo())
    assert observed["test_untouched"] is True
    assert observed["test_seal_evidence"]["sealed_check"] == "sealed_test_access_denied_passed"

    source = inspect.getsource(observe_test_seal)
    # the CHECK NAME is metadata and legitimately appears; what must not appear is any operation
    # that would reach TEST scientific content.
    for forbidden in (
        "dataset_id",
        "truth_vcf",
        "read_bytes",
        "feature_values",
        "split_allocations",
        "SELECT",
    ):
        assert forbidden not in source, forbidden


def test_disjointness_is_a_real_set_intersection() -> None:
    """§11 -- the earlier check only looked at a name prefix, which proved nothing."""
    train = frozenset(f"minos-train-{i:02d}" for i in range(50))
    closure = {"observations": [{"dataset_id": f"minos-chr18-{i:02d}"} for i in range(10)]}
    assert (
        observe_train_validation_disjointness(train_dataset_ids=train, closure_content=closure)
        is True
    )
    # an actual collision must be caught
    colliding = frozenset({*list(train)[:49], "minos-chr18-00"})
    assert (
        observe_train_validation_disjointness(train_dataset_ids=colliding, closure_content=closure)
        is False
    )


@pytest.mark.parametrize(
    ("train_n", "validation_n"),
    [pytest.param(49, 10, id="wrong-train-count"), pytest.param(50, 9, id="wrong-validation")],
)
def test_disjointness_refuses_a_wrong_sized_set(train_n: int, validation_n: int) -> None:
    train = frozenset(f"minos-train-{i:02d}" for i in range(train_n))
    closure = {
        "observations": [{"dataset_id": f"minos-chr18-{i:02d}"} for i in range(validation_n)]
    }
    with pytest.raises(BaselineQualificationObservationError):
        observe_train_validation_disjointness(train_dataset_ids=train, closure_content=closure)


def test_evidence_must_be_the_exact_accepted_six(tmp_path: Path) -> None:
    """§12 -- hashing whatever the caller named would let six unrelated files qualify."""
    from minos_engine.qualification.l2f2_baseline_qualified_qualifier import (
        ACCEPTED_EVIDENCE_SHA256,
    )

    with pytest.raises(BaselineQualificationObservationError, match="missing"):
        observe_evidence_hashes({"probe": tmp_path / "x.json"})

    complete = {name: tmp_path / f"{name}.json" for name in ACCEPTED_EVIDENCE_SHA256}
    for path in complete.values():
        path.write_bytes(b"{}")
    with pytest.raises(BaselineQualificationObservationError, match="hashes"):
        observe_evidence_hashes(complete)

    extra = {**complete, "surprise": tmp_path / "surprise.json"}
    with pytest.raises(BaselineQualificationObservationError, match="unexpected"):
        observe_evidence_hashes(extra)


def test_a_missing_evidence_artifact_is_refused(tmp_path: Path) -> None:
    from minos_engine.qualification.l2f2_baseline_qualified_qualifier import (
        ACCEPTED_EVIDENCE_SHA256,
    )

    paths = {name: tmp_path / f"{name}.json" for name in ACCEPTED_EVIDENCE_SHA256}
    for path in list(paths.values())[1:]:
        path.write_bytes(b"{}")
    with pytest.raises(BaselineQualificationObservationError, match="missing or a symlink"):
        observe_evidence_hashes(paths)


@pytest.mark.skipif(not _REAL_CLOSURE.is_file(), reason="real closure artifact not present")
def test_the_closure_observer_reads_the_result_from_the_artifact() -> None:
    observed = observe_closure_and_selected(_repo(), _REAL_CLOSURE)
    assert observed["selected_config_hash"] == (
        "157d88d1587c13be395c62d60e27d1becdada78fad45e65d883bc1190e51acea"
    )
    assert observed["selected_rank"] == 0
    assert observed["seed_rank"] == 3
    assert observed["observation_count"] == 40
    assert observed["all_candidates_complete"] is True
    assert observed["validation_infrastructure_incidents"] == 0
    assert observed["selected_statistics_verified"] is True
    assert observed["baseline_selected_hash"] == (
        "b13aef13fecf8e966184d03bad5ee0e6f096fb5649b30e336283e2f50f3eba38"
    )


@pytest.mark.skipif(not _REAL_CLOSURE.is_file(), reason="real closure artifact not present")
def test_a_tampered_closure_artifact_is_refused_by_the_observer(tmp_path: Path) -> None:
    from minos_engine.baseline.baseline_selected import BaselineSelectedError

    content = json.loads(_REAL_CLOSURE.read_text(encoding="utf-8"))
    content["selected_config_hash"] = "a" * 64
    forged = tmp_path / "forged.json"
    forged.write_text(json.dumps(content), encoding="utf-8")
    with pytest.raises(BaselineSelectedError):
        observe_closure_and_selected(_repo(), forged)


# --------------------------------------------------------------------------------------------
# §7/§8 — the TRAIN authority gap, reported rather than invented
# --------------------------------------------------------------------------------------------
def test_the_train_observer_requires_a_connection_and_never_guesses() -> None:
    """It observes through the authenticated surface, or it refuses. It never invents."""
    with pytest.raises(BaselineQualificationObservationError, match="does not guess one"):
        observe_train_evidence()


def test_the_train_observer_reads_no_experiments_table_itself() -> None:
    """§10 -- the evaluator-side observer must go through the function, never the raw ledger."""
    source = inspect.getsource(observe_train_evidence)
    for forbidden in (
        "experiments.l2f_experiment_plans",
        "experiments.l2f_experiment_jobs",
        "experiments.l2f_execution_results",
        "experiments.l2f_execution_failures",
    ):
        assert forbidden not in source, forbidden


def test_the_production_entry_cannot_complete_while_train_is_unobservable(
    tmp_path: Path,
) -> None:
    """The whole qualification fails closed on the gap; it does not mint a partial trust."""
    artifact = tmp_path / "closure.json"
    artifact.write_text("{}", encoding="utf-8")
    with pytest.raises(BaselineQualificationObservationError):
        run_baseline_qualified_qualification(
            root=_repo(), closure_artifact=artifact, evidence_paths={}
        )


def test_the_train_observer_never_reads_train_truth() -> None:
    """It cannot yet read anything; when it can, it must still not reach truth or write."""
    source = inspect.getsource(observe_train_evidence)
    for forbidden in (
        "truth_vcf",
        "mutations_vcf",
        "hap.py",
        "score(",
        "INSERT",
        "UPDATE",
        "DELETE",
    ):
        assert forbidden not in source, forbidden


def test_the_closure_authority_source_is_the_accepted_one() -> None:
    assert CLOSURE_AUTHORITY_SOURCE == "b61e2adfb3f871b4e0a1738ae12c1b9f0b7f9130"
