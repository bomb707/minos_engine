"""Stable Layer 1 request/result contracts (interfaces only in Stage 0)."""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, ConfigDict, Field

__all__ = ["ProfileStatus", "ProfileRequest", "ProfileResult"]


class ProfileStatus(str, Enum):
    COMPLETE = "COMPLETE"
    PARTIAL = "PARTIAL"
    FAILED = "FAILED"


class ProfileRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    round_id: str = Field(min_length=1)
    bam_path: str
    bai_path: str
    reference_path: str
    fai_path: str
    region_source: str
    region_coordinate_convention: str
    budget_seconds: float = Field(gt=0)
    expected_hashes: dict[str, str] | None = None
    cpu_limit: int = Field(ge=1)
    memory_limit_bytes: int = Field(gt=0)
    profiler_config_version: str
    profiler_config_hash: str


class ProfileResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    status: ProfileStatus
    profile_path: str | None = None
    windows_path: str | None = None
    manifest_path: str | None = None
    failure_code: str | None = None
    fallback_required: bool = False
    warnings: tuple[str, ...] = ()
