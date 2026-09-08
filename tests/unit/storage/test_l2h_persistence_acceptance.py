"""The external persistence acceptance authority, and what it refuses.

Pinned from outside what it authenticates, in a commit strictly after the evidence — the same
non-circular pattern as ``safe_controller_frozen_acceptance``. These tests never let acceptance
pass on anything but the exact accepted artifact.
"""

from __future__ import annotations

import ast
import hashlib
import json
import shutil

import pytest
from tests.conftest import REPO_ROOT

from minos_engine.storage.decision_persistence_acceptance import (
    ACCEPTED_GATE_HASH,
    ACCEPTED_LIVE_LOOKUP_SURFACE,
    ACCEPTED_MAIN_REVISION,
    ACCEPTED_OVERLAY_BASE_CONTRACT_HASH,
    ACCEPTED_OVERLAY_HEAD_CONTRACT_HASH,
    ACCEPTED_OVERLAY_HEAD_REVISION,
    ACCEPTED_QUALIFICATION_FILE_SHA256,
    ACCEPTED_QUALIFICATION_IDENTITY,
    ACCEPTED_SOURCE_COMMIT,
    ACCEPTED_SOURCE_TREE,
    PERSISTENCE_ACCEPTANCE_SCHEMA,
    REMAINING_ACTIVATION_PREREQUISITES,
    SUPERSEDED_QUALIFICATIONS,
    PersistenceAcceptanceError,
    accepted_persistence_identities,
    verify_accepted_persistence_authority,
)
from minos_engine.storage.decision_persistence_qualification import (
    DECISION_PERSISTENCE_QUALIFICATION_PATH,
    HISTORICAL_V1_PATH,
)

MODULE = REPO_ROOT / "src/minos_engine/storage/decision_persistence_acceptance.py"


def _staged(tmp_path, mutate=None):
    """A repository copy whose qualification document has been tampered with."""
    root = tmp_path / "repo"
    root.mkdir()
    for relative in ("gates", "reports", "manifests", "migrations_runtime", "src", "docs"):
        source = REPO_ROOT / relative
        if source.exists():
            shutil.copytree(source, root / relative, symlinks=False)
    shutil.copy2(REPO_ROOT / "alembic.ini", root / "alembic.ini")
    shutil.copytree(REPO_ROOT / ".git", root / ".git", symlinks=False)
    if mutate is not None:
        path = root / DECISION_PERSISTENCE_QUALIFICATION_PATH
        report = json.loads(path.read_bytes())
        mutate(report)
        from minos_engine.common.canonical_json import canonical_json_bytes

        path.write_bytes(canonical_json_bytes(report))
    return root


# --------------------------------------------------------------------------- #
# it accepts exactly one artifact
# --------------------------------------------------------------------------- #
def test_the_committed_evidence_is_accepted():
    result = verify_accepted_persistence_authority(REPO_ROOT)
    assert result["ok"] is True
    assert result["qualification_identity"] == ACCEPTED_QUALIFICATION_IDENTITY
    assert result["qualification_file_sha256"] == ACCEPTED_QUALIFICATION_FILE_SHA256
    assert result["source_commit"] == ACCEPTED_SOURCE_COMMIT
    assert result["source_tree"] == ACCEPTED_SOURCE_TREE
    assert result["overlay_head_revision"] == ACCEPTED_OVERLAY_HEAD_REVISION
    assert result["main_revision"] == ACCEPTED_MAIN_REVISION
    assert result["live_lookup_surface"] == ACCEPTED_LIVE_LOOKUP_SURFACE
    assert result["safe_controller_frozen_gate_hash"] == ACCEPTED_GATE_HASH
    assert result["decision_count"] == 196
    assert result["check_count"] == 41


