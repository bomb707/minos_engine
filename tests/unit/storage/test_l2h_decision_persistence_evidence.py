"""The accepted persistence evidence, pinned by identity and by bytes.

These constants live in the commit AFTER the one that produced the report, so the qualified
source is identified from outside itself — the same non-circular pattern as
``safe_controller_frozen_acceptance``.
"""

from __future__ import annotations

import hashlib
import json

import pytest
from tests.conftest import REPO_ROOT

from minos_engine.common.canonical_json import canonical_json_bytes
from minos_engine.storage.decision_persistence_qualification import (
    DECISION_PERSISTENCE_QUALIFICATION_PATH,
    MANDATORY_CHECKS,
    DecisionPersistenceQualificationError,
    persistence_report_identity,
    verify_persistence_report,
)
from minos_engine.storage.runtime_decision_contract import (
    REQUIRED_MAIN_REVISION,
    RUNTIME_OVERLAY_REVISION,
    runtime_overlay_contract_hash,
)

REPORT_PATH = REPO_ROOT / DECISION_PERSISTENCE_QUALIFICATION_PATH

QUALIFICATION_IDENTITY = "e88f6cf83063905e1608c9583185b30d09f9943e3abfa92a0508858f8d617f20"
QUALIFICATION_FILE_SHA = "9bb98d5106f239e596715d79e91c8dee2055b2cc3f2b2a860eb625b2b5400775"
QUALIFIED_SOURCE_COMMIT = "1b67ee2f755b82526028d97aca7e8fe8db56e816"
QUALIFIED_SOURCE_TREE = "e5de947544241edeb83527c59de61792e0c18bb5"
OVERLAY_CONTRACT_HASH = "4265fe13583344ebf0f6d1404a9a2fc0e4556096122e060442e5aaf87f8fef25"

ACCEPTED_GATE_HASH = "504e701fe77b651c919ebc015dc6911bbca880613014058979b18ab408f88add"
CAMPAIGN_DECISIONS = 196
CAMPAIGN_ROUNDS = 49
TRAIN_IDENTITY_ROWS = 50


def _report() -> dict:
    return json.loads(REPORT_PATH.read_bytes())


def test_the_committed_evidence_is_the_accepted_one():
    raw = REPORT_PATH.read_bytes()
    assert hashlib.sha256(raw).hexdigest() == QUALIFICATION_FILE_SHA
    report = json.loads(raw)
    assert canonical_json_bytes(report) == raw
    assert persistence_report_identity(report) == QUALIFICATION_IDENTITY
    assert verify_persistence_report(report)["status"] == "PASS"


def test_the_evidence_names_the_source_that_produced_it():
    observation = _report()["observation"]
    assert observation["execution_source_commit"] == QUALIFIED_SOURCE_COMMIT
    assert observation["execution_source_tree"] == QUALIFIED_SOURCE_TREE


def test_the_qualified_source_is_a_real_commit_and_not_this_one():
    """Non-circular: the source that ran the campaign is a strict ancestor of HEAD."""
    from minos_engine.qualification.git_tree import commit_tree_sha, is_ancestor, is_commit

    if not (REPO_ROOT / ".git").exists():  # pragma: no cover - exported tree
        pytest.skip("not a git checkout")
    head = (REPO_ROOT / ".git" / "HEAD").read_text()
    assert head  # a checkout, not a bare export
    assert is_commit(REPO_ROOT, QUALIFIED_SOURCE_COMMIT)
    assert str(commit_tree_sha(REPO_ROOT, QUALIFIED_SOURCE_COMMIT)) == QUALIFIED_SOURCE_TREE
    assert is_ancestor(REPO_ROOT, QUALIFIED_SOURCE_COMMIT, "HEAD")


def test_the_evidence_binds_the_accepted_frozen_controller():
    gate = _report()["observation"]["accepted_gate"]
    assert gate["gate_hash"] == ACCEPTED_GATE_HASH
    assert gate["capability_scope"] == "SAFE_BASELINE_ONLY"
    assert gate["check_count"] == 29


def test_the_evidence_binds_the_overlay_identity_and_its_prerequisite():
    schema = _report()["observation"]["persistence_schema"]
    assert schema["overlay_revision"] == RUNTIME_OVERLAY_REVISION
    assert (
        schema["overlay_contract_hash"] == OVERLAY_CONTRACT_HASH == runtime_overlay_contract_hash()
    )
    assert schema["requires_main_revision"] == REQUIRED_MAIN_REVISION


def test_the_campaign_really_wrote_the_decisions_it_claims():
    observation = _report()["observation"]
    assert observation["campaign_rounds"] == CAMPAIGN_ROUNDS
    assert observation["decision_count"] == CAMPAIGN_DECISIONS
    assert observation["inserted_count"] == CAMPAIGN_DECISIONS
    assert observation["converged_count"] == 0
    assert observation["train_identity_rows_copied"] == TRAIN_IDENTITY_ROWS
    assert observation["decisions_per_round"] == [4]
    assert observation["actual_mode_counts"] == {"SAFE_BASELINE": CAMPAIGN_DECISIONS}
    assert observation["fallback_reason_counts"] == {
        "NONE": CAMPAIGN_ROUNDS,
        "SAFE_BASELINE_FORCED": CAMPAIGN_ROUNDS * 3,
    }
    assert observation["model_bundle_id_values"] == ["NULL"]
    assert observation["config_id_values"] == ["NULL"]
    assert observation["distinct_selected_config_ids"] == 1
    readback = observation["readback"]
    assert readback["manifests_equal"] == CAMPAIGN_DECISIONS
    assert readback["decision_hash_re_derived_from_the_manifest"] == CAMPAIGN_DECISIONS
    assert readback["manifest_bytes_hash_re_derived"] == CAMPAIGN_DECISIONS


