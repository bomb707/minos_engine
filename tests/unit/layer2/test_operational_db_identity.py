"""Canonical operational-database identity contract (unit, no real DB).

These tests pin the canonical name and prove the fail-closed identity guard decides
from the *connected session's* ``current_database()`` — never from the DSN string.
Real-PostgreSQL coverage lives in
``tests/integration/layer2_db/test_operational_db_identity.py``.
"""

from __future__ import annotations

from typing import Any

import pytest

from minos_engine.storage import CANONICAL_OPERATIONAL_DATABASE_NAME as PKG_CANON
from minos_engine.storage.constants import CANONICAL_OPERATIONAL_DATABASE_NAME, ENV_DATABASE_URL
from minos_engine.storage.database import (
    DatabaseNotConfiguredError,
    OperationalDatabaseIdentityError,
    connected_database_name,
    database_url,
    verify_operational_database_identity,
)


class _FakeResult:
    def __init__(self, value: str) -> None:
        self._value = value

    def scalar_one(self) -> str:
        return self._value


class _FakeConnection:
    """Minimal Connection stand-in: ``current_database()`` returns a fixed value that is
    independent of any DSN, so a passing check can only come from reading the live
    session result (not string-matching a URL)."""

    def __init__(self, current_database: str) -> None:
        self._current = current_database
        self.executed = 0

    def execute(self, *_args: Any, **_kwargs: Any) -> _FakeResult:
        self.executed += 1
        return _FakeResult(self._current)


def test_canonical_name_is_exactly_minos_engine_db() -> None:
    assert CANONICAL_OPERATIONAL_DATABASE_NAME == "minos_engine_db"
    # exported from the storage package too (single source of truth)
    assert PKG_CANON == "minos_engine_db"


def test_connected_operational_name_passes() -> None:
    conn = _FakeConnection("minos_engine_db")
    assert verify_operational_database_identity(conn) == "minos_engine_db"  # type: ignore[arg-type]
    assert conn.executed == 1  # it actually queried the connection


def test_other_name_fails_with_typed_error() -> None:
    conn = _FakeConnection("minos_l2e_features")
    with pytest.raises(OperationalDatabaseIdentityError) as exc:
        verify_operational_database_identity(conn)  # type: ignore[arg-type]
    # message names the offending + expected database, and never a DSN/password
    assert "minos_l2e_features" in str(exc.value)
    assert "minos_engine_db" in str(exc.value)


def test_prefix_named_database_still_fails_not_substring_match() -> None:
    # A name that CONTAINS the canonical string as a prefix must still fail: the guard
    # compares the exact live current_database(), not a substring of a DSN.
    conn = _FakeConnection("minos_engine_db_staging")
    with pytest.raises(OperationalDatabaseIdentityError):
        verify_operational_database_identity(conn)  # type: ignore[arg-type]


def test_decision_reads_connection_not_dsn() -> None:
    # The fake connection carries NO url; a pass is only possible by reading the live
    # current_database() result — proving the check is not DSN string matching.
    assert connected_database_name(_FakeConnection("anything_at_all")) == "anything_at_all"  # type: ignore[arg-type]
    assert verify_operational_database_identity(_FakeConnection("minos_engine_db")) == (  # type: ignore[arg-type]
        "minos_engine_db"
    )


def test_missing_database_url_still_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(ENV_DATABASE_URL, raising=False)
    with pytest.raises(DatabaseNotConfiguredError):
        database_url()
