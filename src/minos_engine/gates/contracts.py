"""Generic stage-gate artifact.

``gate_hash`` is computed over the canonical content excluding both
``gate_hash`` itself and the ``created_at`` timestamp, so the same evidence
produces the same hash regardless of when the artifact was stamped
(determinism requirement, assignment §15).
"""

from __future__ import annotations

import re
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from minos_engine.common.hashing import canonical_hash
from minos_engine.common.timestamps import is_iso8601_utc

from .required_checks import required_checks_for

__all__ = ["GateStatus", "EvidenceKind", "EvidenceItem", "GateArtifact"]

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class GateStatus(str, Enum):
    PASS = "PASS"
    HOLD = "HOLD"
    PATCH = "PATCH"
    REJECT = "REJECT"


class EvidenceKind(str, Enum):
    FILE = "file"
    DIRECTORY = "directory"


class EvidenceItem(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    description: str = Field(min_length=1)
    path: str = Field(min_length=1)
    kind: EvidenceKind = EvidenceKind.FILE
    sha256: str | None = None
    size_bytes: int | None = None

    @field_validator("sha256")
    @classmethod
    def _sha_shape(cls, v: str | None) -> str | None:
        if v is not None and not _SHA256_RE.match(v):
            raise ValueError("evidence sha256 must be 64 lowercase hex characters")
        return v


class GateArtifact(BaseModel):
    """A signed stage-gate result artifact.

    PASS invariants (not constructible otherwise):
      * no mandatory check is false;
      * for a *registered* gate name, mandatory_checks contains every required
        check and each required value is true (unknown checks may be retained);
      * every evidence item carries a non-null sha256;
      * for a registered gate name, source provenance is present.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: str = Field(default="gate-artifact-v1")
    gate_name: str = Field(min_length=1)
    status: GateStatus
    engine_git_sha: str = Field(min_length=1)
    input_hashes: dict[str, str] = Field(default_factory=dict)
    evidence: tuple[EvidenceItem, ...] = ()
    mandatory_checks: dict[str, bool] = Field(default_factory=dict)
    # Source provenance (two-commit qualification).
    qualified_source_git_sha: str | None = None
    qualified_source_tree_sha: str | None = None
    qualification_tool_version: str | None = None
    created_at: str
    gate_hash: str = Field(default="")

    @field_validator("created_at")
    @classmethod
    def _ts(cls, v: str) -> str:
        if not is_iso8601_utc(v):
            raise ValueError("created_at must be timezone-aware ISO-8601")
        return v

    def _content_for_hash(self) -> dict[str, Any]:
        # created_at is excluded so re-stamping does not change identity.
        # gate_hash is excluded to avoid a self-referential hash.
        return self.model_dump(mode="json", exclude={"gate_hash", "created_at"})

    def compute_hash(self) -> str:
        return canonical_hash(self._content_for_hash())

    def _enforce_pass_invariants(self) -> None:
        if not self.mandatory_checks:
            raise ValueError("a PASS gate requires at least one mandatory check")
        failing = [k for k, ok in self.mandatory_checks.items() if not ok]
        if failing:
            raise ValueError(f"PASS gate has failing mandatory checks: {failing}")

        missing_sha = [e.path for e in self.evidence if e.sha256 is None]
        if missing_sha:
            raise ValueError(f"PASS gate evidence missing sha256: {missing_sha}")

        required = required_checks_for(self.gate_name)
        if required:
            present = set(self.mandatory_checks)
            absent = sorted(required - present)
            if absent:
                raise ValueError(f"PASS {self.gate_name} gate missing required checks: {absent}")
            not_true = sorted(name for name in required if not self.mandatory_checks[name])
            if not_true:
                raise ValueError(f"PASS {self.gate_name} required checks not true: {not_true}")
            for field_name in (
                "qualified_source_git_sha",
                "qualified_source_tree_sha",
                "qualification_tool_version",
            ):
                value = getattr(self, field_name)
                if not value or not value.strip():
                    raise ValueError(f"PASS {self.gate_name} gate requires {field_name}")

    @model_validator(mode="after")
    def _check(self) -> GateArtifact:
        if self.status is GateStatus.PASS:
            self._enforce_pass_invariants()
        expected = self.compute_hash()
        if self.gate_hash == "":
            object.__setattr__(self, "gate_hash", expected)
        elif self.gate_hash != expected:
            raise ValueError(f"gate_hash does not match canonical content (expected {expected})")
        return self
