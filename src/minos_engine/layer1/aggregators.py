"""Bounded-memory, deterministic online aggregators (Layer 1 spec §8).

Never retains all reads/bases. Uses integer counters, Welford mean/variance, and
fixed integer histograms with deterministic quantiles and MAD. Histogram bins are
fixed by construction (never inferred from the current BAM), so the same input and
version produce the same bytes.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

__all__ = ["Welford", "IntHistogram", "percentile_keys", "PERCENTILES"]

PERCENTILES: tuple[int, ...] = (1, 5, 10, 25, 50, 75, 90, 95, 99)


def percentile_keys() -> tuple[str, ...]:
    return tuple(f"P{p:02d}" for p in PERCENTILES)


@dataclass
class Welford:
    """Numerically stable online mean/variance with min/max (Welford's algorithm)."""

    n: int = 0
    _mean: float = 0.0
    _m2: float = 0.0
    minimum: float = math.inf
    maximum: float = -math.inf

    def observe(self, x: float) -> None:
        self.n += 1
        delta = x - self._mean
        self._mean += delta / self.n
        self._m2 += delta * (x - self._mean)
        if x < self.minimum:
            self.minimum = x
        if x > self.maximum:
            self.maximum = x

    @property
    def mean(self) -> float:
        return self._mean if self.n else 0.0

    @property
    def variance(self) -> float:
        return self._m2 / self.n if self.n else 0.0

    @property
    def stddev(self) -> float:
        return math.sqrt(self.variance)

    @property
    def min_or_zero(self) -> float:
        return self.minimum if self.n else 0.0

    @property
    def max_or_zero(self) -> float:
        return self.maximum if self.n else 0.0


@dataclass
class IntHistogram:
    """Fixed-range integer histogram with deterministic quantiles and MAD.

    Values outside ``[lo, hi]`` are counted in over/underflow bins (never dropped)
    and clamped for quantile interpolation. All bins are fixed at construction.
    """

    lo: int
    hi: int
    counts: list[int] = field(default_factory=list)
    below: int = 0
    above: int = 0
    total: int = 0

    def __post_init__(self) -> None:
        if self.hi < self.lo:
            raise ValueError("IntHistogram requires hi >= lo")
        if not self.counts:
            self.counts = [0] * (self.hi - self.lo + 1)

    def observe(self, value: int) -> None:
        self.total += 1
        if value < self.lo:
            self.below += 1
        elif value > self.hi:
            self.above += 1
        else:
            self.counts[value - self.lo] += 1

    def observe_many(self, values: list[int]) -> None:
        for v in values:
            self.observe(v)

    def count_at_or_below(self, threshold: int) -> int:
        """Number of observations with value <= threshold (inclusive)."""
        if threshold < self.lo:
            return self.below
        upper = min(threshold, self.hi)
        inrange = sum(self.counts[: upper - self.lo + 1])
        return self.below + inrange

    def count_above(self, threshold: int) -> int:
        return self.total - self.count_at_or_below(threshold)

    def quantile(self, p: float) -> float:
        """Deterministic p-quantile (0..1) via cumulative counts, clamped range."""
        if self.total == 0:
            return 0.0
        rank = p * (self.total - 1)
        target = math.floor(rank) + 1  # 1-based rank of the lower neighbor
        cumulative = self.below
        # underflow region collapses to lo
        if cumulative >= target:
            return float(self.lo)
        for i, c in enumerate(self.counts):
            cumulative += c
            if cumulative >= target:
                return float(self.lo + i)
        return float(self.hi)

    def quantile_map(self) -> dict[str, float]:
        return {f"P{p:02d}": self.quantile(p / 100.0) for p in PERCENTILES}

    def median(self) -> float:
        return self.quantile(0.5)

    def mad(self) -> float:
        """Median absolute deviation about the histogram median (deterministic)."""
        if self.total == 0:
            return 0.0
        med = self.median()
        span = self.hi - self.lo
        # med is within [lo, hi], so every absolute deviation is <= span.
        dev = IntHistogram(lo=0, hi=span)
        # deviations of in-range values
        for i, c in enumerate(self.counts):
            if c:
                dev.observe_many([int(abs((self.lo + i) - med))] * c)
        # overflow contributions clamped to the nearest edge deviation
        if self.below:
            dev.observe_many([int(abs(self.lo - med))] * self.below)
        if self.above:
            dev.observe_many([int(abs(self.hi - med))] * self.above)
        return dev.median()
