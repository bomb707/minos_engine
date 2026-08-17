#!/usr/bin/env python
"""Build the Stage 0 PROTOCOL-READY gate and qualification report.

Thin wrapper over ``minos_engine.qualification.runner`` (no test-suite imports,
no terminal-output parsing). Run this at the *qualifiable source* commit
(Commit A, clean worktree); it writes the two Commit-B artifacts:

  * reports/STAGE0_QUALIFICATION_REPORT.md
  * gates/protocol-ready.json

Exit code 0 only when the gate is PASS.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from minos_engine.gates.contracts import GateStatus  # noqa: E402
from minos_engine.qualification.runner import qualify, write_outputs  # noqa: E402


def main() -> int:
    result = qualify(ROOT)
    gate_path, report_path = write_outputs(result, ROOT)
    gate = result.gate
    print(
        f"gate: {gate.status.value}  ->  {gate_path.relative_to(ROOT)}  "
        f"(gate_hash={gate.gate_hash[:12]}, tests={result.accounting.collected} collected/"
        f"{result.accounting.passed} passed, coverage={result.coverage.line_coverage_percent}%)"
    )
    if gate.status is not GateStatus.PASS:
        failing = [k for k, ok in gate.mandatory_checks.items() if not ok]
        print(f"FAILING CHECKS: {failing}", file=sys.stderr)
    return 0 if gate.status is GateStatus.PASS else 1


if __name__ == "__main__":
    raise SystemExit(main())
