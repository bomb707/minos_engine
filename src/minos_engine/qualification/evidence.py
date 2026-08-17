"""Deterministic evidence hashing for files and directories.

A file's evidence hash is ``SHA256(exact file bytes)``. A directory's evidence
hash is a deterministic digest over its regular files:

  1. recursively enumerate regular files (excluding volatile artifacts);
  2. normalize each path relative to the directory, POSIX separators;
  3. sort by normalized path;
  4. hash each file's exact bytes;
  5. hash the canonical ordered list of
     ``[{"path","sha256","size_bytes"}, ...]``.

The excluded set covers only non-source, git-ignored artifacts (caches, compiled
files) so the digest matches a clean committed checkout regardless of whether
tooling has run.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from minos_engine.common.errors import ContractValidationError
from minos_engine.common.hashing import canonical_hash, sha256_hex

__all__ = [
    "EXCLUDED_DIR_NAMES",
    "EXCLUDED_SUFFIXES",
    "FileDigest",
    "sha256_file",
    "sha256_directory",
    "hash_path",
]

EXCLUDED_DIR_NAMES = frozenset(
    {
        ".git",
        "__pycache__",
        ".pytest_cache",
        ".ruff_cache",
        ".mypy_cache",
        ".venv",
        "htmlcov",
    }
)
EXCLUDED_SUFFIXES = frozenset({".pyc", ".pyo"})
EXCLUDED_FILE_NAMES = frozenset({".DS_Store", ".coverage", "coverage.xml"})


class FileDigest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    path: str
    sha256: str
    size_bytes: int


def sha256_file(path: str | Path) -> str:
    p = Path(path)
    if not p.is_file():
        raise ContractValidationError(f"evidence file not found: {p}")
    return sha256_hex(p.read_bytes())


def _iter_files(root: Path) -> Iterator[Path]:
    for child in sorted(root.rglob("*")):
        if child.is_dir():
            continue
        rel_parts = child.relative_to(root).parts
        if any(part in EXCLUDED_DIR_NAMES for part in rel_parts[:-1]):
            continue
        if child.name in EXCLUDED_FILE_NAMES or child.suffix in EXCLUDED_SUFFIXES:
            continue
        if not child.is_file():  # skip symlinks/sockets
            continue
        yield child


def sha256_directory(path: str | Path) -> tuple[str, list[FileDigest]]:
    """Return ``(directory_digest, per_file_digests)`` for a directory."""
    root = Path(path)
    if not root.is_dir():
        raise ContractValidationError(f"evidence directory not found: {root}")
    digests: list[FileDigest] = []
    for f in _iter_files(root):
        data = f.read_bytes()
        digests.append(
            FileDigest(
                path=f.relative_to(root).as_posix(),
                sha256=sha256_hex(data),
                size_bytes=len(data),
            )
        )
    digests.sort(key=lambda d: d.path)
    canonical = [d.model_dump() for d in digests]
    return canonical_hash(canonical), digests


def hash_path(path: str | Path) -> str:
    """Hash a path whether it is a file or a directory (deterministic)."""
    p = Path(path)
    if p.is_dir():
        return sha256_directory(p)[0]
    return sha256_file(p)
