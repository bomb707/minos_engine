"""hap.py-style comparison: raw-result parsing and runner port (no execution).

Stage 1 does not run hap.py. This module parses a normalized raw result payload
(small synthetic JSON, isolated from scoring) and defines a runner port. Truth
identities appear only in these offline comparison inputs and must never reach
production prediction features (enforced by leakage/architecture tests).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from minos_engine.common.errors import ComparisonError, UnavailableError
from minos_engine.twin.unavailable import ReasonCode

__all__ = [
    "RawComparison",
    "parse_raw_result",
    "HappyRunResult",
    "HappyRunner",
    "DisabledHappyRunner",
    "FakeHappyRunner",
]

_CLASSES = ("snp", "indel")
_COUNTS = ("tp", "fp", "fn")


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
    """Parse a raw hap.py-style result, failing closed on malformed input."""
    if not isinstance(raw, dict):
        raise ComparisonError("raw comparison result must be a JSON object")
    snp = _counts(raw, "snp")
    indel = _counts(raw, "indel")
    ti_tv = raw.get("ti_tv")
    het_hom = raw.get("het_hom")
    for name, val in (("ti_tv", ti_tv), ("het_hom", het_hom)):
        if val is not None and (not isinstance(val, (int, float)) or isinstance(val, bool)):
            raise ComparisonError(f"{name} must be a number or null")
    supplied = raw.get("supplied", {})
    if not isinstance(supplied, dict):
        raise ComparisonError("'supplied' must be an object of metric->value")
    return RawComparison(
        snp=snp,
        indel=indel,
        ti_tv=None if ti_tv is None else float(ti_tv),
        het_hom=None if het_hom is None else float(het_hom),
        supplied={k: float(v) for k, v in supplied.items()},
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
