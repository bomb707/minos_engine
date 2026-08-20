"""F3-A closure: exhaustive static/live 0006 inventory equality + frozen contract identities.

Proves the frozen owner-reviewed static inventory in ``l2f_migration_contract`` is exact and
complete against the deployed schema:

* the migration file byte SHA and the domain-separated contract hash are frozen and recompute
  identically, with no self-reference and with per-section mutation sensitivity;
* the static inventory independently enumerates the owned tables, columns, constraints
  (including the three overlapping member UNIQUE constraints with exact columns), indexes,
  composite targets, triggers and the job function;
* live 0006 (scratch PG) equals the frozen live inventory exactly; and
* an explicit 0005 -> 0006 -> 0005 -> 0006 schema lifecycle re-derives the frozen inventory at
  0006 and restores the captured normalized 0005 state exactly.
"""

from __future__ import annotations

import copy
import hashlib
from pathlib import Path

from sqlalchemy import create_engine

from minos_engine.storage import l2f_migration_contract as C
from minos_engine.storage.database import normalize_database_url
from tests.integration.layer2_db.conftest import (
    alembic_downgrade,
    alembic_upgrade,
    scratch_database,
)
from tests.integration.layer2_db.l2f_introspect import full_structural_state, l2f_live_inventory

_HEAD = "0006_l2f_experiment_plan"
_PREV = "0005_l2e_feature_view"
_SCHEMAS = ["profiling", "catalog", "experiments"]
_ROLES = ["minos_admin", "minos_live", "minos_runner", "minos_trainer", "minos_evaluator"]


# --------------------------------------------------------------------------- #
# static (no database) — frozen identities + inventory shape
# --------------------------------------------------------------------------- #
def test_migration_file_byte_sha256_is_frozen() -> None:
    assert C.compute_migration_sha256() == C.L2F_MIGRATION_SHA256
    assert (
        C.L2F_MIGRATION_SHA256 == "1eb3a12b502a5f247a2dc662642fd71931dcada815923e95d18504220445c3c6"
    )


def test_prior_migrations_0001_0005_byte_identical() -> None:
    for rel, expected in C.ACCEPTED_PRIOR_MIGRATION_SHAS.items():
        got = hashlib.sha256(Path(rel).read_bytes()).hexdigest()
        assert got == expected, f"{rel} changed"


def test_contract_hash_frozen_and_recomputes() -> None:
    assert C.L2F_CONTRACT_HASH == "3802f01c5873e08b3268462eb69861652c1cb4fef41fd8b21db394bb6193c318"
    assert C.compute_contract_hash(migration_sha256=C.L2F_MIGRATION_SHA256) == C.L2F_CONTRACT_HASH


def test_contract_hash_has_no_self_reference() -> None:
    # the frozen contract hash must not appear anywhere in its own preimage inputs.
    import json

    preimage_inputs = json.dumps(
        {
            "inventory": C.L2F_STATIC_INVENTORY,
            "prior": C.ACCEPTED_PRIOR_MIGRATION_SHAS,
            "migration_sha256": C.L2F_MIGRATION_SHA256,
            "revision": C.L2F_MIGRATION_REVISION,
        },
        sort_keys=True,
    )
    assert C.L2F_CONTRACT_HASH not in preimage_inputs


def test_contract_hash_is_sensitive_to_migration_sha() -> None:
    other = "0" * 64
    assert C.compute_contract_hash(migration_sha256=other) != C.L2F_CONTRACT_HASH


def test_contract_hash_is_sensitive_to_every_inventory_section() -> None:
    for key, value in C.L2F_STATIC_INVENTORY.items():
        mutated = copy.deepcopy(C.L2F_STATIC_INVENTORY)
        if isinstance(value, list):
            mutated[key] = [*value, "__mutation_probe__"]
        elif isinstance(value, dict):
            mutated[key] = {**value, "__mutation_probe__": 1}
        elif isinstance(value, bool):
            mutated[key] = not value
        else:
            mutated[key] = f"{value}__mutation_probe__"
        got = C.compute_contract_hash(migration_sha256=C.L2F_MIGRATION_SHA256, inventory=mutated)
        assert got != C.L2F_CONTRACT_HASH, f"contract hash ignores inventory section {key!r}"


