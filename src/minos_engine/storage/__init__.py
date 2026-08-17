"""Layer 2 storage foundation (L2-B) — PostgreSQL 16 + SQLAlchemy 2 + Alembic.

Importing this package opens **no** database connection. Engine/session creation is
explicit (see :mod:`minos_engine.storage.database`) and fails closed without
``MINOS_DATABASE_URL``. L2-B creates empty storage structures and their security
rules only — no scientific or practice-dataset records are populated.
"""

from __future__ import annotations

from .constants import ROLES, SCHEMAS

__all__ = ["SCHEMAS", "ROLES"]
