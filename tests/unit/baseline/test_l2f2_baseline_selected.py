"""The frozen Phase-D result, and the gate machinery that will later publish it.

Nothing here needs a database, GATK, a scorer or truth — which is the point: an auditor must be
able to disprove this on a laptop. The real closure artifact is used where it exists, and every
tamper case is synthetic.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from minos_engine.baseline.baseline_selected import (
    BASELINE_SELECTED_DOMAIN,
    BASELINE_SELECTED_MANIFEST,
    ORDERED_RANKING,
    PHASE_D_CLOSURE_HASH,
    SEED_CONFIG_HASH,
    SEED_RANK,
    SELECTED_CONFIG_HASH,
    BaselineSelectedError,
    baseline_selected_content,
    compute_baseline_selected_hash,
    load_committed_baseline_selected,
    verify_closure_artifact,
)
from minos_engine.gates.required_checks import required_checks_for
from minos_engine.qualification.l2f2_baseline_qualified_runner import (
    BASELINE_QUALIFIED_GATE,
    BaselineQualifiedObservation,
    derive_checks,
    verify_baseline_qualified_gate,
)
from minos_engine.qualification.l2f2_train_evidence import (
    TRAIN_CANDIDATE_FAILURE_COUNT,
    TRAIN_EVALUATION_COUNT,
    TRAIN_EVALUATION_SET_SHA256,
    TRAIN_EXECUTION_FAILURE_SET_SHA256,
    TRAIN_LOGICAL_JOB_COUNT,
    TRAIN_PLAN_HASHES,
    verify_train_evidence,
)
from tests.minos_scratch import CANONICAL_MINOS_ROOT

_PROTOCOL = "c548e190571f5e964560cf30021a520ea8aad6674569fa3202af880d7dff77d1"
_INTERPRETATION = "4c169912f67877d6ba254fb280dbd2ff44aa4aaaf65bedfa1bca9975f1efebbd"
#: the operator's real closure artifact, DISCOVERED rather than assumed. Every test that uses it
#: is skipped when it is absent, so this suite stays portable to a machine that has no MINOS root.
_REAL_CLOSURE = (
    CANONICAL_MINOS_ROOT / "minos_l2f2_validation" / "phase_d_real_closure_20260903T094127Z.json"
)


def _repo() -> Path:
    return Path(__file__).resolve().parents[3]


def _train_observed(**overrides: Any) -> dict[str, Any]:
    observed = {
        "revision": "0020_l2f2_phase_c_execution",
        "plan_hashes": list(TRAIN_PLAN_HASHES),
        "logical_job_count": TRAIN_LOGICAL_JOB_COUNT,
        "terminal_job_count": TRAIN_LOGICAL_JOB_COUNT,
        "nonterminal_job_count": 0,
        "succeeded_without_evaluation": 0,
        "evaluation_count": TRAIN_EVALUATION_COUNT,
        "evaluation_failure_count": 0,
        "evaluation_set_sha256": TRAIN_EVALUATION_SET_SHA256,
        "execution_failure_set_sha256": TRAIN_EXECUTION_FAILURE_SET_SHA256,
        "execution_failure_codes": {"GATK_NONZERO_EXIT": TRAIN_CANDIDATE_FAILURE_COUNT},
        "distinct_scoring_contracts": 1,
        "scoring_contract_hash": "b24a07e208ce8e2fff6672102ae4e61aed93c6f352a5af46ba81c4789adb76d6",
        "distinct_execution_environments": 1,
        "execution_environment_hash": "71e14a49833ac77bb9dc576345fb89c4dd68f4a3ad3673eb098d38593c1ef4d3",
    }
    observed.update(overrides)
    return observed


def _observation(**overrides: Any) -> BaselineQualifiedObservation:
    fields: dict[str, Any] = {
        "qualified_source_commit": "b" * 40,
        "qualified_source_tree": "t" * 40,
        "worktree_commit": "b" * 40,
        "worktree_tree": "t" * 40,
        "worktree_clean": True,
        "harness_ready_gate_hash_prefix": "0e8411eb1234",
        "harness_ready_qualification_hash_prefix": "b1d1cc5d1234",
        "closure_artifact_verified": True,
        "closure_hash_recomputed": PHASE_D_CLOSURE_HASH,
        "baseline_selected_hash": compute_baseline_selected_hash(),
        "baseline_selected_manifest_verified": True,
        "candidate_count": 4,
        "member_count": 10,
        "observation_count": 40,
        "all_candidates_complete": True,
        "validation_infrastructure_incidents": 0,
        "selected_config_hash": SELECTED_CONFIG_HASH,
        "selected_rank": 0,
        "selected_inherited_candidate_index": 42,
        "selected_statistics_agree": True,
        "seed_config_hash": SEED_CONFIG_HASH,
        "seed_rank": SEED_RANK,
        "scorer_source_identities_exact": True,
        "happy_digest": "genonet/hap-py@sha256:" + "0" * 64,
        "bcftools_digest": "quay.io/biocontainers/bcftools@sha256:" + "1" * 64,
        "train": verify_train_evidence(_train_observed()),
        "test_untouched": True,
        "train_and_validation_identities_disjoint": True,
        "evidence_hashes": {
            "scoring_contract_hash": "b24a07e208ce8e2fff6672102ae4e61aed93c6f352a5af46ba81c4789adb76d6",
            "minos_subnet_sha": "649bb92c6abccebde58a736a2b2af7fd77a701c1",
            "execution_environment_hash": "71e14a49833ac77bb9dc576345fb89c4dd68f4a3ad3673eb098d38593c1ef4d3",
            "phase_d_activation_evidence": "e58fa267",
            "phase_d_execution_evidence": "1ebc6aea",
            "phase_d_sentinel_evidence": "db8ebc43",
            "phase_d_complete_matrix_evidence": "35431e54",
            "phase_d_closure_artifact": "4eaf622b",
            "phase_d_closure_evidence": "90f0f535",
        },
    }
    fields.update(overrides)
    return BaselineQualifiedObservation(**fields)


# --------------------------------------------------------------------------------------------
# the frozen result
# --------------------------------------------------------------------------------------------
def test_the_freeze_records_the_real_outcome() -> None:
    content = baseline_selected_content()
    assert content["selected_config_hash"] == SELECTED_CONFIG_HASH
    assert content["selected_rank"] == 0
    assert content["seed_rank"] == 3
    assert content["ordered_ranking"][0] == SELECTED_CONFIG_HASH
    assert content["ordered_ranking"][-1] == SEED_CONFIG_HASH
    assert content["phase_d_closure_hash"] == PHASE_D_CLOSURE_HASH


def test_the_selected_baseline_is_not_the_seed() -> None:
    """Recorded, per section 12 rule 5 -- not a reason to re-optimise anything."""
    assert SELECTED_CONFIG_HASH != SEED_CONFIG_HASH
    assert SEED_RANK == 3


def test_the_freeze_carries_no_operational_metadata() -> None:
    blob = json.dumps(baseline_selected_content(), sort_keys=True).lower()
    for leaked in ("timestamp", "hostname", "/home/", "postgres", "minos_l2f2", "operator", "utc"):
        assert leaked not in blob, leaked


def test_the_hash_is_domain_separated_and_stable() -> None:
    from minos_engine.common.canonical_json import canonical_json_bytes
    from minos_engine.common.hashing import sha256_hex

    assert compute_baseline_selected_hash() == sha256_hex(
        BASELINE_SELECTED_DOMAIN.encode("utf-8") + canonical_json_bytes(baseline_selected_content())
    )
    assert BASELINE_SELECTED_DOMAIN not in (
        "minos:l2f2-baseline-search-protocol:v1\n",
        "minos:l2f2-phase-d-validation-closure:v1\n",
    )
    assert compute_baseline_selected_hash() == compute_baseline_selected_hash()


@pytest.mark.parametrize(
    "field",
    [
        "selected_config_hash",
        "selected_rank",
        "selected_objective",
        "selected_cvar",
        "selected_floor",
        "selected_mean",
        "selected_mean_gatk_runtime_ms",
        "selected_candidate_failure_count",
        "phase_d_closure_hash",
        "baseline_protocol_hash",
        "selection_interpretation_hash",
        "phase_d_plan_hash",
        "scoring_contract_hash",
        "execution_environment_hash",
        "ordered_ranking",
        "seed_rank",
    ],
)
def test_changing_any_frozen_field_changes_the_hash(field: str, monkeypatch: Any) -> None:
    from minos_engine.baseline import baseline_selected as mod

    original, baseline = mod.baseline_selected_content, compute_baseline_selected_hash()

    def perturbed() -> dict[str, Any]:
        content = original()
        value = content[field]
        content[field] = (
            "PERTURBED"
            if isinstance(value, str)
            else ["x"]
            if isinstance(value, list)
            else value + 1
        )
        return content

    monkeypatch.setattr(mod, "baseline_selected_content", perturbed)
    assert mod.compute_baseline_selected_hash() != baseline


def test_the_committed_manifest_matches_the_code() -> None:
    document = load_committed_baseline_selected(_repo())
    assert document["baseline_selected_hash"] == compute_baseline_selected_hash()


def test_a_forged_manifest_fails_closed(tmp_path: Path) -> None:
    target = tmp_path / BASELINE_SELECTED_MANIFEST
    target.parent.mkdir(parents=True, exist_ok=True)
    document = json.loads((_repo() / BASELINE_SELECTED_MANIFEST).read_text(encoding="utf-8"))
    document["content"]["selected_config_hash"] = SEED_CONFIG_HASH
    target.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(BaselineSelectedError, match="hash|differs"):
        load_committed_baseline_selected(tmp_path)


# --------------------------------------------------------------------------------------------
# the freeze is verified against the closure, never trusted
# --------------------------------------------------------------------------------------------
@pytest.mark.skipif(not _REAL_CLOSURE.is_file(), reason="real closure artifact not present")
def test_the_freeze_is_verified_against_the_real_closure_artifact() -> None:
    content = verify_closure_artifact(_REAL_CLOSURE)
    assert content["selected_config_hash"] == SELECTED_CONFIG_HASH
    assert content["observation_count"] == 40


@pytest.mark.skipif(not _REAL_CLOSURE.is_file(), reason="real closure artifact not present")
@pytest.mark.parametrize(
    ("field", "value"),
    [
        pytest.param("selected_config_hash", SEED_CONFIG_HASH, id="winner"),
        pytest.param("seed_rank", 0, id="seed-rank"),
        pytest.param("phase_d_plan_hash", "a" * 64, id="plan"),
        pytest.param("baseline_protocol_hash", "a" * 64, id="protocol"),
        pytest.param("selection_interpretation_hash", "a" * 64, id="interpretation"),
        pytest.param("scoring_contract_hash", "a" * 64, id="contract"),
        pytest.param("execution_environment_hash", "a" * 64, id="environment"),
        pytest.param("observation_count", 39, id="observations"),
        pytest.param("ordered_ranking", [SEED_CONFIG_HASH, *ORDERED_RANKING[:3]], id="ranking"),
    ],
)
def test_a_tampered_closure_artifact_is_refused(tmp_path: Path, field: str, value: Any) -> None:
    content = json.loads(_REAL_CLOSURE.read_text(encoding="utf-8"))
    content[field] = value
    forged = tmp_path / "forged_closure.json"
    forged.write_text(json.dumps(content), encoding="utf-8")
    with pytest.raises(BaselineSelectedError):
        verify_closure_artifact(forged)


def test_a_missing_closure_artifact_is_refused(tmp_path: Path) -> None:
    with pytest.raises(BaselineSelectedError, match="missing or a symlink"):
        verify_closure_artifact(tmp_path / "absent.json")


# --------------------------------------------------------------------------------------------
# TRAIN evidence completeness
# --------------------------------------------------------------------------------------------
def test_the_real_train_evidence_summary_passes_every_check() -> None:
    assert all(verify_train_evidence(_train_observed()).values())


@pytest.mark.parametrize(
    ("override", "failing"),
    [
        pytest.param(
            {"evaluation_count": TRAIN_EVALUATION_COUNT - 1},
            "train_every_success_evaluated",
            id="missing-evaluation",
        ),
        pytest.param(
            {"evaluation_count": TRAIN_EVALUATION_COUNT + 1},
            "train_every_success_evaluated",
            id="extra-evaluation",
        ),
        pytest.param(
            {"succeeded_without_evaluation": 1},
            "train_every_success_evaluated",
            id="unevaluated-success",
        ),
        pytest.param(
            {"nonterminal_job_count": 3}, "train_every_logical_job_terminal", id="nonterminal-job"
        ),
        pytest.param(
            {"evaluation_set_sha256": "a" * 64},
            "train_evaluation_set_exact",
            id="wrong-evaluation-set",
        ),
        pytest.param(
            {"execution_failure_set_sha256": "a" * 64},
            "train_execution_failure_set_exact",
            id="wrong-failure-set",
        ),
        pytest.param(
            {"evaluation_failure_count": 1},
            "train_no_infrastructure_incident",
            id="evaluation-failure",
        ),
        pytest.param(
            {"execution_failure_codes": {"HAPPY_TIMEOUT": 35}},
            "train_no_infrastructure_incident",
            id="infrastructure-incident",
        ),
        pytest.param(
            {"execution_failure_codes": {"MADE_UP": 35}},
            "train_failure_codes_bounded",
            id="unknown-code",
        ),
        pytest.param(
            {"distinct_scoring_contracts": 2}, "train_single_scoring_contract", id="two-contracts"
        ),
        pytest.param(
            {"distinct_execution_environments": 2},
            "train_single_execution_environment",
            id="two-environments",
        ),
        pytest.param({"plan_hashes": ["a" * 64]}, "train_plans_exact", id="wrong-plans"),
    ],
)
def test_a_train_evidence_defect_fails_its_check(override: dict, failing: str) -> None:
    checks = verify_train_evidence(_train_observed(**override))
    assert checks[failing] is False, checks


def test_a_candidate_failure_is_represented_not_dropped() -> None:
    """35 GATK_NONZERO_EXIT are the candidate's own outcomes and must be accounted for."""
    checks = verify_train_evidence(_train_observed())
    assert checks["train_no_failed_evaluation_silently_ignored"] is True
    # drop them and the accounting no longer reaches the logical job count
    checks = verify_train_evidence(_train_observed(execution_failure_codes={}))
    assert checks["train_no_failed_evaluation_silently_ignored"] is False


