"""L2-F live-GATK parameter-space contract (committed snapshot of the scoring endpoint).

The authoritative live legal domain for every L2-F GATK CONFIG value, loaded from the
COMMITTED snapshot ``manifests/l2f_gatk_parameter_space_v1.json``. Ordinary offline paths and CI
never call the network; the snapshot is the trust anchor and an optional drift check compares a
caller-supplied fresh endpoint body against it.

This is deliberately a SEPARATE contract from the historical
``callers.contracts.ParameterSpaceSnapshot`` whose identity
``605679294caea090c8a78a5c93f3b816cb2aff05251b33446a7e312e83c205fc`` is embedded in the frozen
upstream L2-C/L2-D/L2-E lineage (split manifests, gates, reports, registry rows, snapshots and
E4/E5 evidence). That historical identity is never modified or regenerated. The L2-F plan field
``parameter_space_hash`` binds THIS live-GATK identity instead — they are distinct things.

Unlike the historical ``ParameterRange``, this contract supports ``allowed_values`` for ANY
declared type (enum, int such as ``sample_ploidy=[2]``, and bool such as
``dont_use_soft_clipped_bases=[false]``), because the live endpoint expresses singleton legal
domains that way. The endpoint silently defaults out-of-range submissions; L2-F must therefore
reject or omit such values BEFORE execution and never depend on that behavior.

GATK-only: the DeepVariant and bcftools inventories are never admitted. The endpoint's
``local_execution_parameters`` (``memory_gb``, ``num_threads``, ``ref_build``, ``threads``,
``timeout``) are local execution controls and never enter any scientific identity.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from minos_engine.callers.contracts import (
    ACTIVE_CALLER,
    ParameterRange,
    ParameterSpaceSnapshot,
    ParameterType,
)
from minos_engine.callers.gatk.config import CanonicalConfig, canonicalize_config
from minos_engine.callers.gatk.parameter_registry import REGISTRY, GatkParameterRegistry
from minos_engine.common.canonical_json import canonical_json_bytes
from minos_engine.common.errors import ConfigValidationError
from minos_engine.common.hashing import sha256_hex
from minos_engine.common.timestamps import is_iso8601_utc

__all__ = [
    "LIVE_SPACE_SCHEMA",
    "LIVE_SOURCE_ENDPOINT",
    "LIVE_OPTIONS_KEY",
    "LIVE_CALLER",
    "LOCAL_EXECUTION_PARAMETERS",
    "LiveSpaceError",
    "LiveParameter",
    "GatkLiveParameterSpace",
    "load_live_gatk_parameter_space",
    "live_gatk_parameter_space",
    "live_parameter_space",
    "canonicalize_live_gatk_config",
    "check_endpoint_drift",
]

#: the exact type inventory the live GATK contract must always present.
LIVE_TYPE_INVENTORY: dict[str, int] = {"int": 14, "float": 7, "bool": 2, "enum": 2}
LIVE_PARAMETER_COUNT = 25

_HEX64 = re.compile(r"^[0-9a-f]{64}$")
#: top-level manifest fields; anything else is rejected rather than silently trusted.
_ALLOWED_TOP_LEVEL = frozenset(
    {
        "schema_version",
        "source_endpoint",
        "options_key",
        "caller",
        "parameters",
        "parameter_space_hash",
        "source_artifact_path",
        "source_gatk_object_sha256",
        "source_raw_response_sha256",
        "retrieved_at",
        "provenance_note",
    }
)

LIVE_SPACE_SCHEMA = "l2f-gatk-live-parameter-space-v1"
LIVE_SOURCE_ENDPOINT = "https://api.theminos.ai/scoring/parameter-ranges"
LIVE_OPTIONS_KEY = "gatk_options"
LIVE_CALLER = "gatk"

#: Endpoint-declared LOCAL execution controls. They are never GATK options and never enter
#: effective_config, parameter_space_hash, config_hash, candidate_set_hash, plan_hash or job_key.
LOCAL_EXECUTION_PARAMETERS: tuple[str, ...] = (
    "memory_gb",
    "num_threads",
    "ref_build",
    "threads",
    "timeout",
)

_TYPES = ("int", "float", "bool", "enum")
#: coupling that must continue to hold across the live domain.
_COUPLED_MIN = "min_assembly_region_size"
_COUPLED_MAX = "max_assembly_region_size"

_REPO_ROOT = Path(__file__).resolve().parents[3]
_LIVE_SPACE_PATH = _REPO_ROOT / "manifests" / "l2f_gatk_parameter_space_v1.json"


class LiveSpaceError(ConfigValidationError):
    """The live-GATK parameter space, or a value proposed against it, is not admissible."""


def _is_bool(value: Any) -> bool:
    return isinstance(value, bool)


def _is_int(value: Any) -> bool:
    """Built-in integers only; ``bool`` is a Python ``int`` subclass and is EXCLUDED."""
    return isinstance(value, int) and not _is_bool(value)


def _is_finite_number(value: Any) -> bool:
    """Finite JSON int/float only — excludes bool, NaN, Infinity, strings and None."""
    if _is_bool(value) or not isinstance(value, int | float):
        return False
    return math.isfinite(float(value))


@dataclass(frozen=True)
class LiveParameter:
    """One live GATK parameter's legal domain (declared type + bounds and/or allowed values)."""

    name: str
    type: str
    minimum: float | int | None
    maximum: float | int | None
    allowed_values: tuple[Any, ...] | None
    default: Any

    def admits(self, value: Any) -> bool:
        """True iff ``value`` is admissible under this parameter's live legal domain."""
        if self.type == "bool":
            if not _is_bool(value):
                return False
        elif self.type == "int":
            if not _is_int(value):
                return False
        elif self.type == "float":
            if not _is_finite_number(value):
                return False
        elif self.type == "enum":
            if not isinstance(value, str):
                return False
        else:  # pragma: no cover - types are validated at load
            return False

        if self.allowed_values is not None:
            probe = float(value) if self.type == "float" else value
            return any((_is_bool(a) == _is_bool(probe)) and a == probe for a in self.allowed_values)
        if self.type in ("int", "float"):
            num = float(value)
            if self.minimum is not None and num < float(self.minimum):
                return False
            if self.maximum is not None and num > float(self.maximum):
                return False
        return True

    def normalized(self, value: Any) -> Any:
        """The declared-type normalization of an admissible value (float ints become floats)."""
        return float(value) if self.type == "float" else value

    def alternative_values(self) -> tuple[Any, ...]:
        """Deterministic one-at-a-time alternatives, excluding the default.

        ``allowed_values`` (declared order) when present; otherwise numeric legal min then max
        for int/float, the single opposite value for bool, and nothing for a bounded enum
        without allowed values (which cannot occur — enums always declare them).
        """
        default = self.normalized(self.default)
        if self.allowed_values is not None:
            return tuple(
                v for v in self.allowed_values if v != default or _is_bool(v) != _is_bool(default)
            )
        if self.type in ("int", "float"):
            return tuple(v for v in (self.minimum, self.maximum) if v is not None and v != default)
        if self.type == "bool":
            return (not default,)
        return ()  # pragma: no cover - enums always carry allowed_values


