"""Template lifecycle + xdist isolation — the test infrastructure's own contract.

Two demonstrated defects are pinned here: template databases must never survive the pytest
session on a persistent cluster (physical cleanup, not just cache bookkeeping), and xdist
workers must fail closed rather than share one MINOS_DATABASE_URL cluster.
"""

from __future__ import annotations

import contextlib
import tempfile
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text

from minos_engine.storage.database import normalize_database_url
from tests.integration.layer2_db import conftest as infra
from tests.integration.layer2_db.conftest import (
    _TEMPLATE_CACHE,
    _resolve_revision,
    alembic_upgrade,
    database_mode,
    drop_session_templates,
    scratch_database,
)

_L2F = "0006_l2f_experiment_plan"


def _database_names(base: str) -> set[str]:
    engine = create_engine(normalize_database_url(base), isolation_level="AUTOCOMMIT")
    try:
        with engine.connect() as c:
            return {r[0] for r in c.execute(text("SELECT datname FROM pg_database"))}
    finally:
        engine.dispose()


@contextlib.contextmanager
def _dedicated_cluster():
    """A throwaway pgserver cluster so the lifecycle proof never disturbs shared state."""
    pgserver = pytest.importorskip("pgserver")
    tmp = tempfile.mkdtemp(prefix="minos_tmpl_life_")
    server = pgserver.get_server(tmp)
    try:
        yield server.get_uri()
    finally:
        with contextlib.suppress(Exception):
            server.cleanup()


# --------------------------------------------------------------------------- #
# DEFECT A — templates must not survive the session on a persistent cluster
# --------------------------------------------------------------------------- #
def test_session_teardown_drops_owned_templates_physically() -> None:
    """The full lifecycle, against a real cluster: create -> exists -> teardown -> gone.

    Other clusters' cache entries are stashed so this proof has no collateral rebuild cost;
    the proof itself is physical (pg_database), never dictionary bookkeeping alone.
    """
    stashed = dict(_TEMPLATE_CACHE)
    _TEMPLATE_CACHE.clear()
    try:
        with (
            _dedicated_cluster() as base,
            scratch_database(base, "minos_tmpl_life_scratch") as url,
        ):
            alembic_upgrade(url, _L2F)

            assert len(_TEMPLATE_CACHE) == 1, "the upgrade should have built one template"
            template = next(iter(_TEMPLATE_CACHE.values()))
            assert template.startswith("minos_tmpl_")
            names = _database_names(base)
            assert template in names, "template must exist PHYSICALLY"
            assert "minos_tmpl_life_scratch" in names

            drop_session_templates()

            names = _database_names(base)
            assert template not in names, "session teardown must drop the physical template"
            # unrelated databases are untouched
            assert "minos_tmpl_life_scratch" in names
            assert "postgres" in names or "template1" in names
            assert _TEMPLATE_CACHE == {}, "cache entries must be cleared"

            # idempotent: calling again with nothing owned is safe
            drop_session_templates()
            assert _TEMPLATE_CACHE == {}
    finally:
        _TEMPLATE_CACHE.clear()
        _TEMPLATE_CACHE.update(stashed)


def test_teardown_never_matches_by_name_pattern() -> None:
    """Cleanup drops ONLY owned entries — a foreign minos_tmpl_* database is not touched."""
    stashed = dict(_TEMPLATE_CACHE)
    _TEMPLATE_CACHE.clear()
    try:
        with _dedicated_cluster() as base:
            foreign = "minos_tmpl_9999999_foreign"
            engine = create_engine(normalize_database_url(base), isolation_level="AUTOCOMMIT")
            try:
                with engine.connect() as c:
                    c.execute(text(f'CREATE DATABASE "{foreign}"'))
            finally:
                engine.dispose()

            drop_session_templates()  # owns nothing -> must not touch the foreign database
            assert foreign in _database_names(base)
    finally:
        _TEMPLATE_CACHE.clear()
        _TEMPLATE_CACHE.update(stashed)


# --------------------------------------------------------------------------- #
# DEFECT B — xdist + shared MINOS_DATABASE_URL fails closed
# --------------------------------------------------------------------------- #
def test_xdist_worker_with_shared_database_url_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PYTEST_XDIST_WORKER", "gw0")
    monkeypatch.setenv("MINOS_DATABASE_URL", "postgresql+psycopg://x:x@127.0.0.1:5433/shared")
    with pytest.raises(pytest.fail.Exception, match="cannot run against a shared"):
        infra._require_xdist_isolation()


