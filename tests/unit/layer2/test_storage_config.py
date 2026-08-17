"""Storage configuration: fail-closed, no SQLite, no connection at import (unit)."""

from __future__ import annotations

import pytest

from minos_engine.storage.constants import ENV_DATABASE_URL, ROLES, SCHEMAS
from minos_engine.storage.database import (
    DatabaseNotConfiguredError,
    create_db_engine,
    database_url,
    normalize_database_url,
)


def test_seven_schemas_and_five_roles_constants():
    assert len(SCHEMAS) == 7 and len(set(SCHEMAS)) == 7
    assert len(ROLES) == 5 and len(set(ROLES)) == 5


def test_missing_env_fails_closed(monkeypatch):
    monkeypatch.delenv(ENV_DATABASE_URL, raising=False)
    with pytest.raises(DatabaseNotConfiguredError):
        database_url()


def test_sqlite_rejected():
    with pytest.raises(DatabaseNotConfiguredError):
        normalize_database_url("sqlite:///tmp/x.db")


def test_unsupported_scheme_rejected():
    with pytest.raises(DatabaseNotConfiguredError):
        normalize_database_url("mysql://user@host/db")


def test_non_psycopg_driver_rejected():
    with pytest.raises(DatabaseNotConfiguredError):
        normalize_database_url("postgresql+asyncpg://u@h/db")


def test_normalizes_to_psycopg():
    assert normalize_database_url("postgresql://u@h/db") == "postgresql+psycopg://u@h/db"
    assert normalize_database_url("postgres://u@h/db") == "postgresql+psycopg://u@h/db"
    assert normalize_database_url("postgresql+psycopg://u@h/db") == "postgresql+psycopg://u@h/db"


def test_empty_url_rejected():
    with pytest.raises(DatabaseNotConfiguredError):
        normalize_database_url("   ")


def test_credentials_not_leaked_in_errors():
    secret = "SuperSecretPw123"
    with pytest.raises(DatabaseNotConfiguredError) as exc:
        normalize_database_url(f"mysql://user:{secret}@host/db")
    assert secret not in str(exc.value)


def test_create_engine_is_lazy_and_does_not_connect():
    # Engine creation must not open a connection (nothing is reachable at this URL).
    engine = create_db_engine("postgresql+psycopg://u@127.0.0.1:1/none")
    try:
        assert engine.url.drivername == "postgresql+psycopg"
    finally:
        engine.dispose()


def test_importing_storage_does_not_require_database(monkeypatch):
    monkeypatch.delenv(ENV_DATABASE_URL, raising=False)
    import importlib

    import minos_engine.storage as storage
    import minos_engine.storage.models as models

    importlib.reload(storage)
    assert storage.SCHEMAS == SCHEMAS
    assert hasattr(models, "Artifact")
