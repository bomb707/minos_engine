"""The committed SAFE-controller qualification evidence.

These tests bind the artifact, not the run. The report records the exact source commit it was
produced from, so it is deliberately NOT re-derived at HEAD: a later commit is a different
checkout, and evidence that silently re-derived itself against whatever is checked out now would
be recording the present rather than the past.
"""

from __future__ import annotations

import copy
import hashlib
import json
from typing import Any

import pytest

from minos_engine.baseline.baseline_selected import SELECTED_CONFIG_HASH
from minos_engine.common.canonical_json import canonical_json_bytes
from minos_engine.layer2.safe_controller_qualification import (
    MANDATORY_CHECKS,
    SAFE_CONTROLLER_QUALIFICATION_PATH,
    SAFE_CONTROLLER_QUALIFICATION_SCHEMA,
    SafeControllerQualificationError,
    qualification_report_identity,
    verify_qualification_report,
)
from minos_engine.qualification.l2f_accepted_identities import repository_root

ACCEPTED_IDENTITY = "7d305bcd7c35c82389259ec1d88058ff9202ce454a0364d15e4b864345aaf821"
QUALIFIED_SOURCE_COMMIT = "9a516c1738b862aa78ce211c03891b57f5444deb"


@pytest.fixture(scope="module")
def evidence() -> dict[str, Any]:
    return dict(json.loads((repository_root() / SAFE_CONTROLLER_QUALIFICATION_PATH).read_bytes()))


def test_the_committed_evidence_verifies_and_passes(evidence: dict[str, Any]) -> None:
    report = verify_qualification_report(evidence)
    assert report["ok"] is True
    assert report["status"] == "PASS"
    assert report["capability_scope"] == "SAFE_BASELINE_ONLY"
    assert qualification_report_identity(evidence) == ACCEPTED_IDENTITY


def test_the_committed_evidence_is_canonical_bytes(evidence: dict[str, Any]) -> None:
    raw = (repository_root() / SAFE_CONTROLLER_QUALIFICATION_PATH).read_bytes()
    assert canonical_json_bytes(evidence) == raw
    assert qualification_report_identity(json.loads(raw)) == ACCEPTED_IDENTITY
    assert hashlib.sha256(raw).hexdigest() == (
        "b0a3a16d2a411d18e708ef6add63a6832778e51cfb8454874763a0f5c8604453"
    )


def test_the_evidence_names_the_source_it_was_produced_from(evidence: dict[str, Any]) -> None:
    observation = evidence["observation"]
    assert observation["source_commit"] == QUALIFIED_SOURCE_COMMIT
    assert len(observation["source_tree"]) == 40
    assert evidence["schema_version"] == SAFE_CONTROLLER_QUALIFICATION_SCHEMA


def test_the_campaign_covered_the_whole_train_corpus(evidence: dict[str, Any]) -> None:
    observation = evidence["observation"]
    assert observation["admitted_partition"] == "train"
    assert observation["skipped_partition_counts"] == {"test": 15, "validation": 10}
    assert observation["profile_count"] == 50
    assert observation["decision_count"] == 200
    assert observation["requested_mode_counts"] == {
        "BOUNDED": 50,
        "FULL_CONTEXTUAL": 50,
        "REFINEMENT": 50,
        "SAFE_BASELINE": 50,
    }
    assert observation["actual_mode_counts"] == {"SAFE_BASELINE": 200}
    assert observation["fallback_reason_counts"] == {
        "NONE": 50,
        "SAFE_BASELINE_FORCED": 150,
    }
    assert observation["selected_config_distribution"] == {SELECTED_CONFIG_HASH: 200}


def test_every_mandatory_check_passed(evidence: dict[str, Any]) -> None:
    for name in MANDATORY_CHECKS:
        assert evidence["checks"][name] is True, name
    assert evidence["status"] == "PASS"


def test_the_evidence_claims_nothing_it_must_not(evidence: dict[str, Any]) -> None:
    assert evidence["gate_issued"] is False
    assert evidence["service_activated"] is False
    assert evidence["validation_read"] is False
    assert evidence["test_accessed"] is False
    observation = evidence["observation"]
    assert observation["models_qualified_status"] == "HOLD_NO_TRAIN_PROMOTABLE_CONTEXTUAL_MODEL"
    assert observation["models_qualified_gate_present"] is False
    assert observation["model_load_count"] == 0
    assert observation["candidate_generation_count"] == 0
    assert observation["parameter_mutation_count"] == 0
    assert observation["invalid_config_count"] == 0
    assert observation["isolation_violations"] == []
    assert observation["forbidden_calls"] == []


def test_no_gate_artifact_accompanies_the_evidence() -> None:
    for name in (
        "safe-controller-frozen.json",
        "controller-frozen.json",
        "models-qualified.json",
    ):
        assert not (repository_root() / "gates" / name).exists()


def test_a_tampered_committed_report_is_refused(evidence: dict[str, Any]) -> None:
    """The artifact is not trusted for being committed."""
    forged = copy.deepcopy(evidence)
    forged["observation"]["invalid_config_count"] = 7
    with pytest.raises(SafeControllerQualificationError):
        verify_qualification_report(forged)
