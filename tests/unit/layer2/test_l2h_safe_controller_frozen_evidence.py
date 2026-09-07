"""The issued SAFE-CONTROLLER-FROZEN gate artifact.

This binds the gate that was actually written: its hash, its bindings, the source it names as
qualified, and the capability it does not confer. It does not re-issue the gate — an evidence test
that regenerated its own subject would be checking the generator, not the artifact.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

import pytest

from minos_engine.gates.required_checks import required_checks_for
from minos_engine.layer2.safe_controller_gate import (
    ACCEPTED_V3_FILE_SHA256,
    ACCEPTED_V3_IDENTITY,
    ACCEPTED_V3_SOURCE_COMMIT,
    ACCEPTED_V3_SOURCE_TREE,
    CAPABILITY_SCOPE,
    SAFE_CONTROLLER_FROZEN_GATE,
    SAFE_CONTROLLER_FROZEN_GATE_PATH,
    verify_safe_controller_frozen_gate,
)
from minos_engine.qualification.l2f_accepted_identities import repository_root

GATE_HASH = "504e701fe77b651c919ebc015dc6911bbca880613014058979b18ab408f88add"
GATE_FILE_SHA = "1bbc1b1b65b7759922d8501a256539850b5b5f95eaf1862a2cef22b9bf39e716"
ISSUING_ENGINE_COMMIT = "6a3bedfa33581fde22e96d6739146c087c23ba79"
ISSUING_ENGINE_TREE = "96e46249ff1dc39344127b9a8cb4daec07e0ab8d"
#: The first issuance, superseded before activation and never accepted for promotion.
SUPERSEDED_GATE_HASH = "3c6d9b0b6f84ed017d577da77f39d87b19bc633e56a66f8c599f1a5f8cfc07ae"
SAFE_CONFIG = "157d88d1587c13be395c62d60e27d1becdada78fad45e65d883bc1190e51acea"


pytestmark = pytest.mark.skipif(
    not (repository_root() / SAFE_CONTROLLER_FROZEN_GATE_PATH).is_file(),
    reason="no gate artifact is committed yet (it is written by the evidence commit)",
)


@pytest.fixture(scope="module")
def gate() -> dict[str, Any]:
    return dict(json.loads((repository_root() / SAFE_CONTROLLER_FROZEN_GATE_PATH).read_bytes()))


def test_the_issued_gate_verifies() -> None:
    report = verify_safe_controller_frozen_gate(repository_root())
    assert report["ok"] is True
    assert report["gate_name"] == SAFE_CONTROLLER_FROZEN_GATE
    assert report["gate_hash"] == GATE_HASH
    assert report["capability_scope"] == CAPABILITY_SCOPE
    assert report["check_count"] == 29


def test_the_issued_gate_bytes_are_pinned() -> None:
    raw = (repository_root() / SAFE_CONTROLLER_FROZEN_GATE_PATH).read_bytes()
    assert hashlib.sha256(raw).hexdigest() == GATE_FILE_SHA


def test_the_gate_is_pass_and_carries_the_registered_checks(gate: dict[str, Any]) -> None:
    assert gate["gate_name"] == SAFE_CONTROLLER_FROZEN_GATE
    assert gate["status"] == "PASS"
    assert gate["gate_hash"] == GATE_HASH
    assert set(gate["mandatory_checks"]) == required_checks_for(SAFE_CONTROLLER_FROZEN_GATE)
    assert len(gate["mandatory_checks"]) == 29
    assert all(gate["mandatory_checks"].values())


def test_the_gate_separates_the_qualified_source_from_the_issuer(gate: dict[str, Any]) -> None:
    """The 200-decision qualification ran from S3; a later commit issued the gate."""
    assert gate["qualified_source_git_sha"] == ACCEPTED_V3_SOURCE_COMMIT
    assert gate["qualified_source_tree_sha"] == ACCEPTED_V3_SOURCE_TREE
    assert gate["engine_git_sha"] == ISSUING_ENGINE_COMMIT
    assert gate["engine_git_sha"] != gate["qualified_source_git_sha"]
    # and the evidence commit is not substituted for either
    assert gate["qualified_source_git_sha"] != "f83b948a832c891f36b47110f50088cfc1ba0de6"


def test_the_gate_binds_every_required_authority(gate: dict[str, Any]) -> None:
    bound = gate["input_hashes"]
    assert bound["qualification_identity"] == ACCEPTED_V3_IDENTITY
    assert bound["qualification_file_sha256"] == ACCEPTED_V3_FILE_SHA256
    assert bound["qualification_source_commit"] == ACCEPTED_V3_SOURCE_COMMIT
    assert bound["qualification_source_tree"] == ACCEPTED_V3_SOURCE_TREE
    assert bound["safe_baseline_config_hash"] == SAFE_CONFIG
    assert (
        bound["safe_controller_policy_hash"]
        == "638d634834c921f5ba00220caaca59c4b317368767c38bd50cafaad232241fa3"
    )
    assert (
        bound["baseline_selected_identity"]
        == "b13aef13fecf8e966184d03bad5ee0e6f096fb5649b30e336283e2f50f3eba38"
    )
    assert (
        bound["baseline_qualified_gate_hash"]
        == "b9436bf3263925ebe187ed5550c7214cfa92bc75a0dd2607a7766103bfa6befa"
    )
    assert (
        bound["baseline_qualification_hash"]
        == "afbcd418dee7f5521dc52b34e2c0b5d7bd31ea5f5d4ec3b1bf0768ab35babee8"
    )
    assert (
        bound["parameter_space_hash"]
        == "b2d401918084d64023305d9262baf5011a89fe517bee4e0bd33af79fb14aee2e"
    )
    assert (
        bound["l2g_v1_campaign_freeze_identity"]
        == "1c2039dec2f3fbb51a8058c947bbf8de9f9c6d235a133b5948aa6b33ac516673"
    )
    assert (
        bound["l2g_v2_campaign_freeze_identity"]
        == "42310a97f2e13d516b57789bbfa0cd6ee6e44d7e732747dd44ace3aad9d33de5"
    )
    assert (
        bound["phase_a_authority_identity"]
        == "9ad0ba48c80e7b305505fea201e93185deb15ae735338086d05b38afcf4deb3f"
    )
    assert (
        bound["train_schedule_manifest_sha256"]
        == "694a8993ef64f72ca3705442c1bc070c0288d46e08e00e39bab1155d4415d454"
    )
    assert (
        bound["registry_snapshot_hash"]
        == "3e60aa65aeed8969e29ebeef83024f6fa2285a13c155d7d6dc0c601d1e94f675"
    )
    assert (
        bound["ownership_corpus_identity"]
        == "9cc53b5d28c8a8da34c25095362c09d8cb1fb57533ff0a0b3e1fdf7000970b03"
    )


def test_the_issuing_commit_is_provable(gate: dict[str, Any]) -> None:
    """Not a length check: the commit must exist here with exactly this tree."""
    from minos_engine.layer2.safe_controller_gate import verify_issuer_provenance

    proved = verify_issuer_provenance(
        repository_root(), commit=gate["engine_git_sha"], tree=ISSUING_ENGINE_TREE
    )
    assert proved["issuer_source_commit"] == ISSUING_ENGINE_COMMIT
    assert proved["issuer_source_tree"] == ISSUING_ENGINE_TREE


def test_the_superseded_first_issuance_is_not_reused(gate: dict[str, Any]) -> None:
    """Its identity is not reclaimed and not pretended to be unchanged."""
    assert gate["gate_hash"] != SUPERSEDED_GATE_HASH
    assert gate["gate_hash"] == GATE_HASH


def test_the_gate_binds_the_three_semantic_states(gate: dict[str, Any]) -> None:
    from minos_engine.layer2.safe_controller_gate import (
        capability_scope_hash,
        contextual_model_status_hash,
        publication_disposition_hash,
    )

    bound = gate["input_hashes"]
    assert len(bound) == 19
    assert bound["capability_scope_hash"] == capability_scope_hash()
    assert bound["contextual_model_status_hash"] == contextual_model_status_hash()
    assert bound["publication_disposition_hash"] == publication_disposition_hash()


def test_the_gate_binds_no_operational_value(gate: dict[str, Any]) -> None:
    body = json.dumps({k: v for k, v in gate.items() if k not in {"created_at", "gate_hash"}})
    for forbidden in ("password", "postgresql://", "/home/", "/tmp/", "hostname"):
        assert forbidden not in body
    for value in gate["input_hashes"].values():
        assert len(value) in {40, 64}


def test_the_evidence_is_the_v3_report_and_not_the_gate(gate: dict[str, Any]) -> None:
    assert len(gate["evidence"]) == 1
    item = gate["evidence"][0]
    assert item["path"] == "reports/layer2/l2h-safe-controller-qualification-v3.json"
    assert item["sha256"] == ACCEPTED_V3_FILE_SHA256
    assert "safe-controller-frozen" not in item["path"]


def test_this_is_the_only_issued_controller_gate() -> None:
    gates = {p.name for p in (repository_root() / "gates").glob("*.json")}
    assert "safe-controller-frozen.json" in gates
    assert "controller-frozen.json" not in gates
    assert "models-qualified.json" not in gates


def test_the_gate_does_not_activate_the_service() -> None:
    from minos_engine.common.errors import StageNotReadyError
    from minos_engine.layer2.service import Layer2Service

    with pytest.raises(StageNotReadyError):
        Layer2Service().select_config(None)  # type: ignore[arg-type]


def test_the_persistence_disposition_is_unchanged() -> None:
    from minos_engine.layer2.decision_publication import DECISION_PUBLICATION_DISPOSITION

    assert DECISION_PUBLICATION_DISPOSITION == (
        "FILE_PUBLISHED_DB_PERSISTENCE_DEFERRED_TO_ACTIVATION"
    )
