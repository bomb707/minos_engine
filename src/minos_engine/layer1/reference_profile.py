"""Reference-context profiling (Layer 1 spec §11–§12).

Describes measurable sequence properties of the target interval, independently of
the BAM alignments: GC fraction, N/ambiguous fraction, Shannon entropy,
homopolymer density + length histogram, and a dinucleotide-repeat indicator.
FASTA-derived indicators are **not** official repeat-mask/mappability annotations
and are never labelled as such. When no reference sequence is available the
result is marked unavailable; nothing is inferred from the reads.
"""

from __future__ import annotations

import math

from .contracts import ReferenceContextMetrics

__all__ = ["profile_reference_sequence", "unavailable_reference_context"]

_ACGT = frozenset("ACGT")


def profile_reference_sequence(
    seq: str,
    *,
    homopolymer_min_run: int = 4,
    dinucleotide_min_run: int = 3,
) -> ReferenceContextMetrics:
    """Profile an already-fetched, uppercased reference interval sequence."""
    s = seq.upper()
    length = len(s)
    if length == 0:
        return unavailable_reference_context(reference_available=True)

    counts = dict.fromkeys("ACGT", 0)
    n_count = 0
    for ch in s:
        if ch in counts:
            counts[ch] += 1
        else:
            n_count += 1

    acgt_total = counts["A"] + counts["C"] + counts["G"] + counts["T"]
    gc = (counts["G"] + counts["C"]) / acgt_total if acgt_total else 0.0
    n_fraction = n_count / length
    entropy = _entropy(counts, acgt_total)

    homo_bases, homo_hist = _homopolymers(s, homopolymer_min_run)
    dinuc_bases = _dinucleotide_repeat_bases(s, dinucleotide_min_run)

    return ReferenceContextMetrics(
        gc_fraction=gc,
        n_fraction=n_fraction,
        entropy_bits=entropy,
        homopolymer_base_fraction=homo_bases / length,
        homopolymer_length_histogram={str(k): v for k, v in sorted(homo_hist.items())},
        dinucleotide_repeat_fraction=dinuc_bases / length,
        ambiguous_reference_excluded=True,
        reference_available=True,
    )


def unavailable_reference_context(*, reference_available: bool) -> ReferenceContextMetrics:
    return ReferenceContextMetrics(
        gc_fraction=0.0,
        n_fraction=0.0,
        entropy_bits=0.0,
        homopolymer_base_fraction=0.0,
        homopolymer_length_histogram={},
        dinucleotide_repeat_fraction=0.0,
        ambiguous_reference_excluded=True,
        reference_available=reference_available,
    )


def _entropy(counts: dict[str, int], total: int) -> float:
    if total == 0:
        return 0.0
    h = 0.0
    for b in "ACGT":
        p = counts[b] / total
        if p > 0:
            h -= p * math.log2(p)
    return h


def _homopolymers(s: str, min_run: int) -> tuple[int, dict[int, int]]:
    """Return (bases in homopolymer runs >= min_run, run-length histogram)."""
    hist: dict[int, int] = {}
    homo_bases = 0
    i = 0
    n = len(s)
    while i < n:
        ch = s[i]
        j = i + 1
        while j < n and s[j] == ch:
            j += 1
        run = j - i
        if ch in _ACGT and run >= min_run:
            homo_bases += run
            hist[run] = hist.get(run, 0) + 1
        i = j
    return homo_bases, hist


def _dinucleotide_repeat_bases(s: str, min_units: int) -> int:
    """Bases covered by maximal repeated 2-mer runs of at least ``min_units`` units."""
    n = len(s)
    if n < 2 * min_units:
        return 0
    covered = 0
    i = 0
    while i + 1 < n:
        a, b = s[i], s[i + 1]
        if a not in _ACGT or b not in _ACGT or a == b:
            i += 1
            continue
        units = 1
        j = i + 2
        while j + 1 < n and s[j] == a and s[j + 1] == b:
            units += 1
            j += 2
        if units >= min_units:
            covered += units * 2
            i = j
        else:
            i += 1
    return covered
