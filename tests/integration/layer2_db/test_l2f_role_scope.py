"""Transaction-scoped role elevation: a pooled connection never retains ``minos_admin``.

F3-C1 persistence and F3-C2 enqueue elevate to the schema owner with ``SET LOCAL ROLE``, so
PostgreSQL restores the original session role on both COMMIT and ROLLBACK. These tests use ONE
reusable pooled engine throughout and re-check the session identity through freshly checked-out
connections of that same engine — so a leak would be observable rather than hidden behind a new
engine or ``engine.dispose()``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Engine, text

from minos_engine.storage import l2f_job_enqueue as EN
from minos_engine.storage import l2f_plan_store as PS
from minos_engine.storage.roles import SCHEMA_OWNER
from tests.integration.layer2_db.conftest import alembic_upgrade, scratch_database
from tests.integration.layer2_db.l2f_plan_seed import seed_upstream_for_plan
from tests.integration.layer2_db.test_l2f_plan_store import (
    _CS,
    _SNAPSHOT_A,
    _engine,
    _provisioned_root,
    _publisher,
    _synthetic_plan,
)

_L2F = "0006_l2f_experiment_plan"


def _identity(engine: Engine) -> tuple[str, str]:
    """(session_user, current_user) read through a NEWLY checked-out pooled connection."""
    with engine.connect() as c:
        row = c.execute(text("SELECT session_user, current_user")).one()
    return str(row[0]), str(row[1])


def _assert_no_leak(engine: Engine, baseline: tuple[str, str], stage: str) -> None:
    session_user, current_user = _identity(engine)
    assert (session_user, current_user) == baseline, stage
    assert current_user == session_user, stage
    assert current_user != SCHEMA_OWNER, stage
    # a leaked minos_admin would make this fail with InsufficientPrivilege.
    with engine.connect() as c:
        assert c.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == _L2F


def _prepared(url: str, plan: Any) -> Engine:
    engine = _engine(url)
    with engine.connect() as conn, conn.begin():
        seed_upstream_for_plan(conn, plan)
    return engine


def test_pooled_session_role_restored_after_persist_and_enqueue(
    isolated_pg_base_url: str, tmp_path: Path
) -> None:
    plan = _synthetic_plan(_SNAPSHOT_A)
    with scratch_database(isolated_pg_base_url, "minos_role_scope") as url:
        alembic_upgrade(url, _L2F)
        engine = _prepared(url, plan)  # ONE reusable pooled engine for the whole test
        try:
            # 1. baseline identity before any elevated work
            baseline = _identity(engine)
            assert baseline[0] == baseline[1] != SCHEMA_OWNER

            # 2. persist successfully
            result = PS._persist_experiment_plan_with_trust(
                engine, plan, _CS, publisher=_publisher(_provisioned_root(tmp_path))
            )
            assert result.plan_created is True
            # 3. same pooled engine, fresh connection: no leaked role
            _assert_no_leak(engine, baseline, "after persist")

            # 4. bounded enqueue succeeds
            enq = EN._enqueue_experiment_jobs_with_trust(engine, plan, _CS, start=0, count=3)
            assert enq.created_count == 3
            # 5. same pooled engine again: still no leaked role
            _assert_no_leak(engine, baseline, "after enqueue")
        finally:
            engine.dispose()


def test_pooled_session_role_restored_after_persist_rollback(
    isolated_pg_base_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """6. A PRE-COMMIT persistence failure rolls back; the role must still be restored."""
    plan = _synthetic_plan(_SNAPSHOT_A)
    with scratch_database(isolated_pg_base_url, "minos_role_scope") as url:
        alembic_upgrade(url, _L2F)
        engine = _prepared(url, plan)
        try:
            baseline = _identity(engine)

            def _boom(*_a: Any, **_k: Any) -> None:
                raise RuntimeError("injected pre-commit persistence failure")

            monkeypatch.setattr(PS, "_verify_persisted_graph", _boom)
            with pytest.raises(RuntimeError):
                PS._persist_experiment_plan_with_trust(
                    engine, plan, _CS, publisher=_publisher(_provisioned_root(tmp_path))
                )
            monkeypatch.undo()

            _assert_no_leak(engine, baseline, "after persist rollback")
            # nothing was committed by the failed attempt.
            with engine.connect() as c:
                assert (
                    int(
                        c.execute(
                            text("SELECT count(*) FROM experiments.l2f_experiment_plans")
                        ).scalar_one()
                    )
                    == 0
                )
        finally:
            engine.dispose()


def test_pooled_session_role_restored_after_enqueue_rollback(
    isolated_pg_base_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """7. An enqueue failure rolls back; the role must still be restored."""
    plan = _synthetic_plan(_SNAPSHOT_A)
    with scratch_database(isolated_pg_base_url, "minos_role_scope") as url:
        alembic_upgrade(url, _L2F)
        engine = _prepared(url, plan)
        try:
            PS._persist_experiment_plan_with_trust(
                engine, plan, _CS, publisher=_publisher(_provisioned_root(tmp_path))
            )
            baseline = _identity(engine)

            def _boom(*_a: Any, **_k: Any) -> None:
                raise RuntimeError("injected enqueue failure")

            monkeypatch.setattr(EN, "_member_index_map", _boom)
            with pytest.raises(RuntimeError):
                EN._enqueue_experiment_jobs_with_trust(engine, plan, _CS, start=0, count=2)
            monkeypatch.undo()

            _assert_no_leak(engine, baseline, "after enqueue rollback")
            with engine.connect() as c:
                assert (
                    int(
                        c.execute(
                            text("SELECT count(*) FROM experiments.l2f_experiment_jobs")
                        ).scalar_one()
                    )
                    == 0
                )
        finally:
            engine.dispose()


def test_transaction_local_privileges_are_still_sufficient(
    isolated_pg_base_url: str, tmp_path: Path
) -> None:
    """The elevation still WORKS: SET LOCAL grants the same in-transaction authority, so the
    full persist -> enqueue -> replay path succeeds and writes real rows."""
    plan = _synthetic_plan(_SNAPSHOT_A)
    with scratch_database(isolated_pg_base_url, "minos_role_scope") as url:
        alembic_upgrade(url, _L2F)
        engine = _prepared(url, plan)
        try:
            root = _provisioned_root(tmp_path)
            first = PS._persist_experiment_plan_with_trust(
                engine, plan, _CS, publisher=_publisher(root)
            )
            replay = PS._persist_experiment_plan_with_trust(
                engine, plan, _CS, publisher=_publisher(root)
            )
            assert first.plan_created is True and replay.replay is True
            EN._enqueue_experiment_jobs_with_trust(engine, plan, _CS, start=0, count=4)
            EN._enqueue_experiment_jobs_with_trust(engine, plan, _CS, start=2, count=4)
            with engine.connect() as c:
                assert (
                    int(
                        c.execute(
                            text("SELECT count(*) FROM experiments.l2f_experiment_plan_members")
                        ).scalar_one()
                    )
                    == plan.train_member_count
                )
                assert (
                    int(
                        c.execute(
                            text("SELECT count(*) FROM experiments.l2f_experiment_jobs")
                        ).scalar_one()
                    )
                    == 6
                )
        finally:
            engine.dispose()
