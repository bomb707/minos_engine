"""BASELINE-QUALIFIED as a real GateArtifact: assembly, trust boundary, offline verification.

The interesting tests are the ones that try to mint PASS without earning it — a caller-built
observation, a stored ``checks`` dictionary, a prefix that merely looks like the HARNESS hash, or
a gate whose Git provenance is a plausible 40-character string.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from minos_engine.baseline.baseline_selected import compute_baseline_selected_hash
from minos_engine.gates.contracts import GateStatus
from minos_engine.gates.required_checks import required_checks_for
from minos_engine.gates.verifier import write_gate
from minos_engine.qualification.l2f2_baseline_qualified_contract import (
    ACCEPTED_BCFTOOLS_DIGEST,
    ACCEPTED_HAPPY_DIGEST,
    BASELINE_QUALIFICATION_DOMAIN,
    BASELINE_QUALIFICATION_SCHEMA,
    BASELINE_QUALIFICATION_TOOL_VERSION,
    HARNESS_READY_GATE_HASH,
    HARNESS_READY_QUALIFICATION_HASH,
    BaselineQualificationResult,
    TrainEvidenceSummary,
    candidate_design_identity,
    compute_baseline_qualification_hash,
    objective_identity,
)
from minos_engine.qualification.l2f2_baseline_qualified_qualifier import (
    BaselineQualificationObservationError,
    TrustedBaselineQualification,
)
from minos_engine.qualification.l2f2_baseline_qualified_runner import (
    BASELINE_QUALIFIED_GATE,
    BaselineQualificationError,
    assemble_baseline_qualified_gate,
    derive_checks,
    observation_from_result,
    verify_baseline_qualified_gate,
    write_baseline_qualification_outputs,
)
from minos_engine.qualification.l2f2_train_evidence import (
    TRAIN_CANDIDATE_FAILURE_COUNT,
    TRAIN_EVALUATION_COUNT,
    TRAIN_EVALUATION_SET_SHA256,
    TRAIN_EXECUTION_FAILURE_SET_SHA256,
    TRAIN_LOGICAL_JOB_COUNT,
    TRAIN_PLAN_HASHES,
)
from tests.baseline_qualification_seam import trusted_for_tests

_CONTRACT = "b24a07e208ce8e2fff6672102ae4e61aed93c6f352a5af46ba81c4789adb76d6"
_ENVIRONMENT = "71e14a49833ac77bb9dc576345fb89c4dd68f4a3ad3673eb098d38593c1ef4d3"


def _repo() -> Path:
    return Path(__file__).resolve().parents[3]


_PROTOCOL_CONTENT = json.loads(
    (_repo() / "manifests/l2f2_baseline_protocol_v1.json").read_text(encoding="utf-8")
)["content"]
_HEAD = subprocess.run(
    ["git", "-C", str(_repo()), "rev-parse", "HEAD"], capture_output=True, text=True, check=True
).stdout.strip()
_HEAD_TREE = subprocess.run(
    ["git", "-C", str(_repo()), "rev-parse", "HEAD^{tree}"],
    capture_output=True,
    text=True,
    check=True,
).stdout.strip()


def _result(**overrides: Any) -> BaselineQualificationResult:
    fields: dict[str, Any] = {
        "qualified_source_git_sha": _HEAD,
        "qualified_source_tree_sha": _HEAD_TREE,
        "worktree_clean": True,
        "descends_closure_authority_source": True,
        "harness_ready_gate_hash": HARNESS_READY_GATE_HASH,
        "harness_ready_qualification_hash": HARNESS_READY_QUALIFICATION_HASH,
        "harness_ready_gate_verified": True,
        "baseline_protocol_hash": "c548e190571f5e964560cf30021a520ea8aad6674569fa3202af880d7dff77d1",
        "objective_identity": objective_identity(_PROTOCOL_CONTENT),
        "candidate_design_identity": candidate_design_identity(_PROTOCOL_CONTENT),
        "selection_interpretation_hash": "4c169912f67877d6ba254fb280dbd2ff44aa4aaaf65bedfa1bca9975f1efebbd",
        "scoring_contract_hash": _CONTRACT,
        "execution_environment_hash": _ENVIRONMENT,
        "minos_subnet_sha": "649bb92c6abccebde58a736a2b2af7fd77a701c1",
        "happy_resolved_digest": ACCEPTED_HAPPY_DIGEST,
        "bcftools_resolved_digest": ACCEPTED_BCFTOOLS_DIGEST,
        "scorer_source_identities_verified": True,
        "baseline_selected_hash": compute_baseline_selected_hash(),
        "baseline_selected_manifest_verified": True,
        "phase_d_closure_hash": "b3f3a0f6281d0d199a1925bf9c6ca91843256f33646d57f10d845f9bf629100b",
        "phase_d_closure_artifact_sha256": "4eaf622baa5755829e936588003277aa277b9d999db089ddc2c94adae4bb9f89",
        "closure_artifact_verified": True,
        "selected_config_hash": "157d88d1587c13be395c62d60e27d1becdada78fad45e65d883bc1190e51acea",
        "selected_rank": 0,
        "selected_inherited_candidate_index": 42,
        "selected_statistics_verified": True,
        "seed_config_hash": "4251cb85e5cd58b7eabfe530b9df23ea7d1d14fd882114b488d67cbd81b751b8",
        "seed_rank": 3,
        "candidate_count": 4,
        "member_count": 10,
        "observation_count": 40,
        "all_candidates_complete": True,
        "validation_infrastructure_incidents": 0,
        "train": TrainEvidenceSummary(
            revision="0020_l2f2_phase_c_execution",
            plan_hashes=TRAIN_PLAN_HASHES,
            logical_job_count=TRAIN_LOGICAL_JOB_COUNT,
            terminal_job_count=TRAIN_LOGICAL_JOB_COUNT,
            nonterminal_job_count=0,
            succeeded_without_evaluation=0,
            evaluation_count=TRAIN_EVALUATION_COUNT,
            evaluation_failure_count=0,
            evaluation_set_sha256=TRAIN_EVALUATION_SET_SHA256,
            execution_failure_set_sha256=TRAIN_EXECUTION_FAILURE_SET_SHA256,
            execution_failure_codes={"GATK_NONZERO_EXIT": TRAIN_CANDIDATE_FAILURE_COUNT},
            distinct_scoring_contracts=1,
            scoring_contract_hash=_CONTRACT,
            distinct_execution_environments=1,
            execution_environment_hash=_ENVIRONMENT,
        ),
        "test_seal_evidence": {"split_frozen_v2": "gates/split-frozen-v2.json"},
        "test_untouched": True,
        "train_and_validation_identities_disjoint": True,
        "evidence_sha256": {
            "phase_d_activation_evidence": "e58fa267130f9671dc7bd7991a5ea15e16ff8edef80a5ed189270d74baa536a2",
            "phase_d_execution_evidence": "1ebc6aeaac7aaf7cd2323623ab7b110e0e4596b67376caebff08f1887a45e000",
            "phase_d_sentinel_evidence": "db8ebc4387b2a3a2f343fc17f0e23c24f0a8c12c11cb0980741193367764d637",
            "phase_d_complete_matrix_evidence": "35431e546b511ad3a802266d1de71991230119f7727770fc057c7f179c56f798",
            "phase_d_closure_artifact": "4eaf622baa5755829e936588003277aa277b9d999db089ddc2c94adae4bb9f89",
            "phase_d_closure_evidence": "90f0f53577c78ded8e876cad35ed30e4ba0ba784316635a0d424aebee2f6bb24",
        },
        "created_at": "2026-09-03T12:00:00Z",
    }
    fields.update(overrides)
    return BaselineQualificationResult(**fields)


def _publish(tmp_path: Path, result: BaselineQualificationResult) -> tuple[Any, Path, Path]:
    gate = assemble_baseline_qualified_gate(
        trusted_for_tests(result), created_at="2026-09-03T12:00:00Z"
    )
    gate_path = write_gate(gate, tmp_path / "gate.json")
    qual_path = tmp_path / "qualification.json"
    qual_path.write_text(
        json.dumps(result.model_dump(mode="json"), indent=2, sort_keys=True), encoding="utf-8"
    )
    return gate, gate_path, qual_path


# --------------------------------------------------------------------------------------------
# the objective and design identities are DERIVED from frozen content
# --------------------------------------------------------------------------------------------
def test_the_objective_identity_covers_the_frozen_objective_subblocks() -> None:
    """It must move when the objective's MEANING moves, not only when a weight literal does."""
    baseline = objective_identity(_PROTOCOL_CONTENT)
    for mutate in (
        lambda c: {**c, "objective": {**c["objective"], "weight_cvar": 0.6}},
        lambda c: {**c, "objective": {**c["objective"], "failure_rate_denominator": "observed"}},
        lambda c: {**c, "objective": {**c["objective"], "missing_rule": "treat as zero"}},
        lambda c: {**c, "tie_break": list(reversed(c["tie_break"]))},
        lambda c: {**c, "decisions": {**c["decisions"], "D3_robustness_parameters": "alpha=0.5"}},
    ):
        assert objective_identity(mutate(_PROTOCOL_CONTENT)) != baseline


