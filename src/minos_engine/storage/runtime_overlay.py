"""Apply / inspect the operational RUNTIME OVERLAY lineage.

One code path for operators and tests, so the overlay is never advanced by an ad-hoc
``alembic -c`` invocation that forgot the version table. The equivalent CLI is::

    alembic -c alembic_runtime.ini upgrade head

Neither route touches ``public.alembic_version``: the main chain stays exactly where the
deployment put it, and the overlay refuses to apply unless it is at ``0005_l2e_feature_view``.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import sqlalchemy as sa

from minos_engine.common.errors import MinosEngineError
from minos_engine.storage.constants import ENV_DATABASE_URL
from minos_engine.storage.database import create_db_engine, normalize_database_url
from minos_engine.storage.runtime_decision_contract import (
    REQUIRED_MAIN_REVISION,
    RUNTIME_OVERLAY_HEAD_REVISION,
    RUNTIME_OVERLAY_REVISION,
    RUNTIME_OVERLAY_SCRIPT_LOCATION,
    RUNTIME_OVERLAY_VERSION_TABLE,
    RUNTIME_OVERLAY_VERSION_TABLE_SCHEMA,
    runtime_overlay_revision,
)

__all__ = [
    "RuntimeOverlayError",
    "database_url_for",
    "downgrade_runtime_overlay",
    "main_lineage_revision",
    "observed_overlay_state",
    "upgrade_runtime_overlay",
]


class RuntimeOverlayError(MinosEngineError):
    """The runtime overlay could not be applied, inspected or reverted."""


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def _config(root: Any = None) -> Any:
    from alembic.config import Config

    base = Path(root) if root is not None else _repo_root()
    location = base / RUNTIME_OVERLAY_SCRIPT_LOCATION
    if not location.is_dir():
        raise RuntimeOverlayError(f"the runtime overlay script location is missing: {location}")
    config = Config()
    config.set_main_option("script_location", str(location))
    return config


@contextmanager
def database_url_for(url: str) -> Iterator[None]:
    """Point ``MINOS_DATABASE_URL`` at ``url`` for one Alembic run, then put it back.

    Alembic's environment reads the URL from the process environment and nowhere else, so a
    programmatic run has to set it. Doing that in one place means no caller can forget to
    restore it and leave a later run pointed at the wrong database.
    """
    previous = os.environ.get(ENV_DATABASE_URL)
    os.environ[ENV_DATABASE_URL] = normalize_database_url(url)
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop(ENV_DATABASE_URL, None)
        else:
            os.environ[ENV_DATABASE_URL] = previous


def _run(url: str, revision: str, *, direction: str, root: Any = None) -> None:
    from alembic import command

    config = _config(root)
    with database_url_for(url):
        getattr(command, direction)(config, revision)


def upgrade_runtime_overlay(url: str, revision: str = "head", *, root: Any = None) -> None:
    """Advance the overlay lineage. Refuses unless the main chain is at the accepted revision."""
    _run(url, revision, direction="upgrade", root=root)


def downgrade_runtime_overlay(url: str, revision: str = "base", *, root: Any = None) -> None:
    """Revert the overlay lineage exactly, leaving the main chain untouched."""
    _run(url, revision, direction="downgrade", root=root)


def main_lineage_revision(conn: Any) -> str | None:
    """The MAIN chain's revision, read from ``public.alembic_version``."""
    if conn.execute(sa.text("SELECT to_regclass('public.alembic_version')")).scalar() is None:
        return None
    value = conn.execute(sa.text("SELECT version_num FROM public.alembic_version")).scalar()
    return None if value is None else str(value)


def observed_overlay_state(url: str) -> dict[str, Any]:
    """Both lineages and the decision row count, as the server actually reports them."""
    engine = create_db_engine(url)
    try:
        with engine.connect() as conn:
            main = main_lineage_revision(conn)
            overlay = runtime_overlay_revision(conn)
            rows = (
                int(conn.execute(sa.text("SELECT count(*) FROM runtime.decisions")).scalar() or 0)
                if conn.execute(sa.text("SELECT to_regclass('runtime.decisions')")).scalar()
                else None
            )
        return {
            "main_revision": main,
            "main_revision_required_by_overlay": REQUIRED_MAIN_REVISION,
            "overlay_revision": overlay,
            "overlay_base_revision": RUNTIME_OVERLAY_REVISION,
            "overlay_head_revision": RUNTIME_OVERLAY_HEAD_REVISION,
            "overlay_version_table": (
                f"{RUNTIME_OVERLAY_VERSION_TABLE_SCHEMA}.{RUNTIME_OVERLAY_VERSION_TABLE}"
            ),
            "decision_row_count": rows,
        }
    finally:
        engine.dispose()
