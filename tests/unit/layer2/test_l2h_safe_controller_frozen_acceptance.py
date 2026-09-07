"""The final acceptance authority for SAFE-CONTROLLER-FROZEN.

The attack this file exists for is the one the structural verifier could not see: change who
issued the artifact, recompute `gate_hash`, and leave every scientific binding untouched. Nothing
inside the gate can refuse that, because the gate is what is lying. The accepted issuer commit and
tree therefore live in a module committed strictly after the artifact, and are proved against git
rather than length-checked.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from minos_engine.common.errors import ContractValidationError
from minos_engine.gates.required_checks import required_checks_for
from minos_engine.layer2.safe_controller_frozen_acceptance import (
    ACCEPTED_GATE_FILE_SHA256,
    ACCEPTED_GATE_HASH,
    ACCEPTED_ISSUER_SOURCE_COMMIT,
    ACCEPTED_ISSUER_SOURCE_TREE,
    ACCEPTED_QUALIFIED_SOURCE_COMMIT,
    ACCEPTED_QUALIFIED_SOURCE_TREE,
    SUPERSEDED_ISSUANCES,
    SafeControllerFrozenAcceptanceError,
    accepted_gate_identities,
    verify_accepted_safe_controller_frozen_gate,
)
from minos_engine.layer2.safe_controller_gate import (
    SAFE_CONTROLLER_FROZEN_GATE,
    SAFE_CONTROLLER_FROZEN_GATE_PATH,
)
from minos_engine.qualification.l2f_accepted_identities import repository_root

EVIDENCE_COMMIT = "2fbabe8cc372ba8506d2798caa847135aeddff60"
REFUSALS = (SafeControllerFrozenAcceptanceError, ContractValidationError, ValidationError)


def _repo_copy(tmp_path: Path) -> Path:
    import shutil

    root = repository_root()
    target = tmp_path / "repo"
    target.mkdir(parents=True)
    for relative in ("reports", "manifests", "gates"):
        shutil.copytree(root / relative, target / relative)
    shutil.copytree(root / ".git", target / ".git", symlinks=True)
    return target


def _gate(repo: Path) -> dict[str, Any]:
    return dict(json.loads((repo / SAFE_CONTROLLER_FROZEN_GATE_PATH).read_bytes()))


def _write(repo: Path, document: dict[str, Any]) -> None:
    (repo / SAFE_CONTROLLER_FROZEN_GATE_PATH).write_text(
        json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def _rehash(document: dict[str, Any]) -> dict[str, Any]:
    """Recompute the gate hash, so only the external authority can catch the edit."""
    from minos_engine.gates.contracts import GateArtifact

    document["gate_hash"] = ""
    return GateArtifact.model_validate(document).model_dump(mode="json")


# ---------------------------------------------------------------------------------------- #
# acceptance
# ---------------------------------------------------------------------------------------- #
def test_the_committed_gate_is_accepted() -> None:
    report = verify_accepted_safe_controller_frozen_gate(repository_root())
    assert report["ok"] is True
    assert report["gate_name"] == SAFE_CONTROLLER_FROZEN_GATE
    assert report["gate_hash"] == ACCEPTED_GATE_HASH
    assert report["gate_file_sha256"] == ACCEPTED_GATE_FILE_SHA256
    assert report["issuer_source_commit"] == ACCEPTED_ISSUER_SOURCE_COMMIT
    assert report["issuer_source_tree"] == ACCEPTED_ISSUER_SOURCE_TREE
    assert report["qualified_source_git_sha"] == ACCEPTED_QUALIFIED_SOURCE_COMMIT
    assert report["qualified_source_tree_sha"] == ACCEPTED_QUALIFIED_SOURCE_TREE
    assert report["capability_scope"] == "SAFE_BASELINE_ONLY"
    assert report["check_count"] == 29


def test_the_accepted_bytes_match_the_committed_artifact() -> None:
    raw = (repository_root() / SAFE_CONTROLLER_FROZEN_GATE_PATH).read_bytes()
    assert hashlib.sha256(raw).hexdigest() == ACCEPTED_GATE_FILE_SHA256


def test_the_acceptance_authority_is_declared_as_data() -> None:
    declared = accepted_gate_identities()
    assert declared["gate_hash"] == ACCEPTED_GATE_HASH
    assert declared["issuer_source_commit"] == ACCEPTED_ISSUER_SOURCE_COMMIT
    assert declared["qualified_source_commit"] == ACCEPTED_QUALIFIED_SOURCE_COMMIT
    assert declared["issuer_source_commit"] != declared["qualified_source_commit"]


def test_the_acceptance_authority_is_not_self_referential() -> None:
    """The pinned issuer commit must be an ANCESTOR of the commit doing the pinning."""
    import subprocess

    root = str(repository_root())
    head = subprocess.run(
        ["git", "-C", root, "rev-parse", "HEAD"], capture_output=True, text=True, check=True
    ).stdout.strip()
    assert head != ACCEPTED_ISSUER_SOURCE_COMMIT, "a commit cannot pin its own identity"
    ancestry = subprocess.run(
        ["git", "-C", root, "merge-base", "--is-ancestor", ACCEPTED_ISSUER_SOURCE_COMMIT, head],
        capture_output=True,
    )
    assert ancestry.returncode == 0, "the pinned issuer commit is not an ancestor of HEAD"


def test_the_superseded_issuance_is_recorded_and_refused() -> None:
    assert len(SUPERSEDED_ISSUANCES) == 1
    entry = SUPERSEDED_ISSUANCES[0]
    assert entry["gate_hash"] == "3c6d9b0b6f84ed017d577da77f39d87b19bc633e56a66f8c599f1a5f8cfc07ae"
    assert entry["issuer_source_commit"] == "9d8864a2a5b906bed0e5989beff827b33bb568fa"
    assert entry["status"] == ("SUPERSEDED_BEFORE_SERVICE_ACTIVATION_NEVER_ACCEPTED_FOR_PROMOTION")
    assert entry["gate_hash"] != ACCEPTED_GATE_HASH


# ---------------------------------------------------------------------------------------- #
# §G issuer-provenance forgery: the attack the structural verifier could not see
# ---------------------------------------------------------------------------------------- #
def _foreign_hex(document: dict[str, Any]) -> None:
    document["engine_git_sha"] = "a" * 40


def _evidence_commit(document: dict[str, Any]) -> None:
    document["engine_git_sha"] = EVIDENCE_COMMIT


def _qualified_source(document: dict[str, Any]) -> None:
    document["engine_git_sha"] = ACCEPTED_QUALIFIED_SOURCE_COMMIT


def _another_real_commit(document: dict[str, Any]) -> None:
    document["engine_git_sha"] = "9d8864a2a5b906bed0e5989beff827b33bb568fa"


ISSUER_FORGERIES = (
    ("arbitrary_forty_hex", _foreign_hex),
    ("the_evidence_commit", _evidence_commit),
    ("the_qualified_controller_source", _qualified_source),
    ("another_real_repository_commit", _another_real_commit),
)


@pytest.mark.parametrize("label,mutate", ISSUER_FORGERIES, ids=[f[0] for f in ISSUER_FORGERIES])
def test_a_reissued_gate_naming_a_different_issuer_is_refused(
    tmp_path: Path, label: str, mutate: Any
) -> None:
    """Science untouched, gate_hash recomputed, only the issuer changed. Must still fail."""
    repo = _repo_copy(tmp_path)
    document = _gate(repo)
    original_checks = dict(document["mandatory_checks"])
    original_inputs = dict(document["input_hashes"])
    mutate(document)
    document = _rehash(document)
    _write(repo, document)
    # the science really is untouched, so nothing else could catch this
    reloaded = _gate(repo)
    assert reloaded["mandatory_checks"] == original_checks
    assert reloaded["input_hashes"] == original_inputs
    assert reloaded["gate_hash"] == _rehash(dict(reloaded))["gate_hash"]
    with pytest.raises(SafeControllerFrozenAcceptanceError, match="names issuer|file hashes to"):
        verify_accepted_safe_controller_frozen_gate(repo)


def test_an_issuer_tree_mismatch_is_refused() -> None:
    """The accepted commit must have exactly the accepted tree."""
    from minos_engine.layer2.safe_controller_gate import (
        SafeControllerGateError,
        verify_issuer_provenance,
    )

    with pytest.raises(SafeControllerGateError, match="has tree"):
        verify_issuer_provenance(
            repository_root(), commit=ACCEPTED_ISSUER_SOURCE_COMMIT, tree="c" * 40
        )


# ---------------------------------------------------------------------------------------- #
# the rest of the acceptance surface
# ---------------------------------------------------------------------------------------- #
def _swap_capability_binding(document: dict[str, Any]) -> None:
    document["input_hashes"]["capability_scope_hash"] = "1" * 64


def _swap_contextual_binding(document: dict[str, Any]) -> None:
    document["input_hashes"]["contextual_model_status_hash"] = "2" * 64


def _swap_publication_binding(document: dict[str, Any]) -> None:
    document["input_hashes"]["publication_disposition_hash"] = "3" * 64


def _swap_qualified_source(document: dict[str, Any]) -> None:
    document["qualified_source_git_sha"] = EVIDENCE_COMMIT


def _rename_gate(document: dict[str, Any]) -> None:
    document["gate_name"] = "CONTROLLER-FROZEN"


def _false_check(document: dict[str, Any]) -> None:
    document["mandatory_checks"]["zero_invalid_configs"] = False


ACCEPTANCE_TAMPERS = (
    ("capability_scope_hash_changed", _swap_capability_binding),
    ("contextual_model_status_hash_changed", _swap_contextual_binding),
    ("publication_disposition_hash_changed", _swap_publication_binding),
    ("qualified_source_replaced", _swap_qualified_source),
    ("renamed_to_controller_frozen", _rename_gate),
    ("required_check_false", _false_check),
)


@pytest.mark.parametrize("label,mutate", ACCEPTANCE_TAMPERS, ids=[t[0] for t in ACCEPTANCE_TAMPERS])
def test_a_tampered_gate_is_refused_by_acceptance(tmp_path: Path, label: str, mutate: Any) -> None:
    repo = _repo_copy(tmp_path)
    document = _gate(repo)
    mutate(document)
    try:
        document = _rehash(document)
    except REFUSALS:
        _write(repo, document)
        with pytest.raises(REFUSALS):
            verify_accepted_safe_controller_frozen_gate(repo)
        return
    _write(repo, document)
    with pytest.raises(REFUSALS):
        verify_accepted_safe_controller_frozen_gate(repo)


def test_a_tampered_gate_file_sha_is_refused(tmp_path: Path) -> None:
    """Even a whitespace-only reformat changes the accepted bytes."""
    repo = _repo_copy(tmp_path)
    document = _gate(repo)
    (repo / SAFE_CONTROLLER_FROZEN_GATE_PATH).write_text(
        json.dumps(document, indent=4, sort_keys=True) + "\n", encoding="utf-8"
    )
    with pytest.raises(SafeControllerFrozenAcceptanceError, match="file hashes to"):
        verify_accepted_safe_controller_frozen_gate(repo)


def test_a_substituted_qualification_is_refused(tmp_path: Path) -> None:
    from minos_engine.common.canonical_json import canonical_json_bytes
    from minos_engine.layer2.safe_controller_qualification import (
        SAFE_CONTROLLER_QUALIFICATION_PATH,
    )

    for version in ("v1", "v2"):
        repo = _repo_copy(tmp_path / version)
        old = json.loads(
            (repo / f"reports/layer2/l2h-safe-controller-qualification-{version}.json").read_bytes()
        )
        old["checks"] = dict.fromkeys(required_checks_for(SAFE_CONTROLLER_FROZEN_GATE), True)
        (repo / SAFE_CONTROLLER_QUALIFICATION_PATH).write_bytes(canonical_json_bytes(old))
        with pytest.raises(SafeControllerFrozenAcceptanceError, match="structurally"):
            verify_accepted_safe_controller_frozen_gate(repo)


def test_a_contextual_gate_appearing_is_refused(tmp_path: Path) -> None:
    for name in ("controller-frozen.json", "models-qualified.json"):
        repo = _repo_copy(tmp_path / name.replace(".json", ""))
        (repo / "gates" / name).write_text("{}", encoding="utf-8")
        with pytest.raises(SafeControllerFrozenAcceptanceError):
            verify_accepted_safe_controller_frozen_gate(repo)


def test_a_caller_cannot_author_acceptance() -> None:
    """There is no acceptance entry that takes a caller's verdict."""
    import inspect

    from minos_engine.layer2 import safe_controller_frozen_acceptance as module

    signature = inspect.signature(module.verify_accepted_safe_controller_frozen_gate)
    assert list(signature.parameters) == ["root"], (
        "the acceptance verifier must take only a repository root"
    )


def test_the_missing_gate_is_refused(tmp_path: Path) -> None:
    repo = _repo_copy(tmp_path)
    (repo / SAFE_CONTROLLER_FROZEN_GATE_PATH).unlink()
    with pytest.raises(SafeControllerFrozenAcceptanceError, match="structurally|missing"):
        verify_accepted_safe_controller_frozen_gate(repo)


def test_acceptance_does_not_activate_the_service() -> None:
    from minos_engine.common.errors import StageNotReadyError
    from minos_engine.layer2.service import Layer2Service

    verify_accepted_safe_controller_frozen_gate(repository_root())
    with pytest.raises(StageNotReadyError):
        Layer2Service().select_config(None)  # type: ignore[arg-type]


def test_acceptance_does_not_waive_persistence() -> None:
    from minos_engine.layer2.decision_publication import DECISION_PUBLICATION_DISPOSITION

    assert DECISION_PUBLICATION_DISPOSITION == (
        "FILE_PUBLISHED_DB_PERSISTENCE_DEFERRED_TO_ACTIVATION"
    )
