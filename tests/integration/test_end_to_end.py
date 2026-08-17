"""Integration: snapshot -> config validate -> submission envelope -> manifest."""

from __future__ import annotations

from minos_engine.callers.gatk.config import canonicalize_config
from minos_engine.manifests.builder import build_release_manifest
from minos_engine.protocol.client import FixtureProtocolClient
from minos_engine.protocol.parameter_ranges import parse_parameter_space
from minos_engine.protocol.submission_contract import build_submission_envelope
from tests.conftest import API_FIXTURES


def test_full_stage0_flow():
    client = FixtureProtocolClient(API_FIXTURES / "valid_round.json")
    snap = client.load_snapshot()

    ps = parse_parameter_space(
        snap.parameter_ranges_raw, retrieved_at=snap.retrieved_at, stale=snap.stale
    )
    # the runtime parameter-space hash matches the snapshot's declared hash
    assert ps.parameter_space_hash == snap.parameter_space_hash

    cc = canonicalize_config({"min_pruning": 3}, parameter_space=ps)
    assert cc.effective_config["min_pruning"] == 3
    assert len(cc.effective_config) == 25

    env = build_submission_envelope(cc.effective_config, version="4.5.0.0")
    assert env.tool == "gatk"
    # submission hash is derived from the exact canonical envelope bytes
    from minos_engine.common.hashing import sha256_hex

    assert env.submission_hash() == sha256_hex(env.canonical_bytes())

    manifest = build_release_manifest(snap, created_at="2026-08-17T12:00:00+00:00")
    assert manifest.parameter_space_hash == snap.parameter_space_hash
    assert manifest.scorer_hash == snap.scorer_hash
    assert manifest.git_sha
