"""L2-F F3-A migration 0006 lifecycle + structure on real PostgreSQL 16 (scratch only).

Proves: single Alembic head 0006; 0005→0006→0005→0006 lifecycle; the five canonical
tables + additive composite-UNIQUE targets are created and fully removed on downgrade
(restoring exactly 0005); the declarative composite FKs and immutability triggers exist;
and migrations 0001–0005 remain byte-identical. Data-dependent direct-SQL train-lineage
rejections are added in a later F3-A step; this file freezes the schema lifecycle.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text

from minos_engine.storage.database import normalize_database_url
from minos_engine.storage.l2f_migration_contract import L2F_COMPOSITE_FKS as _COMPOSITE_FKS
from tests.integration.layer2_db.conftest import (
    alembic_downgrade,
    alembic_upgrade,
    scratch_database,
)

_HEAD = "0006_l2f_experiment_plan"
_PREV = "0005_l2e_feature_view"
_L2F_TABLES = (
    "l2f_experiment_plans",
    "l2f_experiment_plan_members",
    "l2f_config_payloads",
    "l2f_experiment_plan_configs",
    "l2f_experiment_jobs",
)

_COMPOSITE_TARGETS = (
    "uq_l2f_feature_matrices_composite",
    "uq_l2f_profile_snapshots_composite",
    "uq_l2f_feature_sets_composite",
    "uq_l2f_psm_composite",
    "uq_l2f_fmm_composite",
    "uq_l2f_artifacts_id_sha_media",
)


def _count(url: str, sql: str, **p: object) -> int:
    e = create_engine(normalize_database_url(url))
    try:
        with e.connect() as c:
            return int(c.execute(text(sql), p).scalar_one())
    finally:
        e.dispose()


def _l2f_tables(url: str) -> int:
    return _count(
        url,
        "SELECT count(*) FROM information_schema.tables "
        "WHERE table_schema='experiments' AND table_name = ANY(:n)",
        n=list(_L2F_TABLES),
    )


def test_single_head_is_0006() -> None:
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    script = ScriptDirectory.from_config(Config("alembic.ini"))
    assert script.get_heads() == [_HEAD]
    down = {r.down_revision for r in script.get_revisions(_HEAD)}
    assert down == {_PREV}  # descends exactly 0005


def test_migrations_0001_0005_byte_identical() -> None:
    from minos_engine.storage.l2f_migration_contract import ACCEPTED_PRIOR_MIGRATION_SHAS

    for rel, expected in ACCEPTED_PRIOR_MIGRATION_SHAS.items():
        got = hashlib.sha256(Path(rel).read_bytes()).hexdigest()
        assert got == expected, f"{rel} changed"


@pytest.fixture(scope="module")
def lifecycle_url(pg_base_url: str):
    with scratch_database(pg_base_url, "minos_l2f_lifecycle") as url:
        yield url


def test_full_lifecycle_and_downgrade_clean(lifecycle_url: str) -> None:
    url = lifecycle_url
    alembic_upgrade(url, _PREV)
    assert _l2f_tables(url) == 0

    alembic_upgrade(url, _HEAD)
    assert _count(url, "SELECT version_num = :h FROM alembic_version", h=_HEAD) == 1
    assert _l2f_tables(url) == 5
    # additive composite targets present
    assert _count(
        url,
        "SELECT count(*) FROM pg_constraint WHERE conname = ANY(:n)",
        n=list(_COMPOSITE_TARGETS),
    ) == len(_COMPOSITE_TARGETS)
    # declarative composite FKs present
    assert _count(
        url,
        "SELECT count(*) FROM pg_constraint WHERE contype='f' AND conname = ANY(:n)",
        n=list(_COMPOSITE_FKS),
    ) == len(_COMPOSITE_FKS)
    # immutability triggers present
    assert (
        _count(
            url,
            "SELECT count(*) FROM pg_trigger WHERE tgname LIKE 'trg_experiments_l2f%' OR tgname LIKE 'trg_l2f_jobs%'",
        )
        >= 6
    )
    # L2-E tables still present (0006 is additive)
    assert (
        _count(
            url,
            "SELECT count(*) FROM information_schema.tables WHERE table_schema='profiling' AND table_name='feature_matrices'",
        )
        == 1
    )

    alembic_downgrade(url, _PREV)
    assert _l2f_tables(url) == 0
    assert (
        _count(
            url,
            "SELECT count(*) FROM pg_constraint WHERE conname = ANY(:n)",
            n=list(_COMPOSITE_TARGETS),
        )
        == 0
    )
    assert (
        _count(
            url,
            "SELECT count(*) FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace WHERE n.nspname='experiments' AND p.proname='minos_l2f_reject_job_identity_change'",
        )
        == 0
    )
    # 0005 intact after downgrade
    assert (
        _count(
            url,
            "SELECT count(*) FROM information_schema.tables WHERE table_schema='profiling' AND table_name='feature_matrices'",
        )
        == 1
    )

    alembic_upgrade(url, _HEAD)
    assert _l2f_tables(url) == 5


def test_l2f_tables_owned_by_admin_no_app_grants(lifecycle_url: str) -> None:
    url = lifecycle_url  # already at head from the prior test's re-upgrade
    e = create_engine(normalize_database_url(url))
    try:
        with e.connect() as c:
            for t in _L2F_TABLES:
                owner = c.execute(
                    text(
                        "SELECT tableowner FROM pg_tables WHERE schemaname='experiments' AND tablename=:t"
                    ),
                    {"t": t},
                ).scalar_one()
                assert owner == "minos_admin"
                for role in ("minos_live", "minos_runner", "minos_trainer", "minos_evaluator"):
                    has = c.execute(
                        text("SELECT has_table_privilege(:r, 'experiments.' || :t, 'SELECT')"),
                        {"r": role, "t": t},
                    ).scalar_one()
                    assert has is False, f"{role} must have no F3-A access to {t}"
    finally:
        e.dispose()
