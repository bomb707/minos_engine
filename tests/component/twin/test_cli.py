"""Test group H — Twin CLI (plan/replay/parity), JSON + exit codes."""

from __future__ import annotations

import json

from minos_engine.cli.main import main
from tests.conftest import REPO_ROOT

TWIN = REPO_ROOT / "tests" / "fixtures" / "twin"


def test_twin_plan_json(capsys):
    assert main(["twin", "plan", "--request", str(TWIN / "replay" / "valid.json"), "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["caller"] == "gatk"
    assert len(out["plan_hash"]) == 64
    assert "{reference}" in out["redacted_command"]


def test_twin_replay_json_unavailable_scorer(capsys):
    assert main(["twin", "replay", "--fixture", str(TWIN / "replay" / "valid.json"), "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["mode"] == "FIXTURE_REPLAY"
    assert out["scorer_status"] == "UNAVAILABLE"
    assert out["declared_parity_level"] == "FIXTURE_REPLAY"


def test_twin_parity_match_and_mismatch(capsys):
    ok = main(
        [
            "twin",
            "parity",
            "--expected",
            str(TWIN / "parity" / "expectation.json"),
            "--observed",
            str(TWIN / "parity" / "observation_match.json"),
        ]
    )
    assert ok == 0
    capsys.readouterr()
    bad = main(
        [
            "twin",
            "parity",
            "--expected",
            str(TWIN / "parity" / "expectation.json"),
            "--observed",
            str(TWIN / "parity" / "observation_mismatch.json"),
        ]
    )
    assert bad == 1  # verify-failed on mismatch


def test_twin_plan_missing_fixture_nonzero():
    assert main(["twin", "plan", "--request", "/nonexistent/fixture.json"]) != 0


def test_twin_help_states_not_live(capsys):
    import pytest

    with pytest.raises(SystemExit):
        main(["twin", "--help"])
    out = capsys.readouterr().out.lower()
    assert "not a live validator" in out
    assert "truth isolation" in out
