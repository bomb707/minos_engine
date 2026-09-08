"""The persistence contract's pure surface: identities, inventory, and what cannot be faked."""

from __future__ import annotations

import ast
import hashlib
import json
from pathlib import Path

import pytest
from tests.conftest import REPO_ROOT

from minos_engine.storage.decision_persistence import (
    OPERATIONAL_COLUMNS,
    PROFILE_IDENTITY_FIELDS,
    REGISTRY_IDENTITY_FIELDS,
    SCIENTIFIC_COLUMNS,
    SafeDecisionPersistenceError,
    VerifiedPersistedSafeDecision,
)
from minos_engine.storage.decision_persistence_qualification import (
    ALLOWED_RELATIONS,
    MANDATORY_CHECKS,
    PERSISTENCE_QUALIFICATION_SCHEMA,
    DecisionPersistenceQualificationError,
    TrustedPersistenceQualification,
    assemble_persistence_report,
    derive_checks,
    persistence_report_identity,
    verify_persistence_report,
)
from minos_engine.storage.runtime_decision_contract import (
    ADDED_CHECK_CONSTRAINTS,
    FROZEN_INVENTORY,
    LIVE_PROFILE_RESOLVER_COLUMNS,
    R0002_FROZEN_INVENTORY,
    R0002_MIGRATION_PATH,
    R0002_REVISION,
    REQUIRED_MAIN_REVISION,
    REVOKED_IDENTITY_TABLE_GRANTS,
    RUNTIME_OVERLAY_HEAD_REVISION,
    RUNTIME_OVERLAY_MIGRATION_PATH,
    RUNTIME_OVERLAY_REVISION,
    RUNTIME_OVERLAY_VERSION_TABLE,
    RUNTIME_OVERLAY_VERSION_TABLE_SCHEMA,
    decisions_table,
    runtime_overlay_contract_hash,
    runtime_overlay_head_contract_hash,
)

REPORT_PATH = REPO_ROOT / "reports/layer2/l2h-decision-persistence-qualification-v2.json"
MIGRATION = REPO_ROOT / RUNTIME_OVERLAY_MIGRATION_PATH


# --------------------------------------------------------------------------- #
# the overlay lineage is genuinely separate from the main chain
# --------------------------------------------------------------------------- #
def test_the_overlay_is_a_root_revision_of_its_own_lineage():
    source = MIGRATION.read_text()
    assert "down_revision: str | None = None" in source
    assert FROZEN_INVENTORY["down_revision"] is None


def test_the_overlay_tracks_its_version_somewhere_other_than_the_main_table():
    assert RUNTIME_OVERLAY_VERSION_TABLE != "alembic_version"
    assert (
        FROZEN_INVENTORY["version_table"]
        == f"{RUNTIME_OVERLAY_VERSION_TABLE_SCHEMA}.{RUNTIME_OVERLAY_VERSION_TABLE}"
    )
    env = (REPO_ROOT / "migrations_runtime/env.py").read_text()
    assert "version_table" in env and "version_table_schema" in env


def test_no_main_lineage_revision_is_touched_by_this_work():
    """The accepted scientific chain must be byte-identical, and still exactly one head."""
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    script = ScriptDirectory.from_config(Config(str(REPO_ROOT / "alembic.ini")))
    heads = script.get_heads()
    assert len(heads) == 1, heads
    assert heads[0] == "0026_l2f2_phase_d_closure"
    # and the overlay is not reachable from the main graph at all
    assert RUNTIME_OVERLAY_REVISION not in {r.revision for r in script.walk_revisions()}


def test_the_overlay_requires_exactly_the_accepted_operational_revision():
    assert REQUIRED_MAIN_REVISION == "0005_l2e_feature_view"
    source = MIGRATION.read_text()
    assert f'REQUIRED_MAIN_REVISION = "{REQUIRED_MAIN_REVISION}"' in source
    # ... and says so by comparing the WHOLE version set, not by a prefix or a "greater than"
    assert "revisions != (REQUIRED_MAIN_REVISION,)" in source


def test_the_overlay_imports_no_orm_metadata():
    tree = ast.parse(MIGRATION.read_text())
    modules: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            modules.append(node.module)
        elif isinstance(node, ast.Import):
            modules.extend(a.name for a in node.names)
    for module in modules:
        assert not module.startswith("minos_engine.storage.models"), module
        assert module != "minos_engine.storage.metadata", module
    for token in ("Base.metadata", "create_all", "drop_all"):
        assert token not in MIGRATION.read_text(), token


