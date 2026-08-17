#!/usr/bin/env python
"""Build the Stage 0 PROTOCOL-READY gate and qualification report.

Runs the real quality gates (pytest, ruff, ruff format, mypy), performs
in-process mandatory checks, computes schema/registry/config hashes, and writes:

  * reports/STAGE0_QUALIFICATION_REPORT.md
  * gates/protocol-ready.json

A PASS gate is not constructible unless every mandatory check is true
(GateArtifact invariant), so this script cannot emit a green gate on a red tree.
"""

from __future__ import annotations

import re
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(SRC))

from minos_engine import __version__  # noqa: E402
from minos_engine.callers.gatk.config import canonicalize_config  # noqa: E402
from minos_engine.callers.gatk.parameter_registry import REGISTRY  # noqa: E402
from minos_engine.common.errors import SnapshotIncompleteError, StageNotReadyError  # noqa: E402
from minos_engine.common.hashing import canonical_hash  # noqa: E402
from minos_engine.common.versions import engine_git_sha  # noqa: E402
from minos_engine.gates.contracts import EvidenceItem, GateArtifact, GateStatus  # noqa: E402
from minos_engine.gates.verifier import write_gate  # noqa: E402
from minos_engine.layer1.service import Layer1Service  # noqa: E402
from minos_engine.layer2.service import Layer2Service  # noqa: E402
from minos_engine.manifests.builder import (  # noqa: E402
    engine_config_hash,
    protocol_contract_hash,
)
from minos_engine.schema_registry import available_schemas  # noqa: E402
from minos_engine.settings import Settings  # noqa: E402

BIN = Path(sys.executable).parent


def _run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, cwd=str(ROOT), capture_output=True, text=True, check=False)


def _pytest() -> tuple[bool, int, str]:
    proc = _run([str(BIN / "python"), "-m", "pytest", "-q"])
    text = proc.stdout + proc.stderr
    m = re.search(r"(\d+) passed", text)
    count = int(m.group(1)) if m else 0
    return proc.returncode == 0, count, text.strip().splitlines()[-1] if text.strip() else ""


def _tool_ok(cmd: list[str]) -> bool:
    return _run(cmd).returncode == 0


def _required_identity_fails_closed() -> bool:
    from tests.conftest import make_raw_payload, make_raw_response  # type: ignore

    from minos_engine.protocol.snapshot import build_snapshot

    payload = make_raw_payload()
    del payload["provenance"]["scorer_hash"]
    try:
        build_snapshot(make_raw_response(payload))
    except SnapshotIncompleteError:
        return True
    return False


def _layers_blocked() -> bool:
    ok = 0
    try:
        Layer1Service().analyze(None)  # type: ignore[arg-type]
    except StageNotReadyError:
        ok += 1
    try:
        Layer2Service().select_config(None)  # type: ignore[arg-type]
    except StageNotReadyError:
        ok += 1
    return ok == 2


def _no_truth_tokens() -> bool:
    tokens = ("truth.vcf", "mutations.vcf", "hidden_score", "leaderboard", "final_test")
    for p in SRC.rglob("*.py"):
        # crude but sufficient: reject only string-literal-ish path references
        text = p.read_text(encoding="utf-8").lower()
        for tok in tokens:
            if f'"{tok}' in text or f"'{tok}" in text or f"/{tok}" in text:
                return False
    return True


def _docs_present() -> bool:
    required = [
        "docs/architecture/OVERVIEW.md",
        "docs/architecture/DEPENDENCY_RULES.md",
        "docs/contracts/PROTOCOL_CONTRACTS.md",
        "docs/contracts/GATK_CONFIG_CONTRACT.md",
        "docs/runbooks/PROTOCOL_SNAPSHOT.md",
        "docs/decisions/ADR-0001-SINGLE-CANONICAL-ENGINE.md",
        "docs/decisions/ADR-0002-GATK-ONLY.md",
        "docs/decisions/ADR-0003-TRUTH-ISOLATION.md",
        "docs/decisions/ADR-0004-STAGE-GATED-DEVELOPMENT.md",
        "README.md",
    ]
    return all((ROOT / r).exists() for r in required)


def _determinism_ok() -> bool:
    a = canonical_hash({"b": 1, "a": 2})
    b = canonical_hash({"a": 2, "b": 1})
    return a == b


