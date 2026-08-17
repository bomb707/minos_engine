"""Layer 2 entry-gate verification (L1-READY).

Layer 2 begins only after ``l1-ready.json`` verifies successfully (Layer 1 spec
§20 NEXT-STAGE LOCK). This verifier checks the full chain:

  * gate file loads and is schema-valid;
  * canonical ``gate_hash`` matches;
  * ``status == PASS``;
  * ``gate_name == "L1-READY"`` exactly;
  * the complete required-check set for L1-READY is present and true;
  * the qualification report exists and its SHA-256 matches the gate;
  * Layer 1 schema hash matches;
  * profiler configuration hash matches;
  * profiler version matches;
  * qualified source commit/tree is compatible (when expected values supplied);
  * every evidence file exists and every evidence hash matches.

In Stage 0 there is no ``l1-ready.json``, so this always rejects — the correct
blocked state. Layer 2 remains blocked after this change.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, ConfigDict

from minos_engine.common.errors import GateError
from minos_engine.gates.contracts import GateStatus
from minos_engine.gates.required_checks import required_checks_for
from minos_engine.gates.verifier import load_gate, require_gate_pass
from minos_engine.qualification.evidence import sha256_file

__all__ = ["EntryGateRequest", "EntryGateResult", "verify_l1_ready", "require_l1_ready"]

_L1_GATE_NAME = "L1-READY"


class EntryGateRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    l1_ready_path: str
    qualification_report_path: str
    expected_layer1_schema_hash: str
    expected_profiler_config_hash: str
    expected_profiler_version: str
    base_dir: str | None = None
    expected_qualified_source_git_sha: str | None = None
    expected_qualified_source_tree_sha: str | None = None


class EntryGateResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    ok: bool
    reasons: tuple[str, ...] = ()


def verify_l1_ready(request: EntryGateRequest) -> EntryGateResult:  # noqa: C901 - explicit checks
    reasons: list[str] = []
    path = Path(request.l1_ready_path)
    if not path.exists():
        return EntryGateResult(ok=False, reasons=("l1-ready.json is missing",))

    try:
        gate = load_gate(path)
    except Exception as exc:  # noqa: BLE001 - report any parse/schema/hash failure
        return EntryGateResult(ok=False, reasons=(f"l1-ready.json is invalid: {exc}",))

    if gate.gate_name != _L1_GATE_NAME:
        reasons.append(f"gate_name is {gate.gate_name!r}, expected {_L1_GATE_NAME!r}")

    # Status PASS + required-check completeness + evidence hashes (integrity).
    promotion = require_gate_pass(gate, base_dir=request.base_dir)
    if not promotion.ok:
        reasons.extend(promotion.reasons)
    if gate.status is not GateStatus.PASS:
        reasons.append(f"status is {gate.status.value}, not PASS")

    required = required_checks_for(_L1_GATE_NAME)
    missing_checks = sorted(required - set(gate.mandatory_checks))
    if missing_checks:
        reasons.append(f"missing required L1-READY checks: {missing_checks}")

    ih = gate.input_hashes
    if ih.get("layer1_schema_hash") != request.expected_layer1_schema_hash:
        reasons.append("Layer 1 schema hash mismatch")
    if ih.get("profiler_config_hash") != request.expected_profiler_config_hash:
        reasons.append("profiler configuration hash mismatch")
    if ih.get("profiler_version") != request.expected_profiler_version:
        reasons.append("profiler version mismatch")

    # Qualification report existence + hash.
    report_path = Path(request.qualification_report_path)
    if not report_path.exists():
        reasons.append("qualification report is missing")
    else:
        actual = sha256_file(report_path)
        if ih.get("qualification_report_hash") != actual:
            reasons.append("qualification report hash mismatch")

    # Qualified source compatibility (when expected values are supplied).
    if (
        request.expected_qualified_source_git_sha is not None
        and gate.qualified_source_git_sha != request.expected_qualified_source_git_sha
    ):
        reasons.append("qualified source commit mismatch")
    if (
        request.expected_qualified_source_tree_sha is not None
        and gate.qualified_source_tree_sha != request.expected_qualified_source_tree_sha
    ):
        reasons.append("qualified source tree mismatch")

    if not gate.evidence:
        reasons.append("l1-ready has no mandatory evidence")

    return EntryGateResult(ok=not reasons, reasons=tuple(dict.fromkeys(reasons)))


def require_l1_ready(request: EntryGateRequest) -> None:
    """Raise :class:`GateError` unless the L1-READY entry gate passes."""
    result = verify_l1_ready(request)
    if not result.ok:
        raise GateError("; ".join(result.reasons))
