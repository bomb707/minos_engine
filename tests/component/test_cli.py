"""Component tests: CLI commands via main(argv)."""

from __future__ import annotations

import json

import pytest

from minos_engine.cli.main import main
from tests.conftest import API_FIXTURES, GATK_FIXTURES


def test_doctor_json(capsys):
    assert main(["doctor", "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["gatk_registry"]["parameter_count"] == 25
    assert out["stage_gates"]["layer2_blocked"] is True
    assert out["overall_health"] == "healthy"


def test_protocol_snapshot_json(capsys):
    assert (
        main(
            ["protocol", "snapshot", "--fixture", str(API_FIXTURES / "valid_round.json"), "--json"]
        )
        == 0
    )
    out = json.loads(capsys.readouterr().out)
    assert len(out["snapshot_id"]) == 64
    assert out["exact_region"]["contig"] == "chr19"


def test_config_validate_ok(capsys):
    code = main(
        [
            "config",
            "validate",
            "--config",
            str(GATK_FIXTURES / "default_config.json"),
            "--parameter-space",
            str(API_FIXTURES / "gatk_parameter_space.json"),
            "--json",
        ]
    )
    assert code == 0
    out = json.loads(capsys.readouterr().out)
    assert len(out["effective_config"]) == 25


def test_config_validate_invalid_returns_input_error(tmp_path, capsys):
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"min_pruning": 999}))
    assert main(["config", "validate", "--config", str(bad)]) == 2


def test_manifest_build_json(capsys):
    code = main(
        [
            "manifest",
            "build",
            "--fixture",
            str(API_FIXTURES / "valid_round.json"),
            "--created-at",
            "2026-08-17T12:00:00+00:00",
            "--json",
        ]
    )
    assert code == 0
    out = json.loads(capsys.readouterr().out)
    assert out["git_sha"]
    assert len(out["gatk_registry_hash"]) == 64


def test_gate_verify_missing_returns_verify_failed(capsys):
    assert main(["gate", "verify", "--gate", "/nonexistent/gate.json"]) == 1


def test_no_subcommand_errors():
    with pytest.raises(SystemExit):
        main([])
