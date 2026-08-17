"""Group E (support) — deterministic aggregator known-answer tests."""

from __future__ import annotations

import statistics

from minos_engine.layer1.aggregators import IntHistogram, Welford


def test_welford_matches_statistics():
    xs = [1.0, 2.0, 3.0, 4.0, 10.0]
    w = Welford()
    for x in xs:
        w.observe(x)
    assert w.n == 5
    assert abs(w.mean - statistics.fmean(xs)) < 1e-12
    assert abs(w.variance - statistics.pvariance(xs)) < 1e-9
    assert w.min_or_zero == 1.0
    assert w.max_or_zero == 10.0


def test_welford_empty_is_zero():
    w = Welford()
    assert w.mean == 0.0 and w.stddev == 0.0 and w.min_or_zero == 0.0


def test_histogram_quantiles_known_vector():
    h = IntHistogram(0, 100)
    for v in range(1, 101):  # 1..100 uniform
        h.observe(v)
    # deterministic lower-neighbor quantile
    assert h.median() == 50.0
    assert h.quantile(0.0) == 1.0
    assert h.quantile(1.0) == 100.0


def test_histogram_thresholds_and_overflow():
    h = IntHistogram(0, 10)
    h.observe_many([0, 0, 5, 5, 20])  # 20 overflows into 'above'
    assert h.total == 5
    assert h.above == 1
    assert h.count_at_or_below(5) == 4
    assert h.count_above(5) == 1


def test_histogram_mad_symmetric():
    h = IntHistogram(0, 20)
    h.observe_many([10, 10, 10, 8, 12])
    assert h.median() == 10.0
    assert h.mad() == 0.0 or h.mad() == 1.0  # |dev| median of {0,0,0,2,2} = 0


def test_histogram_empty_quantiles_zero():
    h = IntHistogram(0, 10)
    assert h.median() == 0.0
    assert h.mad() == 0.0
