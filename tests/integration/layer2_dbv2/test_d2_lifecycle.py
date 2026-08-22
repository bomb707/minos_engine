"""DB-V2 D2: the migration lifecycle on scratch PostgreSQL, and V1 preservation.

``0008 -> 0009 -> 0008 -> 0009`` end to end, plus the truthful operational lineage
``0005 -> 0006 -> 0007 -> 0008 -> 0009``. Every V1 object is fingerprinted before the migration
and required to be unchanged afterwards; no production constraint is disabled anywhere.
"""

from __future__ import annotations

from typing import Any

import pytest

from minos_engine.storage import dbv2_migration_contract as contract

from .conftest import (
    alembic_downgrade,
    alembic_upgrade,
    dbv2_scratch_database,
    fingerprint,
    provision_roles,
    revision_of,
    rows,
    scalar,
)

pytestmark = [pytest.mark.integration, pytest.mark.postgres, pytest.mark.migration]

_D2 = contract.REVISION
_D1 = contract.DOWN_REVISION
LINEAGE = (
    "0005_l2e_feature_view",
    "0006_l2f_experiment_plan",
    "0007_l2f_job_claiming",
    "0008_l2f_execution_results",
    "0009_dbv2_shadow_schema",
)


def _dbv2_object_counts(url: str) -> dict[str, int]:
    return {
        "schemas": int(
            scalar(url, "SELECT count(*) FROM pg_namespace WHERE nspname LIKE 'dbv2\\_%'")
        ),
        "tables": int(
            scalar(
                url,
                "SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
                "WHERE n.nspname LIKE 'dbv2\\_%' AND c.relkind = 'r'",
            )
        ),
        "functions": int(
            scalar(
                url,
                "SELECT count(*) FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace "
                "WHERE n.nspname LIKE 'dbv2\\_%'",
            )
        ),
        "triggers": int(
            scalar(
                url,
                "SELECT count(*) FROM pg_trigger t JOIN pg_class c ON c.oid = t.tgrelid "
                "JOIN pg_namespace n ON n.oid = c.relnamespace "
                "WHERE n.nspname LIKE 'dbv2\\_%' AND NOT t.tgisinternal",
            )
        ),
    }


#: derived from the frozen migration contract, never from a literal in this file.
EXPECTED_COUNTS = {
    "schemas": len(contract.SHADOW_SCHEMAS),
    "tables": len(contract.SHADOW_TABLES),
    "functions": len(contract.FUNCTIONS),
    "triggers": len(contract.TRIGGERS),
}


def _diff(before: dict[str, Any], after: dict[str, Any], *, ignore_alembic_row: bool) -> list[str]:
    problems: list[str] = []
    for key in sorted(before):
        left, right = before[key], after[key]
        if ignore_alembic_row and key in {"row_hashes", "row_counts"}:
            left = {k: v for k, v in left.items() if k != "public.alembic_version"}
            right = {k: v for k, v in right.items() if k != "public.alembic_version"}
        if left != right:
            problems.append(key)
    return problems


def test_the_full_lifecycle_and_v1_preservation(dbv2_cluster_url: str) -> None:
    """H1-H8 and I: upgrade, verify, downgrade, verify restoration, re-upgrade, verify identity."""
    provision_roles(dbv2_cluster_url)
    with dbv2_scratch_database(dbv2_cluster_url, "minos_dbv2_lifecycle") as url:
        # ---- H1/H2: base -> 0008, then the complete V1 fingerprint --------------------
        alembic_upgrade(url, _D1)
        assert revision_of(url) == _D1
        baseline = fingerprint(url)
        assert baseline["relations"], "the V1 fingerprint captured nothing"
        assert _dbv2_object_counts(url) == dict.fromkeys(EXPECTED_COUNTS, 0)

        # ---- H3/H4: 0008 -> 0009, and the complete D2 schema ---------------------------
        alembic_upgrade(url, _D2)
        assert revision_of(url) == _D2
        assert _dbv2_object_counts(url) == EXPECTED_COUNTS
        after_upgrade = fingerprint(url)
        first_shadow = fingerprint(url, tuple(contract.SHADOW_SCHEMAS))
        assert _diff(baseline, after_upgrade, ignore_alembic_row=True) == [], (
            "0009 changed a V1 object"
        )
        assert scalar(url, "SELECT version_num FROM alembic_version") == _D2

        # ---- H5/H6: 0009 -> 0008, and exact restoration --------------------------------
        alembic_downgrade(url, _D1)
        assert revision_of(url) == _D1
        assert _dbv2_object_counts(url) == dict.fromkeys(EXPECTED_COUNTS, 0)
        after_downgrade = fingerprint(url)
        assert _diff(baseline, after_downgrade, ignore_alembic_row=True) == [], (
            "the downgrade did not restore the exact 0008 fingerprint"
        )

        # ---- H7/H8: 0008 -> 0009 again, identical re-creation ---------------------------
        alembic_upgrade(url, _D2)
        second_shadow = fingerprint(url, tuple(contract.SHADOW_SCHEMAS))
        assert _dbv2_object_counts(url) == EXPECTED_COUNTS
        for key in sorted(first_shadow):
            assert first_shadow[key] == second_shadow[key], f"{key} differs after re-creation"


