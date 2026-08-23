"""L2-F F7-A HARNESS-READY qualification framework — behavioral tests and negative controls.

No GATK process is started, no database is opened and no network is used. The Twin/GATK parity
tests exercise the REAL accepted Stage-1 Twin builder against the REAL accepted F5 invocation
builder; the qualification tests exercise the REAL derivation, assembly and offline verification.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any

import pytest

from minos_engine.common.canonical_json import canonical_json_bytes
from minos_engine.experiments.execution_contract import ExecutionInput
from minos_engine.experiments.gatk_live_space import canonicalize_live_gatk_config
from minos_engine.gates.contracts import GateStatus
from minos_engine.gates.required_checks import required_checks_for
from minos_engine.qualification import l2f_harness_ready_runner as R
from minos_engine.qualification.l2f_failure_inventory import (
    FailureInventoryError,
    build_failure_inventory,
    implemented_execution_exceptions,
    verify_failure_inventory,
)
from minos_engine.qualification.l2f_gatk_twin_parity import (
    F7_PARITY_ADAPTER_VERSION,
    build_twin_plan_for_execution,
    compare_invocation_parity,
)
from minos_engine.qualification.l2f_harness_ready_contract import (
    ACCEPTED_CANDIDATE_COUNT,
    ACCEPTED_CANDIDATE_SET_HASH,
    ACCEPTED_F5_CONTRACT_HASH,
    ACCEPTED_F6_CORRECTIVE_COMMIT,
    ACCEPTED_LOGICAL_JOB_COUNT,
    ACCEPTED_MIGRATION_SHAS,
    ACCEPTED_PARAMETER_SPACE_HASH,
    ACCEPTED_PLAN_HASH,
    HARNESS_READY_GATE,
    HARNESS_READY_GATE_PATH,
    AcceptedIdentities,
    ArtifactVerificationResult,
    BoundaryResult,
    GatkBinaryIdentity,
    HarnessReadyContractError,
    HarnessReadyQualification,
    OfficialExecutionResult,
    QualificationInputIdentity,
    ResumeResult,
    SourceProvenance,
    canonical_qualification_bytes,
    compute_qualification_hash,
    load_qualification_json,
)
from minos_engine.storage.l2f_gatk_runner import build_logical_invocation

_H = {c: c * 64 for c in "0123456789abcdef"}
_GIT = {c: c * 40 for c in "0123456789abcdef"}


# --------------------------------------------------------------------------- #
# fixtures: a fully consistent qualification OBSERVATION set
# --------------------------------------------------------------------------- #
def _inputs(**over: Any) -> ExecutionInput:
    base: dict[str, Any] = {
        "dataset_id": "minos-chr18-0001",
        "round_id": "r1",
        "chromosome": "chr18",
        "profile_id": "p1",
        "content_hash": _H["1"],
        "feature_values_hash": _H["2"],
        "bam_sha256": _H["3"],
        "bai_sha256": _H["4"],
        "reference_sha256": _H["5"],
        "fai_sha256": _H["6"],
        "dictionary_sha256": _H["7"],
        "bam_size_bytes": 1024,
        "region_hash": _H["8"],
        "region_start0": 100,
        "region_end0_exclusive": 200,
    }
    base.update(over)
    return ExecutionInput(**base)


def _accepted_config() -> dict[str, Any]:
    return dict(canonicalize_live_gatk_config({}).effective_config)


def _twin_and_execution(**over: Any) -> tuple[Any, Any, str]:
    """The REAL Twin plan and the REAL F5 logical invocation from one shared CONFIG."""
    inputs = over.pop("inputs", None) or _inputs()
    config = over.pop("effective_config", None) or _accepted_config()
    canonical = canonicalize_live_gatk_config(config)
    invocation = build_logical_invocation(
        effective_config=dict(canonical.effective_config),
        inputs=inputs,
        gatk_executable_sha256=_H["b"],
        gatk_version="4.5.0.0",
    )
    plan = build_twin_plan_for_execution(
        effective_config=dict(canonical.effective_config),
        parameter_space_hash=canonical.parameter_space_hash,
        inputs=inputs,
        output_uri="file:///results/out.vcf",
        gatk_executable_sha256=_H["b"],
        gatk_version="4.5.0.0",
        engine_git_sha=_GIT["a"],
        budget_seconds=3600.0,
    )
    return plan, invocation, canonical.config_hash


def _qualification(**over: Any) -> HarnessReadyQualification:
    """A complete, internally consistent qualification result (every check derives true)."""
    plan, invocation, config_hash = _twin_and_execution()
    parity = compare_invocation_parity(plan, invocation, execution_config_hash=config_hash)
    inventory = build_failure_inventory()
    base: dict[str, Any] = {
        "qualifier_version": "f7a-1",
        "source": SourceProvenance(
            qualified_source_git_sha=_GIT["a"],
            qualified_source_tree_sha=_GIT["b"],
            f6_corrective_commit=ACCEPTED_F6_CORRECTIVE_COMMIT,
            descends_f6_corrective=True,
            worktree_matches_qualified_source=True,
        ),
        "accepted": AcceptedIdentities(
            e5_gate_hashes={"FEATURE-VIEW-READY": _H["1"], "FEATURE-MATRIX-FROZEN-1": _H["2"]},
            migration_sha256=dict(ACCEPTED_MIGRATION_SHAS),
            f5_contract_hash=ACCEPTED_F5_CONTRACT_HASH,
            live_gatk_source_artifact_sha256=_H["3"],
            live_gatk_parameter_space_artifact_sha256=_H["4"],
            parameter_space_hash=ACCEPTED_PARAMETER_SPACE_HASH,
            policy_hash=_H["5"],
            candidate_set_hash=ACCEPTED_CANDIDATE_SET_HASH,
            candidate_count=ACCEPTED_CANDIDATE_COUNT,
            plan_hash=ACCEPTED_PLAN_HASH,
            logical_job_count=ACCEPTED_LOGICAL_JOB_COUNT,
            alembic_head="0008_l2f_execution_results",
        ),
        "gatk_binary": GatkBinaryIdentity(executable_sha256=_H["b"], version="4.5.0.0"),
        "qualification_input": QualificationInputIdentity(
            member_index=0,
            candidate_index=0,
            dataset_id="minos-chr18-0001",
            profile_id="p1",
            chromosome="chr18",
            region_start0=100,
            region_end0_exclusive=200,
            job_key=_H["c"],
            config_hash=config_hash,
            input_identity_hash=_H["d"],
        ),
        "official_execution": OfficialExecutionResult(
            runner_class="SubprocessGatkRunner",
            used_official_runner=True,
            job_status="SUCCEEDED",
            result_hash=_H["e"],
            logical_argv_hash=invocation.argv_hash(),
            vcf_sha256=_H["f"],
            vcf_size_bytes=512,
            result_manifest_sha256=_H["0"],
            published_artifact_count=2,
            runtime_ms=1234,
        ),
        "twin_parity": parity,
        "resume": ResumeResult(
            engines_recreated=True,
            duplicate_rows_created=0,
            terminal_job_reset=False,
            terminal_job_reexecuted=False,
            artifact_bytes_rewritten=False,
            exact_replay_returned_existing=True,
            conflicting_replay_rejected=True,
            exhausted_queue_returns_none=True,
            nonterminal_jobs_remaining=0,
            automatic_retry_observed=False,
        ),
        "artifact_verification": ArtifactVerificationResult(
            artifacts_verified=3,
            config_artifact_ok=True,
            vcf_artifact_ok=True,
            result_manifest_artifact_ok=True,
            content_addressed_names_ok=True,
            media_types_ok=True,
            recomputed_input_identity_hash=_H["d"],
            recomputed_logical_argv_hash=invocation.argv_hash(),
            recomputed_result_hash=_H["e"],
            harness_verifier_status="PASS",
            harness_verifier_checks={"plan_identity_self_binding": True},
            verifier_non_mutating=True,
            fingerprint_before=_H["9"],
            fingerprint_after=_H["9"],
        ),
        "failure_inventory": inventory,
        "boundaries": BoundaryResult(
            truth_paths_resolved=0,
            scoring_paths_resolved=0,
            nontrain_members_touched=0,
            operational_database_written=False,
            operational_database_revision="0005_l2e_feature_view",
            operational_l2f_table_count=0,
            select_config_blocked=True,
            network_access_performed=False,
        ),
    }
    base.update(over)
    return HarnessReadyQualification(**base)


# --------------------------------------------------------------------------- #
# J1 — deterministic canonicalization
# --------------------------------------------------------------------------- #
def test_qualification_canonicalization_is_deterministic() -> None:
    a, b = _qualification(), _qualification()
    assert canonical_qualification_bytes(a) == canonical_qualification_bytes(b)
    assert compute_qualification_hash(a) == compute_qualification_hash(b)
    assert len(compute_qualification_hash(a)) == 64


def test_qualification_roundtrips_through_strict_parsing() -> None:
    raw = canonical_qualification_bytes(_qualification())
    assert compute_qualification_hash(load_qualification_json(raw)) == compute_qualification_hash(
        _qualification()
    )


def test_duplicate_json_keys_are_rejected() -> None:
    raw = canonical_qualification_bytes(_qualification())
    forged = raw.replace(b'{"accepted"', b'{"gate_name": "X", "accepted"', 1)
    with pytest.raises(HarnessReadyContractError, match="duplicate"):
        load_qualification_json(forged)


def test_unknown_fields_are_rejected() -> None:
    document = json.loads(canonical_qualification_bytes(_qualification()))
    document["surprise"] = 1
    with pytest.raises(HarnessReadyContractError):
        load_qualification_json(canonical_json_bytes(document))


def test_noncanonical_bytes_are_rejected() -> None:
    document = json.loads(canonical_qualification_bytes(_qualification()))
    with pytest.raises(HarnessReadyContractError, match="canonical"):
        load_qualification_json(json.dumps(document, indent=2).encode())


# --------------------------------------------------------------------------- #
# J2 — complete required-check enforcement
# --------------------------------------------------------------------------- #
def test_the_registered_required_checks_match_the_runner_inventory() -> None:
    registered = required_checks_for(HARNESS_READY_GATE)
    assert registered == frozenset(R.HARNESS_READY_REQUIRED_CHECKS)
    assert len(registered) == 40


def test_every_required_check_is_derived_from_observations() -> None:
    checks = R.derive_checks(_qualification())
    assert set(checks) == frozenset(R.HARNESS_READY_REQUIRED_CHECKS)
    assert all(checks.values())


def test_a_consistent_result_assembles_a_pass_gate() -> None:
    gate = R.assemble_harness_ready_gate(_qualification())
    assert gate.gate_name == HARNESS_READY_GATE
    assert gate.status is GateStatus.PASS
    assert set(gate.mandatory_checks) == frozenset(R.HARNESS_READY_REQUIRED_CHECKS)


def test_gate_assembly_is_deterministic_for_a_fixed_timestamp() -> None:
    stamp = "2026-01-01T00:00:00+00:00"
    a = R.assemble_harness_ready_gate(_qualification(), created_at=stamp)
    b = R.assemble_harness_ready_gate(_qualification(), created_at=stamp)
    assert a.gate_hash == b.gate_hash


@pytest.mark.parametrize(
    ("field", "mutation"),
    [
        ("resume", {"duplicate_rows_created": 1}),
        ("resume", {"terminal_job_reset": True}),
        ("resume", {"terminal_job_reexecuted": True}),
        ("resume", {"artifact_bytes_rewritten": True}),
        ("resume", {"conflicting_replay_rejected": False}),
        ("resume", {"exhausted_queue_returns_none": False}),
        ("resume", {"nonterminal_jobs_remaining": 1}),
        ("resume", {"automatic_retry_observed": True}),
        ("artifact_verification", {"config_artifact_ok": False}),
        ("artifact_verification", {"vcf_artifact_ok": False}),
        ("artifact_verification", {"result_manifest_artifact_ok": False}),
        ("artifact_verification", {"content_addressed_names_ok": False}),
        ("artifact_verification", {"media_types_ok": False}),
        ("artifact_verification", {"verifier_non_mutating": False}),
        ("artifact_verification", {"harness_verifier_status": "FAIL"}),
        ("boundaries", {"truth_paths_resolved": 1}),
        ("boundaries", {"scoring_paths_resolved": 1}),
        ("boundaries", {"nontrain_members_touched": 1}),
        ("boundaries", {"operational_database_written": True}),
        ("boundaries", {"operational_l2f_table_count": 1}),
        ("boundaries", {"select_config_blocked": False}),
        ("boundaries", {"network_access_performed": True}),
        ("official_execution", {"job_status": "FAILED"}),
        ("official_execution", {"published_artifact_count": 1}),
        ("source", {"descends_f6_corrective": False}),
        ("source", {"worktree_matches_qualified_source": False}),
    ],
)
def test_any_deficient_observation_holds_the_gate(field: str, mutation: dict[str, Any]) -> None:
    """A single deficient observation must HOLD the gate — never PASS."""
    result = _qualification()
    replaced = getattr(result, field).model_copy(update=mutation)
    degraded = _qualification(**{field: replaced})
    gate = R.assemble_harness_ready_gate(degraded)
    assert gate.status is GateStatus.HOLD
    assert not all(gate.mandatory_checks.values())


@pytest.mark.parametrize(
    "mutation",
    [
        {"f5_contract_hash": _H["0"]},
        {"parameter_space_hash": _H["0"]},
        {"candidate_set_hash": _H["0"]},
        {"candidate_count": 38},
        {"plan_hash": _H["0"]},
        {"logical_job_count": 1949},
        {"alembic_head": "0009_something"},
        {"migration_sha256": {"migrations/versions/0006_l2f_experiment_plan.py": _H["0"]}},
    ],
)
def test_a_wrong_accepted_identity_holds_the_gate(mutation: dict[str, Any]) -> None:
    degraded = _qualification(accepted=_qualification().accepted.model_copy(update=mutation))
    assert R.assemble_harness_ready_gate(degraded).status is GateStatus.HOLD


# --------------------------------------------------------------------------- #
# J6/J7 — the official runner is required; a fake runner can never qualify
# --------------------------------------------------------------------------- #
def test_a_fake_runner_can_never_satisfy_the_official_check() -> None:
    fake = _qualification().official_execution.model_copy(
        update={"runner_class": "FakeGatkRunner", "used_official_runner": False}
    )
    gate = R.assemble_harness_ready_gate(_qualification(official_execution=fake))
    assert gate.mandatory_checks["official_gatk_runner_used"] is False
    assert gate.status is GateStatus.HOLD


def test_a_fake_runner_name_alone_is_not_enough() -> None:
    """Claiming ``used_official_runner`` while naming a fake runner still fails."""
    lying = _qualification().official_execution.model_copy(
        update={"runner_class": "FakeGatkRunner", "used_official_runner": True}
    )
    gate = R.assemble_harness_ready_gate(_qualification(official_execution=lying))
    assert gate.mandatory_checks["official_gatk_runner_used"] is False


def test_a_symlinked_or_unpinned_binary_holds_the_gate() -> None:
    binary = _qualification().gatk_binary.model_copy(update={"absolute_path_is_symlink": True})
    gate = R.assemble_harness_ready_gate(_qualification(gatk_binary=binary))
    assert gate.mandatory_checks["official_gatk_binary_pinned"] is False


def test_a_changed_executable_digest_changes_the_qualification_identity() -> None:
    """The pinned digest is bound: changing it produces a different qualification hash."""
    other = _qualification().gatk_binary.model_copy(update={"executable_sha256": _H["0"]})
    assert compute_qualification_hash(_qualification(gatk_binary=other)) != (
        compute_qualification_hash(_qualification())
    )


def test_the_gatk_version_is_documented_as_provisioned_metadata() -> None:
    """F6's honest statement is preserved: the version is NOT probed from the executable."""
    assert _qualification().gatk_binary.version_provenance == "provisioned_metadata_bound_to_digest"


