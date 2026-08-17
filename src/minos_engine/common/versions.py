"""Version and identity helpers.

Required identities (git SHA, scorer hash, upstream commit, image digests, ...)
are *runtime snapshot data*, not hard-coded truth. Stage 0 never invents them.
Where an identity is genuinely absent we distinguish three states so that a
``null`` is never silently emitted for a required field:

  * ``AVAILABLE``      — a concrete value is known.
  * ``UNAVAILABLE``    — the source exists but did not provide it (fail closed).
  * ``UNKNOWN``        — we have not looked / cannot determine it yet.
  * ``NOT_APPLICABLE`` — the identity does not apply in this context.
"""

from __future__ import annotations

import subprocess
from enum import Enum
from pathlib import Path

__all__ = ["IdentityStatus", "engine_git_sha", "python_version"]


class IdentityStatus(str, Enum):
    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"
    UNKNOWN = "unknown"
    NOT_APPLICABLE = "not_applicable"


def _repo_root() -> Path:
    # src/minos_engine/common/versions.py -> repo root is three parents up from
    # the package dir (…/src/minos_engine -> …/src -> repo root).
    return Path(__file__).resolve().parents[3]


def engine_git_sha() -> str | None:
    """Return the engine's git commit SHA, or ``None`` when unavailable.

    The caller is responsible for turning ``None`` into an explicit
    :class:`IdentityStatus` rather than emitting a null.
    """
    root = _repo_root()
    if not (root / ".git").exists():
        return None
    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(root),
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    sha = out.stdout.strip()
    if out.returncode != 0 or not sha:
        return None
    return sha


def python_version() -> str:
    import platform

    return platform.python_version()