@dataclass(frozen=True)
class GatkLiveParameterSpace:
    """The committed, GATK-only live legal domain + its canonical scientific identity."""

    schema_version: str
    source_endpoint: str
    options_key: str
    caller: str
    parameters: tuple[LiveParameter, ...]
    parameter_space_hash: str
    source_artifact_path: str
    source_gatk_object_sha256: str
    source_raw_response_sha256: str
    retrieved_at: str

    def names(self) -> tuple[str, ...]:
        """Declared endpoint order (NOT sorted; generation sorts separately)."""
        return tuple(p.name for p in self.parameters)

    def get(self, name: str) -> LiveParameter:
        for p in self.parameters:
            if p.name == name:
                return p
        raise LiveSpaceError(f"{name!r} is not a live GATK parameter")

    def type_counts(self) -> dict[str, int]:
        return {t: sum(1 for p in self.parameters if p.type == t) for t in _TYPES}

    def validate_effective_config(self, effective_config: dict[str, Any]) -> None:
        """FAIL-CLOSED: every key must be a live GATK parameter admitting its value exactly.

        The complete inventory must be present, no extra/local-execution key may appear, and no
        value may rely on the live service's silent defaulting.
        """
        expected = set(self.names())
        got = set(effective_config)
        if got != expected:
            missing, extra = sorted(expected - got), sorted(got - expected)
            raise LiveSpaceError(
                f"effective_config parameter set mismatch (missing={missing}, extra={extra})"
            )
        for name, value in effective_config.items():
            param = self.get(name)
            if not param.admits(value):
                raise LiveSpaceError(
                    f"{name!r}={value!r} is outside the live legal domain "
                    "(the live service would silently default it)"
                )
        low = effective_config[_COUPLED_MIN]
        high = effective_config[_COUPLED_MAX]
        if not low < high:
            raise LiveSpaceError(f"{_COUPLED_MIN} ({low}) must be < {_COUPLED_MAX} ({high})")

    def scientific_content(self) -> dict[str, Any]:
        """The canonical hash preimage — retrieval time and source provenance excluded."""
        params: list[dict[str, Any]] = []
        for p in self.parameters:
            row: dict[str, Any] = {"name": p.name, "type": p.type}
            if p.minimum is not None:
                row["min"] = p.minimum
            if p.maximum is not None:
                row["max"] = p.maximum
            if p.allowed_values is not None:
                row["allowed_values"] = list(p.allowed_values)
            row["default"] = p.default
            params.append(row)
        return {
            "schema_version": self.schema_version,
            "source_endpoint": self.source_endpoint,
            "options_key": self.options_key,
            "caller": self.caller,
            "parameters": params,
        }

    def recompute_hash(self) -> str:
        return sha256_hex(canonical_json_bytes(self.scientific_content()))


