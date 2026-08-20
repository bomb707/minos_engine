"""F3-A4: populated ``0005 -> 0006 -> 0005 -> 0006`` migration lifecycle (scratch PG only).

Unlike ``test_l2f_migration_lifecycle`` (empty schema lifecycle), this test starts from a
genuinely populated ``0005`` database, captures a **complete** deterministic snapshot of every
seeded upstream row (all columns, ordered by primary key) plus the full ``0005`` structural
and security state (ownership, grants for the five MINOS roles and PUBLIC, schema privileges,
function ownership/grants, constraints, explicit indexes, roles and memberships), then:

  upgrade -> seed L2-F -> downgrade (while populated) -> re-upgrade -> reseed

and proves the downgrade is an exact, destructive teardown of only the L2-F stage: the upstream
rows are byte/logically unchanged, the captured ``0005`` structural/security state is exactly
restored, the five L2-F tables + six composite targets + six L2-F triggers + the L2-F job
function are gone, and shared pre-0006 functions survive.

This lifecycle is destructive and MUST NEVER run against the operational ``minos_engine_db``;
it only ever touches an ephemeral scratch database.
"""

from __future__ import annotations

import pytest
from sqlalchemy import Engine, create_engine, text

from minos_engine.storage.database import normalize_database_url
from tests.integration.layer2_db.conftest import (
    alembic_downgrade,
    alembic_upgrade,
    scratch_database,
)
from tests.integration.layer2_db.l2f_seed import (
    EXPECTED_ROW_COUNTS,
    EXPECTED_UPSTREAM_COUNTS,
    UPSTREAM_TABLES_IN_PK_ORDER,
    seed_l2f_graph,
    seed_upstream_graph,
)

_HEAD = "0006_l2f_experiment_plan"
_PREV = "0005_l2e_feature_view"
_L2F_TABLES = tuple(EXPECTED_ROW_COUNTS)
_COMPOSITE_TARGETS = (
    "uq_l2f_feature_matrices_composite",
    "uq_l2f_profile_snapshots_composite",
    "uq_l2f_feature_sets_composite",
    "uq_l2f_psm_composite",
    "uq_l2f_fmm_composite",
    "uq_l2f_artifacts_id_sha_media",
)
_L2F_TRIGGERS = (
    "trg_experiments_l2f_experiment_plans_append_only",
    "trg_experiments_l2f_experiment_plan_members_append_only",
    "trg_experiments_l2f_config_payloads_append_only",
    "trg_experiments_l2f_experiment_plan_configs_append_only",
    "trg_l2f_jobs_identity_immutable",
    "trg_l2f_jobs_no_delete",
)
_APP_ROLES = ("minos_admin", "minos_live", "minos_runner", "minos_trainer", "minos_evaluator")
_SCHEMAS = ("profiling", "catalog", "experiments")
_FN_SCHEMAS = ("audit", "profiling", "catalog", "experiments")


def _rows(engine: Engine, sql: str, **p: object) -> list[tuple]:
    with engine.connect() as c:
        return [tuple(r) for r in c.execute(text(sql), p).all()]


def _scalar(engine: Engine, sql: str, **p: object) -> object:
    with engine.connect() as c:
        return c.execute(text(sql), p).scalar_one()


def _capture_upstream_rows(engine: Engine) -> dict[str, list[dict]]:
    """Full ordered snapshot of every seeded upstream table (all columns, PK order)."""
    snap: dict[str, list[dict]] = {}
    with engine.connect() as c:
        for schema, table in UPSTREAM_TABLES_IN_PK_ORDER:
            rows = c.execute(text(f"SELECT * FROM {schema}.{table} ORDER BY id")).mappings().all()  # noqa: S608
            snap[f"{schema}.{table}"] = [dict(r) for r in rows]
    return snap


