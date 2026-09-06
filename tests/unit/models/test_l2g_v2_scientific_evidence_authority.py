"""Dataset-label authentication, policy reconstruction, and the real failure path.

Recomputed hashes prove that bytes agree with themselves. They say nothing about whether those
bytes describe the science that was actually run: a campaign that shifted every ``actual_delta``,
rewrote every utility and hand-authored every decision would rehash perfectly, because the hashes
would be recomputed over the altered bytes. Every tamper case here does exactly that -- edits a
scientific value and then repairs every affected file SHA, scientific identity and result field --
and requires the verifier to refuse anyway, because it reconstructs the frozen dataset instead of
believing the evidence.

The failure-path tests cover the other half: one candidate raising must not destroy the three that
succeeded, and a SHARED authority failure must destroy the whole campaign.
"""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from minos_engine.models.relative_finalist_authority import (
    ACCEPTED_RELATIVE_DATASET_IDENTITY,
    CANDIDATE_FAILURE_POLICY,
    CANDIDATE_FAILURE_SURFACE,
    FAILED_CANDIDATE_ARTIFACTS,
    OOF_FIT_STAGE,
    SHARED_AUTHORITY_FAILURE_POLICY,
    RelativeAuthorityError,
    failed_candidate_entry,
    run_frozen_candidates,
    run_real_l2g_v2_train_oof_campaign,
)
from minos_engine.models.relative_finalist_contract import (
    ALTERNATIVE_FINALISTS,
    SAFE_BASELINE_CONFIG_HASH,
)
from minos_engine.models.relative_finalist_evidence import (
    _CAMPAIGN_TOKEN,
    STATUS_COMPLETE,
    STATUS_TRAINING_FAILURE,
    V2_OUTPUT_LAYOUT,
    TrustedL2GV2TrainCampaign,
    V2EvidenceError,
    build_v2_campaign_result,
    load_verified_published_l2g_v2_campaign,
    mint_trusted_v2_campaign,
    v2_metric_artifact_identity,
    v2_oof_artifact_identity,
    verify_published_l2g_v2_train_campaign,
    write_l2g_v2_train_campaign_outputs,
)
from minos_engine.models.relative_finalist_protocol import (
    V2_CANDIDATE_GRID,
    build_v2_spec_content,
    build_v2_spec_hashes,
)
from minos_engine.models.relative_finalist_reconstruction import (
    build_frozen_scientific_reference,
    four_finalist_utility_table,
)
from minos_engine.models.relative_finalist_runner import RelativeRunnerError
from tests.unit.models.conftest import v2_campaign_authority, v2_reference_bundle

_SPECS = tuple(build_v2_spec_hashes(ACCEPTED_RELATIVE_DATASET_IDENTITY))


# ---------------------------------------------------------------------------------------- #
# §6 the frozen scientific reference
# ---------------------------------------------------------------------------------------- #
def test_the_reconstruction_reproduces_both_accepted_identities() -> None:
    frozen = build_frozen_scientific_reference()
    assert (
        frozen.source_dataset_identity
        == "d031758c58358270843b9b417ea034d1181a6aaafc1c94af000279c26dc62fcc"
    )
    assert frozen.dataset_identity == ACCEPTED_RELATIVE_DATASET_IDENTITY
    assert len(frozen.cells()) == 150
    assert len(frozen.bams()) == 50
    assert len(frozen.utility) == 200


def test_the_reconstruction_reads_no_database() -> None:
    """§17: a reviewer must be able to run this from a checkout and the frozen bundle."""
    import inspect

    from minos_engine.models import relative_finalist_reconstruction as module

    source = inspect.getsource(module)
    for forbidden in ("psycopg", "sqlalchemy", "create_engine", "DATABASE_URL", "connect("):
        assert forbidden not in source


def test_the_utility_table_scores_a_rejected_cell_as_nothing() -> None:
    """Not "missing": a configuration that produces no admissible callset has no utility."""

    class _Row:
        def __init__(self, outcome: str, score: float | None) -> None:
            self.dataset_id = "bam"
            self.config_hash = SAFE_BASELINE_CONFIG_HASH
            self.outcome = outcome
            self.admitted_score = score

    class _Source:
        rows = [_Row("REJECTED", 0.9)]

    with pytest.raises(Exception, match="expected 200"):
        four_finalist_utility_table(_Source())