def _parse_parameter(raw: Any) -> LiveParameter:
    if not isinstance(raw, dict):
        raise LiveSpaceError("each live parameter must be a JSON object")
    unknown = set(raw) - {"name", "type", "min", "max", "allowed_values", "default"}
    if unknown:
        raise LiveSpaceError(f"unknown live parameter keys: {sorted(unknown)}")
    name, ptype = raw.get("name"), raw.get("type")
    if not isinstance(name, str) or not name:
        raise LiveSpaceError("live parameter requires a non-empty name")
    if ptype not in _TYPES:
        raise LiveSpaceError(f"{name!r}: unknown live parameter type {ptype!r}")
    if "default" not in raw:
        raise LiveSpaceError(f"{name!r}: live parameter requires a default")

    minimum, maximum = raw.get("min"), raw.get("max")
    allowed = raw.get("allowed_values")
    if ptype == "enum" and allowed is None:
        raise LiveSpaceError(f"{name!r}: enum requires allowed_values")
    if allowed is not None and not isinstance(allowed, list):
        raise LiveSpaceError(f"{name!r}: allowed_values must be a list")
    if ptype in ("bool", "enum") and (minimum is not None or maximum is not None):
        raise LiveSpaceError(f"{name!r}: {ptype} may not declare min/max")

    param = LiveParameter(
        name=name,
        type=ptype,
        minimum=minimum,
        maximum=maximum,
        allowed_values=None if allowed is None else tuple(allowed),
        default=raw["default"],
    )

    # every allowed value must itself be of the declared type
    for value in param.allowed_values or ():
        if not _typed_ok(ptype, value):
            raise LiveSpaceError(f"{name!r}: allowed value {value!r} is not of type {ptype}")
    # bounds must be well-formed numbers of the declared numeric type
    if ptype in ("int", "float"):
        for bound in (minimum, maximum):
            if bound is not None and not _typed_ok(ptype, bound):
                raise LiveSpaceError(f"{name!r}: bound {bound!r} is not a valid {ptype}")
        if minimum is not None and maximum is not None and float(minimum) > float(maximum):
            raise LiveSpaceError(f"{name!r}: min {minimum} > max {maximum}")
    if not _typed_ok(ptype, param.default):
        raise LiveSpaceError(f"{name!r}: default {param.default!r} is not of type {ptype}")
    if not param.admits(param.default):
        raise LiveSpaceError(f"{name!r}: default {param.default!r} is outside its legal domain")
    return param


