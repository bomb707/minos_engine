"""DB-V2 D2: the grant posture, the role preflight and the transaction-scoped elevation.

Everything here is read from ``pg_class.relacl`` / ``pg_namespace.nspacl`` / ``pg_proc.proacl`` or
observed by running the migration, never from the migration's source text. The one exception is
the credential/path leakage scan, where scanning the committed bytes IS the proof required.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import create_engine, text

from minos_engine.storage import dbv2_migration_contract as contract
from minos_engine.storage.database import normalize_database_url

from .conftest import (
    ROLE_CONFIGURATION,
    alembic_upgrade,
    dbv2_scratch_database,
    provision_roles,
    rows,
    scalar,
)

pytestmark = [pytest.mark.integration, pytest.mark.postgres, pytest.mark.migration]

REPO_ROOT = Path(__file__).resolve().parents[3]
MIGRATION = REPO_ROOT / "migrations" / "versions" / "0009_dbv2_shadow_schema.py"
API_REPORT = REPO_ROOT / "reports" / "database" / "MINOS_DATABASE_V2_DATABASE_API.json"

_PRIVILEGE_LETTERS = {
    "SELECT": "r",
    "INSERT": "a",
    "UPDATE": "w",
    "DELETE": "d",
    "TRUNCATE": "D",
    "REFERENCES": "x",
    "TRIGGER": "t",
    "EXECUTE": "X",
    "USAGE": "U",
}


def _d2_acl() -> dict[str, Any]:
    return dict(json.loads(API_REPORT.read_text(encoding="utf-8"))["d2_physical_acl"])


def _acl_map(url: str, sql: str) -> dict[str, dict[str, str]]:
    """object -> {grantee: privilege letters}, parsed from PostgreSQL's own aclitem text."""
    out: dict[str, dict[str, str]] = {}
    for ident, acl in rows(url, sql):
        entry: dict[str, str] = {}
        for item in re.findall(r"([^,{}]+)=([a-zA-Z*]*)/[^,{}]*", acl or ""):
            grantee = item[0] or "PUBLIC"
            entry[grantee] = item[1]
        out[ident] = entry
    return out


# --------------------------------------------------------------------------- #
# J19: the D2 physical ACL, exactly
# --------------------------------------------------------------------------- #
def test_the_d2_physical_acl_matches_exactly(dbv2_url: str) -> None:
    """J19: 780 declared records, compared against the live aclitem entries."""
    acl = _d2_acl()
    assert acl["counts"]["records"] == 780
    schemas = _acl_map(
        dbv2_url,
        "SELECT nspname, COALESCE(array_to_string(nspacl::text[], ','), '') FROM pg_namespace "
        "WHERE nspname LIKE 'dbv2\\_%'",
    )
    tables = _acl_map(
        dbv2_url,
        "SELECT n.nspname || '.' || c.relname, "
        "       COALESCE(array_to_string(c.relacl::text[], ','), '') "
        "FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE n.nspname LIKE 'dbv2\\_%' AND c.relkind = 'r'",
    )
    functions = _acl_map(
        dbv2_url,
        "SELECT n.nspname || '.' || p.proname, "
        "       COALESCE(array_to_string(p.proacl::text[], ','), '') "
        "FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace "
        "WHERE n.nspname LIKE 'dbv2\\_%'",
    )
    live = {"schema": schemas, "table": tables, "function": functions}
    checked = 0
    for record in acl["records"]:
        principal = record["principal"]
        if principal == "PUBLIC":
            continue
        entry = live[record["object_type"]].get(record["object"], {})
        held = entry.get(principal, "")
        for privilege, granted in sorted(record["privileges"].items()):
            letter = _PRIVILEGE_LETTERS[privilege]
            if granted:
                assert letter in held, f"{principal} lacks {privilege} on {record['object']}"
            elif privilege != "EXECUTE" or record["object_type"] == "function":
                assert letter not in held, (
                    f"{principal} holds undeclared {privilege} on {record['object']}"
                )
            checked += 1
        assert "*" not in held, f"{principal} holds a grant option on {record['object']}"
    # 78 objects x 9 named principals x 9 privilege keys; PUBLIC is asserted separately
    assert checked == 78 * 9 * 9


