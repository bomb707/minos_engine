"""Issuing and verifying SAFE-CONTROLLER-FROZEN.

The single most important property under test is negative: a document containing twenty-nine true
booleans must not be able to mint this gate. The superseded v2 qualification contains several
identically named corrected checks, all true, so "every required name is present and true" is not
authority — and if it were, v2 would mint the gate. Every path here therefore runs forwards from
the accepted bytes.
"""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from minos_engine.common.canonical_json import canonical_json_bytes
from minos_engine.common.errors import ContractValidationError
from minos_engine.gates.required_checks import required_checks_for
from minos_engine.layer2.safe_controller_gate import (
    ACCEPTED_V3_FILE_SHA256,
    ACCEPTED_V3_IDENTITY,
    ACCEPTED_V3_SOURCE_COMMIT,
    ACCEPTED_V3_SOURCE_TREE,
    CAPABILITY_SCOPE,
    NOT_AUTHORIZED,
    SAFE_CONTROLLER_FROZEN_GATE,
    AuthenticatedSafeControllerQualification,
    SafeControllerGateError,
    assemble_safe_controller_frozen_gate,
    authenticate_v3_qualification,
    derive_gate_checks,
    reverify_controller_authorities,
)
from minos_engine.layer2.safe_controller_qualification import (
    SAFE_CONTROLLER_QUALIFICATION_PATH,
)
from minos_engine.qualification.l2f_accepted_identities import repository_root

ISSUING_COMMIT = "0" * 40

#: A tampered gate may be refused by the gate CONTRACT (a PASS gate cannot carry a false or
#: unregistered check at all) or by this module's re-derivation. Both are refusals; naming them
#: keeps the tests from passing on an unrelated error.
REFUSALS = (SafeControllerGateError, ContractValidationError, ValidationError)


@pytest.fixture(scope="module")
def authenticated() -> AuthenticatedSafeControllerQualification:
    return authenticate_v3_qualification(repository_root())


@pytest.fixture(scope="module")
def authorities() -> dict[str, Any]:
    return reverify_controller_authorities(repository_root())


def _repo_copy(tmp_path: Path) -> Path:
    import shutil

    root = repository_root()
    target = tmp_path / "repo"
    target.mkdir(parents=True)
    for relative in ("reports", "manifests", "gates"):
        shutil.copytree(root / relative, target / relative)
    shutil.copytree(root / ".git", target / ".git", symlinks=True)
    return target


# ---------------------------------------------------------------------------------------- #
# authentication runs forwards from the bytes
# ---------------------------------------------------------------------------------------- #
def test_the_accepted_v3_report_authenticates(
    authenticated: AuthenticatedSafeControllerQualification,
) -> None:
    assert authenticated.identity == ACCEPTED_V3_IDENTITY
    assert authenticated.file_sha256 == ACCEPTED_V3_FILE_SHA256
    assert authenticated.source_commit == ACCEPTED_V3_SOURCE_COMMIT
    assert authenticated.source_tree == ACCEPTED_V3_SOURCE_TREE
    assert authenticated.report["capability_scope"] == CAPABILITY_SCOPE


def test_a_caller_cannot_mint_an_authenticated_qualification() -> None:
    """§B: the door is the bytes, not a constructor."""
    with pytest.raises(SafeControllerGateError, match="not evidence"):
        AuthenticatedSafeControllerQualification(
            object(),
            report={
                "checks": dict.fromkeys(required_checks_for(SAFE_CONTROLLER_FROZEN_GATE), True)
            },
            identity=ACCEPTED_V3_IDENTITY,
            file_sha256=ACCEPTED_V3_FILE_SHA256,
            source_commit=ACCEPTED_V3_SOURCE_COMMIT,
            source_tree=ACCEPTED_V3_SOURCE_TREE,
        )


def test_a_dictionary_of_true_checks_cannot_assemble_a_gate(
    authorities: dict[str, Any],
) -> None:
    with pytest.raises(SafeControllerGateError, match="not authority"):
        assemble_safe_controller_frozen_gate(
            {"everything": True},  # type: ignore[arg-type]
            authorities,
            engine_git_sha=ISSUING_COMMIT,
        )