def test_acceptance_is_not_activation():
    result = verify_accepted_persistence_authority(REPO_ROOT)
    assert result["service_activation_authorised"] is False
    assert result["remaining_activation_prerequisites"] == [
        "SAFE_CONFIG_ROW_NOT_PROVISIONED",
        "NO_OWNERSHIP_AUTHORITY_FOR_A_NEW_LIVE_ROUND",
    ]
    assert len(REMAINING_ACTIVATION_PREREQUISITES) == 2
    for entry in REMAINING_ACTIVATION_PREREQUISITES:
        assert entry["detail"].strip()

    from minos_engine.common.errors import StageNotReadyError
    from minos_engine.layer2.service import Layer2Service

    with pytest.raises(StageNotReadyError):
        Layer2Service().select_config(None)  # type: ignore[arg-type]


def test_the_identities_are_available_as_data():
    identities = accepted_persistence_identities()
    assert identities["schema_version"] == PERSISTENCE_ACCEPTANCE_SCHEMA
    assert identities["qualification_identity"] == ACCEPTED_QUALIFICATION_IDENTITY
    assert identities["overlay_base_contract_hash"] == ACCEPTED_OVERLAY_BASE_CONTRACT_HASH
    assert identities["overlay_head_contract_hash"] == ACCEPTED_OVERLAY_HEAD_CONTRACT_HASH
    assert identities["service_activation_authorised"] is False


# --------------------------------------------------------------------------- #
# non-circularity, and no test-only dependency
# --------------------------------------------------------------------------- #
def test_the_accepted_source_is_a_strict_ancestor_and_not_head():
    import subprocess

    from minos_engine.qualification.git_tree import is_ancestor

    if not (REPO_ROOT / ".git").exists():  # pragma: no cover - exported tree
        pytest.skip("not a git checkout")
    head = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert head != ACCEPTED_SOURCE_COMMIT, "an authority may not identify its own commit"
    assert is_ancestor(REPO_ROOT, ACCEPTED_SOURCE_COMMIT, "HEAD")


def test_the_production_verifier_imports_nothing_from_tests():
    tree = ast.parse(MODULE.read_text())
    modules: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            modules.append(node.module)
        elif isinstance(node, ast.Import):
            modules.extend(a.name for a in node.names)
    for module in modules:
        assert not module.startswith("tests"), module
        assert "pytest" not in module, module
    assert all(m.startswith("minos_engine") or "." not in m for m in modules)


def test_the_verifier_is_callable_with_no_arguments():
    """A future service calls it without knowing where the repository is."""
    result = verify_accepted_persistence_authority()
    assert result["ok"] is True


# --------------------------------------------------------------------------- #
# what it refuses
# --------------------------------------------------------------------------- #
def test_the_superseded_v1_qualification_is_refused_by_identity(tmp_path):
    """Restoring v1 under the accepted filename must not pass."""
    root = _staged(tmp_path)
    shutil.copy2(REPO_ROOT / HISTORICAL_V1_PATH, root / DECISION_PERSISTENCE_QUALIFICATION_PATH)
    with pytest.raises(PersistenceAcceptanceError):
        verify_accepted_persistence_authority(root)


def test_a_missing_qualification_is_refused(tmp_path):
    root = _staged(tmp_path)
    (root / DECISION_PERSISTENCE_QUALIFICATION_PATH).unlink()
    with pytest.raises(PersistenceAcceptanceError, match="missing"):
        verify_accepted_persistence_authority(root)


def test_a_reformatted_but_semantically_identical_document_is_refused(tmp_path):
    """Acceptance is by BYTES, not only by meaning."""
    root = _staged(tmp_path)
    path = root / DECISION_PERSISTENCE_QUALIFICATION_PATH
    path.write_bytes(json.dumps(json.loads(path.read_bytes()), indent=1).encode())
    with pytest.raises(PersistenceAcceptanceError, match="hashes to"):
        verify_accepted_persistence_authority(root)


