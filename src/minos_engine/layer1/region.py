"""Step 1 — exact region resolution (Layer 1 spec §5).

Produce one unambiguous zero-based half-open interval from the API region string
and its declared coordinate convention. Convert exactly once; preserve the source
string and convention; require ``0 <= start0 < end0 <= BAM length`` and
``<= FASTA length`` with equal contig lengths. Any ambiguity or mismatch is a hard
failure — never silently add/remove a base, and never emit a partial profile.
"""

from __future__ import annotations

from minos_engine.common.errors import ContractValidationError

from .contracts import Region

__all__ = ["RegionResolutionError", "resolve_region", "ONE_BASED", "ZERO_BASED"]

ONE_BASED = "one_based_inclusive"
ZERO_BASED = "zero_based_half_open"


class RegionResolutionError(ContractValidationError):
    """The region string/convention is ambiguous or inconsistent (hard failure)."""


def _parse(source: str) -> tuple[str, int, int]:
    text = source.strip()
    if ":" not in text:
        raise RegionResolutionError(f"unparseable region {source!r} (expected contig:start-end)")
    contig, span = text.rsplit(":", 1)
    if not contig or "-" not in span:
        raise RegionResolutionError(f"unparseable region {source!r} (expected contig:start-end)")
    start_s, _, end_s = span.partition("-")
    # digits only: reject M/k/G abbreviations, commas, signs, whitespace.
    if not (start_s.isdigit() and end_s.isdigit()):
        raise RegionResolutionError(
            f"region coordinates must be plain integers (no abbreviations): {source!r}"
        )
    return contig, int(start_s), int(end_s)


def resolve_region(
    source: str,
    convention: str,
    bam_contigs: dict[str, int],
    fasta_contigs: dict[str, int],
) -> Region:
    """Resolve ``source`` to a verified zero-based half-open :class:`Region`."""
    contig, start, end = _parse(source)

    if convention == ONE_BASED:
        if start < 1:
            raise RegionResolutionError("one-based coordinates must start at >= 1")
        start0 = start - 1
        end0 = end  # inclusive end -> exclusive end
    elif convention == ZERO_BASED:
        start0 = start
        end0 = end
    else:
        raise RegionResolutionError(f"unknown coordinate convention {convention!r}")

    if not start0 < end0:
        raise RegionResolutionError(f"empty or inverted region after conversion: [{start0},{end0})")

    if contig not in bam_contigs:
        raise RegionResolutionError(f"contig {contig!r} not present in BAM header")
    if contig not in fasta_contigs:
        raise RegionResolutionError(f"contig {contig!r} not present in reference FASTA")

    bam_len = bam_contigs[contig]
    fasta_len = fasta_contigs[contig]
    if bam_len != fasta_len:
        raise RegionResolutionError(
            f"contig {contig!r} length mismatch: BAM {bam_len} != FASTA {fasta_len}"
        )
    if end0 > bam_len:
        raise RegionResolutionError(f"region end {end0} exceeds contig length {bam_len}")

    return Region(
        source=source,
        contig=contig,
        start0=start0,
        end0_exclusive=end0,
        length_bp=end0 - start0,
        source_coordinate_system=convention,
        verified=True,
    )
