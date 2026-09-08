"""The corrected persistence evidence, pinned by identity and by bytes.

Committed one commit after the source that produced it, so nothing identifies itself.
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
    PERSISTENCE_QUALIFICATION_SCHEMA,
    DecisionPersistenceQualificationError,
    derive_checks,
    persistence_report_identity,
    verify_persistence_report,
)
from minos_engine.storage.runtime_decision_contract import (
    LIVE_PROFILE_RESOLVER,
    R0002_REVISION,
    REQUIRED_MAIN_REVISION,
    RUNTIME_OVERLAY_HEAD_REVISION,
    RUNTIME_OVERLAY_REVISION,
    runtime_overlay_contract_hash,
    runtime_overlay_head_contract_hash,
)

REPORT_PATH = REPO_ROOT / DECISION_PERSISTENCE_QUALIFICATION_PATH

QUALIFICATION_IDENTITY = "7bfd8f9f816a387653125d6be820b8cf05ccf017498009bc7e706d29fd3337b3"
QUALIFICATION_FILE_SHA = "1e54930d571befc9543c351fcd7cf5dbd93f3ef1ac335da716cc615e3970cf6a"
QUALIFIED_SOURCE_COMMIT = "c05aad9ca160f6fa975c5230e175385132362656"
QUALIFIED_SOURCE_TREE = "5ffe3d765b2fc8aaa4a850beb8399bfc5d34266d"
OVERLAY_HEAD_CONTRACT_HASH = "2af0f847037039c413c3e3b277839cacd4c10063869f7d1210aa9715c687d120"
R0001_CONTRACT_HASH = "4265fe13583344ebf0f6d1404a9a2fc0e4556096122e060442e5aaf87f8fef25"
ACCEPTED_GATE_HASH = "504e701fe77b651c919ebc015dc6911bbca880613014058979b18ab408f88add"

CAMPAIGN_DECISIONS = 196
CAMPAIGN_ROUNDS = 49
TRAIN_IDENTITY_ROWS = 50
INSUFFICIENT_PRIVILEGE = "42501"


def _report() -> dict:
    return json.loads(REPORT_PATH.read_bytes())


def test_the_committed_evidence_is_the_accepted_one():
    raw = REPORT_PATH.read_bytes()
    assert hashlib.sha256(raw).hexdigest() == QUALIFICATION_FILE_SHA
    report = json.loads(raw)
    assert canonical_json_bytes(report) == raw
    assert report["schema_version"] == PERSISTENCE_QUALIFICATION_SCHEMA
    assert persistence_report_identity(report) == QUALIFICATION_IDENTITY
    result = verify_persistence_report(report)
    assert result["ok"] and result["status"] == "PASS"
    assert result["check_count"] == len(MANDATORY_CHECKS) == 41


def test_the_evidence_names_the_source_that_produced_it():
    observation = _report()["observation"]
    assert observation["execution_source_commit"] == QUALIFIED_SOURCE_COMMIT
    assert observation["execution_source_tree"] == QUALIFIED_SOURCE_TREE


def test_the_qualified_source_is_a_real_commit_and_not_this_one():
    from minos_engine.qualification.git_tree import commit_tree_sha, is_ancestor, is_commit

    if not (REPO_ROOT / ".git").exists():  # pragma: no cover - exported tree
        pytest.skip("not a git checkout")
    assert is_commit(REPO_ROOT, QUALIFIED_SOURCE_COMMIT)
    assert str(commit_tree_sha(REPO_ROOT, QUALIFIED_SOURCE_COMMIT)) == QUALIFIED_SOURCE_TREE
    assert is_ancestor(REPO_ROOT, QUALIFIED_SOURCE_COMMIT, "HEAD")


def test_every_check_is_derived_rather_than_recorded():
    report = _report()
    assert report["checks"] == dict(sorted(derive_checks(report["observation"]).items()))
    assert all(report["checks"][name] for name in MANDATORY_CHECKS)


# --------------------------------------------------------------------------- #
# the corrective's own guarantees
# --------------------------------------------------------------------------- #
def test_the_campaign_really_ran_as_the_live_role():
    """v1's whole campaign ran as the owner. This one did not."""
    capabilities = _report()["observation"]["capabilities"]
    assert capabilities["campaign_connection_role"] == "minos_live"


def test_every_enumeration_attempt_was_refused_by_privilege():
    probes = _report()["observation"]["capabilities"]["enumeration_probe_sqlstates"]
    assert len(probes) == 7
    assert set(probes.values()) == {INSUFFICIENT_PRIVILEGE}
    for name in (
        "profile_select_star",
        "profile_count",
        "profile_single_column",
        "profile_exists",
        "registry_select_star",
        "registry_count",
        "registry_single_column",
    ):
        assert probes[name] == INSUFFICIENT_PRIVILEGE, name


