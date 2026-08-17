"""Canonical Python runtime preflight.

The Minos subnet is tested on Python 3.12, so **CPython 3.12.x is the only
supported, tested, and qualified runtime** for the engine. This one module is
the single source of that policy; ``doctor``, Stage 1 qualification, the
TWIN-READY mandatory checks, the CLI, and CI all consult it.

The compatibility contract is the (major, minor) pair ``(3, 12)`` — the patch
level is reported but never pinned into a content hash.
"""

from __future__ import annotations

import platform
import sys

from .errors import UnsupportedRuntimeError

__all__ = [
    "SUPPORTED_MAJOR",
    "SUPPORTED_MINOR",
    "SUPPORTED_RUNTIME_LABEL",
    "current_version",
    "is_supported_runtime",
    "require_supported_runtime",
    "runtime_identity",
    "runtime_report",
]

SUPPORTED_MAJOR = 3
SUPPORTED_MINOR = 12
# Content-stable label — no patch level (the contract is 3.12.x).
SUPPORTED_RUNTIME_LABEL = f"CPython {SUPPORTED_MAJOR}.{SUPPORTED_MINOR}"


def current_version() -> tuple[int, int, int]:
    return (sys.version_info.major, sys.version_info.minor, sys.version_info.micro)


def is_supported_runtime(version: tuple[int, int] | tuple[int, int, int] | None = None) -> bool:
    """True iff ``version`` (default: the running interpreter) is Python 3.12.x."""
    major, minor = (version[0], version[1]) if version is not None else current_version()[:2]
    return major == SUPPORTED_MAJOR and minor == SUPPORTED_MINOR


def require_supported_runtime(
    version: tuple[int, int] | tuple[int, int, int] | None = None,
) -> None:
    """Raise :class:`UnsupportedRuntimeError` unless the runtime is Python 3.12.x."""
    if not is_supported_runtime(version):
        major, minor = (version[0], version[1]) if version is not None else current_version()[:2]
        raise UnsupportedRuntimeError(
            f"unsupported Python runtime {major}.{minor}: MINOS_ENGINE requires "
            f"{SUPPORTED_RUNTIME_LABEL}.x (the only tested/qualified runtime)"
        )


def runtime_identity() -> str:
    """Content-stable runtime identity (no patch level): ``CPython 3.12``."""
    return SUPPORTED_RUNTIME_LABEL


def runtime_report() -> dict[str, object]:
    """Human/JSON report of the runtime policy and the current interpreter."""
    major, minor, micro = current_version()
    return {
        "implementation": platform.python_implementation(),
        "current_version": f"{major}.{minor}.{micro}",
        "supported": SUPPORTED_RUNTIME_LABEL + ".x",
        "supported_identity": SUPPORTED_RUNTIME_LABEL,
        "is_supported": is_supported_runtime(),
    }