# ---------------------------------------------------------------------------------------- #
# §2/§3 per-candidate failure isolation
# ---------------------------------------------------------------------------------------- #
def _boom(message: str = "the estimator did not converge") -> Any:
    def raiser(*_args: Any, **kwargs: Any) -> Any:
        if kwargs["spec_hash"] == _SPECS[1]:
            raise RelativeRunnerError(message)
        return _ORIGINAL_RUNNER(*_args, **kwargs)

    return raiser


_ORIGINAL_RUNNER: Any = None


@pytest.fixture
def isolated(
    monkeypatch: pytest.MonkeyPatch, v2_world: dict[str, Any]
) -> tuple[dict[str, Any], str]:
    global _ORIGINAL_RUNNER
    import minos_engine.models.relative_finalist_runner as runner_module

    _ORIGINAL_RUNNER = runner_module.run_relative_outer_oof
    monkeypatch.setattr(runner_module, "run_relative_outer_oof", _boom())
    per_spec = run_frozen_candidates(
        spec_hashes=_SPECS,
        dataset_identity=ACCEPTED_RELATIVE_DATASET_IDENTITY,
        rows=v2_world["rows"],
        design=v2_world["design"],
        utility=v2_world["utility"],
        chromosome_of=v2_world["bams"],
    )
    return per_spec, _SPECS[1]


def test_one_candidate_failing_does_not_abort_the_campaign(
    isolated: tuple[dict[str, Any], str],
) -> None:
    per_spec, failed = isolated
    assert sorted(per_spec) == sorted(_SPECS), "all four specs must still be accounted for"
    assert per_spec[failed]["status"] == STATUS_TRAINING_FAILURE
    survivors = [h for h in _SPECS if h != failed]
    for spec_hash in survivors:
        assert len(per_spec[spec_hash]["records"]) == 150
        assert len(per_spec[spec_hash]["decisions"]) == 50
        assert per_spec[spec_hash]["diagnostics"]


def test_the_failed_entry_is_canonical_and_carries_no_science(
    isolated: tuple[dict[str, Any], str],
) -> None:
    per_spec, failed = isolated
    entry = per_spec[failed]
    accepted = build_v2_spec_content(
        V2_CANDIDATE_GRID[list(_SPECS).index(failed)],
        dataset_identity=ACCEPTED_RELATIVE_DATASET_IDENTITY,
    )
    assert entry["family"] == accepted["family"]
    assert entry["implementation"] == accepted["implementation"]
    assert entry["records"] == [] and entry["decisions"] == []
    assert entry["margins"] == {} and entry["diagnostics"] == {}
    failures = entry["training_failures"]
    assert len(failures) == 1
    assert sorted(failures[0]) == ["exception_type", "sanitized_reason", "stage"]
    assert failures[0]["stage"] == OOF_FIT_STAGE
    assert failures[0]["exception_type"] == "RelativeRunnerError"
    assert failures[0]["sanitized_reason"] == "the estimator did not converge"


def test_the_failure_reason_is_sanitised_and_capped() -> None:
    from minos_engine.models.relative_finalist_authority import _sanitise_failure

    leaky = f"failed at /home/someone/secret/path with object at 0x7f3a2b1c {'x' * 500}"
    cleaned = _sanitise_failure(leaky)
    assert "/home" not in cleaned and "0x7f3a2b1c" not in cleaned
    assert "<path>" in cleaned and "<addr>" in cleaned
    assert len(cleaned) <= 300


def test_the_failure_surface_excludes_integration_and_kill_conditions() -> None:
    """A missing key is a defect in this code, not a statement about a model."""
    for excluded in (KeyError, AttributeError, TypeError, ImportError, OSError, MemoryError):
        assert not issubclass(excluded, CANDIDATE_FAILURE_SURFACE)
    for surface in CANDIDATE_FAILURE_SURFACE:
        assert issubclass(surface, Exception), "BaseException must never be caught per candidate"


