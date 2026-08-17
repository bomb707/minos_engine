"""GATK CONFIG validation and canonicalization.

Process (Layer 2 spec §8, assignment §8):
  1. reject unknown keys;
  2. require exact JSON types (no implicit coercion, esp. no str->number);
  3. apply protocol-fixed values and explicit defaults;
  4. validate ranges/enums (against the runtime parameter space when supplied,
     else the documented static registry ranges);
  5. validate cross-parameter rules (min_assembly_region_size < max_assembly_region_size);
  6. produce a deterministic effective CONFIG;
  7. serialize with canonical JSON and compute ``config_hash``;
  8. preserve both requested and effective CONFIG.

FIXED parameters (protocol-determined) may not be changed: a request that sets
one to a non-default value is rejected.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict

from minos_engine.callers.contracts import (
    ParameterRange,
    ParameterSpaceSnapshot,
    ParameterState,
    ParameterType,
)
from minos_engine.common.canonical_json import canonical_json_bytes
from minos_engine.common.errors import ConfigValidationError
from minos_engine.common.hashing import sha256_hex

from .parameter_registry import REGISTRY, GatkParameter, GatkParameterRegistry

__all__ = ["CanonicalConfig", "canonicalize_config"]


class CanonicalConfig(BaseModel):
    """The result of canonicalizing a requested GATK CONFIG."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    requested_config: dict[str, Any]
    effective_config: dict[str, Any]
    config_hash: str
    parameter_space_hash: str | None = None

    def effective_bytes(self) -> bytes:
        return canonical_json_bytes(self.effective_config)


def _effective_range(
    name: str, param: GatkParameter, parameter_space: ParameterSpaceSnapshot | None
) -> ParameterRange:
    if parameter_space is not None and name in parameter_space.parameters:
        return parameter_space.parameters[name]
    return param.documented_range()


def _coerce_and_check(
    name: str, param: GatkParameter, value: Any, rng: ParameterRange
) -> Any:
    ptype = param.type
    if ptype is ParameterType.BOOL:
        if not isinstance(value, bool):
            raise ConfigValidationError(f"{name}: expected bool, got {type(value).__name__}")
        return value
    if ptype is ParameterType.ENUM:
        if not isinstance(value, str):
            raise ConfigValidationError(f"{name}: expected enum string, got {type(value).__name__}")
        allowed = rng.enum_values or param.enum_values or ()
        if value not in allowed:
            raise ConfigValidationError(f"{name}: {value!r} not in enum {list(allowed)}")
        return value
    if ptype is ParameterType.INT:
        # bool is a subclass of int; reject it explicitly. No str->number coercion.
        if isinstance(value, bool) or not isinstance(value, int):
            raise ConfigValidationError(f"{name}: expected int, got {type(value).__name__}")
        checked: int | float = value
    elif ptype is ParameterType.FLOAT:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ConfigValidationError(f"{name}: expected float, got {type(value).__name__}")
        checked = float(value)
    else:  # pragma: no cover - exhaustive
        raise ConfigValidationError(f"{name}: unhandled parameter type {ptype}")

    if rng.minimum is not None and checked < rng.minimum:
        raise ConfigValidationError(f"{name}: {checked} < minimum {rng.minimum}")
    if rng.maximum is not None and checked > rng.maximum:
        raise ConfigValidationError(f"{name}: {checked} > maximum {rng.maximum}")
    return checked


def canonicalize_config(
    requested: dict[str, Any],
    *,
    registry: GatkParameterRegistry = REGISTRY,
    parameter_space: ParameterSpaceSnapshot | None = None,
) -> CanonicalConfig:
    """Validate and canonicalize a requested GATK CONFIG.

    ``requested`` is the raw GATK parameter mapping (not the ``{tool, version,
    gatk_options}`` envelope). Raises :class:`ConfigValidationError` on any
    violation. On success returns both the requested and the deterministic
    effective CONFIG plus ``config_hash``.
    """
    if not isinstance(requested, dict):
        raise ConfigValidationError("requested CONFIG must be a JSON object")

    known = set(registry.names())
    unknown = [k for k in requested if k not in known]
    if unknown:
        raise ConfigValidationError(f"unknown GATK parameter(s): {sorted(unknown)}")

    effective: dict[str, Any] = {}
    for name in registry.names():
        param = registry.get(name)
        rng = _effective_range(name, param, parameter_space)
        provided = name in requested
        if param.state is ParameterState.FIXED:
            if provided and requested[name] != param.official_default:
                raise ConfigValidationError(
                    f"{name} is FIXED (protocol-determined) and may not be changed "
                    f"from {param.official_default!r}"
                )
            effective[name] = param.official_default
            continue
        value = requested[name] if provided else param.official_default
        effective[name] = _coerce_and_check(name, param, value, rng)

    _check_cross_parameters(effective)

    config_bytes = canonical_json_bytes(effective)
    return CanonicalConfig(
        requested_config=dict(requested),
        effective_config=effective,
        config_hash=sha256_hex(config_bytes),
        parameter_space_hash=(parameter_space.parameter_space_hash if parameter_space else None),
    )


def _check_cross_parameters(effective: dict[str, Any]) -> None:
    lo = effective["min_assembly_region_size"]
    hi = effective["max_assembly_region_size"]
    if not (lo < hi):
        raise ConfigValidationError(
            f"cross-parameter violation: min_assembly_region_size ({lo}) must be < "
            f"max_assembly_region_size ({hi})"
        )
