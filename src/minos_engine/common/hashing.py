"""SHA-256 identity helpers over canonical JSON bytes.

A structure's identity is ``sha256(canonical_json_bytes(structure))``. This is
the only way identities are computed in the engine, so equal canonical content
always yields equal identity and different content always differs.
"""

from __future__ import annotations

import hashlib
from typing import Any

from .canonical_json import canonical_json_bytes

__all__ = ["sha256_hex", "canonical_hash"]


def sha256_hex(data: bytes) -> str:
    """Return the lowercase hex SHA-256 digest of raw bytes."""
    return hashlib.sha256(data).hexdigest()


def canonical_hash(value: Any) -> str:
    """Return the SHA-256 hex digest of the canonical JSON bytes of ``value``."""
    return sha256_hex(canonical_json_bytes(value))
