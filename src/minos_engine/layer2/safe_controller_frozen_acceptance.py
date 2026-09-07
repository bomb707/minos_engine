"""The accepted SAFE-CONTROLLER-FROZEN gate, pinned from outside the gate it authenticates.

A gate cannot vouch for its own provenance. `verify_safe_controller_frozen_gate` re-authenticates
the science and re-derives every check, but the values describing *who issued the artifact* --
`engine_git_sha`, and the gate's own hash and bytes -- were only ever read from the gate. A forged
gate could name a different issuer, recompute `gate_hash`, and pass with the science untouched.

So the accepted identities live here, in a module committed strictly **after** the artifact it
pins. That is the repository's established non-circular pattern: `BASELINE_QUALIFIED_GATE_HASH` in
``models/contract.py`` pins an earlier gate the same way. Hardcoding the issuer commit into the
very commit it is supposed to identify would be self-referential and would prove nothing, which is
why the corrective source, the reissued gate and this acceptance are three separate commits.

`verify_accepted_safe_controller_frozen_gate` is the final authority. It runs the structural
verifier first and then requires the artifact to be *the accepted one*: exact bytes, exact gate
hash, exact issuer commit and tree (proved against git, not length-checked), exact qualified
controller source, exact qualification identity, and the three semantic bindings re-derived.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Final

from minos_engine.common.errors import MinosEngineError

__all__ = [
    "ACCEPTED_GATE_FILE_SHA256",
    "ACCEPTED_GATE_HASH",
    "ACCEPTED_ISSUER_SOURCE_COMMIT",
    "ACCEPTED_ISSUER_SOURCE_TREE",
    "ACCEPTED_QUALIFIED_SOURCE_COMMIT",
    "ACCEPTED_QUALIFIED_SOURCE_TREE",
    "SUPERSEDED_ISSUANCES",
    "SafeControllerFrozenAcceptanceError",
    "accepted_gate_identities",
    "verify_accepted_safe_controller_frozen_gate",
]

ACCEPTANCE_SCHEMA: Final = "l2h-safe-controller-frozen-acceptance-v1"

#: The accepted gate, by identity AND by bytes.
ACCEPTED_GATE_HASH: Final = "504e701fe77b651c919ebc015dc6911bbca880613014058979b18ab408f88add"
ACCEPTED_GATE_FILE_SHA256: Final = (
    "1bbc1b1b65b7759922d8501a256539850b5b5f95eaf1862a2cef22b9bf39e716"
)

#: The checkout that ISSUED the accepted gate. Pinned here rather than trusted from the gate.
ACCEPTED_ISSUER_SOURCE_COMMIT: Final = "6a3bedfa33581fde22e96d6739146c087c23ba79"
ACCEPTED_ISSUER_SOURCE_TREE: Final = "96e46249ff1dc39344127b9a8cb4daec07e0ab8d"

#: The controller source that actually underwent the 200-decision qualification. A different
#: thing from the issuer, and never substituted for it.
ACCEPTED_QUALIFIED_SOURCE_COMMIT: Final = "7d064fe8bc7185bd5c07d16f1fec9dfdd21970b0"
ACCEPTED_QUALIFIED_SOURCE_TREE: Final = "799cd5cc493bd79f5ad6e5ecc78033eada5a9f5b"

ACCEPTED_QUALIFICATION_IDENTITY: Final = (
    "a0e8840dbdad6beeca7e7548b500868a860438dbea5673b08b11dae70832ba8e"
)
ACCEPTED_QUALIFICATION_FILE_SHA256: Final = (
    "90fb7b5f819eee2f414d8b55adfa7bbbd8164eadd874d3354393bb8f093ec7da"
)

#: Issuances that were replaced before service activation and never accepted for promotion.
#: Recorded so the superseded identity cannot quietly be reclaimed.
SUPERSEDED_ISSUANCES: Final[tuple[dict[str, str], ...]] = (
    {
        "gate_hash": "3c6d9b0b6f84ed017d577da77f39d87b19bc633e56a66f8c599f1a5f8cfc07ae",
        "issuer_source_commit": "9d8864a2a5b906bed0e5989beff827b33bb568fa",
        "reason": (
            "its verifier length-checked engine_git_sha instead of proving it, and it bound the "
            "capability scope, contextual-model status and publication disposition as prose "
            "rather than as identities"
        ),
        "status": "SUPERSEDED_BEFORE_SERVICE_ACTIVATION_NEVER_ACCEPTED_FOR_PROMOTION",
    },
)


class SafeControllerFrozenAcceptanceError(MinosEngineError):
    """The gate on disk is not the accepted SAFE-CONTROLLER-FROZEN artifact."""


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise SafeControllerFrozenAcceptanceError(message)


def accepted_gate_identities() -> dict[str, Any]:
    """Everything this module accepts, as data, so a reader need not read the code."""
    return {
        "schema_version": ACCEPTANCE_SCHEMA,
        "gate_name": "SAFE-CONTROLLER-FROZEN",
        "gate_hash": ACCEPTED_GATE_HASH,
        "gate_file_sha256": ACCEPTED_GATE_FILE_SHA256,
        "issuer_source_commit": ACCEPTED_ISSUER_SOURCE_COMMIT,
        "issuer_source_tree": ACCEPTED_ISSUER_SOURCE_TREE,
        "qualified_source_commit": ACCEPTED_QUALIFIED_SOURCE_COMMIT,
        "qualified_source_tree": ACCEPTED_QUALIFIED_SOURCE_TREE,
        "qualification_identity": ACCEPTED_QUALIFICATION_IDENTITY,
        "qualification_file_sha256": ACCEPTED_QUALIFICATION_FILE_SHA256,
        "superseded_issuances": [dict(sorted(e.items())) for e in SUPERSEDED_ISSUANCES],
    }


def verify_accepted_safe_controller_frozen_gate(root: Any = None) -> dict[str, Any]:
    """THE final authority. Structural verification, then acceptance against pinned identities."""
    from minos_engine.gates.required_checks import required_checks_for
    from minos_engine.gates.verifier import load_gate
    from minos_engine.layer2.safe_controller_gate import (
        CAPABILITY_SCOPE,
        SAFE_CONTROLLER_FROZEN_GATE,
        SAFE_CONTROLLER_FROZEN_GATE_PATH,
        SafeControllerGateError,
        capability_scope_hash,
        contextual_model_status_hash,
        publication_disposition_hash,
        verify_issuer_provenance,
        verify_safe_controller_frozen_gate,
    )
    from minos_engine.layer2.safe_controller_policy import _resolve_root

    base = _resolve_root(root)

    # 1-6, 7-9, 11, 15-23: the structural verifier re-authenticates the evidence and re-derives
    # every check from it. Acceptance adds what a gate cannot say about itself.
    try:
        structural = verify_safe_controller_frozen_gate(base)
    except SafeControllerGateError as error:
        raise SafeControllerFrozenAcceptanceError(
            f"the gate does not verify structurally: {error}"
        ) from None
    _require(bool(structural["ok"]), "the gate did not verify structurally")

    path = base / SAFE_CONTROLLER_FROZEN_GATE_PATH
    raw = path.read_bytes()
    actual_file_sha = hashlib.sha256(raw).hexdigest()
    _require(
        actual_file_sha == ACCEPTED_GATE_FILE_SHA256,
        f"the gate file hashes to {actual_file_sha}, not the accepted {ACCEPTED_GATE_FILE_SHA256}",
    )
    gate = load_gate(path)
    _require(
        gate.gate_name == SAFE_CONTROLLER_FROZEN_GATE,
        f"the gate is named {gate.gate_name}, not {SAFE_CONTROLLER_FROZEN_GATE}",
    )
    _require(gate.status.value == "PASS", "the accepted gate must be PASS")
    _require(
        gate.gate_hash == gate.compute_hash() == ACCEPTED_GATE_HASH,
        f"the gate hash is {gate.gate_hash}, not the accepted {ACCEPTED_GATE_HASH}",
    )
    for entry in SUPERSEDED_ISSUANCES:
        _require(
            gate.gate_hash != entry["gate_hash"],
            f"this is the superseded issuance {entry['gate_hash']}, which was never accepted",
        )

    # 10: the issuer, PROVED. This is what the gate could not say about itself.
    _require(
        gate.engine_git_sha == ACCEPTED_ISSUER_SOURCE_COMMIT,
        f"the gate names issuer {gate.engine_git_sha}, not the accepted "
        f"{ACCEPTED_ISSUER_SOURCE_COMMIT}",
    )
    try:
        provenance = verify_issuer_provenance(
            base,
            commit=ACCEPTED_ISSUER_SOURCE_COMMIT,
            tree=ACCEPTED_ISSUER_SOURCE_TREE,
        )
    except SafeControllerGateError as error:
        raise SafeControllerFrozenAcceptanceError(
            f"the accepted issuer provenance does not hold: {error}"
        ) from None
    _require(
        gate.engine_git_sha != ACCEPTED_QUALIFIED_SOURCE_COMMIT,
        "the issuer and the qualified controller source must not be the same commit",
    )

    # 9: the qualified controller source, unchanged and distinct from the issuer
    _require(
        gate.qualified_source_git_sha == ACCEPTED_QUALIFIED_SOURCE_COMMIT
        and gate.qualified_source_tree_sha == ACCEPTED_QUALIFIED_SOURCE_TREE,
        "the gate does not name the accepted qualified controller source",
    )

    # 5-6: exactly the registered checks, all true
    required = required_checks_for(SAFE_CONTROLLER_FROZEN_GATE)
    _require(
        set(gate.mandatory_checks) == set(required) and all(gate.mandatory_checks.values()),
        "the gate does not carry exactly the registered checks, all true",
    )

    # 7-8, 12-14: the pinned qualification and the three semantic bindings, re-derived
    bound = dict(gate.input_hashes)
    for field, expected in (
        ("qualification_identity", ACCEPTED_QUALIFICATION_IDENTITY),
        ("qualification_file_sha256", ACCEPTED_QUALIFICATION_FILE_SHA256),
        ("qualification_source_commit", ACCEPTED_QUALIFIED_SOURCE_COMMIT),
        ("qualification_source_tree", ACCEPTED_QUALIFIED_SOURCE_TREE),
        ("capability_scope_hash", capability_scope_hash()),
        ("contextual_model_status_hash", contextual_model_status_hash()),
        ("publication_disposition_hash", publication_disposition_hash()),
    ):
        _require(
            bound.get(field) == expected,
            f"the gate's {field} is {bound.get(field)!r}, expected {expected}",
        )

    return {
        "ok": True,
        "schema_version": ACCEPTANCE_SCHEMA,
        "gate_name": SAFE_CONTROLLER_FROZEN_GATE,
        "gate_hash": gate.gate_hash,
        "gate_file_sha256": actual_file_sha,
        "capability_scope": CAPABILITY_SCOPE,
        "not_authorized": list(structural["not_authorized"]),
        "issuer_source_commit": provenance["issuer_source_commit"],
        "issuer_source_tree": provenance["issuer_source_tree"],
        "qualified_source_git_sha": gate.qualified_source_git_sha,
        "qualified_source_tree_sha": gate.qualified_source_tree_sha,
        "qualification_identity": ACCEPTED_QUALIFICATION_IDENTITY,
        "check_count": len(gate.mandatory_checks),
        "superseded_issuances": [entry["gate_hash"] for entry in SUPERSEDED_ISSUANCES],
    }


def _accepted_gate_document(root: Any = None) -> dict[str, Any]:
    """The accepted gate as data, after acceptance has passed."""
    from minos_engine.layer2.safe_controller_gate import SAFE_CONTROLLER_FROZEN_GATE_PATH
    from minos_engine.layer2.safe_controller_policy import _resolve_root

    verify_accepted_safe_controller_frozen_gate(root)
    base = _resolve_root(root)
    return dict(json.loads((base / SAFE_CONTROLLER_FROZEN_GATE_PATH).read_bytes()))