def test_the_overlay_columns_are_not_bound_to_the_db_ready_fingerprint():
    """Adding them to ``Base.metadata`` would silently move an accepted gate."""
    from minos_engine.storage import models as _models  # noqa: F401 - populate Base.metadata
    from minos_engine.storage.fingerprint import storage_schema_hash
    from minos_engine.storage.metadata import Base

    assert decisions_table.metadata is not Base.metadata
    live = Base.metadata.tables["runtime.decisions"]
    for column in ("mode", "decision_manifest", "decided_at", "selected_config_id"):
        assert column not in live.c, column
    # the DB-READY gate's storage fingerprint is a function of THAT metadata, and it has not moved
    committed = json.loads((REPO_ROOT / "gates/db-ready.json").read_bytes())
    assert committed["input_hashes"]["storage_schema_hash"] == storage_schema_hash()


def test_the_contract_hash_binds_the_committed_migration_bytes(tmp_path: Path):
    before = runtime_overlay_contract_hash()
    staged = tmp_path / "repo"
    (staged / Path(RUNTIME_OVERLAY_MIGRATION_PATH).parent).mkdir(parents=True)
    (staged / RUNTIME_OVERLAY_MIGRATION_PATH).write_bytes(
        MIGRATION.read_bytes() + b"\n# a change nobody accepted\n"
    )
    assert runtime_overlay_contract_hash(staged) != before


def test_the_inventory_counts_match_its_own_lists():
    counts = FROZEN_INVENTORY["counts"]
    assert isinstance(counts, dict)
    for key in ("added_columns", "added_check_constraints", "added_grants"):
        assert counts[key] == len(FROZEN_INVENTORY[key]), key
    assert sorted(ADDED_CHECK_CONSTRAINTS) == list(ADDED_CHECK_CONSTRAINTS)
    assert set(ADDED_CHECK_CONSTRAINTS) == set(FROZEN_INVENTORY["added_check_constraints"])


def test_every_declared_check_constraint_is_actually_in_the_migration():
    source = MIGRATION.read_text()
    for name in ADDED_CHECK_CONSTRAINTS:
        assert f'"{name}"' in source, name


def test_the_migration_enumerates_exactly_the_typed_enums_the_engine_defines():
    from minos_engine.layer2.contracts import ControlMode, FallbackReason

    source = MIGRATION.read_text()
    for mode in ControlMode:
        assert f'"{mode.value}"' in source, mode
    for reason in FallbackReason:
        assert f'"{reason.value}"' in source, reason


def test_config_id_is_retired_rather_than_repurposed():
    source = MIGRATION.read_text()
    assert '("ck_decisions_config_id_retired", "config_id IS NULL")' in source
    assert FROZEN_INVENTORY["retired_columns"] == ["config_id"]
    # nothing anywhere writes it
    assert "config_id" in SCIENTIFIC_COLUMNS


def test_the_profile_foreign_key_is_retargeted_not_dropped():
    retargeted = FROZEN_INVENTORY["retargeted_foreign_keys"]
    assert isinstance(retargeted, list) and len(retargeted) == 1
    assert retargeted[0]["to"] == "profiling.bam_profiles.id"
    source = MIGRATION.read_text()
    assert "fk_decisions_profile_id_bam_profiles" in source
    # the downgrade puts the old one back
    assert source.count("fk_decisions_profile_id_profiles") >= 2
    assert 'op.alter_column("decisions", "profile_id", nullable=False, schema="runtime")' in source


def test_the_overlay_adds_exactly_two_grants_and_revokes_them_on_downgrade():
    assert FROZEN_INVENTORY["added_grants"] == [
        "SELECT ON catalog.dataset_registry TO minos_live",
        "SELECT ON profiling.bam_profiles TO minos_live",
    ]
    source = MIGRATION.read_text()
    assert "GRANT SELECT ON {table} TO minos_live;" in source
    assert "REVOKE SELECT ON {table} FROM minos_live;" in source


# --------------------------------------------------------------------------- #
# the persistence API cannot be faked
# --------------------------------------------------------------------------- #
def test_a_persisted_decision_cannot_be_minted_from_a_dictionary():
    with pytest.raises(SafeDecisionPersistenceError):
        VerifiedPersistedSafeDecision(object(), result=None, row={"id": "x"}, converged=False)


def test_a_qualification_cannot_be_minted_from_a_dictionary():
    with pytest.raises(DecisionPersistenceQualificationError):
        TrustedPersistenceQualification(object(), {"decision_count": 200})


