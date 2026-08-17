"""UTC ISO-8601 timestamp validation.

Timestamps in contracts are stored as strings (not generated during
serialization). They must be timezone-aware ISO-8601 in UTC so that identity
hashes are stable and unambiguous. This is a small shared helper reused by all
contract modules to avoid duplicating the parse rule.
"""

from __future__ import annotations

from datetime import datetime, timezone

__all__ = ["parse_iso8601_utc", "is_iso8601_utc", "normalize_iso8601_utc"]


def parse_iso8601_utc(value: str) -> datetime:
    """Parse an ISO-8601 timestamp and return a UTC-aware ``datetime``.

    Accepts a trailing ``Z`` (legacy) and any explicit offset; the result is
    converted to UTC. Raises ``ValueError`` if the string is not ISO-8601 or is
    naive (no timezone).
    """
    text = value.strip()
    if text.endswith("Z") or text.endswith("z"):
        text = text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        raise ValueError(f"timestamp must be timezone-aware: {value!r}")
    return parsed.astimezone(timezone.utc)


def is_iso8601_utc(value: str) -> bool:
    try:
        parse_iso8601_utc(value)
    except ValueError:
        return False
    return True


def normalize_iso8601_utc(value: str) -> str:
    """Return the canonical UTC ISO-8601 form (``...+00:00``) of ``value``."""
    return parse_iso8601_utc(value).isoformat()
