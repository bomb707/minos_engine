"""Layer 2 entry-gate verification.

Layer 2 begins only after ``l1-ready.json`` verifies successfully (Layer 1 spec
§20 NEXT-STAGE LOCK). The verifier rejects:
  * a missing ``l1-ready.json``;
  * a non-PASS status;
  * a missing qualification report;
  * a mismatched Layer 1 schema hash;
  * a mismatched profiler configuration hash;
  * missing mandatory evidence.

In Stage 0 there is no ``l1-ready.json``, so this always rejects — which is the
correct, blocked state.
"""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from minos_engine.common.errors import GateError
from minos_engine.gates.contracts import GateArtifact, GateStatus

__all__ = ["EntryGateRequest", "EntryGateResult", "verify_l1_ready"]


class EntryGateRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    l1_ready_path: str
    expected_layer1_schema_hash: str
    expected_profiler_config_hash: str


class EntryGateResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    ok: bool
    reasons: tuple[str, ...] = ()


def verify_l1_ready(request: EntryGateRequest) -> EntryGateResult:
    """Return a structured pass/fail for the L1-READY entry gate."""
    reasons: list[str] = []
    path = Path(request.l1_ready_path)
    if not path.exists():
        return EntryGateResult(ok=False, reasons=("l1-ready.json is missing",))

    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        gate = GateArtifact.model_validate(raw)
    except Exception as exc:  # noqa: BLE001 - report any parse/validation failure
        return EntryGateResult(ok=False, reasons=(f"l1-ready.json is invalid: {exc}",))

    if gate.status is not GateStatus.PASS:
        reasons.append(f"l1-ready status is {gate.status.value}, not PASS")
    if not gate.evidence:
        reasons.append("l1-ready has no mandatory evidence")

    report_hash = gate.input_hashes.get("qualification_report_hash")
    if not report_hash:
        reasons.append("l1-ready is missing the qualification report hash")

    schema_hash = gate.input_hashes.get("layer1_schema_hash")
    if schema_hash != request.expected_layer1_schema_hash:
        reasons.append("Layer 1 schema hash mismatch")

    profiler_hash = gate.input_hashes.get("profiler_config_hash")
    if profiler_hash != request.expected_profiler_config_hash:
        reasons.append("profiler configuration hash mismatch")

    return EntryGateResult(ok=not reasons, reasons=tuple(reasons))


def require_l1_ready(request: EntryGateRequest) -> None:
    """Raise :class:`GateError` unless the L1-READY entry gate passes."""
    result = verify_l1_ready(request)
    if not result.ok:
        raise GateError("; ".join(result.reasons))
