"""L2-D migration lifecycle on real PostgreSQL 16: upgrade / downgrade / re-upgrade."""

from __future__ import annotations

from sqlalchemy import Engine, create_engine, text

from minos_engine.storage.database import normalize_database_url
from tests.integration.layer2_db.conftest import (
    alembic_downgrade,
    alembic_upgrade,
    scratch_database,
)

_HEAD = "0004_l2d_profile_ingestion"
_V2 = "0003_l2c_split_v2_epochs"
_L2D_TABLES = {
    "bam_profiles",
    "profile_ingest_attempts",
    "profile_snapshots",
    "profile_snapshot_members",
}


def test_postgres_major_version_is_16(l2d_engine: Engine) -> None:
    with l2d_engine.connect() as c:
        num = c.execute(text("SHOW server_version_num")).scalar()
    assert int(str(num)) // 10000 == 16


def _tables(url: str, names: set[str]) -> set[str]:
    eng = create_engine(normalize_database_url(url))
    try:
        with eng.connect() as c:
            return set(
                c.execute(
                    text(
                        "SELECT table_name FROM information_schema.tables WHERE table_name=ANY(:n)"
                    ),
                    {"n": list(names)},
                ).scalars()
            )
    finally:
        eng.dispose()


def _head(url: str) -> str | None:
    eng = create_engine(normalize_database_url(url))
    try:
        with eng.connect() as c:
            return c.execute(text("SELECT version_num FROM alembic_version")).scalar()
    finally:
        eng.dispose()


def test_l2d_migration_lifecycle(pg_base_url: str) -> None:
    with scratch_database(pg_base_url, "minos_l2d_life") as url:
        alembic_upgrade(url, "head")
        assert _head(url) == _HEAD
        assert _tables(url, _L2D_TABLES) == _L2D_TABLES

        eng = create_engine(normalize_database_url(url))
        try:
            with eng.connect() as c:
                owners = set(
                    c.execute(
                        text("SELECT tableowner FROM pg_tables WHERE tablename = ANY(:n)"),
                        {"n": list(_L2D_TABLES)},
                    ).scalars()
                )
                views = set(
                    c.execute(
                        text("SELECT viewname FROM pg_views WHERE viewname LIKE '%profile_members'")
                    ).scalars()
                )
                trainer_legacy = c.execute(
                    text(
                        "SELECT has_table_privilege('minos_trainer','profiling.profiles','SELECT')"
                    )
                ).scalar()
        finally:
            eng.dispose()
        assert owners == {"minos_admin"}
        assert views == {
            "training_profile_members",
            "validation_profile_members",
            "sealed_test_profile_members",
        }
        assert trainer_legacy is False  # legacy reads revoked while 0004 applied

        # downgrade removes ONLY L2-D objects; v2 epoch tables + legacy grants restored
        alembic_downgrade(url, _V2)
        assert _head(url) == _V2
        assert _tables(url, _L2D_TABLES) == set()
        eng = create_engine(normalize_database_url(url))
        try:
            with eng.connect() as c:
                v2_kept = c.execute(
                    text(
                        "SELECT count(*) FROM information_schema.tables "
                        "WHERE table_name IN ('split_snapshots','split_epoch_allocations')"
                    )
                ).scalar()
                trainer_restored = c.execute(
                    text(
                        "SELECT has_table_privilege('minos_trainer','profiling.profiles','SELECT')"
                    )
                ).scalar()
        finally:
            eng.dispose()
        assert v2_kept == 2
        assert trainer_restored is True

        alembic_upgrade(url, "head")
        assert _head(url) == _HEAD
        assert _tables(url, _L2D_TABLES) == _L2D_TABLES