def _typed_ok(ptype: str, value: Any) -> bool:
    if ptype == "bool":
        return _is_bool(value)
    if ptype == "int":
        return _is_int(value)
    if ptype == "float":
        return _is_finite_number(value)
    return isinstance(value, str)


def _require_registry_agreement(parsed: tuple[LiveParameter, ...]) -> None:
    """FAIL-CLOSED cross-check against the accepted GATK registry.

    Exactly ``LIVE_PARAMETER_COUNT`` parameters, exact name-set equality with ``REGISTRY``, exact
    declared-type equality, exact default equality after declared-type normalization, and the
    exact declared type inventory. A self-consistently rehashed document that drops, adds,
    renames or retypes a parameter is therefore rejected at the production boundary.
    """
    if len(parsed) != LIVE_PARAMETER_COUNT:
        raise LiveSpaceError(
            f"live GATK inventory must contain exactly {LIVE_PARAMETER_COUNT} parameters, "
            f"got {len(parsed)}"
        )
    live_names = {p.name for p in parsed}
    registry_names = set(REGISTRY.names())
    if live_names != registry_names:
        missing, extra = sorted(registry_names - live_names), sorted(live_names - registry_names)
        raise LiveSpaceError(
            f"live GATK names disagree with the accepted registry (missing={missing}, "
            f"unknown={extra})"
        )
    for p in parsed:
        entry = REGISTRY.get(p.name)
        if entry.type.value != p.type:
            raise LiveSpaceError(
                f"{p.name!r}: live type {p.type!r} disagrees with the registry "
                f"({entry.type.value!r})"
            )
        expected = (
            float(entry.official_default) if p.type in ("int", "float") else entry.official_default
        )
        actual = p.normalized(p.default)
        if p.type in ("int", "float"):
            actual = float(actual)
        if actual != expected or _is_bool(actual) != _is_bool(expected):
            raise LiveSpaceError(
                f"{p.name!r}: live default {p.default!r} disagrees with the registry "
                f"({entry.official_default!r})"
            )
    counts = {t: sum(1 for p in parsed if p.type == t) for t in _TYPES}
    if counts != LIVE_TYPE_INVENTORY:
        raise LiveSpaceError(f"live GATK type inventory {counts} != required {LIVE_TYPE_INVENTORY}")


def _require_provenance(document: dict[str, Any]) -> None:
    """Provenance fields must be well-formed: lowercase 64-hex hashes and a truthful UTC stamp."""
    for field in (
        "parameter_space_hash",
        "source_gatk_object_sha256",
        "source_raw_response_sha256",
    ):
        value = document.get(field)
        if not isinstance(value, str) or not _HEX64.fullmatch(value):
            raise LiveSpaceError(f"{field} must be a lowercase 64-character hex digest")
    stamp = document.get("retrieved_at")
    if not isinstance(stamp, str) or not is_iso8601_utc(stamp):
        raise LiveSpaceError("retrieved_at must be a timezone-aware ISO-8601 UTC timestamp")


