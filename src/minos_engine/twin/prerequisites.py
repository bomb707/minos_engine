"""Accepted Stage 0 PROTOCOL-READY prerequisite verification.

Stage 1 may be authorized ONLY by the externally-accepted PROTOCOL-READY gate.
A different, locally-regenerated PROTOCOL-READY gate must never authorize Stage 1.
This verifier pins the accepted identity, rehashes Stage 0 evidence from the
qualified commit, and returns three *separate* results (never one ambiguous
boolean):

  * ``identity_accepted``     — gate name/hash/source/tree match the accepted
    values and the qualified commit + tree exist locally;
  * ``evidence_verified``     — Stage 0 evidence rehashes from the qualified
    commit (git-bound);
  * ``promotion_authorized``  — the gate is a valid PASS with its required set.

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
    "PrerequisiteResult",
    "verify_protocol_ready",
    "protocol_ready_gate_hash",
]

ACCEPTED_GATE_NAME = "PROTOCOL-READY"
ACCEPTED_GATE_HASH = "b9cda0bab329b36a0a62b4b7e9ba9b797fc22b46c1055f76db26b591311a1675"
ACCEPTED_SOURCE_SHA = "4a5a14d47f198a214e4dff8da2c4466e5fc7a425"
ACCEPTED_TREE_SHA = "f248480a5dd43c277e493155a31a9a289a2ce31e"


class PrerequisiteResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    identity_accepted: bool
    evidence_verified: bool
    promotion_authorized: bool
    gate_hash: str | None = None
    reasons: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return self.identity_accepted and self.evidence_verified and self.promotion_authorized


def verify_protocol_ready(root: Path, *, gate_path: Path | None = None) -> PrerequisiteResult:
    """Verify the accepted Stage 0 PROTOCOL-READY prerequisite (fail closed)."""
    from minos_engine.gates.verifier import load_gate, require_gate_pass, verify_gate_integrity

    root = root.resolve()
    path = gate_path or (root / "gates" / "protocol-ready.json")
    reasons: list[str] = []

    if not path.exists():
        return PrerequisiteResult(
            identity_accepted=False,
            evidence_verified=False,
            promotion_authorized=False,
            reasons=("protocol-ready.json is missing",),
        )
    try:
        gate = load_gate(path)
    except Exception as exc:  # noqa: BLE001 - any load/schema/hash failure fails closed
        return PrerequisiteResult(
            identity_accepted=False,
            evidence_verified=False,
            promotion_authorized=False,
            reasons=(f"protocol-ready.json invalid: {exc}",),
        )

    # --- accepted identity ---
    if gate.gate_name != ACCEPTED_GATE_NAME:
        reasons.append(f"gate_name {gate.gate_name!r} != {ACCEPTED_GATE_NAME!r}")
    if gate.gate_hash != ACCEPTED_GATE_HASH:
        reasons.append("gate_hash is not the accepted PROTOCOL-READY hash")
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

    # --- evidence verified (git-bound rehash from the qualified commit) ---
    integrity = verify_gate_integrity(gate, base_dir=root)
    evidence_verified = integrity.ok and git_ok
    if not integrity.ok:
        reasons.extend(f"evidence: {r}" for r in integrity.reasons)

    # --- promotion authorized ---
    promotion = require_gate_pass(gate, base_dir=root)
    promotion_authorized = promotion.ok
    if not promotion.ok:
        reasons.extend(f"promotion: {r}" for r in promotion.reasons)

    return PrerequisiteResult(
        identity_accepted=identity_accepted,
        evidence_verified=evidence_verified,
        promotion_authorized=promotion_authorized,
        gate_hash=gate.gate_hash if identity_accepted else None,
        reasons=tuple(dict.fromkeys(reasons)),
    )


def protocol_ready_gate_hash(root: Path, *, gate_path: Path | None = None) -> str | None:
    """Return the accepted gate hash iff the full prerequisite verifies, else None."""
    result = verify_protocol_ready(root, gate_path=gate_path)
    return ACCEPTED_GATE_HASH if result.ok else None
