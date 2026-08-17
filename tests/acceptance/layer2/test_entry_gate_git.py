"""Synthetic-repo git-ancestry negatives for the hardened L2 entry gate.

These use the internal ``_verify_against`` seam with a synthetic accepted-identity
set pointing at throwaway git repositories, so every ancestry failure mode can be
provoked deterministically and proven to fail closed. The public
``verify_l2_entry_gate`` always uses the pinned real ``ACCEPTED`` and is covered by
``tests/acceptance/test_l1_entry_gate.py``.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from minos_engine.gates.contracts import EvidenceItem, GateArtifact, GateStatus
from minos_engine.gates.verifier import write_gate
from minos_engine.layer2.contracts import AcceptedPrerequisiteIdentity
from minos_engine.layer2.entry_gate import EntryGateRequest, _verify_against

TS = "2026-08-17T00:00:00+00:00"
_H64 = "a" * 64
_OID = "b" * 40


def _git(root: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(root), *args], capture_output=True, text=True, check=True
    )
    return proc.stdout.strip()


def _init(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "t")


def _commit(root: Path, name: str) -> str:
    (root / name).write_text(name, encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", name)
    return _git(root, "rev-parse", "HEAD")


def _tree(root: Path, sha: str) -> str:
    return _git(root, "rev-parse", f"{sha}^{{tree}}")


def _place_gate(root: Path) -> None:
    """A loadable HOLD gate so verification proceeds to the git-ancestry checks."""
    gate = GateArtifact(
        gate_name="L1-READY",
        status=GateStatus.HOLD,
        engine_git_sha="x",
        mandatory_checks={"placeholder": True},
        evidence=(EvidenceItem(description="e", path="reports/x.md", sha256=_H64),),
        created_at=TS,
    )
    write_gate(gate, root / "gates" / "l1-ready.json")
    (root / "reports").mkdir(parents=True, exist_ok=True)
    (root / "reports" / "LAYER1_QUALIFICATION_REPORT.md").write_text("r", encoding="utf-8")


def _accepted(**over: str) -> AcceptedPrerequisiteIdentity:
    base = {
        "l1_gate_hash": _H64,
        "protocol_gate_hash": _H64,
        "twin_gate_hash": _H64,
        "layer1_schema_hash": _H64,
        "profiler_config_hash": _H64,
        "profiler_version": "layer1-profiler-v1",
        "qualified_source_commit": _OID,
        "qualified_source_tree": _OID,
        "artifact_commit": _OID,
        "artifact_tree": _OID,
        "v2_framework_commit": _OID,
        "v2_evidence_commit": _OID,
        "owner_commit": _OID,
    }
    base.update(over)
    return AcceptedPrerequisiteIdentity(**base)  # type: ignore[arg-type]


def _linear_chain(root: Path) -> AcceptedPrerequisiteIdentity:
    _init(root)
    src = _commit(root, "c1_source")
    art = _commit(root, "c2_artifact")
    v2f = _commit(root, "c3_framework")
    v2e = _commit(root, "c4_evidence")
    own = _commit(root, "c5_owner")
    _place_gate(root)
    return _accepted(
        qualified_source_commit=src,
        qualified_source_tree=_tree(root, src),
        artifact_commit=art,
        artifact_tree=_tree(root, art),
        v2_framework_commit=v2f,
        v2_evidence_commit=v2e,
        owner_commit=own,
    )


def _req(root: Path, **over: str) -> EntryGateRequest:
    return EntryGateRequest(repo_root=str(root), **over)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
def test_not_a_git_repo_fails_closed(tmp_path):
    _place_gate(tmp_path)  # a gate but no .git
    result = _verify_against(_req(tmp_path), _accepted())
    assert not result.ok
    assert "NOT_A_GIT_REPO" in result.reasons


def test_missing_objects_fail_closed(tmp_path):
    _init(tmp_path)
    _commit(tmp_path, "only")
    _place_gate(tmp_path)
    result = _verify_against(_req(tmp_path), _accepted())  # all-b OIDs are absent
    assert not result.ok
    assert "QUALIFIED_COMMIT_ABSENT" in result.reasons
    assert "ARTIFACT_COMMIT_ABSENT" in result.reasons
    assert "GIT_OBJECT_ABSENT" in result.reasons


def test_full_chain_git_checks_pass(tmp_path):
    accepted = _linear_chain(tmp_path)
    result = _verify_against(_req(tmp_path), accepted)
    # Gate-content checks fail (synthetic gate), but every git-ancestry invariant holds.
    for key in (
        "qualified_commit_present",
        "qualified_tree_matches_git",
        "artifact_commit_present",
        "artifact_tree_matches_git",
        "artifact_proper_descends_source",
        "head_descends_artifact",
        "v2_framework_descends_artifact",
        "v2_evidence_descends_framework",
        "owner_descends_v2_evidence",
        "head_descends_owner",
        "git_objects_present",
        "git_history_complete",
        "history_not_divergent",
    ):
        assert result.checks[key] is True, key


def test_artifact_not_descendant_of_source(tmp_path):
    _init(tmp_path)
    src = _commit(tmp_path, "c1")
    _commit(tmp_path, "c2")
    # Orphan branch: an unrelated root commit that does not descend src.
    _git(tmp_path, "checkout", "-q", "--orphan", "sib")
    (tmp_path / "orphan").write_text("o", encoding="utf-8")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "orphan")
    art = _git(tmp_path, "rev-parse", "HEAD")
    _place_gate(tmp_path)
    accepted = _accepted(
        qualified_source_commit=src,
        qualified_source_tree=_tree(tmp_path, src),
        artifact_commit=art,
        artifact_tree=_tree(tmp_path, art),
    )
    result = _verify_against(_req(tmp_path), accepted)
    assert not result.ok
    assert "ARTIFACT_NOT_DESCENDANT_OF_SOURCE" in result.reasons
    assert "HISTORY_DIVERGENT" in result.reasons


def test_head_not_descendant_of_artifact(tmp_path):
    accepted = _linear_chain(tmp_path)
    # Point head_ref at the source commit, which predates the artifact.
    result = _verify_against(_req(tmp_path, head_ref=accepted.qualified_source_commit), accepted)
    assert not result.ok
    assert "HEAD_NOT_DESCENDANT_OF_ARTIFACT" in result.reasons


def test_qualified_tree_mismatch(tmp_path):
    accepted = _linear_chain(tmp_path)
    tampered = accepted.model_copy(update={"qualified_source_tree": _OID})
    result = _verify_against(_req(tmp_path), tampered)
    assert not result.ok
    assert "QUALIFIED_TREE_MISMATCH" in result.reasons


def test_shallow_history_fails_closed(tmp_path):
    origin = tmp_path / "origin"
    accepted = _linear_chain(origin)  # SHAs refer to the full origin history
    shallow = tmp_path / "shallow"
    subprocess.run(
        ["git", "clone", "-q", "--depth", "1", f"file://{origin}", str(shallow)],
        capture_output=True,
        text=True,
        check=True,
    )
    _place_gate(shallow)  # the shallow clone lacks the older accepted commits
    result = _verify_against(_req(shallow), accepted)
    assert not result.ok
    assert "GIT_HISTORY_SHALLOW" in result.reasons
