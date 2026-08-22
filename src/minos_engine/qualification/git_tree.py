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
    "is_ancestor",
    "object_type",
    "is_commit",
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


def object_exists(root: Path, sha: str) -> bool:
    """True iff ``sha`` names an object (commit/tree/blob) in the local repo."""
    if not sha:
        return False
    return _run_text(root, ["cat-file", "-e", sha]).returncode == 0


def object_type(root: Path, sha: str) -> str | None:
    """Return the git object type of ``sha`` (``commit``/``tree``/``blob``/``tag``) or None."""
    if not sha:
        return None
    proc = _run_text(root, ["cat-file", "-t", sha])
    if proc.returncode != 0:
        return None
    return proc.stdout.strip() or None


def is_commit(root: Path, sha: str) -> bool:
    """True iff ``sha`` names a commit object."""
    return object_type(root, sha) == "commit"


def is_shallow(root: Path) -> bool:
    """True iff the repository is a shallow clone (incomplete history)."""
    proc = _run_text(root, ["rev-parse", "--is-shallow-repository"])
    return proc.returncode == 0 and proc.stdout.strip() == "true"


def is_ancestor(root: Path, ancestor: str, descendant: str) -> bool:
    """True iff ``ancestor`` is an ancestor of (or equal to) ``descendant``.

    Uses ``git merge-base --is-ancestor``: exit 0 = ancestor (or equal), exit 1 =
    not an ancestor, any other exit (e.g. a missing object in a shallow clone) is
    treated as *not proven* and returns ``False`` — fail closed. Callers that need
    to distinguish "missing object" from "genuinely not a descendant" should probe
    :func:`object_exists` / :func:`is_shallow` first for a precise reason.
    """
    if not ancestor or not descendant:
        return False
    proc = _run_text(root, ["merge-base", "--is-ancestor", ancestor, descendant])
    return proc.returncode == 0


def commit_tree_sha(root: Path, commit_sha: str) -> str | None:
    """Return the tree SHA of ``commit_sha``, or None if unavailable."""
    proc = _run_text(root, ["rev-parse", f"{commit_sha}^{{tree}}"])
    if proc.returncode != 0:
        return None
    return proc.stdout.strip() or None


def repo_root(start: Path | None = None) -> Path | None:
    """Discover the git working-tree root from ``start`` (default: cwd)."""
    base = start or Path.cwd()
    proc = _run_text(base, ["rev-parse", "--show-toplevel"])
    if proc.returncode != 0 or not proc.stdout.strip():
        return None
    return Path(proc.stdout.strip())


def list_tracked(root: Path, *pathspec: str) -> list[str]:
    """Return tracked paths (POSIX, repo-root-relative), optionally under pathspec."""
    args = ["ls-files", "-z", "--", *pathspec] if pathspec else ["ls-files", "-z"]
    proc = _run_bytes(root, args)
    if proc.returncode != 0:
        raise GitUnavailableError(f"git ls-files failed: {proc.stderr!r}")
    raw = proc.stdout.decode("utf-8")
    return [p for p in raw.split("\0") if p]


def list_tree(root: Path, ref: str, *pathspec: str) -> list[str]:
    """Return file paths present in the tree of ``ref`` (optionally under pathspec).

    Uses ``git ls-tree`` at the given ref, so the file set reflects the committed
    tree of ``ref`` regardless of the current HEAD/index. This is what evidence
    rehashing must use to reproduce a gate's digests from its qualified commit.
    """
    args = ["ls-tree", "-r", "-z", "--name-only", ref, "--", *pathspec]
    proc = _run_bytes(root, args)
    if proc.returncode != 0:
        raise GitUnavailableError(f"git ls-tree failed for {ref}: {proc.stderr!r}")
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
    """Deterministic digest over the files under ``reldir`` in the tree of ``ref``.

    The file set comes from ``git ls-tree`` at ``ref`` (not the current index), so
    the digest reproduces from the qualified commit regardless of HEAD. Ordering
    is by POSIX path relative to ``reldir``; only committed blobs are hashed.
    """
    prefix = reldir.rstrip("/") + "/"
    tracked = list_tree(root, ref, reldir)
    digests: list[FileDigest] = []
    for path in tracked:
        rel = path[len(prefix) :] if path.startswith(prefix) else path
        data = blob_bytes(root, path, ref)
        digests.append(FileDigest(path=rel, sha256=sha256_hex(data), size_bytes=len(data)))
    digests.sort(key=lambda d: d.path)
    canonical = [d.model_dump() for d in digests]
    return canonical_hash(canonical), digests


def hash_git_path(root: Path, relpath: str, ref: str = "HEAD") -> str:
    """Hash a file or directory from the committed tree of ``ref``."""
    in_tree = list_tree(root, ref, relpath)
    if in_tree == [relpath]:
        return sha256_git_file(root, relpath, ref)[0]
    if in_tree:
        return sha256_git_directory(root, relpath, ref)[0]
    raise GitUnavailableError(f"no content at {relpath!r} in ref {ref}")


def historical_blob_text(root: Path, relpath: str, ref: str) -> str | None:
    """Exact committed text of ``relpath`` at the frozen ``ref``, or ``None`` — fail closed.

    Accepted historical evidence must be verified against the commit that produced it, never
    against the current working tree: a file that was present when a stage was qualified stays
    present in that commit forever, whether or not it still exists at HEAD.

    Returns ``None`` (never a partial or substituted value) when the ref is not a commit, the
    object is unreachable — including in a shallow clone — or the blob is missing.
    """
    if not is_commit(root, ref):
        return None
    try:
        data = blob_bytes(root, relpath, ref)
    except (GitUnavailableError, OSError):
        return None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None