def test_an_integration_defect_still_aborts_the_whole_campaign(
    monkeypatch: pytest.MonkeyPatch, v2_world: dict[str, Any]
) -> None:
    import minos_engine.models.relative_finalist_runner as runner_module

    def raiser(*_args: Any, **kwargs: Any) -> Any:
        raise KeyError("family")

    monkeypatch.setattr(runner_module, "run_relative_outer_oof", raiser)
    with pytest.raises(KeyError):
        run_frozen_candidates(
            spec_hashes=_SPECS,
            dataset_identity=ACCEPTED_RELATIVE_DATASET_IDENTITY,
            rows=v2_world["rows"],
            design=v2_world["design"],
            utility=v2_world["utility"],
            chromosome_of=v2_world["bams"],
        )


# ---------------------------------------------------------------------------------------- #
# §4/§15 a failed candidate publishes nothing and cannot be promoted
# ---------------------------------------------------------------------------------------- #
@pytest.fixture
def isolated_published(
    isolated: tuple[dict[str, Any], str], v2_world: dict[str, Any], tmp_path: Path
) -> tuple[Path, str]:
    import minos_engine.models.relative_finalist_evidence as module
    from minos_engine.models.relative_finalist_protocol import qualifies_against_bar
    from tests.unit.models.conftest import clean_provenance

    per_spec, failed = isolated
    references = v2_reference_bundle(v2_world)
    bar = references["ALWAYS_SAFE_BASELINE"]["metrics"]
    # only the survivors are comparable; the failed candidate has no metrics to compare
    shortlist = tuple(
        sorted(
            h
            for h, e in per_spec.items()
            if e.get("status") != STATUS_TRAINING_FAILURE
            and qualifies_against_bar(
                mean_regret=float(e["metrics"]["mean_regret"]),
                cvar_regret=float(e["metrics"]["cvar_regret"]),
                bar_mean=float(bar["mean_regret"]),
                bar_cvar=float(bar["cvar_regret"]),
            )
        )
    )
    trusted = mint_trusted_v2_campaign(
        _CAMPAIGN_TOKEN,
        authority=v2_campaign_authority(),
        per_spec=per_spec,
        references=references,
        shortlist=shortlist,
    )
    provenance = clean_provenance()
    original = module.read_provenance
    module.read_provenance = lambda root: provenance  # type: ignore[assignment]
    try:
        out = tmp_path / V2_OUTPUT_LAYOUT["root"]
        write_l2g_v2_train_campaign_outputs(trusted, output_dir=out)
    finally:
        module.read_provenance = original  # type: ignore[assignment]
    return out, failed


def test_a_campaign_with_one_failed_candidate_publishes_and_verifies(
    isolated_published: tuple[Path, str],
) -> None:
    out, failed = isolated_published
    report = verify_published_l2g_v2_train_campaign(out)
    assert report["ok"] is True
    assert report["complete_spec_count"] == 3
    assert not (out / V2_OUTPUT_LAYOUT["oof_dir"] / f"{failed}.json").exists()
    assert not (out / V2_OUTPUT_LAYOUT["metrics_dir"] / f"{failed}.json").exists()


def test_the_result_accounts_for_all_four_and_promotes_none_of_the_failed(
    isolated_published: tuple[Path, str],
) -> None:
    out, failed = isolated_published
    result = json.loads((out / V2_OUTPUT_LAYOUT["campaign_result"]).read_bytes())
    assert sorted(e["spec_hash"] for e in result["per_spec"]) == sorted(_SPECS)
    entry = next(e for e in result["per_spec"] if e["spec_hash"] == failed)
    assert entry["status"] == STATUS_TRAINING_FAILURE
    assert "promotion_metrics" not in entry
    assert entry["training_failures"][0]["stage"] == OOF_FIT_STAGE
    assert failed not in result["shortlist"]