def test_the_design_identity_covers_the_frozen_design_subblocks() -> None:
    baseline = candidate_design_identity(_PROTOCOL_CONTENT)
    for mutate in (
        lambda c: {**c, "phase_b": {**c["phase_b"], "candidate_count": 47}},
        lambda c: {**c, "phase_a": {**c["phase_a"], "candidate_count": 38}},
        lambda c: {**c, "decisions": {**c["decisions"], "D8_phase_b_design_family": "RANDOM"}},
    ):
        assert candidate_design_identity(mutate(_PROTOCOL_CONTENT)) != baseline


def test_a_protocol_missing_the_defining_content_refuses_rather_than_inventing() -> None:
    from minos_engine.qualification.l2f2_baseline_qualified_contract import (
        BaselineQualificationContractError,
    )

    stripped = {k: v for k, v in _PROTOCOL_CONTENT.items() if k != "objective"}
    with pytest.raises(BaselineQualificationContractError, match="must not be invented"):
        objective_identity(stripped)


# --------------------------------------------------------------------------------------------
# the canonical qualification identity
# --------------------------------------------------------------------------------------------
def test_the_qualification_hash_is_domain_separated_and_deterministic() -> None:
    assert compute_baseline_qualification_hash(_result()) == compute_baseline_qualification_hash(
        _result()
    )
    assert BASELINE_QUALIFICATION_DOMAIN == "minos:l2f2-baseline-qualified:v1\n"
    assert _result().schema_version == BASELINE_QUALIFICATION_SCHEMA


