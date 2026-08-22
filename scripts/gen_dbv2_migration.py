"""Deterministically generate migration 0009 from the frozen DB-V2 contracts.

The three contract documents under ``reports/database`` are the source of truth for the DB-V2
schema; this generator turns them into the complete executable DDL of
``migrations/versions/0009_dbv2_shadow_schema.py``. The migration itself never reads a report and
never invokes this generator: the committed file contains the entire result as literal SQL.

``generate``
    Write the migration file.

``verify``
    Re-generate in memory and compare byte for byte with the committed file. Never writes.

Generation is a pure function of the committed contracts: the same inputs always produce the same
bytes, so two independent runs are byte-identical and any drift between the contracts and the
committed migration is detectable.
"""

from __future__ import annotations

import argparse
import difflib
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
REPORTS = REPO_ROOT / "reports" / "database"
MIGRATION_PATH = REPO_ROOT / "migrations" / "versions" / "0009_dbv2_shadow_schema.py"
CONTRACT_PATH = REPO_ROOT / "src" / "minos_engine" / "storage" / "dbv2_migration_contract.py"
INVENTORY_PATH = REPO_ROOT / "reports" / "database" / "MINOS_DATABASE_V1_INVENTORY.json"

REVISION = "0009_dbv2_shadow_schema"
DOWN_REVISION = "0008_l2f_execution_results"

#: every DB-V2 function pins this; an unpinned SECURITY DEFINER function is a privilege bug.
SEARCH_PATH = "pg_catalog"
DEFINER = "minos_owner"
SHARED_ALEMBIC_TABLE = "public.alembic_version"


def _no_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    seen: dict[str, Any] = {}
    for key, value in pairs:
        if key in seen:
            raise ValueError(f"duplicate JSON key {key!r}")
        seen[key] = value
    return seen


def load_strict(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_no_duplicate_keys)


class Contracts:
    """The three frozen documents, plus the derived lookups the emitter needs."""

    def __init__(self, reports: Path) -> None:
        self.logical = load_strict(reports / "MINOS_DATABASE_V2_CONTRACT.json")
        self.physical = load_strict(reports / "MINOS_DATABASE_V2_PHYSICAL_DEPLOYMENT.json")
        self.api = load_strict(reports / "MINOS_DATABASE_V2_DATABASE_API.json")
        self.shadow: dict[str, str] = self.physical["schema_mapping"]["canonical_to_shadow"]
        self.tables: dict[str, dict[str, Any]] = {}
        for schema in self.logical["schemas"]:
            for table in schema["tables"]:
                self.tables[f"{schema['schema']}.{table['table']}"] = table

    # -- name translation ------------------------------------------------------------------
    def physical_schema(self, canonical: str) -> str:
        return self.shadow[canonical]

    def physical_table(self, ident: str) -> str:
        schema, bare = ident.split(".", 1)
        return f"{self.shadow[schema]}.{bare}"

    def shadow_tables(self) -> list[str]:
        """The 37 shadow tables in a deterministic FK-safe creation order.

        Alphabetical order is not creatable: ``catalog.artifact_locations`` references
        ``catalog.artifacts``. The sort is a topological walk that always takes the
        alphabetically first table whose foreign-key targets already exist, so the order is a
        pure function of the contract and identical on every run.
        """
        pending = sorted(ident for ident in self.tables if ident != SHARED_ALEMBIC_TABLE)
        created: list[str] = []
        done: set[str] = set()
        while pending:
            for ident in pending:
                targets = {fk["references"] for fk in self.tables[ident].get("foreign_keys", [])}
                if targets - done - {ident} <= {SHARED_ALEMBIC_TABLE}:
                    created.append(ident)
                    done.add(ident)
                    pending.remove(ident)
                    break
            else:  # pragma: no cover - a genuine cycle would be a contract defect
                raise ValueError(f"foreign-key cycle among {pending}")
        return created

    def functions(self) -> list[dict[str, Any]]:
        return sorted(self.api["functions"], key=lambda f: f["name"])

    def triggers(self) -> list[dict[str, Any]]:
        return sorted(self.api["triggers"], key=lambda t: (t["table"], t["name"]))


# --------------------------------------------------------------------------- #
# table DDL
# --------------------------------------------------------------------------- #
def _column_sql(column: dict[str, Any]) -> str:
    parts = [column["name"], column["type"]]
    if "default" in column:
        parts.append(f"DEFAULT {column['default']}")
    parts.append("NULL" if column["nullable"] else "NOT NULL")
    return " ".join(parts)


def table_ddl(contracts: Contracts, ident: str) -> list[str]:
    """CREATE TABLE plus every declared constraint and index, fully shadow-qualified."""
    table = contracts.tables[ident]
    physical = contracts.physical_table(ident)
    body: list[str] = [f"    {_column_sql(column)}" for column in table["columns"]]

    primary_key = table["primary_key"]
    body.append(
        f"    CONSTRAINT {primary_key['name']} PRIMARY KEY ({', '.join(primary_key['columns'])})"
    )
    for unique in table.get("unique_constraints", []):
        body.append(f"    CONSTRAINT {unique['name']} UNIQUE ({', '.join(unique['columns'])})")
    for check in table.get("check_constraints", []):
        body.append(f"    CONSTRAINT {check['name']} CHECK ({check['expression']})")
    for foreign_key in table.get("foreign_keys", []):
        target = contracts.physical_table(foreign_key["references"])
        body.append(
            f"    CONSTRAINT {foreign_key['name']} FOREIGN KEY "
            f"({', '.join(foreign_key['columns'])}) REFERENCES {target} "
            f"({', '.join(foreign_key['referenced_columns'])})"
        )
    statements = ["CREATE TABLE {} (\n{}\n);".format(physical, ",\n".join(body))]

    for index in table.get("indexes", []):
        method = index.get("method", "btree")
        unique = "UNIQUE " if index["name"].startswith("uq_") else ""
        where = f" WHERE {index['where']}" if index.get("where") else ""
        statements.append(
            f"CREATE {unique}INDEX {index['name']} ON {physical} "
            f"USING {method} ({', '.join(index['columns'])}){where};"
        )
    return statements


# --------------------------------------------------------------------------- #
# function bodies
# --------------------------------------------------------------------------- #
NULL_STATE = "(null)"


