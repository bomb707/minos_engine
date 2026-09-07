"""Round/profile ownership, decision publication, and the SAFE-controller qualification authority.

The negative half is the point. A request that names a round and a profile it cannot prove must be
refused outright — not degraded to a safe fallback, because "I could not authenticate you" and "you
asked for something no model can do" are different statements and must not produce the same result.
"""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from minos_engine.baseline.baseline_selected import SELECTED_CONFIG_HASH
from minos_engine.common.canonical_json import canonical_json_bytes
from minos_engine.gates.required_checks import required_checks_for
from minos_engine.layer2.contracts import (
    ArtifactIdentity,
    ComputeLimits,
    ControlMode,
    DecisionRequest,
    FallbackReason,
    Layer1ProfileReference,
    ParameterSpaceIdentity,
    RoundIdentity,
)
from minos_engine.layer2.decision_publication import (
    DECISION_PUBLICATION_DISPOSITION,
    DecisionPublicationError,
    decide_and_publish,
    publish_safe_decision,
    read_published_decision,
)
from minos_engine.layer2.round_profile_authority import (
    ARTIFACT_INVENTORY_PATH,
    OWNERSHIP_SCHEMA,
    SNAPSHOT_MEMBERS_PATH,
    RoundProfileAuthorityError,
    VerifiedRoundProfileAuthority,
    load_verified_round_profile_corpus,
)
from minos_engine.layer2.safe_controller import (
    VerifiedSafeBaselineAuthority,
    load_verified_safe_baseline_authority,
    safe_decision_manifest_identity,
    select_safe_baseline,
)
from minos_engine.layer2.safe_controller_qualification import (
    ADDITIONAL_CHECKS,
    ALL_CHECKS,
    MANDATORY_CHECKS,
    SafeControllerQualificationError,
    TrustedSafeControllerQualification,
    assemble_qualification_report,
    derive_checks,
    qualification_report_identity,
    run_safe_controller_qualification,
    verify_qualification_report,
)
from minos_engine.qualification.l2f_accepted_identities import repository_root


@pytest.fixture(scope="module")
def authority() -> VerifiedSafeBaselineAuthority:
    return load_verified_safe_baseline_authority(repo_root=repository_root())


@pytest.fixture(scope="module")
def ownership() -> VerifiedRoundProfileAuthority:
    return load_verified_round_profile_corpus(root=repository_root())


def _reference(owned: Any, **override: Any) -> Layer1ProfileReference:
    fields: dict[str, Any] = {
        "profile_id": owned.profile_id,
        "profile_manifest_hash": "0" * 64,
        "profile_manifest_sha256": owned.profile_manifest_sha256,
        "fingerprint_hash": owned.fingerprint_hash,
        "region_hash": owned.region_hash,
        "bam_sha256": owned.bam_sha256,
        "bai_sha256": owned.bai_sha256,
        "reference_sha256": owned.reference_sha256,
        "fai_sha256": owned.fai_sha256,
    }
    fields.update(override)
    return Layer1ProfileReference(**fields)


def _request(
    authority: VerifiedSafeBaselineAuthority,
    owned: Any,
    *,
    mode: ControlMode = ControlMode.SAFE_BASELINE,
    round_id: str | None = None,
    model_bundle_id: str | None = None,
    **override: Any,
) -> DecisionRequest:
    return DecisionRequest(
        round=RoundIdentity(round_id=round_id or owned.round_id),
        profile_ref=_reference(owned, **override),
        parameter_space=ParameterSpaceIdentity(parameter_space_hash=authority.parameter_space_hash),
        safe_baseline=ArtifactIdentity(
            uri=authority.baseline_uri, sha256=authority.baseline_config_hash
        ),
        controller_version="qualification-test",
        limits=ComputeLimits(
            remaining_seconds=30.0,
            wall_clock_budget_seconds=600.0,
            cpu_limit=2,
            memory_limit_bytes=1 << 30,
        ),
        model_bundle_id=model_bundle_id,
        requested_mode=mode,
    )


