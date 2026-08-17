"""Group H — determinism and metamorphic invariance."""

from __future__ import annotations

from pathlib import Path

from tests.layer1_fixtures import build_dataset, simple_reads

from minos_engine.common.canonical_json import canonical_json_bytes
from minos_engine.layer1.config import load_layer1_config
from minos_engine.layer1.contracts import ProfileRequest
from minos_engine.layer1.service import Layer1Service

CFG = load_layer1_config()


def _request(ds) -> ProfileRequest:
    return ProfileRequest(
        round_id="d",
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


def test_repeated_runs_identical(tmp_path: Path):
    ds = build_dataset(tmp_path, simple_reads(3000, n_pairs=50), contig_len=3000)
    svc = Layer1Service(require_prerequisite=False)
    b1 = svc.profile(_request(ds))
    b2 = svc.profile(_request(ds))
    assert b1.fingerprint.fingerprint_hash == b2.fingerprint.fingerprint_hash
    # canonical feature content identical (timings/runtime excluded from identity)
    from minos_engine.layer1.fingerprint import feature_values_hash

    assert feature_values_hash(b1.profile) == feature_values_hash(b2.profile)
    assert [w.model_dump() for w in b1.windows] == [w.model_dump() for w in b2.windows]


def test_same_content_two_builds_same_fingerprint(tmp_path: Path):
    reads = simple_reads(3000, n_pairs=50)
    ds1 = build_dataset(tmp_path / "a", reads, contig_len=3000)
    ds2 = build_dataset(tmp_path / "b", reads, contig_len=3000)
    svc = Layer1Service(require_prerequisite=False)
    b1 = svc.profile(_request(ds1))
    b2 = svc.profile(_request(ds2))
    assert b1.fingerprint.fingerprint_hash == b2.fingerprint.fingerprint_hash


def test_unrelated_file_does_not_change_output(tmp_path: Path):
    ds = build_dataset(tmp_path, simple_reads(3000, n_pairs=50), contig_len=3000)
    svc = Layer1Service(require_prerequisite=False)
    before = svc.profile(_request(ds)).fingerprint.fingerprint_hash
    # drop an unrelated file next to the BAM; must not affect the profile
    (tmp_path / "unrelated.txt").write_text("noise", encoding="utf-8")
    after = svc.profile(_request(ds)).fingerprint.fingerprint_hash
    assert before == after


def test_profile_content_bytes_stable(tmp_path: Path):
    # Everything except the operational (wall-clock) sections is byte-stable.
    ds = build_dataset(tmp_path, simple_reads(3000, n_pairs=50), contig_len=3000)
    svc = Layer1Service(require_prerequisite=False)
    volatile = {"stage_timings", "runtime_complexity", "degradation"}
    b1 = {
        k: v
        for k, v in svc.profile(_request(ds)).profile.model_dump(mode="json").items()
        if k not in volatile
    }
    b2 = {
        k: v
        for k, v in svc.profile(_request(ds)).profile.model_dump(mode="json").items()
        if k not in volatile
    }
    assert canonical_json_bytes(b1) == canonical_json_bytes(b2)
