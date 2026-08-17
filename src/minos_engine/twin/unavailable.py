"""Typed 'unavailable' results.

When authoritative validator behavior is not known (e.g. the pinned
AdvancedScorer formula) the Twin returns one of these rather than inventing a
value. Every unavailable result carries a machine-readable ``reason_code`` and
optional human ``detail`` (never secrets).
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, ConfigDict, Field

__all__ = ["AvailabilityStatus", "ReasonCode", "Unavailable"]


class AvailabilityStatus(str, Enum):
    AVAILABLE = "AVAILABLE"
    UNAVAILABLE = "UNAVAILABLE"


class ReasonCode(str, Enum):
    AUTHORITATIVE_SCORER_NOT_AVAILABLE = "AUTHORITATIVE_SCORER_NOT_AVAILABLE"
    TOOL_IDENTITY_UNAVAILABLE = "TOOL_IDENTITY_UNAVAILABLE"
    TOOL_EXECUTION_NOT_ENABLED = "TOOL_EXECUTION_NOT_ENABLED"
    COMPARISON_RESULT_UNAVAILABLE = "COMPARISON_RESULT_UNAVAILABLE"
    PREREQUISITE_GATE_NOT_SATISFIED = "PREREQUISITE_GATE_NOT_SATISFIED"


class Unavailable(BaseModel):
    """An explicit, typed 'we do not know this' outcome."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    status: AvailabilityStatus = AvailabilityStatus.UNAVAILABLE
    reason_code: ReasonCode
    detail: str | None = Field(default=None)