# ---------------------------------------------------------------------------------------- #
# the owned corpus
# ---------------------------------------------------------------------------------------- #
def test_the_corpus_is_the_frozen_snapshot(ownership: VerifiedRoundProfileAuthority) -> None:
    assert len(ownership) == 50
    assert (
        ownership.snapshot_hash
        == "cf717ebb44e76a3408e975e027b51139df28d643dd1616c5edbce3643182c4c7"
    )
    assert (
        ownership.registry_snapshot_hash
        == "3e60aa65aeed8969e29ebeef83024f6fa2285a13c155d7d6dc0c601d1e94f675"
    )
    assert len(ownership.corpus_identity) == 64
    # TEST is sealed and VALIDATION is unauthorised, so neither is enumerated at all
    assert all(ownership.owned(r).partition == "train" for r in ownership.rounds())
    assert ownership.skipped_partition_counts == {"test": 15, "validation": 10}


def test_a_caller_cannot_mint_a_corpus() -> None:
    with pytest.raises(RoundProfileAuthorityError, match="only be minted"):
        VerifiedRoundProfileAuthority(
            object(),
            by_round={},
            snapshot_hash="a" * 64,
            registry_snapshot_hash="b" * 64,
            corpus_identity="c" * 64,
        )


def test_the_ownership_authority_reuses_the_accepted_admission_surface() -> None:
    """§B: do not duplicate L2-D admission logic — call it."""
    import ast

    source = (repository_root() / "src/minos_engine/layer2/round_profile_authority.py").read_text(
        encoding="utf-8"
    )
    imported = {
        f"{node.module}.{alias.name}"
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.ImportFrom) and node.module
        for alias in node.names
    }
    assert "minos_engine.layer2.ingest.validation.validate_admission" in imported
    # and it must not have grown its own copy of the checks
    for reimplemented in ("manifest_profile_id_bound", "attestation_identity_bound", "m5_status"):
        assert reimplemented not in source


# ---------------------------------------------------------------------------------------- #
# §J ownership tamper matrix — every one an AUTHORITY failure, never a fallback
# ---------------------------------------------------------------------------------------- #
OWNERSHIP_TAMPERS = (
    "profile_id",
    "profile_manifest_sha256",
    "fingerprint_hash",
    "region_hash",
    "bam_sha256",
    "bai_sha256",
    "reference_sha256",
    "fai_sha256",
)


@pytest.mark.parametrize("field", OWNERSHIP_TAMPERS)
def test_a_field_from_another_owned_profile_is_refused(
    authority: VerifiedSafeBaselineAuthority,
    ownership: VerifiedRoundProfileAuthority,
    field: str,
) -> None:
    """Borrowed from a real neighbour, so nothing is caught by shape validation alone."""
    owned = ownership.owned(ownership.rounds()[0])
    other = ownership.owned(ownership.rounds()[1])
    request = _request(authority, owned, **{field: getattr(other, field)})
    with pytest.raises(RoundProfileAuthorityError, match=field):
        select_safe_baseline(request=request, authority=authority, ownership=ownership)


def test_an_unregistered_round_is_refused(
    authority: VerifiedSafeBaselineAuthority, ownership: VerifiedRoundProfileAuthority
) -> None:
    owned = ownership.owned(ownership.rounds()[0])
    with pytest.raises(RoundProfileAuthorityError, match="not in the accepted profile snapshot"):
        select_safe_baseline(
            request=_request(authority, owned, round_id="invented-round"),
            authority=authority,
            ownership=ownership,
        )


def test_a_profile_from_another_round_is_refused(
    authority: VerifiedSafeBaselineAuthority, ownership: VerifiedRoundProfileAuthority
) -> None:
    """A real profile, a real round — but not each other's."""
    owned = ownership.owned(ownership.rounds()[0])
    other = ownership.owned(ownership.rounds()[1])
    with pytest.raises(RoundProfileAuthorityError):
        select_safe_baseline(
            request=_request(authority, other, round_id=owned.round_id),
            authority=authority,
            ownership=ownership,
        )


def test_a_missing_manifest_sha_is_refused(
    authority: VerifiedSafeBaselineAuthority, ownership: VerifiedRoundProfileAuthority
) -> None:
    """The default-empty field is a compatibility shim, never an accepted request."""
    owned = ownership.owned(ownership.rounds()[0])
    with pytest.raises(RoundProfileAuthorityError, match="profile_manifest_sha256"):
        select_safe_baseline(
            request=_request(authority, owned, profile_manifest_sha256=""),
            authority=authority,
            ownership=ownership,
        )


