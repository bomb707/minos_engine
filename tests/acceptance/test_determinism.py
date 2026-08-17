"""Determinism: identical fixtures -> identical bytes/hashes across repeated runs."""

from __future__ import annotations

from minos_engine.callers.gatk.config import canonicalize_config
from minos_engine.callers.gatk.parameter_registry import REGISTRY
from minos_engine.gates.contracts import EvidenceItem, GateArtifact, GateStatus
from minos_engine.manifests.builder import build_release_manifest
from minos_engine.protocol.client import FixtureProtocolClient
from tests.conftest import API_FIXTURES, GATK_FIXTURES


def test_snapshot_bytes_and_hash_reproducible():
    def build():
        return FixtureProtocolClient(API_FIXTURES / "valid_round.json").load_snapshot()

    a, b = build(), build()
    assert a.snapshot_id == b.snapshot_id
    assert a.model_dump_json() == b.model_dump_json()


def test_config_bytes_and_hash_reproducible():
    import json

    requested = json.loads((GATK_FIXTURES / "default_config.json").read_text())
    a = canonicalize_config(requested)
    b = canonicalize_config(requested)
    assert a.config_hash == b.config_hash
    assert a.effective_bytes() == b.effective_bytes()


def test_registry_hash_reproducible():
    assert REGISTRY.registry_hash() == REGISTRY.registry_hash()


def test_gate_hash_excludes_creation_time():
    def gate(created_at: str) -> GateArtifact:
        return GateArtifact(
            gate_name="X",
            status=GateStatus.PASS,
            engine_git_sha="sha",
            mandatory_checks={"a": True},
            evidence=(EvidenceItem(description="r", path="reports/r.md"),),
            created_at=created_at,
        )

    assert (
        gate("2026-08-17T12:00:00+00:00").gate_hash == gate("2030-01-01T00:00:00+00:00").gate_hash
    )


def test_manifest_content_hash_reproducible():
    snap = FixtureProtocolClient(API_FIXTURES / "valid_round.json").load_snapshot()
    m1 = build_release_manifest(snap, created_at="2026-08-17T12:00:00+00:00")
    m2 = build_release_manifest(snap, created_at="2030-01-01T00:00:00+00:00")
    assert m1.content_hash() == m2.content_hash()
