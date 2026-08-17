"""SQLAlchemy 2.x declarative base + deterministic naming convention (L2-B).

All constraint and index names are generated deterministically so qualification can
inspect them by name. SHA-256 columns and their CHECK constraints are built through
shared helpers so every hash column enforces the same lowercase-hex shape at the
database level (in addition to Python validation).
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import CHAR, CheckConstraint, DateTime, MetaData, func, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from .constants import HEX64_SQL_REGEX

__all__ = [
    "NAMING_CONVENTION",
    "metadata",
    "Base",
    "uuid_pk",
    "created_at_col",
    "sha256_col",
    "hex64_check",
]

NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

metadata = MetaData(naming_convention=NAMING_CONVENTION)


class Base(DeclarativeBase):
    metadata = metadata


def uuid_pk() -> Mapped[str]:
    """Server-generated UUID primary key (immutable identity)."""
    return mapped_column(
        UUID(as_uuid=False),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )


def created_at_col() -> Mapped[datetime]:
    """UTC-aware creation timestamp with a server default."""
    return mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


def sha256_col(*, nullable: bool = False, unique: bool = False) -> Mapped[str]:
    """A CHAR(64) column holding a lowercase-hex SHA-256 (shape enforced separately)."""
    return mapped_column(CHAR(64), nullable=nullable, unique=unique)


def hex64_check(column: str, name: str) -> CheckConstraint:
    """A CHECK constraint enforcing the canonical lowercase 64-hex SHA-256 shape."""
    return CheckConstraint(f"{column} ~ '{HEX64_SQL_REGEX}'", name=name)
