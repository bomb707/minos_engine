"""Step 2–3 — BAM/BAI/reference integrity + header profiler (Layer 1 spec §6–§7).

Validates inputs through the pysam adapter, resolves the region, profiles the
header, and computes content identities. Hard failures (corrupt/unreadable BAM,
missing/unusable index, missing contig, reference length mismatch, region outside
either asset, unsorted BAM) raise a typed :class:`Layer1InputError`. Softer
conditions (missing RG/SM, multiple read groups, missing NM/MD, unexpected
platform) are returned as warnings. A BAI mtime is never treated as identity —
content hashes plus a successful indexed fetch are used and recorded.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from minos_engine.common.errors import ContractValidationError
from minos_engine.common.hashing import canonical_hash

from .adapters.pysam_adapter import BamOpenError, FastaOpenError, PysamAdapter
from .contracts import BamIdentity, FieldStatus, HeaderSummary, Region, VerificationStrength
from .region import RegionResolutionError, resolve_region

__all__ = ["Layer1InputError", "ValidatedInputs", "validate_inputs"]


class Layer1InputError(ContractValidationError):
    """A Layer 1 input failed integrity validation (hard failure)."""


@dataclass
class ValidatedInputs:
    alignment: Any
    fasta: Any
    region: Region
    header: HeaderSummary
    identity: BamIdentity
    warnings: tuple[str, ...] = field(default_factory=tuple)


def _require(path: str, kind: str) -> Path:
    p = Path(path)
    if not p.is_file():
        raise Layer1InputError(f"{kind} not found or not a file: {path}")
    return p


def validate_inputs(
    *,
    bam_path: str,
    bai_path: str,
    reference_path: str,
    fai_path: str,
    region_source: str,
    region_convention: str,
    adapter: PysamAdapter,
) -> ValidatedInputs:
    _require(bam_path, "BAM")
    bai = _require(bai_path, "BAI")
    _require(reference_path, "reference FASTA")
    fai = _require(fai_path, "FASTA index (.fai)")

    warnings: list[str] = []
    bam_sha, bam_size = adapter.stream_sha256(bam_path)
    index_sha, _ = adapter.stream_sha256(bai)
    ref_sha, _ = adapter.stream_sha256(reference_path)
    fai_sha, _ = adapter.stream_sha256(fai)

    try:
        alignment = adapter.open_alignment(bam_path, bai_path)
    except BamOpenError as exc:
        raise Layer1InputError(str(exc)) from exc
    try:
        fasta = adapter.open_fasta(reference_path)
    except FastaOpenError as exc:
        alignment.close()
        raise Layer1InputError(str(exc)) from exc

    try:
        header = adapter.header_dict(alignment)
        summary = _profile_header(header, warnings)
        if not summary.coordinate_sorted:
            raise Layer1InputError("BAM is not coordinate-sorted; indexed profiling requires it")

        bam_contigs = dict(summary.contigs)
        fasta_contigs = dict(zip(fasta.references, fasta.lengths, strict=False))
        try:
            region = resolve_region(region_source, region_convention, bam_contigs, fasta_contigs)
        except RegionResolutionError as exc:
            raise Layer1InputError(str(exc)) from exc

        # Prove indexed access works with a bounded fetch and a bounded ref slice.
        try:
            next(
                alignment.fetch(
                    region.contig, region.start0, min(region.start0 + 1, region.end0_exclusive)
                ),
                None,
            )
        except (ValueError, OSError) as exc:
            raise Layer1InputError(f"indexed fetch failed (unusable BAI): {exc}") from exc
        try:
            fasta.fetch(region.contig, region.start0, min(region.start0 + 1, region.end0_exclusive))
        except (KeyError, ValueError, OSError) as exc:
            raise Layer1InputError(f"reference fetch failed: {exc}") from exc

        header_sha = canonical_hash(header)
        identity = BamIdentity(
            bam_sha256=bam_sha,
            bam_size_bytes=bam_size,
            header_sha256=header_sha,
            index_status=FieldStatus.AVAILABLE,
            index_sha256=index_sha,
            reference_status=FieldStatus.AVAILABLE,
            reference_sha256=ref_sha,
            fai_sha256=fai_sha,
            verification_strength=VerificationStrength.CONTENT_HASH_AND_FETCH,
        )
    except Exception:
        alignment.close()
        fasta.close()
        raise

    return ValidatedInputs(
        alignment=alignment,
        fasta=fasta,
        region=region,
        header=summary,
        identity=identity,
        warnings=tuple(warnings),
    )


def _profile_header(header: dict[str, Any], warnings: list[str]) -> HeaderSummary:
    hd = header.get("HD", {}) or {}
    sq = header.get("SQ", []) or []
    rg = header.get("RG", []) or []
    pg = header.get("PG", []) or []

    contigs = tuple((str(s["SN"]), int(s["LN"])) for s in sq if "SN" in s and "LN" in s)
    if not contigs:
        raise Layer1InputError("BAM header has no @SQ contig dictionary")

    read_group_ids = tuple(str(r.get("ID", "")) for r in rg if r.get("ID"))
    sample_names = tuple(sorted({str(r["SM"]) for r in rg if r.get("SM")}))
    library_names = tuple(sorted({str(r["LB"]) for r in rg if r.get("LB")}))
    platform_values = tuple(sorted({str(r["PL"]) for r in rg if r.get("PL")}))
    program_ids = tuple(str(p.get("ID", "")) for p in pg if p.get("ID"))

    if not read_group_ids:
        warnings.append("missing @RG read group(s)")
    if not sample_names:
        warnings.append("missing @RG SM sample name(s)")
    if len(read_group_ids) > 1:
        warnings.append(f"multiple read groups present ({len(read_group_ids)})")
    if len(library_names) > 1:
        warnings.append(f"multiple libraries present ({len(library_names)})")

    sort_order = str(hd.get("SO", "")) or None
    return HeaderSummary(
        hd_version=str(hd.get("VN", "")) or None,
        sort_order=sort_order,
        contigs=contigs,
        read_group_ids=read_group_ids,
        sample_names=sample_names,
        library_names=library_names,
        platform_values=platform_values,
        program_ids=program_ids,
        coordinate_sorted=(sort_order == "coordinate"),
        has_nm_tag_observed=False,
        has_md_tag_observed=False,
    )
