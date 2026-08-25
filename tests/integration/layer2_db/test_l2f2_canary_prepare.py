"""Control-plane canary preparation and the 0011 migration lifecycle, against real PostgreSQL.

Preparation is exercised on an EPHEMERAL database only. Nothing here touches the real baseline
store, and no GATK, hap.py, truth or score is involved.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import text

from minos_engine.storage.l2f2_canary_prepare import (
    CanaryPreparationError,
    prepare_l2f2_phase_a_canary,
)
from tests.integration.layer2_db.conftest import (
    alembic_downgrade,
    alembic_upgrade,
    scratch_database,
)
from tests.integration.layer2_db.test_l2f_plan_store import _engine

_BASELINE_DB = "minos_l2f2_baseline"
_RUNNER_BOUNDARY = "0011_l2f2_runner_boundary"
_CORRECTIVE = "0010_l2f2_evaluation_corrective"
_AUTHORITIES = "experiments.l2f2_execution_authorities"


def _inventory(engine: Any) -> dict[str, Any]:
    with engine.connect() as conn:
        return {
            "revision": conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one(),
            "authority_table": int(
                conn.execute(
                    text(
                        "SELECT count(*) FROM information_schema.tables "
                        " WHERE table_schema='experiments' "
                        "   AND table_name='l2f2_execution_authorities'"
                    )
                ).scalar_one()
            ),
            "l2f2_functions": sorted(
                r[0]
                for r in conn.execute(
                    text(
                        "SELECT p.proname FROM pg_proc p "
                        "  JOIN pg_namespace n ON n.oid = p.pronamespace "
                        " WHERE n.nspname='experiments' AND p.proname LIKE 'l2f2_%'"
                    )
                )
            ),
            "plan_composite": int(
                conn.execute(
                    text(
                        "SELECT count(*) FROM pg_constraint "
                        " WHERE conname = 'uq_l2f_experiment_plans_id_hash'"
                    )
                ).scalar_one()
            ),
            "runner_reads_revision": bool(
                conn.execute(
                    text(
                        "SELECT has_table_privilege('minos_runner', 'public.alembic_version', "
                        "'SELECT')"
                    )
                ).scalar_one()
            ),
        }


# --------------------------------------------------------------------------- #
# migration lifecycle
# --------------------------------------------------------------------------- #
def test_0011_downgrades_to_exactly_0010_and_upgrades_back(isolated_pg_base_url: str) -> None:
    with scratch_database(isolated_pg_base_url, _BASELINE_DB) as url:
        alembic_upgrade(url, _RUNNER_BOUNDARY)
        engine = _engine(url)
        try:
            at_0011 = _inventory(engine)
            assert at_0011["revision"] == _RUNNER_BOUNDARY
            assert at_0011["authority_table"] == 1
            assert at_0011["l2f2_functions"] == [
                "l2f2_register_execution_artifact",
                "l2f2_resolve_claimed_execution",
            ]
            assert at_0011["plan_composite"] == 1
            assert at_0011["runner_reads_revision"] is True
        finally:
            engine.dispose()

        alembic_downgrade(url, _CORRECTIVE)
        engine = _engine(url)
        try:
            at_0010 = _inventory(engine)
            assert at_0010 == {
                "revision": _CORRECTIVE,
                "authority_table": 0,
                "l2f2_functions": [],
                "plan_composite": 0,
                "runner_reads_revision": False,
            }
        finally:
            engine.dispose()

        alembic_upgrade(url, _RUNNER_BOUNDARY)
        engine = _engine(url)
        try:
            assert _inventory(engine) == at_0011, "re-upgrade did not restore the 0011 inventory"
        finally:
            engine.dispose()


def test_the_runner_role_gains_no_table_privilege_from_0011(isolated_pg_base_url: str) -> None:
    with scratch_database(isolated_pg_base_url, _BASELINE_DB) as url:
        alembic_upgrade(url, _RUNNER_BOUNDARY)
        engine = _engine(url)
        try:
            with engine.connect() as conn:
                for privilege in ("SELECT", "INSERT", "UPDATE", "DELETE"):
                    granted = conn.execute(
                        text("SELECT has_table_privilege('minos_runner', :t, :p)"),
                        {"t": _AUTHORITIES, "p": privilege},
                    ).scalar_one()
                    assert granted is False, f"minos_runner has {privilege} on the authority table"
                for role in ("minos_evaluator", "minos_trainer", "minos_live"):
                    for privilege in ("SELECT", "INSERT", "UPDATE", "DELETE"):
                        assert (
                            conn.execute(
                                text("SELECT has_table_privilege(:r, :t, :p)"),
                                {"r": role, "t": _AUTHORITIES, "p": privilege},
                            ).scalar_one()
                            is False
                        )
                # the runner CAN execute exactly the two narrow functions
                for function in (
                    "experiments.l2f2_resolve_claimed_execution(text, uuid, text)",
                    "experiments.l2f2_register_execution_artifact(text, char, text, integer)",
                ):
                    assert (
                        conn.execute(
                            text("SELECT has_function_privilege('minos_runner', :f, 'EXECUTE')"),
                            {"f": function},
                        ).scalar_one()
                        is True
                    )
                    for role in ("minos_evaluator", "minos_trainer", "minos_live"):
                        assert (
                            conn.execute(
                                text("SELECT has_function_privilege(:r, :f, 'EXECUTE')"),
                                {"r": role, "f": function},
                            ).scalar_one()
                            is False
                        )
        finally:
            engine.dispose()


# --------------------------------------------------------------------------- #
# control-plane preparation
# --------------------------------------------------------------------------- #
@pytest.fixture
def baseline(isolated_pg_base_url: str) -> Any:
    with scratch_database(isolated_pg_base_url, _BASELINE_DB) as url:
        alembic_upgrade(url, _RUNNER_BOUNDARY)
        engine = _engine(url)
        try:
            yield engine
        finally:
            engine.dispose()


def test_preparation_refuses_a_database_at_the_wrong_revision(
    isolated_pg_base_url: str, tmp_path: Path
) -> None:
    with scratch_database(isolated_pg_base_url, _BASELINE_DB) as url:
        alembic_upgrade(url, _CORRECTIVE)
        engine = _engine(url)
        try:
            with pytest.raises(CanaryPreparationError, match="revision"):
                prepare_l2f2_phase_a_canary(engine, config_artifact_root=tmp_path)
        finally:
            engine.dispose()


def test_preparation_refuses_a_plan_it_did_not_create(
    isolated_pg_base_url: str, tmp_path: Path
) -> None:
    """It never enqueues the canary beside state it did not create.

    A genuinely persisted synthetic plan stands in for "unexplained state": it is a real,
    well-formed plan that is simply not the frozen Phase-A plan.
    """
    from minos_engine.storage.l2f_plan_store import _persist_experiment_plan_with_trust
    from tests.integration.layer2_db.l2f_plan_seed import seed_upstream_for_plan
    from tests.integration.layer2_db.test_l2f_execution import _prepare_env
    from tests.integration.layer2_db.test_l2f_plan_store import (
        _CS,
        _SNAPSHOT_A,
        _provisioned_root,
        _publisher,
    )

    plan, identity, _dataset_root = _prepare_env(
        isolated_pg_base_url, tmp_path, _SNAPSHOT_A, jobs=1
    )
    with scratch_database(isolated_pg_base_url, _BASELINE_DB) as url:
        alembic_upgrade(url, "0006_l2f_experiment_plan")
        engine = _engine(url)
        try:
            with engine.connect() as conn, conn.begin():
                seed_upstream_for_plan(conn, plan, dataset_identity=identity)
            _persist_experiment_plan_with_trust(
                engine, plan, _CS, publisher=_publisher(_provisioned_root(tmp_path))
            )
            engine.dispose()
            alembic_upgrade(url, _RUNNER_BOUNDARY)
            engine = _engine(url)

            with pytest.raises(CanaryPreparationError, match="unexplained experiment plan"):
                prepare_l2f2_phase_a_canary(engine, config_artifact_root=tmp_path / "cfg")
        finally:
            engine.dispose()