def test_public_holds_nothing_on_any_new_object(dbv2_url: str) -> None:
    """F1: PostgreSQL opens new schemas and functions to PUBLIC by default; 0009 closes them."""
    for sql in (
        "SELECT nspname, COALESCE(array_to_string(nspacl::text[], ','), '') FROM pg_namespace "
        "WHERE nspname LIKE 'dbv2\\_%'",
        "SELECT n.nspname || '.' || c.relname, "
        "       COALESCE(array_to_string(c.relacl::text[], ','), '') "
        "FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE n.nspname LIKE 'dbv2\\_%' AND c.relkind = 'r'",
        "SELECT n.nspname || '.' || p.proname, "
        "       COALESCE(array_to_string(p.proacl::text[], ','), '') "
        "FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace "
        "WHERE n.nspname LIKE 'dbv2\\_%'",
    ):
        for ident, held in _acl_map(dbv2_url, sql).items():
            assert held.get("PUBLIC", "") == "", f"PUBLIC holds {held.get('PUBLIC')} on {ident}"


def test_no_runtime_role_holds_a_ddl_privilege(dbv2_url: str) -> None:
    """F2/F3: no CREATE on a schema, no TRUNCATE, REFERENCES or TRIGGER on a table."""
    runtime = (
        "minos_planner",
        "minos_enqueue",
        "minos_runner",
        "minos_verifier",
        "minos_trainer",
        "minos_evaluator",
        "minos_live",
    )
    for role in runtime:
        for schema in contract.SHADOW_SCHEMAS:
            assert not scalar(
                dbv2_url, "SELECT has_schema_privilege(:r, :s, 'CREATE')", r=role, s=schema
            ), f"{role} holds CREATE on {schema}"
        for table in contract.SHADOW_TABLES:
            for privilege in ("TRUNCATE", "REFERENCES", "TRIGGER"):
                assert not scalar(
                    dbv2_url,
                    "SELECT has_table_privilege(:r, :t, :p)",
                    r=role,
                    t=table,
                    p=privilege,
                ), f"{role} holds {privilege} on {table}"


def test_the_runner_cannot_mutate_jobs_directly(dbv2_url: str) -> None:
    """F4."""
    assert scalar(
        dbv2_url,
        "SELECT has_table_privilege('minos_runner', 'dbv2_experiments.experiment_jobs', 'SELECT')",
    )
    for privilege in ("INSERT", "UPDATE", "DELETE"):
        assert not scalar(
            dbv2_url,
            "SELECT has_table_privilege('minos_runner', 'dbv2_experiments.experiment_jobs', :p)",
            p=privilege,
        ), privilege


def test_truth_bindings_are_readable_by_the_evaluator_alone(dbv2_url: str) -> None:
    """F5: the verifier, which otherwise reads every table, is deliberately excluded."""
    assert scalar(
        dbv2_url,
        "SELECT has_table_privilege('minos_evaluator', 'dbv2_evaluation.truth_bindings', 'SELECT')",
    )
    for role in ("minos_verifier", "minos_runner", "minos_trainer", "minos_planner", "minos_live"):
        assert not scalar(
            dbv2_url,
            "SELECT has_table_privilege(:r, 'dbv2_evaluation.truth_bindings', 'SELECT')",
            r=role,
        ), role


def test_the_verifier_is_select_only(dbv2_url: str) -> None:
    """F6."""
    readable = 0
    for table in contract.SHADOW_TABLES:
        if table == "dbv2_evaluation.truth_bindings":
            continue
        assert scalar(
            dbv2_url, "SELECT has_table_privilege('minos_verifier', :t, 'SELECT')", t=table
        ), table
        readable += 1
        for privilege in ("INSERT", "UPDATE", "DELETE", "TRUNCATE"):
            assert not scalar(
                dbv2_url,
                "SELECT has_table_privilege('minos_verifier', :t, :p)",
                t=table,
                p=privilege,
            ), f"{table}/{privilege}"
    assert readable == 36