def _lit(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _transition_guard(machine: dict[str, Any], column: str, *, label: str) -> str:
    """The exact allowed-transition test, generated from the frozen transition list."""
    transitions = machine["transitions"]
    old = f"coalesce(OLD.{column}::text, {_lit(NULL_STATE)})"
    new = f"coalesce(NEW.{column}::text, {_lit(NULL_STATE)})"
    if not transitions:
        return (
            f"    IF NEW.{column} IS DISTINCT FROM OLD.{column} THEN\n"
            f"        RAISE EXCEPTION '{label} is immutable: % -> %', OLD.{column}, NEW.{column}\n"
            "            USING ERRCODE = 'check_violation';\n"
            "    END IF;\n"
        )
    pairs = ", ".join(f"({_lit(source)}, {_lit(target)})" for source, target in transitions)
    return (
        f"    IF NEW.{column} IS DISTINCT FROM OLD.{column}\n"
        f"       AND ({old}, {new}) NOT IN ({pairs}) THEN\n"
        f"        RAISE EXCEPTION 'forbidden {label} transition % -> %',\n"
        f"            coalesce(OLD.{column}::text, {_lit(NULL_STATE)}),\n"
        f"            coalesce(NEW.{column}::text, {_lit(NULL_STATE)})\n"
        "            USING ERRCODE = 'check_violation';\n"
        "    END IF;\n"
    )


def _generic_bodies() -> dict[str, tuple[str, str]]:
    """The three reusable guards. Declarations are (declare_block, body)."""
    return {
        "audit.reject_immutable_column_update": (
            "    col text;\n    before_row jsonb := to_jsonb(OLD);\n"
            "    after_row jsonb := to_jsonb(NEW);\n",
            "    FOREACH col IN ARRAY TG_ARGV LOOP\n"
            "        IF before_row -> col IS DISTINCT FROM after_row -> col THEN\n"
            "            RAISE EXCEPTION 'immutable column %.%.% may not change',\n"
            "                TG_TABLE_SCHEMA, TG_TABLE_NAME, col\n"
            "                USING ERRCODE = 'check_violation';\n"
            "        END IF;\n"
            "    END LOOP;\n"
            "    RETURN NEW;\n",
        ),
        "audit.reject_delete": (
            "",
            "    RAISE EXCEPTION 'DELETE on %.% is not permitted', TG_TABLE_SCHEMA, TG_TABLE_NAME\n"
            "        USING ERRCODE = 'check_violation';\n"
            "    RETURN NULL;\n",
        ),
        "audit.reject_update": (
            "",
            "    RAISE EXCEPTION 'UPDATE on %.% is not permitted: every column is immutable',\n"
            "        TG_TABLE_SCHEMA, TG_TABLE_NAME\n"
            "        USING ERRCODE = 'check_violation';\n"
            "    RETURN NULL;\n",
        ),
    }


def _state_machine_bodies(contracts: Contracts) -> dict[str, tuple[str, str]]:
    """One body per declared state machine, with its transition test generated from the contract."""
    machines = contracts.api["state_machines"]
    shadow = contracts.shadow
    out: dict[str, tuple[str, str]] = {}

    out["catalog.enforce_artifact_lifecycle"] = (
        "",
        _transition_guard(machines["artifact_lifecycle"], "lifecycle_state", label="lifecycle")
        + _transition_guard(
            machines["artifact_verification"], "verification_state", label="verification"
        )
        + "    IF OLD.first_verified_at IS NOT NULL\n"
        "       AND NEW.first_verified_at IS DISTINCT FROM OLD.first_verified_at THEN\n"
        "        RAISE EXCEPTION 'first_verified_at is written exactly once'\n"
        "            USING ERRCODE = 'check_violation';\n"
        "    END IF;\n"
        "    IF NEW.verification_state = 'verified' AND OLD.verification_state <> 'verified'\n"
        "       AND NEW.first_verified_at IS NULL THEN\n"
        "        RAISE EXCEPTION 'the first transition to verified must set first_verified_at'\n"
        "            USING ERRCODE = 'check_violation';\n"
        "    END IF;\n"
        "    IF OLD.last_verified_at IS NOT NULL AND NEW.last_verified_at IS NOT NULL\n"
        "       AND NEW.last_verified_at < OLD.last_verified_at THEN\n"
        "        RAISE EXCEPTION 'last_verified_at may not move backwards'\n"
        "            USING ERRCODE = 'check_violation';\n"
        "    END IF;\n"
        "    RETURN NEW;\n",
    )

    out["catalog.enforce_artifact_location_state"] = (
        "    other_primary uuid;\n",
        "    IF TG_OP = 'UPDATE' THEN\n"
        + _transition_guard(machines["artifact_location"], "location_state", label="location")
        + "    END IF;\n"
        "    IF NEW.is_primary AND NEW.location_state <> 'present' THEN\n"
        "        RAISE EXCEPTION 'only a present location may be primary (state %)',\n"
        "            NEW.location_state USING ERRCODE = 'check_violation';\n"
        "    END IF;\n"
        "    IF NEW.is_primary THEN\n"
        f"        PERFORM 1 FROM {shadow['catalog']}.artifacts\n"
        "            WHERE id = NEW.artifact_id FOR UPDATE;\n"
        f"        SELECT id INTO other_primary FROM {shadow['catalog']}.artifact_locations\n"
        "            WHERE artifact_id = NEW.artifact_id AND is_primary AND id <> NEW.id\n"
        "            LIMIT 1;\n"
        "        IF other_primary IS NOT NULL THEN\n"
        "            RAISE EXCEPTION 'artifact % already has a primary location', NEW.artifact_id\n"
        "                USING ERRCODE = 'unique_violation';\n"
        "        END IF;\n"
        "    END IF;\n"
        "    RETURN NEW;\n",
    )

    out["catalog.enforce_release_state"] = (
        "    other_active uuid;\n",
        _transition_guard(machines["release_state"], "state", label="release")
        + "    IF NEW.state = 'active' AND OLD.state <> 'active' THEN\n"
        f"        SELECT id INTO other_active FROM {shadow['catalog']}.releases\n"
        "            WHERE state = 'active' AND id <> NEW.id FOR UPDATE;\n"
        "        IF other_active IS NOT NULL THEN\n"
        "            RAISE EXCEPTION 'release % is already active', other_active\n"
        "                USING ERRCODE = 'unique_violation';\n"
        "        END IF;\n"
        "    END IF;\n"
        "    RETURN NEW;\n",
    )

    out["profiling.enforce_profile_snapshot_state"] = (
        "",
        _transition_guard(machines["profile_snapshot_state"], "state", label="snapshot")
        + "    RETURN NEW;\n",
    )

    out["experiments.enforce_job_state"] = (
        "    attempt_job uuid;\n",
        _transition_guard(machines["job_state"], "status", label="job")
        + "    IF NEW.attempt_count < OLD.attempt_count THEN\n"
        "        RAISE EXCEPTION 'attempt_count may not decrease (% -> %)',\n"
        "            OLD.attempt_count, NEW.attempt_count USING ERRCODE = 'check_violation';\n"
        "    END IF;\n"
        "    IF NEW.status = 'PENDING' AND OLD.status IN ('CLAIMED', 'RUNNING')\n"
        "       AND (OLD.lease_expires_at IS NULL OR OLD.lease_expires_at >= now()) THEN\n"
        "        RAISE EXCEPTION 'a held lease may only be released after it expires'\n"
        "            USING ERRCODE = 'check_violation';\n"
        "    END IF;\n"
        "    IF OLD.terminal_attempt_id IS NOT NULL\n"
        "       AND NEW.terminal_attempt_id IS DISTINCT FROM OLD.terminal_attempt_id THEN\n"
        "        RAISE EXCEPTION 'terminal_attempt_id is written exactly once'\n"
        "            USING ERRCODE = 'check_violation';\n"
        "    END IF;\n"
        "    IF NEW.terminal_attempt_id IS NOT NULL\n"
        "       AND NEW.terminal_attempt_id IS DISTINCT FROM OLD.terminal_attempt_id THEN\n"
        f"        SELECT job_id INTO attempt_job FROM {shadow['experiments']}.execution_attempts\n"
        "            WHERE id = NEW.terminal_attempt_id;\n"
        "        IF attempt_job IS DISTINCT FROM NEW.id THEN\n"
        "            RAISE EXCEPTION 'terminal_attempt_id % belongs to a different job',\n"
        "                NEW.terminal_attempt_id USING ERRCODE = 'foreign_key_violation';\n"
        "        END IF;\n"
        "    END IF;\n"
        "    RETURN NEW;\n",
    )

    out["experiments.enforce_attempt_outcome"] = (
        "",
        _transition_guard(machines["attempt_outcome"], "outcome", label="attempt outcome")
        + "    IF NEW.outcome IS NOT NULL AND OLD.outcome IS NULL THEN\n"
        "        IF NEW.finished_at IS NULL OR NEW.runtime_ms IS NULL\n"
        "           OR NEW.gatk_executable_sha256 IS NULL OR NEW.gatk_version IS NULL THEN\n"
        "            RAISE EXCEPTION 'the terminal outcome must set every terminal field'\n"
        "                USING ERRCODE = 'check_violation';\n"
        "        END IF;\n"
        "        IF NEW.finished_at < NEW.started_at THEN\n"
        "            RAISE EXCEPTION 'finished_at may not precede started_at'\n"
        "                USING ERRCODE = 'check_violation';\n"
        "        END IF;\n"
        "    END IF;\n"
        "    RETURN NEW;\n",
    )

    out["experiments.enforce_attempt_exclusivity"] = (
        "    conflicting uuid;\n",
        f"    PERFORM 1 FROM {shadow['experiments']}.execution_attempts\n"
        "        WHERE id = NEW.attempt_id FOR UPDATE;\n"
        "    IF TG_TABLE_NAME = 'execution_results' THEN\n"
        f"        SELECT id INTO conflicting FROM {shadow['experiments']}.execution_failures\n"
        "            WHERE attempt_id = NEW.attempt_id LIMIT 1;\n"
        "    ELSE\n"
        f"        SELECT id INTO conflicting FROM {shadow['experiments']}.execution_results\n"
        "            WHERE attempt_id = NEW.attempt_id LIMIT 1;\n"
        "    END IF;\n"
        "    IF conflicting IS NOT NULL THEN\n"
        "        RAISE EXCEPTION 'attempt % already has an outcome row of the other kind',\n"
        "            NEW.attempt_id USING ERRCODE = 'unique_violation';\n"
        "    END IF;\n"
        "    RETURN NEW;\n",
    )

    out["evaluation.enforce_evaluation_run_state"] = (
        "",
        _transition_guard(machines["evaluation_run_state"], "state", label="evaluation")
        + "    IF OLD.completed_at IS NOT NULL\n"
        "       AND NEW.completed_at IS DISTINCT FROM OLD.completed_at THEN\n"
        "        RAISE EXCEPTION 'completed_at is written exactly once'\n"
        "            USING ERRCODE = 'check_violation';\n"
        "    END IF;\n"
        "    RETURN NEW;\n",
    )

    out["models.enforce_training_run_state"] = (
        "",
        _transition_guard(machines["training_run_state"], "state", label="training")
        + "    IF OLD.completed_at IS NOT NULL\n"
        "       AND NEW.completed_at IS DISTINCT FROM OLD.completed_at THEN\n"
        "        RAISE EXCEPTION 'completed_at is written exactly once'\n"
        "            USING ERRCODE = 'check_violation';\n"
        "    END IF;\n"
        "    RETURN NEW;\n",
    )

    out["models.enforce_model_activation"] = (
        "    other_open uuid;\n",
        "    IF TG_OP = 'UPDATE' THEN\n"
        "        IF OLD.deactivated_at IS NOT NULL\n"
        "           AND NEW.deactivated_at IS DISTINCT FROM OLD.deactivated_at THEN\n"
        "            RAISE EXCEPTION 'deactivated_at is written exactly once and never cleared'\n"
        "                USING ERRCODE = 'check_violation';\n"
        "        END IF;\n"
        "    END IF;\n"
        "    IF NEW.deactivated_at IS NULL THEN\n"
        f"        SELECT id INTO other_open FROM {shadow['models']}.model_activations\n"
        "            WHERE deactivated_at IS NULL AND id <> NEW.id FOR UPDATE;\n"
        "        IF other_open IS NOT NULL THEN\n"
        "            RAISE EXCEPTION 'activation % is still open', other_open\n"
        "                USING ERRCODE = 'unique_violation';\n"
        "        END IF;\n"
        "    END IF;\n"
        "    RETURN NEW;\n",
    )

    out["runtime.enforce_service_instance_state"] = (
        "",
        _transition_guard(machines["service_instance_state"], "state", label="instance")
        + "    IF NEW.last_heartbeat_at IS NOT NULL AND OLD.last_heartbeat_at IS NOT NULL\n"
        "       AND NEW.last_heartbeat_at < OLD.last_heartbeat_at THEN\n"
        "        RAISE EXCEPTION 'last_heartbeat_at may not move backwards'\n"
        "            USING ERRCODE = 'check_violation';\n"
        "    END IF;\n"
        "    IF NEW.release_id IS DISTINCT FROM OLD.release_id AND OLD.state <> 'starting' THEN\n"
        "        RAISE EXCEPTION 'release_id may only change while the instance is starting'\n"
        "            USING ERRCODE = 'check_violation';\n"
        "    END IF;\n"
        "    RETURN NEW;\n",
    )

    out["runtime.enforce_lease_transition"] = (
        "",
        "    IF NEW.fence_token <= OLD.fence_token THEN\n"
        "        RAISE EXCEPTION 'fence_token must strictly increase (% -> %)',\n"
        "            OLD.fence_token, NEW.fence_token USING ERRCODE = 'check_violation';\n"
        "    END IF;\n"
        "    IF NEW.holder IS DISTINCT FROM OLD.holder THEN\n"
        "        IF OLD.expires_at > now() THEN\n"
        "            RAISE EXCEPTION 'lease % is held by % until %',\n"
        "                OLD.lease_key, OLD.holder, OLD.expires_at\n"
        "                USING ERRCODE = 'check_violation';\n"
        "        END IF;\n"
        "    ELSE\n"
        "        IF NEW.acquired_at IS DISTINCT FROM OLD.acquired_at THEN\n"
        "            RAISE EXCEPTION 'acquired_at changes only when the holder changes'\n"
        "                USING ERRCODE = 'check_violation';\n"
        "        END IF;\n"
        "        IF NEW.expires_at < OLD.expires_at THEN\n"
        "            RAISE EXCEPTION 'expires_at may not move backwards for the same holder'\n"
        "                USING ERRCODE = 'check_violation';\n"
        "        END IF;\n"
        "    END IF;\n"
        "    RETURN NEW;\n",
    )

    out["runtime.enforce_active_selection_window"] = (
        "    other_open uuid;\n",
        "    IF TG_OP = 'UPDATE' THEN\n"
        "        IF OLD.effective_to IS NOT NULL\n"
        "           AND NEW.effective_to IS DISTINCT FROM OLD.effective_to THEN\n"
        "            RAISE EXCEPTION 'effective_to is written exactly once and never cleared'\n"
        "                USING ERRCODE = 'check_violation';\n"
        "        END IF;\n"
        "    END IF;\n"
        "    IF NEW.effective_to IS NOT NULL AND NEW.effective_to < NEW.effective_from THEN\n"
        "        RAISE EXCEPTION 'effective_to may not precede effective_from'\n"
        "            USING ERRCODE = 'check_violation';\n"
        "    END IF;\n"
        "    IF NEW.effective_to IS NULL THEN\n"
        f"        SELECT id INTO other_open FROM {shadow['runtime']}.active_selections\n"
        "            WHERE effective_to IS NULL AND id <> NEW.id FOR UPDATE;\n"
        "        IF other_open IS NOT NULL THEN\n"
        "            RAISE EXCEPTION 'selection window % is still open', other_open\n"
        "                USING ERRCODE = 'unique_violation';\n"
        "        END IF;\n"
        "    END IF;\n"
        "    RETURN NEW;\n",
    )

    out["catalog.enforce_backup_set_immutability"] = (
        "    col text;\n    before_row jsonb := to_jsonb(OLD);\n"
        "    after_row jsonb := to_jsonb(NEW);\n",
        "    FOREACH col IN ARRAY ARRAY(SELECT jsonb_object_keys(before_row) ORDER BY 1) LOOP\n"
        "        IF col <> 'restore_tested_at'\n"
        "           AND before_row -> col IS DISTINCT FROM after_row -> col THEN\n"
        "            RAISE EXCEPTION 'backup_sets.% is immutable', col\n"
        "                USING ERRCODE = 'check_violation';\n"
        "        END IF;\n"
        "    END LOOP;\n"
        "    IF OLD.restore_tested_at IS NOT NULL AND NEW.restore_tested_at IS NOT NULL\n"
        "       AND NEW.restore_tested_at < OLD.restore_tested_at THEN\n"
        "        RAISE EXCEPTION 'restore_tested_at may not move backwards'\n"
        "            USING ERRCODE = 'check_violation';\n"
        "    END IF;\n"
        "    RETURN NEW;\n",
    )
    return out


def _r1_equality_checks(contracts: Contracts) -> str:
    """Generated from the frozen r1_field_to_column mapping: every field compared in its own type."""
    table = contracts.tables["catalog.backup_sets"]
    types = {column["name"]: column["type"] for column in table["columns"]}
    lines: list[str] = []
    for field, column in sorted(table["r1_field_to_column"].items()):
        column_type = types[column]
        cast = {
            "uuid": "::uuid",
            "bigint": "::bigint",
            "timestamptz": "::timestamptz",
            "integer": "::integer",
        }.get(column_type, "")
        expression = f"(r1 ->> {_lit(field)}){cast}"
        lines.append(
            f"    IF {expression} IS DISTINCT FROM NEW.{column} THEN\n"
            f"        RAISE EXCEPTION 'R1 field % does not equal its mapped column %',\n"
            f"            {_lit(field)}, {_lit(column)} USING ERRCODE = 'check_violation';\n"
            "    END IF;\n"
        )
    return "".join(lines)


def _backup_set_gate_body(contracts: Contracts) -> tuple[str, str]:
    """The cross-table completeness gate.

    A complete snapshot must be the EXACT active operational artifact set: same members, same
    multiplicity, same order, same totals, in both directions. The two manifests are stored inline
    and are proved by their own bytes; only the external database dump needs a present location.
    """
    shadow = contracts.shadow
    predicate = contracts.logical["artifact_snapshot_predicate"]["where"]
    digest_contract = contracts.logical["artifact_snapshot_digest"]
    domain_literal = "E" + _lit(digest_contract["domain"].replace("\n", "\\n"))
    schema_version = _lit(digest_contract["snapshot_schema_version"])
    snapshot_version = schema_version
    recovery_schema_version = _lit(contracts.physical["recovery_manifest_schema_version"])
    sort_sql = "e ->> 'content_sha256', (e ->> 'size_bytes')::bigint, e ->> 'artifact_kind'"
    normalized = (
        "SELECT e ->> 'content_sha256' AS content_sha256,\n"
        "               (e ->> 'size_bytes')::bigint AS size_bytes,\n"
        "               e ->> 'artifact_kind' AS artifact_kind\n"
        "        FROM jsonb_array_elements(snap -> 'entries') AS e"
    )
    active = (
        "SELECT content_sha256, size_bytes, artifact_kind\n"
        f"        FROM {shadow['catalog']}.artifacts WHERE {predicate}"
    )
    declare = (
        "    binding record;\n"
        "    art record;\n"
        "    r1 jsonb;\n"
        "    snap jsonb;\n"
        "    entry_count bigint;\n"
        "    entry_bytes bigint;\n"
        "    db_count bigint;\n"
        "    db_bytes bigint;\n"
        "    offenders bigint;\n"
        "    conflicting uuid;\n"
    )
    body = (
        "    IF TG_OP = 'UPDATE' THEN\n"
        "        IF NEW.completeness IS DISTINCT FROM OLD.completeness THEN\n"
        "            RAISE EXCEPTION 'completeness changed: % -> %',\n"
        "                OLD.completeness, NEW.completeness USING ERRCODE = 'check_violation';\n"
        "        END IF;\n"
        "        RETURN NULL;\n"
        "    END IF;\n"
        f"    SELECT id INTO conflicting FROM {shadow['catalog']}.backup_sets\n"
        "        WHERE id <> NEW.id\n"
        "          AND (recovery_set_id = NEW.recovery_set_id OR backup_key = NEW.backup_key\n"
        "               OR recovery_manifest_sha256 = NEW.recovery_manifest_sha256);\n"
        "    IF conflicting IS NOT NULL THEN\n"
        "        RAISE EXCEPTION 'conflicting recovery_set_id, backup_key or "
        "recovery_manifest_sha256 (row %)', conflicting USING ERRCODE = 'unique_violation';\n"
        "    END IF;\n"
        "    FOR binding IN\n"
        "        SELECT * FROM (VALUES\n"
        "            ('recovery manifest', NEW.recovery_manifest_artifact_id,\n"
        "             NEW.recovery_manifest_sha256, NEW.recovery_manifest_media_type, 'inline',\n"
        f"             {recovery_schema_version}),\n"
        "            ('database backup', NEW.database_backup_artifact_id,\n"
        "             NEW.database_backup_sha256, NEW.database_backup_media_type, 'external',\n"
        "             NULL),\n"
        "            ('artifact snapshot manifest', NEW.artifact_snapshot_manifest_artifact_id,\n"
        "             NEW.artifact_snapshot_manifest_sha256,\n"
        f"             NEW.artifact_snapshot_manifest_media_type, 'inline', {snapshot_version})\n"
        "        ) AS v(label, artifact_id, digest, media_type, storage, schema_version)\n"
        "        WHERE v.artifact_id IS NOT NULL\n"
        "    LOOP\n"
        f"        SELECT * INTO art FROM {shadow['catalog']}.artifacts\n"
        "            WHERE id = binding.artifact_id FOR UPDATE;\n"
        "        IF NOT FOUND THEN\n"
        "            RAISE EXCEPTION 'a referenced artifact does not exist: % (%)',\n"
        "                binding.artifact_id, binding.label\n"
        "                USING ERRCODE = 'foreign_key_violation';\n"
        "        END IF;\n"
        "        IF art.content_sha256 <> binding.digest\n"
        "           OR art.media_type <> binding.media_type THEN\n"
        "            RAISE EXCEPTION '% triple does not bind one artifact', binding.label\n"
        "                USING ERRCODE = 'check_violation';\n"
        "        END IF;\n"
        "        IF art.verification_state <> 'verified' THEN\n"
        "            RAISE EXCEPTION '% is not verification_state = verified (%)',\n"
        "                binding.label, art.verification_state\n"
        "                USING ERRCODE = 'check_violation';\n"
        "        END IF;\n"
        "        IF art.lifecycle_state <> 'active' THEN\n"
        "            RAISE EXCEPTION '% is not lifecycle_state = active (%)',\n"
        "                binding.label, art.lifecycle_state\n"
        "                USING ERRCODE = 'check_violation';\n"
        "        END IF;\n"
        "        IF art.backup_scope <> 'recovery' THEN\n"
        "            RAISE EXCEPTION '% is not backup_scope = recovery (%)',\n"
        "                binding.label, art.backup_scope USING ERRCODE = 'check_violation';\n"
        "        END IF;\n"
        "        IF art.storage_mode <> binding.storage THEN\n"
        "            RAISE EXCEPTION '% is not stored in its declared storage mode: % (expected "
        "%)', binding.label, art.storage_mode, binding.storage\n"
        "                USING ERRCODE = 'check_violation';\n"
        "        END IF;\n"
        "        IF binding.schema_version IS NOT NULL\n"
        "           AND art.schema_version IS DISTINCT FROM binding.schema_version THEN\n"
        "            RAISE EXCEPTION '% declares schema_version %, not %',\n"
        "                binding.label, art.schema_version, binding.schema_version\n"
        "                USING ERRCODE = 'check_violation';\n"
        "        END IF;\n"
        "        IF binding.storage = 'inline' THEN\n"
        "            IF encode(sha256(art.inline_payload), 'hex') <> binding.digest THEN\n"
        "                RAISE EXCEPTION 'inline manifest bytes do not recompute to their raw "
        "digest (%)', binding.label USING ERRCODE = 'check_violation';\n"
        "            END IF;\n"
        "            IF octet_length(art.inline_payload) <> art.size_bytes THEN\n"
        "                RAISE EXCEPTION 'inline manifest byte size does not match the stored "
        "payload (%)', binding.label USING ERRCODE = 'check_violation';\n"
        "            END IF;\n"
        "        ELSE\n"
        f"            PERFORM 1 FROM {shadow['catalog']}.artifact_locations\n"
        "                WHERE artifact_id = art.id AND location_state = 'present' LIMIT 1;\n"
        "            IF NOT FOUND THEN\n"
        "                RAISE EXCEPTION 'the external database dump has no artifact_locations row "
        "in state present'\n"
        "                    USING ERRCODE = 'check_violation';\n"
        "            END IF;\n"
        "        END IF;\n"
        "    END LOOP;\n"
        f"    SELECT * INTO art FROM {shadow['catalog']}.artifacts\n"
        "        WHERE id = NEW.recovery_manifest_artifact_id;\n"
        "    IF encode(sha256(art.inline_payload), 'hex') <> NEW.recovery_manifest_sha256 THEN\n"
        "        RAISE EXCEPTION 'recovery manifest bytes do not recompute to "
        "recovery_manifest_sha256' USING ERRCODE = 'check_violation';\n"
        "    END IF;\n"
        "    r1 := convert_from(art.inline_payload, 'UTF8')::jsonb;\n"
        + _r1_equality_checks(contracts)
        + "    IF NEW.artifact_snapshot_manifest_artifact_id IS NULL THEN\n"
        "        RETURN NULL;\n"
        "    END IF;\n"
        f"    SELECT * INTO art FROM {shadow['catalog']}.artifacts\n"
        "        WHERE id = NEW.artifact_snapshot_manifest_artifact_id;\n"
        f"    IF encode(sha256(convert_to({domain_literal}, 'UTF8') || art.inline_payload),\n"
        "              'hex') <> NEW.artifact_snapshot_sha256 THEN\n"
        "        RAISE EXCEPTION 'snapshot manifest bytes do not recompute to "
        "artifact_snapshot_sha256' USING ERRCODE = 'check_violation';\n"
        "    END IF;\n"
        "    snap := convert_from(art.inline_payload, 'UTF8')::jsonb;\n"
        f"    IF snap ->> 'predicate' IS DISTINCT FROM {_lit(predicate)} THEN\n"
        "        RAISE EXCEPTION 'the snapshot was not taken with the frozen predicate'\n"
        "            USING ERRCODE = 'check_violation';\n"
        "    END IF;\n"
        f"    IF snap ->> 'schema_version' IS DISTINCT FROM {schema_version} THEN\n"
        "        RAISE EXCEPTION 'the snapshot declares schema_version %, not %',\n"
        f"            snap ->> 'schema_version', {schema_version}\n"
        "            USING ERRCODE = 'check_violation';\n"
        "    END IF;\n"
        "    IF jsonb_typeof(snap -> 'entries') <> 'array' THEN\n"
        "        RAISE EXCEPTION 'the snapshot entries are not a JSON array'\n"
        "            USING ERRCODE = 'check_violation';\n"
        "    END IF;\n"
        "    -- 1/2: exactly the three canonical fields, each of the right type and shape\n"
        "    SELECT count(*) INTO offenders FROM jsonb_array_elements(snap -> 'entries') AS e\n"
        "        WHERE (SELECT count(*) FROM jsonb_object_keys(e)) <> 3\n"
        "           OR NOT (e ? 'content_sha256' AND e ? 'size_bytes' AND e ? 'artifact_kind')\n"
        "           OR jsonb_typeof(e -> 'content_sha256') <> 'string'\n"
        "           OR jsonb_typeof(e -> 'size_bytes') <> 'number'\n"
        "           OR jsonb_typeof(e -> 'artifact_kind') <> 'string'\n"
        "           OR (e ->> 'content_sha256') !~ '^[0-9a-f]{64}$'\n"
        "           OR (e ->> 'size_bytes')::numeric < 0\n"
        "           OR (e ->> 'size_bytes')::numeric <> trunc((e ->> 'size_bytes')::numeric)\n"
        "           OR length(e ->> 'artifact_kind') = 0;\n"
        "    IF offenders <> 0 THEN\n"
        "        RAISE EXCEPTION '% snapshot entries have a noncanonical field inventory, type or "
        "value', offenders USING ERRCODE = 'check_violation';\n"
        "    END IF;\n"
        "    -- 3: unique by the complete triple\n"
        "    SELECT count(*) INTO offenders FROM (\n"
        f"        {normalized}\n"
        "        GROUP BY 1, 2, 3 HAVING count(*) > 1\n"
        "    ) AS duplicated;\n"
        "    IF offenders <> 0 THEN\n"
        "        RAISE EXCEPTION 'the snapshot repeats % entries', offenders\n"
        "            USING ERRCODE = 'check_violation';\n"
        "    END IF;\n"
        "    -- 4: deterministic ascending order\n"
        "    SELECT count(*) INTO offenders FROM (\n"
        "        SELECT position,\n"
        f"               row_number() OVER (ORDER BY {sort_sql}) AS expected\n"
        "        FROM jsonb_array_elements(snap -> 'entries') WITH ORDINALITY AS t(e, position)\n"
        "    ) AS ordered WHERE position <> expected;\n"
        "    IF offenders <> 0 THEN\n"
        "        RAISE EXCEPTION 'the snapshot entries are not in the frozen ascending order'\n"
        "            USING ERRCODE = 'check_violation';\n"
        "    END IF;\n"
        "    SELECT count(*), coalesce(sum((e ->> 'size_bytes')::bigint), 0)\n"
        "        INTO entry_count, entry_bytes\n"
        "        FROM jsonb_array_elements(snap -> 'entries') AS e;\n"
        f"    SELECT count(*), coalesce(sum(size_bytes), 0) INTO db_count, db_bytes\n"
        f"        FROM {shadow['catalog']}.artifacts WHERE {predicate};\n"
        "    -- the artifact-catalog bootstrap (B0) must have run: 0009 creates the catalog EMPTY\n"
        "    IF db_count = 0 AND entry_count > 0 THEN\n"
        "        RAISE EXCEPTION 'the artifact-catalog bootstrap (B0) has not run: the shadow "
        "artifact catalog holds no active operational artifact, so a complete snapshot cannot be "
        "registered' USING ERRCODE = 'check_violation';\n"
        "    END IF;\n"
        "    -- 5: exact bidirectional set equality, multiplicity included\n"
        "    SELECT count(*) INTO offenders FROM (\n"
        f"        {normalized}\n"
        "        EXCEPT ALL\n"
        f"        {active}\n"
        "    ) AS extra;\n"
        "    IF offenders <> 0 THEN\n"
        "        RAISE EXCEPTION '% snapshot entries do not resolve to an active operational "
        "artifact', offenders USING ERRCODE = 'check_violation';\n"
        "    END IF;\n"
        "    SELECT count(*) INTO offenders FROM (\n"
        f"        {active}\n"
        "        EXCEPT ALL\n"
        f"        {normalized}\n"
        "    ) AS omitted;\n"
        "    IF offenders <> 0 THEN\n"
        "        RAISE EXCEPTION '% active operational artifacts are absent from the snapshot',\n"
        "            offenders USING ERRCODE = 'check_violation';\n"
        "    END IF;\n"
        "    -- 6/7: every count and every total agrees, four ways and three ways\n"
        "    IF entry_count IS DISTINCT FROM NEW.artifact_count\n"
        "       OR (snap ->> 'artifact_count')::bigint IS DISTINCT FROM NEW.artifact_count\n"
        "       OR db_count IS DISTINCT FROM NEW.artifact_count THEN\n"
        "        RAISE EXCEPTION 'snapshot entry count <> artifact_count (json %, database %, row "
        "%)', entry_count, db_count, NEW.artifact_count\n"
        "            USING ERRCODE = 'check_violation';\n"
        "    END IF;\n"
        "    IF entry_bytes IS DISTINCT FROM NEW.artifact_total_bytes\n"
        "       OR (snap ->> 'artifact_total_bytes')::bigint\n"
        "          IS DISTINCT FROM NEW.artifact_total_bytes\n"
        "       OR db_bytes IS DISTINCT FROM NEW.artifact_total_bytes THEN\n"
        "        RAISE EXCEPTION 'snapshot entry total size <> artifact_total_bytes (json %, "
        "database %, row %)', entry_bytes, db_bytes, NEW.artifact_total_bytes\n"
        "            USING ERRCODE = 'check_violation';\n"
        "    END IF;\n"
        "    -- 8: every included EXTERNAL artifact is verified, present, and singly primary\n"
        "    SELECT count(*) INTO offenders\n"
        f"        FROM {shadow['catalog']}.artifacts AS a\n"
        f"        WHERE {predicate.replace('lifecycle_state', 'a.lifecycle_state').replace('backup_scope', 'a.backup_scope')}\n"
        "          AND a.storage_mode = 'external'\n"
        "          AND (a.verification_state <> 'verified'\n"
        f"               OR NOT EXISTS (SELECT 1 FROM {shadow['catalog']}.artifact_locations AS l\n"
        "                              WHERE l.artifact_id = a.id\n"
        "                                AND l.location_state = 'present')\n"
        f"               OR (SELECT count(*) FROM {shadow['catalog']}.artifact_locations AS l\n"
        "                   WHERE l.artifact_id = a.id AND l.location_state = 'present'\n"
        "                     AND l.is_primary) <> 1);\n"
        "    IF offenders <> 0 THEN\n"
        "        RAISE EXCEPTION '% snapshotted external artifacts are unverified, absent or "
        "ambiguously primary', offenders USING ERRCODE = 'check_violation';\n"
        "    END IF;\n"
        "    -- 9: every included INLINE artifact still recomputes from its own bytes\n"
        "    SELECT count(*) INTO offenders\n"
        f"        FROM {shadow['catalog']}.artifacts AS a\n"
        f"        WHERE {predicate.replace('lifecycle_state', 'a.lifecycle_state').replace('backup_scope', 'a.backup_scope')}\n"
        "          AND a.storage_mode = 'inline'\n"
        "          AND (encode(sha256(a.inline_payload), 'hex') <> a.content_sha256\n"
        "               OR octet_length(a.inline_payload) <> a.size_bytes);\n"
        "    IF offenders <> 0 THEN\n"
        "        RAISE EXCEPTION '% snapshotted inline artifacts do not recompute', offenders\n"
        "            USING ERRCODE = 'check_violation';\n"
        "    END IF;\n"
        "    -- 10: no recovery-scope artifact on either side\n"
        "    SELECT count(*) INTO offenders\n"
        "        FROM jsonb_array_elements(snap -> 'entries') AS e\n"
        f"        JOIN {shadow['catalog']}.artifacts AS a\n"
        "          ON a.content_sha256 = e ->> 'content_sha256'\n"
        "        WHERE a.backup_scope = 'recovery';\n"
        "    IF offenders <> 0 THEN\n"
        "        RAISE EXCEPTION '% recovery artifacts appear in the snapshot', offenders\n"
        "            USING ERRCODE = 'check_violation';\n"
        "    END IF;\n"
        "    RETURN NULL;\n"
    )
    return declare, body


def _audit_event(shadow: dict[str, str], action: str, table: str, evidence: str) -> str:
    """One append-only audit row for one ACTUAL state change, attributed to the LOGIN identity.

    session_user, not current_user: every audited function is SECURITY DEFINER, so current_user is
    always the definer principal and every row would look identical. The evidence hash is taken
    over a jsonb object, whose textual form is key-normalised and unambiguous - never over
    delimiter-joined strings, where 'a:b' || 'c' and 'a' || 'b:c' collide.
    """
    return (
        f"    INSERT INTO {shadow['audit']}.events\n"
        "        (actor_role, action, object_schema, object_table, object_id, payload_hash)\n"
        f"    VALUES (session_user, {_lit(action)}, {_lit(shadow['catalog'])}, {_lit(table)},\n"
        f"            audited_id, encode(sha256(convert_to(({evidence})::text, 'UTF8')), 'hex'));\n"
    )


#: the frozen recovery-scope boundary: a runtime login may publish operational artifacts only.
def _recovery_scope_guard(runtime_roles: tuple[str, ...]) -> str:
    roles = ", ".join(_lit(role) for role in runtime_roles)
    return (
        "    IF p_backup_scope = 'recovery' AND session_user IN (" + roles + ") THEN\n"
        "        RAISE EXCEPTION 'role % may not create a recovery-scope artifact', session_user\n"
        "            USING ERRCODE = 'insufficient_privilege';\n"
        "    END IF;\n"
    )


#: every immutable artifact column a get-or-verify replay must agree on, provenance included.
ARTIFACT_IMMUTABLE_COLUMNS = (
    ("content_sha256", "digest"),
    ("size_bytes", "payload_size"),
    ("media_type", "p_media_type"),
    ("artifact_kind", "p_artifact_kind"),
    ("storage_mode", None),
    ("retention_class", "p_retention_class"),
    ("backup_scope", "p_backup_scope"),
    ("schema_version", "p_schema_version"),
    ("provenance", "p_provenance"),
)


def _artifact_conflict_check(storage_mode: str, *, inline: bool) -> str:
    """Compare EVERY immutable column, then raise a typed conflict naming the first difference."""
    comparisons = []
    for column, parameter in ARTIFACT_IMMUTABLE_COLUMNS:
        if column == "storage_mode":
            comparisons.append(f"existing.storage_mode IS DISTINCT FROM {_lit(storage_mode)}")
            continue
        comparisons.append(f"existing.{column} IS DISTINCT FROM {parameter}")
    if inline:
        comparisons.append("existing.inline_payload IS DISTINCT FROM p_payload")
    joined = "\n           OR ".join(comparisons)
    return (
        f"        IF {joined} THEN\n"
        "            RAISE EXCEPTION 'artifact % already exists with different immutable "
        "metadata', existing.content_sha256 USING ERRCODE = 'unique_violation';\n"
        "        END IF;\n"
        "        RETURN existing.id;\n"
    )


def _artifact_api_bodies(contracts: Contracts) -> dict[str, tuple[str, str]]:
    """The four artifact-control APIs: insert-or-reread, total comparison, one audit row."""
    s = contracts.shadow
    runtime = tuple(
        role
        for role in contracts.api["role_provisioning"]["required_roles"]
        if role not in (DEFINER, "minos_migrate")
    )
    guard = _recovery_scope_guard(runtime)
    out: dict[str, tuple[str, str]] = {}

    inline_evidence = (
        "jsonb_build_object('action', 'artifact.published_inline', 'content_sha256', digest,\n"
        "                'size_bytes', payload_size, 'media_type', p_media_type,\n"
        "                'artifact_kind', p_artifact_kind, 'storage_mode', 'inline',\n"
        "                'retention_class', p_retention_class, 'backup_scope', p_backup_scope,\n"
        "                'schema_version', p_schema_version, 'provenance', p_provenance)"
    )
    out["catalog.get_or_verify_inline_artifact"] = (
        "    existing record;\n    audited_id uuid;\n    digest text;\n    payload_size bigint;\n",
        "    IF p_payload IS NULL THEN\n"
        "        RAISE EXCEPTION 'inline payload must not be null'\n"
        "            USING ERRCODE = 'invalid_parameter_value';\n"
        "    END IF;\n"
        "    IF octet_length(p_payload) > 65536 THEN\n"
        "        RAISE EXCEPTION 'inline payload of % bytes exceeds the 65536-byte bound',\n"
        "            octet_length(p_payload) USING ERRCODE = 'invalid_parameter_value';\n"
        "    END IF;\n"
        "    IF p_backup_scope NOT IN ('operational', 'recovery') THEN\n"
        "        RAISE EXCEPTION 'backup_scope must be operational or recovery, got %',\n"
        "            p_backup_scope USING ERRCODE = 'invalid_parameter_value';\n"
        "    END IF;\n" + guard + "    digest := encode(sha256(p_payload), 'hex');\n"
        "    payload_size := octet_length(p_payload);\n"
        "    -- insert first; a concurrent winner is resolved by re-reading, never by overwriting\n"
        "    BEGIN\n"
        f"        INSERT INTO {s['catalog']}.artifacts\n"
        "            (artifact_kind, content_sha256, size_bytes, media_type, storage_mode,\n"
        "             inline_payload, lifecycle_state, retention_class, backup_scope,\n"
        "             schema_version, provenance, verification_state, first_verified_at,\n"
        "             last_verified_at)\n"
        "        VALUES (p_artifact_kind, digest, payload_size, p_media_type, 'inline', p_payload,\n"
        "                'active', p_retention_class, p_backup_scope, p_schema_version,\n"
        "                p_provenance, 'verified', now(), now())\n"
        "        RETURNING id INTO audited_id;\n"
        "    EXCEPTION WHEN unique_violation THEN\n"
        "        audited_id := NULL;\n"
        "    END;\n"
        "    IF audited_id IS NULL THEN\n"
        f"        SELECT * INTO existing FROM {s['catalog']}.artifacts\n"
        "            WHERE content_sha256 = digest FOR UPDATE;\n"
        "        IF NOT FOUND THEN\n"
        "            RAISE EXCEPTION 'a uniqueness conflict on % resolved to no row', digest\n"
        "                USING ERRCODE = 'unique_violation';\n"
        "        END IF;\n"
        + _artifact_conflict_check("inline", inline=True)
        + "    END IF;\n"
        + _audit_event(s, "artifact.published_inline", "artifacts", inline_evidence)
        + "    RETURN audited_id;\n",
    )

    external_evidence = (
        "jsonb_build_object('action', 'artifact.registered_external',\n"
        "                'content_sha256', digest, 'size_bytes', payload_size,\n"
        "                'media_type', p_media_type, 'artifact_kind', p_artifact_kind,\n"
        "                'storage_mode', 'external', 'retention_class', p_retention_class,\n"
        "                'backup_scope', p_backup_scope, 'schema_version', p_schema_version,\n"
        "                'provenance', p_provenance)"
    )
    out["catalog.get_or_verify_external_artifact"] = (
        "    existing record;\n    audited_id uuid;\n    digest text;\n    payload_size bigint;\n",
        "    IF p_content_sha256 !~ '^[0-9a-f]{64}$' THEN\n"
        "        RAISE EXCEPTION 'content_sha256 must be 64 lowercase hex characters'\n"
        "            USING ERRCODE = 'invalid_parameter_value';\n"
        "    END IF;\n"
        "    IF p_size_bytes IS NULL OR p_size_bytes < 0 THEN\n"
        "        RAISE EXCEPTION 'size_bytes must be non-negative, got %', p_size_bytes\n"
        "            USING ERRCODE = 'invalid_parameter_value';\n"
        "    END IF;\n"
        "    IF p_backup_scope NOT IN ('operational', 'recovery') THEN\n"
        "        RAISE EXCEPTION 'backup_scope must be operational or recovery, got %',\n"
        "            p_backup_scope USING ERRCODE = 'invalid_parameter_value';\n"
        "    END IF;\n" + guard + "    digest := p_content_sha256;\n"
        "    payload_size := p_size_bytes;\n"
        "    BEGIN\n"
        f"        INSERT INTO {s['catalog']}.artifacts\n"
        "            (artifact_kind, content_sha256, size_bytes, media_type, storage_mode,\n"
        "             lifecycle_state, retention_class, backup_scope, schema_version, provenance,\n"
        "             verification_state)\n"
        "        VALUES (p_artifact_kind, digest, payload_size, p_media_type, 'external',\n"
        "                'active', p_retention_class, p_backup_scope, p_schema_version,\n"
        "                p_provenance, 'unverified')\n"
        "        RETURNING id INTO audited_id;\n"
        "    EXCEPTION WHEN unique_violation THEN\n"
        "        audited_id := NULL;\n"
        "    END;\n"
        "    IF audited_id IS NULL THEN\n"
        f"        SELECT * INTO existing FROM {s['catalog']}.artifacts\n"
        "            WHERE content_sha256 = digest FOR UPDATE;\n"
        "        IF NOT FOUND THEN\n"
        "            RAISE EXCEPTION 'a uniqueness conflict on % resolved to no row', digest\n"
        "                USING ERRCODE = 'unique_violation';\n"
        "        END IF;\n"
        + _artifact_conflict_check("external", inline=False)
        + "    END IF;\n"
        + _audit_event(s, "artifact.registered_external", "artifacts", external_evidence)
        + "    RETURN audited_id;\n",
    )

    location_evidence = (
        "jsonb_build_object('action', 'artifact_location.registered',\n"
        "                'artifact_id', p_artifact_id, 'backend_key', p_backend_key,\n"
        "                'object_key', p_object_key, 'is_primary', p_is_primary,\n"
        "                'location_state', 'present')"
    )
    out["catalog.get_or_verify_artifact_location"] = (
        "    backend record;\n    existing record;\n    audited_id uuid;\n",
        "    IF p_object_key IS NULL OR length(p_object_key) = 0 THEN\n"
        "        RAISE EXCEPTION 'object_key must be a non-empty relative key'\n"
        "            USING ERRCODE = 'invalid_parameter_value';\n"
        "    END IF;\n"
        "    IF p_object_key ~ '^/' OR p_object_key ~ '^[a-zA-Z][a-zA-Z0-9+.-]*://'\n"
        "       OR p_object_key ~ '(^|/)[.][.](/|$)' OR p_object_key ~ '//'\n"
        "       OR p_object_key ~ '/$' OR p_object_key ~ '^[.]/' THEN\n"
        "        RAISE EXCEPTION 'object_key % is not a clean relative key', p_object_key\n"
        "            USING ERRCODE = 'invalid_parameter_value';\n"
        "    END IF;\n"
        f"    PERFORM 1 FROM {s['catalog']}.artifacts WHERE id = p_artifact_id FOR UPDATE;\n"
        "    IF NOT FOUND THEN\n"
        "        RAISE EXCEPTION 'unknown artifact %', p_artifact_id\n"
        "            USING ERRCODE = 'foreign_key_violation';\n"
        "    END IF;\n"
        f"    SELECT * INTO backend FROM {s['catalog']}.storage_backends\n"
        "        WHERE backend_key = p_backend_key;\n"
        "    IF NOT FOUND THEN\n"
        "        RAISE EXCEPTION 'unknown storage backend %', p_backend_key\n"
        "            USING ERRCODE = 'foreign_key_violation';\n"
        "    END IF;\n"
        "    BEGIN\n"
        f"        INSERT INTO {s['catalog']}.artifact_locations\n"
        "            (artifact_id, backend_id, object_key, location_state, is_primary)\n"
        "        VALUES (p_artifact_id, backend.id, p_object_key, 'present', p_is_primary)\n"
        "        RETURNING id INTO audited_id;\n"
        "    EXCEPTION WHEN unique_violation THEN\n"
        "        audited_id := NULL;\n"
        "    END;\n"
        "    IF audited_id IS NULL THEN\n"
        f"        SELECT * INTO existing FROM {s['catalog']}.artifact_locations\n"
        "            WHERE (backend_id = backend.id AND object_key = p_object_key)\n"
        "               OR (artifact_id = p_artifact_id AND backend_id = backend.id)\n"
        "            FOR UPDATE;\n"
        "        IF NOT FOUND THEN\n"
        "            RAISE EXCEPTION 'a uniqueness conflict on %/% resolved to no row',\n"
        "                p_backend_key, p_object_key USING ERRCODE = 'unique_violation';\n"
        "        END IF;\n"
        "        IF existing.artifact_id IS DISTINCT FROM p_artifact_id\n"
        "           OR existing.backend_id IS DISTINCT FROM backend.id\n"
        "           OR existing.object_key IS DISTINCT FROM p_object_key\n"
        "           OR existing.is_primary IS DISTINCT FROM p_is_primary THEN\n"
        "            RAISE EXCEPTION 'location %/% is already registered with a different "
        "identity', p_backend_key, existing.object_key USING ERRCODE = 'unique_violation';\n"
        "        END IF;\n"
        "        RETURN existing.id;\n"
        "    END IF;\n"
        + _audit_event(s, "artifact_location.registered", "artifact_locations", location_evidence)
        + "    RETURN audited_id;\n",
    )

    verification_evidence = (
        "jsonb_build_object('action', 'artifact.verification_recorded',\n"
        "                'artifact_id', art.id, 'content_sha256', art.content_sha256,\n"
        "                'size_bytes', art.size_bytes, 'storage_mode', art.storage_mode,\n"
        "                'from_state', art.verification_state, 'to_state', outcome,\n"
        "                'location_id', loc_id)"
    )
    out["catalog.record_artifact_verification"] = (
        "    art record;\n    loc record;\n    loc_id uuid;\n"
        "    candidates bigint;\n    audited_id uuid;\n    outcome text;\n"
        "    observed_digest text;\n    observed_size bigint;\n",
        f"    SELECT * INTO art FROM {s['catalog']}.artifacts\n"
        "        WHERE id = p_artifact_id FOR UPDATE;\n"
        "    IF NOT FOUND THEN\n"
        "        RAISE EXCEPTION 'unknown artifact %', p_artifact_id\n"
        "            USING ERRCODE = 'foreign_key_violation';\n"
        "    END IF;\n"
        "    IF art.storage_mode = 'inline' THEN\n"
        "        -- the authoritative bytes are held here; a caller's claim about them is ignored\n"
        "        observed_digest := encode(sha256(art.inline_payload), 'hex');\n"
        "        observed_size := octet_length(art.inline_payload);\n"
        "    ELSE\n"
        "        observed_digest := p_observed_sha256;\n"
        "        observed_size := p_observed_size_bytes;\n"
        "        IF p_location_id IS NULL THEN\n"
        f"            SELECT count(*) INTO candidates FROM {s['catalog']}.artifact_locations\n"
        "                WHERE artifact_id = art.id;\n"
        "            IF candidates <> 1 THEN\n"
        "                RAISE EXCEPTION 'an external artifact needs exactly one named location "
        "(% candidates)', candidates USING ERRCODE = 'invalid_parameter_value';\n"
        "            END IF;\n"
        f"            SELECT * INTO loc FROM {s['catalog']}.artifact_locations\n"
        "                WHERE artifact_id = art.id FOR UPDATE;\n"
        "            loc_id := loc.id;\n"
        "        ELSE\n"
        f"            SELECT * INTO loc FROM {s['catalog']}.artifact_locations\n"
        "                WHERE id = p_location_id AND artifact_id = art.id FOR UPDATE;\n"
        "            IF NOT FOUND THEN\n"
        "                RAISE EXCEPTION 'location % does not belong to artifact %',\n"
        "                    p_location_id, art.id USING ERRCODE = 'foreign_key_violation';\n"
        "            END IF;\n"
        "            loc_id := loc.id;\n"
        "        END IF;\n"
        "    END IF;\n"
        "    IF observed_digest IS NULL OR observed_size IS NULL THEN\n"
        "        outcome := 'missing';\n"
        "    ELSIF observed_digest = art.content_sha256 AND observed_size = art.size_bytes THEN\n"
        "        outcome := 'verified';\n"
        "    ELSE\n"
        "        outcome := 'corrupt';\n"
        "    END IF;\n"
        "    IF art.verification_state = outcome THEN\n"
        "        -- a non-state-changing observation: refresh the timestamps, write no event\n"
        f"        UPDATE {s['catalog']}.artifacts SET last_verified_at = now()\n"
        "            WHERE id = art.id;\n"
        "        IF loc_id IS NOT NULL THEN\n"
        f"            UPDATE {s['catalog']}.artifact_locations SET last_verified_at = now()\n"
        "                WHERE id = loc_id;\n"
        "        END IF;\n"
        "        RETURN outcome;\n"
        "    END IF;\n"
        f"    UPDATE {s['catalog']}.artifacts\n"
        "        SET verification_state = outcome,\n"
        "            first_verified_at = CASE WHEN outcome = 'verified'\n"
        "                                     THEN coalesce(art.first_verified_at, now())\n"
        "                                     ELSE art.first_verified_at END,\n"
        "            last_verified_at = now()\n"
        "        WHERE id = art.id;\n"
        "    IF loc_id IS NOT NULL THEN\n"
        f"        UPDATE {s['catalog']}.artifact_locations\n"
        "            SET location_state = CASE WHEN outcome = 'verified' THEN 'present'\n"
        "                                      WHEN outcome = 'missing' THEN 'missing'\n"
        "                                      ELSE 'corrupt' END,\n"
        "                -- a non-present location may not be primary; a restored one reclaims\n"
        "                -- the role whenever no other present location already holds it\n"
        "                is_primary = (outcome = 'verified' AND NOT EXISTS (\n"
        f"                    SELECT 1 FROM {s['catalog']}.artifact_locations AS other\n"
        "                    WHERE other.artifact_id = art.id AND other.id <> loc_id\n"
        "                      AND other.is_primary AND other.location_state = 'present')),\n"
        "                last_verified_at = now()\n"
        "            WHERE id = loc_id;\n"
        "    END IF;\n"
        "    audited_id := art.id;\n"
        + _audit_event(s, "artifact.verification_recorded", "artifacts", verification_evidence)
        + "    RETURN outcome;\n",
    )
    return out


BACKUP_SET_IMMUTABLE_COLUMNS = (
    ("backup_key", "p_manifest ->> 'backup_key'"),
    ("recovery_set_id", "(p_manifest ->> 'recovery_set_id')::uuid"),
    ("alembic_revision", "p_manifest ->> 'source_alembic_revision'"),
    ("quiesce_started_at", "(p_manifest ->> 'quiesce_started_at')::timestamptz"),
    ("quiesce_ended_at", "(p_manifest ->> 'quiesce_ended_at')::timestamptz"),
    ("manifest_schema_version", "p_manifest ->> 'schema_version'"),
    ("database_name", "p_manifest ->> 'database_name'"),
    ("recovery_manifest_artifact_id", "(p_manifest ->> 'recovery_manifest_artifact_id')::uuid"),
    ("recovery_manifest_sha256", "p_manifest ->> 'recovery_manifest_sha256'"),
    ("database_backup_kind", "p_manifest ->> 'database_backup_kind'"),
    ("database_backup_artifact_id", "(p_manifest ->> 'database_backup_artifact_id')::uuid"),
    ("database_backup_sha256", "p_manifest ->> 'database_backup_sha256'"),
    ("database_backup_size_bytes", "(p_manifest ->> 'database_backup_size_bytes')::bigint"),
    ("wal_start_lsn", "p_manifest ->> 'wal_start_lsn'"),
    ("wal_end_lsn", "p_manifest ->> 'wal_end_lsn'"),
    (
        "artifact_snapshot_manifest_artifact_id",
        "(p_manifest ->> 'artifact_snapshot_manifest_artifact_id')::uuid",
    ),
    ("artifact_snapshot_manifest_sha256", "p_manifest ->> 'artifact_snapshot_manifest_sha256'"),
    (
        # parenthesised: `IS DISTINCT FROM CASE ... END` is a syntax error without it
        "artifact_snapshot_manifest_media_type",
        "(CASE WHEN p_completeness = 'complete' "
        "THEN 'application/vnd.minos.artifact-snapshot+json' END)",
    ),
    ("artifact_snapshot_sha256", "p_manifest ->> 'artifact_snapshot_sha256'"),
    ("artifact_count", "(p_manifest ->> 'artifact_count')::bigint"),
    ("artifact_total_bytes", "(p_manifest ->> 'artifact_total_bytes')::bigint"),
    ("postgresql_version", "p_manifest ->> 'postgresql_version'"),
    ("backup_tool_version", "p_manifest ->> 'backup_tool_version'"),
    ("artifact_verification_tool_version", "p_manifest ->> 'artifact_verification_tool_version'"),
    ("completeness", "p_completeness"),
    ("created_at", "(p_manifest ->> 'created_at')::timestamptz"),
)


def _register_backup_set_body(contracts: Contracts) -> tuple[str, str]:
    """R2. Serialised on the recovery set's own identity; an exact replay returns the same row."""
    s = contracts.shadow
    columns = [column for column, _ in BACKUP_SET_IMMUTABLE_COLUMNS]
    values = [expression for _, expression in BACKUP_SET_IMMUTABLE_COLUMNS]
    column_sql = ",\n         ".join(
        ", ".join(columns[index : index + 3]) for index in range(0, len(columns), 3)
    )
    value_sql = ",\n           ".join(
        ", ".join(values[index : index + 2]) for index in range(0, len(values), 2)
    )
    comparisons = "\n           OR ".join(
        f"existing.{column} IS DISTINCT FROM {expression}"
        for column, expression in BACKUP_SET_IMMUTABLE_COLUMNS
    )
    declare = "    existing record;\n    new_id uuid;\n"
    body = (
        "    IF p_completeness NOT IN ('complete', 'database_only') THEN\n"
        "        RAISE EXCEPTION 'completeness must be complete or database_only, got %',\n"
        "            p_completeness USING ERRCODE = 'invalid_parameter_value';\n"
        "    END IF;\n"
        "    -- deterministic in the recovery set's own identity, released by commit or abort\n"
        "    PERFORM pg_advisory_xact_lock(\n"
        "        hashtextextended(p_manifest ->> 'recovery_set_id', 0));\n"
        f"    SELECT * INTO existing FROM {s['catalog']}.backup_sets\n"
        "        WHERE recovery_set_id = (p_manifest ->> 'recovery_set_id')::uuid FOR UPDATE;\n"
        "    IF FOUND THEN\n"
        f"        IF {comparisons} THEN\n"
        "            RAISE EXCEPTION 'recovery set % is already registered with different "
        "immutable data', existing.recovery_set_id USING ERRCODE = 'unique_violation';\n"
        "        END IF;\n"
        "        RETURN existing.id;\n"
        "    END IF;\n"
        f"    INSERT INTO {s['catalog']}.backup_sets (\n"
        f"        {column_sql})\n"
        f"    VALUES ({value_sql})\n"
        "    RETURNING id INTO new_id;\n"
        f"    INSERT INTO {s['audit']}.admin_operations\n"
        "        (operation_kind, alembic_revision_from, alembic_revision_to, backup_set_id,\n"
        "         outcome, evidence_hash)\n"
        "    VALUES ('migration', p_manifest ->> 'source_alembic_revision',\n"
        "            p_manifest ->> 'source_alembic_revision', new_id, 'succeeded',\n"
        "            encode(sha256(convert_to(jsonb_build_object(\n"
        "                'action', 'backup_set.registered',\n"
        "                'recovery_set_id', p_manifest ->> 'recovery_set_id',\n"
        "                'backup_key', p_manifest ->> 'backup_key',\n"
        "                'recovery_manifest_sha256', p_manifest ->> 'recovery_manifest_sha256',\n"
        "                'completeness', p_completeness)::text, 'UTF8')), 'hex'));\n"
        "    RETURN new_id;\n"
    )
    return declare, body


def _api_bodies(contracts: Contracts) -> dict[str, tuple[str, str]]:
    """The sixteen SECURITY DEFINER API functions, fully qualified against the shadow namespace."""
    s = contracts.shadow
    out: dict[str, tuple[str, str]] = {}

    out["catalog.__removed_get_or_verify_artifact"] = (
        "    existing record;\n    new_id uuid;\n",
        "    IF p_backup_scope NOT IN ('operational', 'recovery') THEN\n"
        "        RAISE EXCEPTION 'backup_scope must be operational or recovery, got %',\n"
        "            p_backup_scope USING ERRCODE = 'invalid_parameter_value';\n"
        "    END IF;\n"
        f"    SELECT * INTO existing FROM {s['catalog']}.artifacts\n"
        "        WHERE content_sha256 = p_content_sha256 FOR UPDATE;\n"
        "    IF FOUND THEN\n"
        "        IF existing.size_bytes <> p_size_bytes OR existing.media_type <> p_media_type\n"
        "           OR existing.artifact_kind <> p_artifact_kind\n"
        "           OR existing.backup_scope <> p_backup_scope THEN\n"
        "            RAISE EXCEPTION 'artifact % already exists with different immutable metadata',\n"
        "                p_content_sha256 USING ERRCODE = 'unique_violation';\n"
        "        END IF;\n"
        "        RETURN existing.id;\n"
        "    END IF;\n"
        f"    INSERT INTO {s['catalog']}.artifacts\n"
        "        (artifact_kind, content_sha256, size_bytes, media_type, storage_mode,\n"
        "         lifecycle_state, retention_class, backup_scope, provenance, verification_state)\n"
        "    VALUES (p_artifact_kind, p_content_sha256, p_size_bytes, p_media_type, 'external',\n"
        "            'active', 'standard', p_backup_scope, p_provenance, 'unverified')\n"
        "    RETURNING id INTO new_id;\n"
        "    RETURN new_id;\n",
    )

    out["experiments.persist_experiment_plan"] = (
        "    new_id uuid;\n",
        f"    SELECT id INTO new_id FROM {s['experiments']}.experiment_plans\n"
        "        WHERE plan_hash = p_plan ->> 'plan_hash';\n"
        "    IF FOUND THEN\n"
        "        RETURN new_id;\n"
        "    END IF;\n"
        f"    INSERT INTO {s['experiments']}.experiment_plans\n"
        "        (plan_hash, snapshot_id, candidate_set_id, parameter_space_id, partition,\n"
        "         member_count, candidate_count, logical_job_count)\n"
        "    SELECT p_plan ->> 'plan_hash', (p_plan ->> 'snapshot_id')::uuid,\n"
        "           (p_plan ->> 'candidate_set_id')::uuid,\n"
        "           (p_plan ->> 'parameter_space_id')::uuid, p_plan ->> 'partition',\n"
        "           (p_plan ->> 'member_count')::integer,\n"
        "           (p_plan ->> 'candidate_count')::integer,\n"
        "           (p_plan ->> 'logical_job_count')::bigint\n"
        "    RETURNING id INTO new_id;\n"
        f"    INSERT INTO {s['experiments']}.experiment_plan_members\n"
        "        (plan_id, snapshot_member_id, bam_profile_id, dataset_id, member_index)\n"
        "    SELECT new_id, (m ->> 'snapshot_member_id')::uuid, (m ->> 'bam_profile_id')::uuid,\n"
        "           (m ->> 'dataset_id')::uuid, (m ->> 'member_index')::integer\n"
        "    FROM jsonb_array_elements(p_plan -> 'members') AS m;\n"
        f"    INSERT INTO {s['experiments']}.experiment_plan_configs\n"
        "        (plan_id, candidate_config_id, config_hash, config_index)\n"
        "    SELECT new_id, (c ->> 'candidate_config_id')::uuid, c ->> 'config_hash',\n"
        "           (c ->> 'config_index')::integer\n"
        "    FROM jsonb_array_elements(p_plan -> 'configs') AS c;\n"
        "    RETURN new_id;\n",
    )

    out["experiments.enqueue_plan_jobs"] = (
        "    created bigint := 0;\n",
        "    IF p_max_jobs IS NULL OR p_max_jobs <= 0 THEN\n"
        "        RAISE EXCEPTION 'p_max_jobs must be positive, got %', p_max_jobs\n"
        "            USING ERRCODE = 'invalid_parameter_value';\n"
        "    END IF;\n"
        f"    PERFORM 1 FROM {s['experiments']}.experiment_plans WHERE id = p_plan_id;\n"
        "    IF NOT FOUND THEN\n"
        "        RAISE EXCEPTION 'unknown plan %', p_plan_id\n"
        "            USING ERRCODE = 'foreign_key_violation';\n"
        "    END IF;\n"
        f"    WITH candidate AS (\n"
        f"        SELECT m.id AS member_id, c.id AS config_id,\n"
        "               encode(sha256(convert_to(p_plan_id::text || ':' || m.id::text || ':'\n"
        "                                        || c.id::text, 'UTF8')), 'hex') AS job_key\n"
        f"        FROM {s['experiments']}.experiment_plan_members AS m\n"
        f"        JOIN {s['experiments']}.experiment_plan_configs AS c\n"
        "          ON c.plan_id = m.plan_id\n"
        "        WHERE m.plan_id = p_plan_id\n"
        "        ORDER BY m.id, c.id\n"
        "        LIMIT p_max_jobs\n"
        "    ),\n"
        f"    inserted AS (\n"
        f"        INSERT INTO {s['experiments']}.experiment_jobs\n"
        "            (plan_id, plan_member_id, plan_config_id, job_key, status, attempt_count)\n"
        "        SELECT p_plan_id, member_id, config_id, job_key, 'PENDING', 0 FROM candidate\n"
        "        ON CONFLICT ON CONSTRAINT uq_jobs_logical_identity DO NOTHING\n"
        "        RETURNING id\n"
        "    ),\n"
        "    logged AS (\n"
        f"        INSERT INTO {s['experiments']}.job_events\n"
        "            (job_id, from_status, to_status, actor_role)\n"
        "        SELECT id, NULL, 'PENDING', session_user FROM inserted\n"
        "        RETURNING 1\n"
        "    )\n"
        "    SELECT count(*) INTO created FROM logged;\n"
        "    RETURN created;\n",
    )

    out["experiments.claim_next_job"] = (
        "    claimed uuid;\n",
        "    IF p_worker_id IS NULL OR length(p_worker_id) = 0 THEN\n"
        "        RAISE EXCEPTION 'worker id must be non-empty'\n"
        "            USING ERRCODE = 'invalid_parameter_value';\n"
        "    END IF;\n"
        "    IF p_lease_seconds IS NULL OR p_lease_seconds <= 0 THEN\n"
        "        RAISE EXCEPTION 'lease_seconds must be positive, got %', p_lease_seconds\n"
        "            USING ERRCODE = 'invalid_parameter_value';\n"
        "    END IF;\n"
        f"    SELECT id INTO claimed FROM {s['experiments']}.experiment_jobs\n"
        "        WHERE status = 'PENDING' AND (p_plan_id IS NULL OR plan_id = p_plan_id)\n"
        "        ORDER BY created_at, id\n"
        "        FOR UPDATE SKIP LOCKED LIMIT 1;\n"
        "    IF claimed IS NULL THEN\n"
        "        RETURN NULL;\n"
        "    END IF;\n"
        f"    UPDATE {s['experiments']}.experiment_jobs\n"
        "        SET status = 'CLAIMED', claimed_by = p_worker_id, claimed_at = now(),\n"
        "            lease_expires_at = now() + make_interval(secs => p_lease_seconds),\n"
        "            updated_at = now()\n"
        "        WHERE id = claimed;\n"
        f"    INSERT INTO {s['experiments']}.job_events\n"
        "        (job_id, from_status, to_status, actor_role, worker_id)\n"
        "    VALUES (claimed, 'PENDING', 'CLAIMED', session_user, p_worker_id);\n"
        "    RETURN claimed;\n",
    )

    out["experiments.start_attempt"] = (
        "    job record;\n    attempt_id uuid;\n",
        f"    SELECT * INTO job FROM {s['experiments']}.experiment_jobs\n"
        "        WHERE id = p_job_id FOR UPDATE;\n"
        "    IF NOT FOUND OR job.status <> 'CLAIMED' OR job.claimed_by IS DISTINCT FROM p_worker_id"
        " THEN\n"
        "        RAISE EXCEPTION 'job % is not CLAIMED by %', p_job_id, p_worker_id\n"
        "            USING ERRCODE = 'check_violation';\n"
        "    END IF;\n"
        f"    INSERT INTO {s['experiments']}.execution_attempts\n"
        "        (job_id, plan_id, attempt_number, worker_id, started_at)\n"
        "    VALUES (job.id, job.plan_id, job.attempt_count + 1, p_worker_id, now())\n"
        "    RETURNING id INTO attempt_id;\n"
        f"    UPDATE {s['experiments']}.experiment_jobs\n"
        "        SET status = 'RUNNING', attempt_count = job.attempt_count + 1, updated_at = now()\n"
        "        WHERE id = job.id;\n"
        f"    INSERT INTO {s['experiments']}.job_events\n"
        "        (job_id, attempt_id, from_status, to_status, actor_role, worker_id)\n"
        "    VALUES (job.id, attempt_id, 'CLAIMED', 'RUNNING', session_user, p_worker_id);\n"
        "    RETURN attempt_id;\n",
    )

    out["experiments.extend_attempt_lease"] = (
        "    attempt record;\n    job record;\n    new_expiry timestamptz;\n",
        f"    SELECT * INTO attempt FROM {s['experiments']}.execution_attempts\n"
        "        WHERE id = p_attempt_id;\n"
        "    IF NOT FOUND OR attempt.outcome IS NOT NULL THEN\n"
        "        RAISE EXCEPTION 'attempt % is not open', p_attempt_id\n"
        "            USING ERRCODE = 'check_violation';\n"
        "    END IF;\n"
        f"    SELECT * INTO job FROM {s['experiments']}.experiment_jobs\n"
        "        WHERE id = attempt.job_id FOR UPDATE;\n"
        "    IF job.claimed_by IS DISTINCT FROM p_worker_id THEN\n"
        "        RAISE EXCEPTION 'worker % does not hold job %', p_worker_id, job.id\n"
        "            USING ERRCODE = 'check_violation';\n"
        "    END IF;\n"
        "    new_expiry := greatest(job.lease_expires_at,\n"
        "                           now() + make_interval(secs => p_lease_seconds));\n"
        f"    UPDATE {s['experiments']}.experiment_jobs\n"
        "        SET lease_expires_at = new_expiry, updated_at = now() WHERE id = job.id;\n"
        "    RETURN new_expiry;\n",
    )

    out["experiments.record_attempt_result"] = (
        "    attempt record;\n    job record;\n    result_id uuid;\n",
        f"    SELECT * INTO attempt FROM {s['experiments']}.execution_attempts\n"
        "        WHERE id = p_attempt_id;\n"
        "    IF NOT FOUND OR attempt.outcome IS NOT NULL THEN\n"
        "        RAISE EXCEPTION 'attempt % is not open', p_attempt_id\n"
        "            USING ERRCODE = 'check_violation';\n"
        "    END IF;\n"
        f"    SELECT * INTO job FROM {s['experiments']}.experiment_jobs\n"
        "        WHERE id = attempt.job_id FOR UPDATE;\n"
        "    IF job.claimed_by IS DISTINCT FROM p_worker_id THEN\n"
        "        RAISE EXCEPTION 'worker % does not hold job %', p_worker_id, job.id\n"
        "            USING ERRCODE = 'check_violation';\n"
        "    END IF;\n"
        f"    INSERT INTO {s['experiments']}.execution_results\n"
        "        (attempt_id, job_id, plan_id, job_key, result_hash, input_identity_hash,\n"
        "         logical_argv_hash, vcf_artifact_id, manifest_artifact_id)\n"
        "    SELECT attempt.id, job.id, job.plan_id, job.job_key, p_result ->> 'result_hash',\n"
        "           p_result ->> 'input_identity_hash', p_result ->> 'logical_argv_hash',\n"
        "           (p_result ->> 'vcf_artifact_id')::uuid,\n"
        "           (p_result ->> 'manifest_artifact_id')::uuid\n"
        "    RETURNING id INTO result_id;\n"
        f"    UPDATE {s['experiments']}.execution_attempts\n"
        "        SET outcome = 'SUCCEEDED', finished_at = now(),\n"
        "            runtime_ms = (p_result ->> 'runtime_ms')::bigint,\n"
        "            gatk_executable_sha256 = p_result ->> 'gatk_executable_sha256',\n"
        "            gatk_version = p_result ->> 'gatk_version'\n"
        "        WHERE id = attempt.id;\n"
        f"    UPDATE {s['experiments']}.experiment_jobs\n"
        "        SET status = 'SUCCEEDED', terminal_attempt_id = attempt.id, updated_at = now()\n"
        "        WHERE id = job.id;\n"
        f"    INSERT INTO {s['experiments']}.job_events\n"
        "        (job_id, attempt_id, from_status, to_status, actor_role, worker_id)\n"
        "    VALUES (job.id, attempt.id, 'RUNNING', 'SUCCEEDED', session_user, p_worker_id);\n"
        "    RETURN result_id;\n",
    )

    out["experiments.record_attempt_failure"] = (
        "    attempt record;\n    job record;\n    failure_id uuid;\n",
        f"    SELECT * INTO attempt FROM {s['experiments']}.execution_attempts\n"
        "        WHERE id = p_attempt_id;\n"
        "    IF NOT FOUND OR attempt.outcome IS NOT NULL THEN\n"
        "        RAISE EXCEPTION 'attempt % is not open', p_attempt_id\n"
        "            USING ERRCODE = 'check_violation';\n"
        "    END IF;\n"
        f"    SELECT * INTO job FROM {s['experiments']}.experiment_jobs\n"
        "        WHERE id = attempt.job_id FOR UPDATE;\n"
        "    IF job.claimed_by IS DISTINCT FROM p_worker_id THEN\n"
        "        RAISE EXCEPTION 'worker % does not hold job %', p_worker_id, job.id\n"
        "            USING ERRCODE = 'check_violation';\n"
        "    END IF;\n"
        f"    INSERT INTO {s['experiments']}.execution_failures\n"
        "        (attempt_id, job_id, plan_id, failure_code, exit_code, stderr_sha256)\n"
        "    SELECT attempt.id, job.id, job.plan_id, p_failure ->> 'failure_code',\n"
        "           (p_failure ->> 'exit_code')::integer, p_failure ->> 'stderr_sha256'\n"
        "    RETURNING id INTO failure_id;\n"
        f"    UPDATE {s['experiments']}.execution_attempts\n"
        "        SET outcome = 'FAILED', finished_at = now(), runtime_ms = 0,\n"
        "            gatk_executable_sha256 = coalesce(p_failure ->> 'gatk_executable_sha256',\n"
        "                                              repeat('0', 64)),\n"
        "            gatk_version = coalesce(p_failure ->> 'gatk_version', 'unknown')\n"
        "        WHERE id = attempt.id;\n"
        f"    UPDATE {s['experiments']}.experiment_jobs\n"
        "        SET status = 'FAILED', terminal_attempt_id = attempt.id, updated_at = now()\n"
        "        WHERE id = job.id;\n"
        f"    INSERT INTO {s['experiments']}.job_events\n"
        "        (job_id, attempt_id, from_status, to_status, actor_role, worker_id)\n"
        "    VALUES (job.id, attempt.id, 'RUNNING', 'FAILED', session_user, p_worker_id);\n"
        "    RETURN failure_id;\n",
    )

    out["evaluation.record_evaluation_scores"] = (
        "    run record;\n    written bigint := 0;\n",
        f"    SELECT * INTO run FROM {s['evaluation']}.evaluation_runs\n"
        "        WHERE id = p_run_id FOR UPDATE;\n"
        "    IF NOT FOUND OR run.state <> 'running' THEN\n"
        "        RAISE EXCEPTION 'evaluation run % is not running', p_run_id\n"
        "            USING ERRCODE = 'check_violation';\n"
        "    END IF;\n"
        f"    WITH inserted AS (\n"
        f"        INSERT INTO {s['evaluation']}.evaluation_scores\n"
        "            (evaluation_run_id, execution_result_id, score, scoring_version)\n"
        "        SELECT run.id, run.execution_result_id, (e ->> 'score')::double precision,\n"
        "               e ->> 'scoring_version'\n"
        "        FROM jsonb_array_elements(p_scores) AS e\n"
        "        RETURNING 1\n"
        "    )\n"
        "    SELECT count(*) INTO written FROM inserted;\n"
        f"    UPDATE {s['evaluation']}.evaluation_runs\n"
        "        SET state = 'complete', completed_at = now() WHERE id = run.id;\n"
        "    RETURN written;\n",
    )

    out["models.activate_model_version"] = (
        "    new_id uuid;\n",
        f"    PERFORM 1 FROM {s['models']}.model_versions WHERE id = p_model_version_id;\n"
        "    IF NOT FOUND THEN\n"
        "        RAISE EXCEPTION 'unknown model version %', p_model_version_id\n"
        "            USING ERRCODE = 'foreign_key_violation';\n"
        "    END IF;\n"
        f"    PERFORM 1 FROM {s['models']}.model_activations\n"
        "        WHERE model_version_id = p_model_version_id AND deactivated_at IS NULL;\n"
        "    IF FOUND THEN\n"
        f"        SELECT id INTO new_id FROM {s['models']}.model_activations\n"
        "            WHERE model_version_id = p_model_version_id AND deactivated_at IS NULL;\n"
        "        RETURN new_id;\n"
        "    END IF;\n"
        f"    UPDATE {s['models']}.model_activations SET deactivated_at = now()\n"
        "        WHERE deactivated_at IS NULL;\n"
        f"    INSERT INTO {s['models']}.model_activations\n"
        "        (model_version_id, release_id, activated_at, activated_by_role, reason)\n"
        "    VALUES (p_model_version_id, p_release_id, now(), current_user, p_reason)\n"
        "    RETURNING id INTO new_id;\n"
        "    RETURN new_id;\n",
    )

    out["runtime.register_service_instance"] = (
        "    existing record;\n    new_id uuid;\n",
        f"    SELECT * INTO existing FROM {s['runtime']}.service_instances\n"
        "        WHERE instance_key = p_instance_key;\n"
        "    IF FOUND THEN\n"
        "        IF existing.service_name <> p_service_name THEN\n"
        "            RAISE EXCEPTION 'instance_key % belongs to service %',\n"
        "                p_instance_key, existing.service_name\n"
        "                USING ERRCODE = 'unique_violation';\n"
        "        END IF;\n"
        "        RETURN existing.id;\n"
        "    END IF;\n"
        f"    INSERT INTO {s['runtime']}.service_instances\n"
        "        (instance_key, service_name, release_id, state, last_heartbeat_at)\n"
        "    VALUES (p_instance_key, p_service_name, p_release_id, 'starting', now())\n"
        "    RETURNING id INTO new_id;\n"
        "    RETURN new_id;\n",
    )

    out["runtime.acquire_lease"] = (
        "    lease record;\n    token bigint;\n",
        "    IF p_ttl_seconds IS NULL OR p_ttl_seconds <= 0 THEN\n"
        "        RAISE EXCEPTION 'ttl_seconds must be positive, got %', p_ttl_seconds\n"
        "            USING ERRCODE = 'invalid_parameter_value';\n"
        "    END IF;\n"
        f"    SELECT * INTO lease FROM {s['runtime']}.leases\n"
        "        WHERE lease_key = p_lease_key FOR UPDATE;\n"
        "    IF NOT FOUND THEN\n"
        f"        INSERT INTO {s['runtime']}.leases\n"
        "            (lease_key, holder, acquired_at, expires_at, fence_token)\n"
        "        VALUES (p_lease_key, p_holder, now(),\n"
        "                now() + make_interval(secs => p_ttl_seconds), 1)\n"
        "        RETURNING fence_token INTO token;\n"
        "        RETURN token;\n"
        "    END IF;\n"
        "    IF lease.holder <> p_holder AND lease.expires_at > now() THEN\n"
        "        RAISE EXCEPTION 'lease % is held by % until %',\n"
        "            p_lease_key, lease.holder, lease.expires_at\n"
        "            USING ERRCODE = 'check_violation';\n"
        "    END IF;\n"
        f"    UPDATE {s['runtime']}.leases\n"
        "        SET holder = p_holder, fence_token = lease.fence_token + 1,\n"
        "            acquired_at = CASE WHEN lease.holder <> p_holder THEN now()\n"
        "                               ELSE lease.acquired_at END,\n"
        "            expires_at = CASE WHEN lease.holder <> p_holder\n"
        "                              THEN now() + make_interval(secs => p_ttl_seconds)\n"
        "                              ELSE greatest(lease.expires_at,\n"
        "                                   now() + make_interval(secs => p_ttl_seconds)) END\n"
        "        WHERE lease_key = p_lease_key\n"
        "        RETURNING fence_token INTO token;\n"
        "    RETURN token;\n",
    )

    out["runtime.renew_lease"] = (
        "    lease record;\n    token bigint;\n",
        f"    SELECT * INTO lease FROM {s['runtime']}.leases\n"
        "        WHERE lease_key = p_lease_key FOR UPDATE;\n"
        "    IF NOT FOUND OR lease.holder <> p_holder THEN\n"
        "        RAISE EXCEPTION 'lease % is not held by %', p_lease_key, p_holder\n"
        "            USING ERRCODE = 'check_violation';\n"
        "    END IF;\n"
        "    IF lease.fence_token <> p_fence_token THEN\n"
        "        RAISE EXCEPTION 'stale fence token % for lease %', p_fence_token, p_lease_key\n"
        "            USING ERRCODE = 'check_violation';\n"
        "    END IF;\n"
        f"    UPDATE {s['runtime']}.leases\n"
        "        SET fence_token = lease.fence_token + 1,\n"
        "            expires_at = greatest(lease.expires_at,\n"
        "                                  now() + make_interval(secs => p_ttl_seconds))\n"
        "        WHERE lease_key = p_lease_key\n"
        "        RETURNING fence_token INTO token;\n"
        "    RETURN token;\n",
    )

    out["runtime.release_lease"] = (
        "    lease record;\n",
        f"    SELECT * INTO lease FROM {s['runtime']}.leases\n"
        "        WHERE lease_key = p_lease_key FOR UPDATE;\n"
        "    IF NOT FOUND OR lease.holder <> p_holder THEN\n"
        "        RAISE EXCEPTION 'lease % is not held by %', p_lease_key, p_holder\n"
        "            USING ERRCODE = 'check_violation';\n"
        "    END IF;\n"
        "    IF lease.fence_token <> p_fence_token THEN\n"
        "        RAISE EXCEPTION 'stale fence token % for lease %', p_fence_token, p_lease_key\n"
        "            USING ERRCODE = 'check_violation';\n"
        "    END IF;\n"
        f"    UPDATE {s['runtime']}.leases\n"
        "        SET fence_token = lease.fence_token + 1, expires_at = now()\n"
        "        WHERE lease_key = p_lease_key;\n"
        "    RETURN;\n",
    )

    out["runtime.set_active_selection"] = (
        "    new_id uuid;\n",
        f"    PERFORM 1 FROM {s['catalog']}.releases WHERE id = p_release_id;\n"
        "    IF NOT FOUND THEN\n"
        "        RAISE EXCEPTION 'unknown release %', p_release_id\n"
        "            USING ERRCODE = 'foreign_key_violation';\n"
        "    END IF;\n"
        f"    SELECT id INTO new_id FROM {s['runtime']}.active_selections\n"
        "        WHERE effective_to IS NULL AND release_id = p_release_id\n"
        "          AND model_version_id = p_model_version_id\n"
        "          AND candidate_config_id = p_candidate_config_id;\n"
        "    IF FOUND THEN\n"
        "        RETURN new_id;\n"
        "    END IF;\n"
        f"    UPDATE {s['runtime']}.active_selections SET effective_to = now()\n"
        "        WHERE effective_to IS NULL;\n"
        f"    INSERT INTO {s['runtime']}.active_selections\n"
        "        (release_id, model_version_id, candidate_config_id, effective_from,\n"
        "         selected_by_role)\n"
        "    VALUES (p_release_id, p_model_version_id, p_candidate_config_id, now(), current_user)\n"
        "    RETURNING id INTO new_id;\n"
        "    RETURN new_id;\n",
    )
    return out


def _physical_signature(contracts: Contracts, function: dict[str, Any]) -> str:
    """The declared signature with its schema translated into the shadow namespace."""
    schema, rest = function["signature"].split(".", 1)
    return f"{contracts.physical_schema(schema)}.{rest}"


def _physical_name(contracts: Contracts, name: str) -> str:
    schema, bare = name.split(".", 1)
    return f"{contracts.physical_schema(schema)}.{bare}"


def function_ddl(contracts: Contracts) -> list[str]:
    """CREATE FUNCTION for all 34 declared functions, plus the PUBLIC revoke each one needs."""
    bodies: dict[str, tuple[str, str]] = {}
    bodies.update(_generic_bodies())
    bodies.update(_state_machine_bodies(contracts))
    bodies.update(_api_bodies(contracts))
    bodies.update(_artifact_api_bodies(contracts))
    bodies["catalog.register_backup_set"] = _register_backup_set_body(contracts)
    bodies.pop("catalog.__removed_get_or_verify_artifact", None)
    bodies["catalog.enforce_backup_set_shape"] = _backup_set_gate_body(contracts)

    statements: list[str] = []
    for function in contracts.functions():
        name = function["name"]
        if name not in bodies:
            raise ValueError(f"no body for declared function {name}")
        declare, body = bodies[name]
        signature = _physical_signature(contracts, function)
        security = " SECURITY DEFINER" if function["security_mode"] == "DEFINER" else ""
        declare_block = f"DECLARE\n{declare}" if declare else ""
        statements.append(
            f"CREATE FUNCTION {signature}\n"
            f"RETURNS {function['return_type']}\n"
            f"LANGUAGE {function['language']}\n"
            f"{function['volatility']}{security}\n"
            f"SET search_path = {SEARCH_PATH}\n"
            f"AS $minos$\n{declare_block}BEGIN\n{body}END\n$minos$;"
        )
    return statements


def trigger_ddl(contracts: Contracts) -> list[str]:
    """CREATE TRIGGER for all 89 declared triggers, on the translated tables and functions."""
    statements: list[str] = []
    for trigger in contracts.triggers():
        table = contracts.physical_table(trigger["table"])
        function = _physical_name(contracts, trigger["function"])
        arguments = ", ".join(_lit(argument) for argument in trigger.get("arguments", []))
        constraint = "CONSTRAINT " if trigger.get("constraint_trigger") else ""
        deferrability = f" {trigger['deferrability']}" if trigger.get("deferrability") else ""
        statements.append(
            f"CREATE {constraint}TRIGGER {trigger['name']}\n"
            f"    {trigger['timing']} {trigger['event']} ON {table}{deferrability}\n"
            f"    FOR EACH {trigger['for_each']}\n"
            f"    EXECUTE FUNCTION {function}({arguments});"
        )
    return statements


# --------------------------------------------------------------------------- #
# grants
# --------------------------------------------------------------------------- #
TABLE_PRIVILEGES = ("SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE", "REFERENCES", "TRIGGER")


def grant_ddl(contracts: Contracts) -> list[str]:
    """Exactly the D2 physical ACL: only objects this migration itself created."""
    acl = contracts.api["d2_physical_acl"]
    statements: list[str] = []
    signatures = {
        _physical_name(contracts, function["name"]): _physical_signature(contracts, function)
        for function in contracts.functions()
    }

    # PostgreSQL opens new schemas and functions to PUBLIC by default; close them first.
    for schema in acl["objects"]["schemas"]:
        statements.append(f"REVOKE ALL ON SCHEMA {schema} FROM PUBLIC;")
        statements.append(
            f"ALTER DEFAULT PRIVILEGES FOR ROLE {DEFINER} IN SCHEMA {schema} "
            "REVOKE ALL ON TABLES FROM PUBLIC;"
        )
        statements.append(
            f"ALTER DEFAULT PRIVILEGES FOR ROLE {DEFINER} IN SCHEMA {schema} "
            "REVOKE ALL ON SEQUENCES FROM PUBLIC;"
        )
        statements.append(
            f"ALTER DEFAULT PRIVILEGES FOR ROLE {DEFINER} IN SCHEMA {schema} "
            "REVOKE ALL ON FUNCTIONS FROM PUBLIC;"
        )
    for name in acl["objects"]["functions"]:
        statements.append(f"REVOKE ALL ON FUNCTION {signatures[name]} FROM PUBLIC;")
    for table in acl["objects"]["tables"]:
        statements.append(f"REVOKE ALL ON TABLE {table} FROM PUBLIC;")

    for record in acl["records"]:
        principal = record["principal"]
        if principal == "PUBLIC":
            continue
        granted = [key for key in TABLE_PRIVILEGES if record["privileges"].get(key)]
        if record["object_type"] == "schema":
            if record["privileges"].get("USAGE"):
                statements.append(f"GRANT USAGE ON SCHEMA {record['object']} TO {principal};")
            if principal in acl["create_privilege"]["granted_to"]:
                statements.append(f"GRANT CREATE ON SCHEMA {record['object']} TO {principal};")
        elif record["object_type"] == "table":
            if granted:
                statements.append(
                    f"GRANT {', '.join(granted)} ON TABLE {record['object']} TO {principal};"
                )
        elif record["privileges"].get("EXECUTE"):
            statements.append(
                f"GRANT EXECUTE ON FUNCTION {signatures[record['object']]} TO {principal};"
            )
    return statements


# --------------------------------------------------------------------------- #
# role preflight and elevation
# --------------------------------------------------------------------------- #
def preflight_sql(contracts: Contracts) -> list[str]:
    """The role preflight: every check runs BEFORE the first CREATE, ALTER or GRANT."""
    provisioning = contracts.api["role_provisioning"]
    attributes = provisioning["role_attribute_contract"]
    required = sorted(provisioning["required_roles"])
    login_required = sorted(r for r in required if attributes[r]["login"])
    nologin_required = sorted(r for r in required if not attributes[r]["login"])
    checks = (
        "DO $preflight$\n"
        "DECLARE\n"
        "    acting_session text := session_user;\n"
        "    acting_current text := current_user;\n"
        "    missing text;\n"
        "    offender text;\n"
        "BEGIN\n"
        "    -- 1. the ORIGINAL migration identity, recorded before any elevation\n"
        "    RAISE NOTICE 'dbv2 preflight: session_user=% current_user=%',\n"
        "        acting_session, acting_current;\n"
        "    -- 2. every required role exists\n"
        "    SELECT r INTO missing FROM unnest(ARRAY[\n"
        + ",\n".join(f"        {_lit(role)}" for role in required)
        + "\n    ]) AS r\n"
        "        WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = r) LIMIT 1;\n"
        "    IF missing IS NOT NULL THEN\n"
        "        RAISE EXCEPTION 'required role % does not exist; 0009 creates no cluster role',\n"
        "            missing USING ERRCODE = 'invalid_authorization_specification';\n"
        "    END IF;\n"
        "    -- 3. the declared LOGIN/NOLOGIN configuration\n"
        "    SELECT rolname INTO offender FROM pg_roles\n"
        "        WHERE rolname = ANY(ARRAY[\n"
        + ",\n".join(f"            {_lit(role)}" for role in login_required)
        + "\n        ]) AND NOT rolcanlogin LIMIT 1;\n"
        "    IF offender IS NOT NULL THEN\n"
        "        RAISE EXCEPTION 'role % must be LOGIN', offender\n"
        "            USING ERRCODE = 'invalid_authorization_specification';\n"
        "    END IF;\n"
        "    SELECT rolname INTO offender FROM pg_roles\n"
        "        WHERE rolname = ANY(ARRAY[\n"
        + ",\n".join(f"            {_lit(role)}" for role in nologin_required)
        + "\n        ]) AND rolcanlogin LIMIT 1;\n"
        "    IF offender IS NOT NULL THEN\n"
        "        RAISE EXCEPTION 'role % must be NOLOGIN', offender\n"
        "            USING ERRCODE = 'invalid_authorization_specification';\n"
        "    END IF;\n"
        "    -- 4. the migration identity is a member of the NOLOGIN definer principal\n"
        f"    IF NOT pg_has_role(acting_session, {_lit(DEFINER)}, 'MEMBER') THEN\n"
        f"        RAISE EXCEPTION 'migration identity % is not a member of {DEFINER}',\n"
        "            acting_session USING ERRCODE = 'invalid_authorization_specification';\n"
        "    END IF;\n"
        "    -- 5. no required role carries a cluster-wide privilege\n"
        "    SELECT rolname INTO offender FROM pg_roles\n"
        "        WHERE rolname = ANY(ARRAY[\n"
        + ",\n".join(f"            {_lit(role)}" for role in required)
        + "\n        ]) AND (rolsuper OR rolcreaterole OR rolcreatedb) LIMIT 1;\n"
        "    IF offender IS NOT NULL THEN\n"
        "        RAISE EXCEPTION 'role % must not hold SUPERUSER, CREATEROLE or CREATEDB',\n"
        "            offender USING ERRCODE = 'invalid_authorization_specification';\n"
        "    END IF;\n"
        "    -- 6. the definer principal may create schemas in THIS database\n"
        f"    IF NOT has_database_privilege({_lit(DEFINER)}, current_database(), 'CREATE') THEN\n"
        f"        RAISE EXCEPTION '{DEFINER} may not create schemas in %; provision the database "
        "grant before migrating', current_database()\n"
        "            USING ERRCODE = 'insufficient_privilege';\n"
        "    END IF;\n"
        "    -- the shared Alembic table is verified, never altered\n"
        "    PERFORM 1 FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace\n"
        "        WHERE n.nspname = 'public' AND c.relname = 'alembic_version'\n"
        "          AND c.relkind = 'r';\n"
        "    IF NOT FOUND THEN\n"
        "        RAISE EXCEPTION 'public.alembic_version is missing'\n"
        "            USING ERRCODE = 'undefined_table';\n"
        "    END IF;\n"
        "END\n"
        "$preflight$;"
    )
    # 6. elevate only after every check passed, transaction-scoped, never SET ROLE
    elevate = f"SET LOCAL ROLE {DEFINER};"
    # 7. re-check the identity the rest of the migration runs as
    recheck = (
        "DO $elevated$\n"
        "BEGIN\n"
        f"    IF current_user <> {_lit(DEFINER)} THEN\n"
        f"        RAISE EXCEPTION 'elevation failed: current_user is %, expected {DEFINER}',\n"
        "            current_user USING ERRCODE = 'invalid_authorization_specification';\n"
        "    END IF;\n"
        "END\n"
        "$elevated$;"
    )
    return [checks, elevate, recheck]


def schema_ddl(contracts: Contracts) -> list[str]:
    return [
        f"CREATE SCHEMA {contracts.physical_schema(canonical)} AUTHORIZATION {DEFINER};"
        for canonical in sorted(contracts.shadow)
    ]


def downgrade_sql(contracts: Contracts) -> list[str]:
    """Drop exactly the seven shadow schemas. No cluster role and no V1 object is touched."""
    return [
        f"DROP SCHEMA IF EXISTS {contracts.physical_schema(canonical)} CASCADE;"
        for canonical in sorted(contracts.shadow, reverse=True)
    ]


#: the last statement of both directions. Alembic updates public.alembic_version inside this same
#: transaction, AFTER the migration body, and the definer principal deliberately holds no privilege
#: on that shared table - D2 grants none. De-elevating returns the connection to the original
#: migration identity for that update. It is not the safety boundary: SET LOCAL is, and an abort
#: between the elevation and this statement still leaves no elevated session behind.
DEELEVATE = "SET LOCAL ROLE NONE;"


def upgrade_statements(contracts: Contracts) -> list[str]:
    return [
        *preflight_sql(contracts),
        *schema_ddl(contracts),
        *(
            statement
            for ident in contracts.shadow_tables()
            for statement in table_ddl(contracts, ident)
        ),
        *function_ddl(contracts),
        *trigger_ddl(contracts),
        *grant_ddl(contracts),
        DEELEVATE,
    ]


def downgrade_statements(contracts: Contracts) -> list[str]:
    return [*preflight_sql(contracts), *downgrade_sql(contracts), DEELEVATE]


# --------------------------------------------------------------------------- #
# rendering
# --------------------------------------------------------------------------- #
HEADER = '''"""DB-V2 D2 — the complete empty DB-V2 shadow schema (additive to 0008).

Creates the seven ``dbv2_*`` shadow schemas and every object the frozen DB-V2 contracts declare:
{tables} tables, {functions} functions, {triggers} triggers and the {acl}-record D2 physical ACL. No V1
object is created, renamed, altered, dropped or written, and no data is moved: the shadow schema
is created EMPTY.

The migration is self-contained. It reads no report, calls no generator and touches no filesystem
or network; the SQL below is the complete executable result of
``scripts/gen_dbv2_migration.py``, which is a pure function of the three committed contracts:

    logical  {logical}
    physical {physical}
    api      {api}

Role handling. PostgreSQL roles are CLUSTER objects, so this migration creates, alters and drops
none of them. It preflights: it records the original migration identity, requires all nine roles
to exist with their declared LOGIN/NOLOGIN configuration, requires the migration identity to be a
member of the NOLOGIN definer principal, and requires that no required role holds SUPERUSER,
CREATEROLE or CREATEDB. Every one of those checks runs BEFORE the first CREATE, ALTER or GRANT.
Only then does it elevate with ``SET LOCAL ROLE`` — never ``SET ROLE`` — which the transaction
undoes automatically at commit or abort. No RESET ROLE is issued, and not needing one is the
safety boundary.

Grant scope. Only objects this migration itself creates are granted or revoked. Nothing touches
``public``, ``public.alembic_version``, the database, or any existing V1 object.

Downgrade drops exactly the seven shadow schemas and nothing else. No cluster role is dropped or
altered, and no V1 object is affected.
"""

from __future__ import annotations

from alembic import op

revision: str = "{revision}"
down_revision: str | None = "{down_revision}"
branch_labels = None
depends_on = None
'''


def _statement_literal(statement: str) -> str:
    if '"""' in statement:  # pragma: no cover - a contract change would have to introduce it
        raise ValueError("a generated statement contains a triple quote")
    if statement.endswith('"'):  # pragma: no cover
        raise ValueError("a generated statement ends with a quote")
    return f'    r"""{statement}""",'


def render(contracts: Contracts) -> str:
    upgrade = upgrade_statements(contracts)
    downgrade = downgrade_statements(contracts)
    header = HEADER.format(
        tables=len(contracts.shadow_tables()),
        functions=len(contracts.functions()),
        triggers=len(contracts.triggers()),
        acl=contracts.api["d2_physical_acl"]["counts"]["records"],
        logical=contracts.logical["contract_sha256"],
        physical=contracts.physical["contract_sha256"],
        api=contracts.api["contract_sha256"],
        revision=REVISION,
        down_revision=DOWN_REVISION,
    )
    parts = [header]
    parts.append(
        "\n#: every statement of the forward migration, in execution order.\nUPGRADE = (\n"
    )
    parts.extend(f"{_statement_literal(statement)}\n" for statement in upgrade)
    parts.append(")\n")
    parts.append(
        "\n#: every statement of the reverse migration, in execution order.\nDOWNGRADE = (\n"
    )
    parts.extend(f"{_statement_literal(statement)}\n" for statement in downgrade)
    parts.append(")\n")
    parts.append(
        "\n\ndef upgrade() -> None:\n"
        "    for statement in UPGRADE:\n"
        "        op.execute(statement)\n"
        "\n\ndef downgrade() -> None:\n"
        "    for statement in DOWNGRADE:\n"
        "        op.execute(statement)\n"
    )
    return "".join(parts)


CONTRACT_HEADER = '''"""The frozen DB-V2 D2 migration contract.

Everything migration ``0009_dbv2_shadow_schema`` is required to create, as data, so a test can
compare the live schema against it without re-reading the design reports. Generated by
``scripts/gen_dbv2_migration.py``; regenerate and re-commit it whenever the contracts change, and
``scripts/gen_dbv2_migration.py verify`` will refuse any drift between the three.

Nothing here executes SQL or opens a connection.
"""

from __future__ import annotations

from typing import Final

'''


#: the project's configured formatter width; a container that fits is emitted on one line, which
#: is exactly what ``ruff format`` would do, so the generated file is already formatted.
LINE_LENGTH = 100


def _py(value: Any, indent: int = 0, used: int = 0) -> str:
    """Deterministic, already-formatted Python literal. Mappings are emitted in sorted key order.

    ``used`` is the number of characters already on the line before this value (the ``"key": ``
    prefix, or the ``NAME: Final = `` assignment), so the fits-on-one-line test matches what the
    formatter would decide.
    """
    pad = " " * indent
    if isinstance(value, str):
        # json.dumps gives the double-quoted, minimally escaped form the formatter prefers
        return json.dumps(value)
    if not isinstance(value, (dict, list, tuple)):
        return repr(value)
    if not value:
        return "{}" if isinstance(value, dict) else "()"
    if isinstance(value, dict):
        pairs = [(_py(k), _py(v, indent + 4, len(_py(k)) + 2)) for k, v in sorted(value.items())]
        one_line = "{" + ", ".join(f"{k}: {v}" for k, v in pairs) + "}"
        expanded = "{\n" + "".join(f"{pad}    {k}: {v},\n" for k, v in pairs) + pad + "}"
    else:
        parts = [_py(item, indent + 4) for item in value]
        trailing = "," if len(parts) == 1 else ""
        one_line = "(" + ", ".join(parts) + trailing + ")"
        expanded = "(\n" + "".join(f"{pad}    {part},\n" for part in parts) + pad + ")"
    if "\n" not in one_line and indent + used + len(one_line) <= LINE_LENGTH:
        return one_line
    return expanded


def render_contract(contracts: Contracts, migration_source: str) -> str:
    """The frozen migration contract module, as a pure function of the contracts and the file."""
    inventory = load_strict(INVENTORY_PATH)
    tables = contracts.shadow_tables()
    columns = {}
    constraints = {}
    for ident in tables:
        table = contracts.tables[ident]
        physical = contracts.physical_table(ident)
        columns[physical] = tuple(
            (c["name"], c["type"], bool(c["nullable"]), c.get("default")) for c in table["columns"]
        )
        constraints[physical] = {
            "checks": tuple(sorted(c["name"] for c in table.get("check_constraints", []))),
            "foreign_keys": tuple(
                sorted(
                    (fk["name"], contracts.physical_table(fk["references"]))
                    for fk in table.get("foreign_keys", [])
                )
            ),
            "indexes": tuple(sorted(i["name"] for i in table.get("indexes", []))),
            "primary_key": table["primary_key"]["name"],
            "unique": tuple(sorted(u["name"] for u in table.get("unique_constraints", []))),
        }
    acl = contracts.api["d2_physical_acl"]
    acl_digest = hashlib.sha256(
        json.dumps(acl["records"], sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    payload = {
        "REVISION": REVISION,
        "DOWN_REVISION": DOWN_REVISION,
        "MIGRATION_SHA256": hashlib.sha256(migration_source.encode("utf-8")).hexdigest(),
        "LOGICAL_CONTRACT_SHA256": contracts.logical["contract_sha256"],
        "PHYSICAL_CONTRACT_SHA256": contracts.physical["contract_sha256"],
        "DATABASE_API_SHA256": contracts.api["contract_sha256"],
        "SHADOW_SCHEMAS": tuple(sorted(contracts.shadow.values())),
        "SHADOW_TABLES": tuple(sorted(contracts.physical_table(i) for i in tables)),
        "TABLE_COLUMNS": columns,
        "TABLE_CONSTRAINTS": constraints,
        "FUNCTIONS": tuple(
            sorted(_physical_signature(contracts, f) for f in contracts.functions())
        ),
        "SECURITY_DEFINER_FUNCTIONS": tuple(
            sorted(
                _physical_signature(contracts, f)
                for f in contracts.functions()
                if f["security_mode"] == "DEFINER"
            )
        ),
        "TRIGGERS": tuple(
            sorted(
                (
                    t["name"],
                    contracts.physical_table(t["table"]),
                    _physical_name(contracts, t["function"]),
                    t["timing"],
                    t["event"],
                )
                for t in contracts.triggers()
            )
        ),
        "D2_ACL_SHA256": acl_digest,
        "D2_ACL_RECORDS": len(acl["records"]),
        "D2_ACL_OBJECTS": acl["counts"]["objects"],
        "DOWNGRADE_DROPS": tuple(
            s.removeprefix("DROP SCHEMA IF EXISTS ").removesuffix(" CASCADE;")
            for s in downgrade_sql(contracts)
        ),
        "FROZEN_MIGRATION_SHA256": {m["revision"]: m["sha256"] for m in inventory["migrations"]},
        "SEARCH_PATH": SEARCH_PATH,
        "DEFINER_PRINCIPAL": DEFINER,
        "REQUIRED_ROLES": tuple(sorted(contracts.api["role_provisioning"]["required_roles"])),
        "SHARED_TABLE": SHARED_ALEMBIC_TABLE,
    }
    lines = [CONTRACT_HEADER]
    for name in payload:
        lines.append(f"{name}: Final = {_py(payload[name], 0, len(name) + 10)}\n\n")
    body = "".join(lines).rstrip("\n") + "\n"
    digest = hashlib.sha256(body.encode("utf-8")).hexdigest()
    return (
        body
        + f'''
#: sha256 of every line above this one, so the contract cannot be edited without detection.
CONTRACT_SHA256: Final = "{digest}"
'''
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="generate or verify DB-V2 migration 0009")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("generate", "verify"):
        command = sub.add_parser(name)
        command.add_argument("--reports", type=Path, default=REPORTS)
        command.add_argument("--migration", type=Path, default=MIGRATION_PATH)
        command.add_argument("--contract", type=Path, default=CONTRACT_PATH)
    args = parser.parse_args(argv)

    contracts = Contracts(args.reports)
    generated = render(contracts)

    contract_source = render_contract(contracts, generated)

    if args.command == "generate":
        args.migration.parent.mkdir(parents=True, exist_ok=True)
        args.migration.write_text(generated, encoding="utf-8")
        args.contract.write_text(contract_source, encoding="utf-8")
        digest = hashlib.sha256(generated.encode("utf-8")).hexdigest()
        print(f"{args.migration}: {len(generated)} bytes, sha256 {digest}")
        print(f"{args.contract}: {len(contract_source)} bytes")
        return 0

    problems = 0
    for path, expected, label in (
        (args.migration, generated, "migration"),
        (args.contract, contract_source, "migration contract"),
    ):
        if not path.is_file():
            print(f"FAIL: {path} does not exist", file=sys.stderr)
            problems += 1
            continue
        committed = path.read_text(encoding="utf-8")
        if committed == expected:
            print(
                f"verify: {label} byte-identical, "
                f"sha256 {hashlib.sha256(expected.encode('utf-8')).hexdigest()}"
            )
            continue
        problems += 1
        sys.stderr.writelines(
            difflib.unified_diff(
                committed.splitlines(keepends=True),
                expected.splitlines(keepends=True),
                fromfile=f"committed {label}",
                tofile=f"generated {label}",
                n=1,
            )
        )
        print(f"FAIL: the committed {label} differs from the contracts", file=sys.stderr)
    return 1 if problems else 0


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    raise SystemExit(main())