# --------------------------------------------------------------------------- #
# J8/J9/J10 — GATK/Twin semantic parity control and negative controls
# --------------------------------------------------------------------------- #
def test_the_accepted_config_yields_exact_twin_parity() -> None:
    plan, invocation, config_hash = _twin_and_execution()
    result = compare_invocation_parity(plan, invocation, execution_config_hash=config_hash)
    assert result.parity_ok is True
    assert result.first_difference is None
    assert result.compared_token_count > 0
    assert result.adapter_version == F7_PARITY_ADAPTER_VERSION


def test_the_twin_boundary_preserves_the_accepted_live_gatk_config() -> None:
    """The additive adapter must not drop, rename, clamp, coerce or default any value."""
    plan, _invocation, config_hash = _twin_and_execution()
    assert plan.effective_config == _accepted_config()
    assert plan.config_hash == config_hash  # both boundaries derive the SAME CONFIG identity


def test_every_accepted_candidate_reaches_exact_parity() -> None:
    """The STRONG parity proof: all 39 accepted candidates, not just the default CONFIG.

    Each candidate must survive the Twin boundary value-for-value and produce a byte-identical
    semantic invocation, so no accepted candidate can be silently defaulted, clamped or dropped.
    """
    from minos_engine.experiments.candidates import generate_accepted_candidate_set

    candidate_set = generate_accepted_candidate_set()
    assert candidate_set.candidate_count == ACCEPTED_CANDIDATE_COUNT
    for index, candidate in enumerate(candidate_set.configs):
        effective = dict(candidate.effective_config)
        plan, invocation, config_hash = _twin_and_execution(effective_config=effective)
        result = compare_invocation_parity(plan, invocation, execution_config_hash=config_hash)
        assert result.parity_ok is True, (index, result.first_difference)
        assert plan.effective_config == effective, index
        assert plan.config_hash == candidate.config_hash, index