def test_the_enqueue_role_executes_only_the_bounded_enqueue_api(dbv2_url: str) -> None:
    """F7."""
    executable = sorted(
        row[0]
        for row in rows(
            dbv2_url,
            "SELECT n.nspname || '.' || p.proname FROM pg_proc p "
            "JOIN pg_namespace n ON n.oid = p.pronamespace "
            "WHERE n.nspname LIKE 'dbv2\\_%' "
            "  AND has_function_privilege('minos_enqueue', p.oid, 'EXECUTE')",
        )
    )
    assert executable == ["dbv2_experiments.enqueue_plan_jobs"]
    for table in contract.SHADOW_TABLES:
        assert not scalar(
            dbv2_url, "SELECT has_table_privilege('minos_enqueue', :t, 'SELECT')", t=table
        ), table


# --------------------------------------------------------------------------- #
# J20: nothing shared or V1 changed
# --------------------------------------------------------------------------- #
def test_the_migration_names_no_shared_object_in_any_grant(dbv2_url: str) -> None:
    """J20: read from the executed statements, then confirmed against the live catalogs."""
    source = MIGRATION.read_text(encoding="utf-8")
    grants = [
        line.strip()
        for line in source.splitlines()
        if line.strip().startswith(("GRANT ", "REVOKE ", "ALTER DEFAULT PRIVILEGES"))
        or ('r"""GRANT' in line or 'r"""REVOKE' in line or 'r"""ALTER DEFAULT' in line)
    ]
    assert grants, "the migration applies no grants at all"
    for statement in grants:
        assert "ON DATABASE" not in statement, statement
        assert "SCHEMA public " not in statement, statement
        assert "alembic_version" not in statement, statement
        assert "dbv2_" in statement, statement


def test_the_shared_alembic_table_keeps_its_privileges(dbv2_cluster_url: str) -> None:
    """J20: captured before 0009 and compared after."""
    provision_roles(dbv2_cluster_url)
    with dbv2_scratch_database(dbv2_cluster_url, "minos_dbv2_shared") as url:
        alembic_upgrade(url, contract.DOWN_REVISION)
        before = rows(
            url,
            "SELECT COALESCE(array_to_string(c.relacl::text[], ','), '<null>') FROM pg_class c "
            "JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = 'public' AND c.relname = 'alembic_version'",
        )
        before_schema = rows(
            url,
            "SELECT COALESCE(array_to_string(nspacl::text[], ','), '<null>') FROM pg_namespace "
            "WHERE nspname = 'public'",
        )
        before_database = rows(
            url,
            "SELECT COALESCE(array_to_string(datacl::text[], ','), '<null>') FROM pg_database "
            "WHERE datname = current_database()",
        )
        alembic_upgrade(url, contract.REVISION)
        assert (
            rows(
                url,
                "SELECT COALESCE(array_to_string(c.relacl::text[], ','), '<null>') FROM pg_class c "
                "JOIN pg_namespace n ON n.oid = c.relnamespace "
                "WHERE n.nspname = 'public' AND c.relname = 'alembic_version'",
            )
            == before
        )
        assert (
            rows(
                url,
                "SELECT COALESCE(array_to_string(nspacl::text[], ','), '<null>') FROM pg_namespace "
                "WHERE nspname = 'public'",
            )
            == before_schema
        )
        assert (
            rows(
                url,
                "SELECT COALESCE(array_to_string(datacl::text[], ','), '<null>') FROM pg_database "
                "WHERE datname = current_database()",
            )
            == before_database
        )


# --------------------------------------------------------------------------- #
# J21-J23: the preflight fails closed
# --------------------------------------------------------------------------- #
def _dbv2_schema_count(url: str) -> int:
    return int(scalar(url, "SELECT count(*) FROM pg_namespace WHERE nspname LIKE 'dbv2\\_%'"))


