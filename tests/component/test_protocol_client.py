"""Component tests: fixture-backed protocol client, parameter/network parsing."""

from __future__ import annotations

import pytest

from minos_engine.common.errors import (
    ParameterSpaceError,
    ProtocolError,
    SnapshotIncompleteError,
    UnavailableError,
)
from minos_engine.protocol.client import (
    FixtureProtocolClient,
    LiveProtocolClient,
    RawProtocolResponse,
)
from minos_engine.protocol.network_config import parse_network_config
from minos_engine.protocol.parameter_ranges import parse_parameter_space
from minos_engine.protocol.snapshot import build_round_context, build_snapshot
from tests.conftest import make_raw_payload


def test_fixture_client_builds_snapshot_and_context(valid_round_path):
    client = FixtureProtocolClient(valid_round_path)
    snap = client.load_snapshot()
    assert snap.round_status.value == "open"
    assert snap.exact_region.contig == "chr19"
    ctx = client.load_round_context(snap)
    assert ctx.protocol_snapshot_id == snap.snapshot_id
    assert ctx.bam_artifact.sha256


def test_fixture_client_missing_file():
    with pytest.raises(ProtocolError):
        FixtureProtocolClient("/nonexistent/fixture.json").fetch_raw()


def test_live_client_fails_closed():
    with pytest.raises(UnavailableError):
        LiveProtocolClient("https://api.theminos.ai").fetch_raw()


def test_parameter_space_parse_and_hash():
    raw = make_raw_payload()["parameter_space"]
    ps = parse_parameter_space(raw, retrieved_at="2026-08-17T00:00:00+00:00", stale=False)
    assert ps.caller == "gatk"
    assert "min_pruning" in ps.parameters
    assert len(ps.parameter_space_hash) == 64


def test_parameter_space_incomplete_fails_closed():
    with pytest.raises(ParameterSpaceError):
        parse_parameter_space(
            {"caller": "gatk", "source": "x", "parameters": {}},
            retrieved_at="2026-08-17T00:00:00+00:00",
            stale=False,
        )


def test_parameter_space_non_gatk_rejected():
    with pytest.raises(ParameterSpaceError):
        parse_parameter_space(
            {"caller": "deepvariant", "source": "x", "parameters": {"a": {"type": "int"}}},
            retrieved_at="2026-08-17T00:00:00+00:00",
            stale=False,
        )


def test_network_config_parse():
    nc = parse_network_config({"network": "finney", "api_base_url": "https://api.theminos.ai"})
    assert nc.network == "finney"
    assert len(nc.raw_hash) == 64


def test_round_context_requires_artifacts():
    payload = make_raw_payload()
    del payload["artifacts"]
    raw = RawProtocolResponse(payload=payload, retrieved_at="2026-08-17T12:00:00+00:00")
    snap = build_snapshot(raw)
    with pytest.raises(SnapshotIncompleteError):
        build_round_context(raw, snap)
