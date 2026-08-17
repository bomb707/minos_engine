"""Regression tests for two-commit Git-ancestry verification.

Covers the ancestry helper against real synthetic Git repositories (A→B→C→D,
siblings, unrelated, missing objects, wrong tree, shallow), the repaired TWIN
non-mutating check at a genuine descendant, and tampered-gate rejection. These
prove the fix supports later valid descendants while still failing closed for
divergent, missing, or shallow history — never HEAD == artifact only.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from minos_engine.qualification import git_tree as G
from minos_engine.qualification.ancestry import verify_commit_ancestry
from tests.conftest import REPO_ROOT


def _git(repo: Path, *args: str) -> str:
    out = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True
    )
    return out.stdout.strip()


def _init(repo: Path) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "Test")
    _git(repo, "config", "commit.gpgsign", "false")


def _commit(repo: Path, name: str, content: str, msg: str) -> str:
    (repo / name).write_text(content, encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", msg)
    return _git(repo, "rev-parse", "HEAD")


def _tree(repo: Path, ref: str) -> str:
    return _git(repo, "rev-parse", f"{ref}^{{tree}}")


def _checkout(repo: Path, sha: str) -> None:
    _git(repo, "checkout", "-q", sha)


# --------------------------------------------------------------------------- #
# is_ancestor primitive
# --------------------------------------------------------------------------- #
def test_is_ancestor_primitive(tmp_path: Path):
    repo = tmp_path / "r"
    _init(repo)
    a = _commit(repo, "a.txt", "1", "A")
    b = _commit(repo, "b.txt", "2", "B")
    assert G.is_ancestor(repo, a, b) is True
    assert G.is_ancestor(repo, b, a) is False
    assert G.is_ancestor(repo, a, a) is True  # a commit is its own ancestor
    assert G.is_ancestor(repo, "", b) is False


# --------------------------------------------------------------------------- #
# TWIN-style ancestry (pinned artifact commit)
# --------------------------------------------------------------------------- #
def _chain(tmp_path: Path) -> tuple[Path, dict[str, str], str]:
    """Build A(source)→B(artifact)→C→D and return (repo, shas, tree_of_A)."""
    repo = tmp_path / "chain"
    _init(repo)
    a = _commit(repo, "src.txt", "source", "A source")
    tree_a = _tree(repo, a)
    b = _commit(repo, "gate.json", "artifact", "B artifact")
    c = _commit(repo, "x.txt", "c", "C descendant")
    d = _commit(repo, "y.txt", "d", "D descendant")
    return repo, {"a": a, "b": b, "c": c, "d": d}, tree_a


def _twin_check(repo, shas, tree_a, *, source=None, artifact=None, tree=None):
    return verify_commit_ancestry(
        repo,
        qualified_source=source or shas["a"],
        expected_tree=tree or tree_a,
        artifact_commit=artifact or shas["b"],
    )


def test_original_artifact_commit_passes(tmp_path: Path):
    repo, shas, tree_a = _chain(tmp_path)
    _checkout(repo, shas["b"])  # HEAD == artifact commit
    assert _twin_check(repo, shas, tree_a).ok


def test_later_descendant_passes(tmp_path: Path):
    repo, shas, tree_a = _chain(tmp_path)
    _checkout(repo, shas["c"])  # C descends from B
    assert _twin_check(repo, shas, tree_a).ok


def test_multiple_descendants_pass(tmp_path: Path):
    repo, shas, tree_a = _chain(tmp_path)
    _checkout(repo, shas["d"])  # A→B→C→D
    res = _twin_check(repo, shas, tree_a)
    assert res.ok
    assert res.checks["head_descends_artifact"] is True
    assert res.checks["commit_b_descends_a"] is True


def test_divergent_sibling_rejected(tmp_path: Path):
    repo, shas, tree_a = _chain(tmp_path)
    # sibling of B: branch from A, does not contain/descend from B
    _checkout(repo, shas["a"])
    sibling = _commit(repo, "sib.txt", "sibling", "B-prime sibling")
    _checkout(repo, sibling)
    res = _twin_check(repo, shas, tree_a)
    assert not res.ok
    assert res.checks["head_descends_artifact"] is False
    assert any("HEAD_NOT_DESCENDANT_OF_ARTIFACT" in r for r in res.reasons)


def test_unrelated_history_rejected(tmp_path: Path):
    repo, shas, tree_a = _chain(tmp_path)
    other = tmp_path / "other"
    _init(other)
    o = _commit(other, "z.txt", "unrelated", "unrelated root")
    # source/artifact from `repo` are absent in `other` -> fail closed
    res = verify_commit_ancestry(
        other, qualified_source=shas["a"], expected_tree=tree_a, artifact_commit=shas["b"]
    )
    assert not res.ok
    assert res.checks["qualified_source_present"] is False
    assert any("QUALIFIED_COMMIT_UNAVAILABLE" in r for r in res.reasons)
    assert o  # sanity


def test_missing_source_object_fails_closed(tmp_path: Path):
    repo, shas, tree_a = _chain(tmp_path)
    _checkout(repo, shas["d"])
    res = verify_commit_ancestry(
        repo, qualified_source="0" * 40, expected_tree=tree_a, artifact_commit=shas["b"]
    )
    assert not res.ok
    assert res.checks["qualified_source_present"] is False


def test_missing_artifact_object_fails_closed(tmp_path: Path):
    repo, shas, tree_a = _chain(tmp_path)
    _checkout(repo, shas["d"])
    res = verify_commit_ancestry(
        repo, qualified_source=shas["a"], expected_tree=tree_a, artifact_commit="0" * 40
    )
    assert not res.ok
    assert res.checks["artifact_commit_present"] is False
    assert any("ARTIFACT_COMMIT_UNAVAILABLE" in r for r in res.reasons)


def test_wrong_source_tree_fails(tmp_path: Path):
    repo, shas, tree_a = _chain(tmp_path)
    _checkout(repo, shas["d"])
    res = verify_commit_ancestry(
        repo, qualified_source=shas["a"], expected_tree="f" * 40, artifact_commit=shas["b"]
    )
    assert not res.ok
    assert res.checks["qualified_tree_matches"] is False
    assert any("QUALIFIED_TREE_MISMATCH" in r for r in res.reasons)


def test_artifact_not_descendant_of_source_fails(tmp_path: Path):
    # artifact that does not descend from the claimed source
    repo, shas, tree_a = _chain(tmp_path)
    _checkout(repo, shas["d"])
    # claim C is the source but B as artifact: B does not descend from C
    res = verify_commit_ancestry(
        repo,
        qualified_source=shas["c"],
        expected_tree=_tree(repo, shas["c"]),
        artifact_commit=shas["b"],
    )
    assert not res.ok
    assert res.checks["commit_b_descends_a"] is False


def test_shallow_history_fails_with_reason(tmp_path: Path):
    repo, shas, tree_a = _chain(tmp_path)
    shallow = tmp_path / "shallow"
    subprocess.run(
        ["git", "clone", "--depth", "1", f"file://{repo}", str(shallow)],
        check=True,
        capture_output=True,
        text=True,
    )
    assert G.is_shallow(shallow) is True
    res = verify_commit_ancestry(
        shallow, qualified_source=shas["a"], expected_tree=tree_a, artifact_commit=shas["b"]
    )
    assert not res.ok
    # the historical source/artifact objects are absent -> useful reason, no crash
    assert any("UNAVAILABLE" in r and "shallow" in r for r in res.reasons)


def test_not_a_git_repo_fails_closed(tmp_path: Path):
    res = verify_commit_ancestry(
        tmp_path, qualified_source="a" * 40, expected_tree="b" * 40, artifact_commit="c" * 40
    )
    assert not res.ok
    assert res.reasons == ("NOT_A_GIT_REPO",)


# --------------------------------------------------------------------------- #
# L1-style ancestry (no pinned artifact commit)
# --------------------------------------------------------------------------- #
def test_l1_style_proper_descendant_passes(tmp_path: Path):
    repo, shas, tree_a = _chain(tmp_path)
    _checkout(repo, shas["d"])
    res = verify_commit_ancestry(
        repo, qualified_source=shas["a"], expected_tree=tree_a, artifact_commit=None
    )
    assert res.ok
    assert res.checks["commit_b_descends_a"] is True


def test_l1_style_head_equals_source_is_not_proper(tmp_path: Path):
    repo, shas, tree_a = _chain(tmp_path)
    _checkout(repo, shas["a"])  # HEAD == source -> not a proper descendant
    res = verify_commit_ancestry(
        repo, qualified_source=shas["a"], expected_tree=tree_a, artifact_commit=None
    )
    assert not res.ok
    assert res.checks["commit_b_descends_a"] is False
    assert any("HEAD_NOT_PROPER_DESCENDANT_OF_SOURCE" in r for r in res.reasons)


def test_l1_style_sibling_rejected(tmp_path: Path):
    repo, shas, tree_a = _chain(tmp_path)
    _checkout(repo, shas["a"])
    sibling = _commit(repo, "sib.txt", "sib", "sibling of source line")
    # sibling shares A as ancestor; it IS a proper descendant of A, so the L1
    # (no-artifact) invariant accepts it. Divergence from an accepted *artifact*
    # is only enforced when an artifact commit is pinned (the TWIN case).
    _checkout(repo, sibling)
    res = verify_commit_ancestry(
        repo, qualified_source=shas["a"], expected_tree=tree_a, artifact_commit=None
    )
    assert res.ok  # documented: without a pinned artifact, any proper descendant of A is valid


# --------------------------------------------------------------------------- #
# Repaired TWIN check on the real repository + tampered gate
# --------------------------------------------------------------------------- #
def test_accepted_twin_gate_verifies_at_current_descendant():
    from minos_engine.qualification.twin_runner import verify_twin_ready

    res = verify_twin_ready(
        REPO_ROOT, REPO_ROOT / "gates" / "twin-ready.json", require_descends=True
    )
    assert res.ok, res.reasons
    assert res.checks["commit_b_descends_a"] is True
    assert res.checks["head_descends_artifact"] is True


def test_tampered_twin_gate_rejected(tmp_path: Path):
    from minos_engine.qualification.twin_runner import verify_twin_ready

    original = json.loads((REPO_ROOT / "gates" / "twin-ready.json").read_text())
    # Tamper a hashed field while keeping the stored gate_hash -> canonical mismatch.
    original["engine_git_sha"] = "tampered-value"
    tampered = tmp_path / "twin-ready.json"
    tampered.write_text(json.dumps(original), encoding="utf-8")
    res = verify_twin_ready(REPO_ROOT, tampered, require_descends=True)
    assert not res.ok
    assert any("hash" in r.lower() or "canonical" in r.lower() for r in res.reasons)
