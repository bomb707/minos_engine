"""Round-identifier and region rules that BOTH intake and Layer 2 need.

Layer 2 may not import ``minos_engine.intake`` — the architecture boundary the leakage suite
enforces, and the same reason ``layer2/ingest/validation.py`` pins Layer 1's identity-section list
locally rather than importing it. These two rules are therefore stated once here, in ``common``,
which both sides may import, and a unit test cross-checks the region rule against
``intake.contracts.Region`` so the two can never drift apart in silence.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Final

__all__ = [
    "MAX_ROUND_ID_LENGTH",
    "ONE_BASED_INCLUSIVE",
    "ZERO_BASED_HALF_OPEN",
    "is_upstream_round_id",
    "normalize_region",
    "validate_round_identifier",
]

ONE_BASED_INCLUSIVE: Final = "one_based_inclusive"
ZERO_BASED_HALF_OPEN: Final = "zero_based_half_open"

#: Upstream caps the round id at 40 characters; this engine will not exceed that.
MAX_ROUND_ID_LENGTH: Final = 40

_HEX_DIGITS: Final = frozenset("0123456789abcdef")
_REGION_RE: Final = re.compile(r"^([A-Za-z0-9_.]+)(:)(\d+)-(\d+)$")
#: Anything that could become a path segment, a shell token or an invisible difference.
_FORBIDDEN_IN_ROUND_ID: Final[tuple[str, ...]] = ("/", "\\", "..", "\x00", " ", "\t", "\n", "\r")


def is_upstream_round_id(value: str) -> bool:
    """``minos_subnet.templates.tool_params.validate_round_id``, reproduced exactly.

    Kept as a separate, literal transcription so a parity test can hold it to the official
    implementation case by case. The three rules, in upstream's order:

    1. a non-empty string of at most 40 characters;
    2. ``datetime.fromisoformat``; on failure, if it ends with ``Z``, strip that one character and
       retry -- legacy round ids carry both an offset and a trailing ``Z``, which is not valid
       ISO-8601 but exists in the data;
    3. **the result must be timezone-aware.** A naive timestamp is ambiguous and upstream refuses
       it, which is the rule this engine previously failed to apply.
    """
    if not value or not isinstance(value, str):
        return False
    if len(value) > MAX_ROUND_ID_LENGTH:
        return False
    try:
        parsed = datetime.fromisoformat(value)
    except (ValueError, TypeError):
        if not value.endswith("Z"):
            return False
        try:
            parsed = datetime.fromisoformat(value[:-1])
        except (ValueError, TypeError):
            return False
    return parsed.tzinfo is not None


def validate_round_identifier(value: str) -> str:
    """Accept a round id in either shape the engine actually meets, and nothing else.

    The frozen research corpus uses lowercase hex. The **platform** issues an ISO-8601 timestamp,
    and the live branch is :func:`is_upstream_round_id` -- upstream's own rule, transcribed --
    so a timestamp this engine accepts is exactly a timestamp the subnet accepts. In particular a
    **naive** timestamp is refused: upstream requires ``tzinfo`` and so does this.

    On top of upstream's rule the engine refuses separators, traversal fragments and whitespace.
    That is deliberately *stricter*: upstream's ``fromisoformat`` would accept a space-separated
    timestamp, and this engine will not, because a round id becomes a persisted decision's join
    key and part of a scientific identity as well as reaching a path. The strictness only ever
    removes values; it never admits one upstream would reject.
    """
    text = str(value)
    if not text.strip() or text != text.strip():
        raise ValueError("round_id must be non-empty and unpadded")
    if len(text) > MAX_ROUND_ID_LENGTH:
        raise ValueError(f"round_id is {len(text)} characters; the cap is {MAX_ROUND_ID_LENGTH}")
    for bad in _FORBIDDEN_IN_ROUND_ID:
        if bad in text:
            raise ValueError(f"round_id may not contain {bad!r}")
    if set(text) <= _HEX_DIGITS:
        return text
    if not is_upstream_round_id(text):
        raise ValueError(
            f"round_id {text!r} is neither lowercase hex nor a timezone-aware ISO-8601 timestamp"
        )
    return text


def normalize_region(source: str, coordinate_system: str) -> tuple[str, int, int]:
    """Parse ``chrN:start-end`` under an EXPLICIT convention into ``(contig, start0, end0)``.

    Conversion to zero-based half-open happens exactly once. Ambiguity is a hard failure, never a
    silent adjustment — the same rule as ``intake.contracts.Region.from_source``, which a unit test
    holds this function to.
    """
    match = _REGION_RE.match(source.strip())
    if not match:
        raise ValueError(f"malformed region string: {source!r}")
    contig = match.group(1)
    start = int(match.group(3))
    end = int(match.group(4))
    if coordinate_system == ONE_BASED_INCLUSIVE:
        start0, end0_exclusive = start - 1, end
    elif coordinate_system == ZERO_BASED_HALF_OPEN:
        start0, end0_exclusive = start, end
    else:
        raise ValueError(f"unknown coordinate convention: {coordinate_system!r}")
    if start0 < 0:
        raise ValueError(f"region start underflows to negative: {source!r}")
    if end0_exclusive <= start0:
        raise ValueError(f"region end does not follow its start: {source!r}")
    return contig, start0, end0_exclusive