def test_the_timestamp_is_excluded_but_every_scientific_field_is_not() -> None:
    baseline = compute_baseline_qualification_hash(_result())
    assert compute_baseline_qualification_hash(_result(created_at="2030-01-01T00:00:00Z")) == (
        baseline
    )
    for override in (
        {"selected_config_hash": "a" * 64},
        {"selected_rank": 1},
        {"phase_d_closure_hash": "a" * 64},
        {"baseline_selected_hash": "a" * 64},
        {"objective_identity": "a" * 64},
        {"candidate_design_identity": "a" * 64},
        {"qualified_source_git_sha": "a" * 40},
        {"harness_ready_gate_hash": "a" * 64},
        {"test_untouched": False},
    ):
        assert compute_baseline_qualification_hash(_result(**override)) != baseline, override


# --------------------------------------------------------------------------------------------
# the trust boundary
# --------------------------------------------------------------------------------------------
def test_a_caller_cannot_construct_a_trusted_qualification() -> None:
    """The decisive one: an observation saying whatever it likes must not reach the assembler."""
    with pytest.raises(BaselineQualificationObservationError, match="production qualifier alone"):
        TrustedBaselineQualification(object(), _result())  # type: ignore[arg-type]


def test_the_assembler_refuses_anything_that_is_not_trusted() -> None:
    with pytest.raises(BaselineQualificationError, match="only a TrustedBaselineQualification"):
        assemble_baseline_qualified_gate(_result())  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "override",
    [
        pytest.param({"test_untouched": False}, id="test-seal"),
        pytest.param({"scorer_source_identities_verified": False}, id="scorer-source"),
        pytest.param({"closure_artifact_verified": False}, id="closure"),
        pytest.param({"baseline_selected_manifest_verified": False}, id="freeze"),
        pytest.param({"selected_statistics_verified": False}, id="statistics"),
    ],
)
def test_a_false_verification_yields_hold_not_pass(override: dict) -> None:
    gate = assemble_baseline_qualified_gate(
        trusted_for_tests(_result(**override)), created_at="2026-09-03T12:00:00Z"
    )
    assert gate.status is GateStatus.HOLD, override


