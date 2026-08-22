"""The complete stepwise Alembic chain, as one maintained test module.

This replaces eight inline workflow steps that each ran `alembic downgrade` followed by a Python
heredoc embedded in YAML. The behaviour they asserted is preserved exactly and is now
version-controlled, reviewable and runnable locally:

    head (0008) -> 0005 -> 0004 -> 0003 -> 0002 -> 0001 -> base -> head

At every stop the schema is checked in both directions: the objects the stage owns must be gone,
and the objects the previous stage owns must still be there. A downgrade that removes too much is
as much a defect as one that removes too little, and only a stepwise walk can tell them apart —
`test_migration_lifecycle.py` proves head-to-base cleanliness, not each intermediate step.

Scratch databases only; the operational store is never touched.
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import create_engine, text

from minos_engine.storage.constants import SCHEMAS
from minos_engine.storage.database import normalize_database_url

from .conftest import alembic_downgrade, alembic_upgrade, scratch_database

pytestmark = [pytest.mark.integration, pytest.mark.postgres, pytest.mark.migration]

_HEAD = "0008_l2f_execution_results"

#: table-name probes per stage, used to prove presence and absence at each stop.
_L2F = ("l2f_experiment_plans", "l2f_experiment_jobs", "l2f_execution_results")
_L2E = ("feature_sets", "feature_matrices", "feature_matrix_members")
_L2D = ("bam_profiles", "profile_snapshots", "profile_snapshot_members")
#: v2 introduces snapshot/epoch allocation; v1 is the frozen registry + allocation trio.
_L2C_V2 = ("split_snapshots", "split_epoch_allocations")
_L2C_V1 = ("dataset_registry", "split_allocations", "dataset_evaluation_identity")


def _count_tables(url: str, names: tuple[str, ...]) -> int:
    engine = create_engine(normalize_database_url(url))
    try:
        with engine.connect() as conn:
            return int(
                conn.execute(
                    text(
                        "SELECT count(*) FROM information_schema.tables WHERE table_name = ANY(:n)"
                    ),
                    {"n": list(names)},
                ).scalar_one()
            )
    finally:
        engine.dispose()


def _count_stage_schemas(url: str) -> int:
    engine = create_engine(normalize_database_url(url))
    try:
        with engine.connect() as conn:
            return int(
                conn.execute(
                    text("SELECT count(*) FROM pg_namespace WHERE nspname = ANY(:s)"),
                    {"s": list(SCHEMAS)},
                ).scalar_one()
            )
    finally:
        engine.dispose()


def _revision(url: str) -> str:
    engine = create_engine(normalize_database_url(url))
    try:
        with engine.connect() as conn:
            return str(conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one())
    finally:
        engine.dispose()


def _trainer_can_select_artifacts(url: str) -> Any:
    engine = create_engine(normalize_database_url(url))
    try:
        with engine.connect() as conn:
            return conn.execute(
                text("SELECT has_table_privilege('minos_trainer','catalog.artifacts','SELECT')")
            ).scalar_one()
    finally:
        engine.dispose()


def test_stepwise_downgrade_and_reupgrade_chain(pg_base_url: str) -> None:
    """Walk the whole chain, asserting presence AND absence at every stop."""
    with scratch_database(pg_base_url, "minos_stepwise_chain") as url:
        # ---- head: every stage present -------------------------------------------------
        alembic_upgrade(url, "head")
        assert _revision(url) == _HEAD
        assert _count_tables(url, _L2F) == 3, "L2-F tables missing at head"
        assert _count_tables(url, _L2E) == 3, "L2-E tables missing at head"
        # the 0005 grant posture: the trainer must NOT hold SELECT on catalog.artifacts
        assert not _trainer_can_select_artifacts(url), (
            "trainer catalog.artifacts SELECT must be revoked at 0005"
        )

        # ---- 0005: L2-F removed, L2-E retained -----------------------------------------
        alembic_downgrade(url, "0005_l2e_feature_view")
        assert _count_tables(url, _L2F) == 0, "L2-F objects not removed on downgrade to 0005"
        assert _count_tables(url, _L2E) == 3, "L2-E objects lost on downgrade to 0005"

        # ---- 0004: L2-E removed, L2-D retained -----------------------------------------
        alembic_downgrade(url, "0004_l2d_profile_ingestion")
        assert _count_tables(url, _L2E) == 0, "L2-E objects not removed on downgrade to 0004"
        assert _count_tables(url, _L2D) == 3, "L2-D objects lost on downgrade to 0004"

        # ---- 0003: L2-D removed, split v2 retained -------------------------------------
        alembic_downgrade(url, "0003_l2c_split_v2_epochs")
        assert _count_tables(url, _L2D) == 0, "L2-D objects not removed on downgrade to 0003"
        assert _count_tables(url, _L2C_V2) == 2, "split v2 objects lost on downgrade to 0003"

        # ---- 0002: split v2 removed, split v1 retained ---------------------------------
        alembic_downgrade(url, "0002_l2c_dataset_split")
        assert _count_tables(url, _L2C_V2) == 0, "v2 epoch objects not removed on downgrade to 0002"
        assert _count_tables(url, _L2C_V1) == 3, "frozen v1 L2-C tables not retained at 0002"

        # ---- 0001: L2-C removed, L2-B retained -----------------------------------------
        alembic_downgrade(url, "0001_l2b_initial")
        assert _count_tables(url, _L2C_V1) == 0, "L2-C objects not removed on downgrade to 0001"
        assert _count_stage_schemas(url) > 0, "L2-B schemas lost on downgrade to 0001"

        # ---- base: every stage-owned schema removed ------------------------------------
        alembic_downgrade(url, "base")
        assert _count_stage_schemas(url) == 0, "stage schemas not removed at base"

        # ---- re-upgrade: the chain is reversible ---------------------------------------
        alembic_upgrade(url, "head")
        assert _revision(url) == _HEAD
        assert _count_tables(url, _L2F) == 3, "L2-F objects missing after re-upgrade"
        assert _count_tables(url, _L2E) == 3, "L2-E objects missing after re-upgrade"
        assert not _trainer_can_select_artifacts(url), (
            "trainer grant posture not restored after re-upgrade"
        )
