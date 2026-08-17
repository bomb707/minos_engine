"""Load, verify, and write stage-gate artifacts.

Two explicit operations (assignment §7):

  * ``verify_gate_integrity`` — the artifact is structurally sound: schema valid,
    canonical ``gate_hash`` matches, and (when a ``base_dir`` is given) every
    evidence hash still matches the referenced file/directory bytes. This says
    nothing about promotion; a valid HOLD/REJECT passes integrity.
  * ``require_gate_pass`` — integrity holds AND ``status == PASS`` AND the
    required-check set is present and true. Only this authorizes promotion.

The legacy ``verify_gate_file`` performs *integrity* verification only.
"""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from minos_engine.common.errors import GateError
from minos_engine.qualification.evidence import hash_path
from minos_engine.schema_registry import validate_against

from .contracts import GateArtifact, GateStatus
from .required_checks import required_checks_for

__all__ = [
    "GateVerification",
    "load_gate",
    "write_gate",
    "verify_gate_integrity",
    "require_gate_pass",
    "verify_gate_file",
]


class GateVerification(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    ok: bool
    gate_name: str
    status: GateStatus
    gate_hash: str
    mode: str  # "integrity" | "promotion"
    reasons: tuple[str, ...] = ()


def load_gate(path: str | Path) -> GateArtifact:
    p = Path(path)
    if not p.exists():
        raise GateError(f"gate artifact not found: {p}")
    raw = json.loads(p.read_text(encoding="utf-8"))
    validate_against("gate-artifact-v1", raw)
    return GateArtifact.model_validate(raw)


def write_gate(gate: GateArtifact, path: str | Path) -> Path:
    payload = gate.model_dump(mode="json")
    validate_against("gate-artifact-v1", payload)
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return p


def _integrity_reasons(gate: GateArtifact, base_dir: Path | None) -> list[str]:
    reasons: list[str] = []
    if gate.gate_hash != gate.compute_hash():
        reasons.append("gate_hash does not match canonical content")
    if base_dir is not None:
        for item in gate.evidence:
            if item.sha256 is None:
                continue
            target = base_dir / item.path
            if not target.exists():
                reasons.append(f"evidence path missing: {item.path}")
                continue
            actual = hash_path(target)
            if actual != item.sha256:
                reasons.append(f"evidence hash mismatch: {item.path}")
    return reasons


def verify_gate_integrity(
    gate: GateArtifact, *, base_dir: str | Path | None = None
) -> GateVerification:
    base = Path(base_dir) if base_dir is not None else None
    reasons = _integrity_reasons(gate, base)
    return GateVerification(
        ok=not reasons,
        gate_name=gate.gate_name,
        status=gate.status,
        gate_hash=gate.gate_hash,
        mode="integrity",
        reasons=tuple(reasons),
    )


def require_gate_pass(
    gate: GateArtifact, *, base_dir: str | Path | None = None
) -> GateVerification:
    reasons = list(_integrity_reasons(gate, Path(base_dir) if base_dir is not None else None))
    if gate.status is not GateStatus.PASS:
        reasons.append(f"status is {gate.status.value}, not PASS")
    required = required_checks_for(gate.gate_name)
    if required:
        present = set(gate.mandatory_checks)
        absent = sorted(required - present)
        if absent:
            reasons.append(f"missing required checks: {absent}")
        not_true = sorted(n for n in required if gate.mandatory_checks.get(n) is not True)
        if not_true:
            reasons.append(f"required checks not true: {not_true}")
    return GateVerification(
        ok=not reasons,
        gate_name=gate.gate_name,
        status=gate.status,
        gate_hash=gate.gate_hash,
        mode="promotion",
        reasons=tuple(reasons),
    )


def verify_gate_file(path: str | Path, *, base_dir: str | Path | None = None) -> GateVerification:
    """Integrity verification of a gate file (parse failures reported, not raised)."""
    try:
        gate = load_gate(path)
    except Exception as exc:  # noqa: BLE001 - report any failure as a reason
        return GateVerification(
            ok=False,
            gate_name="<unloadable>",
            status=GateStatus.REJECT,
            gate_hash="",
            mode="integrity",
            reasons=(str(exc),),
        )
    return verify_gate_integrity(gate, base_dir=base_dir)
