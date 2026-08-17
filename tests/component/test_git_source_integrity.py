"""Git-tree-bound qualification: untracked/ignored/missing/modified files fail.

Uses temporary git repositories so the tests never depend on the developer's
worktree state. Reproduces the original defect (an ignored+untracked
``configs/runtime/gatk_only.yaml`` that a fresh clone lacks) and proves the fix.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from minos_engine.qualification import git_tree as G
from minos_engine.qualification.runner import (
    REQUIRED_TRACKED_FILES,
    gather_source_integrity,
)
from minos_engine.settings import Settings

_GATK_ONLY = "configs/runtime/gatk_only.yaml"
_GATK_ONLY_BODY = (
    "schema_version: runtime-policy-v1\ncaller:\n  active: gatk\n  allowed:\n    - gatk\n"
)


def _git(root: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(root), *args], capture_output=True, text=True, check=True
    )


def _init(tmp_path: Path, name: str) -> Path:
    root = tmp_path / name
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@t")
    _git(root, "config", "user.name", "t")
    return root


def _write(root: Path, rel: str, body: str = "x\n") -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body, encoding="utf-8")


def _commit(root: Path, msg: str = "c") -> str:
    _git(root, "commit", "-q", "-m", msg)
    return _git(root, "rev-parse", "HEAD").stdout.strip()


# --- git_tree primitives ----------------------------------------------------


def test_anchored_ignore_keeps_config_tracked(tmp_path):
    root = _init(tmp_path, "fixed")
    _write(root, ".gitignore", "/runtime/\n")
    _write(root, _GATK_ONLY, _GATK_ONLY_BODY)
    _git(root, "add", ".gitignore", _GATK_ONLY)
    _commit(root)
    assert G.is_tracked(root, _GATK_ONLY)
    assert G.worktree_matches_ref(root, _GATK_ONLY)
    assert G.blob_bytes(root, _GATK_ONLY).decode() == _GATK_ONLY_BODY


def test_ignored_required_file_is_not_tracked(tmp_path):
    # The original bug: `runtime/` (unanchored) ignores configs/runtime/.
    root = _init(tmp_path, "buggy")
    _write(root, ".gitignore", "runtime/\n")
    _write(root, _GATK_ONLY, _GATK_ONLY_BODY)
    _git(root, "add", "-A")
    _commit(root)
    assert not G.is_tracked(root, _GATK_ONLY)
    assert G.check_ignored(root, _GATK_ONLY)


def test_untracked_required_file_is_not_tracked(tmp_path):
    root = _init(tmp_path, "untracked")
    _write(root, "README.md", "r\n")
    _git(root, "add", "README.md")
    _commit(root)
    _write(root, _GATK_ONLY, _GATK_ONLY_BODY)  # exists on disk, never added
    assert not G.is_tracked(root, _GATK_ONLY)
    assert not G.worktree_matches_ref(root, _GATK_ONLY)


def test_modified_worktree_differs_from_committed_blob(tmp_path):
    root = _init(tmp_path, "drift")
    _write(root, _GATK_ONLY, _GATK_ONLY_BODY)
    _git(root, "add", "-A")
    _commit(root)
    committed = G.blob_bytes(root, _GATK_ONLY)
    _write(root, _GATK_ONLY, _GATK_ONLY_BODY + "# tampered\n")
    assert not G.worktree_matches_ref(root, _GATK_ONLY)
    # Evidence hashing reads the committed blob, not the drifted worktree.
    assert G.blob_bytes(root, _GATK_ONLY) == committed


def test_missing_blob_fails(tmp_path):
    root = _init(tmp_path, "missing")
    _write(root, "README.md", "r\n")
    _git(root, "add", "-A")
    _commit(root)
    with pytest.raises(G.GitUnavailableError):
        G.blob_bytes(root, "configs/does-not-exist.yaml")


def test_directory_hash_only_includes_tracked_entries(tmp_path):
    root = _init(tmp_path, "dir")
    _write(root, "d/a.txt", "a\n")
    _git(root, "add", "-A")
    _commit(root)
    before, _ = G.sha256_git_directory(root, "d")
    again, _ = G.sha256_git_directory(root, "d")
    assert before == again  # deterministic
    _write(root, "d/untracked.txt", "u\n")  # not added
    after, _ = G.sha256_git_directory(root, "d")
    assert before == after  # untracked file excluded


# --- gather_source_integrity ------------------------------------------------


def _full_repo(tmp_path: Path, name: str, *, ignore: str, track_gatk_only: bool) -> Path:
    root = _init(tmp_path, name)
    _write(root, ".gitignore", ignore)  # intended ignore content; not clobbered below
    fillers = list(REQUIRED_TRACKED_FILES) + [
        "src/minos_engine/__init__.py",
        "tests/__init__.py",
    ]
    for rel in fillers:
        if rel == ".gitignore":
            continue  # already written with the intended ignore rules
        _write(root, rel, _GATK_ONLY_BODY if rel == _GATK_ONLY else f"content-of-{rel}\n")
    if track_gatk_only:
        _git(root, "add", "-A")
    else:
        # Add everything EXCEPT the gatk_only file (ignored or intentionally skipped).
        _git(root, "add", ".gitignore")
        for rel in fillers:
            if rel not in (_GATK_ONLY, ".gitignore"):
                _git(root, "add", rel)
    _commit(root)
    return root


def test_gather_passes_when_all_required_tracked(tmp_path):
    root = _full_repo(tmp_path, "ok", ignore="/runtime/\n", track_gatk_only=True)
    si = gather_source_integrity(root)
    assert si.required_source_tracked
    assert si.evidence_hashes_complete
    assert si.worktree_matches_head
    assert all(e.sha256 for e in si.evidence)


def test_gather_fails_when_config_ignored_and_untracked(tmp_path):
    # Exact reproduction of the CI failure precondition.
    root = _full_repo(tmp_path, "ignored", ignore="runtime/\n", track_gatk_only=False)
    si = gather_source_integrity(root)
    # The required-file tracking check is what fails closed (the configs/ dir
    # digest still computes from the other tracked configs).
    assert not si.required_source_tracked
    assert G.check_ignored(root, _GATK_ONLY)


def test_gather_fails_when_required_file_untracked(tmp_path):
    root = _full_repo(tmp_path, "untr", ignore="/runtime/\n", track_gatk_only=False)
    si = gather_source_integrity(root)
    assert not si.required_source_tracked


def test_gather_fails_closed_without_git(tmp_path):
    plain = tmp_path / "nogit"
    plain.mkdir()
    si = gather_source_integrity(plain)
    assert not si.required_source_tracked
    assert not si.evidence_hashes_complete


# --- clean-checkout (export) behavior ---------------------------------------


def test_clean_export_contains_runtime_config_and_settings_loads(tmp_path):
    root = _init(tmp_path, "src")
    _write(root, ".gitignore", "/runtime/\n")
    _write(root, "configs/engine/default.yaml", "schema_version: engine-config-v1\n")
    _write(root, _GATK_ONLY, _GATK_ONLY_BODY)
    _git(root, "add", "-A")
    _commit(root)

    export = tmp_path / "export"
    export.mkdir()
    # `git archive` yields exactly the committed tree — no untracked/ignored files.
    archive = subprocess.run(
        ["git", "-C", str(root), "archive", "HEAD"], capture_output=True, check=True
    )
    subprocess.run(["tar", "-x", "-C", str(export)], input=archive.stdout, check=True)

    assert (export / _GATK_ONLY).exists(), "runtime config must survive a clean checkout"
    settings = Settings.load(base_dir=export / "configs")
    assert settings.runtime_policy.active == "gatk"


def test_bug_repro_export_missing_config_breaks_settings(tmp_path):
    root = _init(tmp_path, "bug")
    _write(root, ".gitignore", "runtime/\n")  # unanchored bug
    _write(root, "configs/engine/default.yaml", "schema_version: engine-config-v1\n")
    _write(root, _GATK_ONLY, _GATK_ONLY_BODY)
    _git(root, "add", "-A")  # gatk_only is ignored, so NOT added
    _commit(root)

    export = tmp_path / "export"
    export.mkdir()
    archive = subprocess.run(
        ["git", "-C", str(root), "archive", "HEAD"], capture_output=True, check=True
    )
    subprocess.run(["tar", "-x", "-C", str(export)], input=archive.stdout, check=True)

    assert not (export / _GATK_ONLY).exists()  # the exact CI precondition
    with pytest.raises(FileNotFoundError):
        Settings.load(base_dir=export / "configs")
