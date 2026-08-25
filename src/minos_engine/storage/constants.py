"""Code-owned storage constants (L2-B): schema names, role names, protected tables.

These are the single source of truth for the seven application schemas and five
least-privilege roles. The Alembic migration, the ORM models, the role/privilege
policy, and the qualification checks all consult these names — nothing is spelled
inline elsewhere.
"""

from __future__ import annotations

__all__ = [
    "SCHEMAS",
    "SCHEMA_SET",
    "ROLES",
    "ROLE_SET",
    "APPEND_ONLY_TABLES",
    "IMMUTABLE_IDENTITY_TABLES",
    "HEX64_SQL_REGEX",
    "ENV_DATABASE_URL",
    "CANONICAL_OPERATIONAL_DATABASE_NAME",
]

#: The seven application schemas (exact count; PostgreSQL system schemas excluded).
SCHEMAS: tuple[str, ...] = (
    "catalog",
    "profiling",
    "experiments",
    "evaluation",
    "models",
    "runtime",
    "audit",
)
SCHEMA_SET = frozenset(SCHEMAS)

#: The five least-privilege roles (NOLOGIN group roles; no committed passwords).
ROLES: tuple[str, ...] = (
    "minos_live",
    "minos_runner",
    "minos_evaluator",
    "minos_trainer",
    "minos_admin",
)
ROLE_SET = frozenset(ROLES)

#: Fully append-only evidence tables (no UPDATE/DELETE for any application role;
#: enforced additionally by an immutability trigger).
APPEND_ONLY_TABLES: tuple[tuple[str, str], ...] = (
    ("profiling", "profiles"),
    ("experiments", "results"),
    ("evaluation", "evaluations"),
    ("models", "model_bundles"),
    ("runtime", "decisions"),
    ("audit", "events"),
)

#: Tables whose *identity* columns are immutable but whose working state may change
#: (experiment jobs transition status / are claimed).
IMMUTABLE_IDENTITY_TABLES: tuple[tuple[str, str], ...] = (("experiments", "jobs"),)

#: PostgreSQL regex for a canonical lowercase SHA-256 (64 hex chars).
HEX64_SQL_REGEX = "^[0-9a-f]{64}$"

#: Environment variable that supplies the database URL (no default; fail closed).
ENV_DATABASE_URL = "MINOS_DATABASE_URL"

#: Canonical PostgreSQL database name for the persistent MINOS Engine operational
#: store (design decision). This is the DATABASE name inside a PostgreSQL cluster — it
#: is NOT a cluster/data-directory path, host, or role. The connection itself still
#: comes only from ``MINOS_DATABASE_URL`` (no default DSN, no hard-coded host); this
#: name is used solely by the fail-closed operational identity check to confirm that a
#: production mutation is running against the real operational store and not a scratch,
#: staging, or mistargeted database. Alembic manages the SCHEMAS/tables *inside* this
#: database; it never creates or renames the database itself.
CANONICAL_OPERATIONAL_DATABASE_NAME = "minos_engine_db"
