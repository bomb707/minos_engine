"""§5: the private Core mappings (l2f_tables) match the real schema created by 0006.

Fails if the migration changes without the Core mapping (or vice-versa). External target
stubs are excluded from the owned-table comparison. Triggers/ownership/grants are the
migration/introspection contract's responsibility, not the Core mapping's.
"""

from __future__ import annotations

import pytest
import sqlalchemy as sa
from sqlalchemy import create_engine, inspect

from minos_engine.storage.database import normalize_database_url
from minos_engine.storage.l2f_tables import L2F_OWNED_TABLES
from tests.integration.layer2_db.conftest import alembic_upgrade, scratch_database

_SCHEMA = "experiments"


def _mapping_view(table: sa.Table) -> dict[str, object]:
    cols = {c.name: bool(c.nullable) for c in table.columns}
    pk = tuple(c.name for c in table.primary_key.columns)
    fks: dict[str, tuple] = {}
    for fk in table.foreign_key_constraints:
        assert fk.name
        local = tuple(fk.column_keys)
        elems = list(fk.elements)
        target_tbl = elems[0].column.table
        target = f"{target_tbl.schema}.{target_tbl.name}"
        tcols = tuple(e.column.name for e in elems)
        fks[fk.name] = (local, target, tcols)
    uniq = {
        c.name: tuple(col.name for col in c.columns)
        for c in table.constraints
        if isinstance(c, sa.UniqueConstraint) and c.name
    }
    checks = {c.name for c in table.constraints if isinstance(c, sa.CheckConstraint) and c.name}
    idx = {i.name: tuple(col.name for col in i.columns) for i in table.indexes if i.name}
    return {"cols": cols, "pk": pk, "fks": fks, "uniq": uniq, "checks": checks, "idx": idx}


def _db_view(insp: sa.Inspector, name: str) -> dict[str, object]:
    cols = {c["name"]: bool(c["nullable"]) for c in insp.get_columns(name, schema=_SCHEMA)}
    pk = tuple(insp.get_pk_constraint(name, schema=_SCHEMA)["constrained_columns"])
    fks: dict[str, tuple] = {}
    for fk in insp.get_foreign_keys(name, schema=_SCHEMA):
        assert fk["name"]
        target = f"{fk['referred_schema']}.{fk['referred_table']}"
        fks[fk["name"]] = (tuple(fk["constrained_columns"]), target, tuple(fk["referred_columns"]))
    uniq = {
        u["name"]: tuple(u["column_names"])
        for u in insp.get_unique_constraints(name, schema=_SCHEMA)
        if u["name"]
    }
    checks = {c["name"] for c in insp.get_check_constraints(name, schema=_SCHEMA) if c["name"]}
    # exclude indexes that merely back a UNIQUE/PK constraint (SQLAlchemy Table.indexes
    # holds only EXPLICIT Index() objects; PostgreSQL reports constraint-backed ones too).
    idx = {
        i["name"]: tuple(i["column_names"])
        for i in insp.get_indexes(name, schema=_SCHEMA)
        if i["name"] and not i.get("duplicates_constraint")
    }
    return {"cols": cols, "pk": pk, "fks": fks, "uniq": uniq, "checks": checks, "idx": idx}


@pytest.fixture(scope="module")
def upgraded_url(pg_base_url: str):
    with scratch_database(pg_base_url, "minos_l2f_parity") as url:
        alembic_upgrade(url, "0006_l2f_experiment_plan")
        yield url


def test_core_mappings_match_migration_schema(upgraded_url: str) -> None:
    engine = create_engine(normalize_database_url(upgraded_url))
    try:
        insp = inspect(engine)
        for table in L2F_OWNED_TABLES:
            mv = _mapping_view(table)
            dv = _db_view(insp, table.name)
            assert mv["cols"] == dv["cols"], f"{table.name}: columns/nullability differ"
            assert set(mv["pk"]) == set(dv["pk"]), f"{table.name}: PK differs"
            assert mv["fks"] == dv["fks"], f"{table.name}: FKs differ"
            assert mv["uniq"] == dv["uniq"], f"{table.name}: UNIQUEs differ"
            assert mv["checks"] == dv["checks"], f"{table.name}: CHECKs differ"
            assert mv["idx"] == dv["idx"], f"{table.name}: indexes differ"
    finally:
        engine.dispose()
