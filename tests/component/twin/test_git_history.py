"""Git-history preflight — distinct reason codes; shallow clone fails closed."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from minos_engine.qualification.git_history import (
    HistoryReason,
    check_gate_history,
    check_repository_history,
)


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(root), *args], capture_output=True, text=True, check=True
    ).stdout.strip()


def _repo(tmp_path: Path, name: str = "repo") -> Path:
    root = tmp_path / name
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@t")
    _git(root, "config", "user.name", "t")
    return root


def _gate_dict(commit: str, tree: str, evidence):
    return {
        "schema_version": "gate-artifact-v1",
        "gate_name": "TWIN-READY",
        "status": "REJECT",  # content only; history check doesn't require PASS
        "engine_git_sha": commit,
        "input_hashes": {},
        "evidence": evidence,
        "mandatory_checks": {},
        "qualified_source_git_sha": commit,
        "qualified_source_tree_sha": tree,
        "created_at": "2026-08-17T12:00:00+00:00",
        "gate_hash": "0" * 64,
    }


def _write(root: Path, rel: str, body: str) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body)


def test_full_repo_history_ok(tmp_path):
    root = _repo(tmp_path)
    _write(root, "src/x.py", "x\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "c")
    commit = _git(root, "rev-parse", "HEAD")
    tree = _git(root, "rev-parse", "HEAD^{tree}")
    from minos_engine.qualification import git_tree as G

    ev_hash = G.hash_git_path(root, "src", commit)
    gate = root / "gate.json"
    gate.write_text(json.dumps(_gate_dict(commit, tree, [{"path": "src", "sha256": ev_hash}])))
    findings = check_gate_history(root, gate)
    assert findings == []


def test_missing_commit_reason(tmp_path):
    root = _repo(tmp_path)
    _write(root, "a.txt", "a\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "c")
    gate = root / "gate.json"
    gate.write_text(json.dumps(_gate_dict("0" * 40, "1" * 40, [])))
    codes = {f.code for f in check_gate_history(root, gate)}
    assert HistoryReason.QUALIFIED_COMMIT_UNAVAILABLE in codes


def test_tree_mismatch_reason(tmp_path):
    root = _repo(tmp_path)
    _write(root, "a.txt", "a\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "c")
    commit = _git(root, "rev-parse", "HEAD")
    gate = root / "gate.json"
    gate.write_text(json.dumps(_gate_dict(commit, "1" * 40, [])))  # wrong tree
    codes = {f.code for f in check_gate_history(root, gate)}
    assert HistoryReason.QUALIFIED_TREE_MISMATCH in codes


def test_evidence_path_missing_reason(tmp_path):
    root = _repo(tmp_path)
    _write(root, "a.txt", "a\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "c")
    commit = _git(root, "rev-parse", "HEAD")
    tree = _git(root, "rev-parse", "HEAD^{tree}")
    gate = root / "gate.json"
    gate.write_text(
        json.dumps(_gate_dict(commit, tree, [{"path": "does-not-exist", "sha256": "a" * 64}]))
    )
    codes = {f.code for f in check_gate_history(root, gate)}
    assert HistoryReason.EVIDENCE_PATH_MISSING in codes


def test_evidence_hash_mismatch_reason(tmp_path):
    root = _repo(tmp_path)
    _write(root, "a.txt", "a\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "c")
    commit = _git(root, "rev-parse", "HEAD")
    tree = _git(root, "rev-parse", "HEAD^{tree}")
    gate = root / "gate.json"
    gate.write_text(json.dumps(_gate_dict(commit, tree, [{"path": "a.txt", "sha256": "b" * 64}])))
    codes = {f.code for f in check_gate_history(root, gate)}
    assert HistoryReason.EVIDENCE_HASH_MISMATCH in codes


def test_shallow_clone_reports_incomplete_history(tmp_path):
    # Commit A then B; B's gate references A. A --depth 1 clone lacks A.
    origin = _repo(tmp_path, "origin")
    _write(origin, "a.txt", "a\n")
    _git(origin, "add", "-A")
    _git(origin, "commit", "-q", "-m", "A")
    commit_a = _git(origin, "rev-parse", "HEAD")
    tree_a = _git(origin, "rev-parse", "HEAD^{tree}")
    (origin / "gates").mkdir()
    (origin / "gates" / "twin-ready.json").write_text(json.dumps(_gate_dict(commit_a, tree_a, [])))
    _git(origin, "add", "-A")
    _git(origin, "commit", "-q", "-m", "B")

    shallow = tmp_path / "shallow"
    subprocess.run(
        ["git", "clone", "--depth", "1", "-q", origin.as_uri(), str(shallow)], check=True
    )
    assert (
        subprocess.run(
            ["git", "-C", str(shallow), "rev-parse", "--is-shallow-repository"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        == "true"
    )

    result = check_repository_history(
        shallow,
        protocol_gate=shallow / "gates" / "twin-ready.json",  # reuse as a gate referencing A
        twin_gate=shallow / "gates" / "twin-ready.json",
    )
    assert not result.ok
    assert result.shallow
    codes = {f.code for f in result.findings}
    assert HistoryReason.GIT_HISTORY_INCOMPLETE in codes
    assert HistoryReason.QUALIFIED_COMMIT_UNAVAILABLE in codes


def test_not_a_git_repo_fails_closed(tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    result = check_repository_history(
        plain, protocol_gate=plain / "p.json", twin_gate=plain / "t.json"
    )
    assert not result.ok
    assert any(f.code is HistoryReason.NOT_A_GIT_REPO for f in result.findings)
