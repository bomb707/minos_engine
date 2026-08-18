"""L2-C migration lifecycle on real PostgreSQL 16: upgrade / downgrade / re-upgrade."""

from __future__ import annotations

from sqlalchemy import Engine, create_engine, text

from minos_engine.storage.database import normalize_database_url
from tests.integration.layer2_db.conftest import (
    alembic_downgrade,
    alembic_upgrade,
    scratch_database,
)

_HEAD = "0002_l2c_dataset_split"
_L2B = "0001_l2b_initial"
_L2C_TABLES = {"dataset_registry", "split_allocations", "dataset_evaluation_identity"}
_L2B_SCHEMAS = ["catalog", "profiling", "experiments", "evaluation", "models", "runtime", "audit"]


def test_postgres_major_version_is_16(l2c_engine: Engine):
    with l2c_engine.connect() as c:
        num = c.execute(text("SHOW server_version_num")).scalar()
    assert int(str(num)) // 10000 == 16


def _tables(url: str, names: list[str] | None = None) -> set[str]:
    eng = create_engine(normalize_database_url(url))
    try:
        with eng.connect() as c:
            return set(
                c.execute(
                    text(
                        "SELECT table_name FROM information_schema.tables WHERE table_name=ANY(:n)"
                    ),
                    {"n": names or list(_L2C_TABLES)},
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


def _l2b_table_count(url: str) -> int:
    eng = create_engine(normalize_database_url(url))
    try:
        with eng.connect() as c:
            return int(
                c.execute(
                    text(
                        "SELECT count(*) FROM information_schema.tables "
                        "WHERE table_schema = ANY(:s) AND table_name NOT IN "
                        "('dataset_registry','split_allocations','dataset_evaluation_identity')"
                    ),
                    {"s": _L2B_SCHEMAS},
                ).scalar()
                or 0
            )
    finally:
        eng.dispose()


def test_l2c_migration_lifecycle(pg_base_url: str):
    with scratch_database(pg_base_url, "minos_l2c_life") as url:
        alembic_upgrade(url, "head")
        assert _head(url) == _HEAD
        assert _tables(url) == _L2C_TABLES
        # L2-C objects are owned by minos_admin.
        eng = create_engine(normalize_database_url(url))
        try:
            with eng.connect() as c:
                owners = set(
                    c.execute(
                        text("SELECT tableowner FROM pg_tables WHERE tablename = ANY(:n)"),
                        {"n": list(_L2C_TABLES)},
                    ).scalars()
                )
                views = set(
                    c.execute(
                        text(
                            "SELECT viewname FROM pg_views "
                            "WHERE viewname IN ('training_dataset_allocations','locked_allocations')"
                        )
                    ).scalars()
                )
        finally:
            eng.dispose()
        assert owners == {"minos_admin"}
        assert views == {"training_dataset_allocations", "locked_allocations"}

        # second upgrade is a no-op
        alembic_upgrade(url, "head")
        assert _head(url) == _HEAD

        # downgrade removes ONLY L2-C objects; L2-B (10 tables) remains intact
        alembic_downgrade(url, _L2B)
        assert _head(url) == _L2B
        assert _tables(url) == set()
        assert _l2b_table_count(url) == 10

        # re-upgrade succeeds
        alembic_upgrade(url, "head")
        assert _head(url) == _HEAD
        assert _tables(url) == _L2C_TABLES