def test_a_non_default_candidate_preserves_its_varied_value() -> None:
    """A candidate that genuinely differs from the baseline keeps that difference end to end."""
    from minos_engine.experiments.candidates import generate_accepted_candidate_set

    baseline = dict(generate_accepted_candidate_set().configs[0].effective_config)
    varied = next(
        dict(c.effective_config)
        for c in generate_accepted_candidate_set().configs
        if dict(c.effective_config) != baseline
    )
    changed = [k for k in varied if varied[k] != baseline[k]]
    assert changed, "the accepted candidate set must contain a non-baseline candidate"
    plan, invocation, config_hash = _twin_and_execution(effective_config=varied)
    result = compare_invocation_parity(plan, invocation, execution_config_hash=config_hash)
    assert result.parity_ok is True
    for key in changed:
        assert plan.effective_config[key] == varied[key]


def _mutate_argv(plan: Any, new_argv: tuple[str, ...]) -> Any:
    invocation = plan.invocation.model_copy(update={"argv": new_argv})
    return plan.model_copy(update={"invocation": invocation, "plan_hash": plan.plan_hash})


@pytest.mark.parametrize(
    ("label", "transform"),
    [
        ("changed_flag", lambda a: (*a[:9], "--not-a-real-flag", *a[10:])),
        ("changed_value", lambda a: (*a[:10], "999999", *a[11:])),
        ("changed_order", lambda a: (*a[:9], a[10], a[9], *a[11:])),
        ("dropped_parameter", lambda a: a[:-2]),
        ("extra_parameter", lambda a: (*a, "--sneaky", "1")),
        ("changed_region", lambda a: tuple("chr19:1-2" if t == a[7] else t for t in a)),
    ],
)
def test_a_semantic_difference_is_reported_and_never_becomes_pass(
    label: str, transform: Any
) -> None:
    plan, invocation, config_hash = _twin_and_execution()
    forged = _mutate_argv(plan, transform(tuple(plan.invocation.argv)))
    result = compare_invocation_parity(forged, invocation, execution_config_hash=config_hash)
    assert result.parity_ok is False, label
    assert result.first_difference is not None
    assert result.first_difference.field in {"argv_token", "argv_length", "region_token"}


