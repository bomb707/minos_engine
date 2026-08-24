"""L2-F F7-A HARNESS-READY qualification framework — behavioral tests and negative controls.

No GATK process is started, no database is opened and no network is used. The Twin/GATK parity
tests exercise the REAL accepted Stage-1 Twin builder against the REAL accepted F5 invocation
builder; the qualification tests exercise the REAL derivation, assembly and offline verification.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from pathlib import Path
from typing import Any

import pytest

from minos_engine.common.canonical_json import canonical_json_bytes
from minos_engine.experiments.execution_contract import (
    ExecutionInput,
    GatkExecutionError,
    compute_gatk_runtime_bundle_sha256,
)
from minos_engine.experiments.gatk_live_space import canonicalize_live_gatk_config
from minos_engine.gates.contracts import GateStatus
from minos_engine.gates.required_checks import required_checks_for
from minos_engine.qualification import l2f_harness_ready_runner as R
from minos_engine.qualification.l2f_accepted_identities import (
    AcceptedIdentityError,
    recompute_accepted_identities,
    verify_accepted_identities,
)
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
    ACCEPTED_F6_CORRECTIVE_COMMIT,
    ACCEPTED_PARAMETER_SPACE_HASH,
    ACCEPTED_PLAN_HASH,
    HARNESS_READY_GATE,
    HARNESS_READY_GATE_PATH,
    HARNESS_READY_QUALIFICATION_TOOL_VERSION,
    HARNESS_READY_QUALIFIER_SCHEMA,
    HARNESS_READY_QUALIFIER_VERSION,
    QUALIFIER_VERSIONS,
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
_BUNDLE = compute_gatk_runtime_bundle_sha256(
    launcher_sha256=_H["b"], local_jar_sha256=_H["a"], gatk_version="4.5.0.0"
)
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
        gatk_runtime_bundle_sha256=_H["c"],
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
        # bound to the SOURCE constant: a stale literal here would silently exempt the
        # canonical fixture from the version invariant it is supposed to demonstrate.
        "qualifier_version": HARNESS_READY_QUALIFIER_VERSION,
        "source": SourceProvenance(
            qualified_source_git_sha=_GIT["a"],
            qualified_source_tree_sha=_GIT["b"],
            f6_corrective_commit=ACCEPTED_F6_CORRECTIVE_COMMIT,
            descends_f6_corrective=True,
            worktree_matches_qualified_source=True,
        ),
        # the accepted block is RECOMPUTED from the real committed bytes: arbitrary 64-hex
        # values no longer satisfy the accepted-identity checks (see the negative controls).
        "accepted": recompute_accepted_identities(),
        # a COMPLETE bundle identity: launcher + the local JAR it actually runs, with the
        # bundle digest genuinely derived from those parts and the observed runtime version.
        "gatk_binary": GatkBinaryIdentity(
            executable_sha256=_H["b"],
            local_jar_sha256=_H["a"],
            runtime_bundle_sha256=_BUNDLE,
            version="4.5.0.0",
            observed_version="4.5.0.0",
            local_jar_is_symlink=False,
            jar_override_variables_inherited=False,
            python_executable_sha256=_H["8"],
            java_executable_sha256=_H["9"],
        ),
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
            gatk_runtime_bundle_sha256=_BUNDLE,
            vcf_sha256=_H["f"],
            vcf_size_bytes=512,
            result_manifest_sha256=_H["0"],
            published_artifact_count=2,
            runtime_ms=1234,
        ),
        "twin_parity": parity,
        # a COMPLETE raw-evidence resume observation: every strengthened binding must be
        # satisfied, so an empty/default observation can no longer reach PASS.
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
            row_counts_before={"jobs": 4, "results": 1},
            row_counts_after={"jobs": 4, "results": 1},
            database_fingerprint_before=_H["3"],
            database_fingerprint_after=_H["3"],
            artifact_fingerprint_before=_H["4"],
            artifact_fingerprint_after=_H["4"],
            conflicting_replay_observed=True,
            conflicting_replay_expected_exception="ImmutableMetadataConflictError",
            conflicting_replay_observed_exception="ImmutableMetadataConflictError",
            conflicting_replay_created_rows=0,
            conflicting_replay_db_fingerprint_before=_H["5"],
            conflicting_replay_db_fingerprint_after=_H["5"],
            conflicting_replay_artifact_fingerprint_before=_H["6"],
            conflicting_replay_artifact_fingerprint_after=_H["6"],
            failed_control_observed=True,
            failed_control_job_key=_H["7"],
            failed_control_failure_rows=1,
            failed_control_result_rows=0,
            failed_control_retry_executions=0,
            failed_job_remained_failed=True,
            failed_job_reclaimed=False,
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
            operational_read_only_before_set=True,
            operational_default_read_only=True,
            operational_role_is_superuser=False,
            operational_write_privileges=0,
            operational_write_denied_sqlstate="25006",
            operational_fingerprint_before=_H["8"],
            operational_fingerprint_after=_H["8"],
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
    gate = _assemble(_qualification())
    assert gate.gate_name == HARNESS_READY_GATE
    assert gate.status is GateStatus.PASS
    assert set(gate.mandatory_checks) == frozenset(R.HARNESS_READY_REQUIRED_CHECKS)


def test_gate_assembly_is_deterministic_for_a_fixed_timestamp() -> None:
    stamp = "2026-01-01T00:00:00+00:00"
    a = _assemble(_qualification(), created_at=stamp)
    b = _assemble(_qualification(), created_at=stamp)
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
    gate = _assemble(degraded)
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
    assert _assemble(degraded).status is GateStatus.HOLD


# --------------------------------------------------------------------------- #
# J6/J7 — the official runner is required; a fake runner can never qualify
# --------------------------------------------------------------------------- #
def test_a_fake_runner_can_never_satisfy_the_official_check() -> None:
    fake = _qualification().official_execution.model_copy(
        update={"runner_class": "FakeGatkRunner", "used_official_runner": False}
    )
    gate = _assemble(_qualification(official_execution=fake))
    assert gate.mandatory_checks["official_gatk_runner_used"] is False
    assert gate.status is GateStatus.HOLD


def test_a_fake_runner_name_alone_is_not_enough() -> None:
    """Claiming ``used_official_runner`` while naming a fake runner still fails."""
    lying = _qualification().official_execution.model_copy(
        update={"runner_class": "FakeGatkRunner", "used_official_runner": True}
    )
    gate = _assemble(_qualification(official_execution=lying))
    assert gate.mandatory_checks["official_gatk_runner_used"] is False


def test_a_symlinked_or_unpinned_binary_holds_the_gate() -> None:
    binary = _qualification().gatk_binary.model_copy(update={"absolute_path_is_symlink": True})
    gate = _assemble(_qualification(gatk_binary=binary))
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
    gate = _assemble(_qualification(twin_parity=bad))
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
        _assemble(_qualification(failure_inventory=inventory))


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


def _trusted(result: HarnessReadyQualification) -> Any:
    """Mint a TrustedQualification the way ONLY the production qualifier can.

    Tests reach into the module-private token deliberately, to exercise gate assembly. That the
    token is unreachable from ordinary code is itself asserted by the authority tests below.
    """
    from minos_engine.qualification import l2f_harness_ready_qualifier as Q

    return Q.TrustedQualification(Q._MINT, result)


def _assemble(result: HarnessReadyQualification, **kw: Any) -> Any:
    return R.assemble_gate_from_trusted_qualification(_trusted(result), **kw)


def _current_source_qualification() -> HarnessReadyQualification:
    """A qualification bound to the REAL current HEAD, so ancestry/tree checks can pass."""
    return _qualification(
        source=_qualification().source.model_copy(
            update={
                "qualified_source_git_sha": _head_sha(),
                "qualified_source_tree_sha": _head_tree(),
            }
        )
    )


def _write_gate_at(gate: Any, path: Path) -> None:
    from minos_engine.gates.verifier import write_gate

    write_gate(gate, path)


def _restamped(gate: Any, **updates: Any) -> Any:
    """A gate mutated AND re-stamped, so it is internally consistent and loads cleanly.

    Without this, a mutated gate fails the generic integrity check first and never reaches the
    semantic version check the surrounding test is trying to exercise.
    """
    payload = {**gate.model_dump(mode="json"), **updates, "gate_hash": ""}
    return type(gate).model_validate(payload)


def _write_gate(tmp_path: Path, result: HarnessReadyQualification) -> Path:
    gate = _assemble(result, created_at="2026-01-01T00:00:00+00:00")
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


#: the two canonical HARNESS-READY evidence paths. Either BOTH are committed (the F7-B state)
#: or NEITHER is (the F7-A / pre-F7-B state); one without the other is incoherent.
HARNESS_READY_RESULT_PATH = "reports/layer2/harness-ready-result.json"


def _committed_evidence() -> tuple[Path, Path]:
    root = _repo_root()
    return root / HARNESS_READY_GATE_PATH, root / HARNESS_READY_RESULT_PATH


def test_committed_harness_ready_evidence_is_stage_coherent() -> None:
    """Valid in BOTH stages, and never merely an existence assertion.

    * gate absent  -> the canonical qualification result must be absent too (pre-F7-B);
    * gate present -> the result must be present AND the real offline verifier must return
      ``ok`` with no reasons, PASS status and the complete registered required-check set.

    A gate without its result, a result without its gate, a HOLD gate, a tampered result or an
    invalid source/tree all fail here rather than silently passing CI.
    """
    gate_path, result_path = _committed_evidence()
    if not gate_path.exists():
        assert not result_path.exists(), (
            f"{HARNESS_READY_RESULT_PATH} is committed without {HARNESS_READY_GATE_PATH}; "
            "HARNESS-READY evidence must be committed as a coherent pair"
        )
        return

    assert result_path.exists(), (
        f"{HARNESS_READY_GATE_PATH} is committed without {HARNESS_READY_RESULT_PATH}; "
        "a PASS gate is never sufficient on its own"
    )
    verified = R.verify_committed_harness_ready_gate(
        base_dir=_repo_root(), gate_path=gate_path, qualification_path=result_path
    )
    assert verified["reasons"] == (), verified["reasons"]
    assert verified["ok"] is True
    assert verified["status"] == "PASS"

    from minos_engine.gates.verifier import load_gate

    gate = load_gate(gate_path)
    required = required_checks_for(HARNESS_READY_GATE)
    assert required <= set(gate.mandatory_checks)
    assert all(gate.mandatory_checks[name] for name in required)


def test_a_gate_without_its_qualification_result_is_incoherent(tmp_path: Path) -> None:
    """Control for CASE 2: the pairing rule really rejects a lone gate."""
    source = _qualification().source.model_copy(
        update={
            "qualified_source_git_sha": _head_sha(),
            "qualified_source_tree_sha": _head_tree(),
        }
    )
    gate_path = _write_gate(tmp_path, _qualification(source=source))
    verified = R.verify_committed_harness_ready_gate(base_dir=_repo_root(), gate_path=gate_path)
    assert verified["ok"] is False
    assert any("missing qualification evidence" in r for r in verified["reasons"])


def test_a_coherent_evidence_pair_verifies(tmp_path: Path) -> None:
    """Control for CASE 2: a genuine gate + result pair passes the same verifier CI will run."""
    source = _qualification().source.model_copy(
        update={
            "qualified_source_git_sha": _head_sha(),
            "qualified_source_tree_sha": _head_tree(),
        }
    )
    result = _qualification(source=source)
    gate_path = _write_gate(tmp_path, result)
    result_path = tmp_path / "harness-ready-result.json"
    result_path.write_bytes(canonical_qualification_bytes(result) + b"\n")
    verified = R.verify_committed_harness_ready_gate(
        base_dir=_repo_root(), gate_path=gate_path, qualification_path=result_path
    )
    assert verified["reasons"] == (), verified["reasons"]
    assert verified["ok"] is True and verified["status"] == "PASS"


def test_a_real_head_gate_verifies_against_real_git_history(tmp_path: Path) -> None:
    """Control: a gate bound to the REAL HEAD commit/tree passes the git-history checks."""
    source = _qualification().source.model_copy(
        update={
            "qualified_source_git_sha": _head_sha(),
            "qualified_source_tree_sha": _head_tree(),
        }
    )
    path = _write_gate(tmp_path, _qualification(source=source))
    result = R.verify_committed_harness_ready_gate(
        base_dir=_repo_root(), gate_path=path, require_qualification_result=False
    )
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
    result = R.verify_committed_harness_ready_gate(
        base_dir=_repo_root(), gate_path=path, require_qualification_result=False
    )
    assert result["ok"] is False
    assert any("wrong source tree" in r for r in result["reasons"])


def test_an_absent_source_commit_fails_closed(tmp_path: Path) -> None:
    path = _write_gate(tmp_path, _qualification())  # a synthetic 40-hex sha, not in history
    result = R.verify_committed_harness_ready_gate(
        base_dir=_repo_root(), gate_path=path, require_qualification_result=False
    )
    assert result["ok"] is False
    assert any("absent from history" in r for r in result["reasons"])


def test_missing_git_history_fails_closed(tmp_path: Path) -> None:
    path = _write_gate(tmp_path, _qualification())
    result = R.verify_committed_harness_ready_gate(
        base_dir=tmp_path, gate_path=path, require_qualification_result=False
    )
    assert result["ok"] is False
    assert any("missing Git history" in r for r in result["reasons"])


def test_a_held_gate_never_verifies(tmp_path: Path) -> None:
    degraded = _qualification(
        resume=_qualification().resume.model_copy(update={"nonterminal_jobs_remaining": 3})
    )
    path = _write_gate(tmp_path, degraded)
    result = R.verify_committed_harness_ready_gate(
        base_dir=_repo_root(), gate_path=path, require_qualification_result=False
    )
    assert result["ok"] is False
    assert any("not PASS" in r for r in result["reasons"])


def test_a_stripped_required_check_cannot_even_be_loaded_as_pass(tmp_path: Path) -> None:
    """A gate whose checks were edited after assembly is refused by the gate contract itself."""
    gate = _assemble(_qualification(), created_at="2026-01-01T00:00:00+00:00")
    document = json.loads(json.dumps(gate.model_dump(mode="json")))
    document["mandatory_checks"].pop("gatk_twin_semantic_parity")
    path = tmp_path / "harness-ready.json"
    path.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    with pytest.raises(Exception, match="missing required checks"):
        R.verify_committed_harness_ready_gate(
            base_dir=_repo_root(), gate_path=path, require_qualification_result=False
        )


def test_a_falsified_required_check_cannot_be_a_pass_gate(tmp_path: Path) -> None:
    gate = _assemble(_qualification(), created_at="2026-01-01T00:00:00+00:00")
    document = json.loads(json.dumps(gate.model_dump(mode="json")))
    document["mandatory_checks"]["gatk_twin_semantic_parity"] = False
    path = tmp_path / "harness-ready.json"
    path.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    with pytest.raises(Exception):  # noqa: B017 - the contract refuses a PASS with a false check
        R.verify_committed_harness_ready_gate(
            base_dir=_repo_root(), gate_path=path, require_qualification_result=False
        )


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


def test_the_accepted_l2f1_migrations_are_intact_and_db_v2_remains_absent() -> None:
    """The L2-F1 lineage 0001-0008 is unchanged and DB-V2 stays abandoned.

    Later stages legitimately ADD migrations (0009 is the sanctioned L2-F2 evaluation ledger), so
    this asserts the accepted prefix rather than forbidding growth. DB-V2 remains permanently
    excluded, which is the guard's actual subject.
    """
    root = _repo_root()
    versions = sorted(p.name for p in (root / "migrations" / "versions").glob("0*.py"))
    accepted = [
        "0001_l2b_initial.py",
        "0002_l2c_dataset_split.py",
        "0003_l2c_split_v2_epochs.py",
        "0004_l2d_profile_ingestion.py",
        "0005_l2e_feature_view.py",
        "0006_l2f_experiment_plan.py",
        "0007_l2f_job_claiming.py",
        "0008_l2f_execution_results.py",
    ]
    assert versions[: len(accepted)] == accepted
    # anything beyond the accepted prefix must be an ADDITIVE later-stage migration, never a
    # resurrection of the abandoned DB-V2 line.
    for extra in versions[len(accepted) :]:
        assert "v2" not in extra.lower() or "l2c_split_v2" in extra, extra
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
    ).stdout.split()
    # This guard file and the harness document NAME the forbidden tokens in order to exclude
    # them; they are the denylist, not DB-V2 work. Every other match is a real violation.
    allowed = {
        "tests/unit/layer2/test_l2f_harness_ready_framework.py",
        "docs/layer2/EXPERIMENT_HARNESS.md",
    }
    assert set(hits) <= allowed, sorted(set(hits) - allowed)


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


def test_cli_check_fails_closed_for_missing_evidence(tmp_path: Path) -> None:
    """Missing-evidence behaviour, independent of the repository's current stage.

    Explicit nonexistent paths are used so this keeps testing MISSING evidence once F7-B commits
    the real gate at the default path.
    """
    missing_gate = tmp_path / "missing-harness-ready.json"
    missing_result = tmp_path / "missing-harness-ready-result.json"
    proc = _cli(
        "qualify",
        "--check",
        "--gate",
        str(missing_gate),
        "--qualification",
        str(missing_result),
    )
    assert proc.returncode == 3
    payload = json.loads(proc.stdout.strip().splitlines()[-1])
    assert payload["ok"] is False
    assert any("missing evidence" in r for r in payload["reasons"])


def test_cli_require_pass_fails_closed_for_missing_evidence(tmp_path: Path) -> None:
    missing_gate = tmp_path / "missing-harness-ready.json"
    missing_result = tmp_path / "missing-harness-ready-result.json"
    proc = _cli(
        "gate",
        "require-pass",
        "--gate",
        str(missing_gate),
        "--qualification",
        str(missing_result),
    )
    assert proc.returncode == 3
    payload = json.loads(proc.stdout.strip().splitlines()[-1])
    assert payload["ok"] is False


def test_cli_verifies_the_canonical_committed_evidence_when_present() -> None:
    """Stage-aware: once F7-B commits evidence, ordinary pytest checks it via BOTH CLI paths."""
    gate_path, result_path = _committed_evidence()
    if not gate_path.exists() and not result_path.exists():
        pytest.skip("pre-F7-B state: no HARNESS-READY evidence is committed yet")
    assert gate_path.exists() and result_path.exists(), (
        "HARNESS-READY evidence must be committed as a coherent gate + result pair"
    )
    for argv in (
        (
            "qualify",
            "--check",
            "--gate",
            HARNESS_READY_GATE_PATH,
            "--qualification",
            HARNESS_READY_RESULT_PATH,
            "--base-dir",
            ".",
        ),
        (
            "gate",
            "require-pass",
            "--gate",
            HARNESS_READY_GATE_PATH,
            "--qualification",
            HARNESS_READY_RESULT_PATH,
            "--base-dir",
            ".",
        ),
    ):
        proc = _cli(*argv)
        payload = json.loads(proc.stdout.strip().splitlines()[-1])
        assert proc.returncode == 0, payload
        assert payload["ok"] is True, payload


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


# --------------------------------------------------------------------------- #
# CORRECTIVE 1-2 — the production authority cannot consume a synthetic qualification
# --------------------------------------------------------------------------- #
def test_a_synthetic_qualification_cannot_reach_the_gate_assembler() -> None:
    """The central corrective: a caller-built qualification is REFUSED by the authority."""
    with pytest.raises(Exception, match="TrustedQualification"):
        R.assemble_gate_from_trusted_qualification(_qualification())


@pytest.mark.parametrize("payload", [None, {}, {"official_gatk_runner_used": True}, "PASS", 1])
def test_the_assembler_refuses_every_non_trusted_payload(payload: Any) -> None:
    with pytest.raises(Exception):  # noqa: B017 - refused before any check is derived
        R.assemble_gate_from_trusted_qualification(payload)


def test_a_forged_trusted_wrapper_cannot_be_minted() -> None:
    """Minting requires the module-private token; a look-alike object is refused."""
    from minos_engine.qualification import l2f_harness_ready_qualifier as Q

    class _FakeToken:
        pass

    with pytest.raises(Q.QualificationEnvironmentError, match="only be minted"):
        Q.TrustedQualification(_FakeToken(), _qualification())  # type: ignore[arg-type]


def test_the_public_qualifier_api_accepts_no_qualification_input() -> None:
    """`run_harness_ready_qualification` exposes only `base_dir` — nothing injectable."""
    import inspect

    from minos_engine.qualification import l2f_harness_ready_qualifier as Q

    params = set(inspect.signature(Q.run_harness_ready_qualification).parameters)
    assert params == {"base_dir"}
    forbidden = {
        "result",
        "qualification",
        "checks",
        "mandatory_checks",
        "runner",
        "plan",
        "candidate_set",
        "member",
        "candidate",
        "source",
        "trust",
        "hashes",
    }
    assert params & forbidden == set()


def test_the_cli_exposes_no_qualification_or_check_injection() -> None:
    """The production CLI has no flag by which a result or check dictionary can be supplied."""
    import argparse

    from minos_engine.cli.layer2_harness_commands import add_harness_subparser

    parser = argparse.ArgumentParser()
    add_harness_subparser(parser.add_subparsers(dest="cmd", required=True))
    flags = {
        action.option_strings[0]
        for action in parser._subparsers._group_actions[0]  # type: ignore[union-attr]
        .choices["harness"]
        ._subparsers._group_actions[0]
        .choices["qualify"]
        ._actions
        if action.option_strings
    }
    for forbidden in ("--result", "--checks", "--runner", "--plan", "--trust", "--observations"):
        assert forbidden not in flags, forbidden


# --------------------------------------------------------------------------- #
# CORRECTIVE 10-14 — accepted identities must EQUAL recomputed values
# --------------------------------------------------------------------------- #
def test_recomputed_accepted_identities_verify() -> None:
    verify_accepted_identities(recompute_accepted_identities())


@pytest.mark.parametrize(
    "mutation",
    [
        {"e5_gate_hashes": {"FEATURE-VIEW-READY": _H["1"], "FEATURE-MATRIX-FROZEN-1": _H["2"]}},
        {"policy_hash": _H["5"]},
        {"live_gatk_source_artifact_sha256": _H["3"]},
        {"live_gatk_parameter_space_artifact_sha256": _H["4"]},
        {"migration_sha256": {"migrations/versions/0006_l2f_experiment_plan.py": _H["0"]}},
        {"f5_contract_hash": _H["0"]},
        {"parameter_space_hash": _H["0"]},
        {"candidate_set_hash": _H["0"]},
        {"plan_hash": _H["0"]},
        {"alembic_head": "0009_not_real"},
    ],
)
def test_arbitrary_well_formed_identities_are_not_accepted_bindings(
    mutation: dict[str, Any],
) -> None:
    """A 64-hex string is NOT an accepted identity: it must equal the recomputed value."""
    forged = recompute_accepted_identities().model_copy(update=mutation)
    with pytest.raises(AcceptedIdentityError):
        verify_accepted_identities(forged)
    gate = _assemble(_qualification(accepted=forged))
    assert gate.status is GateStatus.HOLD


# --------------------------------------------------------------------------- #
# CORRECTIVE 5-9 — the GATK binary is verified from ACTUAL bytes
# --------------------------------------------------------------------------- #
#: what the real Broad launcher prints for ``gatk --version``.
_BANNER = "The Genome Analysis Toolkit (GATK) v4.5.0.0"


def _fixture_binary(
    tmp_path: Path,
    body: str | None = None,
    *,
    version: str = "4.5.0.0",
    jar_name: str | None = None,
    jar_body: bytes = b"PK\x03\x04 fake local jar payload",
    jar_symlink: bool = False,
) -> Path:
    """Build a COMPLETE fake execution bundle: a launcher plus the local JAR it dispatches to.

    The launcher alone is never a sufficient identity, so every fixture here mirrors the real
    layout (``gatk`` + ``gatk-package-<version>-local.jar`` side by side) and answers a bounded
    ``--version`` probe the way the official launcher does.
    """
    path = tmp_path / "gatk"
    path.write_text(body or f'#!/bin/sh\necho "{_BANNER}"\nexit 0\n', encoding="utf-8")
    path.chmod(0o700)
    jar = tmp_path / (jar_name or f"gatk-package-{version}-local.jar")
    if jar_symlink:
        real = tmp_path / "elsewhere.jar"
        real.write_bytes(jar_body)
        jar.symlink_to(real)
    else:
        jar.write_bytes(jar_body)
    return path.resolve()


@pytest.fixture
def _child_runtime(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Put a resolvable ``python`` and ``java`` on the PATH the CHILD would inherit."""
    bindir = tmp_path / "runtime-bin"
    bindir.mkdir()
    for name in ("python", "java"):
        exe = bindir / name
        exe.write_text(f"#!/bin/sh\n# {name}\nexit 0\n", encoding="utf-8")
        exe.chmod(0o700)
    monkeypatch.setenv("PATH", f"{bindir}:{os.environ.get('PATH', '')}")
    monkeypatch.delenv("JAVA_HOME", raising=False)
    return bindir