def test_an_ownership_failure_is_never_a_safe_baseline_forced(
    authority: VerifiedSafeBaselineAuthority, ownership: VerifiedRoundProfileAuthority
) -> None:
    """The distinction that matters: unauthenticated is not degraded."""
    owned = ownership.owned(ownership.rounds()[0])
    other = ownership.owned(ownership.rounds()[1])
    for mode in ControlMode:
        with pytest.raises(RoundProfileAuthorityError):
            select_safe_baseline(
                request=_request(authority, owned, mode=mode, bam_sha256=other.bam_sha256),
                authority=authority,
                ownership=ownership,
            )


def _corpus_copy(tmp_path: Path) -> tuple[Path, Path]:
    """A coherent repository + corpus copy that can be tampered with in isolation."""
    import shutil

    from minos_engine.layer2.round_profile_authority import PROFILE_CORPUS_ROOT

    root = repository_root()
    repo = tmp_path / "repo"
    repo.mkdir()
    for relative in ("reports", "manifests", "gates"):
        shutil.copytree(root / relative, repo / relative)
    shutil.copytree(root / ".git", repo / ".git", symlinks=True)
    corpus = tmp_path / "corpus"
    shutil.copytree(PROFILE_CORPUS_ROOT, corpus)
    return repo, corpus


def test_the_untampered_copy_loads(tmp_path: Path) -> None:
    repo, corpus = _corpus_copy(tmp_path)
    assert len(load_verified_round_profile_corpus(root=repo, corpus_root=corpus)) == 50


def test_a_tampered_profile_document_is_refused(tmp_path: Path) -> None:
    """Bytes are checked against the frozen inventory before anything is parsed."""
    repo, corpus = _corpus_copy(tmp_path)
    members = json.loads((repo / SNAPSHOT_MEMBERS_PATH).read_bytes())
    victim = next(m for m in members["members"] if m["partition"] == "train")["round_id"]
    path = corpus / victim / "bam-profile-v1.json"
    document = json.loads(path.read_bytes())
    document["status"] = "INCOMPLETE"
    path.write_bytes(canonical_json_bytes(document))
    with pytest.raises(RoundProfileAuthorityError, match="hashes to"):
        load_verified_round_profile_corpus(root=repo, corpus_root=corpus)


def test_a_tampered_attestation_is_refused(tmp_path: Path) -> None:
    repo, corpus = _corpus_copy(tmp_path)
    members = json.loads((repo / SNAPSHOT_MEMBERS_PATH).read_bytes())
    victim = next(m for m in members["members"] if m["partition"] == "train")["round_id"]
    path = corpus / victim / "input-integrity-attestation-v1.json"
    attestation = json.loads(path.read_bytes())
    attestation["bam_sha256"] = "e" * 64
    path.write_bytes(canonical_json_bytes(attestation))
    with pytest.raises(RoundProfileAuthorityError):
        load_verified_round_profile_corpus(root=repo, corpus_root=corpus)


def test_an_inventory_rewritten_to_match_a_tampered_artifact_is_still_refused(
    tmp_path: Path,
) -> None:
    """Repairing the inventory does not repair the membership it disagrees with."""
    repo, corpus = _corpus_copy(tmp_path)
    members = json.loads((repo / SNAPSHOT_MEMBERS_PATH).read_bytes())
    victim = next(m for m in members["members"] if m["partition"] == "train")["round_id"]
    path = corpus / victim / "profile-manifest-v1.json"
    document = json.loads(path.read_bytes())
    document["windows_row_count"] = int(document["windows_row_count"]) + 1
    raw = canonical_json_bytes(document)
    path.write_bytes(raw)
    inventory = json.loads((repo / ARTIFACT_INVENTORY_PATH).read_bytes())
    for entry in inventory["entries"]:
        if entry["round_id"] == victim:
            entry["artifacts"]["profile-manifest-v1.json"] = {
                "sha256": hashlib.sha256(raw).hexdigest(),
                "size_bytes": len(raw),
            }
    (repo / ARTIFACT_INVENTORY_PATH).write_bytes(canonical_json_bytes(inventory))
    with pytest.raises(RoundProfileAuthorityError, match="snapshot recorded"):
        load_verified_round_profile_corpus(root=repo, corpus_root=corpus)


def test_a_missing_corpus_member_is_refused(tmp_path: Path) -> None:
    repo, corpus = _corpus_copy(tmp_path)
    members = json.loads((repo / SNAPSHOT_MEMBERS_PATH).read_bytes())
    victim = next(m for m in members["members"] if m["partition"] == "train")["round_id"]
    (corpus / victim / "bam-profile-v1.json").unlink()
    with pytest.raises(RoundProfileAuthorityError, match="missing"):
        load_verified_round_profile_corpus(root=repo, corpus_root=corpus)


