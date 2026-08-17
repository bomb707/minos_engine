"""Exact per-base coverage via difference arrays (Layer 1 spec §10).

Two views are accumulated over the region as bounded ``int32`` difference arrays:
``duplicate_including`` and ``fragment_primary`` (duplicate-excluding, fragment-
aware — the caller supplies overlap-clipped intervals so overlapping mates are not
double-counted). Deletions do not add base depth (blocks come from
``read.get_blocks()``, which splits at D/N); deletion coverage is recorded
separately by the evidence profiler. Prefix-summing yields exact per-base depth,
from which fixed bins and deterministic quantiles are computed.
"""

from __future__ import annotations

import numpy as np

from .contracts import CoverageView

__all__ = ["CoverageAccumulator", "coverage_view", "DUP_INCLUDING", "FRAGMENT_PRIMARY"]

DUP_INCLUDING = 0
FRAGMENT_PRIMARY = 1


class CoverageAccumulator:
    """Bounded difference-array accumulator for the two coverage views."""

    def __init__(self, region_start0: int, length_bp: int) -> None:
        self._start0 = region_start0
        self._length = length_bp
        # +1 slack for the terminal -1 event of a block ending at the last base.
        self._diff = [
            np.zeros(length_bp + 1, dtype=np.int64),
            np.zeros(length_bp + 1, dtype=np.int64),
        ]

    def add(self, view: int, abs_start0: int, abs_end0: int) -> None:
        """Add +1 over reference-relative ``[abs_start0, abs_end0)`` for a view."""
        s = max(abs_start0, self._start0) - self._start0
        e = min(abs_end0, self._start0 + self._length) - self._start0
        if e <= s:
            return
        self._diff[view][s] += 1
        self._diff[view][e] -= 1

    def depth(self, view: int) -> np.ndarray:
        """Prefix-sum to per-base depth over the region (length_bp entries)."""
        return np.cumsum(self._diff[view][:-1])

    def window_depth(self, view: int, win_start0: int, win_end0: int) -> np.ndarray:
        full = self.depth(view)
        s = win_start0 - self._start0
        e = win_end0 - self._start0
        return full[s:e]


def coverage_view(
    depth: np.ndarray,
    *,
    view_name: str,
    depth_semantics: str,
    quantile_ps: tuple[float, ...],
    min_callable_depth: int = 10,
) -> CoverageView:
    """Summarize a per-base depth array into a :class:`CoverageView` (deterministic)."""
    n = int(depth.size)
    if n == 0:
        zero_q = {f"P{int(round(p * 100)):02d}": 0.0 for p in quantile_ps}
        return CoverageView(
            view_name=view_name,
            depth_semantics=depth_semantics,
            mean_depth_reads_per_base=0.0,
            median_depth_reads_per_base=0.0,
            stddev_depth=0.0,
            coefficient_of_variation=0.0,
            depth_mad=0.0,
            max_depth=0,
            zero_depth_fraction=0.0,
            depth_lt5_fraction=0.0,
            depth_lt10_fraction=0.0,
            depth_lt20_fraction=0.0,
            depth_gt50_fraction=0.0,
            depth_gt100_fraction=0.0,
            depth_gt200_fraction=0.0,
            callable_base_fraction=0.0,
            depth_quantiles=zero_q,
        )
    d = depth.astype(np.float64)
    mean = float(d.mean())
    std = float(d.std())
    median = float(np.median(d))
    mad = float(np.median(np.abs(d - median)))
    cv = float(std / mean) if mean > 0 else 0.0
    quantiles = {
        f"P{int(round(p * 100)):02d}": float(np.quantile(d, p, method="linear"))
        for p in quantile_ps
    }

    def frac(mask: np.ndarray) -> float:
        return float(mask.sum()) / n

    return CoverageView(
        view_name=view_name,
        depth_semantics=depth_semantics,
        mean_depth_reads_per_base=mean,
        median_depth_reads_per_base=median,
        stddev_depth=std,
        coefficient_of_variation=cv,
        depth_mad=mad,
        max_depth=int(depth.max()),
        zero_depth_fraction=frac(depth == 0),
        depth_lt5_fraction=frac(depth < 5),
        depth_lt10_fraction=frac(depth < 10),
        depth_lt20_fraction=frac(depth < 20),
        depth_gt50_fraction=frac(depth > 50),
        depth_gt100_fraction=frac(depth > 100),
        depth_gt200_fraction=frac(depth > 200),
        callable_base_fraction=frac(depth >= min_callable_depth),
        depth_quantiles=quantiles,
    )