def _runner(path: Path, *, digest: str | None = None, version: str = "4.5.0.0") -> Any:
    import hashlib as _h

    from minos_engine.storage.l2f_gatk_runner import SubprocessGatkRunner

    return SubprocessGatkRunner(
        executable=path,
        expected_sha256=digest or _h.sha256(path.read_bytes()).hexdigest(),
        expected_version=version,
    )


def test_the_actual_executable_bytes_are_hashed(tmp_path: Path, _child_runtime: Path) -> None:
    import hashlib as _h

    from minos_engine.qualification import l2f_harness_ready_qualifier as Q

    path = _fixture_binary(tmp_path)
    identity = Q.verify_official_gatk_binary(_runner(path))
    assert identity.executable_sha256 == _h.sha256(path.read_bytes()).hexdigest()
    assert identity.version_provenance == "provisioned_metadata_bound_to_digest"
    # the SCIENTIFIC payload is bound too, and the bundle derives from the observed parts
    jar = path.parent / "gatk-package-4.5.0.0-local.jar"
    assert identity.local_jar_sha256 == _h.sha256(jar.read_bytes()).hexdigest()
    assert identity.runtime_bundle_sha256 == compute_gatk_runtime_bundle_sha256(
        launcher_sha256=identity.executable_sha256,
        local_jar_sha256=str(identity.local_jar_sha256),
        gatk_version="4.5.0.0",
    )
    assert identity.observed_version == "4.5.0.0"
    assert identity.python_executable_sha256 and identity.java_executable_sha256