@pytest.mark.parametrize("version", ["v1", "v2"])
def test_a_superseded_qualification_cannot_mint_the_gate(tmp_path: Path, version: str) -> None:
    """Even reshaped to carry every required name as true."""
    repo = _repo_copy(tmp_path)
    old = json.loads(
        (repo / f"reports/layer2/l2h-safe-controller-qualification-{version}.json").read_bytes()
    )
    old["checks"] = dict.fromkeys(required_checks_for(SAFE_CONTROLLER_FROZEN_GATE), True)
    (repo / SAFE_CONTROLLER_QUALIFICATION_PATH).write_bytes(canonical_json_bytes(old))
    with pytest.raises(SafeControllerGateError, match="hashes to"):
        authenticate_v3_qualification(repo)


def test_v2_carries_the_required_names_and_still_cannot_mint(tmp_path: Path) -> None:
    """The concrete reason 'all names true' is not authority."""
    v2 = json.loads(
        (
            repository_root() / "reports/layer2/l2h-safe-controller-qualification-v2.json"
        ).read_bytes()
    )
    required = required_checks_for(SAFE_CONTROLLER_FROZEN_GATE)
    shared = required & set(v2["checks"])
    assert shared, "v2 shares no required check names, which would make this test vacuous"
    assert all(v2["checks"][name] for name in shared)
    repo = _repo_copy(tmp_path)
    (repo / SAFE_CONTROLLER_QUALIFICATION_PATH).write_bytes(canonical_json_bytes(v2))
    with pytest.raises(SafeControllerGateError):
        authenticate_v3_qualification(repo)


def _tamper(repo: Path, mutate: Any) -> None:
    report = json.loads((repo / SAFE_CONTROLLER_QUALIFICATION_PATH).read_bytes())
    mutate(report)
    (repo / SAFE_CONTROLLER_QUALIFICATION_PATH).write_bytes(canonical_json_bytes(report))


def _change_schema(report: dict[str, Any]) -> None:
    report["schema_version"] = "l2h-safe-controller-qualification-v4"


def _change_source_commit(report: dict[str, Any]) -> None:
    report["observation"]["source_commit"] = "f" * 40


def _change_source_tree(report: dict[str, Any]) -> None:
    report["observation"]["source_tree"] = "e" * 40


def _widen_capability(report: dict[str, Any]) -> None:
    report["capability_scope"] = "FULL_CONTEXTUAL"


def _hold_status(report: dict[str, Any]) -> None:
    report["status"] = "HOLD"


def _claim_a_gate(report: dict[str, Any]) -> None:
    report["gate_issued"] = True


def _claim_activation(report: dict[str, Any]) -> None:
    report["service_activated"] = True


def _falsify_a_check(report: dict[str, Any]) -> None:
    report["checks"]["zero_invalid_configs"] = False


def _omit_a_check(report: dict[str, Any]) -> None:
    del report["checks"]["sealed_authorities_never_opened"]


def _substitute_unknown_check(report: dict[str, Any]) -> None:
    del report["checks"]["sealed_authorities_never_opened"]
    report["checks"]["everything_is_fine"] = True


def _claim_model_qualification(report: dict[str, Any]) -> None:
    report["observation"]["models_qualified_status"] = "PASS"


QUALIFICATION_TAMPERS = (
    ("schema_changed", _change_schema),
    ("source_commit_changed", _change_source_commit),
    ("source_tree_changed", _change_source_tree),
    ("capability_widened", _widen_capability),
    ("status_hold", _hold_status),
    ("claims_a_gate", _claim_a_gate),
    ("claims_activation", _claim_activation),
    ("required_check_false", _falsify_a_check),
    ("required_check_omitted", _omit_a_check),
    ("unknown_check_substituted", _substitute_unknown_check),
    ("claims_model_qualification", _claim_model_qualification),
)


@pytest.mark.parametrize(
    "label,mutate", QUALIFICATION_TAMPERS, ids=[t[0] for t in QUALIFICATION_TAMPERS]
)
def test_a_tampered_qualification_cannot_mint_the_gate(
    tmp_path: Path, label: str, mutate: Any
) -> None:
    """Every one changes the bytes, so the file SHA refuses before anything else is consulted."""
    repo = _repo_copy(tmp_path)
    _tamper(repo, mutate)
    with pytest.raises(SafeControllerGateError):
        authenticate_v3_qualification(repo)


