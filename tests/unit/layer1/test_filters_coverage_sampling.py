"""Groups C/D — read-filter policy, coverage accumulator, sampling determinism."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from minos_engine.layer1.coverage import (
    DUP_INCLUDING,
    FRAGMENT_PRIMARY,
    CoverageAccumulator,
    coverage_view,
)
from minos_engine.layer1.filters import ReadFilterPolicy
from minos_engine.layer1.sampling import WindowFeature, select_windows


@dataclass
class FakeRead:
    is_unmapped: bool = False
    is_secondary: bool = False
    is_supplementary: bool = False
    is_duplicate: bool = False
    is_qcfail: bool = False
    mapping_quality: int = 60


def test_filter_exclusion_priority_and_hash_stable():
    p = ReadFilterPolicy()
    assert p.classify(FakeRead(is_unmapped=True, is_duplicate=True)) == "unmapped"
    assert p.classify(FakeRead(is_secondary=True)) == "secondary"
    assert p.classify(FakeRead(is_duplicate=True)) == "duplicate"
    assert p.classify(FakeRead()) is None
    assert p.eligible(FakeRead()) is True
    assert p.policy_hash() == ReadFilterPolicy().policy_hash()


def test_filter_mapq_floor():
    p = ReadFilterPolicy(min_mapping_quality=20)
    assert p.classify(FakeRead(mapping_quality=10)) == "below_mapq"
    assert p.classify(FakeRead(mapping_quality=30)) is None


def test_coverage_difference_array_exact():
    cov = CoverageAccumulator(region_start0=100, length_bp=10)
    # two overlapping reads: [100,105) and [103,108)
    cov.add(DUP_INCLUDING, 100, 105)
    cov.add(DUP_INCLUDING, 103, 108)
    depth = cov.depth(DUP_INCLUDING)
    expected = np.array([1, 1, 1, 2, 2, 1, 1, 1, 0, 0])
    assert depth.tolist() == expected.tolist()


def test_coverage_clips_to_region():
    cov = CoverageAccumulator(region_start0=100, length_bp=5)
    cov.add(FRAGMENT_PRIMARY, 98, 102)  # starts before region
    depth = cov.depth(FRAGMENT_PRIMARY)
    assert depth.tolist() == [1, 1, 0, 0, 0]


def test_coverage_view_stats():
    depth = np.array([10, 10, 10, 0, 20])
    v = coverage_view(
        depth,
        view_name="v",
        depth_semantics="s",
        quantile_ps=(0.5,),
        min_callable_depth=10,
    )
    assert v.max_depth == 20
    assert v.zero_depth_fraction == 0.2
    assert v.callable_base_fraction == 0.8  # 4 of 5 >= 10
    assert v.mean_depth_reads_per_base == 10.0


def _feat(wid, **over):
    base = {
        "depth_mean": 30.0,
        "mq_mean": 60.0,
        "bq_mean": 35.0,
        "softclip_read_fraction": 0.0,
        "nm_per_base": 0.0,
        "indel_burden": 0.0,
        "entropy_bits": 1.9,
        "homopolymer_fraction": 0.0,
        "is_boundary": False,
    }
    base.update(over)
    return WindowFeature(window_id=wid, **base)


_THR = {
    "low_coverage_depth": 10.0,
    "high_coverage_depth": 100.0,
    "low_mapping_quality_mean": 40.0,
    "low_base_quality_mean": 30.0,
    "clipping_read_fraction": 0.1,
    "nm_per_base": 0.02,
    "indel_burden": 0.02,
    "low_entropy_bits": 1.5,
    "homopolymer_burden": 0.05,
}


def test_sampling_is_deterministic_and_bounded():
    feats = [_feat(i, depth_mean=5.0 if i == 3 else 30.0) for i in range(8)]
    feats[0] = _feat(0, is_boundary=True)
    feats[7] = _feat(7, is_boundary=True)
    plan1 = select_windows(feats, _THR, per_stratum_quota=2, total_quota=4, tiebreak_seed="seed")
    plan2 = select_windows(feats, _THR, per_stratum_quota=2, total_quota=4, tiebreak_seed="seed")
    assert plan1.selected == plan2.selected
    assert len(plan1.selected) <= 4
    assert 3 in plan1.selected  # low-coverage window reserved
    # weights are 1/pi for selected, 0 for unselected
    for wid in plan1.selected:
        assert plan1.analysis_weight[wid] > 0
    assert plan1.plan_hash == plan2.plan_hash


def test_sampling_semantic_change_changes_hash():
    feats = [_feat(i) for i in range(5)]
    a = select_windows(feats, _THR, per_stratum_quota=2, total_quota=4, tiebreak_seed="seed-a")
    b = select_windows(feats, _THR, per_stratum_quota=2, total_quota=4, tiebreak_seed="seed-b")
    assert a.plan_hash != b.plan_hash
