"""Load, verify, and write stage-gate artifacts.

Verification re-parses the artifact through the :class:`GateArtifact` contract
(which recomputes and checks ``gate_hash`` and enforces the PASS invariant) and
validates it against the JSON Schema.
"""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from minos_engine.common.errors import GateError
from minos_engine.schema_registry import validate_against

from .contracts import GateArtifact, GateStatus

__all__ = ["GateVerification", "verify_gate_file", "load_gate", "write_gate"]


class GateVerification(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    ok: bool
    gate_name: str
    status: GateStatus
    gate_hash: str
    reasons: tuple[str, ...] = ()


def load_gate(path: str | Path) -> GateArtifact:
    p = Path(path)
    if not p.exists():
        raise GateError(f"gate artifact not found: {p}")
    raw = json.loads(p.read_text(encoding="utf-8"))
    validate_against("gate-artifact-v1", raw)
    return GateArtifact.model_validate(raw)


def verify_gate_file(path: str | Path) -> GateVerification:
    """Verify a gate file. A parse/hash/PASS-invariant failure is reported, not raised."""
    reasons: list[str] = []
    try:
        gate = load_gate(path)
    except Exception as exc:  # noqa: BLE001 - report any failure as a reason
        return GateVerification(
            ok=False,
            gate_name="<unloadable>",
            status=GateStatus.REJECT,
            gate_hash="",
            reasons=(str(exc),),
        )

    if gate.gate_hash != gate.compute_hash():
        reasons.append("gate_hash does not match canonical content")
    if gate.status is GateStatus.PASS:
        failing = [k for k, ok in gate.mandatory_checks.items() if not ok]
        if failing:
            reasons.append(f"PASS gate has failing checks: {failing}")

    return GateVerification(
        ok=not reasons,
        gate_name=gate.gate_name,
        status=gate.status,
        gate_hash=gate.gate_hash,
        reasons=tuple(reasons),
    )


def write_gate(gate: GateArtifact, path: str | Path) -> Path:
    """Write a gate artifact as pretty JSON (schema-validated first)."""
    payload = gate.model_dump(mode="json")
    validate_against("gate-artifact-v1", payload)
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return p
