"""Protocol-contract tests: saved fixtures satisfy the JSON schemas and contracts."""

from __future__ import annotations

from minos_engine.protocol.client import FixtureProtocolClient
from minos_engine.schema_registry import validate_against
from tests.conftest import API_FIXTURES


def test_valid_round_fixture_builds_and_schema_validates():
    client = FixtureProtocolClient(API_FIXTURES / "valid_round.json")
    snap = client.load_snapshot()
    validate_against("round-protocol-snapshot-v1", snap.model_dump(mode="json"))
    ctx = client.load_round_context(snap)
    validate_against("round-context-v1", ctx.model_dump(mode="json"))


def test_parameter_space_fixture_schema_validates():
    import json

    raw = json.loads((API_FIXTURES / "gatk_parameter_space.json").read_text())
    validate_against("parameter-space-snapshot-v1", raw)


def test_fixture_snapshot_is_reproducible():
    a = FixtureProtocolClient(API_FIXTURES / "valid_round.json").load_snapshot()
    b = FixtureProtocolClient(API_FIXTURES / "valid_round.json").load_snapshot()
    assert a.snapshot_id == b.snapshot_id
    assert a.model_dump_json() == b.model_dump_json()