def _capture_structural_state(engine: Engine) -> dict[str, list]:
    grantees = [*_APP_ROLES, "PUBLIC"]
    schema_privs = []
    for role in _APP_ROLES:
        for schema in _SCHEMAS:
            for priv in ("USAGE", "CREATE"):
                has = _scalar(
                    engine, "SELECT has_schema_privilege(:r, :s, :p)", r=role, s=schema, p=priv
                )
                schema_privs.append((role, schema, priv, bool(has)))
    return {
        "table_owners": sorted(
            _rows(
                engine,
                "SELECT schemaname, tablename, tableowner FROM pg_tables WHERE schemaname = ANY(:s)",
                s=list(_SCHEMAS),
            )
        ),
        "schema_owners": sorted(
            _rows(
                engine,
                "SELECT nspname, pg_get_userbyid(nspowner) FROM pg_namespace WHERE nspname = ANY(:s)",
                s=list(_SCHEMAS),
            )
        ),
        "table_grants": sorted(
            _rows(
                engine,
                "SELECT grantee, table_schema, table_name, privilege_type "
                "FROM information_schema.role_table_grants "
                "WHERE table_schema = ANY(:s) AND grantee = ANY(:g)",
                s=list(_SCHEMAS),
                g=grantees,
            )
        ),
        "schema_privileges": sorted(schema_privs),
        "constraints": sorted(
            _rows(
                engine,
                "SELECT n.nspname, cl.relname, c.conname, c.contype "
                "FROM pg_constraint c JOIN pg_class cl ON cl.oid = c.conrelid "
                "JOIN pg_namespace n ON n.oid = cl.relnamespace WHERE n.nspname = ANY(:s)",
                s=list(_SCHEMAS),
            )
        ),
        "indexes": sorted(
            _rows(
                engine,
                "SELECT schemaname, tablename, indexname FROM pg_indexes WHERE schemaname = ANY(:s)",
                s=list(_SCHEMAS),
            )
        ),
        "roles": sorted(
            _rows(
                engine, "SELECT rolname FROM pg_roles WHERE rolname = ANY(:g)", g=list(_APP_ROLES)
            )
        ),
        "role_memberships": sorted(
            _rows(
                engine,
                "SELECT pg_get_userbyid(m.roleid), pg_get_userbyid(m.member) FROM pg_auth_members m "
                "WHERE pg_get_userbyid(m.roleid) = ANY(:g) OR pg_get_userbyid(m.member) = ANY(:g)",
                g=list(_APP_ROLES),
            )
        ),
        "functions": sorted(
            _rows(
                engine,
                "SELECT n.nspname, p.proname, pg_get_userbyid(p.proowner) FROM pg_proc p "
                "JOIN pg_namespace n ON n.oid = p.pronamespace WHERE n.nspname = ANY(:s)",
                s=list(_FN_SCHEMAS),
            )
        ),
        "function_grants": sorted(
            _rows(
                engine,
                "SELECT grantee, routine_schema, routine_name, privilege_type "
                "FROM information_schema.role_routine_grants "
                "WHERE routine_schema = ANY(:s) AND grantee = ANY(:g)",
                s=list(_FN_SCHEMAS),
                g=grantees,
            )
        ),
    }


def _l2f_table_count(engine: Engine) -> int:
    return int(
        _scalar(
            engine,
            "SELECT count(*) FROM information_schema.tables "
            "WHERE table_schema = 'experiments' AND table_name = ANY(:n)",
            n=list(_L2F_TABLES),
        )
    )


@pytest.fixture(scope="module")
def lifecycle_url(pg_base_url: str):
    with scratch_database(pg_base_url, "minos_l2f_populated") as url:
        yield url