# ---------------------------------------------------------------------------------------- #
# decision publication
# ---------------------------------------------------------------------------------------- #
def test_a_decision_publishes_content_addressed_and_reads_back(
    authority: VerifiedSafeBaselineAuthority,
    ownership: VerifiedRoundProfileAuthority,
    tmp_path: Path,
) -> None:
    owned = ownership.owned(ownership.rounds()[0])
    result, published = decide_and_publish(
        request=_request(authority, owned),
        authority=authority,
        ownership=ownership,
        output_root=tmp_path / "decisions",
    )
    assert published.path.name == f"{result.decision.decision_manifest_hash}.json"
    assert published.reused is False
    assert oct(published.path.stat().st_mode & 0o777) == "0o640"
    manifest = read_published_decision(
        identity=published.identity, output_root=tmp_path / "decisions"
    )
    assert safe_decision_manifest_identity(manifest) == published.identity
    assert manifest["selected_config_hash"] == SELECTED_CONFIG_HASH


def test_a_retry_converges_instead_of_conflicting(
    authority: VerifiedSafeBaselineAuthority,
    ownership: VerifiedRoundProfileAuthority,
    tmp_path: Path,
) -> None:
    """Idempotency is a property of the naming, not of a protocol."""
    owned = ownership.owned(ownership.rounds()[0])
    root = tmp_path / "decisions"
    first, published = decide_and_publish(
        request=_request(authority, owned),
        authority=authority,
        ownership=ownership,
        output_root=root,
    )
    second, again = decide_and_publish(
        request=_request(authority, owned),
        authority=authority,
        ownership=ownership,
        output_root=root,
    )
    assert first.decision == second.decision
    assert again.reused is True
    assert again.sha256 == published.sha256
    assert len(list(root.glob("*.json"))) == 1


def test_two_different_decisions_never_contend_for_one_name(
    authority: VerifiedSafeBaselineAuthority,
    ownership: VerifiedRoundProfileAuthority,
    tmp_path: Path,
) -> None:
    owned = ownership.owned(ownership.rounds()[0])
    root = tmp_path / "decisions"
    for mode in ControlMode:
        decide_and_publish(
            request=_request(authority, owned, mode=mode),
            authority=authority,
            ownership=ownership,
            output_root=root,
        )
    assert len(list(root.glob("*.json"))) == len(ControlMode)


def test_a_mutated_published_decision_is_refused_on_read(
    authority: VerifiedSafeBaselineAuthority,
    ownership: VerifiedRoundProfileAuthority,
    tmp_path: Path,
) -> None:
    owned = ownership.owned(ownership.rounds()[0])
    root = tmp_path / "decisions"
    _, published = decide_and_publish(
        request=_request(authority, owned),
        authority=authority,
        ownership=ownership,
        output_root=root,
    )
    manifest = json.loads(published.path.read_bytes())
    manifest["selected_config_hash"] = "0" * 64
    published.path.write_bytes(canonical_json_bytes(manifest))
    with pytest.raises(DecisionPublicationError, match="does not hash to the identity"):
        read_published_decision(identity=published.identity, output_root=root)


def test_publishing_different_bytes_under_one_identity_is_refused(tmp_path: Path) -> None:
    root = tmp_path / "decisions"
    identity = "a" * 64
    publish_safe_decision(manifest={"a": 1}, identity=identity, output_root=root)
    with pytest.raises(DecisionPublicationError, match="never name two decisions"):
        publish_safe_decision(manifest={"a": 2}, identity=identity, output_root=root)


def test_the_disposition_is_named_not_inferred() -> None:
    assert DECISION_PUBLICATION_DISPOSITION == (
        "FILE_PUBLISHED_DB_PERSISTENCE_DEFERRED_TO_ACTIVATION"
    )


