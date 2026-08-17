"""Stable Layer 2 request/result contracts (interfaces only in Stage 0)."""

from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

__all__ = ["ControlMode", "DecisionRequest", "DecisionResult"]


class ControlMode(str, Enum):
    SAFE_BASELINE = "SAFE_BASELINE"
    BOUNDED = "BOUNDED"
    FULL_CONTEXTUAL = "FULL_CONTEXTUAL"
    REFINEMENT = "REFINEMENT"


class DecisionRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    round_id: str = Field(min_length=1)
    profile_manifest_hash: str
    parameter_space_hash: str
    safe_baseline_id: str
    controller_version: str
    model_bundle_id: str | None = None
    remaining_seconds: float = Field(ge=0)
    # In Stage 0 the profile/context payloads are opaque; typed in Stage 4+.
    profile: dict[str, Any] = Field(default_factory=dict)
    round_context: dict[str, Any] = Field(default_factory=dict)
    parameter_space_snapshot: dict[str, Any] = Field(default_factory=dict)
    compute_limits: dict[str, Any] = Field(default_factory=dict)


class DecisionResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    decision_id: str
    mode: ControlMode
    selected_config: dict[str, Any]
    config_hash: str
    decision_manifest_hash: str
    fallback_reason: str | None = None
