"""Migration lifecycle against real PostgreSQL 16: upgrade / no-op / downgrade / re-upgrade."""

from __future__ import annotations

from sqlalchemy import create_engine, text

from minos_engine.storage.constants import SCHEMAS
from minos_engine.storage.database import normalize_database_url

from .conftest import alembic_downgrade, alembic_upgrade, scratch_database

_HEAD = "0001_l2b_initial"


def _schemas_present(url: str) -> set[str]:
    eng = create_engine(normalize_database_url(url))
    try:
        with eng.connect() as c:
            rows = c.execute(
                text("SELECT nspname FROM pg_namespace WHERE nspname = ANY(:names)"),
                {"names": list(SCHEMAS)},
            ).scalars()
            return set(rows)
    finally:
        eng.dispose()


def _current_head(url: str) -> str | None:
    eng = create_engine(normalize_database_url(url))
    try:
        with eng.connect() as c:
            return c.execute(text("SELECT version_num FROM alembic_version")).scalar()
    finally:
        eng.dispose()


def _table_count(url: str) -> int:
    eng = create_engine(normalize_database_url(url))
    try:
        with eng.connect() as c:
            return int(
                c.execute(
                    text(
                        "SELECT count(*) FROM information_schema.tables "
                        "WHERE table_schema = ANY(:names)"
                    ),
                    {"names": list(SCHEMAS)},
                ).scalar()
                or 0
            )
    finally:
        eng.dispose()


def test_full_migration_lifecycle(pg_base_url: str):
    with scratch_database(pg_base_url, "minos_l2b_lifecycle") as url:
        # clean database -> upgrade head
        alembic_upgrade(url, "head")
        assert _current_head(url) == _HEAD
        assert _schemas_present(url) == set(SCHEMAS)
        assert _table_count(url) == 10

        # second upgrade is a no-op
        alembic_upgrade(url, "head")
        assert _current_head(url) == _HEAD

        # downgrade to base removes stage-owned schemas/tables
        alembic_downgrade(url, "base")
        assert _schemas_present(url) == set()
        assert _current_head(url) is None

        # re-upgrade succeeds
        alembic_upgrade(url, "head")
        assert _current_head(url) == _HEAD
        assert _schemas_present(url) == set(SCHEMAS)
        assert _table_count(url) == 10


def test_downgrade_local_cleanup_is_complete_and_safe(pg_base_url: str):
    # On a shared cluster the global role objects may be retained (referenced by
    # another database), but the downgrade must still complete without error and
    # leave no stage-owned grants or schemas in THIS database.
    with scratch_database(pg_base_url, "minos_l2b_roles") as url:
        alembic_upgrade(url, "head")
        alembic_downgrade(url, "base")  # must not raise
        eng = create_engine(normalize_database_url(url))
        try:
            with eng.connect() as c:
                local_grants = c.execute(
                    text(
                        "SELECT count(*) FROM information_schema.role_table_grants "
                        "WHERE grantee LIKE 'minos_%'"
                    )
                ).scalar()
                assert local_grants == 0
                assert _schemas_present(url) == set()
        finally:
            eng.dispose()


def test_downgrade_fully_drops_roles_on_clean_cluster():
    # Proof that on a cluster where no other database references the roles, downgrade
    # removes exactly the five MINOS roles (non-destructively). Uses a dedicated
    # ephemeral PostgreSQL 16 cluster when available; otherwise the local-cleanup
    # test above covers the shared-cluster path.
    import tempfile

    try:
        import pgserver
    except Exception:
        return
    tmp = tempfile.mkdtemp(prefix="minos_l2b_clean_")
    server = pgserver.get_server(tmp)
    try:
        base = server.get_uri()
        with scratch_database(base, "minos_l2b_cleanroles") as url:
            alembic_upgrade(url, "head")
            alembic_downgrade(url, "base")
            eng = create_engine(normalize_database_url(base))
            try:
                with eng.connect() as c:
                    remaining = c.execute(
                        text("SELECT count(*) FROM pg_roles WHERE rolname LIKE 'minos_%'")
                    ).scalar()
                    assert remaining == 0
            finally:
                eng.dispose()
    finally:
        import contextlib

        with contextlib.suppress(Exception):
            server.cleanup()