def main() -> int:
    git_sha = engine_git_sha() or ""
    settings = Settings.load()

    tests_pass, test_count, test_summary = _pytest()
    ruff_pass = _tool_ok([str(BIN / "ruff"), "check", "."])
    fmt_pass = _tool_ok([str(BIN / "ruff"), "format", "--check", "."])
    mypy_pass = _tool_ok([str(BIN / "mypy"), "src"])

    registry_hash = REGISTRY.registry_hash()
    cfg_hash = engine_config_hash(settings)
    contract_hash = protocol_contract_hash()
    default_config_hash = canonicalize_config({}).config_hash

    mandatory = {
        "all_tests_pass": tests_pass,
        "ruff_check_pass": ruff_pass,
        "ruff_format_pass": fmt_pass,
        "mypy_pass": mypy_pass,
        "canonical_determinism": _determinism_ok(),
        "gatk_only_policy": settings.runtime_policy.active == "gatk"
        and settings.runtime_policy.allowed == ("gatk",),
        "gatk_registry_has_25_params": len(REGISTRY) == 25,
        "required_identity_fails_closed": _required_identity_fails_closed(),
        "layer1_not_implemented_and_layer2_blocked": _layers_blocked(),
        "no_truth_or_locked_test_access": _no_truth_tokens(),
        "six_schemas_present": len(available_schemas()) == 6,
        "documentation_complete": _docs_present(),
        "engine_git_sha_available": bool(git_sha),
    }

    status = GateStatus.PASS if all(mandatory.values()) else GateStatus.REJECT
    created_at = datetime.now(UTC).isoformat()

    evidence = (
        EvidenceItem(
            description="Pre-implementation audit", path="reports/STAGE0_PREIMPLEMENTATION_AUDIT.md"
        ),
        EvidenceItem(
            description="Stage 0 qualification report",
            path="reports/STAGE0_QUALIFICATION_REPORT.md",
        ),
        EvidenceItem(description="JSON schemas", path="schemas/"),
        EvidenceItem(description="Architecture & truth-isolation tests", path="tests/leakage/"),
    )
    input_hashes = {
        "gatk_registry_hash": registry_hash,
        "engine_config_hash": cfg_hash,
        "protocol_contract_hash": contract_hash,
        "default_config_hash": default_config_hash,
    }

    # A PASS gate cannot be constructed if any mandatory check is false.
    gate = GateArtifact(
        gate_name="PROTOCOL-READY",
        status=status,
        engine_git_sha=git_sha or "unavailable",
        input_hashes=input_hashes,
        evidence=evidence,
        mandatory_checks=mandatory,
        created_at=created_at,
    )
    gate_path = write_gate(gate, ROOT / "gates" / "protocol-ready.json")

    report = _render_report(
        git_sha=git_sha,
        test_count=test_count,
        test_summary=test_summary,
        tools={"ruff_check": ruff_pass, "ruff_format": fmt_pass, "mypy": mypy_pass},
        hashes=input_hashes,
        mandatory=mandatory,
        status=status,
        gate_hash=gate.gate_hash,
        created_at=created_at,
    )
    (ROOT / "reports" / "STAGE0_QUALIFICATION_REPORT.md").write_text(report, encoding="utf-8")

    print(
        f"gate: {status.value}  ->  {gate_path.relative_to(ROOT)}  (gate_hash={gate.gate_hash[:12]})"
    )
    return 0 if status is GateStatus.PASS else 1


def _render_report(**k: object) -> str:
    mandatory = k["mandatory"]
    assert isinstance(mandatory, dict)
    hashes = k["hashes"]
    assert isinstance(hashes, dict)
    tools = k["tools"]
    assert isinstance(tools, dict)
    check_rows = "\n".join(
        f"| `{name}` | {'PASS' if ok else 'FAIL'} |" for name, ok in mandatory.items()
    )
    hash_rows = "\n".join(f"| `{name}` | `{value}` |" for name, value in hashes.items())
    return f"""# STAGE 0 — Qualification Report

**Gate:** PROTOCOL-READY — **{k["status"].value if hasattr(k["status"], "value") else k["status"]}**
**Engine version:** {__version__}
**Engine Git SHA:** `{k["git_sha"]}`
**Generated:** {k["created_at"]}
**Gate hash:** `{k["gate_hash"]}`

> This report is generated by `scripts/build_protocol_ready_gate.py`. It is not
> hand-authored. A PASS gate is not constructible with any failing mandatory check.

## Test execution
- Command: `pytest -q`
- Result: {k["test_summary"]}
- Passed: **{k["test_count"]}**

## Static analysis
| Tool | Command | Result |
|---|---|---|
| ruff (lint) | `ruff check .` | {"PASS" if tools["ruff_check"] else "FAIL"} |
| ruff (format) | `ruff format --check .` | {"PASS" if tools["ruff_format"] else "FAIL"} |
| mypy | `mypy src` | {"PASS" if tools["mypy"] else "FAIL"} |

## Mandatory checks
| Check | Status |
|---|---|
{check_rows}

## Hashes
| Identity | Value |
|---|---|
{hash_rows}

## Evidence
- `reports/STAGE0_PREIMPLEMENTATION_AUDIT.md`
- `gates/protocol-ready.json`
- `schemas/` (6 JSON Schemas)
- `tests/` (unit, component, protocol_contract, integration, leakage, acceptance)

## Known limitations
- Live protocol integration is not enabled (fixture-backed only); the live
  client fails closed with `UnavailableError`.
- Commit-reveal is modeled as typed-unavailable (the platform has no crypto
  commit-reveal scheme).
- Layer 1 is not implemented; Layer 2 is blocked until L1-READY.
- No GATK/hap.py execution, no PostgreSQL, no ML in Stage 0.
"""


if __name__ == "__main__":
    raise SystemExit(main())
