"""Content identity helpers and tool identity for the Twin.

Content identity is ``sha256(canonical_json_bytes(content))`` using the Stage 0
utilities — there is exactly one hashing implementation. Operational metadata
(e.g. ``created_at``) is excluded from content hashes so timestamps never
contaminate content-addressed identity.
"""

from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from minos_engine.common.hashing import canonical_hash

from .unavailable import AvailabilityStatus

__all__ = ["SHA256_RE", "content_hash", "ToolIdentity"]

SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def content_hash(model: BaseModel, *, exclude: set[str] | None = None) -> str:
    """Deterministic content hash of a model, excluding operational metadata."""
    payload: dict[str, Any] = model.model_dump(mode="json", exclude=exclude or set())
    return canonical_hash(payload)


class ToolIdentity(BaseModel):
    """Identity of an external tool (GATK, hap.py). Fails closed when unknown."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(min_length=1)
    version: str | None = None
    digest: str | None = None

    @field_validator("name")
    @classmethod
    def _lower(cls, v: str) -> str:
        return v.strip()

    @property
    def availability(self) -> AvailabilityStatus:
        # A tool identity is 'available' only when a version or an image digest
        # pins it; a bare name is not sufficient to attest a real run.
        if (self.version and self.version.strip()) or (self.digest and self.digest.strip()):
            return AvailabilityStatus.AVAILABLE
        return AvailabilityStatus.UNAVAILABLE

    def require_available(self) -> None:
        from minos_engine.common.errors import UnavailableError

        if self.availability is AvailabilityStatus.UNAVAILABLE:
            raise UnavailableError(
                f"tool identity {self.name!r} is unavailable (no version/digest pinned)"
            )
