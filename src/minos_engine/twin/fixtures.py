"""Deterministic Twin replay fixtures (non-truth). Safe to import anywhere.

A replay fixture bundles a Twin execution request with a *raw comparison result*
(already-compared counts) plus the truth/query VCF identities used offline. It
carries provenance (id, classification) so the CLI/tests can replay it
deterministically. Large BAM/VCF/reference/truth bytes are never included — only
small synthetic JSON and content identities.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from minos_engine.common.errors import TwinError
from minos_engine.twin.identities import SHA256_RE, ToolIdentity

from .contracts import TwinExecutionRequest

__all__ = ["TwinReplayFixture", "load_replay_fixture", "fixtures_dir"]


class TwinReplayFixture(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    fixture_id: str = Field(min_length=1)
    classification: str  # synthetic | public | practice
    request: TwinExecutionRequest
    comparison_raw: dict[str, Any]
    truth_vcf_sha256: str
    query_vcf_sha256: str
    comparison_tool: ToolIdentity
    expected: dict[str, str] = Field(default_factory=dict)

    @field_validator("truth_vcf_sha256", "query_vcf_sha256")
    @classmethod
    def _sha(cls, v: str, info: Any) -> str:
        if not SHA256_RE.match(v):
            raise ValueError(f"{info.field_name} must be 64 lowercase hex characters")
        return v


def load_replay_fixture(path: str | Path) -> TwinReplayFixture:
    p = Path(path)
    if not p.exists():
        raise TwinError(f"twin replay fixture not found: {p}")
    data = json.loads(p.read_text(encoding="utf-8"))
    return TwinReplayFixture.model_validate(data)


def fixtures_dir() -> Path:
    """Directory of the tracked on-disk Twin fixtures."""
    return Path(__file__).resolve().parents[3] / "tests" / "fixtures" / "twin"
