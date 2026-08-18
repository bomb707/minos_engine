"""minos_admin owns and can administer stage objects; app roles cannot (Defect 2)."""

from __future__ import annotations

import pytest
from sqlalchemy import Connection, text
from sqlalchemy.exc import ProgrammingError

from minos_engine.storage.constants import SCHEMAS

_APP_ROLES = ("minos_live", "minos_runner", "minos_evaluator", "minos_trainer")


# --- role attributes ------------------------------------------------------------
def test_minos_admin_is_nologin_non_superuser(rollback_conn: Connection):
    row = rollback_conn.execute(
        text(
            "SELECT rolcanlogin, rolsuper, rolcreatedb, rolcreaterole, rolreplication, "
            "rolbypassrls FROM pg_roles WHERE rolname = 'minos_admin'"
        )
    ).one()
    canlogin, super_, createdb, createrole, repl, bypassrls = row
    assert canlogin is False
    assert super_ is False
    assert createdb is False
    assert createrole is False
    assert repl is False
    assert bypassrls is False


# --- ownership ------------------------------------------------------------------
def test_admin_owns_all_schemas(rollback_conn: Connection):
    owners = dict(
        rollback_conn.execute(
            text(
                "SELECT nspname, pg_get_userbyid(nspowner) FROM pg_namespace "
                "WHERE nspname = ANY(:s)"
            ),
            {"s": list(SCHEMAS)},
        ).all()
    )
    assert set(owners) == set(SCHEMAS)
    assert all(o == "minos_admin" for o in owners.values()), owners


def test_admin_owns_all_stage_tables(rollback_conn: Connection):
    owners = rollback_conn.execute(
        text(
            "SELECT schemaname || '.' || tablename, tableowner FROM pg_tables "
            "WHERE schemaname = ANY(:s)"
        ),
        {"s": list(SCHEMAS)},
    ).all()
    assert len(owners) == 10
    assert all(o[1] == "minos_admin" for o in owners), owners


def test_admin_owns_trigger_functions(rollback_conn: Connection):
    owners = dict(
        rollback_conn.execute(
            text(
                "SELECT proname, pg_get_userbyid(proowner) FROM pg_proc "
                "WHERE proname LIKE 'minos_reject_%'"
            )
        ).all()
    )
    assert owners == {
        "minos_reject_mutation": "minos_admin",
        "minos_reject_identity_change": "minos_admin",
    }


# --- admin can administer -------------------------------------------------------
def test_admin_can_create_alter_index_trigger_drop(rollback_conn: Connection):
    with rollback_conn.begin_nested():
        rollback_conn.execute(text("SET ROLE minos_admin"))
        rollback_conn.execute(text("CREATE TABLE catalog.tmp_admin_test (id int PRIMARY KEY)"))
        rollback_conn.execute(text("ALTER TABLE catalog.tmp_admin_test ADD COLUMN note text"))
        rollback_conn.execute(
            text("CREATE INDEX ix_tmp_admin_test_note ON catalog.tmp_admin_test (note)")
        )
        rollback_conn.execute(
            text(
                "CREATE TRIGGER trg_tmp_admin_test BEFORE UPDATE ON catalog.tmp_admin_test "
                "FOR EACH ROW EXECUTE FUNCTION audit.minos_reject_mutation()"
            )
        )
        rollback_conn.execute(text("DROP TRIGGER trg_tmp_admin_test ON catalog.tmp_admin_test"))
        rollback_conn.execute(text("DROP INDEX catalog.ix_tmp_admin_test_note"))
        rollback_conn.execute(text("DROP TABLE catalog.tmp_admin_test"))
        rollback_conn.execute(text("RESET ROLE"))


# --- application roles cannot administer ----------------------------------------
@pytest.mark.parametrize("role", _APP_ROLES)
def test_app_role_cannot_create_objects(rollback_conn: Connection, role: str):
    with pytest.raises(ProgrammingError), rollback_conn.begin_nested():
        rollback_conn.execute(text(f"SET ROLE {role}"))
        rollback_conn.execute(text("CREATE TABLE catalog.sneaky (id int)"))


def test_no_app_role_is_member_of_admin(rollback_conn: Connection):
    n = rollback_conn.execute(
        text(
            "SELECT count(*) FROM pg_auth_members m "
            "JOIN pg_roles r ON m.roleid = r.oid "
            "JOIN pg_roles mem ON m.member = mem.oid "
            "WHERE r.rolname = 'minos_admin' AND mem.rolname LIKE 'minos_%'"
        )
    ).scalar()
    assert n == 0  # application roles can never SET ROLE minos_admin


def test_public_cannot_execute_stage_functions(rollback_conn: Connection):
    for fn in ("audit.minos_reject_mutation()", "experiments.minos_reject_identity_change()"):
        has = rollback_conn.execute(
            text("SELECT has_function_privilege('public', :f, 'EXECUTE')"), {"f": fn}
        ).scalar()
        assert has is False, fn