# --------------------------------------------------------------------------------------------
# the gate contract
# --------------------------------------------------------------------------------------------
def test_the_gate_is_registered_with_its_full_check_set() -> None:
    required = required_checks_for(BASELINE_QUALIFIED_GATE)
    assert len(required) == 42
    for expected in (
        "qualified_source_present",
        "train_evaluation_set_exact",
        "validation_closure_hash_recomputed",
        "selected_config_is_closure_rank_zero",
        "no_seed_override",
        "test_untouched",
        "closure_reproducible_from_committed_identities",
    ):
        assert expected in required


def test_a_clean_observation_satisfies_every_required_check() -> None:
    checks = derive_checks(_observation())
    required = required_checks_for(BASELINE_QUALIFIED_GATE)
    assert set(checks) == required, set(checks) ^ required
    assert all(checks.values()), sorted(n for n, ok in checks.items() if not ok)


@pytest.mark.parametrize(
    ("override", "failing"),
    [
        pytest.param({"worktree_clean": False}, "worktree_matches_qualified_source", id="dirty"),
        pytest.param({"worktree_tree": "z" * 40}, "qualified_source_tree_matches", id="tree"),
        pytest.param(
            {"worktree_commit": "z" * 40}, "worktree_matches_qualified_source", id="commit"
        ),
        pytest.param(
            {"closure_hash_recomputed": "a" * 64},
            "validation_closure_hash_recomputed",
            id="closure-hash",
        ),
        pytest.param(
            {"selected_config_hash": SEED_CONFIG_HASH},
            "selected_config_is_closure_rank_zero",
            id="wrong-winner",
        ),
        pytest.param({"selected_rank": 1}, "selected_config_is_closure_rank_zero", id="rank"),
        pytest.param(
            {"selected_inherited_candidate_index": 0},
            "selected_inherited_index_exact",
            id="inherited-index",
        ),
        pytest.param(
            {"selected_statistics_agree": False},
            "selected_statistics_agree_with_closure",
            id="statistics",
        ),
        pytest.param(
            {"observation_count": 39},
            "validation_forty_terminal_outcomes",
            id="missing-observation",
        ),
        pytest.param(
            {"validation_infrastructure_incidents": 1},
            "validation_no_infrastructure_incident",
            id="incident",
        ),
        pytest.param(
            {"all_candidates_complete": False},
            "validation_all_candidates_complete",
            id="incomplete",
        ),
        pytest.param({"test_untouched": False}, "test_untouched", id="test-contamination"),
        pytest.param(
            {"train_and_validation_identities_disjoint": False},
            "train_and_validation_identities_not_mixed",
            id="mixed",
        ),
        pytest.param(
            {"happy_digest": "genonet/hap-py:latest"},
            "happy_immutable_digest_exact",
            id="happy-tag",
        ),
        pytest.param(
            {"bcftools_digest": "bcftools:1.20"},
            "bcftools_immutable_digest_exact",
            id="bcftools-tag",
        ),
        pytest.param(
            {"baseline_selected_manifest_verified": False},
            "baseline_selected_authority_bound",
            id="freeze",
        ),
        pytest.param(
            {"harness_ready_gate_hash_prefix": "deadbeef12"},
            "harness_ready_gate_bound",
            id="harness-gate",
        ),
    ],
)
def test_a_defective_observation_fails_its_check(override: dict, failing: str) -> None:
    checks = derive_checks(_observation(**override))
    assert checks[failing] is False, checks


