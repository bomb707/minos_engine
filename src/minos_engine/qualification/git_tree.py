"""Git-tree-bound source integrity for qualification.

A PASS qualification must depend only on content that is committed in the
qualified Git commit — never on ignored, untracked, or drifted working-tree
files. This module resolves and hashes evidence from the committed blobs of a
given ref, and reports whether required files are tracked and match HEAD.

The defect this fixes: an ignored+untracked ``configs/runtime/gatk_only.yaml``
was present on the developer's disk and hashed by working-tree evidence, so a
PASS gate was emitted even though a fresh clone (CI) lacked the file. Binding
evidence to ``git cat-file`` blobs makes untracked/ignored content ineligible.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from minos_engine.common.errors import MinosEngineError
from minos_engine.common.hashing import canonical_hash, sha256_hex

from .evidence import FileDigest

__all__ = [
    "GitUnavailableError",
    "is_git_repo",
    "list_tracked",
    "is_tracked",
    "check_ignored",
    "blob_bytes",
    "worktree_matches_ref",
    "sha256_git_file",
    "sha256_git_directory",
    "hash_git_path",
]


class GitUnavailableError(MinosEngineError):
    """Git is not available or the path is not a git repository."""


def _run_text(root: Path, args: list[str]) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            ["git", "-C", str(root), *args],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:  # pragma: no cover - env failure
        raise GitUnavailableError(f"git invocation failed: {exc}") from exc


def _run_bytes(root: Path, args: list[str]) -> subprocess.CompletedProcess[bytes]:
    try:
        return subprocess.run(
            ["git", "-C", str(root), *args],
            capture_output=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:  # pragma: no cover - env failure
        raise GitUnavailableError(f"git invocation failed: {exc}") from exc


def is_git_repo(root: Path) -> bool:
    proc = _run_text(root, ["rev-parse", "--is-inside-work-tree"])
    return proc.returncode == 0 and proc.stdout.strip() == "true"


def list_tracked(root: Path, *pathspec: str) -> list[str]:
    """Return tracked paths (POSIX, repo-root-relative), optionally under pathspec."""
    args = ["ls-files", "-z", "--", *pathspec] if pathspec else ["ls-files", "-z"]
    proc = _run_bytes(root, args)
    if proc.returncode != 0:
        raise GitUnavailableError(f"git ls-files failed: {proc.stderr!r}")
    raw = proc.stdout.decode("utf-8")
    return [p for p in raw.split("\0") if p]


def is_tracked(root: Path, relpath: str) -> bool:
    proc = _run_text(root, ["ls-files", "--error-unmatch", "--", relpath])
    return proc.returncode == 0


def check_ignored(root: Path, relpath: str) -> bool:
    """True iff ``relpath`` is excluded by a gitignore rule."""
    proc = _run_text(root, ["check-ignore", relpath])
    return proc.returncode == 0


def blob_bytes(root: Path, relpath: str, ref: str = "HEAD") -> bytes:
    """Return the committed blob bytes of ``relpath`` at ``ref`` (fail closed)."""
    proc = _run_bytes(root, ["cat-file", "blob", f"{ref}:{relpath}"])
    if proc.returncode != 0:
        raise GitUnavailableError(
            f"no committed blob for {relpath!r} at {ref} (untracked or missing)"
        )
    return proc.stdout


def worktree_matches_ref(root: Path, relpath: str, ref: str = "HEAD") -> bool:
    """True iff ``relpath`` is tracked and its working-tree bytes equal ``ref``'s blob."""
    if not is_tracked(root, relpath):
        return False
    proc = _run_text(root, ["diff", "--quiet", ref, "--", relpath])
    return proc.returncode == 0


def sha256_git_file(root: Path, relpath: str, ref: str = "HEAD") -> tuple[str, int]:
    data = blob_bytes(root, relpath, ref)
    return sha256_hex(data), len(data)


def sha256_git_directory(
    root: Path, reldir: str, ref: str = "HEAD"
) -> tuple[str, list[FileDigest]]:
    """Deterministic digest over the *tracked* files under ``reldir`` at ``ref``.

    Ordering is by POSIX path relative to ``reldir``; only committed blobs are
    hashed, so untracked/ignored files are never included.
    """
    prefix = reldir.rstrip("/") + "/"
    tracked = list_tracked(root, reldir)
    digests: list[FileDigest] = []
    for path in tracked:
        rel = path[len(prefix) :] if path.startswith(prefix) else path
        data = blob_bytes(root, path, ref)
        digests.append(FileDigest(path=rel, sha256=sha256_hex(data), size_bytes=len(data)))
    digests.sort(key=lambda d: d.path)
    canonical = [d.model_dump() for d in digests]
    return canonical_hash(canonical), digests


def hash_git_path(root: Path, relpath: str, ref: str = "HEAD") -> str:
    """Hash a tracked file or directory from the committed tree at ``ref``."""
    if list_tracked(root, relpath) == [relpath]:
        return sha256_git_file(root, relpath, ref)[0]
    # Treat as a directory (or a prefix matching multiple tracked files).
    if list_tracked(root, relpath):
        return sha256_git_directory(root, relpath, ref)[0]
    raise GitUnavailableError(f"no tracked content at {relpath!r} for ref {ref}")
