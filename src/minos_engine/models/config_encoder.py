"""Deterministic encoding of one canonical GATK configuration into model input.

The 25 frozen parameters are 14 ``int``, 7 ``float``, 2 ``enum`` and 2 ``bool``. Each keeps its
type: an ``enum`` becomes one-hot over its frozen vocabulary rather than an integer, because
treating ``NONE < CONSERVATIVE < AGGRESSIVE`` as a magnitude asserts an ordering the parameter
space never claimed. Numerics are min-max scaled into ``[0, 1]`` using the frozen bounds from the
parameter space itself, so the scaling is a property of the search space and not of whichever
rows happened to be sampled — which also means it needs no fold-local fitting.

Feature order is the parameter space's own order, so the encoding is stable across runs and
machines and its identity can be hashed.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Final

from minos_engine.common.canonical_json import canonical_json_bytes
from minos_engine.common.errors import MinosEngineError
from minos_engine.common.hashing import sha256_hex

__all__ = [
    "CONFIG_ENCODER_DOMAIN",
    "CONFIG_ENCODER_SCHEMA",
    "ConfigEncoderError",
    "ConfigEncoding",
    "build_config_encoding",
]

CONFIG_ENCODER_SCHEMA: Final = "l2g-config-encoding-v1"
CONFIG_ENCODER_DOMAIN: Final = "minos:l2g-config-encoding:v1\n"
PARAMETER_SPACE_MANIFEST: Final = "manifests/l2f_gatk_parameter_space_v1.json"
PARAMETER_SPACE_HASH: Final = "b2d401918084d64023305d9262baf5011a89fe517bee4e0bd33af79fb14aee2e"


class ConfigEncoderError(MinosEngineError):
    """The configuration cannot be encoded against the frozen parameter space."""


def _is_fixed(parameter: dict[str, Any]) -> bool:
    """A parameter the frozen space pins to exactly one value."""
    allowed = parameter.get("allowed_values")
    return isinstance(allowed, list) and len(allowed) == 1


class ConfigEncoding:
    """The frozen mapping from canonical config values to an ordered float vector."""

    __slots__ = ("_parameters", "feature_names", "fixed_names")

    def __init__(self, parameters: tuple[dict[str, Any], ...]) -> None:
        self._parameters = parameters
        # A parameter whose frozen space admits exactly one value is FIXED: it is identical in
        # every configuration and carries no information a model could use. It is recorded in the
        # schema as fixed and excluded from the variable input, rather than contributing a
        # constant column that would silently dilute regularisation.
        self.fixed_names: tuple[str, ...] = tuple(
            f"cfg.{p['name']}" for p in parameters if _is_fixed(p)
        )
        names: list[str] = []
        for parameter in parameters:
            if _is_fixed(parameter):
                continue
            kind, name = parameter["type"], parameter["name"]
            if kind == "enum":
                names.extend(f"cfg.{name}={value}" for value in parameter["allowed_values"])
            else:
                names.append(f"cfg.{name}")
        self.feature_names: tuple[str, ...] = tuple(names)

    def encode(self, values: dict[str, Any]) -> tuple[float, ...]:
        """Encode one canonical configuration. Unknown or missing parameters are refused."""
        expected = {p["name"] for p in self._parameters}
        unknown = sorted(set(values) - expected)
        if unknown:
            raise ConfigEncoderError(
                f"configuration carries parameters outside the frozen space: {unknown}"
            )
        missing = sorted(expected - set(values))
        if missing:
            raise ConfigEncoderError(f"configuration is missing frozen parameters: {missing}")

        vector: list[float] = []
        for parameter in self._parameters:
            name, kind = parameter["name"], parameter["type"]
            if _is_fixed(parameter):
                allowed = list(parameter["allowed_values"])
                if values[name] != allowed[0]:
                    raise ConfigEncoderError(
                        f"{name}={values[name]!r} is fixed at {allowed[0]!r} in the frozen space"
                    )
                continue
            value = values[name]
            if kind == "enum":
                vocabulary = list(parameter["allowed_values"])
                if value not in vocabulary:
                    raise ConfigEncoderError(
                        f"{name}={value!r} is not in its frozen vocabulary {vocabulary}"
                    )
                # one-hot: an enum is a choice, never a magnitude
                vector.extend(1.0 if value == option else 0.0 for option in vocabulary)
            elif kind == "bool":
                if not isinstance(value, bool):
                    raise ConfigEncoderError(f"{name} must be a bool, got {type(value).__name__}")
                vector.append(1.0 if value else 0.0)
            else:
                if isinstance(value, bool) or not isinstance(value, int | float):
                    raise ConfigEncoderError(f"{name} must be numeric, got {value!r}")
                low, high = float(parameter["min"]), float(parameter["max"])
                if high <= low:  # pragma: no cover - guarded by the frozen space
                    raise ConfigEncoderError(f"{name} has a degenerate range")
                if not low <= float(value) <= high:
                    raise ConfigEncoderError(
                        f"{name}={value} lies outside its frozen range [{low}, {high}]"
                    )
                # scaled by the SEARCH SPACE's own bounds, so no fold-local fitting is needed
                vector.append((float(value) - low) / (high - low))
        return tuple(vector)

    def content(self) -> dict[str, Any]:
        return {
            "feature_names": list(self.feature_names),
            "fixed_parameters": list(self.fixed_names),
            "parameter_space_hash": PARAMETER_SPACE_HASH,
            "parameters": [
                {
                    "name": p["name"],
                    "type": p["type"],
                    **(
                        {"allowed_values": list(p["allowed_values"])}
                        if "allowed_values" in p
                        else {}
                    ),
                    **({"fixed": True} if _is_fixed(p) else {}),
                    **({"min": p["min"], "max": p["max"]} if "min" in p else {}),
                }
                for p in self._parameters
            ],
            "scaling": "MINMAX_FROM_FROZEN_PARAMETER_SPACE_BOUNDS",
            "enum_encoding": "ONE_HOT_OVER_FROZEN_VOCABULARY",
            "schema_version": CONFIG_ENCODER_SCHEMA,
        }

    def identity(self) -> str:
        return sha256_hex(
            CONFIG_ENCODER_DOMAIN.encode("utf-8") + canonical_json_bytes(self.content())
        )


def build_config_encoding(root: Path | None = None) -> ConfigEncoding:
    """Build the encoder from the ACCEPTED live parameter-space authority.

    An earlier revision parsed the manifest directly and trusted its embedded
    ``parameter_space_hash`` — so a document whose parameter CONTENT had been altered while that
    field was left untouched would have been accepted. ``load_committed_live_gatk_parameter_space``
    already proves the whole chain: strict JSON with duplicate-key rejection, the source
    artifact's byte SHA, manifest/source agreement, the exact 25 names, types, defaults and
    allowed values, and a RECOMPUTED parameter-space hash. The encoder is built from that object,
    so the tamper case is refused before any encoding exists.
    """
    from minos_engine.experiments.gatk_live_space import (
        load_committed_live_gatk_parameter_space,
    )

    _ = root  # the accepted loader reads only the two fixed committed paths
    space = load_committed_live_gatk_parameter_space()
    if space.parameter_space_hash != PARAMETER_SPACE_HASH:
        raise ConfigEncoderError(
            f"parameter space is {space.parameter_space_hash}, expected {PARAMETER_SPACE_HASH}"
        )
    parameters = tuple(
        {
            "name": p.name,
            "type": p.type,
            **({"allowed_values": list(p.allowed_values)} if p.allowed_values else {}),
            **({"min": p.minimum, "max": p.maximum} if p.minimum is not None else {}),
        }
        for p in space.parameters
    )
    if len(parameters) != 25:
        raise ConfigEncoderError(f"expected 25 frozen parameters, found {len(parameters)}")
    return ConfigEncoding(parameters)