def test_a_seed_override_would_be_caught(monkeypatch: Any) -> None:
    """If some future change selected the seed despite a non-zero rank, the gate says so."""
    checks = derive_checks(_observation(selected_config_hash=SEED_CONFIG_HASH, selected_rank=3))
    assert checks["no_seed_override"] is False
    assert checks["selected_config_is_closure_rank_zero"] is False


def test_the_check_set_is_deterministic() -> None:
    assert derive_checks(_observation()) == derive_checks(_observation())


# --------------------------------------------------------------------------------------------
# the offline verifier
# --------------------------------------------------------------------------------------------
def _write_gate(tmp_path: Path, **overrides: Any) -> tuple[Path, Path]:
    checks = derive_checks(_observation())
    qualification = {
        "gate_name": BASELINE_QUALIFIED_GATE,
        "qualified_source_commit": "b" * 40,
        "qualified_source_tree": "t" * 40,
        "checks": checks,
    }
    gate = {
        "gate_name": BASELINE_QUALIFIED_GATE,
        "status": "PASS",
        "qualified_source_commit": "b" * 40,
        "qualified_source_tree": "t" * 40,
        "baseline_selected_hash": compute_baseline_selected_hash(),
    }
    gate.update(overrides.pop("gate", {}))
    qualification.update(overrides.pop("qualification", {}))
    gate_path = tmp_path / "gate.json"
    qual_path = tmp_path / "qualification.json"
    gate_path.write_text(json.dumps(gate), encoding="utf-8")
    qual_path.write_text(json.dumps(qualification), encoding="utf-8")
    return gate_path, qual_path


