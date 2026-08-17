"""Cost-model and config-identity unit tests."""

from __future__ import annotations

from minos_engine.layer1.config import load_layer1_config
from minos_engine.layer1.contracts import PileupMode
from minos_engine.layer1.cost_model import choose_mode, predict_pileup_seconds

_COEFFS = {"b0": -14.0, "b1": 0.85, "b2": 0.55, "b3": 0.01, "b4": 2e-5, "b5": 1.2, "b6": 0.8}


def test_small_region_chooses_full():
    pred = predict_pileup_seconds(
        _COEFFS,
        region_bp=50_000,
        read_count=5_000,
        mean_depth=30,
        max_depth_proxy=60,
        clipping_rate=0.02,
        cigar_complexity=0.02,
    )
    assert (
        choose_mode(
            pred, pileup_soft_seconds=90, remaining_seconds=280, serialization_reserve_seconds=10
        )
        is PileupMode.FULL
    )


def test_large_region_chooses_adaptive():
    pred = predict_pileup_seconds(
        _COEFFS,
        region_bp=10_000_000,
        read_count=1_500_000,
        mean_depth=25,
        max_depth_proxy=52,
        clipping_rate=0.04,
        cigar_complexity=0.02,
    )
    assert pred > 90
    assert (
        choose_mode(
            pred, pileup_soft_seconds=90, remaining_seconds=280, serialization_reserve_seconds=10
        )
        is PileupMode.ADAPTIVE
    )


def test_no_time_skips():
    assert (
        choose_mode(
            1.0, pileup_soft_seconds=90, remaining_seconds=5, serialization_reserve_seconds=10
        )
        is PileupMode.SKIPPED
    )


def test_config_loads_and_is_hashable():
    cfg = load_layer1_config()
    assert cfg.profiler_config_version == "layer1-profiler-v1"
    assert len(cfg.config_hash) == 64
    assert cfg.window.primary_bp == 100000
    assert cfg.budget.hard_seconds == 300
    assert cfg.coverage.overlap_policy == "fragment_primary"
    # stable across calls (cached, same content)
    assert load_layer1_config().config_hash == cfg.config_hash
