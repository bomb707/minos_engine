"""Offline truth-fixture loading (isolated). Truth identities never leave here.

This loader returns the *identity* (sha256) and small metadata of a truth VCF
fixture for offline comparison. It never returns truth variant content into a
production feature contract. Truth file paths/identities produced here may only
be consumed by offline comparison inputs (``ComparisonRequest``), never by
protocol, CONFIG, Layer 1, or submission contracts.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from minos_engine.common.errors import ComparisonError
from minos_engine.twin.identities import SHA256_RE

__all__ = ["TruthFixture", "load_truth_fixture"]


class TruthFixture(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    fixture_id: str = Field(min_length=1)
    truth_vcf_sha256: str
    region_source: str
    classification: str  # synthetic | public | practice
    sentinel: str | None = None  # test-only marker to prove non-leakage

    @field_validator("truth_vcf_sha256")
    @classmethod
    def _sha(cls, v: str) -> str:
        if not SHA256_RE.match(v):
            raise ValueError("truth_vcf_sha256 must be 64 lowercase hex characters")
        return v


def load_truth_fixture(path: str | Path) -> TruthFixture:
    p = Path(path)
    if not p.exists():
        raise ComparisonError(f"truth fixture not found: {p}")
    data: dict[str, Any] = json.loads(p.read_text(encoding="utf-8"))
    return TruthFixture.model_validate(data)
