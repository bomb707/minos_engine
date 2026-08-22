"""DB-V2 D2: what migration 0009 actually created, read back from the PostgreSQL catalogs.

Every assertion here compares the LIVE schema against the frozen migration contract in
``minos_engine.storage.dbv2_migration_contract``. Nothing is asserted from the migration's source
text: the contract says what must exist and the catalogs say what does.
"""

from __future__ import annotations

import pytest

from minos_engine.storage import dbv2_migration_contract as contract

from .conftest import rows, scalar

pytestmark = [pytest.mark.integration, pytest.mark.postgres, pytest.mark.migration]


# --------------------------------------------------------------------------- #
# J1-J5: the namespace
# --------------------------------------------------------------------------- #
def test_exactly_seven_shadow_schemas_exist(dbv2_url: str) -> None:
    """J1."""
    found = [
        r[0]
        for r in rows(
            dbv2_url,
            "SELECT nspname FROM pg_namespace WHERE nspname LIKE 'dbv2\\_%' ORDER BY nspname",
        )
    ]
    assert tuple(found) == contract.SHADOW_SCHEMAS
    assert len(found) == 7


def test_exactly_thirty_seven_shadow_tables_exist(dbv2_url: str) -> None:
    """J2."""
    found = [
        f"{r[0]}.{r[1]}"
        for r in rows(
            dbv2_url,
            "SELECT n.nspname, c.relname FROM pg_class c "
            "JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname LIKE 'dbv2\\_%' AND c.relkind = 'r' "
            "ORDER BY n.nspname, c.relname",
        )
    ]
    assert tuple(found) == contract.SHADOW_TABLES
    assert len(found) == 37


def test_no_retired_schema_exists(dbv2_url: str) -> None:
    """J3: D2 renames nothing, so the retirement namespace must be empty."""
    assert (
        scalar(
            dbv2_url,
            "SELECT count(*) FROM pg_namespace WHERE nspname LIKE 'v1\\_retired\\_%'",
        )
        == 0
    )


def test_the_alembic_table_is_shared_and_not_duplicated(dbv2_url: str) -> None:
    """J4."""
    found = rows(
        dbv2_url,
        "SELECT n.nspname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE c.relname = 'alembic_version' AND c.relkind = 'r' ORDER BY n.nspname",
    )
    assert [r[0] for r in found] == ["public"]
    assert scalar(dbv2_url, "SELECT version_num FROM alembic_version") == contract.REVISION


#: the nine canonical identities V1 already occupies, which is exactly why the shadow namespace
#: exists. Each must coexist with its shadow twin, as two independent relations.
COLLIDING_IDENTITIES = (
    "audit.events",
    "catalog.artifacts",
    "catalog.datasets",
    "profiling.bam_profiles",
    "profiling.feature_matrices",
    "profiling.feature_matrix_members",
    "profiling.feature_sets",
    "profiling.profile_snapshot_members",
    "profiling.profile_snapshots",
)


def test_no_logical_name_collision_occurred(dbv2_url: str) -> None:
    """J5: the nine names V1 occupies still resolve to V1, and their shadow twins are separate."""
    for canonical in COLLIDING_IDENTITIES:
        shadow = f"dbv2_{canonical}"
        canonical_oid = scalar(dbv2_url, "SELECT to_regclass(:t)::oid", t=canonical)
        shadow_oid = scalar(dbv2_url, "SELECT to_regclass(:t)::oid", t=shadow)
        assert canonical_oid, f"{canonical} was removed by 0009"
        assert shadow_oid, f"{shadow} was not created"
        assert canonical_oid != shadow_oid, f"{canonical} and {shadow} are the same relation"
    # and nothing 0009 created lives outside the shadow namespace
    created = {
        f"{r[0]}.{r[1]}"
        for r in rows(
            dbv2_url,
            "SELECT n.nspname, c.relname FROM pg_class c "
            "JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE c.relkind = 'r' AND c.relowner = 'minos_owner'::regrole",
        )
    }
    assert created == set(contract.SHADOW_TABLES)