def test_a_report_cannot_be_assembled_from_claimed_results():
    with pytest.raises(DecisionPersistenceQualificationError):
        assemble_persistence_report({"observation": {}})  # type: ignore[arg-type]


def test_decided_at_is_operational_and_never_part_of_the_decision():
    """A retry must converge; a timestamp in the identity would make that impossible."""
    assert "decided_at" in OPERATIONAL_COLUMNS
    assert "decided_at" not in SCIENTIFIC_COLUMNS
    assert "created_at" not in SCIENTIFIC_COLUMNS
    assert "id" not in SCIENTIFIC_COLUMNS


def test_the_scientific_columns_cover_every_column_the_contract_names():
    covered = set(SCIENTIFIC_COLUMNS) | set(OPERATIONAL_COLUMNS)
    assert covered == {c.name for c in decisions_table.c}


def test_the_two_hashes_have_different_preimages():
    """0001 created two hash columns; the overlay gives each exactly one meaning."""
    source = (REPO_ROOT / "src/minos_engine/storage/decision_persistence.py").read_text()
    assert "hashlib.sha256(manifest_bytes).hexdigest()" in source
    assert "safe_decision_manifest_identity(manifest)" in source


def test_the_live_path_may_touch_only_the_relations_it_declares():
    for relation in ALLOWED_RELATIONS:
        assert "." in relation
    assert "evaluation.evaluations" not in ALLOWED_RELATIONS
    assert "models.model_bundles" not in ALLOWED_RELATIONS
    assert "experiments.jobs" not in ALLOWED_RELATIONS


def test_the_write_path_imports_no_truth_or_scoring():
    tree = ast.parse((REPO_ROOT / "src/minos_engine/storage/decision_persistence.py").read_text())
    modules: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            modules.append(node.module)
        elif isinstance(node, ast.Import):
            modules.extend(a.name for a in node.names)
    for module in modules:
        parts = module.split(".")
        for banned in ("hap", "scoring", "truth", "evaluation", "mutations"):
            assert banned not in parts, module


def test_the_profile_cross_check_never_uses_the_unauthenticated_manifest_hash():
    fields = {column for column, _ in PROFILE_IDENTITY_FIELDS}
    assert "profile_manifest_hash" not in fields
    assert "profile_manifest_sha256" in fields
    assert {column for column, _ in REGISTRY_IDENTITY_FIELDS} == {
        "dataset_id",
        "round_id",
        "chromosome",
    }


# --------------------------------------------------------------------------- #
# the committed evidence
# --------------------------------------------------------------------------- #
def _report() -> dict:
    if not REPORT_PATH.is_file():
        pytest.skip("no persistence qualification is committed yet (P-SOURCE)")
    return json.loads(REPORT_PATH.read_bytes())


def test_the_committed_report_verifies():
    report = _report()
    result = verify_persistence_report(report)
    assert result["ok"] and result["status"] == "PASS"
    assert result["check_count"] == len(MANDATORY_CHECKS)


def test_the_committed_report_is_canonical_bytes():
    from minos_engine.common.canonical_json import canonical_json_bytes

    if not REPORT_PATH.is_file():
        pytest.skip("no persistence qualification is committed yet (P-SOURCE)")
    raw = REPORT_PATH.read_bytes()
    assert canonical_json_bytes(json.loads(raw)) == raw


def test_every_check_is_derived_rather_than_recorded():
    report = _report()
    assert report["checks"] == dict(sorted(derive_checks(report["observation"]).items()))
    assert all(report["checks"][name] for name in MANDATORY_CHECKS)


def test_a_tampered_observation_changes_the_verdict():
    report = _report()
    tampered = json.loads(json.dumps(report))
    tampered["observation"]["final_row_count"] = 1
    with pytest.raises(DecisionPersistenceQualificationError):
        verify_persistence_report(tampered)


def test_a_hand_written_pass_is_refused():
    report = _report()
    tampered = json.loads(json.dumps(report))
    tampered["observation"]["privileges"]["public_table_grants_in_application_schemas"] = ["x.y"]
    tampered["checks"]["public_holds_no_privilege"] = True
    with pytest.raises(DecisionPersistenceQualificationError):
        verify_persistence_report(tampered)


def test_the_report_carries_no_operational_values():
    report = _report()
    blob = json.dumps(report).lower()
    for pattern in ("postgresql://", "password", "127.0.0.1", "/home/", "/tmp/"):
        assert pattern not in blob, pattern