def test_a_noncanonical_qualification_cannot_mint_the_gate(tmp_path: Path) -> None:
    repo = _repo_copy(tmp_path)
    report = json.loads((repo / SAFE_CONTROLLER_QUALIFICATION_PATH).read_bytes())
    (repo / SAFE_CONTROLLER_QUALIFICATION_PATH).write_bytes(
        json.dumps(report, indent=2).encode("utf-8")
    )
    with pytest.raises(SafeControllerGateError, match="hashes to"):
        authenticate_v3_qualification(repo)


# ---------------------------------------------------------------------------------------- #
# the checks are derived, and every authority is reverified
# ---------------------------------------------------------------------------------------- #
def test_the_derived_checks_are_exactly_the_registered_set(
    authenticated: AuthenticatedSafeControllerQualification, authorities: dict[str, Any]
) -> None:
    checks = derive_gate_checks(authenticated, authorities)
    assert set(checks) == required_checks_for(SAFE_CONTROLLER_FROZEN_GATE)
    assert len(checks) == 29
    assert all(checks.values())


def test_the_authorities_are_recomputed_not_copied(authorities: dict[str, Any]) -> None:
    assert (
        authorities["safe_controller_policy_hash"]
        == "638d634834c921f5ba00220caaca59c4b317368767c38bd50cafaad232241fa3"
    )
    assert (
        authorities["safe_baseline_config_hash"]
        == "157d88d1587c13be395c62d60e27d1becdada78fad45e65d883bc1190e51acea"
    )
    assert (
        authorities["baseline_selected_identity"]
        == "b13aef13fecf8e966184d03bad5ee0e6f096fb5649b30e336283e2f50f3eba38"
    )
    assert (
        authorities["baseline_qualified_gate_hash"]
        == "b9436bf3263925ebe187ed5550c7214cfa92bc75a0dd2607a7766103bfa6befa"
    )
    assert (
        authorities["baseline_qualification_hash"]
        == "afbcd418dee7f5521dc52b34e2c0b5d7bd31ea5f5d4ec3b1bf0768ab35babee8"
    )
    assert (
        authorities["parameter_space_hash"]
        == "b2d401918084d64023305d9262baf5011a89fe517bee4e0bd33af79fb14aee2e"
    )
    assert (
        authorities["l2g_v1_campaign_freeze_identity"]
        == "1c2039dec2f3fbb51a8058c947bbf8de9f9c6d235a133b5948aa6b33ac516673"
    )
    assert (
        authorities["l2g_v2_campaign_freeze_identity"]
        == "42310a97f2e13d516b57789bbfa0cd6ee6e44d7e732747dd44ace3aad9d33de5"
    )
    assert authorities["models_qualified_status"] == "HOLD_NO_TRAIN_PROMOTABLE_CONTEXTUAL_MODEL"
    assert authorities["models_qualified_gate_present"] is False
    assert authorities["controller_frozen_gate_present"] is False
    assert authorities["select_config_blocked"] is True


AUTHORITY_TAMPERS = (
    ("safe_config", "safe_baseline_config_hash"),
    ("parameter_space", "parameter_space_hash"),
    ("baseline_selected", "baseline_selected_identity"),
    ("baseline_qualified_gate", "baseline_qualified_gate_hash"),
    ("baseline_qualification", "baseline_qualification_hash"),
    ("l2g_v1_freeze", "l2g_v1_campaign_freeze_identity"),
    ("l2g_v2_freeze", "l2g_v2_campaign_freeze_identity"),
    ("policy", "safe_controller_policy_hash"),
)


@pytest.mark.parametrize("label,field", AUTHORITY_TAMPERS, ids=[t[0] for t in AUTHORITY_TAMPERS])
def test_a_disagreeing_authority_makes_a_check_false(
    authenticated: AuthenticatedSafeControllerQualification,
    authorities: dict[str, Any],
    label: str,
    field: str,
) -> None:
    """The report and this process must agree; a disagreement cannot produce a PASS gate."""
    forged = dict(authorities)
    forged[field] = "0" * 64
    checks = derive_gate_checks(authenticated, forged)
    assert not all(checks.values()), f"{field} disagreed and every check still passed"
    with pytest.raises(SafeControllerGateError, match="required check is false"):
        assemble_safe_controller_frozen_gate(authenticated, forged, engine_git_sha=ISSUING_COMMIT)