def test_populated_downgrade_preserves_0005_exactly(lifecycle_url: str) -> None:
    url = lifecycle_url
    engine = create_engine(normalize_database_url(url))
    try:
        # 1) upgrade explicitly to 0005 and 2) confirm the exact revision.
        alembic_upgrade(url, _PREV)
        assert _scalar(engine, "SELECT version_num FROM alembic_version") == _PREV
        assert _l2f_table_count(engine) == 0

        # 3) seed the complete upstream graph WHILE STILL AT 0005.
        with engine.connect() as conn, conn.begin():
            refs = seed_upstream_graph(conn)
        for (schema, table), expected in EXPECTED_UPSTREAM_COUNTS.items():
            n = _scalar(engine, f"SELECT count(*) FROM {schema}.{table}")  # noqa: S608
            assert n == expected, f"{schema}.{table}: {n} != {expected}"

        # 4) complete upstream row snapshot + 5) 0005 structural/security fingerprint.
        upstream_before = _capture_upstream_rows(engine)
        structural_before = _capture_structural_state(engine)

        # 6) upgrade the populated 0005 -> 0006.
        alembic_upgrade(url, _HEAD)
        assert _scalar(engine, "SELECT version_num FROM alembic_version") == _HEAD

        # 7) seed the L2-F graph and verify expected counts.
        with engine.connect() as conn, conn.begin():
            seed_l2f_graph(conn, refs)
        assert _l2f_table_count(engine) == 5
        for table, expected in EXPECTED_ROW_COUNTS.items():
            n = _scalar(engine, f"SELECT count(*) FROM experiments.{table}")  # noqa: S608
            assert n == expected, f"{table}: {n} != {expected}"

        # 8) downgrade the populated 0006 -> 0005.
        alembic_downgrade(url, _PREV)
        assert _scalar(engine, "SELECT version_num FROM alembic_version") == _PREV

        # 9a) the complete upstream row snapshot is EXACTLY unchanged (all columns, all rows).
        assert _capture_upstream_rows(engine) == upstream_before

        # 9b) the captured 0005 structural/security state is EXACTLY restored.
        assert _capture_structural_state(engine) == structural_before

        # 9c) all five L2-F tables are absent.
        assert _l2f_table_count(engine) == 0
        # 9d) all six additive composite UNIQUE targets are absent.
        assert (
            _scalar(
                engine,
                "SELECT count(*) FROM pg_constraint WHERE conname = ANY(:n)",
                n=list(_COMPOSITE_TARGETS),
            )
            == 0
        )
        # 9e) all six L2-F triggers disappeared with their tables.
        assert (
            _scalar(
                engine,
                "SELECT count(*) FROM pg_trigger WHERE NOT tgisinternal AND tgname = ANY(:n)",
                n=list(_L2F_TRIGGERS),
            )
            == 0
        )
        # 9f) the L2-F job identity function is absent; the shared 0001 append-only function
        # (and other pre-0006 functions) survive.
        assert (
            _scalar(
                engine,
                "SELECT count(*) FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace "
                "WHERE n.nspname = 'experiments' "
                "AND p.proname = 'minos_l2f_reject_job_identity_change'",
            )
            == 0
        )
        assert (
            _scalar(
                engine,
                "SELECT count(*) FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace "
                "WHERE n.nspname = 'audit' AND p.proname = 'minos_reject_mutation'",
            )
            == 1
        )

        # 10) re-upgrade the still-populated 0005 -> 0006.
        alembic_upgrade(url, _HEAD)
        assert _l2f_table_count(engine) == 5
        for table in _L2F_TABLES:
            assert _scalar(engine, f"SELECT count(*) FROM experiments.{table}") == 0  # noqa: S608

        # 11) upstream rows remain byte/logically identical after the re-upgrade.
        assert _capture_upstream_rows(engine) == upstream_before

        # 12) reseed the L2-F graph against the surviving upstream and prove it works again.
        with engine.connect() as conn, conn.begin():
            seed_l2f_graph(conn, refs)
        for table, expected in EXPECTED_ROW_COUNTS.items():
            n = _scalar(engine, f"SELECT count(*) FROM experiments.{table}")  # noqa: S608
            assert n == expected, f"reseed {table}: {n} != {expected}"
    finally:
        engine.dispose()