def test_the_report_binds_the_accepted_frozen_controller_and_this_source():
    report = _report()
    gate = report["observation"]["accepted_gate"]
    assert gate["gate_hash"] == "504e701fe77b651c919ebc015dc6911bbca880613014058979b18ab408f88add"
    assert (
        gate["gate_file_sha256"]
        == "1bbc1b1b65b7759922d8501a256539850b5b5f95eaf1862a2cef22b9bf39e716"
    )
    assert gate["issuer_source_commit"] == "6a3bedfa33581fde22e96d6739146c087c23ba79"
    assert gate["qualified_source_commit"] == "7d064fe8bc7185bd5c07d16f1fec9dfdd21970b0"
    assert gate["capability_scope"] == "SAFE_BASELINE_ONLY"
    assert len(report["observation"]["execution_source_commit"]) == 40


def test_the_report_binds_the_overlay_identity():
    report = _report()
    schema = report["observation"]["persistence_schema"]
    assert schema["overlay_base_revision"] == RUNTIME_OVERLAY_REVISION
    assert schema["overlay_head_revision"] == RUNTIME_OVERLAY_HEAD_REVISION
    assert schema["requires_main_revision"] == REQUIRED_MAIN_REVISION
    assert schema["overlay_base_contract_hash"] == runtime_overlay_contract_hash()
    assert schema["overlay_head_contract_hash"] == runtime_overlay_head_contract_hash()


def test_the_report_issues_no_gate_and_activates_nothing():
    report = _report()
    assert report["gate_issued"] is False
    assert report["service_activated"] is False
    assert report["test_accessed"] is False
    assert report["validation_read"] is False
    assert report["observation"]["select_config_blocked"] is True
    assert report["schema_version"] == PERSISTENCE_QUALIFICATION_SCHEMA


def test_the_report_identity_is_domain_separated():
    report = _report()
    from minos_engine.common.canonical_json import canonical_json_bytes

    plain = hashlib.sha256(canonical_json_bytes(report)).hexdigest()
    assert persistence_report_identity(report) != plain


def test_the_public_service_is_still_blocked():
    from minos_engine.common.errors import StageNotReadyError
    from minos_engine.layer2.service import Layer2Service

    with pytest.raises(StageNotReadyError):
        Layer2Service().select_config(None)  # type: ignore[arg-type]


def test_no_controller_gate_was_issued_by_this_stage():
    assert not (REPO_ROOT / "gates/models-qualified.json").exists()
    assert not (REPO_ROOT / "gates/controller-frozen.json").exists()
    assert (REPO_ROOT / "gates/safe-controller-frozen.json").is_file()


def test_the_accepted_frozen_gate_still_verifies_unchanged():
    from minos_engine.layer2.safe_controller_frozen_acceptance import (
        verify_accepted_safe_controller_frozen_gate,
    )

    result = verify_accepted_safe_controller_frozen_gate(REPO_ROOT)
    assert result["ok"]
    assert result["gate_hash"] == (
        "504e701fe77b651c919ebc015dc6911bbca880613014058979b18ab408f88add"
    )


# --------------------------------------------------------------------------- #
# r0002: the privilege corrective, structurally
# --------------------------------------------------------------------------- #
R0002 = REPO_ROOT / R0002_MIGRATION_PATH


def test_r0001_is_never_rewritten_by_the_corrective():
    """r0001 is committed, qualified and applied; the corrective is additive."""
    assert R0002_FROZEN_INVENTORY["down_revision"] == RUNTIME_OVERLAY_REVISION
    assert f'down_revision: str | None = "{RUNTIME_OVERLAY_REVISION}"' in R0002.read_text()
    # r0001's own contract hash is a function of its bytes; it must still be what it was
    assert (
        runtime_overlay_contract_hash()
        == "4265fe13583344ebf0f6d1404a9a2fc0e4556096122e060442e5aaf87f8fef25"
    )


def test_the_corrective_revision_fits_alembics_version_column():
    """Alembic stores the revision in varchar(32); a longer id fails only at apply time."""
    assert len(R0002_REVISION) <= 32
    assert len(RUNTIME_OVERLAY_REVISION) <= 32


def test_the_corrective_revokes_both_raw_identity_table_grants():
    source = R0002.read_text()
    assert REVOKED_IDENTITY_TABLE_GRANTS == (
        "catalog.dataset_registry",
        "profiling.bam_profiles",
    )
    for table in REVOKED_IDENTITY_TABLE_GRANTS:
        assert f'"{table}"' in source, table
    assert "REVOKE SELECT ON {table} FROM minos_live;" in source
    assert R0002_FROZEN_INVENTORY["revoked_grants"] == [
        "SELECT ON catalog.dataset_registry FROM minos_live",
        "SELECT ON profiling.bam_profiles FROM minos_live",
    ]