def test_a_models_qualified_gate_appearing_makes_the_gate_unissuable(
    authenticated: AuthenticatedSafeControllerQualification, authorities: dict[str, Any]
) -> None:
    forged = dict(authorities)
    forged["models_qualified_gate_present"] = True
    checks = derive_gate_checks(authenticated, forged)
    assert checks["models_qualified_remains_hold"] is False


def test_an_unblocked_service_makes_the_gate_unissuable(
    authenticated: AuthenticatedSafeControllerQualification, authorities: dict[str, Any]
) -> None:
    forged = dict(authorities)
    forged["select_config_blocked"] = False
    checks = derive_gate_checks(authenticated, forged)
    assert checks["select_config_public_boundary_blocked"] is False


def test_a_widened_policy_makes_the_gate_unissuable(
    authenticated: AuthenticatedSafeControllerQualification, authorities: dict[str, Any]
) -> None:
    forged = dict(authorities)
    forged["policy_allowed_modes"] = ["SAFE_BASELINE", "FULL_CONTEXTUAL"]
    checks = derive_gate_checks(authenticated, forged)
    assert checks["allowed_modes_exactly_safe_baseline"] is False


# ---------------------------------------------------------------------------------------- #
# the assembled gate
# ---------------------------------------------------------------------------------------- #
@pytest.fixture(scope="module")
def assembled(
    authenticated: AuthenticatedSafeControllerQualification, authorities: dict[str, Any]
) -> Any:
    return assemble_safe_controller_frozen_gate(
        authenticated, authorities, engine_git_sha=ISSUING_COMMIT
    )


def test_the_gate_names_the_qualified_controller_source_not_the_issuer(
    assembled: Any,
) -> None:
    """§G: the issuing commit must not quietly become the qualified one."""
    assert assembled.qualified_source_git_sha == ACCEPTED_V3_SOURCE_COMMIT
    assert assembled.qualified_source_tree_sha == ACCEPTED_V3_SOURCE_TREE
    assert assembled.engine_git_sha == ISSUING_COMMIT
    assert assembled.engine_git_sha != assembled.qualified_source_git_sha


def test_the_gate_is_pass_and_mode_scoped(assembled: Any) -> None:
    assert assembled.gate_name == SAFE_CONTROLLER_FROZEN_GATE
    assert assembled.status.value == "PASS"
    assert assembled.gate_name != "CONTROLLER-FROZEN"
    assert set(assembled.mandatory_checks) == required_checks_for(SAFE_CONTROLLER_FROZEN_GATE)
    assert all(assembled.mandatory_checks.values())
    assert NOT_AUTHORIZED == (
        "MODELS-QUALIFIED",
        "CONTROLLER-FROZEN",
        "BOUNDED",
        "FULL_CONTEXTUAL",
        "REFINEMENT",
    )


def test_the_gate_binds_the_authorities(assembled: Any) -> None:
    bound = assembled.input_hashes
    assert bound["qualification_identity"] == ACCEPTED_V3_IDENTITY
    assert bound["qualification_file_sha256"] == ACCEPTED_V3_FILE_SHA256
    assert bound["qualification_source_commit"] == ACCEPTED_V3_SOURCE_COMMIT
    assert bound["qualification_source_tree"] == ACCEPTED_V3_SOURCE_TREE
    assert (
        bound["safe_baseline_config_hash"]
        == "157d88d1587c13be395c62d60e27d1becdada78fad45e65d883bc1190e51acea"
    )
    assert (
        bound["phase_a_authority_identity"]
        == "9ad0ba48c80e7b305505fea201e93185deb15ae735338086d05b38afcf4deb3f"
    )
    assert (
        bound["ownership_corpus_identity"]
        == "9cc53b5d28c8a8da34c25095362c09d8cb1fb57533ff0a0b3e1fdf7000970b03"
    )


