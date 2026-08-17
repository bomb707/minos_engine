"""Alembic environment (L2-B) — synchronous, credential-free, env-driven.

The database URL comes only from ``MINOS_DATABASE_URL`` (via
:func:`minos_engine.storage.database.create_db_engine`); no URL is stored in
``alembic.ini``. Importing this module does not connect — a connection is opened only
when migrations run.
"""

from __future__ import annotations

from alembic import context

from minos_engine.storage import models as _models  # noqa: F401 - populate metadata
from minos_engine.storage.database import create_db_engine, database_url
from minos_engine.storage.metadata import Base

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(
        url=database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        include_schemas=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    engine = create_db_engine()
    try:
        with engine.connect() as connection:
            context.configure(
                connection=connection,
                target_metadata=target_metadata,
                include_schemas=True,
                transaction_per_migration=True,
            )
            with context.begin_transaction():
                context.run_migrations()
    finally:
        engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
