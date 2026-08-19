"""L2-C v2 migration lifecycle on real PostgreSQL 16: upgrade / downgrade / re-upgrade.

Proves ``0003_l2c_split_v2_epochs`` adds ONLY the v2 epoch objects on top of the frozen
v1 L2-C schema, and that a downgrade to ``0002`` removes exactly the v2 objects while the
v1 dataset_registry + split_allocations + views remain intact (v1 stays historical).
"""

from __future__ import annotations

from sqlalchemy import Engine, create_engine, text

from minos_engine.storage.database import normalize_database_url
from tests.integration.layer2_db.conftest import (
    alembic_downgrade,
    alembic_upgrade,
    scratch_database,
)

_HEAD = "0003_l2c_split_v2_epochs"
_L2C_V1 = "0002_l2c_dataset_split"
_V2_TABLES = {"split_snapshots", "split_epoch_allocations"}
_V1_TABLES = {"dataset_registry", "split_allocations", "dataset_evaluation_identity"}


def test_postgres_major_version_is_16(l2c_v2_engine: Engine) -> None:
    with l2c_v2_engine.connect() as c:
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


def test_v2_migration_lifecycle(pg_base_url: str) -> None:
    with scratch_database(pg_base_url, "minos_l2c_v2_life") as url:
        alembic_upgrade(url, _HEAD)
        assert _head(url) == _HEAD
        assert _tables(url, _V2_TABLES) == _V2_TABLES

        # v2 objects are owned by minos_admin; all three partition-separated views exist.
        expected_views = {
            "training_epoch_allocations",
            "validation_epoch_allocations",
            "sealed_test_epoch_allocations",
        }
        eng = create_engine(normalize_database_url(url))
        try:
            with eng.connect() as c:
                owners = set(
                    c.execute(
                        text("SELECT tableowner FROM pg_tables WHERE tablename = ANY(:n)"),
                        {"n": list(_V2_TABLES)},
                    ).scalars()
                )
                views = set(
                    c.execute(
                        text("SELECT viewname FROM pg_views WHERE viewname = ANY(:v)"),
                        {"v": list(expected_views)},
                    ).scalars()
                )
                # the sealed-test view has NO privileges granted to any application role.
                sealed_grants = c.execute(
                    text(
                        "SELECT count(*) FROM information_schema.role_table_grants "
                        "WHERE table_schema = 'evaluation' "
                        "AND table_name = 'sealed_test_epoch_allocations' "
                        "AND grantee LIKE 'minos_%' AND grantee <> 'minos_admin'"
                    )
                ).scalar()
        finally:
            eng.dispose()
        assert owners == {"minos_admin"}
        assert views == expected_views
        assert sealed_grants == 0  # sealed at birth

        # second upgrade is a no-op
        alembic_upgrade(url, _HEAD)
        assert _head(url) == _HEAD

        # downgrade to v1 removes ONLY the v2 epoch objects; v1 L2-C tables remain intact
        alembic_downgrade(url, _L2C_V1)
        assert _head(url) == _L2C_V1
        assert _tables(url, _V2_TABLES) == set()
        assert _tables(url, _V1_TABLES) == _V1_TABLES

        # re-upgrade succeeds
        alembic_upgrade(url, _HEAD)
        assert _head(url) == _HEAD
        assert _tables(url, _V2_TABLES) == _V2_TABLES