def test_the_gate_identity_holds_no_operational_value(assembled: Any) -> None:
    """§H: no timestamp, host, PID, path or credential in the scientific identity."""
    hashed = assembled.model_dump(mode="json", exclude={"gate_hash", "created_at"})
    body = json.dumps(hashed)
    for forbidden in ("password", "postgresql://", "/home/", "/tmp/", "hostname"):
        assert forbidden not in body
    # created_at is deliberately outside compute_hash
    assert "created_at" not in hashed


def test_the_gate_evidence_is_the_v3_report(assembled: Any) -> None:
    assert len(assembled.evidence) == 1
    item = assembled.evidence[0]
    assert item.path == SAFE_CONTROLLER_QUALIFICATION_PATH
    assert item.sha256 == ACCEPTED_V3_FILE_SHA256
    # the gate does not hash itself as evidence
    assert "safe-controller-frozen" not in item.path


def test_the_gate_hash_is_stable_and_excludes_the_timestamp(
    authenticated: AuthenticatedSafeControllerQualification, authorities: dict[str, Any]
) -> None:
    first = assemble_safe_controller_frozen_gate(
        authenticated, authorities, engine_git_sha=ISSUING_COMMIT, created_at="2026-01-01T00:00:00Z"
    )
    second = assemble_safe_controller_frozen_gate(
        authenticated, authorities, engine_git_sha=ISSUING_COMMIT, created_at="2027-06-15T12:34:56Z"
    )
    assert first.compute_hash() == second.compute_hash()


def test_a_short_engine_sha_is_refused(
    authenticated: AuthenticatedSafeControllerQualification, authorities: dict[str, Any]
) -> None:
    with pytest.raises(SafeControllerGateError, match="full git object name"):
        assemble_safe_controller_frozen_gate(authenticated, authorities, engine_git_sha="abc1234")


def test_the_registered_set_still_has_the_corrected_checks() -> None:
    required = required_checks_for(SAFE_CONTROLLER_FROZEN_GATE)
    for name in (
        "models_qualified_remains_hold",
        "allowed_modes_exactly_safe_baseline",
        "zero_contextual_model_loads",
        "zero_candidate_generation",
        "sealed_authorities_never_opened",
        "train_ownership_anchored_to_accepted_authority",
        "publication_identity_is_derived_not_declared",
        "select_config_public_boundary_blocked",
    ):
        assert name in required


def test_the_derivation_never_reads_the_reports_own_checks() -> None:
    """A structural proof, not a behavioural one: the deriver must not touch report['checks']."""
    import ast

    source = (repository_root() / "src/minos_engine/layer2/safe_controller_gate.py").read_text(
        encoding="utf-8"
    )
    tree = ast.parse(source)
    deriver = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "derive_gate_checks"
    )
    for node in ast.walk(deriver):
        if (
            isinstance(node, ast.Subscript)
            and isinstance(node.slice, ast.Constant)
            and node.slice.value == "checks"
        ):
            raise AssertionError("derive_gate_checks reads the report's own checks")


def test_the_service_is_still_blocked() -> None:
    from minos_engine.common.errors import StageNotReadyError
    from minos_engine.layer2.service import Layer2Service

    with pytest.raises(StageNotReadyError):
        Layer2Service().select_config(None)  # type: ignore[arg-type]


def test_no_contextual_gate_exists() -> None:
    for name in ("models-qualified.json", "controller-frozen.json"):
        assert not (repository_root() / "gates" / name).exists()


def test_the_qualification_reports_are_all_unchanged() -> None:
    expected = {
        "v1": "b0a3a16d2a411d18e708ef6add63a6832778e51cfb8454874763a0f5c8604453",
        "v2": "483f77c63938331b4930439a244e8db8992049d7a4fab75faf76629ed5975e9d",
        "v3": "90fb7b5f819eee2f414d8b55adfa7bbbd8164eadd874d3354393bb8f093ec7da",
    }
    for version, sha in expected.items():
        path = (
            repository_root() / f"reports/layer2/l2h-safe-controller-qualification-{version}.json"
        )
        assert hashlib.sha256(path.read_bytes()).hexdigest() == sha