def test_a_non_gatk_caller_is_rejected() -> None:
    plan, invocation, config_hash = _twin_and_execution()
    forged = _mutate_argv(plan, ("bcftools", *tuple(plan.invocation.argv)[1:]))
    result = compare_invocation_parity(forged, invocation, execution_config_hash=config_hash)
    assert result.parity_ok is False
    assert result.first_difference is not None
    assert result.first_difference.field == "caller_token"


def test_a_parity_mismatch_holds_the_gate() -> None:
    plan, invocation, config_hash = _twin_and_execution()
    forged = _mutate_argv(plan, tuple(plan.invocation.argv)[:-2])
    bad = compare_invocation_parity(forged, invocation, execution_config_hash=config_hash)
    gate = R.assemble_harness_ready_gate(_qualification(twin_parity=bad))
    assert gate.mandatory_checks["gatk_twin_semantic_parity"] is False
    assert gate.status is GateStatus.HOLD


def test_a_disagreeing_config_identity_is_rejected_even_with_equal_argv() -> None:
    plan, invocation, _config_hash = _twin_and_execution()
    result = compare_invocation_parity(plan, invocation, execution_config_hash=_H["0"])
    assert result.parity_ok is False
    assert result.first_difference is not None
    assert result.first_difference.field == "config_hash"