def test_static_inventory_enumerates_all_objects() -> None:
    live = C.L2F_LIVE_INVENTORY
    owned = live["owned_tables"]
    assert set(owned) == {f"experiments.{t}" for t in C.L2F_TABLES}
    assert {name: len(t["columns"]) for name, t in owned.items()} == {
        "experiments.l2f_experiment_plans": 21,
        "experiments.l2f_experiment_plan_members": 12,
        "experiments.l2f_config_payloads": 7,
        "experiments.l2f_experiment_plan_configs": 7,
        "experiments.l2f_experiment_jobs": 10,
    }
    for t in owned.values():
        assert t["owner"] == "minos_admin"
        assert t["persistence"] == "permanent"
        assert t["rowsecurity"] is False
    assert live["no_app_role_grants"] is True
    assert len(live["owned_constraints"]) == 61
    assert {c["name"] for c in live["composite_targets"]} == set(
        C.L2F_STATIC_INVENTORY["composite_target_names"]
    )
    assert len(live["owned_indexes"]) == 23
    assert {t["name"] for t in live["owned_triggers"]} == set(C.L2F_TRIGGERS)
    assert len(live["job_function"]) == 1
    assert live["job_function"][0]["schema"] == "experiments"
    assert live["job_function"][0]["name"] == "minos_l2f_reject_job_identity_change"


def test_static_inventory_proves_three_member_unique_constraints() -> None:
    """The three overlapping member UNIQUE constraints exist with their exact columns."""
    by_name = {c["name"]: c for c in C.L2F_LIVE_INVENTORY["owned_constraints"]}
    expected = {
        "uq_l2f_pm_plan_snapshot_member": ["plan_id", "profile_snapshot_member_id"],
        "uq_l2f_pm_plan_matrix_member": ["plan_id", "feature_matrix_member_id"],
        "uq_l2f_pm_plan_member_index": ["plan_id", "member_index"],
    }
    for name, cols in expected.items():
        assert name in by_name, f"{name} missing from static inventory"
        assert by_name[name]["type"] == "UNIQUE"
        assert by_name[name]["columns"] == cols


# --------------------------------------------------------------------------- #
# live (scratch PostgreSQL) — exhaustive equality
# --------------------------------------------------------------------------- #
def test_live_0006_equals_frozen_inventory(pg_base_url: str) -> None:
    with scratch_database(pg_base_url, "minos_l2f_static_inv") as url:
        alembic_upgrade(url, _HEAD)
        engine = create_engine(normalize_database_url(url))
        try:
            with engine.connect() as conn:
                assert l2f_live_inventory(conn) == C.L2F_LIVE_INVENTORY
        finally:
            engine.dispose()


def test_schema_lifecycle_inventory_equality(pg_base_url: str) -> None:
    with scratch_database(pg_base_url, "minos_l2f_inv_lifecycle") as url:
        engine = create_engine(normalize_database_url(url))
        try:
            # 1) explicit 0005 + 2) capture normalized exhaustive 0005 state.
            alembic_upgrade(url, _PREV)
            with engine.connect() as conn:
                state_0005 = full_structural_state(conn, _SCHEMAS, _ROLES)

            # 3) 0006 + 4) live equals the frozen inventory exactly.
            alembic_upgrade(url, _HEAD)
            with engine.connect() as conn:
                assert l2f_live_inventory(conn) == C.L2F_LIVE_INVENTORY

            # 5) downgrade + 6) normalized state equals the captured 0005 state exactly.
            alembic_downgrade(url, _PREV)
            with engine.connect() as conn:
                assert full_structural_state(conn, _SCHEMAS, _ROLES) == state_0005

            # 7) re-upgrade + assert the same frozen inventory again.
            alembic_upgrade(url, _HEAD)
            with engine.connect() as conn:
                assert l2f_live_inventory(conn) == C.L2F_LIVE_INVENTORY
        finally:
            engine.dispose()
