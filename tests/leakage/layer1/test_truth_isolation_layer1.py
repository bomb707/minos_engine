"""Group I — Layer 1 runtime truth isolation.

Layer 1 opens only the explicit BAM/BAI/FASTA/FAI paths it is given and never
enumerates a directory, so truth/mutation artifacts that sit beside a round's BAM
can never be discovered or read. The static import/string scans in
``tests/leakage/test_truth_isolation.py`` already cover the ``layer1`` package;
this adds a runtime metamorphic check.
"""

from __future__ import annotations

from pathlib import Path

from tests.layer1_fixtures import build_dataset, simple_reads

from minos_engine.layer1.config import load_layer1_config
from minos_engine.layer1.contracts import ProfileRequest
from minos_engine.layer1.service import Layer1Service

CFG = load_layer1_config()

# Sentinel filenames a real round directory would contain next to the BAM.
_SENTINELS = ("truth.vcf.gz", "mutations.vcf.gz", "confident_regions.bed")


def _request(ds):
    return ProfileRequest(
        round_id="iso",
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


def test_truth_sentinels_do_not_affect_profile(tmp_path: Path):
    ds = build_dataset(tmp_path, simple_reads(3000, n_pairs=40), contig_len=3000)
    svc = Layer1Service(require_prerequisite=False)
    before = svc.profile(_request(ds)).fingerprint.fingerprint_hash

    # place truth/mutation sentinels beside the BAM
    for name in _SENTINELS:
        (tmp_path / name).write_bytes(b"POISON-should-never-be-read")
    after = svc.profile(_request(ds)).fingerprint.fingerprint_hash
    assert before == after  # identical -> Layer 1 never consulted the sentinels


def test_profile_output_contains_no_forbidden_paths(tmp_path: Path):
    ds = build_dataset(tmp_path, simple_reads(3000, n_pairs=40), contig_len=3000)
    for name in _SENTINELS:
        (tmp_path / name).write_bytes(b"POISON")
    svc = Layer1Service(require_prerequisite=False)
    bundle = svc.profile(_request(ds))
    blob = bundle.profile.model_dump_json()
    for token in ("truth.vcf", "mutations.vcf", "confident_regions"):
        assert token not in blob
