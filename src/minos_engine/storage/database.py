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

from sqlalchemy import Connection, Engine, create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from minos_engine.common.errors import MinosEngineError

from .constants import CANONICAL_OPERATIONAL_DATABASE_NAME, ENV_DATABASE_URL

__all__ = [
    "DatabaseNotConfiguredError",
    "OperationalDatabaseIdentityError",
    "database_url",
    "normalize_database_url",
    "create_db_engine",
    "make_session_factory",
    "session_scope",
    "connected_database_name",
    "verify_operational_database_identity",
    "verify_operational_engine_identity",
]

_PSYCOPG_PREFIX = "postgresql+psycopg://"


class DatabaseNotConfiguredError(MinosEngineError):
    """``MINOS_DATABASE_URL`` is unset/empty, or names an unsupported backend."""


class OperationalDatabaseIdentityError(MinosEngineError):
    """The CONNECTED database is not the canonical operational store.

    Raised by the fail-closed operational identity check when the live
    ``current_database()`` of a session does not equal
    :data:`CANONICAL_OPERATIONAL_DATABASE_NAME`. This is decided from the connected
    session — never by parsing the DSN string — so a URL that merely *names* the
    canonical database but resolves elsewhere cannot pass.
    """


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


# --------------------------------------------------------------------------- #
# canonical operational-database identity (fail-closed; live current_database())
# --------------------------------------------------------------------------- #
def connected_database_name(conn: Connection) -> str:
    """Return the LIVE ``current_database()`` of the connected session.

    This is the database PostgreSQL is actually serving on this connection — read from
    the server, not parsed from the DSN string. Generic; imposes no canonical name, so
    synthetic / scratch integration databases keep working.
    """
    return str(conn.execute(text("SELECT current_database()")).scalar_one())


def verify_operational_database_identity(conn: Connection) -> str:
    """Fail closed unless the CONNECTED database is the canonical operational store.

    Queries the live session (``current_database()``) and requires it to equal
    :data:`CANONICAL_OPERATIONAL_DATABASE_NAME`. Because the decision comes from the
    connected server and not the DSN text, a URL that merely mentions the canonical
    name (e.g. in a host, role, or ``application_name``) while resolving to a different
    database is rejected. Returns the confirmed name on success.

    This is intended only for production/accepted operational mutation boundaries; it
    must NOT be wired into generic helpers or synthetic/scratch tests.
    """
    name = connected_database_name(conn)
    if name != CANONICAL_OPERATIONAL_DATABASE_NAME:
        raise OperationalDatabaseIdentityError(
            f"connected database is {name!r}, not the canonical operational store "
            f"{CANONICAL_OPERATIONAL_DATABASE_NAME!r}; refusing operational mutation"
        )
    return name


def verify_operational_engine_identity(engine: Engine) -> str:
    """Open one connection and apply :func:`verify_operational_database_identity`.

    Convenience for operational write boundaries that hold an :class:`Engine` (e.g. the
    accepted epoch-1 feature-matrix builder). Fail-closed with the same typed error.
    """
    with engine.connect() as conn:
        return verify_operational_database_identity(conn)