def load_live_gatk_parameter_space(document: dict[str, Any]) -> GatkLiveParameterSpace:
    """Parse + STRICTLY validate a live-space document (no I/O, no network).

    Self-hash consistency is necessary but never sufficient: after it, the document must also
    agree with the accepted GATK registry, present the exact type inventory, name the real
    endpoint, and carry well-formed provenance. Unknown top-level fields are rejected rather than
    silently trusted.
    """
    unknown_top = set(document) - _ALLOWED_TOP_LEVEL
    if unknown_top:
        raise LiveSpaceError(f"unknown top-level fields: {sorted(unknown_top)}")
    if document.get("source_endpoint") != LIVE_SOURCE_ENDPOINT:
        raise LiveSpaceError(
            f"source_endpoint must be {LIVE_SOURCE_ENDPOINT!r}, got "
            f"{document.get('source_endpoint')!r}"
        )
    _require_provenance(document)
    if document.get("schema_version") != LIVE_SPACE_SCHEMA:
        raise LiveSpaceError(f"unexpected schema_version {document.get('schema_version')!r}")
    if document.get("options_key") != LIVE_OPTIONS_KEY:
        raise LiveSpaceError(f"options_key must be {LIVE_OPTIONS_KEY!r}")
    if document.get("caller") != LIVE_CALLER:
        raise LiveSpaceError(f"caller must be {LIVE_CALLER!r} (GATK-only policy)")
    raw_params = document.get("parameters")
    if not isinstance(raw_params, list) or not raw_params:
        raise LiveSpaceError("parameters must be a non-empty list")

    parsed = tuple(_parse_parameter(p) for p in raw_params)
    names = [p.name for p in parsed]
    if len(set(names)) != len(names):
        dupes = sorted({n for n in names if names.count(n) > 1})
        raise LiveSpaceError(f"duplicate live parameter names: {dupes}")

    _require_registry_agreement(parsed)

    space = GatkLiveParameterSpace(
        schema_version=LIVE_SPACE_SCHEMA,
        source_endpoint=LIVE_SOURCE_ENDPOINT,
        options_key=LIVE_OPTIONS_KEY,
        caller=LIVE_CALLER,
        parameters=parsed,
        parameter_space_hash=str(document["parameter_space_hash"]),
        source_artifact_path=str(document.get("source_artifact_path", "")),
        source_gatk_object_sha256=str(document["source_gatk_object_sha256"]),
        source_raw_response_sha256=str(document["source_raw_response_sha256"]),
        retrieved_at=str(document["retrieved_at"]),
    )
    recomputed = space.recompute_hash()
    if space.parameter_space_hash != recomputed:
        raise LiveSpaceError(
            f"parameter_space_hash {space.parameter_space_hash!r} does not bind the content "
            f"(recomputed {recomputed!r})"
        )
    return space


def _load_committed() -> GatkLiveParameterSpace:
    return load_live_gatk_parameter_space(json.loads(_LIVE_SPACE_PATH.read_text(encoding="utf-8")))


_COMMITTED: GatkLiveParameterSpace | None = None


def live_gatk_parameter_space() -> GatkLiveParameterSpace:
    """THE committed live-GATK legal domain (parsed once; never a network call)."""
    global _COMMITTED  # noqa: PLW0603 - single immutable committed-snapshot cache
    if _COMMITTED is None:
        _COMMITTED = _load_committed()
    return _COMMITTED


def live_parameter_space(
    registry: GatkParameterRegistry = REGISTRY,
) -> ParameterSpaceSnapshot:
    """The internal LIVE-GATK canonicalization envelope (an IMPLEMENTATION DETAIL).

    Projects the committed live domain onto the historical ``ParameterSpaceSnapshot`` shape so the
    accepted canonicalizer enforces exact types, defaults, bounds and coupling. Its own
    ``ParameterSpaceSnapshot.parameter_space_hash`` is NOT an L2-F scientific identity and is never
    persisted or bound to a plan — :func:`canonicalize_live_gatk_config` overwrites it with the
    committed live-GATK identity.
    """
    live = live_gatk_parameter_space()
    ranges: dict[str, ParameterRange] = {}
    for name in registry.names():
        param = registry.get(name)
        lp = live.get(name)
        if param.type is ParameterType.ENUM:
            ranges[name] = ParameterRange(
                type=param.type,
                enum_values=tuple(str(v) for v in (lp.allowed_values or ())),
                default=lp.default,
            )
            continue
        minimum, maximum = lp.minimum, lp.maximum
        if lp.allowed_values is not None and lp.type in ("int", "float"):
            minimum, maximum = min(lp.allowed_values), max(lp.allowed_values)
        ranges[name] = ParameterRange(
            type=param.type, minimum=minimum, maximum=maximum, default=lp.default
        )
    phash = ParameterSpaceSnapshot.compute_hash(ACTIVE_CALLER, ranges)
    return ParameterSpaceSnapshot(
        caller=ACTIVE_CALLER,
        parameters=ranges,
        source=live.source_endpoint,
        retrieved_at=live.retrieved_at,
        parameter_space_hash=phash,
        stale=False,
    )