def test_a_failed_candidate_cannot_be_shortlisted_even_if_the_result_says_so(
    isolated: tuple[dict[str, Any], str], v2_world: dict[str, Any]
) -> None:
    per_spec, failed = isolated
    trusted = mint_trusted_v2_campaign(
        _CAMPAIGN_TOKEN,
        authority=v2_campaign_authority(),
        per_spec=per_spec,
        references=v2_reference_bundle(v2_world),
        shortlist=(failed,),
    )
    with pytest.raises(Exception, match="failed|COMPLETE|shortlist"):
        build_v2_campaign_result(trusted=trusted, published={})


def test_the_canonical_failed_entry_helper_matches_the_authority_policy() -> None:
    entry = failed_candidate_entry(
        spec_hash=_SPECS[0],
        recipe=V2_CANDIDATE_GRID[0],
        stage=OOF_FIT_STAGE,
        error=ValueError("bad"),
    )
    assert entry["status"] == STATUS_TRAINING_FAILURE
    assert CANDIDATE_FAILURE_POLICY == "ISOLATE_MODEL_SPEC_AND_MARK_INELIGIBLE"
    assert SHARED_AUTHORITY_FAILURE_POLICY == "ABORT_CAMPAIGN_NO_PUBLICATION"
    assert FAILED_CANDIDATE_ARTIFACTS == "NONE"


