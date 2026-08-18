"""Code-owned role/privilege policy (L2-B) — canonical description + policy hash.

The authoritative DDL is the frozen migration ``0001_l2b_initial`` (which hardcodes
its own copy so it can never drift with later ORM changes). This module is the
human-readable policy of record and the source of ``role_policy_hash`` bound into the
DB-READY gate. Ownership model: the seven schemas and all stage tables/functions are
owned by ``minos_admin`` (a NOLOGIN, non-superuser group/ownership role); application
roles receive only least-privilege grants and never membership in ``minos_admin``.
"""

from __future__ import annotations

from minos_engine.common.hashing import canonical_hash

from .constants import APPEND_ONLY_TABLES, ROLES, SCHEMAS

__all__ = [
    "SCHEMA_OWNER",
    "SCHEMA_USAGE",
    "TABLE_GRANTS",
    "role_policy",
    "role_policy_hash",
]

#: Every application schema (and its tables/functions) is owned by this role.
SCHEMA_OWNER = "minos_admin"

# schema -> application roles granted USAGE. minos_admin is the OWNER and is not
# listed (its access is by ownership). A role without USAGE cannot reference the
# schema at all (this denies minos_live/trainer the evaluation schema).
SCHEMA_USAGE: dict[str, tuple[str, ...]] = {
    "catalog": ("minos_live", "minos_runner", "minos_trainer"),
    "profiling": ("minos_live", "minos_runner", "minos_trainer"),
    "experiments": ("minos_runner", "minos_evaluator", "minos_trainer"),
    "evaluation": ("minos_evaluator",),
    "models": ("minos_live", "minos_trainer"),
    "runtime": ("minos_live",),
    "audit": ("minos_live", "minos_runner", "minos_evaluator", "minos_trainer"),
}

# (schema, table, role, (privileges...)). minos_admin owns all tables (implicit ALL).
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


def role_policy() -> dict[str, object]:
    """Canonical, deterministic description of the role/privilege/ownership policy."""
    return {
        "roles": list(ROLES),
        "schemas": list(SCHEMAS),
        "schema_owner": SCHEMA_OWNER,
        "table_owner": SCHEMA_OWNER,
        "function_owner": SCHEMA_OWNER,
        "admin_is_nologin_non_superuser": True,
        "admin_receives_no_cluster_admin_attrs": True,
        "app_roles_never_member_of_admin": True,
        "schema_usage": {s: list(SCHEMA_USAGE[s]) for s in SCHEMAS},
        "table_grants": [
            {"schema": s, "table": t, "role": r, "privileges": list(p)}
            for (s, t, r, p) in TABLE_GRANTS
        ],
        "append_only_tables": [f"{s}.{t}" for (s, t) in APPEND_ONLY_TABLES],
        "public_schema_revoked": True,
        "public_table_revoked": True,
        "public_sequence_revoked": True,
        "public_function_execute_revoked": True,
        "safe_default_privileges": True,
    }


def role_policy_hash() -> str:
    return canonical_hash(role_policy())