def test_a_complete_gate_verifies_offline(tmp_path: Path) -> None:
    gate_path, qual_path = _write_gate(tmp_path)
    result = verify_baseline_qualified_gate(
        gate_path=gate_path, qualification_path=qual_path, root=_repo()
    )
    assert result["ok"] is True, result["reasons"]
    assert result["required_check_count"] == 42


def test_a_gate_missing_one_mandatory_check_fails(tmp_path: Path) -> None:
    checks = derive_checks(_observation())
    checks.pop("test_untouched")
    gate_path, qual_path = _write_gate(tmp_path, qualification={"checks": checks})
    result = verify_baseline_qualified_gate(
        gate_path=gate_path, qualification_path=qual_path, root=_repo()
    )
    assert result["ok"] is False
    assert any("missing mandatory checks" in r for r in result["reasons"])


def test_a_gate_reporting_a_false_mandatory_check_fails(tmp_path: Path) -> None:
    checks = derive_checks(_observation())
    checks["no_seed_override"] = False
    gate_path, qual_path = _write_gate(tmp_path, qualification={"checks": checks})
    result = verify_baseline_qualified_gate(
        gate_path=gate_path, qualification_path=qual_path, root=_repo()
    )
    assert result["ok"] is False
    assert any("reported false" in r for r in result["reasons"])


