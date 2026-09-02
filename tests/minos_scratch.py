"""Where a test may create MINOS physical scratch state, discovered rather than hard-coded.

The operator's machine has a canonical MINOS physical root, and the campaign specs are emphatic
that scratch state must stay inside it and outside the repository: MINOS directories scattered
across a working filesystem are exactly the mess that rule exists to prevent.

A CI runner has no MINOS physical root at all. Hard-coding the operator's path made these
fixtures die at setup on every other machine — the constraint is about protecting a filesystem
that exists, and asserting it unconditionally turned it into a portability defect.

So the root is discovered. When the canonical root is present and writable, scratch state goes
there and containment is enforced there. When it is absent, there is no MINOS filesystem to keep
tidy, and the caller's temporary directory is used instead. Either way the chosen root is
returned, so the containment assertion is made against the root actually in force rather than
against a constant that may not apply.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

__all__ = ["CANONICAL_MINOS_ROOT", "minos_scratch_root"]

#: the operator's canonical MINOS physical root. Used when it exists; never required to.
CANONICAL_MINOS_ROOT = Path("/home/hr/bittensor")

#: scratch lives beside the MINOS trees, never inside a repository working copy.
_SCRATCH_DIRECTORY = ".minos_scratch"


def _usable(root: Path) -> bool:
    try:
        return root.is_dir() and not root.is_symlink() and os.access(root, os.W_OK | os.X_OK)
    except OSError:  # pragma: no cover - an unreadable path is simply not usable
        return False


def minos_scratch_root(prefix: str, *, fallback: Path) -> tuple[Path, Path]:
    """Create a scratch directory and return ``(scratch, effective_root)``.

    ``fallback`` is used when the canonical MINOS root is absent or not writable — pass a
    pytest-provided temporary directory. The returned ``effective_root`` is the root the scratch
    directory is guaranteed to lie under, and is what a containment assertion should use.

    The scratch directory is always outside the repository working copy, on both paths.
    """
    if _usable(CANONICAL_MINOS_ROOT):
        effective_root = CANONICAL_MINOS_ROOT
        parent = effective_root / _SCRATCH_DIRECTORY
    else:
        effective_root = fallback.resolve()
        parent = effective_root
    parent.mkdir(parents=True, exist_ok=True)

    scratch = Path(tempfile.mkdtemp(prefix=prefix, dir=parent)).resolve()
    repository = Path(__file__).resolve().parent.parent
    if not scratch.is_relative_to(effective_root.resolve()):  # pragma: no cover - fail closed
        raise AssertionError(f"scratch {scratch} is not under {effective_root}")
    if scratch.is_relative_to(repository):  # pragma: no cover - fail closed
        raise AssertionError(f"scratch {scratch} is inside the repository at {repository}")
    return scratch, effective_root


def prune_scratch_parent(scratch: Path) -> None:
    """Remove the shared scratch parent if this run left it empty. Never forced."""
    import contextlib

    with contextlib.suppress(OSError):
        scratch.parent.rmdir()
