"""F3-A: the private Core mappings (l2f_tables) produce the SAME schema as migration 0006.

Strengthened (F3-A closure) to a full mapping-relevant comparison. Both sides are built and
introspected identically with the normalized introspector: one scratch database is upgraded by
alembic to ``0006``; a second scratch database materializes the private ``l2f_metadata`` via
``create_all`` (into throwaway schemas — never ``Base.metadata``, never the operational DB).
For the five owned tables the test asserts equality of:

* ordered columns — name, exact PostgreSQL type (format_type), nullability, normalized server
  default, identity/generated attributes, collation;
* every constraint — PK/UNIQUE/CHECK/FK by name, ordered columns, full normalized
  ``pg_get_constraintdef`` (so CHECK expressions and FK targets/columns match), and FK options
  (match type, ON UPDATE/DELETE, deferrable/deferred/validated);
* every index — name and full normalized ``pg_get_indexdef``.

Triggers, ownership and grants are deliberately out of scope for the Core mapping (they are the
migration/live-inventory responsibility) — the mapping never declares them.
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import Connection, create_engine, text

from minos_engine.storage.database import normalize_database_url
from minos_engine.storage.l2f_tables import L2F_OWNED_TABLES, l2f_metadata
from tests.integration.layer2_db.conftest import alembic_upgrade, scratch_database
from tests.integration.layer2_db.l2f_introspect import (
    introspect_constraints,
    introspect_indexes,
    introspect_table,
)

_HEAD = "0006_l2f_experiment_plan"
_OWNED = [("experiments", t.name) for t in L2F_OWNED_TABLES]


def _owned_view(conn: Connection) -> dict[str, Any]:
    """Mapping-relevant schema view of the five owned tables (no owner/acl/persistence)."""
    return {
        "columns": {f"{s}.{t}": introspect_table(conn, s, t)["columns"] for s, t in _OWNED},
        "constraints": introspect_constraints(conn, _OWNED),
        "indexes": introspect_indexes(conn, _OWNED),
    }


@pytest.fixture(scope="module")
def migration_view(pg_base_url: str) -> dict[str, Any]:
    with scratch_database(pg_base_url, "minos_l2f_parity_mig") as url:
        alembic_upgrade(url, _HEAD)
        engine = create_engine(normalize_database_url(url))
        try:
            with engine.connect() as conn:
                return _owned_view(conn)
        finally:
            engine.dispose()


@pytest.fixture(scope="module")
def mapping_view(pg_base_url: str) -> dict[str, Any]:
    with scratch_database(pg_base_url, "minos_l2f_parity_map") as url:
        engine = create_engine(normalize_database_url(url))
        try:
            with engine.begin() as conn:
                for schema in ("experiments", "profiling", "catalog"):
                    conn.execute(text(f"CREATE SCHEMA IF NOT EXISTS {schema}"))
            # materialize the PRIVATE Core metadata only, in an isolated throwaway database.
            l2f_metadata.create_all(engine)
            with engine.connect() as conn:
                return _owned_view(conn)
        finally:
            engine.dispose()


def test_core_mapping_columns_match_migration(
    migration_view: dict[str, Any], mapping_view: dict[str, Any]
) -> None:
    assert mapping_view["columns"] == migration_view["columns"]


def test_core_mapping_constraints_match_migration(
    migration_view: dict[str, Any], mapping_view: dict[str, Any]
) -> None:
    # includes PK/UNIQUE/CHECK/FK names, ordered columns, full definitions and FK options.
    assert mapping_view["constraints"] == migration_view["constraints"]


def test_core_mapping_indexes_match_migration(
    migration_view: dict[str, Any], mapping_view: dict[str, Any]
) -> None:
    assert mapping_view["indexes"] == migration_view["indexes"]