def canonicalize_live_gatk_config(requested: dict[str, Any]) -> CanonicalConfig:
    """THE accepted L2-F canonicalization boundary — no caller-selected trust object.

    Canonicalizes ``requested`` through the accepted canonicalizer against the live envelope
    (exact types, defaults, bounds, coupling), then validates the COMPLETE resulting
    ``effective_config`` against the committed :class:`GatkLiveParameterSpace` — including
    ``allowed_values`` for bool/int/enum — so any value the scoring API would silently default is
    rejected. The returned ``CanonicalConfig.parameter_space_hash`` is exactly the committed
    live-GATK identity, which is what every downstream L2-F identity binds.
    """
    return _canonicalize_live_against_registry(requested, REGISTRY)


def _canonicalize_live_against_registry(
    requested: dict[str, Any], registry: GatkParameterRegistry
) -> CanonicalConfig:
    """PRIVATE override-capable helper for synthetic tests ONLY. The accepted boundary is
    :func:`canonicalize_live_gatk_config` (no registry override)."""
    live = live_gatk_parameter_space()
    config = canonicalize_config(
        requested, registry=registry, parameter_space=live_parameter_space(registry)
    )
    live.validate_effective_config(dict(config.effective_config))
    # bind the COMMITTED live-GATK scientific identity, never the internal envelope's hash.
    return config.model_copy(update={"parameter_space_hash": live.parameter_space_hash})


def check_endpoint_drift(fresh_response: dict[str, Any]) -> dict[str, Any]:
    """OPTIONAL drift check against a CALLER-SUPPLIED fresh endpoint body.

    Never called by ordinary offline CI and never performs I/O itself: the caller fetches and
    passes the parsed response. Returns a report; raises nothing on drift so the caller decides.
    """
    committed = live_gatk_parameter_space()
    tools = fresh_response.get("tools")
    if not isinstance(tools, dict) or "gatk" not in tools:
        raise LiveSpaceError("fresh response has no tools.gatk object")
    gatk = tools["gatk"]
    if gatk.get("options_key") != LIVE_OPTIONS_KEY:
        raise LiveSpaceError(f"fresh tools.gatk.options_key must be {LIVE_OPTIONS_KEY!r}")
    fresh_doc = {
        "schema_version": LIVE_SPACE_SCHEMA,
        "source_endpoint": LIVE_SOURCE_ENDPOINT,
        "options_key": LIVE_OPTIONS_KEY,
        "caller": LIVE_CALLER,
        "parameters": [_normalize_source_parameter(p) for p in gatk.get("parameters", [])],
    }
    fresh_hash = sha256_hex(canonical_json_bytes(fresh_doc))
    return {
        "committed_parameter_space_hash": committed.parameter_space_hash,
        "fresh_parameter_space_hash": fresh_hash,
        "drifted": fresh_hash != committed.parameter_space_hash,
        "committed_names": list(committed.names()),
        "fresh_names": [p.get("name") for p in gatk.get("parameters", [])],
    }


def _normalize_source_parameter(raw: Any) -> dict[str, Any]:
    """Normalize one raw endpoint parameter to the committed snapshot's declared-type form."""
    if not isinstance(raw, dict):
        raise LiveSpaceError("each source parameter must be a JSON object")
    ptype = raw.get("type")
    if ptype not in _TYPES:
        raise LiveSpaceError(f"unknown source parameter type {ptype!r}")

    def _n(value: Any) -> Any:
        return float(value) if ptype == "float" else value

    row: dict[str, Any] = {"name": raw.get("name"), "type": ptype}
    if "min" in raw:
        row["min"] = _n(raw["min"])
    if "max" in raw:
        row["max"] = _n(raw["max"])
    if "allowed_values" in raw:
        row["allowed_values"] = [_n(v) for v in raw["allowed_values"]]
    row["default"] = _n(raw.get("default"))
    return row
