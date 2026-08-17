"""Generic stage-gate artifact.

``gate_hash`` is computed over the canonical content excluding both
``gate_hash`` itself and the ``created_at`` timestamp, so the same evidence
produces the same hash regardless of when the artifact was stamped
(determinism requirement, assignment §15).
"""

from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from minos_engine.common.hashing import canonical_hash
from minos_engine.common.timestamps import is_iso8601_utc

__all__ = ["GateStatus", "EvidenceItem", "GateArtifact"]


class GateStatus(str, Enum):
    PASS = "PASS"
    HOLD = "HOLD"
    PATCH = "PATCH"
    REJECT = "REJECT"


class EvidenceItem(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    description: str = Field(min_length=1)
    path: str = Field(min_length=1)
    sha256: str | None = None


class GateArtifact(BaseModel):
    """A signed stage-gate result artifact."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: str = Field(default="gate-artifact-v1")
    gate_name: str = Field(min_length=1)
    status: GateStatus
    engine_git_sha: str = Field(min_length=1)
    input_hashes: dict[str, str] = Field(default_factory=dict)
    evidence: tuple[EvidenceItem, ...] = ()
    mandatory_checks: dict[str, bool] = Field(default_factory=dict)
    created_at: str
    gate_hash: str = Field(default="")

    @field_validator("created_at")
    @classmethod
    def _ts(cls, v: str) -> str:
        if not is_iso8601_utc(v):
            raise ValueError("created_at must be timezone-aware ISO-8601")
        return v

    def _content_for_hash(self) -> dict[str, Any]:
        return self.model_dump(mode="json", exclude={"gate_hash", "created_at"})

    def compute_hash(self) -> str:
        return canonical_hash(self._content_for_hash())

    @model_validator(mode="after")
    def _check(self) -> GateArtifact:
        # A PASS gate must not be constructible with a false/missing mandatory check.
        if self.status is GateStatus.PASS:
            if not self.mandatory_checks:
                raise ValueError("a PASS gate requires at least one mandatory check")
            failing = [k for k, ok in self.mandatory_checks.items() if not ok]
            if failing:
                raise ValueError(f"PASS gate has failing mandatory checks: {failing}")
        expected = self.compute_hash()
        if self.gate_hash == "":
            object.__setattr__(self, "gate_hash", expected)
        elif self.gate_hash != expected:
            raise ValueError(f"gate_hash does not match canonical content (expected {expected})")
        return self