def test_the_overlay_was_refused_by_every_foreign_schema_including_train_and_validation():
    refusals = _report()["observation"]["overlay_refusals"]
    assert set(refusals) == {
        "no_main_lineage",
        "0001_l2b_initial",
        "0020_l2f2_phase_c_execution",
        "0026_l2f2_phase_d_closure",
    }
    for name, drill in refusals.items():
        assert drill["refused"] is True, name
        assert drill["applied"] is False, name
        assert drill["added_columns"] == 0, name


def test_the_main_lineage_stayed_where_it_was():
    observation = _report()["observation"]
    for stage in ("state_before_overlay", "state_after_overlay", "final_state"):
        assert observation[stage]["main_revision"] == REQUIRED_MAIN_REVISION, stage
    assert observation["state_before_overlay"]["overlay_revision"] is None
    assert observation["final_state"]["overlay_revision"] == RUNTIME_OVERLAY_REVISION
    assert observation["downgrade"]["schema_restored"] is True
    assert observation["downgrade"]["grants_restored"] is True


def test_the_isolation_observers_measured_rather_than_asserted():
    observation = _report()["observation"]
    assert observation["forbidden_sealed_path_open_attempts"] == 0
    assert observation["test_identity_authority_open_attempts"] == 0
    assert observation["validation_identity_authority_open_attempts"] == 0
    sql = observation["sql_observation"]
    assert sql["observed_statements"] > 1000, "the recorder saw the real campaign"
    assert sql["relations_outside_the_allowed_set"] == []
    assert sql["forbidden_sql_tokens_seen"] == []
    assert sql["unqualified_profile_scans"] == 0
    assert sql["profiles_opened_from_the_train_corpus"] == TRAIN_IDENTITY_ROWS
    assert sql["profiles_opened_outside_the_train_corpus"] == sql["declared_negative_probes"]


def test_the_failure_drills_actually_failed():
    observation = _report()["observation"]
    assert observation["rollback"]["insert_executed"] is True
    assert observation["rollback"]["failure_injected"] is True
    assert observation["rollback"]["no_visible_state"] is True
    assert observation["conflict"]["refusing_constraint"] == "uq_decisions_decision_hash"
    assert observation["conflict"]["divergent_stored_state_refused"] is True
    assert observation["conflict"]["forgery_removed"] is True
    assert observation["concurrency"]["errors"] == []
    assert observation["concurrency"]["rows_for_that_identity"] == 1
    assert observation["prerequisite_refusals"] == {
        "missing_profile": True,
        "missing_safe_config": True,
    }


def test_the_privilege_matrix_is_what_the_engine_documents():
    privileges = _report()["observation"]["privileges"]
    assert privileges["minos_live_privilege_types"] == ["INSERT", "SELECT"]
    assert privileges["minos_live_write_targets"] == ["audit.events", "runtime.decisions"]
    assert privileges["minos_live_can_insert_decisions"] is True
    assert privileges["minos_live_can_read_the_owning_profile"] is True
    assert privileges["public_table_grants_in_application_schemas"] == []
    assert privileges["security_definer_functions"] == 0
    assert privileges["decisions_table_owner"] == "minos_admin"
    assert privileges["owner_is_superuser"] is False
    assert all(privileges["denials"].values())


@pytest.mark.parametrize(
    "path,value",
    [
        (("final_row_count",), 0),
        (("inserted_count",), 1),
        (("execution_source_commit",), "0" * 40),
        (("train_identity_rows_copied",), 3),
        (("persistence_schema", "overlay_revision"), "r9999_something_else"),
        (("privileges", "security_definer_functions"), 2),
        (("privileges", "owner_is_superuser"), True),
        (("sql_observation", "unqualified_profile_scans"), 1),
        (("rollback", "insert_executed"), False),
        (("conflict", "refusing_constraint"), "some_other_constraint"),
    ],
)
def test_a_tampered_observation_cannot_keep_its_verdict(path, value):
    report = json.loads(json.dumps(_report()))
    node = report["observation"]
    for key in path[:-1]:
        node = node[key]
    node[path[-1]] = value
    with pytest.raises(DecisionPersistenceQualificationError):
        verify_persistence_report(report)


def test_no_gate_was_issued_and_the_service_is_still_blocked():
    report = _report()
    assert report["gate_issued"] is False
    assert report["service_activated"] is False
    assert report["observation"]["select_config_blocked"] is True
    assert len(report["mandatory_checks"]) == len(MANDATORY_CHECKS) == 30
    assert not (REPO_ROOT / "gates/models-qualified.json").exists()
    assert not (REPO_ROOT / "gates/controller-frozen.json").exists()