def test_a_gate_with_an_unregistered_check_fails(tmp_path: Path) -> None:
    checks = derive_checks(_observation())
    checks["looks_good_to_me"] = True
    gate_path, qual_path = _write_gate(tmp_path, qualification={"checks": checks})
    result = verify_baseline_qualified_gate(
        gate_path=gate_path, qualification_path=qual_path, root=_repo()
    )
    assert result["ok"] is False
    assert any("unregistered" in r for r in result["reasons"])


def test_a_gate_naming_a_different_source_than_the_qualification_fails(tmp_path: Path) -> None:
    gate_path, qual_path = _write_gate(tmp_path, gate={"qualified_source_commit": "z" * 40})
    result = verify_baseline_qualified_gate(
        gate_path=gate_path, qualification_path=qual_path, root=_repo()
    )
    assert result["ok"] is False
    assert any("qualified_source_commit" in r for r in result["reasons"])


def test_a_gate_with_a_forged_baseline_selected_hash_fails(tmp_path: Path) -> None:
    gate_path, qual_path = _write_gate(tmp_path, gate={"baseline_selected_hash": "a" * 64})
    result = verify_baseline_qualified_gate(
        gate_path=gate_path, qualification_path=qual_path, root=_repo()
    )
    assert result["ok"] is False
    assert any("baseline-selected" in r for r in result["reasons"])


