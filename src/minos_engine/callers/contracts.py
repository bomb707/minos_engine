"""Caller-scoped contracts: parameter typing, states, and the parameter space.

``ParameterSpaceSnapshot`` is the *runtime* legal-range snapshot. Its
``parameter_space_hash`` is computed over the caller + ranges content only (not
the fetch time), so an unchanged range keeps the same identity and a changed
range creates a new compatibility domain (Layer 2 spec §6 DYNAMIC RANGE RULE).
"""

from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from minos_engine.common.hashing import canonical_hash
from minos_engine.common.timestamps import is_iso8601_utc

__all__ = [
    "ACTIVE_CALLER",
    "ParameterType",
    "ParameterState",
    "ControlGroup",
    "ParameterRange",
    "ParameterSpaceSnapshot",
]

ACTIVE_CALLER = "gatk"


class ParameterType(str, Enum):
    INT = "int"
    FLOAT = "float"
    BOOL = "bool"
    ENUM = "enum"


class ParameterState(str, Enum):
    """Activation state (Layer 2 spec §7)."""

    FIXED = "FIXED"
    ACTIVE = "ACTIVE"
    CONDITIONAL = "CONDITIONAL"
    EXPERIMENTAL = "EXPERIMENTAL"
    DISABLED = "DISABLED"


class ControlGroup(str, Enum):
    EVIDENCE_FILTERS = "evidence_filters"
    LIBRARY = "library"
    ASSEMBLY_GRAPH = "assembly_graph"
    ACTIVE_REGION = "active_region"
    LIKELIHOOD = "likelihood"
    PRIOR = "prior"
    PROTOCOL = "protocol"
    EVIDENCE = "evidence"
    READ_RETENTION = "read_retention"


class ParameterRange(BaseModel):
    """One parameter's legal domain within a runtime parameter space."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    type: ParameterType
    minimum: float | int | None = None
    maximum: float | int | None = None
    enum_values: tuple[str, ...] | None = None
    default: Any = None

    @model_validator(mode="after")
    def _check(self) -> ParameterRange:
        if self.type is ParameterType.ENUM:
            if not self.enum_values:
                raise ValueError("enum parameter requires enum_values")
        else:
            if self.enum_values is not None:
                raise ValueError("enum_values only valid for enum parameters")
        if (
            self.minimum is not None
            and self.maximum is not None
            and self.minimum > self.maximum
        ):
            raise ValueError(f"minimum {self.minimum} > maximum {self.maximum}")
        return self


class ParameterSpaceSnapshot(BaseModel):
    """Immutable runtime snapshot of the caller's legal parameter space."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: str = Field(default="parameter-space-snapshot-v1")
    caller: str
    parameters: dict[str, ParameterRange]
    source: str = Field(min_length=1)
    retrieved_at: str
    parameter_space_hash: str
    stale: bool

    @field_validator("caller")
    @classmethod
    def _gatk_only(cls, v: str) -> str:
        if v != ACTIVE_CALLER:
            raise ValueError(f"caller must be '{ACTIVE_CALLER}' (GATK-only policy), got {v!r}")
        return v

    @field_validator("retrieved_at")
    @classmethod
    def _ts(cls, v: str) -> str:
        if not is_iso8601_utc(v):
            raise ValueError("retrieved_at must be timezone-aware ISO-8601")
        return v

    @staticmethod
    def compute_hash(caller: str, parameters: dict[str, ParameterRange]) -> str:
        """Deterministic hash over caller + ranges content (fetch-time excluded)."""
        payload = {
            "caller": caller,
            "parameters": {
                name: rng.model_dump(mode="json") for name, rng in sorted(parameters.items())
            },
        }
        return canonical_hash(payload)

    @model_validator(mode="after")
    def _check_hash(self) -> ParameterSpaceSnapshot:
        expected = self.compute_hash(self.caller, self.parameters)
        if self.parameter_space_hash != expected:
            raise ValueError(
                "parameter_space_hash does not match canonical ranges content "
                f"(expected {expected})"
            )
        return self