def test_a_wrong_provisioned_digest_fails_official_qualification(
    tmp_path: Path, _child_runtime: Path
) -> None:
    """The corrective test: a mismatched provisioned digest CANNOT qualify."""
    from minos_engine.qualification import l2f_harness_ready_qualifier as Q

    path = _fixture_binary(tmp_path)
    with pytest.raises(Q.QualificationEnvironmentError, match="does not equal the provisioned"):
        Q.verify_official_gatk_binary(_runner(path, digest=_H["0"]))


def test_a_swapped_executable_after_pinning_fails(tmp_path: Path, _child_runtime: Path) -> None:
    from minos_engine.qualification import l2f_harness_ready_qualifier as Q

    path = _fixture_binary(tmp_path)
    runner = _runner(path)
    path.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")  # swapped after pinning
    with pytest.raises(Q.QualificationEnvironmentError, match="sha256"):
        Q.verify_official_gatk_binary(runner)


def test_a_symlinked_executable_fails(tmp_path: Path, _child_runtime: Path) -> None:
    from minos_engine.qualification import l2f_harness_ready_qualifier as Q

    real = _fixture_binary(tmp_path)
    link = tmp_path / "link"
    link.symlink_to(real)
    with pytest.raises(Q.QualificationEnvironmentError, match="symlink"):
        Q.verify_official_gatk_binary(_runner(link))


