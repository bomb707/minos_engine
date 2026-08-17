"""CI guard: PostgreSQL integration must actually run in CI (never all-skipped)."""

from __future__ import annotations

import os

from .conftest import postgres_base_url


def test_postgres_available_when_ci():
    """In GitHub CI this must fail if no PostgreSQL backend is reachable.

    Locally (no ``CI`` env) it is a no-op; the L2-B integration suite still runs
    against the bundled ephemeral PostgreSQL 16 via ``pgserver``.
    """
    if os.environ.get("CI"):
        assert postgres_base_url() is not None, (
            "PostgreSQL 16 integration must run in CI; MINOS_DATABASE_URL is required "
            "and integration tests must never all skip"
        )


def test_backend_is_postgres_not_sqlite():
    url = os.environ.get("MINOS_DATABASE_URL", "")
    assert "sqlite" not in url.lower()
