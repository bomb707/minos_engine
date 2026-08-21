"""F3-A closure: exhaustive static/live 0006 inventory equality + frozen contract identities.

Proves the frozen static inventory (pending owner acceptance) in ``l2f_migration_contract`` is
exact and complete against the deployed schema, with raw + effective ACLs:

* the migration file byte SHA and the domain-separated contract hash are frozen and recompute
  identically, with no self-reference and with per-section mutation sensitivity across every
  major nested section, the revision lineage, every accepted prior-migration SHA, the migration
  SHA and the static constants;
* the static inventory independently enumerates the owned tables, columns, constraints
  (including the three overlapping member UNIQUE constraints), indexes, composite targets,
  triggers and the job function, with the correct effective ACLs (the job function's NULL raw
  ACL surfaces its implicit PUBLIC EXECUTE; no owned table grants any effective application-role
  or PUBLIC privilege);
* live 0006 (scratch PG) equals the frozen live inventory exactly; and
* an explicit 0005 -> 0006 -> 0005 -> 0006 schema lifecycle re-derives the frozen inventory at
  0006 and restores the captured normalized 0005 state (all MINOS schemas) exactly.
"""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
from typing import Any

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
_ROLES = ["minos_admin", "minos_live", "minos_runner", "minos_trainer", "minos_evaluator"]

_FROZEN_MIGRATION_SHA = "1eb3a12b502a5f247a2dc662642fd71931dcada815923e95d18504220445c3c6"
_FROZEN_CONTRACT_HASH = "c7a2e978857830ccff67821ded1196472d5f38baacb19a64352ec686ce74916b"


def _mutate(value: Any) -> Any:
    if isinstance(value, list):
        return [*value, "__mutation_probe__"]
    if isinstance(value, dict):
        return {**value, "__mutation_probe__": 1}
    if isinstance(value, bool):
        return not value
    return f"{value}__mutation_probe__"


def _hash_with_inventory(inventory: dict[str, Any]) -> str:
    return C.compute_contract_hash(migration_sha256=_FROZEN_MIGRATION_SHA, inventory=inventory)


# --------------------------------------------------------------------------- #
# frozen identities
# --------------------------------------------------------------------------- #
def test_migration_file_byte_sha256_is_frozen() -> None:
    assert C.compute_migration_sha256() == C.L2F_MIGRATION_SHA256 == _FROZEN_MIGRATION_SHA


def test_prior_migrations_0001_0005_byte_identical() -> None:
    for rel, expected in C.ACCEPTED_PRIOR_MIGRATION_SHAS.items():
        assert hashlib.sha256(Path(rel).read_bytes()).hexdigest() == expected, f"{rel} changed"


def test_contract_hash_frozen_and_recomputes() -> None:
    assert C.L2F_CONTRACT_HASH == _FROZEN_CONTRACT_HASH
    assert C.compute_contract_hash(migration_sha256=C.L2F_MIGRATION_SHA256) == C.L2F_CONTRACT_HASH


def test_contract_hash_has_no_self_reference() -> None:
    preimage = C.contract_preimage(migration_sha256=C.L2F_MIGRATION_SHA256)
    assert C.L2F_CONTRACT_HASH not in json.dumps(preimage, sort_keys=True)


# --------------------------------------------------------------------------- #
# mutation matrix — every preimage input must change the hash
# --------------------------------------------------------------------------- #
def test_hash_sensitive_to_migration_sha() -> None:
    assert C.compute_contract_hash(migration_sha256="0" * 64) != C.L2F_CONTRACT_HASH


def test_hash_sensitive_to_revision_and_down_revision() -> None:
    assert (
        C.compute_contract_hash(migration_sha256=C.L2F_MIGRATION_SHA256, revision="x")
        != C.L2F_CONTRACT_HASH
    )
    assert (
        C.compute_contract_hash(migration_sha256=C.L2F_MIGRATION_SHA256, down_revision="x")
        != C.L2F_CONTRACT_HASH
    )


def test_hash_sensitive_to_every_prior_migration_sha() -> None:
    for key in C.ACCEPTED_PRIOR_MIGRATION_SHAS:
        mutated = dict(C.ACCEPTED_PRIOR_MIGRATION_SHAS)
        mutated[key] = "0" * 64
        got = C.compute_contract_hash(
            migration_sha256=C.L2F_MIGRATION_SHA256, prior_migration_shas=mutated
        )
        assert got != C.L2F_CONTRACT_HASH, f"hash ignores prior migration {key!r}"


def test_hash_sensitive_to_every_top_level_inventory_section() -> None:
    for key, value in C.L2F_STATIC_INVENTORY.items():
        mutated = copy.deepcopy(C.L2F_STATIC_INVENTORY)
        mutated[key] = _mutate(value)
        assert _hash_with_inventory(mutated) != C.L2F_CONTRACT_HASH, f"hash ignores {key!r}"


