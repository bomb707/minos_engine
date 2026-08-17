"""Mutation-manifest vs truth-VCF normalization and reconciliation (offline).

Both are offline validation inputs; neither is passed to Layer 1. Records are
normalized (contig naming, multiallelic decomposition, minimal representation) and
reconciled by (contig, pos, ref, alt). Reports matched / unmatched / complex counts
so the mutation file is used substantively, not merely hashed.
"""

from __future__ import annotations

from dataclasses import dataclass

import pysam


@dataclass
class Reconciliation:
    mutation_records: int
    truth_records: int
    matched: int
    unmatched_mutation: int
    unmatched_truth: int
    complex_excluded_mutation: int
    complex_excluded_truth: int
    normalization_notes: list[str]


def _norm_contig(c: str) -> str:
    return c if c.startswith("chr") else f"chr{c}"


def _minimal(ref: str, alt: str) -> tuple[str, str]:
    # trim shared suffix then shared prefix (keep >=1 base) — minimal representation
    while len(ref) > 1 and len(alt) > 1 and ref[-1] == alt[-1]:
        ref, alt = ref[:-1], alt[:-1]
    off = 0
    while len(ref) - off > 1 and len(alt) - off > 1 and ref[off] == alt[off]:
        off += 1
    return ref[off:], alt[off:]


def _keys(path: str, contig: str, s0: int, e0: int) -> tuple[set[tuple], int, int]:
    vf = pysam.VariantFile(path)
    keyset: set[tuple] = set()
    total = 0
    complex_n = 0
    try:
        it = vf.fetch(contig, s0, e0)
    except (ValueError, OSError):
        it = (r for r in vf if _norm_contig(r.contig) == contig and s0 <= r.pos - 1 < e0)
    for rec in it:
        p0 = rec.pos - 1
        if p0 < s0 or p0 >= e0:
            continue
        alts = rec.alts or ()
        if len(alts) > 1:
            complex_n += 1  # multiallelic -> decompose each below, but flag it
        for alt in alts:
            total += 1
            if alt is None or "<" in alt or "[" in alt or "]" in alt:
                complex_n += 1
                continue
            r2, a2 = _minimal(rec.ref, alt)
            keyset.add((contig, p0, r2, a2))
    vf.close()
    return keyset, total, complex_n


def reconcile(mutations_vcf: str, truth_vcf: str, contig: str, s0: int, e0: int) -> Reconciliation:
    notes: list[str] = [
        "contig normalized to chrNN",
        "multiallelics decomposed",
        "minimal representation (prefix/suffix trim)",
    ]
    mkeys, mtot, mcx = _keys(mutations_vcf, contig, s0, e0)
    tkeys, ttot, tcx = _keys(truth_vcf, contig, s0, e0)
    matched = len(mkeys & tkeys)
    return Reconciliation(
        mutation_records=mtot,
        truth_records=ttot,
        matched=matched,
        unmatched_mutation=len(mkeys - tkeys),
        unmatched_truth=len(tkeys - mkeys),
        complex_excluded_mutation=mcx,
        complex_excluded_truth=tcx,
        normalization_notes=notes,
    )