def test_copy_does_not_leak_the_authenticated_report(
    authenticated: AuthenticatedSafeControllerQualification,
) -> None:
    borrowed = authenticated.report
    borrowed["status"] = "HOLD"
    assert authenticated.report["status"] == "PASS"
    assert copy.deepcopy(authenticated.observation)["decision_count"] == 200


# ---------------------------------------------------------------------------------------- #
# verifying the issued gate
# ---------------------------------------------------------------------------------------- #
def _issued_repo(tmp_path: Path) -> Path:
    """A copy carrying the real issued gate, so tampering is isolated."""
    return _repo_copy(tmp_path)


def test_the_committed_gate_verifies() -> None:
    from minos_engine.layer2.safe_controller_gate import verify_safe_controller_frozen_gate

    report = verify_safe_controller_frozen_gate(repository_root())
    assert report["ok"] is True
    assert report["gate_name"] == SAFE_CONTROLLER_FROZEN_GATE
    assert report["capability_scope"] == CAPABILITY_SCOPE
    assert report["check_count"] == 29
    assert report["qualified_source_git_sha"] == ACCEPTED_V3_SOURCE_COMMIT
    assert report["qualified_source_tree_sha"] == ACCEPTED_V3_SOURCE_TREE
    assert report["engine_git_sha"] != ACCEPTED_V3_SOURCE_COMMIT


