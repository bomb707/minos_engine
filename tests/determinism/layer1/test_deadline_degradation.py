"""Group G — deadline / degradation behavior with an injected clock (no sleeps)."""

from __future__ import annotations

from pathlib import Path

from tests.layer1_fixtures import build_dataset, simple_reads

from minos_engine.layer1.config import load_layer1_config
from minos_engine.layer1.contracts import PileupMode, ProfileStatus
from minos_engine.layer1.service import Layer1Service

CFG = load_layer1_config()


class SteppingClock:
    """Monotonic clock that advances a fixed delta on every read (deterministic)."""

    def __init__(self, delta: float) -> None:
        self._t = 0.0
        self._delta = delta

    def monotonic(self) -> float:
        self._t += self._delta
        return self._t


def _request(ds):
    from minos_engine.layer1.contracts import ProfileRequest

    return ProfileRequest(
        round_id="g",
        bam_path=str(ds.bam),
        bai_path=str(ds.bai),
        reference_path=str(ds.reference),
        fai_path=str(ds.fai),
        region_source=ds.region_source,
        region_coordinate_convention="one_based_inclusive",
        budget_seconds=120,
        cpu_limit=1,
        memory_limit_bytes=1_000_000_000,
        profiler_config_version=CFG.profiler_config_version,
        profiler_config_hash=CFG.config_hash,
    )


def test_pileup_skipped_under_deadline_pressure(tmp_path: Path):
    ds = build_dataset(tmp_path, simple_reads(3000, n_pairs=40), contig_len=3000)
    # a fast-advancing clock exhausts the budget before pileup -> deterministic degrade
    svc = Layer1Service(clock=SteppingClock(30.0), require_prerequisite=False)
    bundle = svc.profile(_request(ds))
    assert bundle.profile.status is ProfileStatus.PARTIAL
    assert bundle.profile.runtime_complexity.chosen_pileup_mode is PileupMode.SKIPPED
    assert bundle.profile.degradation  # a degradation record exists
    rec = bundle.profile.degradation[0]
    assert "variant_evidence" in rec.omitted_features
    assert rec.usable is True
    # no silent COMPLETE with missing families
    assert "variant_evidence" not in bundle.profile.completion.completed_families


def test_full_run_completes_with_real_clock(tmp_path: Path):
    ds = build_dataset(tmp_path, simple_reads(3000, n_pairs=40), contig_len=3000)
    svc = Layer1Service(require_prerequisite=False)
    bundle = svc.profile(_request(ds))
    assert bundle.profile.status is ProfileStatus.COMPLETE
    assert "variant_evidence" in bundle.profile.completion.completed_families