def test_a_non_pass_gate_fails(tmp_path: Path) -> None:
    gate_path, qual_path = _write_gate(tmp_path, gate={"status": "FAIL"})
    result = verify_baseline_qualified_gate(
        gate_path=gate_path, qualification_path=qual_path, root=_repo()
    )
    assert result["ok"] is False


def test_a_missing_artifact_fails_closed(tmp_path: Path) -> None:
    gate_path, qual_path = _write_gate(tmp_path)
    result = verify_baseline_qualified_gate(
        gate_path=tmp_path / "absent.json", qualification_path=qual_path, root=_repo()
    )
    assert result["ok"] is False
    assert any("missing or a symlink" in r for r in result["reasons"])


def test_verification_needs_no_database_gatk_or_truth() -> None:
    """Asserted on the CODE. The docstring legitimately names what it refuses to touch."""
    import ast
    import inspect

    from minos_engine.qualification import l2f2_baseline_qualified_runner as mod

    tree = ast.parse(inspect.getsource(mod.verify_baseline_qualified_gate))
    body = ast.unparse(
        ast.Module(
            body=[
                node
                for node in ast.walk(tree)
                if isinstance(node, ast.stmt) and not isinstance(node, ast.Expr | ast.FunctionDef)
            ],
            type_ignores=[],
        )
    ).lower()
    for forbidden in (
        "create_engine",
        "create_db_engine",
        "psycopg",
        "subprocess",
        "gatk",
        "hap.py",
        "truth",
        "sqlalchemy",
    ):
        assert forbidden not in body, forbidden


# --------------------------------------------------------------------------------------------
# no PASS gate has been issued yet
# --------------------------------------------------------------------------------------------
def test_no_baseline_qualified_gate_artifact_exists_yet() -> None:
    """Section 19: this is source machinery only. The evidence commit comes later."""
    gates = _repo() / "gates"
    assert not list(gates.glob("*BASELINE-QUALIFIED*")), "a PASS gate was published too early"
    assert not list(gates.glob("*baseline-qualified*"))