# --------------------------------------------------------------------------- #
# J6-J7: columns and constraints
# --------------------------------------------------------------------------- #
_TYPE_ALIASES = {
    "timestamptz": "timestamp with time zone",
    "char(64)": "character(64)",
    "varchar(32)": "character varying(32)",
}


def test_every_column_matches_type_nullability_and_default(dbv2_url: str) -> None:
    """J6."""
    live = rows(
        dbv2_url,
        "SELECT c.table_schema || '.' || c.table_name, c.column_name, "
        "       format_type(a.atttypid, a.atttypmod), c.is_nullable, c.column_default "
        "FROM information_schema.columns c "
        "JOIN pg_class cl ON cl.relname = c.table_name "
        "JOIN pg_namespace n ON n.oid = cl.relnamespace AND n.nspname = c.table_schema "
        "JOIN pg_attribute a ON a.attrelid = cl.oid AND a.attname = c.column_name "
        "WHERE c.table_schema LIKE 'dbv2\\_%' "
        "ORDER BY 1, c.ordinal_position",
    )
    by_table: dict[str, list[tuple[str, str, bool, str | None]]] = {}
    for table, column, data_type, nullable, default in live:
        by_table.setdefault(table, []).append((column, data_type, nullable == "YES", default))
    assert set(by_table) == set(contract.TABLE_COLUMNS)
    for table, declared in sorted(contract.TABLE_COLUMNS.items()):
        actual = by_table[table]
        assert [c[0] for c in actual] == [c[0] for c in declared], f"{table}: column order"
        for (name, want_type, want_null, want_default), (_, got_type, got_null, got_default) in zip(
            declared, actual, strict=True
        ):
            assert got_type == _TYPE_ALIASES.get(want_type, want_type), f"{table}.{name}: type"
            assert got_null is want_null, f"{table}.{name}: nullability"
            if want_default is None:
                assert got_default is None, f"{table}.{name}: unexpected default {got_default!r}"
            else:
                assert got_default is not None, f"{table}.{name}: missing default"


def test_every_constraint_and_index_matches(dbv2_url: str) -> None:
    """J7: primary keys, unique constraints, foreign keys, checks and indexes."""
    constraints = rows(
        dbv2_url,
        "SELECT n.nspname || '.' || rel.relname, con.conname, con.contype, "
        "       COALESCE(fn.nspname || '.' || fr.relname, '') "
        "FROM pg_constraint con "
        "JOIN pg_class rel ON rel.oid = con.conrelid "
        "JOIN pg_namespace n ON n.oid = rel.relnamespace "
        "LEFT JOIN pg_class fr ON fr.oid = con.confrelid "
        "LEFT JOIN pg_namespace fn ON fn.oid = fr.relnamespace "
        "WHERE n.nspname LIKE 'dbv2\\_%' ORDER BY 1, 2",
    )
    indexes = rows(
        dbv2_url,
        "SELECT schemaname || '.' || tablename, indexname FROM pg_indexes "
        "WHERE schemaname LIKE 'dbv2\\_%' ORDER BY 1, 2",
    )
    live: dict[str, dict[str, object]] = {
        table: {
            "checks": set(),
            "foreign_keys": set(),
            "unique": set(),
            "primary_key": None,
            "indexes": set(),
        }
        for table in contract.TABLE_CONSTRAINTS
    }
    for table, name, kind, target in constraints:
        entry = live[table]
        if kind == "p":
            entry["primary_key"] = name
        elif kind == "u":
            entry["unique"].add(name)  # type: ignore[union-attr]
        elif kind == "f":
            entry["foreign_keys"].add((name, target))  # type: ignore[union-attr]
        elif kind == "c":
            entry["checks"].add(name)  # type: ignore[union-attr]
    for table, name in indexes:
        # a PK/UNIQUE constraint also creates an index; only explicitly declared ones are compared
        live[table]["indexes"].add(name)  # type: ignore[union-attr]

    for table, declared in sorted(contract.TABLE_CONSTRAINTS.items()):
        entry = live[table]
        assert entry["primary_key"] == declared["primary_key"], f"{table}: primary key"
        assert entry["unique"] == set(declared["unique"]), f"{table}: unique constraints"
        assert entry["foreign_keys"] == set(declared["foreign_keys"]), f"{table}: foreign keys"
        assert entry["checks"] >= set(declared["checks"]), f"{table}: check constraints"
        assert entry["indexes"] >= set(declared["indexes"]), f"{table}: indexes"


