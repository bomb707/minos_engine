"""Integration — all replay fixtures process; invalid ones fail closed."""

from __future__ import annotations

import pytest

from minos_engine.common.errors import ConfigValidationError, PolicyViolationError
from minos_engine.twin.execution_plan import build_execution_plan
from minos_engine.twin.fixtures import load_replay_fixture
from minos_engine.twin.identities import ToolIdentity
from minos_engine.twin.service import TwinService
from tests.conftest import REPO_ROOT

REPLAY = REPO_ROOT / "tests" / "fixtures" / "twin" / "replay"
_VALID = ["valid", "snp_heavy", "indel_heavy", "high_fp", "high_fn", "zero_boundary"]
_H = "a" * 64


@pytest.mark.parametrize("name", _VALID)
def test_valid_replay_fixtures(name):
    fixture = load_replay_fixture(REPLAY / f"{name}.json")
    service = TwinService(protocol_ready_check=lambda: _H)
    result = service.replay_fixture(fixture, now_iso="2026-08-17T12:00:00+00:00", fixture_hash=_H)
    assert result.manifest.manifest_hash
    assert result.plan.caller == "gatk"


def test_invalid_caller_fixture_rejected():
    fixture = load_replay_fixture(REPLAY / "invalid_caller.json")
    with pytest.raises(PolicyViolationError):
        build_execution_plan(fixture.request)


@pytest.mark.parametrize("name", ["unknown_parameter", "out_of_range"])
def test_bad_config_fixtures_rejected(name):
    fixture = load_replay_fixture(REPLAY / f"{name}.json")
    with pytest.raises(ConfigValidationError):
        build_execution_plan(fixture.request)


def test_fixture_manifest_hashes_match_files():
    import hashlib
    import json

    base = REPO_ROOT / "tests" / "fixtures" / "twin"
    manifest = json.loads((base / "FIXTURE_MANIFEST.json").read_text())
    for entry in manifest["fixtures"]:
        actual = hashlib.sha256((base / entry["path"]).read_bytes()).hexdigest()
        assert actual == entry["sha256"], entry["path"]


def test_offline_truth_tool_identity_unavailable_without_pin():
    # A tool identity without version/digest is unavailable (fail closed).
    from minos_engine.common.errors import UnavailableError
    from minos_engine.twin.unavailable import AvailabilityStatus

    bare = ToolIdentity(name="gatk")
    assert bare.availability is AvailabilityStatus.UNAVAILABLE
    with pytest.raises(UnavailableError):
        bare.require_available()
