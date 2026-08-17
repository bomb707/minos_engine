"""Immutable protocol contracts: RoundProtocolSnapshot, RoundContext, and helpers.

Required version identities (``scorer_hash``, ``minos_upstream_commit``,
``gatk_image_digest``, ``happy_image_digest``, ``reference_sha256``,
``parameter_space_hash``) are non-null and non-empty; an unknown required
identity fails closed at construction. ``stale`` is always explicit. A
snapshot's identity is deterministic from its canonical content.
"""

from __future__ import annotations

import re
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from minos_engine.common.hashing import canonical_hash
from minos_engine.common.timestamps import is_iso8601_utc
from minos_engine.intake.contracts import ArtifactIdentity, Region

__all__ = [
    "RoundStatus",
    "CommitRevealState",
    "RoundProtocolSnapshot",
    "RoundContext",
]

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class RoundStatus(str, Enum):
    PENDING = "pending"
    OPEN = "open"
    SCORING = "scoring"
    COMPLETED = "completed"
    UNKNOWN = "unknown"


class CommitRevealState(BaseModel):
    """Commit-reveal state, modeled explicitly and fail-closed.

    Commit-reveal behavior is owner-reported (enabled; score visibility delayed
    ~two epochs) but not yet verified through the integrated protocol source.
    Stage 0 represents it as typed-unavailable (``available`` defaults to
    ``False`` with a reason) until the authoritative runtime source, fields, and
    timing semantics are confirmed. We never fabricate the enabled state, phase,
    block/epoch timing, or a reveal timestamp. When a future protocol version
    exposes verified fields, populate them.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    available: bool = False
    enabled: bool | None = None
    phase: str | None = None
    detail: str | None = None


def _require_nonempty(value: str, field_name: str) -> str:
    stripped = value.strip()
    if not stripped:
        raise ValueError(f"required identity {field_name!r} must be non-empty")
    return stripped


class RoundProtocolSnapshot(BaseModel):
    """Immutable, self-identifying snapshot of live round + provenance state."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: str = Field(default="round-protocol-snapshot-v1")
    snapshot_id: str = Field(default="")
    retrieved_at: str
    round_id: str = Field(min_length=1)
    round_status: RoundStatus
    exact_region: Region
    deadline_at: str
    commit_reveal_state: CommitRevealState = Field(default_factory=CommitRevealState)
    parameter_ranges_raw: dict[str, Any]
    parameter_space_hash: str
    network_config_raw: dict[str, Any]
    minos_upstream_commit: str
    scorer_hash: str
    gatk_image_digest: str
    happy_image_digest: str
    reference_sha256: str
    source_endpoints: dict[str, str] = Field(default_factory=dict)
    stale: bool

    @field_validator("retrieved_at", "deadline_at")
    @classmethod
    def _ts(cls, v: str) -> str:
        if not is_iso8601_utc(v):
            raise ValueError("timestamp must be timezone-aware ISO-8601")
        return v

    @field_validator(
        "minos_upstream_commit",
        "scorer_hash",
        "gatk_image_digest",
        "happy_image_digest",
        "parameter_space_hash",
    )
    @classmethod
    def _nonempty_identity(cls, v: str, info: Any) -> str:
        return _require_nonempty(v, info.field_name)

    @field_validator("reference_sha256")
    @classmethod
    def _ref_sha(cls, v: str) -> str:
        s = _require_nonempty(v, "reference_sha256")
        if not _SHA256_RE.match(s):
            raise ValueError("reference_sha256 must be 64 lowercase hex characters")
        return s

    @field_validator("parameter_space_hash")
    @classmethod
    def _psh(cls, v: str) -> str:
        s = v.strip()
        if not _SHA256_RE.match(s):
            raise ValueError("parameter_space_hash must be 64 lowercase hex characters")
        return s

    def _content_without_id(self) -> dict[str, Any]:
        return self.model_dump(mode="json", exclude={"snapshot_id"})

    def compute_id(self) -> str:
        return canonical_hash(self._content_without_id())

    @model_validator(mode="after")
    def _identity(self) -> RoundProtocolSnapshot:
        expected = self.compute_id()
        if self.snapshot_id == "":
            object.__setattr__(self, "snapshot_id", expected)
        elif self.snapshot_id != expected:
            raise ValueError(f"snapshot_id does not match canonical content (expected {expected})")
        return self


class RoundContext(BaseModel):
    """The truth-free per-round context handed to Layer 1 / Layer 2."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: str = Field(default="round-context-v1")
    round_id: str = Field(min_length=1)
    status: RoundStatus
    exact_region: Region
    time_remaining_seconds: float = Field(ge=0.0)
    bam_artifact: ArtifactIdentity
    bai_artifact: ArtifactIdentity
    reference_artifact: ArtifactIdentity
    protocol_snapshot_id: str = Field(min_length=1)