def test_partial_index_predicates_are_present(dbv2_url: str) -> None:
    """J7: the snapshot index must carry the exact predicate, not just the columns."""
    definition = scalar(
        dbv2_url,
        "SELECT indexdef FROM pg_indexes WHERE schemaname = 'dbv2_catalog' "
        "AND indexname = 'ix_artifacts_operational_snapshot'",
    )
    assert "WHERE" in definition
    assert "lifecycle_state = 'active'" in definition
    assert "backup_scope = 'operational'" in definition
    assert "content_sha256, size_bytes, artifact_kind" in definition


# --------------------------------------------------------------------------- #
# J8-J9: functions and triggers
# --------------------------------------------------------------------------- #
def test_all_thirty_four_functions_match_signature_security_and_search_path(dbv2_url: str) -> None:
    """J8."""
    live = rows(
        dbv2_url,
        "SELECT n.nspname || '.' || p.proname || '(' || "
        "       pg_get_function_arguments(p.oid) || ')', p.prosecdef, "
        "       COALESCE(array_to_string(p.proconfig, ','), '') "
        "FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace "
        "WHERE n.nspname LIKE 'dbv2\\_%' ORDER BY 1",
    )
    assert len(live) == 34
    names = {signature.split("(", 1)[0] for signature, _, _ in live}
    assert names == {s.split("(", 1)[0] for s in contract.FUNCTIONS}
    definer = {signature.split("(", 1)[0] for signature, secdef, _ in live if secdef}
    assert definer == {s.split("(", 1)[0] for s in contract.SECURITY_DEFINER_FUNCTIONS}
    for signature, _, config in live:
        assert config == f"search_path={contract.SEARCH_PATH}", signature


def test_all_eighty_nine_triggers_match(dbv2_url: str) -> None:
    """J9: name, table, function, timing and event, read from pg_trigger."""
    live = rows(
        dbv2_url,
        "SELECT t.tgname, n.nspname || '.' || c.relname, "
        "       fn.nspname || '.' || p.proname, "
        "       CASE WHEN (t.tgtype & 2) <> 0 THEN 'BEFORE' ELSE 'AFTER' END, "
        "       t.tgtype "
        "FROM pg_trigger t JOIN pg_class c ON c.oid = t.tgrelid "
        "JOIN pg_namespace n ON n.oid = c.relnamespace "
        "JOIN pg_proc p ON p.oid = t.tgfoid "
        "JOIN pg_namespace fn ON fn.oid = p.pronamespace "
        "WHERE n.nspname LIKE 'dbv2\\_%' AND NOT t.tgisinternal "
        "ORDER BY 2, 1",
    )
    assert len(live) == 89
    events = {4: "INSERT", 8: "DELETE", 16: "UPDATE"}
    actual = set()
    for name, table, function, timing, tgtype in live:
        fired = " OR ".join(label for bit, label in sorted(events.items()) if tgtype & bit)
        actual.add((name, table, function, timing, fired))
    assert actual == set(contract.TRIGGERS)


def test_every_function_body_resolves_only_to_shadow_objects(dbv2_url: str) -> None:
    """J18: no body may name a canonical V1 table, in any schema-qualified position."""
    bodies = rows(
        dbv2_url,
        "SELECT n.nspname || '.' || p.proname, p.prosrc "
        "FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace "
        "WHERE n.nspname LIKE 'dbv2\\_%' ORDER BY 1",
    )
    canonical = [s.removeprefix("dbv2_") for s in contract.SHADOW_SCHEMAS]
    for name, source in bodies:
        for schema in canonical:
            for token in (f" {schema}.", f"({schema}.", f"\n{schema}.", f"FROM {schema}."):
                assert token not in source, f"{name} references canonical {schema}"
    # and the tables they DO name all exist in the shadow namespace
    joined = "\n".join(source for _, source in bodies)
    for table in contract.SHADOW_TABLES:
        if table in joined:
            assert scalar(dbv2_url, "SELECT to_regclass(:t) IS NOT NULL", t=table)