def test_a_missing_role_fails_before_any_object_exists(isolated_cluster_url: str) -> None:
    """J21 and J23: nothing partial survives a refused migration.

    A dedicated cluster, because a required role can only be dropped where nothing depends on it.
    """
    incomplete = {r: c for r, c in ROLE_CONFIGURATION.items() if r != "minos_verifier"}
    provision_roles(isolated_cluster_url, incomplete)
    with dbv2_scratch_database(isolated_cluster_url, "minos_dbv2_missing_role") as url:
        alembic_upgrade(url, contract.DOWN_REVISION)
        with pytest.raises(Exception, match="required role .* does not exist"):
            alembic_upgrade(url, contract.REVISION)
        assert _dbv2_schema_count(url) == 0, "a refused migration left a partial schema"
        assert scalar(url, "SELECT version_num FROM alembic_version") == contract.DOWN_REVISION


def test_an_incompatible_role_attribute_fails_before_any_ddl(dbv2_cluster_url: str) -> None:
    """J22: the definer principal must be NOLOGIN, and 0009 will not fix it."""
    provision_roles(dbv2_cluster_url)
    with dbv2_scratch_database(dbv2_cluster_url, "minos_dbv2_bad_role") as url:
        alembic_upgrade(url, contract.DOWN_REVISION)
        engine = create_engine(
            normalize_database_url(dbv2_cluster_url), isolation_level="AUTOCOMMIT"
        )
        with engine.connect() as conn:
            conn.execute(text("ALTER ROLE minos_owner LOGIN"))
        engine.dispose()
        try:
            with pytest.raises(Exception, match="must be NOLOGIN"):
                alembic_upgrade(url, contract.REVISION)
            assert _dbv2_schema_count(url) == 0
            assert scalar(url, "SELECT version_num FROM alembic_version") == contract.DOWN_REVISION
        finally:
            provision_roles(dbv2_cluster_url)


def test_a_cluster_privileged_role_fails_before_any_ddl(dbv2_cluster_url: str) -> None:
    """J22: SUPERUSER, CREATEROLE and CREATEDB are all refused."""
    provision_roles(dbv2_cluster_url)
    with dbv2_scratch_database(dbv2_cluster_url, "minos_dbv2_privileged_role") as url:
        alembic_upgrade(url, contract.DOWN_REVISION)
        engine = create_engine(
            normalize_database_url(dbv2_cluster_url), isolation_level="AUTOCOMMIT"
        )
        with engine.connect() as conn:
            conn.execute(text("ALTER ROLE minos_trainer CREATEDB"))
        engine.dispose()
        try:
            with pytest.raises(Exception, match="SUPERUSER, CREATEROLE or CREATEDB"):
                alembic_upgrade(url, contract.REVISION)
            assert _dbv2_schema_count(url) == 0
        finally:
            engine = create_engine(
                normalize_database_url(dbv2_cluster_url), isolation_level="AUTOCOMMIT"
            )
            with engine.connect() as conn:
                conn.execute(text("ALTER ROLE minos_trainer NOCREATEDB"))
            engine.dispose()
            provision_roles(dbv2_cluster_url)


# --------------------------------------------------------------------------- #
# J24-J25: the elevation is transaction-scoped
# --------------------------------------------------------------------------- #
def test_the_elevation_does_not_leak_after_commit(dbv2_cluster_url: str) -> None:
    """J24: SET LOCAL ROLE is undone by the transaction, on the very connection that ran it."""
    provision_roles(dbv2_cluster_url)
    with dbv2_scratch_database(dbv2_cluster_url, "minos_dbv2_elevation") as url:
        alembic_upgrade(url, contract.REVISION)
        engine = create_engine(normalize_database_url(url))
        try:
            with engine.connect() as conn:
                identity = conn.execute(text("SELECT current_user, session_user")).one()
            assert identity[0] == identity[1]
            assert identity[0] != contract.DEFINER_PRINCIPAL
        finally:
            engine.dispose()


def test_the_elevation_does_not_leak_after_rollback(dbv2_url: str) -> None:
    """J25: the same statement the migration issues, aborted, leaves the session unelevated."""
    engine = create_engine(normalize_database_url(dbv2_url))
    try:
        with engine.connect() as conn:
            original = conn.execute(text("SELECT current_user")).scalar_one()
            conn.rollback()
            for finish in ("rollback", "commit"):
                conn.execute(text(f"SET LOCAL ROLE {contract.DEFINER_PRINCIPAL}"))
                assert (
                    conn.execute(text("SELECT current_user")).scalar_one()
                    == contract.DEFINER_PRINCIPAL
                ), finish
                getattr(conn, finish)()
                assert conn.execute(text("SELECT current_user")).scalar_one() == original, (
                    f"the elevation leaked past {finish}"
                )
                conn.rollback()
    finally:
        engine.dispose()