# --------------------------------------------------------------------------------------------
# canonical assembly
# --------------------------------------------------------------------------------------------
def test_the_assembled_gate_is_a_canonical_gate_artifact() -> None:
    gate = assemble_baseline_qualified_gate(
        trusted_for_tests(_result()), created_at="2026-09-03T12:00:00Z"
    )
    assert gate.gate_name == BASELINE_QUALIFIED_GATE
    assert gate.status is GateStatus.PASS
    assert gate.schema_version == "gate-artifact-v1"
    assert gate.qualified_source_git_sha == _HEAD
    assert gate.qualified_source_tree_sha == _HEAD_TREE
    assert gate.engine_git_sha == _HEAD
    assert gate.qualification_tool_version == BASELINE_QUALIFICATION_TOOL_VERSION
    assert gate.gate_hash
    assert set(gate.mandatory_checks) == required_checks_for(BASELINE_QUALIFIED_GATE)
    for key in (
        "qualification_hash",
        "baseline_selected_hash",
        "phase_d_closure_hash",
        "baseline_protocol_hash",
        "selection_interpretation_hash",
        "scoring_contract_hash",
        "objective_identity",
        "candidate_design_identity",
    ):
        assert gate.input_hashes.get(key), key
    assert gate.input_hashes["qualification_hash"] == compute_baseline_qualification_hash(_result())


def test_the_gate_hash_is_deterministic() -> None:
    a = assemble_baseline_qualified_gate(
        trusted_for_tests(_result()), created_at="2026-09-03T12:00:00Z"
    )
    b = assemble_baseline_qualified_gate(
        trusted_for_tests(_result()), created_at="2026-09-03T12:00:00Z"
    )
    assert a.gate_hash == b.gate_hash


# --------------------------------------------------------------------------------------------
# offline verification through the canonical framework
# --------------------------------------------------------------------------------------------
def test_a_published_gate_verifies_offline(tmp_path: Path) -> None:
    _, gate_path, qual_path = _publish(tmp_path, _result())
    outcome = verify_baseline_qualified_gate(
        gate_path=gate_path, qualification_path=qual_path, root=_repo()
    )
    assert outcome["ok"] is True, outcome["reasons"]
    assert outcome["required_check_count"] == 42


def test_a_tampered_gate_hash_is_rejected(tmp_path: Path) -> None:
    _, gate_path, qual_path = _publish(tmp_path, _result())
    payload = json.loads(gate_path.read_text(encoding="utf-8"))
    payload["gate_hash"] = "0" * 64
    gate_path.write_text(json.dumps(payload), encoding="utf-8")
    outcome = verify_baseline_qualified_gate(
        gate_path=gate_path, qualification_path=qual_path, root=_repo()
    )
    assert outcome["ok"] is False


def test_tampered_qualification_bytes_are_rejected(tmp_path: Path) -> None:
    """The gate's qualification_hash is over the artifact's own bytes, so editing it shows."""
    _, gate_path, qual_path = _publish(tmp_path, _result())
    payload = json.loads(qual_path.read_text(encoding="utf-8"))
    payload["selected_rank"] = 1
    qual_path.write_text(json.dumps(payload), encoding="utf-8")
    outcome = verify_baseline_qualified_gate(
        gate_path=gate_path, qualification_path=qual_path, root=_repo()
    )
    assert outcome["ok"] is False
    assert any("qualification_hash" in r for r in outcome["reasons"])


