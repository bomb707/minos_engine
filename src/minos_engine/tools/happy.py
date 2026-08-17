"""hap.py-style comparison: raw-result parsing and runner port (no execution).

Stage 1 does not run hap.py. This module parses a normalized raw result payload
(small synthetic JSON, isolated from scoring) and defines a runner port. Truth
identities appear only in these offline comparison inputs and must never reach
production prediction features (enforced by leakage/architecture tests).
"""

from __future__ import annotations

import math
from abc import ABC, abstractmethod
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from minos_engine.common.errors import ComparisonError, UnavailableError
from minos_engine.twin.unavailable import ReasonCode

__all__ = [
    "SUPPLIED_METRIC_ALLOWLIST",
    "TOP_LEVEL_KEYS",
    "RawComparison",
    "parse_raw_result",
    "HappyRunResult",
    "HappyRunner",
    "DisabledHappyRunner",
    "FakeHappyRunner",
]

_CLASSES = ("snp", "indel")
_COUNTS = ("tp", "fp", "fn")
TOP_LEVEL_KEYS = frozenset({"snp", "indel", "ti_tv", "het_hom", "supplied"})
# The only supplied metric names the authoritative raw format permits. Each is a
# recomputable rate in [0, 1]; unknown names are rejected.
SUPPLIED_METRIC_ALLOWLIST = frozenset(
    {"snp_precision", "snp_recall", "snp_f1", "indel_precision", "indel_recall", "indel_f1"}
)


def _finite_number(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ComparisonError(f"{name} must be a number, got {type(value).__name__}")
    if not math.isfinite(value):
        raise ComparisonError(f"{name} must be finite (no NaN/Infinity)")
    return float(value)


class RawComparison(BaseModel):
    """Parsed raw counts, isolated from any scoring logic."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    snp: dict[str, int]
    indel: dict[str, int]
    ti_tv: float | None = None
    het_hom: float | None = None
    supplied: dict[str, float] = Field(default_factory=dict)


def _counts(raw: dict[str, Any], klass: str) -> dict[str, int]:
    block = raw.get(klass)
    if not isinstance(block, dict):
        raise ComparisonError(f"comparison result missing '{klass}' counts")
    out: dict[str, int] = {}
    for key in _COUNTS:
        value = block.get(key)
        if not isinstance(value, int) or isinstance(value, bool):
            raise ComparisonError(f"{klass}.{key} must be an integer, got {value!r}")
        if value < 0:
            raise ComparisonError(f"{klass}.{key} must be non-negative")
        out[key] = value
    extra = set(block) - set(_COUNTS)
    if extra:
        raise ComparisonError(f"unexpected keys in {klass}: {sorted(extra)}")
    return out


def parse_raw_result(raw: dict[str, Any]) -> RawComparison:
    """Parse a raw hap.py-style result, failing closed on any malformed input.

    Every rejection raises the typed :class:`ComparisonError` — never a bare
    ValueError/OverflowError/JSON/hashing exception.
    """
    if not isinstance(raw, dict):
        raise ComparisonError("raw comparison result must be a JSON object")
    unknown_top = set(raw) - TOP_LEVEL_KEYS
    if unknown_top:
        raise ComparisonError(f"unknown top-level keys: {sorted(unknown_top)}")

    snp = _counts(raw, "snp")
    indel = _counts(raw, "indel")

    ti_tv = raw.get("ti_tv")
    het_hom = raw.get("het_hom")
    parsed: dict[str, float | None] = {"ti_tv": None, "het_hom": None}
    for name, val in (("ti_tv", ti_tv), ("het_hom", het_hom)):
        if val is None:
            continue
        num = _finite_number(val, name)
        if num < 0:
            raise ComparisonError(f"{name} must be >= 0")
        parsed[name] = num

    supplied_raw = raw.get("supplied", {})
    if not isinstance(supplied_raw, dict):
        raise ComparisonError("'supplied' must be an object of metric->value")
    unknown_supplied = set(supplied_raw) - SUPPLIED_METRIC_ALLOWLIST
    if unknown_supplied:
        raise ComparisonError(f"unknown supplied metric(s): {sorted(unknown_supplied)}")
    supplied: dict[str, float] = {}
    for key, val in supplied_raw.items():
        num = _finite_number(val, f"supplied.{key}")
        if not (0.0 <= num <= 1.0):
            raise ComparisonError(f"supplied.{key} must be a rate in [0, 1]")
        supplied[key] = num

    return RawComparison(
        snp=snp,
        indel=indel,
        ti_tv=parsed["ti_tv"],
        het_hom=parsed["het_hom"],
        supplied=supplied,
    )


class HappyRunResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    executed: bool
    raw: dict[str, Any]
    note: str | None = None


class HappyRunner(ABC):
    @abstractmethod
    def run(self, request_hash: str) -> HappyRunResult: ...


class DisabledHappyRunner(HappyRunner):
    def run(self, request_hash: str) -> HappyRunResult:
        raise UnavailableError(
            f"{ReasonCode.TOOL_EXECUTION_NOT_ENABLED.value}: real hap.py execution is not "
            "enabled in Stage 1 (fixture replay only)"
        )


class FakeHappyRunner(HappyRunner):
    """Returns a pre-supplied raw result (fixture replay); never runs hap.py."""

    def __init__(self, raw: dict[str, Any]) -> None:
        self._raw = raw

    def run(self, request_hash: str) -> HappyRunResult:
        return HappyRunResult(executed=False, raw=dict(self._raw), note="fixture replay")