# --------------------------------------------------------------------------- #
# J29-J30: generation integrity and leakage
# --------------------------------------------------------------------------- #
def _generate(target: Path, contract_target: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            str(REPO_ROOT / "scripts" / "gen_dbv2_migration.py"),
            "verify",
            "--migration",
            str(target),
            "--contract",
            str(contract_target),
        ],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        check=False,
    )


def test_generation_is_deterministic_and_verification_detects_drift(tmp_path: Path) -> None:
    """J29: two independent runs are byte-identical, and a single edited byte is caught."""
    first, second = tmp_path / "a.py", tmp_path / "b.py"
    contract_a, contract_b = tmp_path / "ca.py", tmp_path / "cb.py"
    for target, contract_target in ((first, contract_a), (second, contract_b)):
        result = subprocess.run(
            [
                sys.executable,
                str(REPO_ROOT / "scripts" / "gen_dbv2_migration.py"),
                "generate",
                "--migration",
                str(target),
                "--contract",
                str(contract_target),
            ],
            capture_output=True,
            text=True,
            cwd=REPO_ROOT,
            check=True,
        )
        assert result.returncode == 0
    assert first.read_bytes() == second.read_bytes()
    assert contract_a.read_bytes() == contract_b.read_bytes()
    assert first.read_bytes() == MIGRATION.read_bytes()

    assert _generate(MIGRATION, Path(contract.__file__)).returncode == 0
    drifted = tmp_path / "drifted.py"
    drifted.write_text(
        MIGRATION.read_text(encoding="utf-8").replace("dbv2_catalog", "dbv2_katalog", 1),
        encoding="utf-8",
    )
    failed = _generate(drifted, contract_a)
    assert failed.returncode == 1
    assert "differs from the contracts" in failed.stderr


def test_the_migration_contains_no_credential_dsn_or_operational_path() -> None:
    """J30: the one place where scanning the committed bytes IS the proof."""
    source = MIGRATION.read_text(encoding="utf-8")
    for pattern in (
        r"postgresql(\+\w+)?://",
        r"(?i)\bpassword\b\s*[:=]",
        r"(?i)\bsecret\b\s*[:=]",
        r"127\.0\.0\.1",
        r"localhost",
        r":5433",
        r"(?i)\bPGPASSWORD\b",
        r"/home/",
        r"MINOS_DATABASE_URL",
    ):
        assert re.search(pattern, source) is None, pattern
    assert (
        "minos_engine_db" not in source.replace("provision the database grant before migrating", "")
        or "current_database()" in source
    )


def test_the_migration_binds_the_frozen_contract_hashes() -> None:
    """G: the migration contract pins the exact bytes and every upstream hash."""
    import hashlib

    assert hashlib.sha256(MIGRATION.read_bytes()).hexdigest() == contract.MIGRATION_SHA256
    reports = json.loads(API_REPORT.read_text(encoding="utf-8"))
    assert reports["contract_sha256"] == contract.DATABASE_API_SHA256
    for revision, digest in contract.FROZEN_MIGRATION_SHA256.items():
        path = REPO_ROOT / "migrations" / "versions" / f"{revision}.py"
        assert hashlib.sha256(path.read_bytes()).hexdigest() == digest, revision


def test_the_migration_reads_no_report_and_touches_no_filesystem() -> None:
    """C3/C6: self-contained means no import of json, pathlib, os or the design reports."""
    source = MIGRATION.read_text(encoding="utf-8")
    for forbidden in ("import json", "import os", "import pathlib", "open(", "Path(", "requests"):
        assert forbidden not in source, forbidden
    body = source.split('"""', 2)[2]
    assert "reports/database" not in body
    assert "gen_dbv2_migration" not in body
