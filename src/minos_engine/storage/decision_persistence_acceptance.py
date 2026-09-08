"""The accepted production persistence authority, pinned from outside what it authenticates.

A qualification cannot vouch for itself, and neither can a migration. ``verify_persistence_report``
re-derives every check from the observation, but the values describing *which* campaign this is --
its identity, its bytes, the checkout that ran it -- were only ever read from the document. A
different document with a different observation verifies just as happily.

So the accepted identities live here, in a module committed strictly **after** the evidence it
pins. That is the repository's established non-circular pattern:
``layer2/safe_controller_frozen_acceptance.py`` pins the frozen controller gate the same way, and
``models/contract.py`` pins an earlier gate before that.

:func:`verify_accepted_persistence_authority` is what a future ``Layer2Service`` calls. It is
production code with no test-only dependency: it reads the committed evidence, runs the
qualification verifier, and then requires the artifact to be *the accepted one* -- exact bytes,
exact identity, exact qualified source proved against git, exact overlay head and contract hash,
exact runtime persistence contract, and the accepted SAFE-CONTROLLER-FROZEN gate underneath it.

It deliberately does **not** say the service may start. Two activation prerequisites remain and
are named in :data:`REMAINING_ACTIVATION_PREREQUISITES` so no caller can mistake this for
readiness.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Final

from minos_engine.common.errors import MinosEngineError

__all__ = [
    "ACCEPTED_GATE_HASH",
    "ACCEPTED_OVERLAY_HEAD_CONTRACT_HASH",
    "ACCEPTED_OVERLAY_HEAD_REVISION",
    "ACCEPTED_QUALIFICATION_FILE_SHA256",
    "ACCEPTED_QUALIFICATION_IDENTITY",
    "ACCEPTED_SOURCE_COMMIT",
    "ACCEPTED_SOURCE_TREE",
    "PERSISTENCE_ACCEPTANCE_SCHEMA",
    "REMAINING_ACTIVATION_PREREQUISITES",
    "SUPERSEDED_QUALIFICATIONS",
    "PersistenceAcceptanceError",
    "accepted_persistence_identities",
    "verify_accepted_persistence_authority",
]

PERSISTENCE_ACCEPTANCE_SCHEMA: Final = "l2h-decision-persistence-acceptance-v1"

#: The accepted qualification, by identity AND by bytes.
ACCEPTED_QUALIFICATION_IDENTITY: Final = (
    "7bfd8f9f816a387653125d6be820b8cf05ccf017498009bc7e706d29fd3337b3"
)
ACCEPTED_QUALIFICATION_FILE_SHA256: Final = (
    "1e54930d571befc9543c351fcd7cf5dbd93f3ef1ac335da716cc615e3970cf6a"
)

#: The checkout that ran the corrected campaign. Pinned here rather than trusted from the report.
ACCEPTED_SOURCE_COMMIT: Final = "c05aad9ca160f6fa975c5230e175385132362656"
ACCEPTED_SOURCE_TREE: Final = "5ffe3d765b2fc8aaa4a850beb8399bfc5d34266d"

#: The operational schema this authority is about.
ACCEPTED_MAIN_REVISION: Final = "0005_l2e_feature_view"
ACCEPTED_OVERLAY_HEAD_REVISION: Final = "r0002_l2h_live_profile_lookup"
ACCEPTED_OVERLAY_BASE_REVISION: Final = "r0001_l2h_runtime_decisions"
ACCEPTED_OVERLAY_HEAD_CONTRACT_HASH: Final = (
    "2af0f847037039c413c3e3b277839cacd4c10063869f7d1210aa9715c687d120"
)
ACCEPTED_OVERLAY_BASE_CONTRACT_HASH: Final = (
    "4265fe13583344ebf0f6d1404a9a2fc0e4556096122e060442e5aaf87f8fef25"
)

#: The controller whose decisions this store is authorised to hold.
ACCEPTED_GATE_HASH: Final = "504e701fe77b651c919ebc015dc6911bbca880613014058979b18ab408f88add"

#: The narrow live lookup surface, named here so a store that lost it is refused.
ACCEPTED_LIVE_LOOKUP_SURFACE: Final = "runtime.l2h_resolve_owned_profile"

#: Qualifications replaced before service activation and never accepted for promotion.
SUPERSEDED_QUALIFICATIONS: Final[tuple[dict[str, str], ...]] = (
    {
        "schema": "l2h-decision-persistence-qualification-v1",
        "identity": "e88f6cf83063905e1608c9583185b30d09f9943e3abfa92a0508858f8d617f20",
        "file_sha256": "9bb98d5106f239e596715d79e91c8dee2055b2cc3f2b2a860eb625b2b5400775",
        "reason": (
            "it derived least privilege from observed query behaviour rather than from database "
            "capability, and so certified r0001's raw SELECT grants on profiling.bam_profiles "
            "and catalog.dataset_registry"
        ),
        "status": "SUPERSEDED_BEFORE_SERVICE_ACTIVATION_NEVER_ACCEPTED_FOR_PROMOTION",
    },
)

#: Accepted persistence is NOT readiness. These are still open, and naming them here means a
#: caller cannot read acceptance as permission to start.
REMAINING_ACTIVATION_PREREQUISITES: Final[tuple[dict[str, str], ...]] = (
    {
        "name": "SAFE_CONFIG_ROW_NOT_PROVISIONED",
        "detail": (
            "catalog.gatk_configs is empty in the operational store, so the live path fails "
            "closed. provision_safe_config_row is the administrative step that binds it and is "
            "separately authorised"
        ),
    },
    {
        "name": "NO_OWNERSHIP_AUTHORITY_FOR_A_NEW_LIVE_ROUND",
        "detail": (
            "l2h-round-profile-ownership-v2 is TRAIN-only: the corpus is the frozen fifty-row "
            "TRAIN schedule, and a round outside it cannot obtain a VerifiedRoundProfileAuthority "
            "at all. Supporting a live round will require changing and requalifying the frozen "
            "controller source, and very likely a decision-manifest v2 and a further overlay "
            "revision with it"
        ),
    },
)


class PersistenceAcceptanceError(MinosEngineError):
    """The persistence evidence on disk is not the accepted authority."""


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise PersistenceAcceptanceError(message)


def accepted_persistence_identities() -> dict[str, Any]:
    """Everything this module accepts, as data, so a reader need not read the code."""
    return {
        "schema_version": PERSISTENCE_ACCEPTANCE_SCHEMA,
        "qualification_identity": ACCEPTED_QUALIFICATION_IDENTITY,
        "qualification_file_sha256": ACCEPTED_QUALIFICATION_FILE_SHA256,
        "source_commit": ACCEPTED_SOURCE_COMMIT,
        "source_tree": ACCEPTED_SOURCE_TREE,
        "main_revision": ACCEPTED_MAIN_REVISION,
        "overlay_base_revision": ACCEPTED_OVERLAY_BASE_REVISION,
        "overlay_head_revision": ACCEPTED_OVERLAY_HEAD_REVISION,
        "overlay_base_contract_hash": ACCEPTED_OVERLAY_BASE_CONTRACT_HASH,
        "overlay_head_contract_hash": ACCEPTED_OVERLAY_HEAD_CONTRACT_HASH,
        "live_lookup_surface": ACCEPTED_LIVE_LOOKUP_SURFACE,
        "safe_controller_frozen_gate_hash": ACCEPTED_GATE_HASH,
        "superseded_qualifications": [dict(sorted(e.items())) for e in SUPERSEDED_QUALIFICATIONS],
        "remaining_activation_prerequisites": [
            dict(sorted(e.items())) for e in REMAINING_ACTIVATION_PREREQUISITES
        ],
        "service_activation_authorised": False,
    }


def verify_accepted_persistence_authority(root: Any = None) -> dict[str, Any]:
    """THE final authority on production decision persistence. Callable by the live service."""
    from minos_engine.layer2.safe_controller_frozen_acceptance import (
        SafeControllerFrozenAcceptanceError,
        verify_accepted_safe_controller_frozen_gate,
    )
    from minos_engine.layer2.safe_controller_policy import _resolve_root
    from minos_engine.qualification.git_tree import commit_tree_sha, is_commit
    from minos_engine.storage.decision_persistence_qualification import (
        DECISION_PERSISTENCE_QUALIFICATION_PATH,
        MANDATORY_CHECKS,
        PERSISTENCE_QUALIFICATION_SCHEMA,
        DecisionPersistenceQualificationError,
        persistence_report_identity,
        verify_persistence_report,
    )
    from minos_engine.storage.runtime_decision_contract import (
        LIVE_PROFILE_RESOLVER,
        REQUIRED_MAIN_REVISION,
        RUNTIME_OVERLAY_HEAD_REVISION,
        runtime_overlay_contract_hash,
        runtime_overlay_head_contract_hash,
    )

    base = Path(_resolve_root(root))

    # 1. the controller whose decisions this store may hold
    try:
        gate = verify_accepted_safe_controller_frozen_gate(base)
    except SafeControllerFrozenAcceptanceError as error:
        raise PersistenceAcceptanceError(
            f"persistence is not accepted without the accepted frozen controller: {error}"
        ) from None
    _require(
        gate["gate_hash"] == ACCEPTED_GATE_HASH,
        f"the frozen controller gate is {gate['gate_hash']}, not the accepted {ACCEPTED_GATE_HASH}",
    )

    # 2. the evidence, by bytes and by identity
    path = base / DECISION_PERSISTENCE_QUALIFICATION_PATH
    _require(path.is_file(), f"the persistence qualification is missing: {path}")
    _require(not path.is_symlink(), f"{path} is a symlink")
    raw = path.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    _require(
        digest == ACCEPTED_QUALIFICATION_FILE_SHA256,
        f"the qualification hashes to {digest}, not the accepted "
        f"{ACCEPTED_QUALIFICATION_FILE_SHA256}",
    )
    report = json.loads(raw)
    _require(
        report.get("schema_version") == PERSISTENCE_QUALIFICATION_SCHEMA,
        f"the qualification is {report.get('schema_version')!r}, not "
        f"{PERSISTENCE_QUALIFICATION_SCHEMA}",
    )
    identity = persistence_report_identity(report)
    _require(
        identity == ACCEPTED_QUALIFICATION_IDENTITY,
        f"the qualification identity is {identity}, not the accepted "
        f"{ACCEPTED_QUALIFICATION_IDENTITY}",
    )
    for entry in SUPERSEDED_QUALIFICATIONS:
        _require(
            identity != entry["identity"],
            f"this is the superseded qualification {entry['identity']}, which was never accepted",
        )

    # 3. it verifies on its own terms
    try:
        verified = verify_persistence_report(report, root=base)
    except DecisionPersistenceQualificationError as error:
        raise PersistenceAcceptanceError(
            f"the accepted qualification does not verify: {error}"
        ) from None
    _require(verified["status"] == "PASS", "the accepted qualification must be PASS")
    _require(
        verified["check_count"] == len(MANDATORY_CHECKS),
        "the qualification does not carry exactly the registered checks",
    )

    # 4. the checkout that ran it, PROVED against git rather than read from the document
    _require(
        verified["execution_source_commit"] == ACCEPTED_SOURCE_COMMIT
        and verified["execution_source_tree"] == ACCEPTED_SOURCE_TREE,
        "the qualification names a source other than the accepted one",
    )
    _require(
        is_commit(base, ACCEPTED_SOURCE_COMMIT),
        f"the accepted source {ACCEPTED_SOURCE_COMMIT} is not a commit in this repository",
    )
    actual_tree = str(commit_tree_sha(base, ACCEPTED_SOURCE_COMMIT) or "")
    _require(
        actual_tree == ACCEPTED_SOURCE_TREE,
        f"the accepted source has tree {actual_tree}, not {ACCEPTED_SOURCE_TREE}",
    )

    # 5. the persistence contract the store must actually be at
    schema = report["observation"]["persistence_schema"]
    _require(
        schema.get("requires_main_revision") == ACCEPTED_MAIN_REVISION == REQUIRED_MAIN_REVISION,
        "the qualification does not require the accepted operational schema",
    )
    _require(
        schema.get("overlay_head_revision")
        == ACCEPTED_OVERLAY_HEAD_REVISION
        == RUNTIME_OVERLAY_HEAD_REVISION,
        "the qualification does not name the accepted overlay head",
    )
    _require(
        schema.get("overlay_head_contract_hash")
        == ACCEPTED_OVERLAY_HEAD_CONTRACT_HASH
        == runtime_overlay_head_contract_hash(base),
        "the overlay lineage's contract hash is not the accepted one",
    )
    _require(
        schema.get("overlay_base_contract_hash")
        == ACCEPTED_OVERLAY_BASE_CONTRACT_HASH
        == runtime_overlay_contract_hash(base),
        "r0001's contract hash has moved; its bytes must never change",
    )
    _require(
        LIVE_PROFILE_RESOLVER == ACCEPTED_LIVE_LOOKUP_SURFACE
        and schema.get("live_lookup_surface", "").startswith(ACCEPTED_LIVE_LOOKUP_SURFACE),
        "the qualification does not name the accepted live lookup surface",
    )

    # 6. the corrective's own guarantee, restated from the accepted observation
    capabilities = report["observation"]["capabilities"]
    _require(
        capabilities.get("campaign_connection_role") == "minos_live",
        "the accepted campaign did not run under the live role",
    )
    _require(
        capabilities.get("partition_bearing_relations_readable_by_live") == [],
        "the accepted campaign left the live role able to read a partition-bearing relation",
    )

    _require(report.get("gate_issued") is False, "this authority issues no gate")
    _require(report.get("service_activated") is False, "the public service is not activated")

    return {
        "ok": True,
        "schema_version": PERSISTENCE_ACCEPTANCE_SCHEMA,
        "qualification_identity": identity,
        "qualification_file_sha256": digest,
        "source_commit": ACCEPTED_SOURCE_COMMIT,
        "source_tree": actual_tree,
        "main_revision": ACCEPTED_MAIN_REVISION,
        "overlay_head_revision": ACCEPTED_OVERLAY_HEAD_REVISION,
        "overlay_head_contract_hash": ACCEPTED_OVERLAY_HEAD_CONTRACT_HASH,
        "live_lookup_surface": ACCEPTED_LIVE_LOOKUP_SURFACE,
        "safe_controller_frozen_gate_hash": gate["gate_hash"],
        "decision_count": verified["decision_count"],
        "check_count": verified["check_count"],
        "superseded_qualifications": [e["identity"] for e in SUPERSEDED_QUALIFICATIONS],
        "remaining_activation_prerequisites": [
            e["name"] for e in REMAINING_ACTIVATION_PREREQUISITES
        ],
        "service_activation_authorised": False,
    }
