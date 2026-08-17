"""Deterministic storage fingerprints (L2-B) for the DB-READY gate.

Hashes the compiled PostgreSQL DDL of the ORM metadata (tables, columns,
constraints, indexes) and the role/privilege policy, so the gate binds the exact
storage schema and security matrix. Pure computation — opens no connection.
"""

from __future__ import annotations

from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateIndex, CreateTable

from minos_engine.common.hashing import canonical_hash

from . import models as _models  # noqa: F401 - populate Base.metadata
from .metadata import Base
from .roles import role_policy_hash
from .triggers import append_only_trigger_names, identity_trigger_names

__all__ = [
    "table_names",
    "constraint_names",
    "index_names",
    "storage_schema_hash",
    "storage_fingerprint",
]

_DIALECT = postgresql.dialect()  # type: ignore[no-untyped-call]


def table_names() -> tuple[str, ...]:
    return tuple(f"{t.schema}.{t.name}" for t in Base.metadata.sorted_tables)


def constraint_names() -> tuple[str, ...]:
    names: list[str] = []
    for t in Base.metadata.sorted_tables:
        for c in t.constraints:
            if c.name:
                names.append(str(c.name))
    return tuple(sorted(names))


def index_names() -> tuple[str, ...]:
    names: list[str] = []
    for t in Base.metadata.sorted_tables:
        for ix in t.indexes:
            if ix.name:
                names.append(str(ix.name))
    return tuple(sorted(names))


def storage_schema_hash() -> str:
    """SHA-256 over the compiled CREATE TABLE/INDEX DDL of the whole metadata."""
    stmts: list[str] = []
    for table in Base.metadata.sorted_tables:
        stmts.append(str(CreateTable(table).compile(dialect=_DIALECT)).strip())
        for ix in table.indexes:
            stmts.append(str(CreateIndex(ix).compile(dialect=_DIALECT)).strip())
    return canonical_hash(sorted(stmts))


def storage_fingerprint() -> dict[str, object]:
    return {
        "schema_hash": storage_schema_hash(),
        "role_policy_hash": role_policy_hash(),
        "tables": list(table_names()),
        "append_only_triggers": list(append_only_trigger_names()),
        "identity_triggers": list(identity_trigger_names()),
    }
