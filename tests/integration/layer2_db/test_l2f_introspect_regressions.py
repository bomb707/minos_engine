"""F3-A closure regression tests for the normalized introspector.

* duplicate index names in two schemas must not cross-contaminate their key definitions
  (the introspector must scope index-key lookup by index OID, not by bare index name); and
* raw vs effective ACL representation is correct on a live 0006 — the L2-F trigger function's
  NULL raw ACL surfaces effective PUBLIC EXECUTE, the five owned L2-F tables grant nothing
  effective to application roles or PUBLIC, and the sealed-test views remain ungranted.
"""

from __future__ import annotations

from sqlalchemy import create_engine, text

from minos_engine.storage.database import normalize_database_url
from tests.integration.layer2_db.conftest import alembic_upgrade, scratch_database
from tests.integration.layer2_db.l2f_introspect import (
    introspect_functions,
    introspect_indexes,
    introspect_relation,
    introspect_table,
    introspect_view,
)

_HEAD = "0006_l2f_experiment_plan"
_APP_ROLES = {"minos_live", "minos_runner", "minos_trainer", "minos_evaluator"}
_OWNED = [
    ("experiments", "l2f_experiment_plans"),
    ("experiments", "l2f_experiment_plan_members"),
    ("experiments", "l2f_config_payloads"),
    ("experiments", "l2f_experiment_plan_configs"),
    ("experiments", "l2f_experiment_jobs"),
]
_SEALED_TEST_VIEWS = [
    ("evaluation", "sealed_test_profile_members"),
    ("evaluation", "sealed_test_epoch_allocations"),
]


def test_duplicate_index_name_across_schemas_does_not_cross_contaminate(pg_base_url: str) -> None:
    with scratch_database(pg_base_url, "minos_l2f_dupidx") as url:
        engine = create_engine(normalize_database_url(url))
        try:
            with engine.begin() as c:
                c.execute(text("CREATE SCHEMA s1"))
                c.execute(text("CREATE SCHEMA s2"))
                c.execute(text("CREATE TABLE s1.t1 (a int, b int)"))
                c.execute(text("CREATE TABLE s2.t2 (c int, d int)"))
                # SAME index name in both schemas, DIFFERENT key columns.
                c.execute(text("CREATE INDEX idx_dup ON s1.t1 (a)"))
                c.execute(text("CREATE INDEX idx_dup ON s2.t2 (c, d)"))
            with engine.connect() as conn:
                idx = introspect_indexes(conn, [("s1", "t1"), ("s2", "t2")])
            by_table = {(i["schema"], i["table"]): i for i in idx if i["name"] == "idx_dup"}
            assert set(by_table) == {("s1", "t1"), ("s2", "t2")}
            # key definitions must reflect each table's OWN index, not the other's.
            k1 = by_table[("s1", "t1")]["key_definitions"]
            k2 = by_table[("s2", "t2")]["key_definitions"]
            assert k1 == ["a"], k1
            assert k2 == ["c", "d"], k2
            assert k1 != k2
        finally:
            engine.dispose()


def test_sequence_acl_uses_sequence_acldefault(pg_base_url: str) -> None:
    """A sequence with a NULL raw ACL must expand effective privileges via acldefault('s', ...)
    (SELECT/UPDATE/USAGE) — never the relation default's table-only privileges."""
    with scratch_database(pg_base_url, "minos_l2f_seqacl") as url:
        engine = create_engine(normalize_database_url(url))
        try:
            with engine.begin() as c:
                c.execute(text("CREATE SCHEMA sq"))
                c.execute(text("CREATE SEQUENCE sq.seq1"))
            with engine.connect() as conn:
                r = introspect_relation(conn, "sq", "seq1", "S")
            assert r["kind"] == "sequence"
            assert r["acl_is_default"] is True and r["acl_raw"] == []
            privs = {e["privilege"] for e in r["acl_effective"]}
            assert {"SELECT", "UPDATE", "USAGE"} <= privs
            table_only = {"INSERT", "DELETE", "TRIGGER", "TRUNCATE", "REFERENCES"}
            assert not (privs & table_only), f"sequence effective ACL has table-only privs: {privs}"
        finally:
            engine.dispose()


def test_effective_acls_on_live_0006(pg_base_url: str) -> None:
    with scratch_database(pg_base_url, "minos_l2f_acl") as url:
        alembic_upgrade(url, _HEAD)
        engine = create_engine(normalize_database_url(url))
        try:
            with engine.connect() as conn:
                # 1) trigger function: NULL raw acl -> effective PUBLIC EXECUTE.
                jf = introspect_functions(
                    conn, [("experiments", "minos_l2f_reject_job_identity_change")]
                )[0]
                assert jf["acl_is_default"] is True
                assert jf["acl_raw"] == []
                eff = {(e["grantee"], e["privilege"]) for e in jf["acl_effective"]}
                assert ("PUBLIC", "EXECUTE") in eff

                # 2) owned L2-F tables: no effective application-role or PUBLIC privilege.
                for schema, table in _OWNED:
                    t = introspect_table(conn, schema, table)
                    grantees = {e["grantee"] for e in t["acl_effective"]}
                    assert grantees == {"minos_admin"}, f"{table}: {grantees}"
                    assert not (grantees & (_APP_ROLES | {"PUBLIC"}))

                # 3) sealed-test views remain ungranted (owner-only, no app-role/PUBLIC).
                for schema, view in _SEALED_TEST_VIEWS:
                    v = introspect_view(conn, schema, view)
                    grantees = {e["grantee"] for e in v["acl_effective"]}
                    assert grantees == {"minos_admin"}, f"{view}: {grantees}"
                    assert not (grantees & (_APP_ROLES | {"PUBLIC"}))
        finally:
            engine.dispose()