# --------------------------------------------------------------------------- #
# J21 — typed failure classification closure
# --------------------------------------------------------------------------- #
def test_the_inventory_covers_every_implemented_typed_exception() -> None:
    inventory = build_failure_inventory()
    classified = {e.exception_type for e in inventory.entries}
    assert implemented_execution_exceptions() <= classified
    assert inventory.complete and inventory.unambiguous
    verify_failure_inventory(inventory)


def test_every_inventory_entry_forbids_automatic_retry() -> None:
    for entry in build_failure_inventory().entries:
        assert entry.automatic_retry_allowed is False


def test_an_unclassified_exception_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    import minos_engine.qualification.l2f_failure_inventory as INV

    monkeypatch.setattr(
        INV, "implemented_execution_exceptions", lambda: frozenset({"BrandNewExecutionError"})
    )
    with pytest.raises(FailureInventoryError, match="unclassified"):
        verify_failure_inventory(build_failure_inventory())


@pytest.mark.parametrize(
    ("mutation", "match"),
    [
        ({"required_final_state": "RUNNING"}, "stranded"),
        ({"outcome_row_exists": False}, "durable FAILED"),
    ],
)
def test_a_structurally_wrong_classification_fails_closed(
    mutation: dict[str, Any], match: str
) -> None:
    inventory = build_failure_inventory()
    victim = next(
        e
        for e in inventory.entries
        if e.state_before_failure == "RUNNING"
        and e.required_final_state == "FAILED"
        and e.commit_outcome == "known"
    )
    forged = victim.model_copy(update=mutation)
    entries = tuple(forged if e.case == victim.case else e for e in inventory.entries)
    with pytest.raises(FailureInventoryError, match=match):
        verify_failure_inventory(inventory.model_copy(update={"entries": entries}))