def test_a_forged_all_true_gate_is_rejected_by_the_artifact_hash(tmp_path: Path) -> None:
    """Flipping the checks after assembly breaks GateArtifact's own canonical hash."""
    result = _result(test_untouched=False)
    gate = assemble_baseline_qualified_gate(
        trusted_for_tests(result), created_at="2026-09-03T12:00:00Z"
    )
    assert gate.status is GateStatus.HOLD
    forged = gate.model_copy(
        update={
            "status": GateStatus.PASS,
            "mandatory_checks": dict.fromkeys(gate.mandatory_checks, True),
        }
    )
    gate_path = tmp_path / "gate.json"
    gate_path.write_text(json.dumps(forged.model_dump(mode="json")), encoding="utf-8")
    qual_path = tmp_path / "qualification.json"
    qual_path.write_text(json.dumps(result.model_dump(mode="json")), encoding="utf-8")
    outcome = verify_baseline_qualified_gate(
        gate_path=gate_path, qualification_path=qual_path, root=_repo()
    )
    assert outcome["ok"] is False
    assert any("gate_hash" in r or "unusable" in r for r in outcome["reasons"])


def test_a_hash_consistent_forgery_still_dies_on_re_derivation(tmp_path: Path) -> None:
    """The decisive layer. Recompute the gate hash so the artifact validator is satisfied — the
    checks must STILL be re-derived from the qualification and contradict the forged ones."""
    result = _result(test_untouched=False)
    gate = assemble_baseline_qualified_gate(
        trusted_for_tests(result), created_at="2026-09-03T12:00:00Z"
    )
    payload = gate.model_dump(mode="json")
    payload["status"] = GateStatus.PASS.value
    payload["mandatory_checks"] = dict.fromkeys(gate.mandatory_checks, True)
    payload["gate_hash"] = ""
    from minos_engine.gates.contracts import GateArtifact

    # re-validating with an empty hash makes the artifact internally consistent again
    consistent = GateArtifact.model_validate(payload)
    assert consistent.status is GateStatus.PASS
    assert consistent.gate_hash

    gate_path = tmp_path / "gate.json"
    gate_path.write_text(json.dumps(consistent.model_dump(mode="json")), encoding="utf-8")
    qual_path = tmp_path / "qualification.json"
    qual_path.write_text(json.dumps(result.model_dump(mode="json")), encoding="utf-8")
    outcome = verify_baseline_qualified_gate(
        gate_path=gate_path, qualification_path=qual_path, root=_repo()
    )
    assert outcome["ok"] is False
    assert any("derive false" in r or "disagree" in r for r in outcome["reasons"]), outcome[
        "reasons"
    ]


@pytest.mark.parametrize("field", ["qualified_source_git_sha", "qualified_source_tree_sha"])
def test_a_gate_disagreeing_with_the_qualification_source_is_rejected(
    tmp_path: Path, field: str
) -> None:
    gate, _, qual_path = _publish(tmp_path, _result())
    forged = gate.model_copy(update={field: "0" * 40})
    gate_path = tmp_path / "forged.json"
    gate_path.write_text(json.dumps(forged.model_dump(mode="json")), encoding="utf-8")
    outcome = verify_baseline_qualified_gate(
        gate_path=gate_path, qualification_path=qual_path, root=_repo()
    )
    assert outcome["ok"] is False


def test_a_nonexistent_qualified_source_commit_is_rejected(tmp_path: Path) -> None:
    """A plausible 40-character string is not provenance; Git must actually hold the commit."""
    _, gate_path, qual_path = _publish(
        tmp_path, _result(qualified_source_git_sha="0123456789abcdef0123456789abcdef01234567")
    )
    outcome = verify_baseline_qualified_gate(
        gate_path=gate_path, qualification_path=qual_path, root=_repo()
    )
    assert outcome["ok"] is False
    assert any("does not exist" in r for r in outcome["reasons"])


def test_a_tree_that_is_not_the_commits_actual_tree_is_rejected(tmp_path: Path) -> None:
    _, gate_path, qual_path = _publish(tmp_path, _result(qualified_source_tree_sha="a" * 40))
    outcome = verify_baseline_qualified_gate(
        gate_path=gate_path, qualification_path=qual_path, root=_repo()
    )
    assert outcome["ok"] is False
    assert any("actual tree" in r for r in outcome["reasons"])


