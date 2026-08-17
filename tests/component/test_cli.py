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
    # Runtime policy is reported and enforced (Python 3.12 only).
    assert out["runtime"]["supported"] == "CPython 3.12.x"
    assert out["runtime"]["is_supported"] is True
    assert out["mandatory_checks"]["python_runtime_is_3_12"] is True


def test_git_history_check_json(capsys):
    from tests.conftest import REPO_ROOT

    code = main(
        [
            "git-history",
            "check",
            "--protocol-gate",
            str(REPO_ROOT / "gates" / "protocol-ready.json"),
            "--twin-gate",
            str(REPO_ROOT / "gates" / "twin-ready.json"),
            "--base-dir",
            str(REPO_ROOT),
            "--json",
        ]
    )
    assert code == 0
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is True
    assert out["shallow"] is False


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


def test_human_output_branches(capsys):
    # Exercise the non-JSON (human) rendering paths.
    assert main(["doctor"]) == 0
    assert "overall health" in capsys.readouterr().out
    assert main(["protocol", "snapshot", "--fixture", str(API_FIXTURES / "valid_round.json")]) == 0
    assert "snapshot_id" in capsys.readouterr().out
    assert main(["config", "validate", "--config", str(GATK_FIXTURES / "default_config.json")]) == 0
    assert "CONFIG valid" in capsys.readouterr().out


def _write_pass_gate(tmp_path):
    from minos_engine.gates.contracts import EvidenceItem, GateArtifact, GateStatus
    from minos_engine.gates.verifier import write_gate

    gate = GateArtifact(
        gate_name="TEST",
        status=GateStatus.PASS,
        engine_git_sha="abc",
        mandatory_checks={"a": True},
        evidence=(EvidenceItem(description="e", path="reports/x.md", sha256="a" * 64),),
        created_at="2026-08-17T12:00:00+00:00",
    )
    p = tmp_path / "gate.json"
    write_gate(gate, p)
    return p


def test_gate_verify_integrity_and_require_pass(tmp_path, capsys):
    p = _write_pass_gate(tmp_path)
    assert main(["gate", "verify-integrity", "--gate", str(p), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["mode"] == "integrity"
    # TEST is an unregistered gate name, so require-pass succeeds on a PASS gate.
    assert main(["gate", "require-pass", "--gate", str(p), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["mode"] == "promotion"


def test_gate_require_pass_missing_returns_verify_failed():
    assert main(["gate", "require-pass", "--gate", "/nonexistent/gate.json"]) == 1