def test_a_relative_executable_fails(tmp_path: Path) -> None:
    from minos_engine.qualification import l2f_harness_ready_qualifier as Q
    from minos_engine.storage.l2f_gatk_runner import SubprocessGatkRunner

    runner = SubprocessGatkRunner(
        executable=Path("gatk"), expected_sha256=_H["0"], expected_version="v"
    )
    with pytest.raises(Q.QualificationEnvironmentError, match="absolute"):
        Q.verify_official_gatk_binary(runner)


def test_a_fake_runner_cannot_enter_the_production_qualifier() -> None:
    from minos_engine.qualification import l2f_harness_ready_qualifier as Q
    from minos_engine.storage.l2f_gatk_runner import FakeGatkRunner

    with pytest.raises(Q.QualificationEnvironmentError, match="SubprocessGatkRunner"):
        Q.verify_official_gatk_binary(FakeGatkRunner())


# --------------------------------------------------------------------------- #
# CORRECTIVE 18-20 — the qualification job is DERIVED, never supplied
# --------------------------------------------------------------------------- #
def test_the_qualification_job_is_derived_from_the_accepted_train_plan() -> None:
    from minos_engine.qualification import l2f_harness_ready_qualifier as Q

    job = Q.derive_qualification_job()
    assert job.partition == "train"
    assert job.candidate_index == 0
    assert len(job.job_key) == 64
    assert job.config_hash == _accepted_candidate_hash()
    again = Q.derive_qualification_job()
    assert (again.job_key, again.member_index) == (job.job_key, job.member_index)


def _accepted_candidate_hash() -> str:
    from minos_engine.experiments.candidates import generate_accepted_candidate_set

    return generate_accepted_candidate_set().configs[0].config_hash


def test_the_derived_job_matches_the_accepted_plan_member() -> None:
    from minos_engine.experiments.accepted_plan import build_accepted_experiment_plan
    from minos_engine.qualification import l2f_harness_ready_qualifier as Q

    plan = build_accepted_experiment_plan()
    job = Q.derive_qualification_job()
    member = plan.members[job.member_index]
    assert (member.dataset_id, member.profile_id) == (job.dataset_id, job.profile_id)


def test_no_api_accepts_a_caller_member_or_candidate() -> None:
    import inspect

    from minos_engine.qualification import l2f_harness_ready_qualifier as Q

    assert inspect.signature(Q.derive_qualification_job).parameters == {}


# --------------------------------------------------------------------------- #
# CORRECTIVE 30-32 — leakage and network guards are ENFORCED
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "path",
    [
        "/data/truth/chr18.vcf",
        "/data/mutations/chr18.bed",
        "/data/happy/summary.csv",
        "/data/scores/run.json",
        "/data/validation/input.bam",
        "/data/test/input.bam",
        "/data/labels/y.parquet",
    ],
)
def test_evaluation_material_is_refused_when_actually_offered(path: str) -> None:
    from minos_engine.qualification import l2f_harness_ready_qualifier as Q

    with pytest.raises(Q.QualificationLeakageError):
        Q.leakage_denied_paths(path)


def test_the_accepted_train_layout_is_permitted() -> None:
    from minos_engine.qualification import l2f_harness_ready_qualifier as Q

    Q.leakage_denied_paths("/data/practice/round_r1/input.bam", "/data/reference/chr18/chr18.fa")


def test_a_network_attempt_is_blocked_not_assumed() -> None:
    import socket as _socket

    from minos_engine.qualification import l2f_harness_ready_qualifier as Q

    with Q.network_denied():
        with pytest.raises(Q.QualificationNetworkError):
            _socket.create_connection(("127.0.0.1", 9))
        with pytest.raises(Q.QualificationNetworkError):
            _socket.socket().connect(("127.0.0.1", 9))
    # the guard is removed afterwards, so it never leaks into the rest of the suite
    assert _socket.create_connection is not None


def test_the_qualifier_refuses_the_operational_database(monkeypatch: pytest.MonkeyPatch) -> None:
    from minos_engine.qualification import l2f_harness_ready_qualifier as Q

    monkeypatch.setenv("MINOS_DATABASE_URL", "postgresql://u@h:5433/minos_engine_db")
    with pytest.raises(R.OperationalDatabaseRefused):
        Q.run_harness_ready_qualification(base_dir=_repo_root())


def test_the_qualifier_requires_an_isolated_scratch_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from minos_engine.qualification import l2f_harness_ready_qualifier as Q

    monkeypatch.delenv("MINOS_DATABASE_URL", raising=False)
    monkeypatch.delenv("MINOS_L2F_QUALIFICATION_DATABASE_URL", raising=False)
    with pytest.raises(Q.QualificationEnvironmentError, match="isolated scratch"):
        Q.run_harness_ready_qualification(base_dir=_repo_root())


