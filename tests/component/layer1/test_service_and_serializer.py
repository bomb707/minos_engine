"""Groups J/L — service workflow, atomic serialization, failure injection."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from tests.conftest import REPO_ROOT
from tests.layer1_fixtures import build_dataset, simple_reads

from minos_engine.common.errors import GateError
from minos_engine.layer1.config import load_layer1_config
from minos_engine.layer1.contracts import ProfileRequest, ProfileStatus
from minos_engine.layer1.serializer import WINDOW_ARROW_SCHEMA
from minos_engine.layer1.service import Layer1Service

CFG = load_layer1_config()


def _request(ds, region=None) -> ProfileRequest:
    return ProfileRequest(
        round_id="synthetic",
        bam_path=str(ds.bam),
        bai_path=str(ds.bai),
        reference_path=str(ds.reference),
        fai_path=str(ds.fai),
        region_source=region or ds.region_source,
        region_coordinate_convention="one_based_inclusive",
        budget_seconds=120,
        cpu_limit=1,
        memory_limit_bytes=1_000_000_000,
        profiler_config_version=CFG.profiler_config_version,
        profiler_config_hash=CFG.config_hash,
    )


def _dataset(tmp_path: Path):
    return build_dataset(tmp_path, simple_reads(3000, n_pairs=60), contig="chr1", contig_len=3000)


def test_profile_complete(tmp_path: Path):
    svc = Layer1Service(require_prerequisite=False)
    bundle = svc.profile(_request(_dataset(tmp_path)))
    assert bundle.profile.status is ProfileStatus.COMPLETE
    assert bundle.profile.provenance.profiler_version == "layer1-profiler-v1"
    assert bundle.profile.identity.bam_size_bytes > 0
    assert len(bundle.windows) >= 1


def test_analyze_writes_three_artifacts(tmp_path: Path):
    svc = Layer1Service(require_prerequisite=False)
    out = tmp_path / "out"
    res = svc.analyze(_request(_dataset(tmp_path)), out)
    assert res.status is ProfileStatus.COMPLETE
    for p in (res.profile_path, res.windows_path, res.manifest_path):
        assert Path(p).exists()
    manifest = json.loads(Path(res.manifest_path).read_text())
    # manifest hashes the actual files
    import hashlib

    prof_sha = hashlib.sha256(Path(res.profile_path).read_bytes()).hexdigest()
    win_sha = hashlib.sha256(Path(res.windows_path).read_bytes()).hexdigest()
    assert manifest["profile_sha256"] == prof_sha
    assert manifest["windows_sha256"] == win_sha


def test_parquet_has_fixed_schema(tmp_path: Path):
    import pyarrow.parquet as pq

    svc = Layer1Service(require_prerequisite=False)
    out = tmp_path / "out"
    res = svc.analyze(_request(_dataset(tmp_path)), out)
    schema = pq.read_schema(res.windows_path)
    assert schema.names == WINDOW_ARROW_SCHEMA.names


def test_input_failure_is_typed_result(tmp_path: Path):
    ds = _dataset(tmp_path)
    ds.bam.unlink()
    svc = Layer1Service(require_prerequisite=False)
    res = svc.analyze(_request(ds), tmp_path / "out")
    assert res.status is ProfileStatus.FAILED
    assert res.failure_code == "INPUT_VALIDATION_FAILED"
    assert res.fallback_required


def test_serialization_failure_leaves_no_complete_set(tmp_path: Path, monkeypatch):
    import minos_engine.layer1.serializer as ser

    def boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(ser.os, "replace", boom)
    svc = Layer1Service(require_prerequisite=False)
    res = svc.analyze(_request(_dataset(tmp_path)), tmp_path / "out")
    assert res.status is ProfileStatus.FAILED
    assert res.failure_code == "SERIALIZATION_FAILURE"
    assert not (tmp_path / "out" / "bam-profile-v1.json").exists()


def test_config_hash_mismatch_rejected(tmp_path: Path):
    ds = _dataset(tmp_path)
    req = _request(ds).model_copy(update={"profiler_config_hash": "b" * 64})
    from minos_engine.common.errors import ConfigValidationError

    svc = Layer1Service(require_prerequisite=False)
    with pytest.raises(ConfigValidationError):
        svc.profile(req)


def test_accepted_twin_ready_prerequisite_verifies():
    # The accepted committed TWIN-READY gate authorizes Layer 1 in the real repo
    # (full git history present). A missing gate dir fails closed.
    from minos_engine.layer1.prerequisites import verify_twin_ready_prerequisite

    result = verify_twin_ready_prerequisite(REPO_ROOT)
    assert result.ok, result.reasons


def test_prerequisite_missing_gate_blocks(tmp_path: Path):
    svc = Layer1Service(base_dir=tmp_path, require_prerequisite=True)
    with pytest.raises(GateError):
        svc.profile(_request(_dataset(tmp_path)))
