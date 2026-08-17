"""pysam-backed BAM/FASTA adapter — the only place Layer 1 touches sequence I/O.

Opens exactly the paths it is given (never globs a directory), streams content
hashes in bounded memory, and exposes the narrow surface the profilers need:
header, index statistics, regional fetch, pileup, and reference slices. Injected
into ``Layer1Service`` so tests can substitute a stub, though the test suite
mostly drives it with real tiny pysam-built fixtures.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from minos_engine.common.errors import MinosEngineError

__all__ = ["BamOpenError", "FastaOpenError", "PysamAdapter", "pysam_version"]

_CHUNK = 1 << 20


class BamOpenError(MinosEngineError):
    """The BAM/BAI could not be opened or is unusable for indexed access."""


class FastaOpenError(MinosEngineError):
    """The reference FASTA/FAI could not be opened."""


def pysam_version() -> str:
    import pysam

    return str(getattr(pysam, "__version__", "unknown"))


class PysamAdapter:
    """Thin, injectable wrapper over pysam. Holds no per-request state."""

    def stream_sha256(self, path: str | Path) -> tuple[str, int]:
        """Return ``(sha256_hex, size_bytes)`` streamed in bounded memory."""
        p = Path(path)
        h = hashlib.sha256()
        size = 0
        with p.open("rb") as fh:
            while True:
                block = fh.read(_CHUNK)
                if not block:
                    break
                size += len(block)
                h.update(block)
        return h.hexdigest(), size

    def open_alignment(self, bam_path: str, bai_path: str | None) -> Any:
        import pysam

        try:
            if bai_path:
                af = pysam.AlignmentFile(bam_path, "rb", index_filename=bai_path)
            else:
                af = pysam.AlignmentFile(bam_path, "rb")
        except (OSError, ValueError) as exc:
            raise BamOpenError(f"cannot open BAM {bam_path!r}: {exc}") from exc
        return af

    def open_fasta(self, reference_path: str) -> Any:
        import pysam

        try:
            return pysam.FastaFile(reference_path)
        except (OSError, ValueError) as exc:
            raise FastaOpenError(f"cannot open reference {reference_path!r}: {exc}") from exc

    def header_dict(self, alignment: Any) -> dict[str, Any]:
        return dict(alignment.header.to_dict())

    def index_statistics(self, alignment: Any) -> list[tuple[str, int]]:
        try:
            return [(s.contig, int(s.mapped)) for s in alignment.get_index_statistics()]
        except (ValueError, AttributeError):
            return []