def test_the_live_role_holds_no_partition_bearing_relation():
    capabilities = _report()["observation"]["capabilities"]
    assert len(capabilities["partition_bearing_relations_probed"]) == 7
    assert (
        "evaluation.sealed_test_profile_members"
        in (capabilities["partition_bearing_relations_probed"])
    )
    assert capabilities["partition_bearing_relations_readable_by_live"] == []


def test_the_resolver_is_recorded_as_hardened():
    resolver = _report()["observation"]["capabilities"]["resolver"]
    assert resolver["name"] == LIVE_PROFILE_RESOLVER
    assert resolver["owner"] == "minos_admin"
    assert resolver["security_definer"] is True
    assert resolver["language"] == "plpgsql"
    assert resolver["uses_dynamic_sql"] is False
    assert resolver["returns_at_most_one_row"] is True
    assert resolver["refuses_absent_arguments"] is True
    assert "search_path=pg_catalog, pg_temp" in resolver["search_path_setting"]
    assert resolver["unknown_profile_row_count"] == 0
    assert resolver["execute_privilege"] == {
        "minos_evaluator": False,
        "minos_live": True,
        "minos_runner": False,
        "minos_trainer": False,
        "public": False,
    }
    assert set(resolver["nonlive_execute_sqlstates"].values()) == {INSUFFICIENT_PRIVILEGE}
    assert set(resolver["absent_argument_sqlstates"].values()) == {"22004"}


def test_the_corrective_changed_privilege_and_nothing_else():
    corrective = _report()["observation"]["corrective"]
    assert corrective["revision"] == R0002_REVISION
    assert corrective["grants_revoked"] == [
        "catalog.dataset_registry:minos_live:SELECT",
        "profiling.bam_profiles:minos_live:SELECT",
    ]
    assert corrective["grants_added"] == [
        "public.alembic_version:minos_live:SELECT",
        "runtime.alembic_version_runtime:minos_live:SELECT",
    ]
    assert corrective["columns_unchanged"] is True
    assert corrective["constraints_unchanged"] is True
    assert corrective["indexes_unchanged"] is True


def test_the_full_overlay_lifecycle_was_exercised():
    observation = _report()["observation"]
    assert observation["state_before_overlay"]["overlay_revision"] is None
    assert observation["state_after_overlay"]["overlay_revision"] == RUNTIME_OVERLAY_REVISION
    assert (
        observation["state_after_corrective"]["overlay_revision"] == RUNTIME_OVERLAY_HEAD_REVISION
    )
    assert observation["state_final_overlay"]["overlay_revision"] == RUNTIME_OVERLAY_HEAD_REVISION
    downgrade = observation["downgrade"]
    assert downgrade["corrective_restores_the_prior_revision"] is True
    assert downgrade["schema_restored"] is True
    assert downgrade["grants_restored"] is True
    for stage in ("state_before_overlay", "state_after_corrective", "state_final_overlay"):
        assert observation[stage]["main_revision"] == REQUIRED_MAIN_REVISION, stage


def test_the_evidence_binds_both_overlay_revisions():
    schema = _report()["observation"]["persistence_schema"]
    assert schema["overlay_base_revision"] == RUNTIME_OVERLAY_REVISION
    assert schema["overlay_head_revision"] == RUNTIME_OVERLAY_HEAD_REVISION
    assert schema["overlay_base_contract_hash"] == R0001_CONTRACT_HASH
    assert schema["overlay_base_contract_hash"] == runtime_overlay_contract_hash()
    assert schema["overlay_head_contract_hash"] == OVERLAY_HEAD_CONTRACT_HASH
    assert schema["overlay_head_contract_hash"] == runtime_overlay_head_contract_hash()
    assert schema["requires_main_revision"] == REQUIRED_MAIN_REVISION
    assert [entry["identity"] for entry in schema["supersedes"]] == [
        "e88f6cf83063905e1608c9583185b30d09f9943e3abfa92a0508858f8d617f20"
    ]


def test_the_sql_observation_never_names_a_forbidden_relation():
    sql = _report()["observation"]["sql_observation"]
    assert sql["forbidden_relations_named"] == []
    assert sql["relations_outside_the_allowed_set"] == []
    assert sql["unqualified_profile_scans"] == 0
    assert sql["observed_statements"] > 1000
    assert LIVE_PROFILE_RESOLVER in sql["observed_relations"]
    assert "profiling.bam_profiles" not in sql["observed_relations"]
    assert "catalog.dataset_registry" not in sql["observed_relations"]
    assert sql["profiles_opened_from_the_train_corpus"] == TRAIN_IDENTITY_ROWS