def test_an_ambiguous_commit_may_not_claim_a_definite_final_state() -> None:
    inventory = build_failure_inventory()
    victim = next(e for e in inventory.entries if e.commit_outcome == "ambiguous")
    forged = victim.model_copy(update={"required_final_state": "SUCCEEDED"})
    entries = tuple(forged if e.case == victim.case else e for e in inventory.entries)
    with pytest.raises(FailureInventoryError, match="ambiguous"):
        verify_failure_inventory(inventory.model_copy(update={"entries": entries}))


def test_a_post_commit_wrapper_error_may_not_change_the_committed_result() -> None:
    inventory = build_failure_inventory()
    victim = next(e for e in inventory.entries if e.exception_type == "PostCommitWrapperError")
    forged = victim.model_copy(update={"required_final_state": "FAILED"})
    entries = tuple(forged if e.case == victim.case else e for e in inventory.entries)
    with pytest.raises(FailureInventoryError, match="PostCommitWrapperError"):
        verify_failure_inventory(inventory.model_copy(update={"entries": entries}))


def test_only_bounded_failure_codes_may_appear() -> None:
    inventory = build_failure_inventory()
    victim = next(e for e in inventory.entries if e.failure_code is not None)
    forged = victim.model_copy(update={"failure_code": "SOME FREE TEXT REASON"})
    entries = tuple(forged if e.case == victim.case else e for e in inventory.entries)
    with pytest.raises(FailureInventoryError, match="bounded failure code"):
        verify_failure_inventory(inventory.model_copy(update={"entries": entries}))


def test_gate_assembly_refuses_a_broken_inventory() -> None:
    inventory = build_failure_inventory().model_copy(update={"complete": False})
    with pytest.raises(FailureInventoryError):
        R.assemble_harness_ready_gate(_qualification(failure_inventory=inventory))


