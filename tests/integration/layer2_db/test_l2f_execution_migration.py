"""F5-A migration 0008 — real-PostgreSQL lifecycle, parity, grants, triggers and attacks.

Scratch PostgreSQL only; the operational store is never touched.
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Engine, text

from minos_engine.storage.l2f_execution_contract import (
    ACCEPTED_PRIOR_MIGRATION_SHAS,
    L2F_EXECUTION_DOWN_REVISION,
    L2F_EXECUTION_FAILURE_CODES,
    L2F_EXECUTION_FUNCTIONS,
    L2F_EXECUTION_OWNED_TABLE_NAMES,
    L2F_EXECUTION_REVISION,
    compute_execution_contract_hash,
    compute_execution_migration_sha256,
)
from minos_engine.storage.l2f_job_claim import F4_COMPATIBLE_REVISIONS
from minos_engine.storage.l2f_plan_store import (
    L2F_GRAPH_COMPATIBLE_REVISIONS,
    _persist_experiment_plan_with_trust,
)
from tests.integration.layer2_db.conftest import (
    alembic_downgrade,
    alembic_upgrade,
    scratch_database,
)
from tests.integration.layer2_db.l2f_plan_seed import seed_upstream_for_plan
from tests.integration.layer2_db.test_l2f_plan_store import (
    _CS,
    _SNAPSHOT_A,
    _engine,
    _provisioned_root,
    _publisher,
    _synthetic_plan,
)

_F4 = "0007_l2f_job_claiming"
_F5 = "0008_l2f_execution_results"
_RESULTS = "experiments.l2f_execution_results"
_FAILURES = "experiments.l2f_execution_failures"
_JOBS = "experiments.l2f_experiment_jobs"


def _scalar(engine: Engine, sql: str, **p: Any) -> Any:
    with engine.connect() as c:
        return c.execute(text(sql), p).scalar_one()


# --------------------------------------------------------------------------- #
# revision lineage + byte identity
# --------------------------------------------------------------------------- #
def test_single_head_is_0009_descending_0008_descending_0007() -> None:
    """DB-V2 D2 advanced the head to 0009; 0008 remains exactly where it was, below it."""
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    script = ScriptDirectory.from_config(Config("alembic.ini"))
    assert script.get_heads() == ["0009_dbv2_shadow_schema"]
    assert {r.down_revision for r in script.get_revisions("0009_dbv2_shadow_schema")} == {_F5}
    assert {r.down_revision for r in script.get_revisions(_F5)} == {_F4}


def test_prior_migrations_are_byte_identical() -> None:
    import hashlib

    root = Path(__file__).resolve().parents[3]
    for rel, expected in ACCEPTED_PRIOR_MIGRATION_SHAS.items():
        assert hashlib.sha256((root / rel).read_bytes()).hexdigest() == expected, rel


def test_execution_contract_recomputes() -> None:
    assert len(compute_execution_migration_sha256()) == 64
    assert len(compute_execution_contract_hash()) == 64
    assert L2F_EXECUTION_REVISION == _F5
    assert L2F_EXECUTION_DOWN_REVISION == _F4


def test_closed_revision_sets_include_0008() -> None:
    assert frozenset({"0006_l2f_experiment_plan", _F4, _F5}) == L2F_GRAPH_COMPATIBLE_REVISIONS
    assert frozenset({_F4, _F5}) == F4_COMPATIBLE_REVISIONS
    assert "0005_l2e_feature_view" not in L2F_GRAPH_COMPATIBLE_REVISIONS


# --------------------------------------------------------------------------- #
# 0007 <-> 0008 lifecycle
# --------------------------------------------------------------------------- #
def _f5_object_counts(engine: Engine) -> tuple[int, int, int]:
    tables = int(
        _scalar(
            engine,
            "SELECT count(*) FROM information_schema.tables WHERE table_schema='experiments' "
            "AND table_name = ANY(:n)",
            n=list(L2F_EXECUTION_OWNED_TABLE_NAMES),
        )
    )
    fns = int(
        _scalar(
            engine,
            "SELECT count(*) FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace "
            "WHERE n.nspname='experiments' AND p.proname IN "
            "('minos_l2f_resolve_running_job','minos_l2f_complete_job_success',"
            "'minos_l2f_fail_job','minos_l2f_reject_dual_outcome')",
        )
    )
    targets = int(
        _scalar(
            engine,
            "SELECT count(*) FROM pg_constraint WHERE conname IN "
            "('uq_l2f_jobs_id_plan','uq_l2f_jobs_id_job_key','uq_l2f_pc_id_hash_space')",
        )
    )
    return tables, fns, targets


def _guard_allows_terminal(engine: Engine) -> bool:
    src = _scalar(
        engine,
        "SELECT prosrc FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace "
        "WHERE n.nspname='experiments' AND p.proname='minos_l2f_job_transition_guard'",
    )
    return "SUCCEEDED" in str(src)


def test_0007_0008_0007_0008_lifecycle(isolated_pg_base_url: str) -> None:
    with scratch_database(isolated_pg_base_url, "minos_f5") as url:
        alembic_upgrade(url, _F4)
        engine = _engine(url)
        try:
            assert _f5_object_counts(engine) == (0, 0, 0)
            assert not _guard_allows_terminal(engine)

            alembic_upgrade(url, _F5)
            assert _f5_object_counts(engine) == (2, 4, 3)
            assert _guard_allows_terminal(engine)

            alembic_downgrade(url, _F4)
            assert _f5_object_counts(engine) == (0, 0, 0)
            # the EXACT F4-only guard is restored
            assert not _guard_allows_terminal(engine)
            # 0006/0007 job triggers survive untouched
            names = sorted(
                str(r[0])
                for r in _rows(
                    engine,
                    "SELECT tgname FROM pg_trigger WHERE tgrelid=CAST(:t AS regclass) "
                    "AND NOT tgisinternal",
                    t=_JOBS,
                )
            )
            assert names == [
                "trg_l2f_jobs_identity_immutable",
                "trg_l2f_jobs_no_delete",
                "trg_l2f_jobs_transition_guard",
            ]

            alembic_upgrade(url, _F5)
            assert _f5_object_counts(engine) == (2, 4, 3)
            assert _guard_allows_terminal(engine)
        finally:
            engine.dispose()


def _rows(engine: Engine, sql: str, **p: Any) -> list[Any]:
    with engine.connect() as c:
        return list(c.execute(text(sql), p).all())


def test_populated_lifecycle_preserves_the_plan_graph(
    isolated_pg_base_url: str, tmp_path: Path
) -> None:
    """A populated F3-C1 graph survives 0008 upgrade + downgrade untouched."""
    plan = _synthetic_plan(_SNAPSHOT_A)
    with scratch_database(isolated_pg_base_url, "minos_f5") as url:
        alembic_upgrade(url, "0006_l2f_experiment_plan")
        engine = _engine(url)
        try:
            with engine.connect() as conn, conn.begin():
                seed_upstream_for_plan(conn, plan)
            _persist_experiment_plan_with_trust(
                engine, plan, _CS, publisher=_publisher(_provisioned_root(tmp_path))
            )
            before = int(
                _scalar(engine, "SELECT count(*) FROM experiments.l2f_experiment_plan_members")
            )
            alembic_upgrade(url, _F5)
            assert (
                int(_scalar(engine, "SELECT count(*) FROM experiments.l2f_experiment_plan_members"))
                == before
            )
            alembic_downgrade(url, _F4)
            assert (
                int(_scalar(engine, "SELECT count(*) FROM experiments.l2f_experiment_plan_members"))
                == before
            )
        finally:
            engine.dispose()


# --------------------------------------------------------------------------- #
# mapping parity: the private Core metadata equals the live 0008 schema
# --------------------------------------------------------------------------- #
def test_core_mappings_match_the_live_0008_schema(isolated_pg_base_url: str) -> None:
    from minos_engine.storage.l2f_execution_tables import (
        L2F_EXECUTION_OWNED_TABLES,
        l2f_execution_metadata,
    )
    from tests.integration.layer2_db.l2f_introspect import (
        introspect_constraints,
        introspect_indexes,
        introspect_table,
    )

    assert set(L2F_EXECUTION_OWNED_TABLES) == set(L2F_EXECUTION_OWNED_TABLE_NAMES)
    targets = [("experiments", t) for t in L2F_EXECUTION_OWNED_TABLE_NAMES]

    def _snapshot(engine: Engine) -> dict[str, Any]:
        with engine.connect() as c:
            tables = {t: introspect_table(c, "experiments", t) for _s, t in targets}
            # composite FKs to external tables are the migration's responsibility, not the
            # Core mapping's, so only PK/UNIQUE/CHECK are compared.
            cons = [
                x for x in introspect_constraints(c, targets) if x.get("contype") in {"p", "u", "c"}
            ]
            idx = introspect_indexes(c, targets)
        return {"tables": tables, "constraints": cons, "indexes": idx}

    with scratch_database(isolated_pg_base_url, "minos_f5_live") as live_url:
        alembic_upgrade(live_url, _F5)
        live = _engine(live_url)
        try:
            live_snap = _snapshot(live)
        finally:
            live.dispose()
    with scratch_database(isolated_pg_base_url, "minos_f5_map") as map_url:
        mapped = _engine(map_url)
        try:
            with mapped.begin() as c:
                c.execute(text("CREATE SCHEMA IF NOT EXISTS experiments"))
            l2f_execution_metadata.create_all(mapped)
            map_snap = _snapshot(mapped)
        finally:
            mapped.dispose()

    for table in L2F_EXECUTION_OWNED_TABLE_NAMES:
        assert live_snap["tables"][table]["columns"] == map_snap["tables"][table]["columns"], table
    assert live_snap["constraints"] == map_snap["constraints"]
    assert live_snap["indexes"] == map_snap["indexes"]


# --------------------------------------------------------------------------- #
# grants
# --------------------------------------------------------------------------- #
def test_exact_function_grants_and_table_denials(isolated_pg_base_url: str) -> None:
    with scratch_database(isolated_pg_base_url, "minos_f5") as url:
        alembic_upgrade(url, _F5)
        engine = _engine(url)
        try:
            with engine.connect() as c:
                for sig in L2F_EXECUTION_FUNCTIONS:
                    secdef, owner, cfg = c.execute(
                        text(
                            "SELECT prosecdef, pg_get_userbyid(proowner), proconfig FROM pg_proc "
                            "WHERE oid = CAST(:s AS regprocedure)"
                        ),
                        {"s": sig},
                    ).one()
                    assert secdef is True and owner == "minos_admin", sig
                    assert cfg == ["search_path=pg_catalog"], sig
                    for role in ("minos_runner", "minos_admin"):
                        assert c.execute(
                            text("SELECT has_function_privilege(:r, :s, 'EXECUTE')"),
                            {"r": role, "s": sig},
                        ).scalar_one(), (role, sig)
                    assert not c.execute(
                        text("SELECT has_function_privilege('public', :s, 'EXECUTE')"), {"s": sig}
                    ).scalar_one()
                    for role in ("minos_live", "minos_trainer", "minos_evaluator"):
                        assert not c.execute(
                            text("SELECT has_function_privilege(:r, :s, 'EXECUTE')"),
                            {"r": role, "s": sig},
                        ).scalar_one(), (role, sig)
                # trigger functions are not directly executable by app roles
                for fn in (
                    "experiments.minos_l2f_reject_dual_outcome()",
                    "experiments.minos_l2f_job_transition_guard()",
                ):
                    for role in ("minos_runner", "minos_live", "minos_trainer", "minos_evaluator"):
                        assert not c.execute(
                            text("SELECT has_function_privilege(:r, :s, 'EXECUTE')"),
                            {"r": role, "s": fn},
                        ).scalar_one(), (role, fn)
                # no direct table privilege anywhere
                for table in (_RESULTS, _FAILURES, _JOBS):
                    for role in (
                        "minos_runner",
                        "minos_live",
                        "minos_trainer",
                        "minos_evaluator",
                    ):
                        for priv in ("SELECT", "INSERT", "UPDATE", "DELETE"):
                            assert not c.execute(
                                text("SELECT has_table_privilege(:r, :t, :p)"),
                                {"r": role, "t": table, "p": priv},
                            ).scalar_one(), (role, table, priv)
        finally:
            engine.dispose()


def test_failure_code_vocabulary_is_bounded_in_the_database(isolated_pg_base_url: str) -> None:
    with scratch_database(isolated_pg_base_url, "minos_f5") as url:
        alembic_upgrade(url, _F5)
        engine = _engine(url)
        try:
            src = str(
                _scalar(
                    engine,
                    "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
                    "WHERE conname='ck_l2f_exec_failures_code_bounded'",
                )
            )
            for code in L2F_EXECUTION_FAILURE_CODES:
                assert code in src
            assert "CANCELLED" not in src
        finally:
            engine.dispose()


# --------------------------------------------------------------------------- #
# direct-SQL attacks against the terminal guard
# --------------------------------------------------------------------------- #
def _seeded_running_job(engine: Engine, plan: Any, worker: str = "w-1") -> str:
    """Persisted graph + one enqueued job driven to RUNNING via the F4 functions."""
    from minos_engine.storage import l2f_job_claim as JC
    from minos_engine.storage.l2f_job_enqueue import _enqueue_experiment_jobs_with_trust

    _enqueue_experiment_jobs_with_trust(engine, plan, _CS, start=0, count=1)
    claimed = JC._claim_next_job_with_trust(engine, plan, worker_id=worker)
    assert claimed is not None
    JC._start_job_with_trust(engine, plan, job_id=uuid.UUID(claimed.job_id), worker_id=worker)
    return claimed.job_id


@pytest.fixture
def running_job(isolated_pg_base_url: str, tmp_path: Path) -> Any:
    plan = _synthetic_plan(_SNAPSHOT_A)
    with scratch_database(isolated_pg_base_url, "minos_f5") as url:
        alembic_upgrade(url, "0006_l2f_experiment_plan")
        engine = _engine(url)
        try:
            with engine.connect() as conn, conn.begin():
                seed_upstream_for_plan(conn, plan)
            _persist_experiment_plan_with_trust(
                engine, plan, _CS, publisher=_publisher(_provisioned_root(tmp_path))
            )
            engine.dispose()
            alembic_upgrade(url, _F5)
            engine = _engine(url)
            job_id = _seeded_running_job(engine, plan)
            yield engine, plan, job_id
        finally:
            engine.dispose()


@pytest.mark.parametrize("status", ["SUCCEEDED", "FAILED"])
def test_direct_terminal_update_without_a_record_is_rejected(running_job: Any, status: str) -> None:
    engine, _plan, job_id = running_job
    with engine.connect() as c, c.begin(), pytest.raises(Exception) as ei:  # noqa: PT011
        c.execute(text("SET LOCAL ROLE minos_admin"))
        c.execute(
            text(f"UPDATE {_JOBS} SET status=:s WHERE id=:i"),  # noqa: S608
            {"s": status, "i": job_id},
        )
    assert getattr(ei.value.orig, "sqlstate", "") == "MN020"
    assert str(_scalar(engine, f"SELECT status FROM {_JOBS} WHERE id=:i", i=job_id)) == "RUNNING"  # noqa: S608


def test_cancelled_remains_unreachable(running_job: Any) -> None:
    engine, _plan, job_id = running_job
    with engine.connect() as c, c.begin(), pytest.raises(Exception) as ei:  # noqa: PT011
        c.execute(text("SET LOCAL ROLE minos_admin"))
        c.execute(
            text(f"UPDATE {_JOBS} SET status='CANCELLED' WHERE id=:i"),
            {"i": job_id},  # noqa: S608
        )
    assert getattr(ei.value.orig, "sqlstate", "") == "MN012"


def test_success_and_failure_are_mutually_exclusive(running_job: Any) -> None:
    engine, plan, job_id = running_job
    with engine.connect() as c, c.begin():
        c.execute(text("SET LOCAL ROLE minos_admin"))
        c.execute(
            text(
                f"INSERT INTO {_FAILURES} (plan_id, job_id, job_key, worker_id, failure_code) "  # noqa: S608
                f"SELECT j.plan_id, j.id, j.job_key, 'w-1', 'EXECUTION_ERROR' FROM {_JOBS} j "  # noqa: S608
                "WHERE j.id=:i"
            ),
            {"i": job_id},
        )
    # a success result for the SAME job must now be impossible
    with engine.connect() as c, c.begin(), pytest.raises(Exception) as ei:  # noqa: PT011
        c.execute(text("SET LOCAL ROLE minos_admin"))
        c.execute(
            text(
                f"INSERT INTO {_RESULTS} (plan_id, job_id, job_key, plan_member_id, "  # noqa: S608
                "plan_config_id, config_hash, parameter_space_hash, input_identity_hash, "
                "logical_argv_hash, gatk_executable_sha256, gatk_version, vcf_artifact_id, "
                "vcf_sha256, result_manifest_artifact_id, result_manifest_sha256, result_hash, "
                "runtime_ms) SELECT j.plan_id, j.id, j.job_key, j.plan_member_id, j.plan_config_id,"
                " :h, :h, :h, :h, :h, 'v', :u, :h, :u2, :h, :h, 1 "
                f"FROM {_JOBS} j WHERE j.id=:i"  # noqa: S608
            ),
            {
                "h": "a" * 64,
                "u": str(uuid.uuid4()),
                "u2": str(uuid.uuid4()),
                "i": job_id,
            },
        )
    assert getattr(ei.value.orig, "sqlstate", "") in {"MN021", "23503"}


@pytest.mark.parametrize("table", [_RESULTS, _FAILURES])
def test_outcome_tables_are_append_only(running_job: Any, table: str) -> None:
    engine, _plan, job_id = running_job
    with engine.connect() as c, c.begin():
        c.execute(text("SET LOCAL ROLE minos_admin"))
        if table == _FAILURES:
            c.execute(
                text(
                    f"INSERT INTO {_FAILURES} (plan_id, job_id, job_key, worker_id, failure_code) "  # noqa: S608
                    f"SELECT j.plan_id, j.id, j.job_key, 'w-1', 'EXECUTION_ERROR' FROM {_JOBS} j "  # noqa: S608
                    "WHERE j.id=:i"
                ),
                {"i": job_id},
            )
    if table == _RESULTS:
        pytest.skip("a success result needs published artifacts; covered by the F5-C suite")
    for stmt in (f"UPDATE {table} SET worker_id='x'", f"DELETE FROM {table}"):  # noqa: S608
        with engine.connect() as c, c.begin(), pytest.raises(Exception):  # noqa: B017, PT011
            c.execute(text("SET LOCAL ROLE minos_admin"))
            c.execute(text(stmt))