def test_hash_sensitive_to_every_nested_live_section() -> None:
    for key, value in C.L2F_LIVE_INVENTORY.items():
        mutated = copy.deepcopy(C.L2F_STATIC_INVENTORY)
        mutated["live"][key] = _mutate(value)
        assert _hash_with_inventory(mutated) != C.L2F_CONTRACT_HASH, f"hash ignores live.{key!r}"


def test_hash_sensitive_to_effective_acl_entries() -> None:
    mutated = copy.deepcopy(C.L2F_STATIC_INVENTORY)
    mutated["live"]["job_function"][0]["acl_effective"] = []
    assert _hash_with_inventory(mutated) != C.L2F_CONTRACT_HASH
    mutated2 = copy.deepcopy(C.L2F_STATIC_INVENTORY)
    first = next(iter(mutated2["live"]["owned_tables"]))
    mutated2["live"]["owned_tables"][first]["acl_effective"] = []
    assert _hash_with_inventory(mutated2) != C.L2F_CONTRACT_HASH


def test_hash_sensitive_to_static_constants() -> None:
    for key in ("config_payload_schema", "config_payload_media_type", "plan_logical_identity"):
        mutated = copy.deepcopy(C.L2F_STATIC_INVENTORY)
        mutated[key] = _mutate(mutated[key])
        assert _hash_with_inventory(mutated) != C.L2F_CONTRACT_HASH, f"hash ignores {key!r}"


# --------------------------------------------------------------------------- #
# inventory shape + ACL facts
# --------------------------------------------------------------------------- #
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
        assert t["kind"] == "table"
        assert t["persistence"] == "permanent"
        assert t["rowsecurity"] is False and t["rowsecurity_forced"] is False
        assert t["rls_policies"] == [] and t["column_acls"] == []
        assert t["replica_identity"] == "default"
    assert live["no_app_role_grants"] is True
    assert len(live["owned_constraints"]) == 61
    assert {c["name"] for c in live["composite_targets"]} == set(
        C.L2F_STATIC_INVENTORY["composite_target_names"]
    )
    assert len(live["owned_indexes"]) == 23
    assert {t["name"] for t in live["owned_triggers"]} == set(C.L2F_TRIGGERS)
    assert len(live["job_function"]) == 1


def test_static_inventory_proves_three_member_unique_constraints() -> None:
    by_name = {c["name"]: c for c in C.L2F_LIVE_INVENTORY["owned_constraints"]}
    expected = {
        "uq_l2f_pm_plan_snapshot_member": ["plan_id", "profile_snapshot_member_id"],
        "uq_l2f_pm_plan_matrix_member": ["plan_id", "feature_matrix_member_id"],
        "uq_l2f_pm_plan_member_index": ["plan_id", "member_index"],
    }
    for name, cols in expected.items():
        assert name in by_name, f"{name} missing"
        assert by_name[name]["type"] == "UNIQUE"
        assert by_name[name]["columns"] == cols


def test_static_inventory_effective_acls() -> None:
    """The job function's NULL raw ACL surfaces PUBLIC EXECUTE; owned tables grant nothing to
    application roles or PUBLIC (effective)."""
    jf = C.L2F_LIVE_INVENTORY["job_function"][0]
    assert jf["acl_is_default"] is True
    assert jf["acl_raw"] == []
    eff = {(e["grantee"], e["privilege"]) for e in jf["acl_effective"]}
    assert ("PUBLIC", "EXECUTE") in eff
    app = {"minos_live", "minos_runner", "minos_trainer", "minos_evaluator"}
    for t in C.L2F_LIVE_INVENTORY["owned_tables"].values():
        grantees = {e["grantee"] for e in t["acl_effective"]}
        assert grantees == {"minos_admin"}, grantees
        assert not (grantees & (app | {"PUBLIC"}))


# --------------------------------------------------------------------------- #
# live equality (scratch PostgreSQL)
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
    name = "minos_l2f_inv_lifecycle"
    with scratch_database(pg_base_url, name) as url:
        engine = create_engine(normalize_database_url(url))
        try:
            alembic_upgrade(url, _PREV)
            with engine.connect() as conn:
                state_0005 = full_structural_state(conn, _ROLES, dbname=name)

            alembic_upgrade(url, _HEAD)
            with engine.connect() as conn:
                assert l2f_live_inventory(conn) == C.L2F_LIVE_INVENTORY

            alembic_downgrade(url, _PREV)
            with engine.connect() as conn:
                assert full_structural_state(conn, _ROLES, dbname=name) == state_0005

            alembic_upgrade(url, _HEAD)
            with engine.connect() as conn:
                assert l2f_live_inventory(conn) == C.L2F_LIVE_INVENTORY
        finally:
            engine.dispose()