# --------------------------------------------------------------------------------------------
# prefix collisions must fail
# --------------------------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("field", "prefix", "check"),
    [
        pytest.param("harness_ready_gate_hash", "0e8411eb", "harness_ready_gate_bound", id="gate"),
        pytest.param(
            "harness_ready_qualification_hash",
            "b1d1cc5d",
            "harness_ready_qualification_bound",
            id="qualification",
        ),
    ],
)
def test_an_eight_character_prefix_collision_is_rejected(
    field: str, prefix: str, check: str
) -> None:
    """The old implementation used startswith(); each of these would have passed it."""
    forged = prefix + "f" * 56
    assert len(forged) == 64
    assert forged not in (HARNESS_READY_GATE_HASH, HARNESS_READY_QUALIFICATION_HASH)
    checks = derive_checks(observation_from_result(_result(**{field: forged})))
    assert checks[check] is False


def test_the_full_harness_identities_are_the_committed_ones() -> None:
    gate = json.loads((_repo() / "gates/harness-ready.json").read_text(encoding="utf-8"))
    assert gate["gate_hash"] == HARNESS_READY_GATE_HASH
    assert gate["input_hashes"]["qualification_hash"] == HARNESS_READY_QUALIFICATION_HASH
    assert len(HARNESS_READY_GATE_HASH) == 64
    assert len(HARNESS_READY_QUALIFICATION_HASH) == 64


@pytest.mark.parametrize(
    ("override", "failing"),
    [
        pytest.param(
            {"happy_resolved_digest": "other/img@sha256:" + "0" * 64},
            "happy_immutable_digest_exact",
            id="happy",
        ),
        pytest.param(
            {"bcftools_resolved_digest": "other/img@sha256:" + "1" * 64},
            "bcftools_immutable_digest_exact",
            id="bcftools",
        ),
        pytest.param({"objective_identity": "a" * 64}, "objective_authority_exact", id="objective"),
        pytest.param(
            {"candidate_design_identity": "a" * 64}, "candidate_design_authority_exact", id="design"
        ),
        pytest.param(
            {"descends_closure_authority_source": False}, "qualified_source_present", id="ancestry"
        ),
        pytest.param(
            {"harness_ready_gate_verified": False},
            "harness_ready_gate_bound",
            id="harness-unverified",
        ),
    ],
)
def test_a_wrong_identity_fails_its_check(override: dict, failing: str) -> None:
    checks = derive_checks(observation_from_result(_result(**override)))
    assert checks[failing] is False, checks


# --------------------------------------------------------------------------------------------
# the writer, and the two-commit boundary
# --------------------------------------------------------------------------------------------
def test_the_writer_emits_a_gate_and_canonical_qualification_bytes(tmp_path: Path) -> None:
    result = _result()
    gate = assemble_baseline_qualified_gate(
        trusted_for_tests(result), created_at="2026-09-03T12:00:00Z"
    )
    gate_path, result_path = write_baseline_qualification_outputs(gate, result, root=tmp_path)
    assert gate_path.is_file() and result_path.is_file()
    outcome = verify_baseline_qualified_gate(
        gate_path=gate_path, qualification_path=result_path, root=_repo()
    )
    assert outcome["ok"] is True, outcome["reasons"]


def test_no_real_pass_gate_or_qualification_is_committed() -> None:
    """Source only. The evidence commit comes next, not now."""
    assert not (_repo() / "gates/baseline-qualified.json").exists()
    assert not (_repo() / "reports/layer2/baseline-qualified-result.json").exists()


def test_the_verifier_needs_no_database_gatk_or_truth() -> None:
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
        "sqlalchemy",
        "gatk",
        "hap.py",
        "truth",
    ):
        assert forbidden not in body, forbidden


def test_the_gate_carries_no_external_ci_assertion() -> None:
    """No fabricated GitHub Actions attestation anywhere in the gate."""
    gate = assemble_baseline_qualified_gate(
        trusted_for_tests(_result()), created_at="2026-09-03T12:00:00Z"
    )
    blob = json.dumps(gate.model_dump(mode="json")).lower()
    for forbidden in ("github", "actions", "ci_green", "workflow_run"):
        assert forbidden not in blob, forbidden
