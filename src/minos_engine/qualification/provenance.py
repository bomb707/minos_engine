"""Git provenance helpers for the two-commit qualification process.

Commit A (qualifiable source) is committed with a clean worktree; the gate is
then built against exactly that tree and records:
  * ``qualified_source_git_sha``  — Commit A's commit SHA (HEAD at build time);
  * ``qualified_source_tree_sha`` — Commit A's tree object SHA (content identity);
  * ``qualification_tool_version``.
Commit B contains only the report + gate; its parent is Commit A.

There is no circular hash dependency: the gate hashes source files (Commit A),
never the report or the gate itself; the report may reference the gate hash
because the report is not part of the gate's evidence.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from pydantic import BaseModel, ConfigDict

__all__ = ["GitProvenance", "read_provenance", "verify_parent_is"]


class GitProvenance(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    head_sha: str | None
    tree_sha: str | None
    worktree_clean: bool
    parent_sha: str | None


def _git(root: Path, *args: str) -> str | None:
    try:
        proc = subprocess.run(
            ["git", *args],
            cwd=str(root),
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout.strip()


def read_provenance(root: Path) -> GitProvenance:
    head = _git(root, "rev-parse", "HEAD")
    tree = _git(root, "rev-parse", "HEAD^{tree}")
    status = _git(root, "status", "--porcelain")
    parent = _git(root, "rev-parse", "HEAD~1")
    return GitProvenance(
        head_sha=head,
        tree_sha=tree,
        worktree_clean=(status == ""),
        parent_sha=parent,
    )


def verify_parent_is(root: Path, expected_parent_sha: str) -> bool:
    """True when HEAD's parent commit equals ``expected_parent_sha`` (Commit A)."""
    parent = _git(root, "rev-parse", "HEAD~1")
    return parent is not None and parent == expected_parent_sha
