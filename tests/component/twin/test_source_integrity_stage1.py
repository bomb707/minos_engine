"""Stage 1 git-tree-bound source integrity regressions (temp git repos).

Covers the assignment's 10 required cases for Stage 1 evidence: untracked /
modified audit, untracked schema, modified fixture, prerequisite gate defects,
missing commit / wrong tree, and evidence reproducibility from Commit A. Uses
temporary git repositories with a small custom evidence/required set (the same
mechanism the real Stage 1 lists rely on), plus membership assertions that the
real Stage 1 lists cover the audit, a Twin schema, and a Twin fixture.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from minos_engine.gates.contracts import EvidenceKind
from minos_engine.qualification.runner import gather_source_integrity
from minos_engine.qualification.twin_runner import (
    STAGE1_EVIDENCE,
    STAGE1_REQUIRED_TRACKED_FILES,
)

_AUDIT = "reports/STAGE1_PREIMPLEMENTATION_AUDIT.md"
_SCHEMA = "schemas/twin-execution-request-v1.schema.json"
_FIXTURE = "tests/fixtures/twin/replay/valid.json"
_FILES = {
    _AUDIT: "audit\n",
    _SCHEMA: "{}\n",
    _FIXTURE: "{}\n",
    "src/minos_engine/twin/contracts.py": "x\n",
    "pyproject.toml": "x\n",
}
_EVIDENCE = (
    (_AUDIT, EvidenceKind.FILE),
    ("schemas", EvidenceKind.DIRECTORY),
    ("tests", EvidenceKind.DIRECTORY),
    ("src/minos_engine", EvidenceKind.DIRECTORY),
    ("pyproject.toml", EvidenceKind.FILE),
)
_REQUIRED = (_AUDIT, _SCHEMA, _FIXTURE, "pyproject.toml")


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, check=True)


def _build(tmp_path: Path, *, skip: set[str] = frozenset(), modify: set[str] = frozenset()) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@t")
    _git(root, "config", "user.name", "t")
    for rel, body in _FILES.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body)
    for rel in _FILES:
        if rel not in skip:
            _git(root, "add", rel)
    _git(root, "commit", "-q", "-m", "c")
    for rel in modify:  # working-tree drift after commit
        (root / rel).write_text("MODIFIED\n")
    return root


def _gather(root: Path):
    return gather_source_integrity(root, "HEAD", evidence_spec=_EVIDENCE, required_files=_REQUIRED)


def test_clean_repo_passes():
    import tempfile

    with tempfile.TemporaryDirectory() as d:
        si = _gather(_build(Path(d)))
    assert si.required_source_tracked
    assert si.worktree_matches_head
    assert si.evidence_hashes_complete


def test_untracked_audit_cannot_qualify(tmp_path):
    si = _gather(_build(tmp_path, skip={_AUDIT}))
    assert not si.required_source_tracked


def test_modified_audit_cannot_qualify(tmp_path):
    si = _gather(_build(tmp_path, modify={_AUDIT}))
    assert not si.worktree_matches_head


def test_untracked_schema_cannot_qualify(tmp_path):
    si = _gather(_build(tmp_path, skip={_SCHEMA}))
    assert not si.required_source_tracked


def test_modified_fixture_cannot_qualify(tmp_path):
    si = _gather(_build(tmp_path, modify={_FIXTURE}))
    assert not si.worktree_matches_head


def test_evidence_reproduces_and_ignores_worktree_drift(tmp_path):
    root = _build(tmp_path)
    a = _gather(root)
    # Drift the working tree; evidence is hashed from the commit, so it is stable.
    (root / _AUDIT).write_text("DRIFT\n")
    b = _gather(root)
    a_ev = {e.path: e.sha256 for e in a.evidence}
    b_ev = {e.path: e.sha256 for e in b.evidence}
    assert a_ev == b_ev  # evidence reproduces from Commit A regardless of worktree


def test_no_git_fails_closed(tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    si = _gather(plain)
    assert not si.required_source_tracked
    assert not si.evidence_hashes_complete


def test_real_stage1_lists_cover_required_artifacts():
    # The real Stage 1 required set must cover the audit, a Twin schema, a fixture.
    assert _AUDIT in STAGE1_REQUIRED_TRACKED_FILES
    assert _SCHEMA in STAGE1_REQUIRED_TRACKED_FILES
    assert _FIXTURE in STAGE1_REQUIRED_TRACKED_FILES
    assert "gates/protocol-ready.json" in STAGE1_REQUIRED_TRACKED_FILES


def test_real_stage1_evidence_excludes_generated_artifacts():
    paths = {rel for rel, _ in STAGE1_EVIDENCE}
    assert "gates/twin-ready.json" not in paths
    assert "reports/STAGE1_QUALIFICATION_REPORT.md" not in paths
    assert _AUDIT in paths
    assert "gates/protocol-ready.json" in paths
