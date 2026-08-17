"""Official submission contract (envelope construction only — no side effects).

Protocol owns the submission contract; Layer 2 returns a decision and never
submits (Overall spec §6). CONFIG *generation* and CONFIG *submission* are
separate operations (assignment rule 10): this module builds and hashes the
submission envelope but performs no network I/O. The live submit call lives on
the live client and raises ``UnavailableError`` in Stage 0.

Envelope shape mirrors the platform: ``{"tool": "gatk", "version": ...,
"gatk_options": {...}}``. Infra-only keys are stripped before submission.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from minos_engine.common.hashing import canonical_hash
from minos_engine.callers.contracts import ACTIVE_CALLER

__all__ = ["INFRA_KEYS", "SubmissionEnvelope", "build_submission_envelope"]

# Infra-only keys never sent to the platform (they are execution details).
INFRA_KEYS = frozenset({"threads", "memory_gb", "timeout", "ref_build", "num_threads"})


class SubmissionEnvelope(BaseModel):
    """The exact object that would be submitted to the platform."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    tool: str = Field(default=ACTIVE_CALLER)
    version: str = Field(min_length=1)
    gatk_options: dict[str, Any]

    @field_validator("tool")
    @classmethod
    def _gatk_only(cls, v: str) -> str:
        if v != ACTIVE_CALLER:
            raise ValueError(f"submission tool must be '{ACTIVE_CALLER}', got {v!r}")
        return v

    def canonical_bytes(self) -> bytes:
        from minos_engine.common.canonical_json import canonical_json_bytes

        return canonical_json_bytes(self.model_dump(mode="json"))

    def submission_hash(self) -> str:
        return canonical_hash(self.model_dump(mode="json"))


def build_submission_envelope(effective_config: dict[str, Any], *, version: str) -> SubmissionEnvelope:
    """Wrap an effective GATK CONFIG in a submission envelope, stripping infra keys.

    ``effective_config`` is expected to be an already-validated, canonicalized
    GATK parameter mapping (see ``callers.gatk.config``). This function does not
    revalidate parameter legality; it only shapes the envelope.
    """
    gatk_options = {k: v for k, v in effective_config.items() if k not in INFRA_KEYS}
    return SubmissionEnvelope(tool=ACTIVE_CALLER, version=version, gatk_options=gatk_options)