def test_xdist_worker_without_shared_url_is_permitted(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ephemeral mode is the supported parallel mode: each worker gets its own cluster."""
    monkeypatch.setenv("PYTEST_XDIST_WORKER", "gw0")
    monkeypatch.delenv("MINOS_DATABASE_URL", raising=False)
    infra._require_xdist_isolation()  # must not raise
    assert database_mode() == "ephemeral"


def test_serial_shared_url_is_permitted(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PYTEST_XDIST_WORKER", raising=False)
    monkeypatch.setenv("MINOS_DATABASE_URL", "postgresql+psycopg://x:x@127.0.0.1:5433/shared")
    infra._require_xdist_isolation()  # serial + shared is the supported CI mode
    assert database_mode() == "shared"


# --------------------------------------------------------------------------- #
# DEFECT C — the template cache identity is a CONCRETE revision, never "head"
# --------------------------------------------------------------------------- #
def test_head_resolves_to_the_concrete_repository_revision() -> None:
    resolved = _resolve_revision("head")
    assert resolved == "0009_l2f_evaluation_results"
    assert _resolve_revision("0009_l2f_evaluation_results") == resolved
    assert _resolve_revision(_L2F) == _L2F


def test_an_unknown_revision_fails_open() -> None:
    assert _resolve_revision("not_a_real_revision") is None


def test_a_future_head_gets_a_different_cache_identity() -> None:
    """A temporary additive migration moves head -> the resolved identity moves with it."""
    probe = Path("migrations/versions/9990_tmpl_future_probe.py")
    probe.write_text(
        '"""Temporary offline probe: metadata only, never committed, never applied."""\n\n'
        'revision: str = "9990_tmpl_future_probe"\n'
        'down_revision: str | None = "0009_l2f_evaluation_results"\n'
        "branch_labels = None\ndepends_on = None\n\n\n"
        "def upgrade() -> None:\n    pass\n\n\ndef downgrade() -> None:\n    pass\n",
        encoding="utf-8",
    )
    try:
        assert _resolve_revision("head") == "9990_tmpl_future_probe"
        assert _resolve_revision("0009_l2f_evaluation_results") == "0009_l2f_evaluation_results"
    finally:
        probe.unlink()
    assert _resolve_revision("head") == "0009_l2f_evaluation_results"


def test_multiple_heads_decline_the_template_fast_path() -> None:
    """An ambiguous 'head' resolves to None, so cloning declines and real Alembic runs."""
    probe = Path("migrations/versions/9991_tmpl_branch_probe.py")
    probe.write_text(
        '"""Temporary offline probe creating a SECOND head. Never committed, never applied."""\n\n'
        'revision: str = "9991_tmpl_branch_probe"\n'
        'down_revision: str | None = "0008_l2f_execution_results"\n'
        "branch_labels = None\ndepends_on = None\n\n\n"
        "def upgrade() -> None:\n    pass\n\n\ndef downgrade() -> None:\n    pass\n",
        encoding="utf-8",
    )
    try:
        assert _resolve_revision("head") is None  # ambiguous -> fail open to real Alembic
        # an explicit revision still resolves regardless of the branch
        assert _resolve_revision("0009_l2f_evaluation_results") == "0009_l2f_evaluation_results"
    finally:
        probe.unlink()


def test_head_and_explicit_revision_share_one_template(monkeypatch: pytest.MonkeyPatch) -> None:
    """The cache key equivalence, proven against a real cluster: one physical template serves
    both the symbolic and the explicit spelling of the same revision."""
    stashed = dict(_TEMPLATE_CACHE)
    _TEMPLATE_CACHE.clear()
    try:
        with _dedicated_cluster() as base:
            with scratch_database(base, "minos_tmpl_key_a") as url_a:
                alembic_upgrade(url_a, "head")
                assert len(_TEMPLATE_CACHE) == 1
                key_a = next(iter(_TEMPLATE_CACHE))
                assert key_a[1] == "0009_l2f_evaluation_results"

                with scratch_database(base, "minos_tmpl_key_b") as url_b:
                    alembic_upgrade(url_b, "0009_l2f_evaluation_results")
                    assert len(_TEMPLATE_CACHE) == 1, "explicit spelling must reuse the template"
            drop_session_templates()
    finally:
        _TEMPLATE_CACHE.clear()
        _TEMPLATE_CACHE.update(stashed)
