"""Round/profile ownership, decision publication, and the SAFE-controller qualification authority.

The negative half is the point. A request that names a round and a profile it cannot prove must be
refused outright — not degraded to a safe fallback, because "I could not authenticate you" and "you
asked for something no model can do" are different statements and must not produce the same result.
"""

from __future__ import annotations

import copy
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
    OWNERSHIP_SCHEMA,
    PHASE_A_AUTHORITY_PATH,
    TRAIN_SCHEDULE_PATH,
    RoundProfileAuthorityError,
    VerifiedRoundProfileAuthority,
    load_verified_round_profile_corpus,
)
from minos_engine.layer2.safe_controller import (
    VerifiedSafeBaselineAuthority,
    load_verified_safe_baseline_authority,
    safe_decision_manifest_content,
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
from minos_engine.layer2.sealed_access_guard import (
    SEALED_IDENTITY_AUTHORITIES,
    SealedAccessError,
    sealed_access_guard,
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
def test_the_corpus_is_the_anchored_train_projection(
    ownership: VerifiedRoundProfileAuthority,
) -> None:
    assert len(ownership) == 50
    assert ownership.partition == "train"
    assert len(ownership.corpus_identity) == 64
    anchors = ownership.anchors
    assert (
        anchors["registry_snapshot_hash"]
        == "3e60aa65aeed8969e29ebeef83024f6fa2285a13c155d7d6dc0c601d1e94f675"
    )
    assert (
        anchors["baseline_protocol_hash"]
        == "c548e190571f5e964560cf30021a520ea8aad6674569fa3202af880d7dff77d1"
    )
    assert (
        anchors["train_schedule_manifest_sha256"]
        == "694a8993ef64f72ca3705442c1bc070c0288d46e08e00e39bab1155d4415d454"
    )
    chromosomes = {ownership.owned(r).chromosome for r in ownership.rounds()}
    assert chromosomes == {"chr18", "chr19", "chr20", "chr21", "chr22"}


def test_a_caller_cannot_mint_a_corpus() -> None:
    with pytest.raises(RoundProfileAuthorityError, match="only be minted"):
        VerifiedRoundProfileAuthority(
            object(),
            by_round={},
            anchors={"registry_snapshot_hash": "b" * 64},
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


def _train_rounds(repo: Path) -> list[str]:
    """The 50 TRAIN round ids, from the TRAIN-ONLY schedule. Never from a sealed membership."""
    schedule = json.loads((repo / TRAIN_SCHEDULE_PATH).read_bytes())
    return [str(e["round_id"]) for batch in schedule["batches"] for e in batch]


def test_the_untampered_copy_loads(tmp_path: Path) -> None:
    repo, corpus = _corpus_copy(tmp_path)
    assert len(load_verified_round_profile_corpus(root=repo, corpus_root=corpus)) == 50


def test_a_tampered_profile_document_is_refused(tmp_path: Path) -> None:
    """Bytes are checked against the frozen inventory before anything is parsed."""
    repo, corpus = _corpus_copy(tmp_path)
    victim = _train_rounds(repo)[0]
    path = corpus / victim / "bam-profile-v1.json"
    document = json.loads(path.read_bytes())
    document["status"] = "INCOMPLETE"
    path.write_bytes(canonical_json_bytes(document))
    # the accepted admission authority binds the manifest to the profile BYTES, so an edited
    # profile is refused without needing a separate artifact inventory
    with pytest.raises(RoundProfileAuthorityError, match="not admissible"):
        load_verified_round_profile_corpus(root=repo, corpus_root=corpus)


def test_a_tampered_attestation_is_refused(tmp_path: Path) -> None:
    repo, corpus = _corpus_copy(tmp_path)
    victim = _train_rounds(repo)[0]
    path = corpus / victim / "input-integrity-attestation-v1.json"
    attestation = json.loads(path.read_bytes())
    attestation["bam_sha256"] = "e" * 64
    path.write_bytes(canonical_json_bytes(attestation))
    with pytest.raises(RoundProfileAuthorityError):
        load_verified_round_profile_corpus(root=repo, corpus_root=corpus)


def test_a_tampered_train_schedule_is_refused(tmp_path: Path) -> None:
    """The schedule is anchored to the accepted Phase-A authority, so editing it fails closed."""
    repo, corpus = _corpus_copy(tmp_path)
    schedule = json.loads((repo / TRAIN_SCHEDULE_PATH).read_bytes())
    schedule["batches"][0][0]["round_id"] = "smuggled-round"
    (repo / TRAIN_SCHEDULE_PATH).write_bytes(canonical_json_bytes(schedule))
    with pytest.raises(RoundProfileAuthorityError, match="hashes to"):
        load_verified_round_profile_corpus(root=repo, corpus_root=corpus)


def test_a_phase_a_authority_rewritten_to_match_a_tampered_schedule_is_still_refused(
    tmp_path: Path,
) -> None:
    """Repairing the anchor does not repair the accepted protocol the anchor must agree with."""
    import hashlib as _hashlib

    repo, corpus = _corpus_copy(tmp_path)
    schedule = json.loads((repo / TRAIN_SCHEDULE_PATH).read_bytes())
    schedule["batches"][0][0]["round_id"] = "smuggled-round"
    raw = canonical_json_bytes(schedule)
    (repo / TRAIN_SCHEDULE_PATH).write_bytes(raw)
    authority = json.loads((repo / PHASE_A_AUTHORITY_PATH).read_bytes())
    authority["content"]["train_schedule_manifest_sha256"] = _hashlib.sha256(raw).hexdigest()
    (repo / PHASE_A_AUTHORITY_PATH).write_bytes(canonical_json_bytes(authority))
    # the schedule now hashes correctly, so only the per-member attestation binding can catch it
    with pytest.raises(RoundProfileAuthorityError):
        load_verified_round_profile_corpus(root=repo, corpus_root=corpus)


def test_a_phase_a_authority_citing_a_foreign_protocol_is_refused(tmp_path: Path) -> None:
    repo, corpus = _corpus_copy(tmp_path)
    authority = json.loads((repo / PHASE_A_AUTHORITY_PATH).read_bytes())
    authority["content"]["baseline_protocol_hash"] = "e" * 64
    (repo / PHASE_A_AUTHORITY_PATH).write_bytes(canonical_json_bytes(authority))
    with pytest.raises(RoundProfileAuthorityError, match="cannot anchor anything"):
        load_verified_round_profile_corpus(root=repo, corpus_root=corpus)


def test_a_missing_corpus_member_is_refused(tmp_path: Path) -> None:
    repo, corpus = _corpus_copy(tmp_path)
    victim = _train_rounds(repo)[0]
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


def test_a_manifest_cannot_be_published_under_a_name_it_does_not_hash_to(
    tmp_path: Path,
) -> None:
    """§H(1): the identity is derived, never accepted from the caller."""
    with pytest.raises(DecisionPublicationError, match="may not be published as"):
        publish_safe_decision(
            manifest={"schema_version": "not-a-decision"},
            identity="a" * 64,
            output_root=tmp_path / "decisions",
        )


def test_a_conflicting_record_under_an_existing_identity_is_refused(
    authority: VerifiedSafeBaselineAuthority,
    ownership: VerifiedRoundProfileAuthority,
    tmp_path: Path,
) -> None:
    """§H(2): an existing final record is never overwritten, even by a well-formed writer."""
    owned = ownership.owned(ownership.rounds()[0])
    root = tmp_path / "decisions"
    manifest = safe_decision_manifest_content(
        request=_request(authority, owned), authority=authority, ownership=ownership
    )
    identity = safe_decision_manifest_identity(manifest)
    first = publish_safe_decision(manifest=manifest, identity=identity, output_root=root)
    original = first.path.read_bytes()
    # a squatter puts different bytes under the same name, then a legitimate writer retries
    first.path.write_bytes(canonical_json_bytes({"schema_version": "squatted"}))
    with pytest.raises(DecisionPublicationError, match="never name two decisions"):
        publish_safe_decision(manifest=manifest, identity=identity, output_root=root)
    first.path.write_bytes(original)


def test_concurrent_publishers_of_one_decision_converge(
    authority: VerifiedSafeBaselineAuthority,
    ownership: VerifiedRoundProfileAuthority,
    tmp_path: Path,
) -> None:
    """Real threads, one record. No process-local lock is involved in the guarantee."""
    from concurrent.futures import ThreadPoolExecutor

    owned = ownership.owned(ownership.rounds()[0])
    root = tmp_path / "decisions"
    manifest = safe_decision_manifest_content(
        request=_request(authority, owned), authority=authority, ownership=ownership
    )
    identity = safe_decision_manifest_identity(manifest)
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(
            pool.map(
                lambda _: publish_safe_decision(
                    manifest=manifest, identity=identity, output_root=root
                ),
                range(24),
            )
        )
    assert len(list(root.glob("*.json"))) == 1
    assert sum(1 for r in results if not r.reused) == 1, "exactly one writer creates the record"
    assert all(r.sha256 == results[0].sha256 for r in results)
    assert not list(root.glob(".*.tmp")), "a staged file was left behind"


def test_a_final_record_is_never_clobbered_by_a_second_writer(tmp_path: Path) -> None:
    """The old check-then-replace sequence could lose a writer's bytes; link() cannot."""
    import os

    root = tmp_path / "decisions"
    root.mkdir(parents=True)
    target = root / "record.json"
    target.write_bytes(b"first")
    staged = root / ".staged"
    staged.write_bytes(b"second")
    with pytest.raises(FileExistsError):
        os.link(staged, target)
    assert target.read_bytes() == b"first"


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
    assert OWNERSHIP_SCHEMA == "l2h-round-profile-ownership-v2"


def test_no_sealed_authority_is_opened_while_loading_ownership() -> None:
    """The corrective proof: a hard guard, not a claim about which fields were used."""
    with sealed_access_guard() as observed:
        loaded = load_verified_round_profile_corpus(root=repository_root())
    assert len(loaded) == 50
    assert observed.open_attempts == 0
    assert observed.attempted_paths == []


def test_the_guard_actually_refuses_a_sealed_authority() -> None:
    """A guard that never fires proves nothing, so prove it fires."""
    sealed = repository_root() / "manifests" / "profile_snapshot_epoch1_members.json"
    with (
        sealed_access_guard() as observed,
        pytest.raises(SealedAccessError, match="sealed member identities"),
    ):
        sealed.read_bytes()
    assert observed.open_attempts == 1
    assert observed.attempted_paths == ["profile_snapshot_epoch1_members.json"]


def test_every_sealed_bearing_authority_is_guarded() -> None:
    for name in SEALED_IDENTITY_AUTHORITIES:
        path = repository_root() / "manifests" / name
        if not path.is_file():
            continue
        with sealed_access_guard(), pytest.raises(SealedAccessError):
            path.read_bytes()


def test_ownership_loads_with_every_sealed_authority_removed(tmp_path: Path) -> None:
    """Delete them outright. If the loader needed one, this fails for the right reason."""
    repo, corpus = _corpus_copy(tmp_path)
    removed = 0
    for name in SEALED_IDENTITY_AUTHORITIES:
        path = repo / "manifests" / name
        if path.is_file():
            path.unlink()
            removed += 1
    assert removed, "no sealed authority was present to remove"
    assert len(load_verified_round_profile_corpus(root=repo, corpus_root=corpus)) == 50


def test_ownership_loads_with_unscheduled_corpus_directories_removed(tmp_path: Path) -> None:
    """Only the 50 scheduled directories are opened; the rest need not even exist."""
    import shutil

    repo, corpus = _corpus_copy(tmp_path)
    scheduled = set(_train_rounds(repo))
    removed = 0
    for directory in sorted(corpus.iterdir()):
        if directory.is_dir() and directory.name not in scheduled:
            shutil.rmtree(directory)
            removed += 1
    assert removed == 25
    assert len(load_verified_round_profile_corpus(root=repo, corpus_root=corpus)) == 50


def test_only_scheduled_train_identities_reach_the_qualification_report(
    qualification: dict[str, Any],
) -> None:
    """Containment proved from the TRAIN-only side.

    The obvious test -- collect the sealed ids and assert their absence -- would itself have to
    read the sealed membership, which is the defect this corrective exists to fix. So the proof
    runs the other way: every round and profile identity the report mentions must be one this
    authority owns.
    """
    from minos_engine.layer2.round_profile_authority import load_verified_round_profile_corpus

    owned = load_verified_round_profile_corpus(root=repository_root())
    allowed = (
        {owned.owned(r).round_id for r in owned.rounds()}
        | {owned.owned(r).profile_id for r in owned.rounds()}
        | {owned.owned(r).dataset_id for r in owned.rounds()}
    )
    body = json.dumps(qualification)
    # the report should not carry per-member identities at all; if it ever does, they must be ours
    for token in body.replace('"', " ").split():
        if token.startswith("minos-chr") and token not in allowed:
            raise AssertionError(f"an unowned dataset identity reached the evidence: {token}")


def test_the_report_records_why_v1_is_superseded(qualification: dict[str, Any]) -> None:
    supersedes = qualification["supersedes"]
    assert supersedes["schema"] == "l2h-safe-controller-qualification-v1"
    assert (
        supersedes["identity"] == "7d305bcd7c35c82389259ec1d88058ff9202ce454a0364d15e4b864345aaf821"
    )
    assert supersedes["status"] == "HISTORICAL_EVIDENCE_NOT_VALID_FOR_QUALIFICATION"
    assert "enumerated" in supersedes["reason"]


def test_the_isolation_flags_are_observed_not_authored(qualification: dict[str, Any]) -> None:
    observation = qualification["observation"]
    assert observation["forbidden_sealed_path_open_attempts"] == 0
    assert observation["attempted_sealed_authorities"] == []
    assert observation["sealed_identity_authorities"] == sorted(SEALED_IDENTITY_AUTHORITIES)
    assert observation["materialized_round_count"] == observation["profile_count"] == 50
    assert qualification["checks"]["sealed_authorities_never_opened"] is True


def test_a_report_whose_isolation_observation_contradicts_its_flags_is_refused(
    qualification: dict[str, Any],
) -> None:
    """§E: the verifier must reject a hard-coded access claim its observations do not support."""
    forged = copy.deepcopy(qualification)
    forged["observation"]["forbidden_sealed_path_open_attempts"] = 2
    forged["observation"]["attempted_sealed_authorities"] = ["profile_snapshot_epoch1_members.json"]
    with pytest.raises(SafeControllerQualificationError, match="sealed-authority open attempt"):
        verify_qualification_report(forged)


def test_a_report_claiming_a_sealed_partition_was_admitted_is_refused(
    qualification: dict[str, Any],
) -> None:
    forged = copy.deepcopy(qualification)
    forged["observation"]["admitted_partition"] = "test"
    with pytest.raises(SafeControllerQualificationError):
        verify_qualification_report(forged)


def test_the_superseded_v1_report_cannot_pass_the_v2_verifier() -> None:
    """§J: a future gate must not be authorizable by the superseded evidence."""
    from minos_engine.layer2.safe_controller_qualification import (
        SAFE_CONTROLLER_QUALIFICATION_SCHEMA,
    )

    v1 = json.loads(
        (
            repository_root() / "reports/layer2/l2h-safe-controller-qualification-v1.json"
        ).read_bytes()
    )
    assert v1["schema_version"] != SAFE_CONTROLLER_QUALIFICATION_SCHEMA
    with pytest.raises(SafeControllerQualificationError, match="unexpected qualification schema"):
        verify_qualification_report(v1)
