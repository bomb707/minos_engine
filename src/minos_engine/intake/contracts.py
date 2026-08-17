"""Immutable intake contracts: Region and ArtifactIdentity.

These are frozen pydantic v2 models. ``extra="forbid"`` rejects unknown keys so
that a contract drift fails loudly rather than silently absorbing data.
"""

from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from minos_engine.common.hashing import canonical_hash
from minos_engine.common.timestamps import is_iso8601_utc

__all__ = ["CoordinateSystem", "Region", "ArtifactIdentity"]

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_CONTIG_RE = re.compile(r"^(chr([1-9]|1[0-9]|2[0-2]|X|Y|M))$")

CoordinateSystem = str  # "zero_based_half_open" | "one_based_inclusive"


class Region(BaseModel):
    """One unambiguous zero-based, half-open genomic interval.

    ``start0`` is inclusive, ``end0_exclusive`` is exclusive, so
    ``length_bp == end0_exclusive - start0``. The source string and its
    coordinate convention are preserved for provenance (Layer 1 spec §5).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    source: str = Field(min_length=1, description="Original region string, e.g. 'chr19:13000000-23000000'")
    source_coordinate_system: str = Field(
        description="Convention of the source string (e.g. one_based_inclusive)"
    )
    contig: str = Field(min_length=1)
    start0: int = Field(ge=0, description="Zero-based inclusive start")
    end0_exclusive: int = Field(gt=0, description="Zero-based exclusive end")
    length_bp: int = Field(gt=0)
    verified: bool = False

    @field_validator("contig")
    @classmethod
    def _contig_shape(cls, v: str) -> str:
        if not _CONTIG_RE.match(v):
            raise ValueError(f"unsupported contig identifier: {v!r}")
        return v

    @model_validator(mode="after")
    def _check_interval(self) -> Region:
        if not (self.start0 < self.end0_exclusive):
            raise ValueError(
                f"require start0 < end0_exclusive, got {self.start0} !< {self.end0_exclusive}"
            )
        if self.length_bp != self.end0_exclusive - self.start0:
            raise ValueError(
                f"length_bp ({self.length_bp}) != end0_exclusive - start0 "
                f"({self.end0_exclusive - self.start0})"
            )
        return self


class ArtifactIdentity(BaseModel):
    """Content identity of a round artifact (BAM/BAI/FASTA/...).

    A filename alone is never an identity; a valid identity requires a content
    ``sha256`` and a non-negative ``size_bytes`` in addition to the ``uri``.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    uri: str = Field(min_length=1)
    sha256: str
    size_bytes: int = Field(ge=0)
    media_type: str = Field(min_length=1)
    created_at_or_observed_at: str

    @field_validator("sha256")
    @classmethod
    def _sha_shape(cls, v: str) -> str:
        if not _SHA256_RE.match(v):
            raise ValueError("sha256 must be 64 lowercase hex characters")
        return v

    @field_validator("created_at_or_observed_at")
    @classmethod
    def _ts_shape(cls, v: str) -> str:
        if not is_iso8601_utc(v):
            raise ValueError("created_at_or_observed_at must be timezone-aware ISO-8601")
        return v

    def identity_hash(self) -> str:
        """Deterministic identity over the canonical content of this artifact."""
        payload: dict[str, Any] = self.model_dump()
        return canonical_hash(payload)
