"""Persistence qualification v1: historical evidence, never accepted, never edited.

v1 is a real campaign that really happened, and its file stays exactly as it was written. What it
is not is an accepted authority, because it certified a privilege surface it had only *measured*:
a SQL recorder showed the write path never scanned ``profiling.bam_profiles``, and that was
reported as least privilege. It is not. With ``r0001``'s grants in place the live role could list
every profile identity in the store, TEST included, whatever the code chose to do.

These tests pin it by bytes so nobody can quietly revise history, and assert that nothing treats
it as current.
"""

from __future__ import annotations

import hashlib
import json

import pytest
from tests.conftest import REPO_ROOT

from minos_engine.common.canonical_json import canonical_json_bytes
from minos_engine.storage.decision_persistence_qualification import (
    DECISION_PERSISTENCE_QUALIFICATION_PATH,
    HISTORICAL_V1_PATH,
    PERSISTENCE_QUALIFICATION_SCHEMA,
    SUPERSEDED_QUALIFICATIONS,
    DecisionPersistenceQualificationError,
    verify_persistence_report,
)

V1_PATH = REPO_ROOT / HISTORICAL_V1_PATH
V1_IDENTITY = "e88f6cf83063905e1608c9583185b30d09f9943e3abfa92a0508858f8d617f20"
V1_FILE_SHA = "9bb98d5106f239e596715d79e91c8dee2055b2cc3f2b2a860eb625b2b5400775"
V1_SOURCE_COMMIT = "1b67ee2f755b82526028d97aca7e8fe8db56e816"


def test_the_historical_report_is_byte_identical_to_what_was_committed():
    raw = V1_PATH.read_bytes()
    assert hashlib.sha256(raw).hexdigest() == V1_FILE_SHA
    report = json.loads(raw)
    assert canonical_json_bytes(report) == raw
    assert report["schema_version"] == "l2h-decision-persistence-qualification-v1"
    assert report["observation"]["execution_source_commit"] == V1_SOURCE_COMMIT


def test_it_is_recorded_as_superseded_with_its_reason():
    assert len(SUPERSEDED_QUALIFICATIONS) == 1
    entry = SUPERSEDED_QUALIFICATIONS[0]
    assert entry["identity"] == V1_IDENTITY
    assert entry["file_sha256"] == V1_FILE_SHA
    assert entry["schema"] == "l2h-decision-persistence-qualification-v1"
    assert entry["status"] == "SUPERSEDED_BEFORE_SERVICE_ACTIVATION_NEVER_ACCEPTED_FOR_PROMOTION"
    assert "capability" in entry["reason"]


def test_the_current_verifier_refuses_the_historical_report():
    """v2 is a different schema with a different domain; v1 cannot pass as current."""
    report = json.loads(V1_PATH.read_bytes())
    assert report["schema_version"] != PERSISTENCE_QUALIFICATION_SCHEMA
    with pytest.raises(DecisionPersistenceQualificationError):
        verify_persistence_report(report)


def test_the_accepted_report_is_not_the_historical_one():
    assert DECISION_PERSISTENCE_QUALIFICATION_PATH != HISTORICAL_V1_PATH
    assert PERSISTENCE_QUALIFICATION_SCHEMA.endswith("-v2")


def test_the_defect_it_was_superseded_for_is_visible_in_its_own_observation():
    """v1's own evidence shows the grants it certified. It is not being blamed for a guess."""
    report = json.loads(V1_PATH.read_bytes())
    grants = report["observation"]["privileges"]["minos_live_grants"]
    assert "profiling.bam_profiles:SELECT" in grants
    assert "catalog.dataset_registry:SELECT" in grants
    assert report["checks"]["live_role_holds_least_privilege"] is True
    # ... and that it never executed a single statement as the live role
    assert "capabilities" not in report["observation"]