# --------------------------------------------------------------------------- #
# J3/J4/J5 — offline gate verification: ancestry, wrong tree, missing history
# --------------------------------------------------------------------------- #
def _repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def _head_sha() -> str:
    return subprocess.run(  # noqa: S603 - fixed argv, no shell
        ["git", "rev-parse", "HEAD"],  # noqa: S607
        cwd=_repo_root(),
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def _head_tree() -> str:
    return subprocess.run(  # noqa: S603 - fixed argv, no shell
        ["git", "rev-parse", "HEAD^{tree}"],  # noqa: S607
        cwd=_repo_root(),
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def _write_gate(tmp_path: Path, result: HarnessReadyQualification) -> Path:
    gate = R.assemble_harness_ready_gate(result, created_at="2026-01-01T00:00:00+00:00")
    path = tmp_path / "harness-ready.json"
    from minos_engine.gates.verifier import write_gate

    write_gate(gate, path)
    return path


def test_missing_evidence_fails_closed(tmp_path: Path) -> None:
    result = R.verify_committed_harness_ready_gate(
        base_dir=tmp_path, gate_path="gates/harness-ready.json"
    )
    assert result["ok"] is False
    assert any("missing evidence" in r for r in result["reasons"])


def test_the_f7a_source_commit_ships_no_committed_gate() -> None:
    """F7-A must not commit HARNESS-READY evidence: the gate belongs to F7-B."""
    assert not (_repo_root() / HARNESS_READY_GATE_PATH).exists()


def test_a_real_head_gate_verifies_against_real_git_history(tmp_path: Path) -> None:
    """Control: a gate bound to the REAL HEAD commit/tree passes the git-history checks."""
    source = _qualification().source.model_copy(
        update={
            "qualified_source_git_sha": _head_sha(),
            "qualified_source_tree_sha": _head_tree(),
        }
    )
    path = _write_gate(tmp_path, _qualification(source=source))
    result = R.verify_committed_harness_ready_gate(base_dir=_repo_root(), gate_path=path)
    history = [
        r
        for r in result["reasons"]
        if "source tree" in r or "descend" in r or "absent from history" in r
    ]
    assert history == [], result["reasons"]


def test_a_wrong_source_tree_fails_closed(tmp_path: Path) -> None:
    source = _qualification().source.model_copy(
        update={"qualified_source_git_sha": _head_sha(), "qualified_source_tree_sha": _GIT["0"]}
    )
    path = _write_gate(tmp_path, _qualification(source=source))
    result = R.verify_committed_harness_ready_gate(base_dir=_repo_root(), gate_path=path)
    assert result["ok"] is False
    assert any("wrong source tree" in r for r in result["reasons"])


def test_an_absent_source_commit_fails_closed(tmp_path: Path) -> None:
    path = _write_gate(tmp_path, _qualification())  # a synthetic 40-hex sha, not in history
    result = R.verify_committed_harness_ready_gate(base_dir=_repo_root(), gate_path=path)
    assert result["ok"] is False
    assert any("absent from history" in r for r in result["reasons"])


def test_missing_git_history_fails_closed(tmp_path: Path) -> None:
    path = _write_gate(tmp_path, _qualification())
    result = R.verify_committed_harness_ready_gate(base_dir=tmp_path, gate_path=path)
    assert result["ok"] is False
    assert any("missing Git history" in r for r in result["reasons"])


def test_a_held_gate_never_verifies(tmp_path: Path) -> None:
    degraded = _qualification(
        resume=_qualification().resume.model_copy(update={"nonterminal_jobs_remaining": 3})
    )
    path = _write_gate(tmp_path, degraded)
    result = R.verify_committed_harness_ready_gate(base_dir=_repo_root(), gate_path=path)
    assert result["ok"] is False
    assert any("not PASS" in r for r in result["reasons"])


def test_a_stripped_required_check_cannot_even_be_loaded_as_pass(tmp_path: Path) -> None:
    """A PASS gate missing a required check is refused by the gate contract itself."""
    gate = R.assemble_harness_ready_gate(_qualification(), created_at="2026-01-01T00:00:00+00:00")
    document = json.loads(json.dumps(gate.model_dump(mode="json")))
    document["mandatory_checks"].pop("gatk_twin_semantic_parity")
    path = tmp_path / "harness-ready.json"
    path.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    with pytest.raises(Exception, match="missing required checks"):
        R.verify_committed_harness_ready_gate(base_dir=_repo_root(), gate_path=path)


def test_a_falsified_required_check_cannot_be_a_pass_gate(tmp_path: Path) -> None:
    gate = R.assemble_harness_ready_gate(_qualification(), created_at="2026-01-01T00:00:00+00:00")
    document = json.loads(json.dumps(gate.model_dump(mode="json")))
    document["mandatory_checks"]["gatk_twin_semantic_parity"] = False
    path = tmp_path / "harness-ready.json"
    path.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    with pytest.raises(Exception):  # noqa: B017 - the contract refuses a PASS with a false check
        R.verify_committed_harness_ready_gate(base_dir=_repo_root(), gate_path=path)


def test_a_committed_qualification_result_must_reproduce_its_bound_hash(tmp_path: Path) -> None:
    source = _qualification().source.model_copy(
        update={
            "qualified_source_git_sha": _head_sha(),
            "qualified_source_tree_sha": _head_tree(),
        }
    )
    result = _qualification(source=source)
    gate_path = _write_gate(tmp_path, result)
    qual_path = tmp_path / "qualification.json"

    qual_path.write_bytes(canonical_qualification_bytes(result) + b"\n")
    ok = R.verify_committed_harness_ready_gate(
        base_dir=_repo_root(), gate_path=gate_path, qualification_path=qual_path
    )
    assert not any("qualification hash mismatch" in r for r in ok["reasons"]), ok["reasons"]

    tampered = result.model_copy(
        update={
            "official_execution": result.official_execution.model_copy(update={"runtime_ms": 99999})
        }
    )
    qual_path.write_bytes(canonical_qualification_bytes(tampered) + b"\n")
    bad = R.verify_committed_harness_ready_gate(
        base_dir=_repo_root(), gate_path=gate_path, qualification_path=qual_path
    )
    assert bad["ok"] is False
    assert any("qualification hash mismatch" in r for r in bad["reasons"])


# --------------------------------------------------------------------------- #
# J26 — the operational database endpoint is refused
# --------------------------------------------------------------------------- #
def test_the_operational_database_endpoint_is_refused() -> None:
    with pytest.raises(R.OperationalDatabaseRefused):
        R.refuse_operational_database("postgresql://u@127.0.0.1:5433/minos_engine_db")


def test_the_operational_database_is_refused_from_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "MINOS_DATABASE_URL", "postgresql://u@h:5433/minos_engine_db?sslmode=require"
    )
    with pytest.raises(R.OperationalDatabaseRefused):
        R.refuse_operational_database()


def test_a_scratch_database_is_permitted() -> None:
    R.refuse_operational_database("postgresql://u@127.0.0.1:5555/minos_f7_scratch")


# --------------------------------------------------------------------------- #
# J24/J25/J27 — leakage, stage gating and DB-V2 absence
# --------------------------------------------------------------------------- #
def test_select_config_remains_blocked() -> None:
    from minos_engine.common.errors import StageNotReadyError
    from minos_engine.layer2.service import Layer2Service

    with pytest.raises(StageNotReadyError):
        Layer2Service().select_config(None)  # type: ignore[arg-type]


def test_the_f7_surface_names_no_evaluation_concept() -> None:
    forbidden = (
        "truth",
        "hap_py",
        "happy_",
        "mutation_manifest",
        "tp_count",
        "fp_count",
        "fn_count",
        "leaderboard",
        "scoring",
        "training_target",
        "baseline_qualified",
    )
    from minos_engine.qualification import l2f_failure_inventory as INV
    from minos_engine.qualification import l2f_gatk_twin_parity as P
    from minos_engine.qualification import l2f_harness_ready_contract as C

    for module in (R, C, P, INV):
        for name in dir(module):
            assert not any(token in name.lower() for token in forbidden), (module.__name__, name)


def test_migration_0009_and_all_db_v2_artifacts_remain_absent() -> None:
    root = _repo_root()
    versions = sorted(p.name for p in (root / "migrations" / "versions").glob("0*.py"))
    assert versions == [
        "0001_l2b_initial.py",
        "0002_l2c_dataset_split.py",
        "0003_l2c_split_v2_epochs.py",
        "0004_l2d_profile_ingestion.py",
        "0005_l2e_feature_view.py",
        "0006_l2f_experiment_plan.py",
        "0007_l2f_job_claiming.py",
        "0008_l2f_execution_results.py",
    ]
    tracked = subprocess.run(  # noqa: S603 - fixed argv, no shell
        ["git", "ls-files"],  # noqa: S607
        cwd=root,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.splitlines()
    for path in tracked:
        lowered = path.lower()
        assert "dbv2" not in lowered, path
        assert "db-v2" not in lowered, path
        assert "minos_database_v2" not in lowered, path
        assert "v1_retired" not in lowered, path


def test_no_db_v2_contract_symbol_exists() -> None:
    root = _repo_root()
    hits = subprocess.run(  # noqa: S603 - fixed argv, no shell
        ["git", "grep", "-lE", "MINOS_DB_RECOVERY_ROOT|v1_retired_|dbv2"],  # noqa: S607
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    ).stdout.strip()
    assert hits == "", hits


# --------------------------------------------------------------------------- #
# I — CLI behavior
# --------------------------------------------------------------------------- #
def _cli(*args: str) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "PYTHONPATH": str(_repo_root() / "src")}
    env.pop("MINOS_DATABASE_URL", None)
    return subprocess.run(  # noqa: S603 - fixed argv, no shell
        ["python", "-m", "minos_engine.cli.main", "layer2", "harness", *args],  # noqa: S607
        cwd=_repo_root(),
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )


def test_cli_check_fails_closed_when_no_gate_is_committed() -> None:
    proc = _cli("qualify", "--check")
    assert proc.returncode == 3
    payload = json.loads(proc.stdout.strip().splitlines()[-1])
    assert payload["ok"] is False
    assert any("missing evidence" in r for r in payload["reasons"])


def test_cli_require_pass_fails_closed_when_no_gate_is_committed() -> None:
    proc = _cli("gate", "require-pass")
    assert proc.returncode == 3
    payload = json.loads(proc.stdout.strip().splitlines()[-1])
    assert payload["ok"] is False


def test_cli_qualify_reports_the_unavailable_official_environment() -> None:
    """Without a provisioned official GATK binary the live path fails closed, never PASSes."""
    proc = _cli("qualify")
    assert proc.returncode == 3
    payload = json.loads(proc.stdout.strip().splitlines()[-1])
    assert payload["ok"] is False
    assert payload["reasons"]


def test_cli_check_accepts_a_real_gate_file(tmp_path: Path) -> None:
    source = _qualification().source.model_copy(
        update={
            "qualified_source_git_sha": _head_sha(),
            "qualified_source_tree_sha": _head_tree(),
        }
    )
    path = _write_gate(tmp_path, _qualification(source=source))
    proc = _cli("qualify", "--check", "--gate", str(path))
    payload = json.loads(proc.stdout.strip().splitlines()[-1])
    assert payload["gate"] == HARNESS_READY_GATE
    # every git-history reason must be absent for a genuinely HEAD-bound gate
    assert not any("descend" in r or "wrong source tree" in r for r in payload["reasons"])
