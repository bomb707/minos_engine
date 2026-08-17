"""L2-B initial storage foundation: roles, seven schemas, tables, triggers, grants.

Revision ID: 0001_l2b_initial
Revises:
Create Date: 2026-08-17

Deterministic single initial migration. Upgrade creates the five NOLOGIN roles, the
seven application schemas, immutability trigger functions, all tables/constraints/
indexes (dependency order via metadata), the append-only/identity triggers, revokes
unsafe PUBLIC privileges, grants least-privilege access, and sets safe default
privileges. Downgrade reverses in strict order and removes only stage-owned objects
(never a database-wide destructive operation).
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

from minos_engine.storage import models as _models  # noqa: F401 - populate metadata
from minos_engine.storage.metadata import Base
from minos_engine.storage.roles import (
    create_roles_sql,
    create_schemas_sql,
    default_privileges_sql,
    drop_roles_only_sql,
    drop_schemas_sql,
    grant_sql,
    reset_default_privileges_sql,
    revoke_all_from_roles_sql,
    revoke_public_sql,
)
from minos_engine.storage.triggers import (
    create_functions_sql,
    create_triggers_sql,
    drop_functions_sql,
    drop_triggers_sql,
)

revision: str = "0001_l2b_initial"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _run(statements: list[str]) -> None:
    for stmt in statements:
        op.execute(stmt)


def upgrade() -> None:
    bind = op.get_bind()
    _run(create_roles_sql())
    _run(create_schemas_sql())
    _run(create_functions_sql())
    Base.metadata.create_all(bind=bind)
    _run(create_triggers_sql())
    _run(revoke_public_sql())
    _run(grant_sql())
    _run(default_privileges_sql())


def downgrade() -> None:
    bind = op.get_bind()
    _run(drop_triggers_sql())
    _run(reset_default_privileges_sql())
    _run(revoke_all_from_roles_sql())
    Base.metadata.drop_all(bind=bind)
    _run(drop_functions_sql())
    _run(drop_schemas_sql())
    _run(drop_roles_only_sql())
