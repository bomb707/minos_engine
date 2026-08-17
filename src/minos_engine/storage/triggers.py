"""Append-only / immutable-identity trigger DDL (L2-B).

Database-level protection complements privilege revocation: even a role that somehow
held UPDATE/DELETE cannot mutate append-only evidence, and the identity columns of
experiment jobs cannot change while their working state may. ``DROP TABLE`` during a
migration downgrade does not fire row triggers, so the admin migration path remains
possible.
"""

from __future__ import annotations

from .constants import APPEND_ONLY_TABLES, IMMUTABLE_IDENTITY_TABLES

__all__ = [
    "REJECT_MUTATION_FUNCTION",
    "REJECT_IDENTITY_FUNCTION",
    "create_functions_sql",
    "drop_functions_sql",
    "create_triggers_sql",
    "drop_triggers_sql",
    "append_only_trigger_names",
    "identity_trigger_names",
]

REJECT_MUTATION_FUNCTION = "audit.minos_reject_mutation"
REJECT_IDENTITY_FUNCTION = "experiments.minos_reject_identity_change"

_JOB_IDENTITY_COLUMNS = ("id", "job_key", "profile_id", "config_id", "created_at")


def _append_only_trigger(schema: str, table: str) -> str:
    return f"trg_{schema}_{table}_append_only"


def _identity_trigger(schema: str, table: str) -> str:
    return f"trg_{schema}_{table}_identity_immutable"


def append_only_trigger_names() -> tuple[str, ...]:
    return tuple(_append_only_trigger(s, t) for (s, t) in APPEND_ONLY_TABLES)


def identity_trigger_names() -> tuple[str, ...]:
    return tuple(_identity_trigger(s, t) for (s, t) in IMMUTABLE_IDENTITY_TABLES)


def create_functions_sql() -> list[str]:
    reject = (
        f"CREATE OR REPLACE FUNCTION {REJECT_MUTATION_FUNCTION}() RETURNS trigger "
        "LANGUAGE plpgsql AS $$ BEGIN "
        "RAISE EXCEPTION 'append-only: % on %.% is not permitted', "
        "TG_OP, TG_TABLE_SCHEMA, TG_TABLE_NAME USING ERRCODE = 'restrict_violation'; "
        "END; $$;"
    )
    conds = " OR ".join(f"NEW.{c} IS DISTINCT FROM OLD.{c}" for c in _JOB_IDENTITY_COLUMNS)
    identity = (
        f"CREATE OR REPLACE FUNCTION {REJECT_IDENTITY_FUNCTION}() RETURNS trigger "
        "LANGUAGE plpgsql AS $$ BEGIN "
        f"IF {conds} THEN "
        "RAISE EXCEPTION 'immutable identity: identity columns of %.% cannot change', "
        "TG_TABLE_SCHEMA, TG_TABLE_NAME USING ERRCODE = 'restrict_violation'; "
        "END IF; RETURN NEW; END; $$;"
    )
    return [reject, identity]


def drop_functions_sql() -> list[str]:
    return [
        f"DROP FUNCTION IF EXISTS {REJECT_IDENTITY_FUNCTION}();",
        f"DROP FUNCTION IF EXISTS {REJECT_MUTATION_FUNCTION}();",
    ]


def create_triggers_sql() -> list[str]:
    out: list[str] = []
    for schema, table in APPEND_ONLY_TABLES:
        name = _append_only_trigger(schema, table)
        out.append(
            f"CREATE TRIGGER {name} BEFORE UPDATE OR DELETE ON {schema}.{table} "
            f"FOR EACH ROW EXECUTE FUNCTION {REJECT_MUTATION_FUNCTION}();"
        )
    for schema, table in IMMUTABLE_IDENTITY_TABLES:
        name = _identity_trigger(schema, table)
        out.append(
            f"CREATE TRIGGER {name} BEFORE UPDATE ON {schema}.{table} "
            f"FOR EACH ROW EXECUTE FUNCTION {REJECT_IDENTITY_FUNCTION}();"
        )
    return out


def drop_triggers_sql() -> list[str]:
    out: list[str] = []
    for schema, table in APPEND_ONLY_TABLES:
        out.append(
            f"DROP TRIGGER IF EXISTS {_append_only_trigger(schema, table)} ON {schema}.{table};"
        )
    for schema, table in IMMUTABLE_IDENTITY_TABLES:
        out.append(
            f"DROP TRIGGER IF EXISTS {_identity_trigger(schema, table)} ON {schema}.{table};"
        )
    return out