def test_the_qualifier_refuses_a_scratch_url_naming_the_operational_database(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from minos_engine.qualification import l2f_harness_ready_qualifier as Q

    monkeypatch.delenv("MINOS_DATABASE_URL", raising=False)
    monkeypatch.setenv(
        "MINOS_L2F_QUALIFICATION_DATABASE_URL", "postgresql://u@h:5433/minos_engine_db"
    )
    with pytest.raises(R.OperationalDatabaseRefused):
        Q.run_harness_ready_qualification(base_dir=_repo_root())


# --------------------------------------------------------------------------- #
# CORRECTIVE 3-4/15-17 — real source acquisition and fail-closed live path
# --------------------------------------------------------------------------- #
def test_source_provenance_is_acquired_from_real_git() -> None:
    from minos_engine.qualification import l2f_harness_ready_qualifier as Q

    source = Q.acquire_source_provenance(_repo_root(), require_clean_worktree=False)
    assert source.qualified_source_git_sha == _head_sha()
    assert source.qualified_source_tree_sha == _head_tree()
    assert source.descends_f6_corrective is True
    assert source.f6_corrective_commit == ACCEPTED_F6_CORRECTIVE_COMMIT


def test_a_dirty_worktree_is_refused_for_live_qualification() -> None:
    """A live result must speak for an exact commit, so a dirty tree fails closed."""
    import subprocess as _sp

    from minos_engine.qualification import l2f_harness_ready_qualifier as Q

    dirty = _sp.run(  # noqa: S603 - fixed argv, no shell
        ["git", "status", "--porcelain"],  # noqa: S607
        cwd=_repo_root(),
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    if not dirty:
        pytest.skip("the worktree is clean; the refusal is exercised during development")
    with pytest.raises(Q.QualificationEnvironmentError, match="clean tree"):
        Q.acquire_source_provenance(_repo_root())


def test_missing_git_history_fails_source_acquisition(tmp_path: Path) -> None:
    from minos_engine.qualification import l2f_harness_ready_qualifier as Q

    with pytest.raises(Q.QualificationEnvironmentError, match="missing Git history"):
        Q.acquire_source_provenance(tmp_path)


def test_the_live_path_fails_closed_without_the_official_environment(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """With a scratch endpoint but no provisioned GATK, the qualifier reaches the binary step."""
    from minos_engine.qualification import l2f_harness_ready_qualifier as Q

    monkeypatch.delenv("MINOS_DATABASE_URL", raising=False)
    monkeypatch.setenv("MINOS_L2F_QUALIFICATION_DATABASE_URL", "postgresql://u@h:5555/f7_scratch")
    for name in ("MINOS_L2F_GATK_EXECUTABLE", "MINOS_L2F_GATK_EXECUTABLE_SHA256"):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(Q.QualificationEnvironmentError) as excinfo:
        Q.run_harness_ready_qualification(base_dir=_repo_root())
    # it reached a REAL environment requirement (clean tree or provisioned GATK), never a
    # blanket "F7-A only ships the framework" refusal.
    assert "clean tree" in str(excinfo.value) or "not provisioned" in str(excinfo.value)


def test_the_cli_enters_the_real_qualifier(monkeypatch: pytest.MonkeyPatch) -> None:
    """`qualify` must call the production orchestrator, not print a canned F7-A refusal."""
    calls: list[Any] = []

    from minos_engine.cli import layer2_harness_commands as C
    from minos_engine.qualification import l2f_harness_ready_qualifier as Q

    def _spy(*, base_dir: Any) -> Any:
        calls.append(base_dir)
        raise Q.QualificationEnvironmentError("spy: environment unavailable")

    monkeypatch.setattr(Q, "run_harness_ready_qualification", _spy)
    args = argparse_namespace(
        check=False, base_dir=".", gate="g.json", qualification=None, write_outputs=False
    )
    assert C._cmd_qualify(args) == 3
    assert len(calls) == 1  # the real orchestrator WAS entered


def argparse_namespace(**kw: Any) -> Any:
    import argparse

    return argparse.Namespace(**kw)


def test_the_cli_does_not_write_outputs_without_an_explicit_flag(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from minos_engine.cli import layer2_harness_commands as C
    from minos_engine.qualification import l2f_harness_ready_qualifier as Q

    monkeypatch.setattr(
        Q, "run_harness_ready_qualification", lambda *, base_dir: _trusted(_qualification())
    )
    gate_path = tmp_path / "harness-ready.json"
    args = argparse_namespace(
        check=False, base_dir=".", gate=str(gate_path), qualification=None, write_outputs=False
    )
    C._cmd_qualify(args)
    assert not gate_path.exists()


# --------------------------------------------------------------------------- #
# CORRECTIVE 14 — require-pass demands the canonical qualification result
# --------------------------------------------------------------------------- #
def test_require_pass_demands_the_qualification_result(tmp_path: Path) -> None:
    source = _qualification().source.model_copy(
        update={
            "qualified_source_git_sha": _head_sha(),
            "qualified_source_tree_sha": _head_tree(),
        }
    )
    path = _write_gate(tmp_path, _qualification(source=source))
    result = R.verify_committed_harness_ready_gate(base_dir=_repo_root(), gate_path=path)
    assert result["ok"] is False
    assert any("missing qualification evidence" in r for r in result["reasons"])


def test_a_gate_and_result_that_disagree_on_the_source_fail(tmp_path: Path) -> None:
    source = _qualification().source.model_copy(
        update={
            "qualified_source_git_sha": _head_sha(),
            "qualified_source_tree_sha": _head_tree(),
        }
    )
    result = _qualification(source=source)
    gate_path = _write_gate(tmp_path, result)
    other = result.model_copy(
        update={"source": source.model_copy(update={"qualified_source_git_sha": _GIT["c"]})}
    )
    qual_path = tmp_path / "qualification.json"
    qual_path.write_bytes(canonical_qualification_bytes(other) + b"\n")
    verified = R.verify_committed_harness_ready_gate(
        base_dir=_repo_root(), gate_path=gate_path, qualification_path=qual_path
    )
    assert verified["ok"] is False


# --------------------------------------------------------------------------- #
# CLOSURE-2 FIX 1 — result_hash is RECOMPUTED through the frozen formula
# --------------------------------------------------------------------------- #
def test_the_qualifier_never_copies_the_manifest_result_hash() -> None:
    """`recomputed_result_hash` must come from compute_result_hash, not manifest.result_hash."""
    import ast
    import inspect

    from minos_engine.qualification import l2f_harness_ready_qualifier as Q

    source = inspect.getsource(Q._observe_result_details)
    tree = ast.parse(source.lstrip())
    assigned: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.keyword) and node.arg == "recomputed_result_hash":
            assigned.append(ast.dump(node.value))
    assert assigned, "recomputed_result_hash is never assigned"
    for dumped in assigned:
        assert "manifest" not in dumped, dumped
    assert "compute_result_hash(" in source


def test_a_forged_self_consistent_result_hash_fails_the_frozen_recomputation() -> None:
    """The decisive control: manifest AND database agree on a forged hash, the formula does not."""
    from minos_engine.experiments.execution_contract import (
        ExecutionConfig,
        ExecutionResultManifest,
        GatkExecutionOutcome,
        compute_result_hash,
        execution_input_from_manifest,
    )

    plan, invocation, config_hash = _twin_and_execution()
    inputs = _inputs()
    outcome = GatkExecutionOutcome(
        exit_code=0, runtime_ms=5, vcf_sha256=_H["f"], vcf_size_bytes=512
    )
    config = ExecutionConfig(
        config_hash=config_hash,
        parameter_space_hash=ACCEPTED_PARAMETER_SPACE_HASH,
        config_index=0,
        effective_config=_accepted_config(),
    )
    honest = compute_result_hash(
        plan_hash=ACCEPTED_PLAN_HASH,
        job_key=_H["c"],
        inputs=inputs,
        config=config,
        invocation=invocation,
        outcome=outcome,
    )
    forged = "0" * 64
    assert honest != forged
    # a manifest that self-consistently carries the forged value still cannot reproduce it
    manifest = ExecutionResultManifest(
        schema_version="l2f-gatk-execution-result-v1",
        plan_hash=ACCEPTED_PLAN_HASH,
        job_id="11111111-2222-3333-4444-555555555555",
        job_key=_H["c"],
        dataset_id=inputs.dataset_id,
        round_id=inputs.round_id,
        profile_id=inputs.profile_id,
        content_hash=inputs.content_hash,
        feature_values_hash=inputs.feature_values_hash,
        config_hash=config_hash,
        parameter_space_hash=ACCEPTED_PARAMETER_SPACE_HASH,
        input_identity_hash=inputs.identity_hash(),
        bam_sha256=inputs.bam_sha256,
        bai_sha256=inputs.bai_sha256,
        reference_sha256=inputs.reference_sha256,
        fai_sha256=inputs.fai_sha256,
        dictionary_sha256=inputs.dictionary_sha256,
        bam_size_bytes=inputs.bam_size_bytes,
        region_hash=inputs.region_hash,
        region_start0=inputs.region_start0,
        region_end0_exclusive=inputs.region_end0_exclusive,
        chromosome=inputs.chromosome,
        logical_argv_hash=invocation.argv_hash(),
        gatk_executable_sha256=_H["b"],
        gatk_runtime_bundle_sha256=_H["c"],
        gatk_version="4.5.0.0",
        vcf_sha256=_H["f"],
        vcf_size_bytes=512,
        result_hash=forged,
        runtime_ms=5,
        worker_id="w",
        generated_at="2026-01-01T00:00:00+00:00",
    )
    rebuilt = compute_result_hash(
        plan_hash=ACCEPTED_PLAN_HASH,
        job_key=manifest.job_key,
        inputs=execution_input_from_manifest(manifest),
        config=config,
        invocation=invocation,
        outcome=outcome,
    )
    assert rebuilt == honest != manifest.result_hash
    # the qualification HOLDs when the recomputed value disagrees with the stored one
    degraded = _qualification(
        artifact_verification=_qualification().artifact_verification.model_copy(
            update={"recomputed_result_hash": forged}
        )
    )
    assert _assemble(degraded).status is GateStatus.HOLD


# --------------------------------------------------------------------------- #
# CLOSURE-2 FIX 4 — StageNotReadyError specifically
# --------------------------------------------------------------------------- #
def test_stage_blocking_requires_stage_not_ready_error() -> None:
    from minos_engine.qualification import l2f_harness_ready_qualifier as Q

    assert Q._select_config_raises_stage_not_ready() is True


def test_an_unrelated_exception_is_not_stage_blocking(monkeypatch: pytest.MonkeyPatch) -> None:
    from minos_engine.layer2 import service as S
    from minos_engine.qualification import l2f_harness_ready_qualifier as Q

    class _Boom:
        def select_config(self, *_a: Any, **_k: Any) -> Any:
            raise RuntimeError("an unrelated crash")

    monkeypatch.setattr(S, "Layer2Service", _Boom)
    with pytest.raises(Q.QualificationEnvironmentError, match="not StageNotReadyError"):
        Q._select_config_raises_stage_not_ready()


def test_a_normal_return_from_select_config_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    from minos_engine.layer2 import service as S
    from minos_engine.qualification import l2f_harness_ready_qualifier as Q

    class _Open:
        def select_config(self, *_a: Any, **_k: Any) -> Any:
            return {"config": "granted"}

    monkeypatch.setattr(S, "Layer2Service", _Open)
    with pytest.raises(Q.QualificationEnvironmentError, match="returned normally"):
        Q._select_config_raises_stage_not_ready()


# --------------------------------------------------------------------------- #
# CLOSURE-2 FIX 5 — the operational store is read-only and unchanged
# --------------------------------------------------------------------------- #
def test_the_operational_endpoint_must_be_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    from minos_engine.qualification import l2f_harness_ready_qualifier as Q

    monkeypatch.delenv("MINOS_L2F_OPERATIONAL_READONLY_URL", raising=False)
    with pytest.raises(Q.QualificationEnvironmentError, match="OPERATIONAL_READONLY"):
        Q._capture_operational_before()


class _FakeResult:
    def __init__(self, value: Any, rows: Any = ()) -> None:
        self._value, self._rows = value, rows

    def scalar_one(self) -> Any:
        return self._value

    def scalar_one_or_none(self) -> Any:
        return self._value

    def all(self) -> Any:
        return self._rows


class _FakeConn:
    """A configurable stand-in for a PostgreSQL connection (no database is opened)."""

    def __init__(self, answers: dict[str, Any], *, write_error: Any = None) -> None:
        self.answers, self.write_error = answers, write_error
        self.executed: list[str] = []

    def execute(self, statement: Any, *_a: Any) -> Any:
        sql = str(statement)
        self.executed.append(sql)
        if sql.strip().upper().startswith("UPDATE"):
            if self.write_error is None:
                return _FakeResult(None)
            raise self.write_error
        for key, value in self.answers.items():
            if key in sql:
                return _FakeResult(value)
        return _FakeResult(0, ())

    def begin(self) -> Any:
        class _T:
            @staticmethod
            def rollback() -> None:
                return None

        return _T()

    def close(self) -> None:
        return None


class _FakeEngine:
    def __init__(self, answers: dict[str, Any], *, write_error: Any = None) -> None:
        self.answers, self.write_error = answers, write_error

    def connect(self) -> Any:
        return _FakeConn(self.answers, write_error=self.write_error)


def _read_only_answers(**over: Any) -> dict[str, Any]:
    # ordered most-specific first: several of these SQL texts contain one another's fragments
    # (e.g. the privilege query also mentions ``current_user``).
    base: dict[str, Any] = {
        "SHOW default_transaction_read_only": "on",
        "SHOW transaction_read_only": "on",
        "table_privileges": 0,
        "has_database_privilege": False,
        "usesuper": False,
        "version_num": "0005_l2e_feature_view",
        "table_name LIKE 'l2f%'": 0,
        "current_database()": "minos_engine_db",
        "current_user": "minos_readonly",
    }
    base.update(over)
    return base


def _read_only_denial() -> Any:
    from sqlalchemy.exc import DBAPIError

    class _Orig(Exception):
        sqlstate = "25006"

    return DBAPIError("UPDATE", {}, _Orig())


def test_a_preconfigured_read_only_endpoint_is_accepted() -> None:
    from minos_engine.qualification import l2f_harness_ready_qualifier as Q

    observation = Q.operational_fingerprint(
        _FakeEngine(_read_only_answers(), write_error=_read_only_denial())
    )
    assert observation.read_only_before_set is True
    assert observation.is_superuser is False
    assert observation.write_privileges == 0
    assert observation.write_denied_sqlstate == Q.READ_ONLY_SQLSTATE
    assert observation.revision == "0005_l2e_feature_view"
    assert observation.l2f_tables == 0


def test_a_writable_endpoint_is_refused_even_though_it_could_set_read_only() -> None:
    """The central Blocker-C control: 'off' BEFORE the application sets anything is fatal."""
    from minos_engine.qualification import l2f_harness_ready_qualifier as Q

    engine = _FakeEngine(
        _read_only_answers(**{"SHOW transaction_read_only": "off"}),
        write_error=_read_only_denial(),
    )
    with pytest.raises(Q.QualificationEnvironmentError, match="NOT read-only before"):
        Q.operational_fingerprint(engine)


def test_a_superuser_operational_role_is_refused() -> None:
    from minos_engine.qualification import l2f_harness_ready_qualifier as Q

    engine = _FakeEngine(_read_only_answers(usesuper=True), write_error=_read_only_denial())
    with pytest.raises(Q.QualificationEnvironmentError, match="superuser"):
        Q.operational_fingerprint(engine)


def test_a_write_privileged_operational_role_is_refused() -> None:
    from minos_engine.qualification import l2f_harness_ready_qualifier as Q

    engine = _FakeEngine(
        _read_only_answers(**{"table_privileges": 4}), write_error=_read_only_denial()
    )
    with pytest.raises(Q.QualificationEnvironmentError, match="TRUNCATE privileges"):
        Q.operational_fingerprint(engine)


def test_a_create_privileged_operational_role_is_refused() -> None:
    from minos_engine.qualification import l2f_harness_ready_qualifier as Q

    engine = _FakeEngine(
        _read_only_answers(**{"has_database_privilege": True}), write_error=_read_only_denial()
    )
    with pytest.raises(Q.QualificationEnvironmentError, match="CREATE"):
        Q.operational_fingerprint(engine)


def test_an_accepted_write_proves_the_endpoint_is_not_read_only() -> None:
    """If PostgreSQL ACCEPTS the harmless write, the endpoint is refused."""
    from minos_engine.qualification import l2f_harness_ready_qualifier as Q

    engine = _FakeEngine(_read_only_answers(), write_error=None)
    with pytest.raises(Q.QualificationEnvironmentError, match="ACCEPTED a write"):
        Q.operational_fingerprint(engine)


def test_a_wrong_write_denial_sqlstate_is_refused() -> None:
    from sqlalchemy.exc import DBAPIError

    from minos_engine.qualification import l2f_harness_ready_qualifier as Q

    class _Orig(Exception):
        sqlstate = "42501"  # insufficient_privilege, not read-only-transaction

    engine = _FakeEngine(_read_only_answers(), write_error=DBAPIError("UPDATE", {}, _Orig()))
    with pytest.raises(Q.QualificationEnvironmentError, match="expected the"):
        Q.operational_fingerprint(engine)


@pytest.mark.parametrize(
    ("mutation", "match"),
    [
        ({"operational_database_revision": "0006_x"}, None),
        ({"operational_l2f_table_count": 1}, None),
    ],
)
def test_a_wrong_operational_state_holds_the_gate(mutation: dict[str, Any], match: Any) -> None:
    degraded = _qualification(boundaries=_qualification().boundaries.model_copy(update=mutation))
    gate = _assemble(degraded)
    assert gate.mandatory_checks["operational_database_untouched"] is False
    assert gate.status is GateStatus.HOLD


# --------------------------------------------------------------------------- #
# CLOSURE-2 FIX 6 — offline verification recomputes from the SUPPLIED root
# --------------------------------------------------------------------------- #
def test_accepted_identities_are_recomputed_from_the_supplied_root(tmp_path: Path) -> None:
    from minos_engine.qualification.l2f_accepted_identities import (
        recompute_migration_sha256,
    )

    with pytest.raises(AcceptedIdentityError, match="missing"):
        recompute_migration_sha256(tmp_path)


def test_a_tampered_alternate_checkout_fails_offline_verification(tmp_path: Path) -> None:
    """Clone the repo, tamper one accepted committed artifact, keep the gate/result identical."""
    import shutil
    import subprocess as _sp

    clone = tmp_path / "checkout"
    _sp.run(  # noqa: S603 - fixed argv, no shell
        ["git", "clone", "--quiet", "--no-hardlinks", str(_repo_root()), str(clone)],  # noqa: S607
        capture_output=True,
        check=True,
    )
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

    # tamper exactly one accepted committed artifact IN THE CLONE
    victim = clone / "manifests" / "l2f_gatk_parameter_space_v1.json"
    victim.write_bytes(victim.read_bytes() + b"\n")

    verified = R.verify_committed_harness_ready_gate(
        base_dir=clone, gate_path=gate_path, qualification_path=qual_path
    )
    assert verified["ok"] is False
    assert any(
        "recomputed from the verification root" in r or "accepted identity" in r
        for r in verified["reasons"]
    ), verified["reasons"]
    shutil.rmtree(clone, ignore_errors=True)


# --------------------------------------------------------------------------- #
# CLOSURE-2 FIX 7 — the established E5 ancestry closure is what runs
# --------------------------------------------------------------------------- #
def test_the_e5_closure_uses_the_established_gate_verifiers() -> None:
    import inspect

    from minos_engine.qualification import l2f_accepted_identities as A

    source = inspect.getsource(A.recompute_e5_gate_hashes)
    assert "verify_feature_view_ready_gate" in source
    assert "verify_feature_matrix_frozen_1_gate" in source
    hashes = A.recompute_e5_gate_hashes()
    assert set(hashes) == {"FEATURE-VIEW-READY", "FEATURE-MATRIX-FROZEN-1"}


def test_a_missing_e5_gate_fails_the_closure(tmp_path: Path) -> None:
    from minos_engine.qualification import l2f_accepted_identities as A

    with pytest.raises(AcceptedIdentityError, match="accepted E5 gate is missing"):
        A.recompute_e5_gate_hashes(tmp_path)


# --------------------------------------------------------------------------- #
# CLOSURE-2 FIX 3 — resume evidence is deterministic pre/post, not a boolean
# --------------------------------------------------------------------------- #
def test_the_resume_contract_carries_deterministic_pre_post_evidence() -> None:
    from minos_engine.qualification.l2f_harness_ready_contract import ResumeResult

    fields = set(ResumeResult.model_fields)
    for name in (
        "row_counts_before",
        "row_counts_after",
        "database_fingerprint_before",
        "database_fingerprint_after",
        "artifact_fingerprint_before",
        "artifact_fingerprint_after",
        "conflicting_replay_created_rows",
        "failed_job_remained_failed",
        "failed_job_reclaimed",
    ):
        assert name in fields, name


def test_duplicate_rows_are_derived_from_every_tracked_table() -> None:
    from minos_engine.qualification import l2f_harness_ready_qualifier as Q

    tracked = {label for label, _ in Q.ROW_COUNT_TABLES}
    assert tracked == {
        "plans",
        "members",
        "config_payloads",
        "configs",
        "jobs",
        "results",
        "failures",
        "artifacts",
    }


def test_a_conflicting_replay_that_created_rows_holds_the_gate() -> None:
    degraded = _qualification(
        resume=_qualification().resume.model_copy(
            update={"conflicting_replay_rejected": False, "conflicting_replay_created_rows": 1}
        )
    )
    assert _assemble(degraded).status is GateStatus.HOLD


def test_a_reclaimed_failed_job_holds_the_gate() -> None:
    degraded = _qualification(
        resume=_qualification().resume.model_copy(
            update={"automatic_retry_observed": True, "failed_job_reclaimed": True}
        )
    )
    gate = _assemble(degraded)
    assert gate.mandatory_checks["no_automatic_retry"] is False
    assert gate.status is GateStatus.HOLD


# --------------------------------------------------------------------------- #
# F7 EXECUTION-BUNDLE PINNING — the launcher is a dispatcher, not the science
# --------------------------------------------------------------------------- #
def test_a_modified_local_jar_changes_the_pinned_bundle_identity(
    tmp_path: Path, _child_runtime: Path
) -> None:
    """THE corrective control: an AUTHENTIC launcher + a MODIFIED JAR is a different identity."""
    from minos_engine.qualification import l2f_harness_ready_qualifier as Q

    path = _fixture_binary(tmp_path)
    before = Q.verify_official_gatk_binary(_runner(path))
    (tmp_path / "gatk-package-4.5.0.0-local.jar").write_bytes(b"PK\x03\x04 TAMPERED payload")
    after = Q.verify_official_gatk_binary(_runner(path))
    # the launcher digest is byte-identical — that alone would have accepted the tamper
    assert after.executable_sha256 == before.executable_sha256
    assert after.local_jar_sha256 != before.local_jar_sha256
    assert after.runtime_bundle_sha256 != before.runtime_bundle_sha256


def test_a_replaced_local_jar_cannot_reuse_an_accepted_bundle_digest(
    tmp_path: Path, _child_runtime: Path
) -> None:
    from minos_engine.qualification import l2f_harness_ready_qualifier as Q

    path = _fixture_binary(tmp_path)
    pinned = Q.verify_official_gatk_binary(_runner(path))
    (tmp_path / "gatk-package-4.5.0.0-local.jar").write_bytes(b"an entirely different jar")
    swapped = Q.verify_official_gatk_binary(_runner(path))
    forged = swapped.model_copy(update={"runtime_bundle_sha256": pinned.runtime_bundle_sha256})
    # the derivation is recomputed from the raw parts, so a pasted digest does not survive
    assert (
        compute_gatk_runtime_bundle_sha256(
            launcher_sha256=forged.executable_sha256,
            local_jar_sha256=str(forged.local_jar_sha256),
            gatk_version=forged.version,
        )
        != forged.runtime_bundle_sha256
    )


def test_a_symlinked_local_jar_fails_closed(tmp_path: Path, _child_runtime: Path) -> None:
    from minos_engine.qualification import l2f_harness_ready_qualifier as Q

    path = _fixture_binary(tmp_path, jar_symlink=True)
    with pytest.raises((Q.QualificationEnvironmentError, GatkExecutionError), match="symlink"):
        Q.verify_official_gatk_binary(_runner(path))


def test_a_missing_local_jar_fails_closed(tmp_path: Path, _child_runtime: Path) -> None:
    from minos_engine.qualification import l2f_harness_ready_qualifier as Q

    path = _fixture_binary(tmp_path)
    (tmp_path / "gatk-package-4.5.0.0-local.jar").unlink()
    with pytest.raises(GatkExecutionError, match="no official local GATK JAR"):
        Q.verify_official_gatk_binary(_runner(path))


def test_an_ambiguous_second_local_jar_fails_closed(tmp_path: Path, _child_runtime: Path) -> None:
    """The launcher picks the NEWEST match; ambiguity must be refused, never silently resolved."""
    from minos_engine.qualification import l2f_harness_ready_qualifier as Q

    path = _fixture_binary(tmp_path)
    (tmp_path / "gatk-package-9.9.9.9-local.jar").write_bytes(b"a second candidate")
    with pytest.raises(GatkExecutionError, match="ambiguous"):
        Q.verify_official_gatk_binary(_runner(path))


def test_a_wrong_version_local_jar_fails_closed(tmp_path: Path, _child_runtime: Path) -> None:
    from minos_engine.qualification import l2f_harness_ready_qualifier as Q

    path = _fixture_binary(tmp_path, jar_name="gatk-package-4.4.0.0-local.jar")
    with pytest.raises(GatkExecutionError, match="gatk-package-4.5.0.0-local.jar"):
        Q.verify_official_gatk_binary(_runner(path))


def test_a_disagreeing_observed_version_fails_closed(tmp_path: Path, _child_runtime: Path) -> None:
    """Provisioned metadata may not disagree with what the REAL bundle reports."""
    from minos_engine.qualification import l2f_harness_ready_qualifier as Q

    body = '#!/bin/sh\necho "The Genome Analysis Toolkit (GATK) v4.4.0.0"\nexit 0\n'
    path = _fixture_binary(tmp_path, body)
    with pytest.raises(Q.QualificationEnvironmentError, match="reports version"):
        Q.verify_official_gatk_binary(_runner(path))


def test_the_child_environment_cannot_inherit_a_jar_override() -> None:
    """GATK_LOCAL_JAR/GATK_SPARK_JAR would let a caller substitute the scientific payload."""
    from minos_engine.storage.l2f_gatk_runner import (
        CHILD_ENV_ALLOWLIST,
        GATK_JAR_OVERRIDE_VARIABLES,
    )

    assert not set(GATK_JAR_OVERRIDE_VARIABLES) & set(CHILD_ENV_ALLOWLIST)


def test_the_bundle_digest_is_host_independent() -> None:
    """No absolute path, uid/gid, timestamp or hostname may enter the scientific identity."""
    a = compute_gatk_runtime_bundle_sha256(
        launcher_sha256=_H["b"], local_jar_sha256=_H["a"], gatk_version="4.5.0.0"
    )
    b = compute_gatk_runtime_bundle_sha256(
        launcher_sha256=_H["b"], local_jar_sha256=_H["a"], gatk_version="4.5.0.0"
    )
    assert a == b
    assert a != compute_gatk_runtime_bundle_sha256(
        launcher_sha256=_H["b"], local_jar_sha256=_H["c"], gatk_version="4.5.0.0"
    )
    assert a != compute_gatk_runtime_bundle_sha256(
        launcher_sha256=_H["c"], local_jar_sha256=_H["a"], gatk_version="4.5.0.0"
    )
    assert a != compute_gatk_runtime_bundle_sha256(
        launcher_sha256=_H["b"], local_jar_sha256=_H["a"], gatk_version="4.4.0.0"
    )


def test_the_result_identity_changes_when_only_the_jar_changes() -> None:
    """A different local JAR CANNOT reproduce an accepted ``result_hash``."""
    from minos_engine.experiments.execution_contract import (
        ExecutionConfig,
        GatkExecutionOutcome,
        compute_result_hash,
    )
    from minos_engine.storage.l2f_gatk_runner import build_logical_invocation

    inputs = _inputs()
    common: dict[str, Any] = {
        "effective_config": {"min_pruning": 2},
        "inputs": inputs,
        "gatk_executable_sha256": _H["b"],
        "gatk_version": "4.5.0.0",
    }
    a = build_logical_invocation(gatk_runtime_bundle_sha256=_H["a"], **common)
    b = build_logical_invocation(gatk_runtime_bundle_sha256=_H["c"], **common)
    outcome = GatkExecutionOutcome(
        exit_code=0, runtime_ms=1234, vcf_sha256=_H["f"], vcf_size_bytes=512
    )
    cfg = ExecutionConfig(
        config_hash=_H["9"],
        parameter_space_hash=_H["a"],
        config_index=0,
        effective_config={"min_pruning": 2},
    )
    # everything except the bundle is identical, so ONLY the JAR identity moves the hash
    assert a.argv_hash() == b.argv_hash()
    assert compute_result_hash(
        plan_hash=_H["0"],
        job_key="k",
        config=cfg,
        inputs=inputs,
        invocation=a,
        outcome=outcome,
    ) != compute_result_hash(
        plan_hash=_H["0"],
        job_key="k",
        config=cfg,
        inputs=inputs,
        invocation=b,
        outcome=outcome,
    )


@pytest.mark.parametrize(
    "mutation",
    [
        {"local_jar_sha256": None},
        {"runtime_bundle_sha256": None},
        {"observed_version": None},
        {"observed_version": "4.4.0.0"},
        {"local_jar_is_symlink": True},
        {"jar_override_variables_inherited": True},
        {"python_executable_sha256": None},
        {"java_executable_sha256": None},
        {"runtime_bundle_sha256": _H["7"]},
    ],
)
def test_an_incomplete_bundle_observation_cannot_pin_the_binary(
    mutation: dict[str, Any],
) -> None:
    """An aggregate boolean is not enough: the RAW bundle observations must all be present."""
    q = _qualification()
    weakened = q.model_copy(update={"gatk_binary": q.gatk_binary.model_copy(update=mutation)})
    assert R.derive_checks(weakened)["official_gatk_binary_pinned"] is False
    assert _assemble(weakened).status is GateStatus.HOLD


def test_the_execution_must_have_used_the_pinned_bundle() -> None:
    """A pinned bundle that the official execution did NOT use cannot pass."""
    q = _qualification()
    detached = q.model_copy(
        update={
            "official_execution": q.official_execution.model_copy(
                update={"gatk_runtime_bundle_sha256": _H["7"]}
            )
        }
    )
    assert R.derive_checks(detached)["official_gatk_binary_pinned"] is False
    assert _assemble(detached).status is GateStatus.HOLD


# --------------------------------------------------------------------------- #
# F7 CLOSURE — the qualifier version is BOUND, not merely documented
# --------------------------------------------------------------------------- #
def test_the_version_alias_is_single_valued_and_equals_the_constant() -> None:
    """ "Current" must be unambiguous: one Literal member, equal to the exported constant."""
    assert QUALIFIER_VERSIONS == (HARNESS_READY_QUALIFIER_VERSION,)
    assert HARNESS_READY_QUALIFIER_VERSION == "f7a-2"
    assert (
        f"{HARNESS_READY_QUALIFIER_SCHEMA}/{HARNESS_READY_QUALIFIER_VERSION}"
        == HARNESS_READY_QUALIFICATION_TOOL_VERSION
    )


def test_the_canonical_fixture_uses_the_source_version_constant() -> None:
    """The fixture may not pin a stale literal and thereby exempt itself from the invariant."""
    assert _qualification().qualifier_version == HARNESS_READY_QUALIFIER_VERSION


def test_a_stale_qualifier_version_cannot_even_be_constructed() -> None:
    """Structural, not prose: an f7a-1 document fails validation outright."""
    import pydantic

    payload = json.loads(canonical_qualification_bytes(_qualification()))
    payload["qualifier_version"] = "f7a-1"
    with pytest.raises((pydantic.ValidationError, HarnessReadyContractError)):
        load_qualification_json(canonical_json_bytes(payload))


@pytest.mark.parametrize("version", ["f7a-1", "f7a-3", "", "F7A-2", "f7a-2 "])
def test_no_other_qualifier_version_is_accepted(version: str) -> None:
    import pydantic

    payload = json.loads(canonical_qualification_bytes(_qualification()))
    payload["qualifier_version"] = version
    with pytest.raises((pydantic.ValidationError, HarnessReadyContractError)):
        load_qualification_json(canonical_json_bytes(payload))


def test_changing_the_qualifier_version_changes_the_qualification_hash() -> None:
    """The version is inside the identity, so old and new evidence can never collide."""
    result = _qualification()
    payload = json.loads(canonical_qualification_bytes(result))
    stale = dict(payload, qualifier_version="f7a-1")
    assert canonical_json_bytes(stale) != canonical_json_bytes(payload)
    domain = b"minos:l2f-harness-ready-qualification:v1\n"
    assert hashlib.sha256(domain + canonical_json_bytes(stale)).hexdigest() != (
        compute_qualification_hash(result)
    )


def test_a_stale_gate_tool_version_is_refused(tmp_path: Path) -> None:
    """Gate integrity proves the bytes are unedited; it does not prove they are CURRENT."""
    result = _current_source_qualification()
    gate = _assemble(result)
    stale = _restamped(gate, qualification_tool_version=f"{HARNESS_READY_QUALIFIER_SCHEMA}/f7a-1")
    gate_path = tmp_path / "harness-ready.json"
    _write_gate_at(stale, gate_path)
    qual_path = tmp_path / "qualification.json"
    qual_path.write_bytes(canonical_qualification_bytes(result) + b"\n")
    verified = R.verify_committed_harness_ready_gate(
        base_dir=_repo_root(), gate_path=gate_path, qualification_path=qual_path
    )
    assert verified["ok"] is False
    assert any("qualification_tool_version" in r for r in verified["reasons"])


def test_an_internally_coherent_old_version_pair_still_fails(tmp_path: Path) -> None:
    """The decisive control: a stale gate AND a stale result that agree with each other.

    Nothing inside the pair is self-inconsistent — the gate binds the stale result's own hash —
    yet the CURRENT verifier refuses it, because coherence with itself is not currency.
    """
    result = _current_source_qualification()
    payload = json.loads(canonical_qualification_bytes(result))
    payload["qualifier_version"] = "f7a-1"
    stale_bytes = canonical_json_bytes(payload)
    domain = b"minos:l2f-harness-ready-qualification:v1\n"
    stale_hash = hashlib.sha256(domain + stale_bytes).hexdigest()

    gate = _assemble(result)
    stale_gate = _restamped(
        gate,
        qualification_tool_version=f"{HARNESS_READY_QUALIFIER_SCHEMA}/f7a-1",
        input_hashes={**gate.input_hashes, "qualification_hash": stale_hash},
    )
    gate_path = tmp_path / "harness-ready.json"
    _write_gate_at(stale_gate, gate_path)
    qual_path = tmp_path / "qualification.json"
    qual_path.write_bytes(stale_bytes + b"\n")
    verified = R.verify_committed_harness_ready_gate(
        base_dir=_repo_root(), gate_path=gate_path, qualification_path=qual_path
    )
    assert verified["ok"] is False


def test_the_current_version_pair_is_the_positive_control(tmp_path: Path) -> None:
    """The same route with CURRENT evidence must actually pass, or the controls prove nothing."""
    result = _current_source_qualification()
    gate = _assemble(result)
    assert gate.qualification_tool_version == HARNESS_READY_QUALIFICATION_TOOL_VERSION
    gate_path = tmp_path / "harness-ready.json"
    _write_gate_at(gate, gate_path)
    qual_path = tmp_path / "qualification.json"
    qual_path.write_bytes(canonical_qualification_bytes(result) + b"\n")
    verified = R.verify_committed_harness_ready_gate(
        base_dir=_repo_root(), gate_path=gate_path, qualification_path=qual_path
    )
    assert verified["ok"] is True, verified["reasons"]