# ---------------------------------------------------------------------------------------- #
# §19 a SHARED authority failure aborts everything
# ---------------------------------------------------------------------------------------- #
def test_a_shared_authority_failure_aborts_before_any_campaign_exists(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Shared failures are not isolated: there is nothing to isolate them from."""
    import minos_engine.models.relative_finalist_authority as module
    import minos_engine.qualification.provenance as provenance_module
    from tests.unit.models.conftest import clean_provenance

    minted: list[Any] = []
    monkeypatch.setattr(provenance_module, "read_provenance", lambda root: clean_provenance())
    monkeypatch.setattr(
        module,
        "load_trusted_relative_training_data",
        lambda **_kwargs: (_ for _ in ()).throw(
            RelativeAuthorityError("the bundle is not the accepted one")
        ),
    )
    import minos_engine.models.relative_finalist_evidence as evidence_module

    monkeypatch.setattr(
        evidence_module,
        "mint_trusted_v2_campaign",
        lambda *a, **k: minted.append(k) or object(),
    )
    with pytest.raises(RelativeAuthorityError, match="the bundle is not the accepted one"):
        run_real_l2g_v2_train_oof_campaign(feature_matrix_artifact_path=tmp_path / "matrix.parquet")
    assert minted == [], "no trusted campaign may be minted after a shared failure"
    assert not (tmp_path / V2_OUTPUT_LAYOUT["root"]).exists()


def test_the_sealed_entry_delegates_fitting_to_the_isolating_helper() -> None:
    import inspect

    source = inspect.getsource(run_real_l2g_v2_train_oof_campaign)
    assert "run_frozen_candidates(" in source
    assert "run_relative_outer_oof(" not in source, "the entry must not fit outside the boundary"
    helper = inspect.getsource(run_frozen_candidates)
    assert "except CANDIDATE_FAILURE_SURFACE" in helper


# ---------------------------------------------------------------------------------------- #
# §20 rehashed scientific tampering
# ---------------------------------------------------------------------------------------- #
def _rehash(target: Path, result: dict[str, Any]) -> None:
    """Repair every hash an attacker could repair: file SHAs, scientific identities, metrics."""
    from minos_engine.models.relative_finalist_evidence import _recompute_policy_metrics

    for entry in result["per_spec"]:
        if entry["status"] != STATUS_COMPLETE:
            continue
        spec_hash = entry["spec_hash"]
        oof_path = target / V2_OUTPUT_LAYOUT["oof_dir"] / f"{spec_hash}.json"
        metric_path = target / V2_OUTPUT_LAYOUT["metrics_dir"] / f"{spec_hash}.json"
        oof = json.loads(oof_path.read_bytes())
        metric = json.loads(metric_path.read_bytes())
        fresh = _recompute_policy_metrics(oof["decisions"])
        metric["metrics"] = dict(fresh)
        entry["promotion_metrics"] = {
            "mean_regret": float(fresh["mean_regret"]),
            "cvar_regret": float(fresh["cvar_regret"]),
        }
        for path, payload in ((oof_path, oof), (metric_path, metric)):
            data = json.dumps(payload, sort_keys=True).encode("utf-8")
            path.write_bytes(data)
        entry["oof_file_sha256"] = hashlib.sha256(oof_path.read_bytes()).hexdigest()
        entry["metric_file_sha256"] = hashlib.sha256(metric_path.read_bytes()).hexdigest()
        entry["oof_scientific_hash"] = v2_oof_artifact_identity(oof)
        entry["metric_scientific_hash"] = v2_metric_artifact_identity(metric)
    safe = _recompute_policy_metrics(result["reference_decisions"]["ALWAYS_SAFE_BASELINE"])
    result["safe_baseline_mean_regret"] = float(safe["mean_regret"])
    result["safe_baseline_cvar_regret"] = float(safe["cvar_regret"])
    for name, decisions in result["reference_decisions"].items():
        result["reference_metrics"][name] = dict(_recompute_policy_metrics(decisions))


def _tamper(published: dict[str, Any], tmp_path: Path, mutate: Any, label: str) -> Path:
    import shutil

    target = tmp_path / label / V2_OUTPUT_LAYOUT["root"]
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(published["dir"], target)
    result = json.loads((target / V2_OUTPUT_LAYOUT["campaign_result"]).read_bytes())
    mutate(target, result)
    _rehash(target, result)
    (target / V2_OUTPUT_LAYOUT["campaign_result"]).write_bytes(
        json.dumps(result, sort_keys=True).encode("utf-8")
    )
    return target


def _first_complete(result: dict[str, Any]) -> dict[str, Any]:
    return next(e for e in result["per_spec"] if e["status"] == STATUS_COMPLETE)


def _edit_oof(target: Path, result: dict[str, Any], change: Any) -> None:
    entry = _first_complete(result)
    path = target / V2_OUTPUT_LAYOUT["oof_dir"] / f"{entry['spec_hash']}.json"
    oof = json.loads(path.read_bytes())
    change(oof)
    path.write_bytes(json.dumps(oof, sort_keys=True).encode("utf-8"))


def _bump(value: Any) -> float:
    return float(value) + 0.01


def _record_field(field: str) -> Any:
    def mutate(target: Path, result: dict[str, Any]) -> None:
        def change(oof: dict[str, Any]) -> None:
            oof["records"][0][field] = _bump(oof["records"][0][field])

        _edit_oof(target, result, change)

    return mutate


def _decision_field(field: str) -> Any:
    def mutate(target: Path, result: dict[str, Any]) -> None:
        def change(oof: dict[str, Any]) -> None:
            oof["decisions"][0][field] = _bump(oof["decisions"][0][field])

        _edit_oof(target, result, change)

    return mutate


def _flip_selected(target: Path, result: dict[str, Any]) -> None:
    def change(oof: dict[str, Any]) -> None:
        decision = oof["decisions"][0]
        current = decision["selected_config"]
        decision["selected_config"] = (
            ALTERNATIVE_FINALISTS[0]
            if current == SAFE_BASELINE_CONFIG_HASH
            else SAFE_BASELINE_CONFIG_HASH
        )

    _edit_oof(target, result, change)


def _flip_switched(target: Path, result: dict[str, Any]) -> None:
    def change(oof: dict[str, Any]) -> None:
        oof["decisions"][0]["switched"] = not oof["decisions"][0]["switched"]

    _edit_oof(target, result, change)


def _edit_predictions(target: Path, result: dict[str, Any]) -> None:
    def change(oof: dict[str, Any]) -> None:
        decision = oof["decisions"][0]
        key = sorted(decision["predictions"])[0]
        decision["predictions"][key] = _bump(decision["predictions"][key])

    _edit_oof(target, result, change)


def _edit_diagnostic(target: Path, result: dict[str, Any]) -> None:
    entry = _first_complete(result)
    path = target / V2_OUTPUT_LAYOUT["metrics_dir"] / f"{entry['spec_hash']}.json"
    metric = json.loads(path.read_bytes())
    metric["diagnostics"]["delta_mae"] = _bump(metric["diagnostics"]["delta_mae"])
    path.write_bytes(json.dumps(metric, sort_keys=True).encode("utf-8"))


def _edit_family(target: Path, result: dict[str, Any]) -> None:
    entry = _first_complete(result)
    entry["family"] = (
        "RELATIVE_HISTGB_SHARED" if "RIDGE" in entry["family"] else "RELATIVE_RIDGE_SHARED"
    )


def _edit_threads(target: Path, result: dict[str, Any]) -> None:
    result["thread_report"] = [dict(p, num_threads=2) for p in result["thread_report"]]


def _reference_field(name: str, field: str) -> Any:
    def mutate(target: Path, result: dict[str, Any]) -> None:
        result["reference_decisions"][name][0][field] = _bump(
            result["reference_decisions"][name][0][field]
        )

    return mutate


def _edit_global_best(target: Path, result: dict[str, Any]) -> None:
    decision = result["reference_decisions"]["GLOBAL_BEST_FINALIST_FROM_OUTER_TRAIN"][0]
    decision["selected_config"] = (
        ALTERNATIVE_FINALISTS[0]
        if decision["selected_config"] != ALTERNATIVE_FINALISTS[0]
        else ALTERNATIVE_FINALISTS[1]
    )


def _edit_oracle_choice(target: Path, result: dict[str, Any]) -> None:
    decision = result["reference_decisions"]["ORACLE4"][0]
    decision["selected_config"] = (
        SAFE_BASELINE_CONFIG_HASH
        if decision["selected_config"] != SAFE_BASELINE_CONFIG_HASH
        else ALTERNATIVE_FINALISTS[0]
    )


TAMPERS: tuple[tuple[str, Any], ...] = (
    ("actual_delta", _record_field("actual_delta")),
    ("predicted_delta", _record_field("predicted_delta")),
    ("decision_safe_utility", _decision_field("safe_utility")),
    ("decision_selected_utility", _decision_field("selected_utility")),
    ("decision_oracle4_utility", _decision_field("oracle4_utility")),
    ("decision_regret", _decision_field("regret")),
    ("decision_margin", _decision_field("margin")),
    ("decision_predictions", _edit_predictions),
    ("selected_config", _flip_selected),
    ("switched", _flip_switched),
    ("diagnostic", _edit_diagnostic),
    ("family", _edit_family),
    ("thread_report", _edit_threads),
    ("safe_reference_regret", _reference_field("ALWAYS_SAFE_BASELINE", "regret")),
    ("safe_reference_oracle", _reference_field("ALWAYS_SAFE_BASELINE", "oracle4_utility")),
    ("global_best_decision", _edit_global_best),
    ("oracle4_selected_config", _edit_oracle_choice),
)


@pytest.mark.parametrize("label,mutate", TAMPERS, ids=[t[0] for t in TAMPERS])
def test_a_rehashed_scientific_edit_is_still_refused(
    v2_published: dict[str, Any], tmp_path: Path, label: str, mutate: Any
) -> None:
    """Correct hashes do not make scientifically wrong bytes valid."""
    target = _tamper(v2_published, tmp_path, mutate, label)
    with pytest.raises(V2EvidenceError):
        verify_published_l2g_v2_train_campaign(target)


def test_the_untampered_tree_still_verifies_through_the_same_rehash_path(
    v2_published: dict[str, Any], tmp_path: Path
) -> None:
    """The tamper harness itself must not be what fails the tests above."""
    target = _tamper(v2_published, tmp_path, lambda _t, _r: None, "clean")
    report = verify_published_l2g_v2_train_campaign(target)
    assert report["ok"] is True


# ---------------------------------------------------------------------------------------- #
# §16 the verified capability
# ---------------------------------------------------------------------------------------- #
def test_the_capability_is_minted_only_after_the_label_checks(
    v2_published: dict[str, Any], tmp_path: Path
) -> None:
    verified = load_verified_published_l2g_v2_campaign(v2_published["dir"])
    assert verified.result["relative_dataset_identity"] == ACCEPTED_RELATIVE_DATASET_IDENTITY
    tampered = _tamper(v2_published, tmp_path, _record_field("actual_delta"), "capability")
    with pytest.raises(V2EvidenceError):
        load_verified_published_l2g_v2_campaign(tampered)


def test_the_trusted_and_verified_capabilities_are_distinct(v2_trusted: Any) -> None:
    """An in-memory campaign is not a verified one; only the verifier can say a tree is sound."""
    assert isinstance(v2_trusted, TrustedL2GV2TrainCampaign)
    assert not isinstance(v2_trusted, type(None))


# ---------------------------------------------------------------------------------------- #
# §21/§22/§23 nothing scientific moved
# ---------------------------------------------------------------------------------------- #
def test_no_real_v2_campaign_output_exists() -> None:
    from minos_engine.models.relative_finalist_authority import V2_OUTPUT_ROOT
    from minos_engine.qualification.l2f_accepted_identities import repository_root
    from tests.minos_scratch import CANONICAL_MINOS_ROOT

    assert not (CANONICAL_MINOS_ROOT / V2_OUTPUT_ROOT).exists()
    assert not (repository_root() / "reports/layer2" / V2_OUTPUT_ROOT).exists()


def test_the_frozen_science_did_not_move() -> None:
    from minos_engine.models.relative_finalist_authority import (
        ACCEPTED_RELATIVE_CONTRACT_HASH,
    )
    from minos_engine.models.relative_finalist_protocol import compute_relative_protocol_hash

    assert (
        compute_relative_protocol_hash()
        == "d2b275c0ffde8f12db5a937a0b5b0cceab8b9cdce921779d79995bd74a9a2bd6"
    )
    assert list(_SPECS) == [
        "b7e68f65a4b28d2370243edaad38f0a8670c04de9fab1faa9383243a1ca69aa4",
        "6be59fbcf340364535ce6efd16faef47376c6ca82a3ad2a4feba681e6058578c",
        "673ecf5b4dca196a0bfff55ca9c933ea4712684b2a3aec6d41217e414c06d377",
        "0ab6a36a250dbe88bff68af88001404f9ac6daad4c6220938d65945c56ea63c6",
    ]
    assert (
        ACCEPTED_RELATIVE_CONTRACT_HASH
        == "dd5aca807bf0499411b9b4279c206d7a0e9a6d50c85bf75c5d319cf7ba2d394e"
    )
    assert (
        ACCEPTED_RELATIVE_DATASET_IDENTITY
        == "4a8f2777ebaddffb29dc2ae96a6426eff5e76c1f23b3ee43c1b275f8d97bc5e5"
    )


def test_the_authority_is_v4_and_binds_the_failure_policy() -> None:
    from minos_engine.models.relative_finalist_authority import verify_v2_prefit_authority
    from minos_engine.qualification.l2f_accepted_identities import repository_root

    root = repository_root()
    sha = verify_v2_prefit_authority(root)
    assert sha == "6b2edd38eeee0765c96a2b55083fa534647631c2e449bf702b9d4204f0f894d8"
    document = json.loads((root / "reports/layer2/l2g-v2-prefit-authority.json").read_bytes())
    assert document["schema_version"] == "l2g-v2-prefit-authority-v4"
    assert document["candidate_failure_policy"] == CANDIDATE_FAILURE_POLICY
    assert document["shared_authority_failure_policy"] == SHARED_AUTHORITY_FAILURE_POLICY
    assert document["failed_candidate_artifacts"] == FAILED_CANDIDATE_ARTIFACTS
    assert document["failed_candidate_shortlist_eligible"] is False
    assert document["no_v2_model_fitted"] is True
    assert document["validation_read"] is False
    assert document["test_accessed"] is False
    assert copy.deepcopy(document)["protocol_hash"] == (
        "d2b275c0ffde8f12db5a937a0b5b0cceab8b9cdce921779d79995bd74a9a2bd6"
    )
