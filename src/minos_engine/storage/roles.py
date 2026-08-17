"""Code-owned role/privilege policy (L2-B) — the single source of the grant matrix.

The Alembic migration emits exactly these statements; the qualification checks and
the role-denial tests assert against this same matrix. Roles are NOLOGIN group
roles with no committed passwords; integration tests use the admin/migration
connection and ``SET ROLE`` to prove privileges. No role is granted membership in
another role, so nothing is inherited (``minos_evaluator`` is never inherited by
``minos_live``).
"""

from __future__ import annotations

from minos_engine.common.hashing import canonical_hash

from .constants import APPEND_ONLY_TABLES, ROLES, SCHEMAS

__all__ = [
    "SCHEMA_USAGE",
    "TABLE_GRANTS",
    "create_roles_sql",
    "revoke_all_from_roles_sql",
    "drop_roles_only_sql",
    "create_schemas_sql",
    "drop_schemas_sql",
    "revoke_public_sql",
    "grant_sql",
    "default_privileges_sql",
    "reset_default_privileges_sql",
    "role_policy",
    "role_policy_hash",
]

# schema -> roles granted USAGE. A role without USAGE cannot reference the schema at
# all (this is how minos_live is denied evaluation, and trainer denied evaluation).
SCHEMA_USAGE: dict[str, tuple[str, ...]] = {
    "catalog": ("minos_live", "minos_runner", "minos_trainer", "minos_admin"),
    "profiling": ("minos_live", "minos_runner", "minos_trainer", "minos_admin"),
    "experiments": ("minos_runner", "minos_evaluator", "minos_trainer", "minos_admin"),
    "evaluation": ("minos_evaluator", "minos_admin"),
    "models": ("minos_live", "minos_trainer", "minos_admin"),
    "runtime": ("minos_live", "minos_admin"),
    "audit": ("minos_live", "minos_runner", "minos_evaluator", "minos_trainer", "minos_admin"),
}

# (schema, table, role, (privileges...)). minos_admin gets ALL separately.
TABLE_GRANTS: tuple[tuple[str, str, str, tuple[str, ...]], ...] = (
    ("catalog", "artifacts", "minos_live", ("SELECT",)),
    ("catalog", "artifacts", "minos_runner", ("SELECT",)),
    ("catalog", "artifacts", "minos_trainer", ("SELECT",)),
    ("catalog", "gatk_configs", "minos_live", ("SELECT",)),
    ("catalog", "gatk_configs", "minos_runner", ("SELECT",)),
    ("catalog", "gatk_configs", "minos_trainer", ("SELECT",)),
    ("catalog", "datasets", "minos_live", ("SELECT",)),
    ("catalog", "datasets", "minos_runner", ("SELECT",)),
    ("catalog", "datasets", "minos_trainer", ("SELECT",)),
    ("profiling", "profiles", "minos_live", ("SELECT",)),
    ("profiling", "profiles", "minos_runner", ("SELECT", "INSERT")),
    ("profiling", "profiles", "minos_trainer", ("SELECT",)),
    ("experiments", "jobs", "minos_runner", ("SELECT", "INSERT", "UPDATE")),
    ("experiments", "results", "minos_runner", ("SELECT", "INSERT")),
    ("experiments", "results", "minos_evaluator", ("SELECT",)),
    ("experiments", "results", "minos_trainer", ("SELECT",)),
    ("evaluation", "evaluations", "minos_evaluator", ("SELECT", "INSERT")),
    ("models", "model_bundles", "minos_live", ("SELECT",)),
    ("models", "model_bundles", "minos_trainer", ("SELECT", "INSERT")),
    ("runtime", "decisions", "minos_live", ("SELECT", "INSERT")),
    ("audit", "events", "minos_live", ("INSERT",)),
    ("audit", "events", "minos_runner", ("INSERT",)),
    ("audit", "events", "minos_evaluator", ("INSERT",)),
    ("audit", "events", "minos_trainer", ("INSERT",)),
)


def create_roles_sql() -> list[str]:
    """Idempotent, password-free NOLOGIN role creation."""
    out: list[str] = []
    for role in ROLES:
        out.append(
            "DO $$ BEGIN "
            f"IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{role}') THEN "
            f"CREATE ROLE {role} NOLOGIN; END IF; END $$;"
        )
    return out