def test_the_narrow_surface_is_a_hardened_definer_by_construction():
    source = R0002.read_text()
    assert "SECURITY DEFINER" in source
    assert "SET search_path = pg_catalog, pg_temp" in source or (
        "SET search_path = {RESOLVER_SEARCH_PATH}" in source
    )
    assert "SET ROLE minos_admin" in source, "the function must be owned by minos_admin"
    assert "REVOKE ALL ON FUNCTION {RESOLVER_SIGNATURE} FROM PUBLIC;" in source
    assert "GRANT EXECUTE ON FUNCTION {RESOLVER_SIGNATURE} TO minos_live;" in source
    # no dynamic SQL anywhere in the function body
    for token in ("EXECUTE format", "quote_ident", "EXECUTE '"):
        assert token not in source, token
    # absent arguments are refused rather than treated as a wildcard
    assert "null_value_not_allowed" in source
    assert "cardinality_violation" in source
    assert R0002_FROZEN_INVENTORY["function_uses_dynamic_sql"] is False
    assert R0002_FROZEN_INVENTORY["function_public_execute"] is False
    assert R0002_FROZEN_INVENTORY["function_owner"] == "minos_admin"


def test_the_resolver_returns_exactly_the_cross_checked_identity_and_no_document():
    """``profile_document`` is never exposed: the write path does not read it."""
    assert "profile_document" not in LIVE_PROFILE_RESOLVER_COLUMNS
    assert "p.profile_document" not in R0002.read_text(), "the SELECT list must not reach it"
    checked = {c for c, _ in PROFILE_IDENTITY_FIELDS} | {c for c, _ in REGISTRY_IDENTITY_FIELDS}
    assert checked <= set(LIVE_PROFILE_RESOLVER_COLUMNS)
    assert "profile_row_id" in LIVE_PROFILE_RESOLVER_COLUMNS


def test_the_write_path_no_longer_reads_the_raw_identity_tables():
    """Checked against the module's real string literals, not its prose."""
    source = (REPO_ROOT / "src/minos_engine/storage/decision_persistence.py").read_text()
    tree = ast.parse(source)
    docstrings = {
        id(node.body[0].value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Module | ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef)
        and node.body
        and isinstance(node.body[0], ast.Expr)
        and isinstance(node.body[0].value, ast.Constant)
        and isinstance(node.body[0].value.value, str)
    }
    literals = [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and id(node) not in docstrings
    ]
    for table in REVOKED_IDENTITY_TABLE_GRANTS:
        for literal in literals:
            assert table not in literal, f"{table} still appears in executable SQL"
    # the surface is referenced by its frozen constant, never spelled out inline
    names = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
    assert "LIVE_PROFILE_RESOLVER" in names


def test_the_corrective_downgrade_restores_the_prior_grants():
    source = R0002.read_text()
    assert "GRANT SELECT ON {table} TO minos_live;" in source
    assert "DROP FUNCTION IF EXISTS" in source
    assert "REVOKE SELECT ON {table} FROM minos_live;" in source


def test_the_corrective_still_requires_the_accepted_operational_revision():
    source = R0002.read_text()
    assert f'REQUIRED_MAIN_REVISION = "{REQUIRED_MAIN_REVISION}"' in source
    assert "revisions != (REQUIRED_MAIN_REVISION,)" in source
    assert R0002_FROZEN_INVENTORY["requires_main_revision"] == REQUIRED_MAIN_REVISION


def test_the_main_lineage_is_still_untouched_and_single_headed():
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    script = ScriptDirectory.from_config(Config(str(REPO_ROOT / "alembic.ini")))
    assert list(script.get_heads()) == ["0026_l2f2_phase_d_closure"]
    revisions = {r.revision for r in script.walk_revisions()}
    assert R0002_REVISION not in revisions
    assert RUNTIME_OVERLAY_REVISION not in revisions


def test_the_overlay_lineage_has_exactly_one_head_and_it_is_the_corrective():
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    config = Config()
    config.set_main_option("script_location", str(REPO_ROOT / "migrations_runtime"))
    script = ScriptDirectory.from_config(config)
    assert list(script.get_heads()) == [RUNTIME_OVERLAY_HEAD_REVISION]