def _write_gate_document(repo: Path, document: dict[str, Any]) -> None:
    from minos_engine.layer2.safe_controller_gate import SAFE_CONTROLLER_FROZEN_GATE_PATH

    (repo / SAFE_CONTROLLER_FROZEN_GATE_PATH).write_text(
        json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def _gate_document(repo: Path) -> dict[str, Any]:
    from minos_engine.layer2.safe_controller_gate import SAFE_CONTROLLER_FROZEN_GATE_PATH

    return dict(json.loads((repo / SAFE_CONTROLLER_FROZEN_GATE_PATH).read_bytes()))


def _rehash(document: dict[str, Any]) -> dict[str, Any]:
    """Repair the gate hash, so only re-derivation can catch the edit."""
    from minos_engine.gates.contracts import GateArtifact

    document["gate_hash"] = ""
    return GateArtifact.model_validate(document).model_dump(mode="json")


def _false_check(document: dict[str, Any]) -> None:
    document["mandatory_checks"]["zero_invalid_configs"] = False


def _omit_check(document: dict[str, Any]) -> None:
    del document["mandatory_checks"]["sealed_authorities_never_opened"]


def _unknown_check(document: dict[str, Any]) -> None:
    del document["mandatory_checks"]["sealed_authorities_never_opened"]
    document["mandatory_checks"]["everything_is_fine"] = True


def _rename_to_controller_frozen(document: dict[str, Any]) -> None:
    document["gate_name"] = "CONTROLLER-FROZEN"


def _qualified_source_is_the_evidence_commit(document: dict[str, Any]) -> None:
    document["qualified_source_git_sha"] = "f83b948a832c891f36b47110f50088cfc1ba0de6"


def _swap_an_input_hash(document: dict[str, Any]) -> None:
    document["input_hashes"]["safe_baseline_config_hash"] = "0" * 64


def _drop_evidence(document: dict[str, Any]) -> None:
    document["evidence"] = []


def _wrong_evidence_hash(document: dict[str, Any]) -> None:
    document["evidence"][0]["sha256"] = "1" * 64


GATE_TAMPERS = (
    ("required_check_false", _false_check),
    ("required_check_omitted", _omit_check),
    ("unknown_check_substituted", _unknown_check),
    ("renamed_to_controller_frozen", _rename_to_controller_frozen),
    ("qualified_source_replaced_with_evidence_commit", _qualified_source_is_the_evidence_commit),
    ("authority_binding_swapped", _swap_an_input_hash),
    ("evidence_dropped", _drop_evidence),
    ("evidence_hash_wrong", _wrong_evidence_hash),
)


@pytest.mark.parametrize("label,mutate", GATE_TAMPERS, ids=[t[0] for t in GATE_TAMPERS])
def test_a_tampered_gate_is_refused(tmp_path: Path, label: str, mutate: Any) -> None:
    """Each edit is rehashed, so only re-derivation from the evidence can refuse it."""
    from minos_engine.layer2.safe_controller_gate import verify_safe_controller_frozen_gate

    repo = _issued_repo(tmp_path)
    document = _gate_document(repo)
    mutate(document)
    try:
        document = _rehash(document)
    except REFUSALS:
        # some edits cannot even be re-validated as a gate; that is a refusal too
        _write_gate_document(repo, document)
        with pytest.raises(REFUSALS):
            verify_safe_controller_frozen_gate(repo)
        return
    _write_gate_document(repo, document)
    with pytest.raises(REFUSALS):
        verify_safe_controller_frozen_gate(repo)


def test_a_gate_with_a_tampered_hash_is_refused(tmp_path: Path) -> None:
    from minos_engine.layer2.safe_controller_gate import verify_safe_controller_frozen_gate

    repo = _issued_repo(tmp_path)
    document = _gate_document(repo)
    document["gate_hash"] = "9" * 64
    _write_gate_document(repo, document)
    with pytest.raises(REFUSALS):
        verify_safe_controller_frozen_gate(repo)


def test_tampered_evidence_bytes_are_refused(tmp_path: Path) -> None:
    """§I: the report's exact bytes are rehashed during verification."""
    from minos_engine.layer2.safe_controller_gate import verify_safe_controller_frozen_gate

    repo = _issued_repo(tmp_path)
    report = json.loads((repo / SAFE_CONTROLLER_QUALIFICATION_PATH).read_bytes())
    report["observation"]["decision_count"] = 199
    (repo / SAFE_CONTROLLER_QUALIFICATION_PATH).write_bytes(canonical_json_bytes(report))
    with pytest.raises(SafeControllerGateError):
        verify_safe_controller_frozen_gate(repo)


def test_a_models_qualified_artifact_appearing_refuses_the_gate(tmp_path: Path) -> None:
    """This mode-scoped gate may not stand beside a contextual-capability one."""
    from minos_engine.layer2.safe_controller_gate import verify_safe_controller_frozen_gate

    repo = _issued_repo(tmp_path)
    (repo / "gates/models-qualified.json").write_text("{}", encoding="utf-8")
    # the derived models_qualified_remains_hold check fails first, which is the same refusal
    with pytest.raises(SafeControllerGateError):
        verify_safe_controller_frozen_gate(repo)


def test_a_controller_frozen_artifact_appearing_refuses_the_gate(tmp_path: Path) -> None:
    from minos_engine.layer2.safe_controller_gate import verify_safe_controller_frozen_gate

    repo = _issued_repo(tmp_path)
    (repo / "gates/controller-frozen.json").write_text("{}", encoding="utf-8")
    with pytest.raises(SafeControllerGateError, match="contextual-capability gate exists"):
        verify_safe_controller_frozen_gate(repo)


def test_a_substituted_v1_or_v2_report_refuses_the_gate(tmp_path: Path) -> None:
    from minos_engine.layer2.safe_controller_gate import verify_safe_controller_frozen_gate

    for version in ("v1", "v2"):
        repo = _issued_repo(tmp_path / version)
        old = json.loads(
            (repo / f"reports/layer2/l2h-safe-controller-qualification-{version}.json").read_bytes()
        )
        old["checks"] = dict.fromkeys(required_checks_for(SAFE_CONTROLLER_FROZEN_GATE), True)
        (repo / SAFE_CONTROLLER_QUALIFICATION_PATH).write_bytes(canonical_json_bytes(old))
        with pytest.raises(SafeControllerGateError):
            verify_safe_controller_frozen_gate(repo)


def test_the_gate_is_the_only_issued_controller_gate() -> None:
    from minos_engine.layer2.safe_controller_gate import SAFE_CONTROLLER_FROZEN_GATE_PATH

    gates = sorted(p.name for p in (repository_root() / "gates").glob("*.json"))
    assert "safe-controller-frozen.json" in gates
    assert "controller-frozen.json" not in gates
    assert "models-qualified.json" not in gates
    assert (repository_root() / SAFE_CONTROLLER_FROZEN_GATE_PATH).is_file()