def test_the_publication_path_touches_no_database() -> None:
    import ast

    source = (repository_root() / "src/minos_engine/layer2/decision_publication.py").read_text(
        encoding="utf-8"
    )
    imported = {
        node.module
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.ImportFrom) and node.module
    } | {
        alias.name
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    for forbidden in ("sqlalchemy", "psycopg", "minos_engine.storage"):
        assert not any(m == forbidden or m.startswith(forbidden + ".") for m in imported)


# ---------------------------------------------------------------------------------------- #
# the qualification authority
# ---------------------------------------------------------------------------------------- #
@pytest.fixture(scope="module")
def qualification(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    trusted = run_safe_controller_qualification(
        repo_root=repository_root(),
        output_root=tmp_path_factory.mktemp("l2h-decisions"),
    )
    return assemble_qualification_report(trusted)


def test_the_qualification_passes_over_the_whole_owned_corpus(
    qualification: dict[str, Any],
) -> None:
    report = verify_qualification_report(qualification)
    assert report["ok"] is True
    assert report["status"] == "PASS"
    assert report["capability_scope"] == "SAFE_BASELINE_ONLY"
    observation = qualification["observation"]
    assert observation["profile_count"] == 50
    assert observation["decision_count"] == 200
    assert observation["selected_config_distribution"] == {SELECTED_CONFIG_HASH: 200}
    assert observation["actual_mode_counts"] == {"SAFE_BASELINE": 200}
    assert observation["fallback_reason_counts"] == {
        FallbackReason.NONE.value: 50,
        FallbackReason.SAFE_BASELINE_FORCED.value: 150,
    }
    assert observation["admitted_partition"] == "train"
    assert observation["skipped_partition_counts"] == {"test": 15, "validation": 10}


def test_all_twenty_one_mandatory_checks_are_present_and_true(
    qualification: dict[str, Any],
) -> None:
    assert len(MANDATORY_CHECKS) == 21
    for name in MANDATORY_CHECKS:
        assert qualification["checks"][name] is True, name
    assert list(qualification["mandatory_checks"]) == list(MANDATORY_CHECKS)


def test_the_qualification_claims_no_model_qualification(qualification: dict[str, Any]) -> None:
    observation = qualification["observation"]
    assert observation["models_qualified_status"] == "HOLD_NO_TRAIN_PROMOTABLE_CONTEXTUAL_MODEL"
    assert observation["models_qualified_gate_present"] is False
    assert observation["model_load_count"] == 0
    assert qualification["gate_issued"] is False
    assert qualification["service_activated"] is False
    assert qualification["validation_read"] is False
    assert qualification["test_accessed"] is False


def test_a_caller_cannot_manufacture_a_pass() -> None:
    """§F: `{"check": true}` must not become a qualification."""
    with pytest.raises(SafeControllerQualificationError, match="only be minted"):
        TrustedSafeControllerQualification(object(), observation={"everything": True})
    with pytest.raises(SafeControllerQualificationError, match="trusted qualification run"):
        assemble_qualification_report({"checks": dict.fromkeys(ALL_CHECKS, True)})


def _edit(report: dict[str, Any], mutate: Any) -> dict[str, Any]:
    copied = copy.deepcopy(report)
    mutate(copied)
    return copied


def _flip_a_check(content: dict[str, Any]) -> None:
    content["checks"]["zero_invalid_configs"] = False


def _claim_pass_on_a_failed_check(content: dict[str, Any]) -> None:
    content["observation"]["invalid_config_count"] = 3


def _drop_a_check(content: dict[str, Any]) -> None:
    del content["checks"]["owning_round_profile_cross_check"]


def _add_unknown_check(content: dict[str, Any]) -> None:
    content["checks"]["everything_is_fine"] = True


def _claim_model_qualification(content: dict[str, Any]) -> None:
    content["observation"]["models_qualified_status"] = "PASS"


def _widen_capability(content: dict[str, Any]) -> None:
    content["capability_scope"] = "FULL_CONTEXTUAL"


def _reorder_mandatory(content: dict[str, Any]) -> None:
    content["mandatory_checks"] = sorted(content["mandatory_checks"])


def _claim_a_gate(content: dict[str, Any]) -> None:
    content["gate_issued"] = True


def _shrink_the_corpus(content: dict[str, Any]) -> None:
    content["observation"]["profile_count"] = 1


REPORT_TAMPERS = (
    ("flipped_check", _flip_a_check),
    ("check_contradicts_observation", _claim_pass_on_a_failed_check),
    ("omitted_mandatory_check", _drop_a_check),
    ("unknown_check", _add_unknown_check),
    ("claimed_model_qualification", _claim_model_qualification),
    ("widened_capability_scope", _widen_capability),
    ("reordered_mandatory_checks", _reorder_mandatory),
    ("claimed_a_gate", _claim_a_gate),
    ("shrunken_corpus", _shrink_the_corpus),
)


@pytest.mark.parametrize("label,mutate", REPORT_TAMPERS, ids=[t[0] for t in REPORT_TAMPERS])
def test_a_tampered_qualification_report_is_refused(
    qualification: dict[str, Any], label: str, mutate: Any
) -> None:
    with pytest.raises(SafeControllerQualificationError):
        verify_qualification_report(_edit(qualification, mutate))


def test_the_report_is_canonical_and_has_a_stable_identity(
    qualification: dict[str, Any],
) -> None:
    raw = canonical_json_bytes(qualification)
    assert json.loads(raw.decode("utf-8")) == qualification
    assert qualification_report_identity(json.loads(raw)) == qualification_report_identity(
        qualification
    )


def test_the_report_carries_no_operational_value(qualification: dict[str, Any]) -> None:
    """No timestamp, host, PID, password or path may enter a scientific identity."""
    body = json.dumps(qualification)
    for forbidden in ("password", "postgresql://", "/home/", "hostname", "/tmp/"):
        assert forbidden not in body
    for key in qualification["observation"]:
        assert "time" not in key or key == "low_time_deterministic"
        assert not key.endswith("_at")


def test_derive_checks_reads_no_verdict_from_its_input(qualification: dict[str, Any]) -> None:
    """Injecting a verdict into the observation must not change a derived check."""
    poisoned = copy.deepcopy(qualification["observation"])
    poisoned["checks"] = dict.fromkeys(ALL_CHECKS, False)
    poisoned["status"] = "HOLD"
    assert derive_checks(poisoned) == derive_checks(qualification["observation"])


# ---------------------------------------------------------------------------------------- #
# §H the mode-scoped gate: designed, not issued
# ---------------------------------------------------------------------------------------- #
def test_the_gate_is_registered_but_not_issued() -> None:
    required = required_checks_for("SAFE-CONTROLLER-FROZEN")
    assert required, "the mode-scoped gate must be registered"
    assert required <= set(ALL_CHECKS), "the gate requires a check the qualifier cannot produce"
    assert not (repository_root() / "gates/safe-controller-frozen.json").exists()
    assert not (repository_root() / "gates/controller-frozen.json").exists()
    assert not (repository_root() / "gates/models-qualified.json").exists()


def test_the_gate_scope_cannot_imply_contextual_capability() -> None:
    required = required_checks_for("SAFE-CONTROLLER-FROZEN")
    for scoping in (
        "allowed_modes_exactly_safe_baseline",
        "zero_contextual_model_loads",
        "models_qualified_remains_hold",
        "select_config_public_boundary_blocked",
    ):
        assert scoping in required
    assert required != required_checks_for("MODELS-QUALIFIED")


def test_the_additional_checks_are_declared_separately() -> None:
    assert set(MANDATORY_CHECKS).isdisjoint(ADDITIONAL_CHECKS)
    assert list(ALL_CHECKS) == list(MANDATORY_CHECKS) + list(ADDITIONAL_CHECKS)


def test_the_ownership_schema_is_named() -> None:
    assert OWNERSHIP_SCHEMA == "l2h-round-profile-ownership-v1"


def test_a_sealed_partition_member_is_never_opened(tmp_path: Path) -> None:
    """Deleting a TEST member's artifacts entirely must not affect the TRAIN corpus.

    If the loader ever touched a sealed partition, this would fail — which is a stronger proof
    than asserting that it does not, because it fails for the right reason.
    """
    import shutil

    repo, corpus = _corpus_copy(tmp_path)
    members = json.loads((repo / SNAPSHOT_MEMBERS_PATH).read_bytes())
    sealed = [m["round_id"] for m in members["members"] if m["partition"] != "train"]
    assert sealed, "the snapshot has no sealed member to prove anything with"
    for round_id in sealed:
        shutil.rmtree(corpus / round_id)
    loaded = load_verified_round_profile_corpus(root=repo, corpus_root=corpus)
    assert len(loaded) == 50
    assert set(loaded.rounds()).isdisjoint(sealed)


def test_no_sealed_identity_reaches_the_qualification_report(
    qualification: dict[str, Any],
) -> None:
    repo = repository_root()
    members = json.loads((repo / SNAPSHOT_MEMBERS_PATH).read_bytes())
    sealed = {m["round_id"] for m in members["members"] if m["partition"] != "train"} | {
        m["profile_id"] for m in members["members"] if m["partition"] != "train"
    }
    body = json.dumps(qualification)
    for identity in sealed:
        assert identity not in body, f"a sealed identity {identity} reached the evidence"