def test_the_truthful_operational_lineage(dbv2_cluster_url: str) -> None:
    """H: 0005 -> 0006 -> 0007 -> 0008 -> 0009, with zero L2-F business rows at every stop."""
    provision_roles(dbv2_cluster_url)
    with dbv2_scratch_database(dbv2_cluster_url, "minos_dbv2_lineage") as url:
        alembic_upgrade(url, LINEAGE[0])
        assert revision_of(url) == LINEAGE[0]
        for revision in LINEAGE[1:]:
            alembic_upgrade(url, revision)
            assert revision_of(url) == revision
            l2f_tables = rows(
                url,
                "SELECT table_schema, table_name FROM information_schema.tables "
                "WHERE table_name LIKE 'l2f\\_%' ORDER BY 1, 2",
            )
            for schema, table in l2f_tables:
                count = scalar(url, f'SELECT count(*) FROM "{schema}"."{table}"')
                assert count == 0, f"{schema}.{table} holds {count} rows at {revision}"
        assert _dbv2_object_counts(url) == EXPECTED_COUNTS
        # nothing published, enqueued or executed: the shadow schema is created EMPTY
        for table in contract.SHADOW_TABLES:
            assert scalar(url, f"SELECT count(*) FROM {table}") == 0, f"{table} is not empty"


def test_downgrade_removes_only_db_v2_objects(dbv2_cluster_url: str) -> None:
    """J26."""
    provision_roles(dbv2_cluster_url)
    with dbv2_scratch_database(dbv2_cluster_url, "minos_dbv2_downgrade") as url:
        alembic_upgrade(url, _D1)
        before = fingerprint(url)
        alembic_upgrade(url, _D2)
        alembic_downgrade(url, _D1)
        after = fingerprint(url)
        assert _diff(before, after, ignore_alembic_row=True) == []
        assert set(contract.DOWNGRADE_DROPS) == set(contract.SHADOW_SCHEMAS)
        assert _dbv2_object_counts(url) == dict.fromkeys(EXPECTED_COUNTS, 0)


def test_downgrade_drops_no_cluster_role(dbv2_cluster_url: str) -> None:
    """J27: roles are cluster objects and outlive every migration that used them."""
    provision_roles(dbv2_cluster_url)
    with dbv2_scratch_database(dbv2_cluster_url, "minos_dbv2_roles") as url:
        alembic_upgrade(url, _D2)
        before = rows(
            url,
            "SELECT rolname, rolcanlogin, rolsuper, rolcreaterole, rolcreatedb FROM pg_roles "
            "WHERE rolname LIKE 'minos\\_%' ORDER BY 1",
        )
        alembic_downgrade(url, _D1)
        after = rows(
            url,
            "SELECT rolname, rolcanlogin, rolsuper, rolcreaterole, rolcreatedb FROM pg_roles "
            "WHERE rolname LIKE 'minos\\_%' ORDER BY 1",
        )
        assert before == after
        assert {r[0] for r in after} >= set(contract.REQUIRED_ROLES)


def test_reupgrade_produces_the_identical_schema(dbv2_cluster_url: str) -> None:
    """J28: a second upgrade is not merely successful, it is the same schema."""
    provision_roles(dbv2_cluster_url)
    with dbv2_scratch_database(dbv2_cluster_url, "minos_dbv2_reupgrade") as url:
        alembic_upgrade(url, _D2)
        first = fingerprint(url, tuple(contract.SHADOW_SCHEMAS))
        alembic_downgrade(url, _D1)
        alembic_upgrade(url, _D2)
        second = fingerprint(url, tuple(contract.SHADOW_SCHEMAS))
        for key in sorted(first):
            assert first[key] == second[key], f"{key} differs after re-upgrade"
