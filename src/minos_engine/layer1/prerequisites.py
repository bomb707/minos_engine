"""Accepted Stage 1 TWIN-READY prerequisite verification for Layer 1.

Layer 1 may run only when the externally-accepted TWIN-READY gate verifies. This
pins the accepted identity, rehashes the gate's evidence from its qualified
commit (git-bound), and returns three separate results — never one ambiguous
boolean. A locally-regenerated TWIN-READY gate can never authorize Layer 1.
Everything fails closed if the repository, commit, tree, evidence, or accepted
identity is unavailable.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, ConfigDict

from minos_engine.qualification import git_tree as G

__all__ = [
    "ACCEPTED_GATE_NAME",
    "ACCEPTED_GATE_HASH",
    "ACCEPTED_SOURCE_SHA",
    "ACCEPTED_TREE_SHA",
    "TwinPrerequisiteResult",
    "verify_twin_ready_prerequisite",
]

ACCEPTED_GATE_NAME = "TWIN-READY"
ACCEPTED_GATE_HASH = "3464fb7604fd6b18d9008bafa9e3cf1ad18d7d440d22806bbe043a11d3e9b22a"
ACCEPTED_SOURCE_SHA = "e9263ef78ce30c4d7c03497d906dd31a159f7156"
ACCEPTED_TREE_SHA = "f84c06617a4adf79cdb8305dc698eaf97c1441ed"


class TwinPrerequisiteResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    identity_accepted: bool
    evidence_verified: bool
    promotion_authorized: bool
    gate_hash: str | None = None
    reasons: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return self.identity_accepted and self.evidence_verified and self.promotion_authorized


def verify_twin_ready_prerequisite(
    root: Path, *, gate_path: Path | None = None
) -> TwinPrerequisiteResult:
    """Verify the accepted Stage 1 TWIN-READY prerequisite (fail closed)."""
    from minos_engine.gates.verifier import load_gate, require_gate_pass, verify_gate_integrity

    root = root.resolve()
    path = gate_path or (root / "gates" / "twin-ready.json")
    reasons: list[str] = []

    if not path.exists():
        return TwinPrerequisiteResult(
            identity_accepted=False,
            evidence_verified=False,
            promotion_authorized=False,
            reasons=("twin-ready.json is missing",),
        )
    try:
        gate = load_gate(path)
    except Exception as exc:  # noqa: BLE001 - any load/schema/hash failure fails closed
        return TwinPrerequisiteResult(
            identity_accepted=False,
            evidence_verified=False,
            promotion_authorized=False,
            reasons=(f"twin-ready.json invalid: {exc}",),
        )

    if gate.gate_name != ACCEPTED_GATE_NAME:
        reasons.append(f"gate_name {gate.gate_name!r} != {ACCEPTED_GATE_NAME!r}")
    if gate.gate_hash != ACCEPTED_GATE_HASH:
        reasons.append("gate_hash is not the accepted TWIN-READY hash")
    if gate.qualified_source_git_sha != ACCEPTED_SOURCE_SHA:
        reasons.append("qualified_source_git_sha is not accepted")
    if gate.qualified_source_tree_sha != ACCEPTED_TREE_SHA:
        reasons.append("qualified_source_tree_sha is not accepted")

    git_ok = G.is_git_repo(root)
    if not git_ok:
        reasons.append("not a git repository")
    else:
        if not G.object_exists(root, ACCEPTED_SOURCE_SHA):
            reasons.append("accepted qualified commit is not present locally")
        elif G.commit_tree_sha(root, ACCEPTED_SOURCE_SHA) != ACCEPTED_TREE_SHA:
            reasons.append("accepted qualified commit's tree does not match the accepted tree")
        if not G.object_exists(root, ACCEPTED_TREE_SHA):
            reasons.append("accepted qualified tree is not present locally")

    identity_accepted = not reasons

    integrity = verify_gate_integrity(gate, base_dir=root)
    evidence_verified = integrity.ok and git_ok
    if not integrity.ok:
        reasons.extend(f"evidence: {r}" for r in integrity.reasons)

    promotion = require_gate_pass(gate, base_dir=root)
    promotion_authorized = promotion.ok
    if not promotion.ok:
        reasons.extend(f"promotion: {r}" for r in promotion.reasons)

    return TwinPrerequisiteResult(
        identity_accepted=identity_accepted,
        evidence_verified=evidence_verified,
        promotion_authorized=promotion_authorized,
        gate_hash=gate.gate_hash if identity_accepted else None,
        reasons=tuple(dict.fromkeys(reasons)),
    )
