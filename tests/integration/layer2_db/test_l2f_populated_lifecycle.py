"""F3-A4: populated ``0005 -> 0006 -> 0005 -> 0006`` migration lifecycle (scratch PG only).

Unlike ``test_l2f_migration_lifecycle`` (which exercises the *empty* schema lifecycle), this
test seeds a complete valid graph, downgrades **while populated**, and proves the downgrade
is a clean destructive teardown of the L2-F stage that leaves the upstream L2-D/L2-E rows,
constraints and ownership untouched — then re-upgrades and reseeds to prove the graph works
again on the surviving upstream.

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
_APP_ROLES = ("minos_admin", "minos_live", "minos_runner", "minos_trainer", "minos_evaluator")


def _scalar(engine: Engine, sql: str, **p: object) -> object:
    with engine.connect() as c:
        return c.execute(text(sql), p).scalar_one()


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


def test_populated_downgrade_and_reupgrade(lifecycle_url: str) -> None:
    url = lifecycle_url
    engine = create_engine(normalize_database_url(url))
    try:
        # 1) upgrade 0005 -> 0006 and 2) seed the complete valid graph (committed).
        alembic_upgrade(url, _HEAD)
        with engine.connect() as conn, conn.begin():
            refs = seed_upstream_graph(conn)
            seed_l2f_graph(conn, refs)

        # 3) all five L2-F tables contain the expected rows.
        assert _l2f_table_count(engine) == 5
        for table, expected in EXPECTED_ROW_COUNTS.items():
            n = _scalar(engine, f"SELECT count(*) FROM experiments.{table}")  # noqa: S608
            assert n == expected, f"{table}: {n} != {expected}"
        # upstream rows present as seeded.
        for (schema, table), expected in EXPECTED_UPSTREAM_COUNTS.items():
            n = _scalar(engine, f"SELECT count(*) FROM {schema}.{table}")  # noqa: S608
            assert n == expected, f"{schema}.{table}: {n} != {expected}"
        # snapshot a representative upstream identity to prove it is untouched by downgrade.
        sa_snapshot_hash = _scalar(
            engine,
            "SELECT snapshot_hash FROM profiling.profile_snapshots WHERE id = :i",
            i=refs.sa,
        )

        # 4) downgrade to 0005 WHILE POPULATED.
        alembic_downgrade(url, _PREV)

        # 5a) all five L2-F tables are gone.
        assert _l2f_table_count(engine) == 0
        # 5b) all six additive composite UNIQUE targets are gone.
        assert (
            _scalar(
                engine,
                "SELECT count(*) FROM pg_constraint WHERE conname = ANY(:n)",
                n=list(_COMPOSITE_TARGETS),
            )
            == 0
        )
        # 5c) the L2-F job identity-change trigger function is gone; the SHARED append-only
        # function from 0001 is preserved.
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
        # 5d) pre-existing L2-D/L2-E rows remain unchanged (counts + identity).
        for (schema, table), expected in EXPECTED_UPSTREAM_COUNTS.items():
            n = _scalar(engine, f"SELECT count(*) FROM {schema}.{table}")  # noqa: S608
            assert n == expected, f"{schema}.{table} changed on downgrade: {n} != {expected}"
        assert (
            _scalar(
                engine,
                "SELECT snapshot_hash FROM profiling.profile_snapshots WHERE id = :i",
                i=refs.sa,
            )
            == sa_snapshot_hash
        )
        # 5e) 0005 constraints, roles and ownership remain intact.
        assert (
            _scalar(
                engine,
                "SELECT count(*) FROM pg_constraint WHERE conname = "
                "'uq_feature_matrices_logical_identity'",
            )
            == 1
        )
        assert _scalar(
            engine,
            "SELECT count(*) FROM pg_roles WHERE rolname = ANY(:n)",
            n=list(_APP_ROLES),
        ) == len(_APP_ROLES)
        for schema, table in (
            ("profiling", "feature_matrices"),
            ("profiling", "profile_snapshots"),
            ("catalog", "artifacts"),
        ):
            owner = _scalar(
                engine,
                "SELECT tableowner FROM pg_tables WHERE schemaname = :s AND tablename = :t",
                s=schema,
                t=table,
            )
            assert owner == "minos_admin", f"{schema}.{table} owner changed: {owner}"

        # 6) re-upgrade 0005 -> 0006.
        alembic_upgrade(url, _HEAD)
        assert _l2f_table_count(engine) == 5
        for table in _L2F_TABLES:
            assert _scalar(engine, f"SELECT count(*) FROM experiments.{table}") == 0  # noqa: S608

        # 7) reseed the L2-F graph against the surviving upstream and prove it works again.
        with engine.connect() as conn, conn.begin():
            seed_l2f_graph(conn, refs)
        for table, expected in EXPECTED_ROW_COUNTS.items():
            n = _scalar(engine, f"SELECT count(*) FROM experiments.{table}")  # noqa: S608
            assert n == expected, f"reseed {table}: {n} != {expected}"
    finally:
        engine.dispose()
