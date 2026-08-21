"""F3-A: populated ``0005 -> 0006 -> 0005 -> 0006`` migration lifecycle (scratch PG only).

Starts from a genuinely populated ``0005`` database and proves the downgrade is an exact,
destructive teardown of only the L2-F stage:

* a **complete** deterministic snapshot of every seeded upstream row (all columns, PK order)
  is byte/logically unchanged across the downgrade and re-upgrade; and
* the **full normalized structure + security state across every MINOS schema** (catalog,
  profiling, experiments, evaluation, models, runtime, audit) — captured with the shared
  introspector (``full_structural_state``), which uses ``pg_catalog`` + ``aclexplode`` over
  ``COALESCE(acl, acldefault(...))`` (never ``information_schema`` grant views) and therefore
  covers every relation kind including **views** (definitions, security_barrier, check_option),
  constraints, indexes, triggers, functions, raw + **effective** ACLs (PUBLIC, grantor,
  grantee, grant option) for schemas/tables/views/functions/columns, default ACLs, roles (with
  membership admin/inherit/set options), the database and the alembic revision — is restored
  exactly.

Post-downgrade it also proves the five L2-F tables, six composite targets, six L2-F triggers
and the L2-F job function are gone while shared pre-0006 functions survive.

This lifecycle is destructive and MUST NEVER run against the operational ``minos_engine_db``;
it only ever touches an ephemeral scratch database.
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import Engine, create_engine, text

from minos_engine.storage.database import normalize_database_url
from tests.integration.layer2_db.conftest import (
    alembic_downgrade,
    alembic_upgrade,
    scratch_database,
)
from tests.integration.layer2_db.l2f_introspect import full_structural_state
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
_ROLES = ["minos_admin", "minos_live", "minos_runner", "minos_trainer", "minos_evaluator"]
_DBNAME = "minos_l2f_populated"


def _scalar(engine: Engine, sql: str, **p: object) -> Any:
    with engine.connect() as c:
        return c.execute(text(sql), p).scalar_one()


def _capture_upstream_rows(engine: Engine) -> dict[str, list[dict]]:
    snap: dict[str, list[dict]] = {}
    with engine.connect() as c:
        for schema, table in UPSTREAM_TABLES_IN_PK_ORDER:
            rows = c.execute(text(f"SELECT * FROM {schema}.{table} ORDER BY id")).mappings().all()  # noqa: S608
            snap[f"{schema}.{table}"] = [dict(r) for r in rows]
    return snap


def _capture_structural(engine: Engine) -> dict[str, Any]:
    # exhaustive normalized state across ALL MINOS schemas (introspector uses MINOS_SCHEMAS).
    with engine.connect() as c:
        return full_structural_state(c, _ROLES, dbname=_DBNAME)


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
        # 1) upgrade explicitly to 0005 and confirm the exact revision.
        alembic_upgrade(url, _PREV)
        assert _scalar(engine, "SELECT version_num FROM alembic_version") == _PREV
        assert _l2f_table_count(engine) == 0

        # 2) seed the complete upstream graph WHILE STILL AT 0005.
        with engine.connect() as conn, conn.begin():
            refs = seed_upstream_graph(conn)
        for (schema, table), expected in EXPECTED_UPSTREAM_COUNTS.items():
            n = _scalar(engine, f"SELECT count(*) FROM {schema}.{table}")  # noqa: S608
            assert n == expected, f"{schema}.{table}: {n} != {expected}"

        # 3) complete upstream row snapshot + full normalized 0005 structure/security state.
        upstream_before = _capture_upstream_rows(engine)
        structural_before = _capture_structural(engine)

        # 4) upgrade the populated 0005 -> 0006; seed L2-F; verify counts.
        alembic_upgrade(url, _HEAD)
        with engine.connect() as conn, conn.begin():
            seed_l2f_graph(conn, refs)
        assert _l2f_table_count(engine) == 5
        for table, expected in EXPECTED_ROW_COUNTS.items():
            n = _scalar(engine, f"SELECT count(*) FROM experiments.{table}")  # noqa: S608
            assert n == expected, f"{table}: {n} != {expected}"

        # 5) downgrade the populated 0006 -> 0005.
        alembic_downgrade(url, _PREV)
        assert _scalar(engine, "SELECT version_num FROM alembic_version") == _PREV

        # 6a) the complete upstream row snapshot is EXACTLY unchanged (all columns, all rows).
        assert _capture_upstream_rows(engine) == upstream_before
        # 6b) the full normalized 0005 structure + security state is EXACTLY restored
        # (tables/columns/constraints/indexes/triggers/functions/schema+table+function ACLs,
        # default ACLs, roles and membership options).
        assert _capture_structural(engine) == structural_before

        # 6c) all five L2-F tables are absent.
        assert _l2f_table_count(engine) == 0
        # 6d) all six additive composite UNIQUE targets are absent.
        assert (
            _scalar(
                engine,
                "SELECT count(*) FROM pg_constraint WHERE conname = ANY(:n)",
                n=list(_COMPOSITE_TARGETS),
            )
            == 0
        )
        # 6e) all six L2-F triggers disappeared with their tables.
        assert (
            _scalar(
                engine,
                "SELECT count(*) FROM pg_trigger WHERE NOT tgisinternal AND tgname = ANY(:n)",
                n=list(_L2F_TRIGGERS),
            )
            == 0
        )
        # 6f) the L2-F job function is absent; the shared 0001 append-only function survives.
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

        # 7) re-upgrade the still-populated 0005 -> 0006.
        alembic_upgrade(url, _HEAD)
        assert _l2f_table_count(engine) == 5
        for table in _L2F_TABLES:
            assert _scalar(engine, f"SELECT count(*) FROM experiments.{table}") == 0  # noqa: S608

        # upstream rows remain byte/logically identical after the re-upgrade.
        assert _capture_upstream_rows(engine) == upstream_before

        # 8) reseed the L2-F graph against the surviving upstream and prove it works again.
        with engine.connect() as conn, conn.begin():
            seed_l2f_graph(conn, refs)
        for table, expected in EXPECTED_ROW_COUNTS.items():
            n = _scalar(engine, f"SELECT count(*) FROM experiments.{table}")  # noqa: S608
            assert n == expected, f"reseed {table}: {n} != {expected}"
    finally:
        engine.dispose()