TAMPERS = [
    (("capabilities", "campaign_connection_role"), "postgres"),
    (
        ("capabilities", "partition_bearing_relations_readable_by_live"),
        ["profiling.bam_profiles"],
    ),
    (("capabilities", "resolver", "security_definer"), False),
    (("persistence_schema", "overlay_head_revision"), "r0001_l2h_runtime_decisions"),
    (("persistence_schema", "overlay_head_contract_hash"), "0" * 64),
    (("persistence_schema", "overlay_base_contract_hash"), "0" * 64),
    (("persistence_schema", "requires_main_revision"), "0026_l2f2_phase_d_closure"),
    (("persistence_schema", "live_lookup_surface"), "runtime.something_else"),
    (("execution_source_commit",), "0" * 40),
    (("decision_count",), 1),
    (("corrective", "grants_revoked"), []),
]


@pytest.mark.parametrize("path,value", TAMPERS)
def test_any_tampering_is_refused_on_the_bytes_alone(tmp_path, path, value):
    """The byte pin is the first gate: nothing semantic even has to be consulted."""

    def mutate(report: dict) -> None:
        node = report["observation"]
        for key in path[:-1]:
            node = node[key]
        node[path[-1]] = value

    root = _staged(tmp_path, mutate)
    with pytest.raises(PersistenceAcceptanceError, match="hashes to"):
        verify_accepted_persistence_authority(root)


@pytest.mark.parametrize("path,value", TAMPERS)
def test_the_semantic_checks_refuse_even_when_the_byte_pin_is_lifted(
    tmp_path, monkeypatch, path, value
):
    """With the file SHA re-pinned to the forgery, every semantic guarantee still has to hold."""
    import hashlib as _hashlib

    from minos_engine.common.canonical_json import canonical_json_bytes
    from minos_engine.storage import decision_persistence_acceptance as module

    def mutate(report: dict) -> None:
        node = report["observation"]
        for key in path[:-1]:
            node = node[key]
        node[path[-1]] = value

    root = _staged(tmp_path, mutate)
    forged = (root / DECISION_PERSISTENCE_QUALIFICATION_PATH).read_bytes()
    monkeypatch.setattr(
        module, "ACCEPTED_QUALIFICATION_FILE_SHA256", _hashlib.sha256(forged).hexdigest()
    )
    assert canonical_json_bytes(json.loads(forged)) == forged
    with pytest.raises(PersistenceAcceptanceError) as caught:
        verify_accepted_persistence_authority(root)
    assert "hashes to" not in str(caught.value), "the byte pin must not be what refused it"


def test_a_forged_pass_verdict_is_refused(tmp_path):
    def mutate(report: dict) -> None:
        report["observation"]["capabilities"]["enumeration_probe_sqlstates"]["profile_count"] = (
            "00000"
        )
        report["checks"]["live_role_cannot_enumerate_profile_table"] = True

    root = _staged(tmp_path, mutate)
    with pytest.raises(PersistenceAcceptanceError):
        verify_accepted_persistence_authority(root)


@pytest.mark.parametrize("field", ["gate_issued", "service_activated"])
def test_an_issued_gate_or_activated_service_is_refused(tmp_path, field):
    target = tmp_path / field
    target.mkdir()
    root = _staged(target)
    path = root / DECISION_PERSISTENCE_QUALIFICATION_PATH
    report = json.loads(path.read_bytes())
    report[field] = True
    from minos_engine.common.canonical_json import canonical_json_bytes

    path.write_bytes(canonical_json_bytes(report))
    with pytest.raises(PersistenceAcceptanceError):
        verify_accepted_persistence_authority(root)


def test_the_accepted_constants_agree_with_the_document_on_disk():
    raw = (REPO_ROOT / DECISION_PERSISTENCE_QUALIFICATION_PATH).read_bytes()
    assert hashlib.sha256(raw).hexdigest() == ACCEPTED_QUALIFICATION_FILE_SHA256
    assert len(SUPERSEDED_QUALIFICATIONS) == 1
    assert SUPERSEDED_QUALIFICATIONS[0]["status"].startswith("SUPERSEDED_BEFORE_SERVICE_ACTIVATION")
