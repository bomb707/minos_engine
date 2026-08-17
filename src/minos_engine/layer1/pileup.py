"""Bounded pileup and truth-free per-position evidence (Layer 1 spec §12–§13).

Runs the pinned pysam pileup over selected windows and aggregates *evidence
proxies* — reference-vs-alternate support, candidate SNP/indel densities, strand
balance, and support/allele-fraction threshold curves. These are descriptive
signals, never variant calls, TP/FP/FN, scores, or GATK parameters. The reference
allele comes from the FASTA; ambiguous reference positions are excluded from the
SNP-like density denominator. Columns that reach ``max_depth`` are recorded and
reduce completeness/confidence — no full-evidence claim when capped.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .contracts import VariantEvidenceMetrics

__all__ = ["WindowEvidence", "profile_pileup_window", "aggregate_evidence"]

_ACGT = frozenset("ACGT")


@dataclass
class WindowEvidence:
    win_start0: int
    win_end0: int
    columns: int = 0
    callable_columns: int = 0
    total_base_obs: int = 0
    alt_base_obs: int = 0
    snp_sites: int = 0
    insertion_sites: int = 0
    deletion_sites: int = 0
    forward_alt: int = 0
    reverse_alt: int = 0
    low_quality_alt: int = 0
    columns_capped: int = 0
    support_curve: dict[int, int] = field(default_factory=dict)
    af_curve: dict[float, int] = field(default_factory=dict)

    @property
    def length_bp(self) -> int:
        return self.win_end0 - self.win_start0

    def snp_density(self) -> float:
        return self.snp_sites / self.length_bp if self.length_bp else 0.0

    def indel_density(self) -> float:
        return (
            (self.insertion_sites + self.deletion_sites) / self.length_bp if self.length_bp else 0.0
        )


def profile_pileup_window(
    alignment: Any,
    fasta: Any,
    contig: str,
    win_start0: int,
    win_end0: int,
    *,
    max_depth: int,
    stepper: str,
    support_thresholds: tuple[int, ...],
    af_thresholds: tuple[float, ...],
    low_alt_bq: int = 20,
) -> WindowEvidence:
    """Pileup one window and return its truth-free evidence proxies."""
    ev = WindowEvidence(win_start0=win_start0, win_end0=win_end0)
    ev.support_curve = dict.fromkeys(support_thresholds, 0)
    ev.af_curve = dict.fromkeys(af_thresholds, 0)
    ref_seq = fasta.fetch(contig, win_start0, win_end0).upper()

    for col in alignment.pileup(
        contig,
        win_start0,
        win_end0,
        truncate=True,
        min_base_quality=0,
        min_mapping_quality=0,
        ignore_overlaps=True,
        compute_baq=False,
        max_depth=max_depth,
        stepper=stepper,
    ):
        pos = col.reference_pos
        if pos < win_start0 or pos >= win_end0:
            continue
        ev.columns += 1
        ref_base = ref_seq[pos - win_start0] if 0 <= pos - win_start0 < len(ref_seq) else "N"
        depth = 0
        alt = 0
        fwd_alt = 0
        rev_alt = 0
        low_alt = 0
        ins = 0
        dele = 0
        alt_by_base: dict[str, int] = {}
        for pr in col.pileups:
            if pr.indel > 0:
                ins += 1
            elif pr.indel < 0:
                dele += 1
            if pr.is_del or pr.is_refskip or pr.query_position is None:
                continue
            aln = pr.alignment
            base = aln.query_sequence[pr.query_position].upper()
            depth += 1
            if base != ref_base and base in _ACGT and ref_base in _ACGT:
                alt += 1
                alt_by_base[base] = alt_by_base.get(base, 0) + 1
                if aln.is_reverse:
                    rev_alt += 1
                else:
                    fwd_alt += 1
                quals = aln.query_qualities
                if quals is not None and quals[pr.query_position] < low_alt_bq:
                    low_alt += 1

        if depth >= max_depth:
            ev.columns_capped += 1
        ev.total_base_obs += depth
        ev.alt_base_obs += alt
        ev.forward_alt += fwd_alt
        ev.reverse_alt += rev_alt
        ev.low_quality_alt += low_alt

        if ref_base in _ACGT:
            ev.callable_columns += 1
            top_alt = max(alt_by_base.values()) if alt_by_base else 0
            af = top_alt / depth if depth else 0.0
            for t in support_thresholds:
                if top_alt >= t:
                    ev.support_curve[t] += 1
            for aft in af_thresholds:
                if af >= aft:
                    ev.af_curve[aft] += 1
            if top_alt >= support_thresholds[0] and af >= af_thresholds[0]:
                ev.snp_sites += 1
        if ins >= support_thresholds[0]:
            ev.insertion_sites += 1
        if dele >= support_thresholds[0]:
            ev.deletion_sites += 1
    return ev


def aggregate_evidence(
    windows: list[WindowEvidence],
    *,
    eligible_region_bases: int,
    analyzed_bases: int,
) -> VariantEvidenceMetrics:
    """Aggregate per-window evidence into the global variant-evidence section."""
    total_base = sum(w.total_base_obs for w in windows)
    alt_base = sum(w.alt_base_obs for w in windows)
    snp = sum(w.snp_sites for w in windows)
    ins = sum(w.insertion_sites for w in windows)
    dele = sum(w.deletion_sites for w in windows)
    fwd = sum(w.forward_alt for w in windows)
    rev = sum(w.reverse_alt for w in windows)
    low = sum(w.low_quality_alt for w in windows)
    capped = sum(w.columns_capped for w in windows)
    columns = sum(w.columns for w in windows) or 1
    alt_denom = fwd + rev or 1
    analyzed = analyzed_bases or 1

    support_keys: dict[str, int] = {}
    af_keys: dict[str, int] = {}
    for w in windows:
        for t, c in w.support_curve.items():
            support_keys[f"support_ge_{t}"] = support_keys.get(f"support_ge_{t}", 0) + c
        for aft, c in w.af_curve.items():
            key = f"af_ge_{int(round(aft * 100)):02d}"
            af_keys[key] = af_keys.get(key, 0) + c

    return VariantEvidenceMetrics(
        analyzed_callable_bases=sum(w.callable_columns for w in windows),
        eligible_region_bases=eligible_region_bases,
        mismatch_fraction=alt_base / (total_base or 1),
        candidate_snp_density_per_base=snp / analyzed,
        candidate_insertion_density_per_base=ins / analyzed,
        candidate_deletion_density_per_base=dele / analyzed,
        support_threshold_site_counts=dict(sorted(support_keys.items())),
        allele_fraction_threshold_site_counts=dict(sorted(af_keys.items())),
        forward_alt_fraction=fwd / alt_denom,
        reverse_alt_fraction=rev / alt_denom,
        low_quality_alt_fraction=low / (alt_base or 1),
        columns_reaching_max_depth=capped,
        max_depth_capped_fraction=capped / columns,
    )