def revoke_all_from_roles_sql() -> list[str]:
    """Revoke every grant from the five MINOS roles (schemas must still exist)."""
    out: list[str] = []
    for schema in SCHEMAS:
        for role in ROLES:
            out.append(f"REVOKE ALL ON ALL TABLES IN SCHEMA {schema} FROM {role};")
            out.append(f"REVOKE ALL ON ALL SEQUENCES IN SCHEMA {schema} FROM {role};")
            out.append(f"REVOKE ALL ON SCHEMA {schema} FROM {role};")
    return out


def drop_roles_only_sql() -> list[str]:
    """Drop exactly the five MINOS roles, safely and non-destructively.

    Roles are dropped only after their grants and default privileges are removed in
    this database. Because PostgreSQL roles are cluster-global, a role may still be
    referenced by grants in *another* database (e.g. a second application database on
    the same cluster). In that case ``DROP ROLE`` raises
    ``dependent_objects_still_exist``; we catch exactly that condition and retain the
    role with a NOTICE, rather than resorting to any destructive database-wide cleanup
    (never ``DROP OWNED BY`` / ``DROP DATABASE``). Nothing outside the five MINOS roles
    is ever touched.
    """
    out: list[str] = []
    for role in ROLES:
        out.append(
            "DO $$ BEGIN "
            f"DROP ROLE IF EXISTS {role}; "
            "EXCEPTION WHEN dependent_objects_still_exist THEN "
            f"RAISE NOTICE 'role {role} retained: still referenced in another database'; "
            "END $$;"
        )
    return out


def create_schemas_sql() -> list[str]:
    return [f"CREATE SCHEMA IF NOT EXISTS {s};" for s in SCHEMAS]


def drop_schemas_sql() -> list[str]:
    # Schemas are emptied by drop_all + function drops first; RESTRICT refuses to
    # touch anything unexpected still present (never a broad CASCADE).
    return [f"DROP SCHEMA IF EXISTS {s} RESTRICT;" for s in SCHEMAS]


def revoke_public_sql() -> list[str]:
    out: list[str] = []
    for s in SCHEMAS:
        out.append(f"REVOKE ALL ON SCHEMA {s} FROM PUBLIC;")
        out.append(f"REVOKE ALL ON ALL TABLES IN SCHEMA {s} FROM PUBLIC;")
        out.append(f"REVOKE ALL ON ALL SEQUENCES IN SCHEMA {s} FROM PUBLIC;")
    return out


def grant_sql() -> list[str]:
    out: list[str] = []
    for s in SCHEMAS:
        for role in SCHEMA_USAGE[s]:
            out.append(f"GRANT USAGE ON SCHEMA {s} TO {role};")
        # Admin owns/administers everything in the schema.
        out.append(f"GRANT ALL ON ALL TABLES IN SCHEMA {s} TO minos_admin;")
    for schema, table, role, privs in TABLE_GRANTS:
        out.append(f"GRANT {', '.join(privs)} ON {schema}.{table} TO {role};")
    return out


def default_privileges_sql() -> list[str]:
    out: list[str] = []
    for s in SCHEMAS:
        out.append(f"ALTER DEFAULT PRIVILEGES IN SCHEMA {s} REVOKE ALL ON TABLES FROM PUBLIC;")
        out.append(f"ALTER DEFAULT PRIVILEGES IN SCHEMA {s} GRANT ALL ON TABLES TO minos_admin;")
    return out


def reset_default_privileges_sql() -> list[str]:
    """Undo the default-privilege entries so the roles can be dropped cleanly."""
    return [
        f"ALTER DEFAULT PRIVILEGES IN SCHEMA {s} REVOKE ALL ON TABLES FROM minos_admin;"
        for s in SCHEMAS
    ]


def role_policy() -> dict[str, object]:
    """Canonical, deterministic description of the role/privilege policy."""
    return {
        "roles": list(ROLES),
        "schemas": list(SCHEMAS),
        "schema_usage": {s: list(SCHEMA_USAGE[s]) for s in SCHEMAS},
        "table_grants": [
            {"schema": s, "table": t, "role": r, "privileges": list(p)}
            for (s, t, r, p) in TABLE_GRANTS
        ],
        "append_only_tables": [f"{s}.{t}" for (s, t) in APPEND_ONLY_TABLES],
        "public_revoked": True,
        "admin_all_tables": True,
    }


def role_policy_hash() -> str:
    return canonical_hash(role_policy())