# --------------------------------------------------------------------------- #
# the v1 properties, preserved
# --------------------------------------------------------------------------- #
def test_the_campaign_still_wrote_the_decisions_it_claims():
    observation = _report()["observation"]
    assert observation["campaign_rounds"] == CAMPAIGN_ROUNDS
    assert observation["decision_count"] == CAMPAIGN_DECISIONS
    assert observation["inserted_count"] == CAMPAIGN_DECISIONS
    assert observation["converged_count"] == 0
    assert observation["train_identity_rows_copied"] == TRAIN_IDENTITY_ROWS
    assert observation["decisions_per_round"] == [4]
    assert observation["actual_mode_counts"] == {"SAFE_BASELINE": CAMPAIGN_DECISIONS}
    assert observation["model_bundle_id_values"] == ["NULL"]
    assert observation["config_id_values"] == ["NULL"]
    readback = observation["readback"]
    assert readback["manifests_equal"] == CAMPAIGN_DECISIONS
    assert readback["decision_hash_re_derived_from_the_manifest"] == CAMPAIGN_DECISIONS
    assert readback["manifest_bytes_hash_re_derived"] == CAMPAIGN_DECISIONS


def test_the_failure_drills_still_failed():
    observation = _report()["observation"]
    assert observation["rollback"]["insert_executed"] is True
    assert observation["rollback"]["failure_injected"] is True
    assert observation["rollback"]["no_visible_state"] is True
    assert observation["conflict"]["refusing_constraint"] == "uq_decisions_decision_hash"
    assert observation["conflict"]["divergent_stored_state_refused"] is True
    assert observation["concurrency"]["errors"] == []
    assert observation["concurrency"]["rows_for_that_identity"] == 1
    assert observation["retry"]["converged"] is True
    assert observation["prerequisite_refusals"] == {
        "missing_profile": True,
        "missing_safe_config": True,
    }


def test_the_overlay_was_refused_by_train_and_validation_schemas():
    refusals = _report()["observation"]["overlay_refusals"]
    assert set(refusals) >= {
        "no_main_lineage",
        "0001_l2b_initial",
        "0020_l2f2_phase_c_execution",
    }
    for name, drill in refusals.items():
        assert drill["refused"] is True, name
        assert drill["applied"] is False, name
        assert drill["added_columns"] == 0, name


def test_the_isolation_observers_measured_rather_than_asserted():
    observation = _report()["observation"]
    assert observation["forbidden_sealed_path_open_attempts"] == 0
    assert observation["test_identity_authority_open_attempts"] == 0
    assert observation["validation_identity_authority_open_attempts"] == 0
    assert observation["no_truth_or_scoring"]["forbidden_imports"] == []


def test_the_evidence_binds_the_accepted_frozen_controller():
    gate = _report()["observation"]["accepted_gate"]
    assert gate["gate_hash"] == ACCEPTED_GATE_HASH
    assert gate["capability_scope"] == "SAFE_BASELINE_ONLY"
    assert gate["check_count"] == 29


def test_no_gate_was_issued_and_the_service_is_still_blocked():
    report = _report()
    assert report["gate_issued"] is False
    assert report["service_activated"] is False
    assert report["observation"]["select_config_blocked"] is True
    assert report["test_accessed"] is False
    assert report["validation_read"] is False
    assert not (REPO_ROOT / "gates/models-qualified.json").exists()
    assert not (REPO_ROOT / "gates/controller-frozen.json").exists()


def test_the_report_carries_no_operational_values():
    blob = json.dumps(_report()).lower()
    for pattern in ("postgresql://", "password", "127.0.0.1", "/home/", "/tmp/", "role=minos_live"):
        assert pattern not in blob, pattern


@pytest.mark.parametrize(
    "path,value",
    [
        (("capabilities", "campaign_connection_role"), "postgres"),
        (("capabilities", "enumeration_probe_sqlstates", "profile_count"), "00000"),
        (("capabilities", "partition_bearing_relations_readable_by_live"), ["x.y"]),
        (("capabilities", "resolver", "security_definer"), False),
        (("capabilities", "resolver", "uses_dynamic_sql"), True),
        (("capabilities", "resolver", "execute_privilege", "public"), True),
        (("corrective", "grants_revoked"), []),
        (("downgrade", "corrective_restores_the_prior_revision"), False),
        (("final_row_count",), 0),
        (("execution_source_commit",), "0" * 40),
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
