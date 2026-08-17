"""Reference-genome resolution registry.

Maps a contig to its expected reference identity (FASTA + FAI content hashes).
Stage 0 provides the in-memory registry and lookup contract; it does not download
or open reference files. The registry is populated from a versioned reference
manifest (later stages) or from the protocol snapshot's ``reference_sha256``.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, field_validator

from minos_engine.common.errors import UnavailableError

from .contracts import _SHA256_RE  # reuse the shared sha256 shape check

__all__ = ["ReferenceEntry", "ReferenceRegistry"]


class ReferenceEntry(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    contig: str = Field(min_length=1)
    build: str = Field(default="GRCh38")
    fasta_sha256: str
    fai_sha256: str | None = None

    @field_validator("fasta_sha256")
    @classmethod
    def _sha(cls, v: str) -> str:
        if not _SHA256_RE.match(v):
            raise ValueError("fasta_sha256 must be 64 lowercase hex characters")
        return v


class ReferenceRegistry:
    """A small, explicit registry of resolved reference identities."""

    def __init__(self, entries: dict[str, ReferenceEntry] | None = None) -> None:
        self._entries: dict[str, ReferenceEntry] = dict(entries or {})

    def register(self, entry: ReferenceEntry) -> None:
        self._entries[entry.contig] = entry

    def resolve(self, contig: str) -> ReferenceEntry:
        """Return the reference identity for a contig, or fail closed if unknown."""
        try:
            return self._entries[contig]
        except KeyError as exc:
            raise UnavailableError(
                f"no resolved reference registered for contig {contig!r}"
            ) from exc

    def contigs(self) -> tuple[str, ...]:
        return tuple(sorted(self._entries))
