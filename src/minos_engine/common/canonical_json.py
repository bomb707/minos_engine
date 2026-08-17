"""Canonical JSON serialization — the single source of deterministic bytes.

Rules (Overall spec §3, assignment §6):
  * UTF-8 output.
  * Object keys sorted lexicographically.
  * Compact separators ``(",", ":")`` — no incidental whitespace.
  * NaN and Infinity are rejected (finite numbers only).
  * Booleans and integers are preserved as distinct JSON types.
  * Float representation is deterministic: Python's shortest round-trip
    ``repr`` is used, which is stable across CPython runs on a platform for a
    given IEEE-754 double. Integral values that arrive as ``int`` stay ``int``;
    a ``float`` that is integral is still emitted as a float (e.g. ``2.0``),
    preserving the requested JSON type.
  * No timestamps or other ambient state are generated during serialization.

There is exactly one canonicalization utility in the engine; every hash in the
system is computed over the bytes produced here.
"""

from __future__ import annotations

import json
import math
from typing import Any

from .errors import CanonicalizationError

__all__ = ["canonical_json_bytes", "canonical_json_str"]


def _check_finite(value: Any) -> None:
    """Recursively reject NaN/Infinity anywhere in the structure."""
    if isinstance(value, float):
        if not math.isfinite(value):
            raise CanonicalizationError(
                f"non-finite float is not canonically serializable: {value!r}"
            )
    elif isinstance(value, dict):
        for k, v in value.items():
            if not isinstance(k, str):
                raise CanonicalizationError(
                    f"object keys must be strings for canonical JSON, got {type(k).__name__}"
                )
            _check_finite(v)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _check_finite(item)


def canonical_json_str(value: Any) -> str:
    """Return the canonical JSON string for ``value``.

    Raises :class:`CanonicalizationError` on NaN/Infinity, non-string object
    keys, or values ``json`` cannot serialize.
    """
    _check_finite(value)
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError) as exc:  # pragma: no cover - defensive
        raise CanonicalizationError(f"value is not canonically serializable: {exc}") from exc


def canonical_json_bytes(value: Any) -> bytes:
    """Return the canonical UTF-8 bytes for ``value``."""
    return canonical_json_str(value).encode("utf-8")
