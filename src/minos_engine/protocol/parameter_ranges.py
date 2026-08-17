"""Parse raw protocol parameter-range payloads into a ParameterSpaceSnapshot.

The runtime legal-range snapshot is fetched, validated, versioned and hashed
here. A changed range yields a different ``parameter_space_hash`` (a new
compatibility domain) — it never silently mutates an existing space.
"""

from __future__ import annotations

from typing import Any

from minos_engine.callers.contracts import (
    ACTIVE_CALLER,
    ParameterRange,
    ParameterSpaceSnapshot,
    ParameterType,
)
from minos_engine.common.errors import ParameterSpaceError

__all__ = ["parse_parameter_space"]


def _parse_range(name: str, raw: dict[str, Any]) -> ParameterRange:
    if "type" not in raw:
        raise ParameterSpaceError(f"parameter {name!r} is missing 'type'")
    try:
        ptype = ParameterType(raw["type"])
    except ValueError as exc:
        raise ParameterSpaceError(f"parameter {name!r} has invalid type {raw['type']!r}") from exc
    enum_values = raw.get("enum_values")
    try:
        return ParameterRange(
            type=ptype,
            minimum=raw.get("minimum"),
            maximum=raw.get("maximum"),
            enum_values=tuple(enum_values) if enum_values is not None else None,
            default=raw.get("default"),
        )
    except ValueError as exc:
        raise ParameterSpaceError(f"parameter {name!r}: {exc}") from exc


def parse_parameter_space(
    raw: dict[str, Any], *, retrieved_at: str, stale: bool
) -> ParameterSpaceSnapshot:
    """Build an immutable :class:`ParameterSpaceSnapshot` from a raw payload.

    Raises :class:`ParameterSpaceError` on incomplete or non-GATK input
    (fail closed on incomplete legal parameter state).
    """
    caller = raw.get("caller", ACTIVE_CALLER)
    if caller != ACTIVE_CALLER:
        raise ParameterSpaceError(
            f"parameter space caller must be '{ACTIVE_CALLER}', got {caller!r}"
        )
    source = raw.get("source")
    if not source:
        raise ParameterSpaceError("parameter space payload is missing a non-empty 'source'")
    params_raw = raw.get("parameters")
    if not isinstance(params_raw, dict) or not params_raw:
        raise ParameterSpaceError("parameter space payload has no 'parameters'")
    parameters = {name: _parse_range(name, spec) for name, spec in params_raw.items()}
    phash = ParameterSpaceSnapshot.compute_hash(caller, parameters)
    return ParameterSpaceSnapshot(
        caller=caller,
        parameters=parameters,
        source=source,
        retrieved_at=retrieved_at,
        parameter_space_hash=phash,
        stale=stale,
    )
