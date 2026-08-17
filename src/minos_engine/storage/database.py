"""Engine / session infrastructure (L2-B) — env-driven and fail-closed.

Importing this module (or ``minos_engine``) opens **no** database connection. The
database URL comes only from ``MINOS_DATABASE_URL`` (psycopg 3 driver); there is no
implicit SQLite fallback and no hard-coded host. Missing configuration fails closed
the moment a database operation is requested. Transaction boundaries are explicit:
``session_scope`` commits on success and rolls back on any exception, always closing
the session.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker

from minos_engine.common.errors import MinosEngineError

from .constants import ENV_DATABASE_URL

__all__ = [
    "DatabaseNotConfiguredError",
    "database_url",
    "normalize_database_url",
    "create_db_engine",
    "make_session_factory",
    "session_scope",
]

_PSYCOPG_PREFIX = "postgresql+psycopg://"


class DatabaseNotConfiguredError(MinosEngineError):
    """``MINOS_DATABASE_URL`` is unset/empty, or names an unsupported backend."""


def normalize_database_url(url: str) -> str:
    """Return a psycopg-3 PostgreSQL URL, rejecting SQLite / unsupported backends."""
    if not url or not url.strip():
        raise DatabaseNotConfiguredError(f"{ENV_DATABASE_URL} is empty")
    u = url.strip()
    if u.startswith("sqlite"):
        raise DatabaseNotConfiguredError("SQLite is not a supported backend (PostgreSQL 16 only)")
    if u.startswith(_PSYCOPG_PREFIX):
        return u
    if u.startswith("postgresql+"):
        raise DatabaseNotConfiguredError(
            "only the psycopg driver is supported (postgresql+psycopg)"
        )
    if u.startswith("postgresql://"):
        return _PSYCOPG_PREFIX + u[len("postgresql://") :]
    if u.startswith("postgres://"):
        return _PSYCOPG_PREFIX + u[len("postgres://") :]
    raise DatabaseNotConfiguredError("unsupported database URL scheme (expected PostgreSQL)")


def database_url() -> str:
    """Read and normalize ``MINOS_DATABASE_URL`` (fail closed if unset/empty)."""
    raw = os.environ.get(ENV_DATABASE_URL)
    if raw is None or not raw.strip():
        raise DatabaseNotConfiguredError(
            f"{ENV_DATABASE_URL} is not set; database operations require an explicit "
            "PostgreSQL URL (no default, no SQLite fallback)"
        )
    return normalize_database_url(raw)


def create_db_engine(url: str | None = None, *, echo: bool = False) -> Engine:
    """Create a SQLAlchemy engine (explicit, testable; no connection until used)."""
    resolved = normalize_database_url(url) if url is not None else database_url()
    return create_engine(resolved, echo=echo, future=True, pool_pre_ping=True)


def make_session_factory(engine: Engine) -> sessionmaker[Session]:
    """A session factory that never autocommits or autoflushes unexpectedly."""
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, future=True)


@contextmanager
def session_scope(factory: sessionmaker[Session]) -> Iterator[Session]:
    """Explicit transaction boundary: commit on success, rollback on error, always close."""
    session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
