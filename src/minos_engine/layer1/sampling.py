"""Deterministic adaptive window sampling (Layer 1 spec §15).

Each primary window is assigned to every applicable stratum from its scan
features (no pileup needed). Deterministic per-stratum quotas are reserved; the
selected set is deduplicated while retaining all selection reasons; ties are
broken by ``H(BAM hash, region, profiler version, config hash, window id)`` — never
by process RNG, thread order, or wall-clock. Inclusion probability ``πᵢ`` and
analysis weight ``1/πᵢ`` are stored so weighted regional estimates use
``Σ wᵢ·xᵢ / Σ wᵢ``.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from minos_engine.common.hashing import canonical_hash

__all__ = ["WindowFeature", "SamplingPlan", "select_windows"]

# Stratum priority (first applicable becomes the row's primary stratum label).
STRATA_PRIORITY = (
    "boundary",
    "high_coverage",
    "low_coverage",
    "low_mapping_quality",
    "low_base_quality",
    "clipping",
    "nm_burden",
    "indel_burden",
    "low_entropy",
    "homopolymer",
    "uniform",
)


@dataclass(frozen=True)
class WindowFeature:
    window_id: int
    depth_mean: float
    mq_mean: float
    bq_mean: float
    softclip_read_fraction: float
    nm_per_base: float
    indel_burden: float
    entropy_bits: float
    homopolymer_fraction: float
    is_boundary: bool


@dataclass
class SamplingPlan:
    selected: tuple[int, ...]
    primary_stratum: dict[int, str]
    difficult_flags: dict[int, tuple[str, ...]]
    stratum_counts: dict[str, int]
    selection_probability: dict[int, float]
    analysis_weight: dict[int, float]
    plan_hash: str = field(default="")


def _applicable_strata(f: WindowFeature, thr: dict[str, float]) -> list[str]:
    strata = ["uniform"]
    if f.is_boundary:
        strata.append("boundary")
    if f.depth_mean < thr["low_coverage_depth"]:
        strata.append("low_coverage")
    if f.depth_mean > thr["high_coverage_depth"]:
        strata.append("high_coverage")
    if f.mq_mean < thr.get("low_mapping_quality_mean", 40.0):
        strata.append("low_mapping_quality")
    if f.bq_mean < thr.get("low_base_quality_mean", 30.0):
        strata.append("low_base_quality")
    if f.softclip_read_fraction > thr["clipping_read_fraction"]:
        strata.append("clipping")
    if f.nm_per_base > thr["nm_per_base"]:
        strata.append("nm_burden")
    if f.indel_burden > thr["indel_burden"]:
        strata.append("indel_burden")
    if f.entropy_bits < thr["low_entropy_bits"]:
        strata.append("low_entropy")
    if f.homopolymer_fraction > thr["homopolymer_burden"]:
        strata.append("homopolymer")
    return strata


def _tiebreak(seed: str, window_id: int) -> int:
    return int(canonical_hash({"seed": seed, "window_id": window_id}), 16)


def select_windows(
    features: list[WindowFeature],
    thresholds: dict[str, float],
    *,
    per_stratum_quota: int,
    total_quota: int,
    tiebreak_seed: str,
) -> SamplingPlan:
    """Deterministically select windows for refinement/pileup."""
    membership: dict[str, list[int]] = {}
    primary: dict[int, str] = {}
    flags: dict[int, tuple[str, ...]] = {}
    for f in features:
        strata = _applicable_strata(f, thresholds)
        for s in strata:
            membership.setdefault(s, []).append(f.window_id)
        ordered = [s for s in STRATA_PRIORITY if s in strata]
        primary[f.window_id] = ordered[0] if ordered else "uniform"
        flags[f.window_id] = tuple(s for s in ordered if s != "uniform")

    selected: list[int] = []
    seen: set[int] = set()
    # Reserve per-stratum quotas in priority order; ties broken by the hash.
    for stratum in STRATA_PRIORITY:
        members = membership.get(stratum, [])
        members_sorted = sorted(members, key=lambda wid: _tiebreak(tiebreak_seed, wid))
        taken = 0
        for wid in members_sorted:
            if taken >= per_stratum_quota or len(selected) >= total_quota:
                break
            if wid not in seen:
                seen.add(wid)
                selected.append(wid)
                taken += 1

    selected_sorted = tuple(sorted(selected))
    stratum_counts = {s: len(m) for s, m in sorted(membership.items())}

    pi: dict[int, float] = {}
    weight: dict[int, float] = {}
    n = len(features) or 1
    for f in features:
        stratum = primary[f.window_id]
        stratum_size = len(membership.get(stratum, [])) or 1
        designed = min(1.0, per_stratum_quota / stratum_size)
        pi[f.window_id] = designed
        if f.window_id in seen:
            weight[f.window_id] = 1.0 / designed if designed > 0 else float(n)
        else:
            weight[f.window_id] = 0.0

    plan = SamplingPlan(
        selected=selected_sorted,
        primary_stratum=primary,
        difficult_flags=flags,
        stratum_counts=stratum_counts,
        selection_probability=pi,
        analysis_weight=weight,
    )
    plan.plan_hash = canonical_hash(
        {
            "selected": list(selected_sorted),
            "stratum_counts": stratum_counts,
            "per_stratum_quota": per_stratum_quota,
            "total_quota": total_quota,
            "seed": tiebreak_seed,
            "algorithm": "layer1-adaptive-sampling-v1",
        }
    )
    return plan
