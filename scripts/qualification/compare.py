"""Comparison of observed Layer 1 values vs the independent oracle.

Tolerances are predeclared here (never tuned to results). Integer identities
require exact equality; deterministic floats use a strict absolute/relative
tolerance; declared-approximate fields use wider, family-specific tolerances.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

# Predeclared tolerances (Phase 4).
ABS_TOL = 1e-9
REL_TOL = 1e-6
APPROX_ABS = 0.01  # means/proportions
APPROX_REL = 0.02
BREADTH_ABS = 0.005

# Integer identities that must match EXACTLY.
EXACT_INT_KEYS: frozenset[str] = frozenset(
    {
        "filter.observed",
        "filter.included",
        "filter.excluded_unmapped",
        "filter.excluded_secondary",
        "filter.excluded_supplementary",
        "filter.excluded_duplicate",
        "filter.excluded_qcfail",
        "filter.excluded_below_mapq",
        "reads.total_observed_alignments",
        "reads.included_primary_alignments",
        "mq.count",
        "bq.bases_observed",
        "bq.bases_with_quality",
        "rl.count",
        "cigar.aligned_query_bases",
        "cigar.soft_clipped_bases",
        "cigar.hard_clipped_bases",
        "cigar.inserted_bases",
        "cigar.deleted_bases",
        "cigar.skipped_bases",
        "cigar.query_consuming_bases",
        "coverage.region_len",
        "coverage.max_depth",
    }
)

# Deterministic floats: strict tolerance (Layer 1 computes these exactly, not sampled).
FLOAT_STRICT_KEYS: frozenset[str] = frozenset(
    {
        "reads.duplicate_fraction",
        "reads.secondary_fraction",
        "reads.supplementary_fraction",
        "reads.qcfail_fraction",
        "reads.mapped_fraction",
        "reads.reverse_strand_fraction",
        "mq.mean",
        "bq.mean",
        "rl.mean",
        "coverage.mean_depth",
        "coverage.zero_depth_fraction",
        "ref.gc_fraction",
        "ref.n_fraction",
        "ref.entropy_bits",
        "ref.homopolymer_base_fraction",
    }
)


@dataclass
class FieldResult:
    key: str
    observed: Any
    expected: Any
    kind: str  # "exact" | "float_strict" | "approx"
    abs_error: float
    rel_error: float
    signed_error: float
    tol_abs: float
    tol_rel: float
    ok: bool


def _num(x: Any) -> float | None:
    if isinstance(x, bool):
        return None
    if isinstance(x, (int, float)):
        return float(x)
    return None


def compare_field(key: str, observed: Any, expected: Any) -> FieldResult:
    if key in EXACT_INT_KEYS:
        ok = observed == expected
        ae = abs(float(observed) - float(expected)) if _num(observed) is not None else float("nan")
        return FieldResult(key, observed, expected, "exact", ae, 0.0, ae, 0.0, 0.0, ok)
    o, e = _num(observed), _num(expected)
    if o is None or e is None:
        return FieldResult(
            key, observed, expected, "exact", 0.0, 0.0, 0.0, 0.0, 0.0, observed == expected
        )
    ae = abs(o - e)
    re = ae / abs(e) if e != 0 else (0.0 if ae == 0 else float("inf"))
    if key in FLOAT_STRICT_KEYS:
        ok = ae <= ABS_TOL or re <= REL_TOL
        return FieldResult(key, o, e, "float_strict", ae, re, o - e, ABS_TOL, REL_TOL, ok)
    ok = ae <= APPROX_ABS or re <= APPROX_REL
    return FieldResult(key, o, e, "approx", ae, re, o - e, APPROX_ABS, APPROX_REL, ok)


def observed_from_profile(profile: Any) -> dict[str, Any]:
    """Extract the oracle-comparable metric keys from a Layer 1 BamProfile."""
    fc = profile.filter_counts
    r = profile.reads
    mq = profile.mapping_quality
    bq = profile.base_quality
    rl = profile.read_length
    cg = profile.alignment
    cov = profile.coverage
    dup = cov.duplicate_including
    ref = profile.reference_context
    return {
        "filter.observed": fc.observed,
        "filter.included": fc.included,
        "filter.excluded_unmapped": fc.excluded_unmapped,
        "filter.excluded_secondary": fc.excluded_secondary,
        "filter.excluded_supplementary": fc.excluded_supplementary,
        "filter.excluded_duplicate": fc.excluded_duplicate,
        "filter.excluded_qcfail": fc.excluded_qcfail,
        "filter.excluded_below_mapq": fc.excluded_below_mapq,
        "reads.total_observed_alignments": r.total_observed_alignments,
        "reads.included_primary_alignments": r.included_primary_alignments,
        "reads.duplicate_fraction": r.duplicate_fraction,
        "reads.secondary_fraction": r.secondary_fraction,
        "reads.supplementary_fraction": r.supplementary_fraction,
        "reads.qcfail_fraction": r.qcfail_fraction,
        "reads.mapped_fraction": r.mapped_fraction,
        "reads.reverse_strand_fraction": r.reverse_strand_fraction,
        "mq.count": mq.count,
        "mq.mean": mq.mean,
        "bq.bases_observed": bq.bases_observed,
        "bq.bases_with_quality": bq.bases_with_quality,
        "bq.mean": bq.mean_base_quality_phred,
        "rl.count": rl.count,
        "rl.mean": rl.mean,
        "cigar.aligned_query_bases": cg.aligned_query_bases,
        "cigar.soft_clipped_bases": cg.soft_clipped_bases,
        "cigar.hard_clipped_bases": cg.hard_clipped_bases,
        "cigar.inserted_bases": cg.inserted_bases,
        "cigar.deleted_bases": cg.deleted_bases,
        "cigar.skipped_bases": cg.skipped_bases,
        "cigar.query_consuming_bases": cg.query_consuming_bases,
        "coverage.region_len": cov.eligible_region_bases,
        "coverage.max_depth": dup.max_depth,
        "coverage.mean_depth": dup.mean_depth_reads_per_base,
        "coverage.zero_depth_fraction": dup.zero_depth_fraction,
        "ref.gc_fraction": ref.gc_fraction,
        "ref.n_fraction": ref.n_fraction,
        "ref.entropy_bits": ref.entropy_bits,
        "ref.homopolymer_base_fraction": ref.homopolymer_base_fraction,
    }


def compare_all(observed: dict[str, Any], expected: dict[str, Any]) -> list[FieldResult]:
    keys = [k for k in expected if k in observed]
    return [compare_field(k, observed[k], expected[k]) for k in sorted(keys)]
