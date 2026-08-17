"""ReleaseManifest contract.

Distinguishes three non-value states for *optional future* identities
(``unavailable`` / ``unknown`` / ``not_applicable``) so that a required identity
is never represented by a bare ``null``. ``content_hash`` excludes ``created_at``
so the same build content hashes identically regardless of stamp time.
"""

from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from minos_engine.common.hashing import canonical_hash
from minos_engine.common.timestamps import is_iso8601_utc
from minos_engine.common.versions import IdentityStatus

__all__ = ["OptionalIdentity", "ReleaseManifest"]

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class OptionalIdentity(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    status: IdentityStatus
    value: str | None = None

    @field_validator("value")
    @classmethod
    def _consistent(cls, v: str | None, info: Any) -> str | None:
        status = info.data.get("status")
        if status is IdentityStatus.AVAILABLE and not (v and v.strip()):
            raise ValueError("AVAILABLE optional identity requires a non-empty value")
        if status is not IdentityStatus.AVAILABLE and v is not None:
            raise ValueError(f"{status} optional identity must have a null value")
        return v


class ReleaseManifest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: str = Field(default="release-manifest-v1")
    engine_version: str = Field(min_length=1)
    git_sha: str = Field(min_length=1)
    engine_config_hash: str
    protocol_contract_hash: str
    gatk_registry_hash: str
    minos_upstream_commit: str = Field(min_length=1)
    scorer_hash: str = Field(min_length=1)
    parameter_space_hash: str
    created_at: str
    optional_identities: dict[str, OptionalIdentity] = Field(default_factory=dict)

    @field_validator("git_sha", "minos_upstream_commit", "scorer_hash")
    @classmethod
    def _nonempty(cls, v: str, info: Any) -> str:
        if not v.strip():
            raise ValueError(f"required identity {info.field_name!r} must be non-empty")
        return v.strip()

    @field_validator(
        "engine_config_hash", "protocol_contract_hash", "gatk_registry_hash", "parameter_space_hash"
    )
    @classmethod
    def _hash_shape(cls, v: str, info: Any) -> str:
        if not _SHA256_RE.match(v):
            raise ValueError(f"{info.field_name} must be 64 lowercase hex characters")
        return v

    @field_validator("created_at")
    @classmethod
    def _ts(cls, v: str) -> str:
        if not is_iso8601_utc(v):
            raise ValueError("created_at must be timezone-aware ISO-8601")
        return v

    def content_hash(self) -> str:
        """Hash of the manifest content excluding the creation timestamp."""
        return canonical_hash(self.model_dump(mode="json", exclude={"created_at"}))
