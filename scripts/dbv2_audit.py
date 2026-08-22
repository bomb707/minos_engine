"""DB-V2 D1 audit + report validator (design-only; never mutates any database).

Three responsibilities, all read-only:

``inventory``
    Introspect a PostgreSQL database and emit the complete V1 object inventory
    (schemas, tables, columns, constraints, indexes, triggers, functions, sequences, views,
    grants, row-level policies, row counts) plus a static scan of the repository's database and
    filesystem access paths.

``contract-hash``
    Recompute the deterministic canonical hash over a DB-V2 contract document, excluding the
    document's own ``contract_sha256`` field.

``validate``
    Strictly parse the three reports (duplicate JSON keys are an error), recompute the contract
    hash, and cross-reference the inventory, the target contract and the current-to-target
    mapping so no document can drift from the others.

No credential, password or full DSN is ever written to an emitted report: only the database
name, the schema-qualified object identities and aggregate counts are recorded.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any, cast

REPO_ROOT = Path(__file__).resolve().parents[1]

#: domain-separated prefix for the DB-V2 contract hash.
CONTRACT_HASH_DOMAIN = b"minos:db-v2-contract:v1\n"
#: the field the hash is written into; it is excluded from its own preimage.
CONTRACT_HASH_FIELD = "contract_sha256"

#: the frozen temporary physical schema namespace (DB-V2 D1.1). Deployment names, never the
#: final application contract.
SHADOW_SCHEMA_PREFIX = "dbv2_"
RETIRED_SCHEMA_PREFIX = "v1_retired_"
SHARED_ALEMBIC_TABLE = "public.alembic_version"

#: the exact operational preparation path. No stamp, no skipped revision, no permanent multi-head.
#: the canonical (post-cutover) schemas. Nothing beginning with one of these may ever be a
#: retirement target: after cutover those names are the live V2 system.
CANONICAL_SCHEMAS = (
    "catalog",
    "profiling",
    "experiments",
    "evaluation",
    "models",
    "runtime",
    "audit",
)

EXPECTED_REVISION_PATH = (
    "0005_l2e_feature_view",
    "0006_l2f_experiment_plan",
    "0007_l2f_job_claiming",
    "0008_l2f_execution_results",
    "0009_dbv2_shadow_schema",
)

#: the externally provisioned recovery root (DB-V2 D1.3). It has no default: a default would
#: silently select the source checkout, which is the defect D1.3 exists to remove.
RECOVERY_ROOT_ENV = "MINOS_DB_RECOVERY_ROOT"

#: the exact media types the three recovery artifacts must carry.
RECOVERY_MEDIA_TYPES = {
    "recovery_manifest": "application/vnd.minos.db-recovery-manifest+json",
    "database_backup": "application/vnd.postgresql.dump",
    "artifact_snapshot_manifest": "application/vnd.minos.artifact-snapshot+json",
}

#: every immutable field the R1 manifest's canonical bytes must carry.
REQUIRED_R1_FIELDS = (
    "schema_version",
    "recovery_set_id",
    "database_name",
    "source_alembic_revision",
    "database_backup_kind",
    "database_backup_sha256",
    "database_backup_size_bytes",
    "quiesce_started_at",
    "quiesce_ended_at",
    "wal_start_lsn",
    "wal_end_lsn",
    "artifact_snapshot_manifest_sha256",
    "artifact_snapshot_sha256",
    "artifact_count",
    "artifact_total_bytes",
    "created_at",
    "postgresql_version",
    "backup_tool_version",
    "artifact_verification_tool_version",
)

#: top-level repository directories. A recovery path naming one of these is inside the checkout.
REPO_RELATIVE_RE = re.compile(r"(?<![\w<./-])(reports|docs|src|scripts|tests|migrations)/")

#: a paragraph that says post-cutover rollback drops the shadow schema is a defect UNLESS the
#: paragraph explicitly records it as withdrawn or forbidden.
DROP_SHADOW_RE = re.compile(r"drop(?:s|ping|ped)?\s+(?:the\s+)?shadow", re.IGNORECASE)
WITHDRAWAL_MARKERS = ("withdrawn", "not dropped", "never drop", "must not", "would delete")

#: markdown link targets that are not repository paths.
EXTERNAL_LINK_RE = re.compile(r"^(https?:|mailto:|#)")

# --------------------------------------------------------------------------- #
# DB-V2 D1.4: the enforceable contract
# --------------------------------------------------------------------------- #
#: an artifact is either an engine payload or one of the three artifacts a recovery set is MADE of.
BACKUP_SCOPES = frozenset({"operational", "recovery"})

#: the EXACT R1 artifact-snapshot predicate. Unqualified "all active artifacts" is not a fixed
#: point: R2 makes three more artifacts active, so the digest would change after every R2.
SNAPSHOT_WHERE = "lifecycle_state = 'active' AND backup_scope = 'operational'"
SNAPSHOT_SORT = ("content_sha256", "size_bytes", "artifact_kind")
SNAPSHOT_DIGEST_DOMAIN = "minos:db-v2-artifact-snapshot:v1\n"

#: the five columns that are present together or absent together.
SNAPSHOT_SHAPE_COLUMNS = (
    "artifact_snapshot_manifest_artifact_id",
    "artifact_snapshot_manifest_sha256",
    "artifact_snapshot_sha256",
    "artifact_snapshot_manifest_media_type",
    "artifact_count",
    "artifact_total_bytes",
)

#: the RAW manifest digest is what the composite foreign key binds; the domain-separated
#: scientific identity deliberately participates in no foreign key at all.
SNAPSHOT_RAW_DIGEST_COLUMN = "artifact_snapshot_manifest_sha256"
SNAPSHOT_SCIENTIFIC_COLUMN = "artifact_snapshot_sha256"
COMPLETENESS_STATES = frozenset({"complete", "database_only"})

#: the pseudo-state a nullable state column starts in.
NULL_STATE = "(null)"

#: the cross-table gate. A CHECK cannot reference another table, so completeness is enforced here.
BACKUP_SET_GATE = "catalog.enforce_backup_set_shape"
GATE_REQUIRED_CHECKS = (
    "a referenced artifact does not exist",
    "does not bind one artifact",
    "verification_state = 'verified'",
    "lifecycle_state = 'active'",
    "backup_scope = 'recovery'",
    "is not stored in its declared storage mode",
    "declares the wrong schema_version",
    "inline manifest bytes do not recompute to their raw digest",
    "inline manifest byte size does not match the stored payload",
    "snapshot manifest bytes do not recompute to artifact_snapshot_sha256",
    "the snapshot was not taken with the frozen predicate",
    "the snapshot declares the wrong schema_version",
    "noncanonical field inventory, type or value",
    "the snapshot repeats entries",
    "not in the frozen ascending order",
    "the artifact-catalog bootstrap (B0) has not run",
    "do not resolve to an active operational artifact",
    "are absent from the snapshot",
    "snapshot entry count <> artifact_count",
    "snapshot entry total size <> artifact_total_bytes",
    "unverified, absent or ambiguously primary",
    "snapshotted inline artifacts do not recompute",
    "recovery artifacts appear in the snapshot",
    "recovery manifest bytes do not recompute to recovery_manifest_sha256",
    "an R1 field does not equal its mapped column",
    "the external database dump has no artifact_locations row in state 'present'",
    "conflicting recovery_set_id, backup_key or recovery_manifest_sha256",
    "completeness changed",
)

#: every function pins its search_path: an unpinned SECURITY DEFINER function is a privilege bug.
SAFE_SEARCH_PATH = "pg_catalog"
FUNCTION_REQUIRED_FIELDS = (
    "accepted_source_states",
    "concurrency",
    "configured_search_path",
    "cutover_recreation_required",
    "downgrade_behavior",
    "executable_roles",
    "idempotency",
    "language",
    "resulting_states",
    "return_type",
    "revoked_roles",
    "rows_locked",
    "security_mode",
    "signature",
    "sqlstates",
    "tables_mutated",
    "tables_read",
    "volatility",
)

GENERIC_IMMUTABILITY_FUNCTION = "audit.reject_immutable_column_update"
GENERIC_NO_UPDATE_FUNCTION = "audit.reject_update"
IMMUTABILITY_FUNCTIONS = frozenset(
    {
        GENERIC_IMMUTABILITY_FUNCTION,
        GENERIC_NO_UPDATE_FUNCTION,
        "catalog.enforce_backup_set_immutability",
    }
)

#: PostgreSQL roles are cluster objects; 0009 preflights them and never creates them.
DEFINER_PRINCIPAL = "minos_owner"
MIGRATION_ROLE = "minos_migrate"
RUNTIME_ROLES = (
    "minos_planner",
    "minos_enqueue",
    "minos_runner",
    "minos_verifier",
    "minos_trainer",
    "minos_evaluator",
    "minos_live",
)
REQUIRED_ROLES = (MIGRATION_ROLE, DEFINER_PRINCIPAL, *RUNTIME_ROLES)
ACL_PRIVILEGE_KEYS = (
    "USAGE",
    "SELECT",
    "INSERT",
    "UPDATE",
    "DELETE",
    "TRUNCATE",
    "REFERENCES",
    "TRIGGER",
    "EXECUTE",
)
DDL_TABLE_PRIVILEGES = ("TRUNCATE", "REFERENCES", "TRIGGER")
TRUTH_BINDINGS = "evaluation.truth_bindings"

#: R1 -> S1 -> B0 -> R2 -> B1. B0 and B1 are D3; D2.2 freezes the order only.
RECOVERY_SEQUENCE = ("R1", "S1", "B0", "R2", "B1")

#: the migration elevates transaction-scoped, and only after every preflight check has passed.
ELEVATION_STATEMENT = "SET LOCAL ROLE minos_owner"
PREFLIGHT_CHECKS_BEFORE_ELEVATION = (
    "record session_user and current_user",
    "verify every required role exists in pg_roles",
    "verify every required role has its declared LOGIN/NOLOGIN",
    "verify the migration identity has membership",
    "verify no required role has SUPERUSER, CREATEROLE or CREATEDB",
    "verify the definer principal may create schemas in this database",
)
PREFLIGHT_REQUIRED_STEPS = (
    *PREFLIGHT_CHECKS_BEFORE_ELEVATION,
    ELEVATION_STATEMENT,
    "re-check current_user",
    "only then: CREATE SCHEMA",
)

#: D2 applies the physical ACL only: the logical final matrix restricted to the dbv2_* namespace.
D2_ACL_RECORDS = 810
D2_ACL_OBJECTS = 81
D2_FORBIDDEN_ACL_TARGETS = (
    "ON DATABASE minos_engine_db",
    "ON SCHEMA public",
    "public.alembic_version",
)


# --------------------------------------------------------------------------- #
# strict JSON
# --------------------------------------------------------------------------- #
def _no_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    seen: dict[str, Any] = {}
    for key, value in pairs:
        if key in seen:
            raise ValueError(f"duplicate JSON key {key!r}")
        seen[key] = value
    return seen


def load_strict(path: Path) -> Any:
    """Parse JSON, rejecting duplicate keys anywhere in the document."""
    return json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_no_duplicate_keys)


def canonical_bytes(value: Any) -> bytes:
    """Deterministic canonical JSON: sorted keys, tight separators, UTF-8, newline-terminated."""
    return (
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        + b"\n"
    )


def contract_hash(document: dict[str, Any]) -> str:
    """Domain-separated canonical hash over a contract, excluding its own hash field."""
    preimage = {k: v for k, v in document.items() if k != CONTRACT_HASH_FIELD}
    return hashlib.sha256(CONTRACT_HASH_DOMAIN + canonical_bytes(preimage)).hexdigest()


# --------------------------------------------------------------------------- #
# live-schema introspection (read-only)
# --------------------------------------------------------------------------- #
_SYSTEM_SCHEMAS = ("pg_catalog", "information_schema", "pg_toast")

_Q_SCHEMAS = """
SELECT n.nspname AS schema, pg_get_userbyid(n.nspowner) AS owner
FROM pg_namespace n
WHERE n.nspname NOT IN %(sys)s AND n.nspname NOT LIKE 'pg_temp%%'
ORDER BY 1
"""

_Q_TABLES = """
SELECT n.nspname AS schema, c.relname AS name, c.relkind AS kind,
       pg_get_userbyid(c.relowner) AS owner, c.relrowsecurity AS rls
FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE c.relkind IN ('r','v','m','p','S') AND n.nspname NOT IN %(sys)s
ORDER BY 1, 3, 2
"""

_Q_COLUMNS = """
SELECT n.nspname AS schema, c.relname AS table, a.attnum AS position, a.attname AS name,
       format_type(a.atttypid, a.atttypmod) AS type, a.attnotnull AS not_null,
       pg_get_expr(d.adbin, d.adrelid) AS default_expr, a.attidentity AS identity
FROM pg_attribute a
JOIN pg_class c ON c.oid = a.attrelid
JOIN pg_namespace n ON n.oid = c.relnamespace
LEFT JOIN pg_attrdef d ON d.adrelid = a.attrelid AND d.adnum = a.attnum
WHERE a.attnum > 0 AND NOT a.attisdropped
  AND c.relkind IN ('r','v','m','p') AND n.nspname NOT IN %(sys)s
ORDER BY 1, 2, 3
"""

_Q_CONSTRAINTS = """
SELECT n.nspname AS schema, c.relname AS table, con.conname AS name, con.contype AS type,
       pg_get_constraintdef(con.oid) AS definition
FROM pg_constraint con
JOIN pg_class c ON c.oid = con.conrelid
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname NOT IN %(sys)s
ORDER BY 1, 2, 4, 3
"""

_Q_INDEXES = """
SELECT n.nspname AS schema, c.relname AS table, i.relname AS name,
       pg_get_indexdef(x.indexrelid) AS definition,
       x.indisunique AS is_unique, x.indisprimary AS is_primary
FROM pg_index x
JOIN pg_class c ON c.oid = x.indrelid
JOIN pg_class i ON i.oid = x.indexrelid
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname NOT IN %(sys)s
ORDER BY 1, 2, 3
"""

_Q_TRIGGERS = """
SELECT n.nspname AS schema, c.relname AS table, t.tgname AS name,
       pg_get_triggerdef(t.oid) AS definition
FROM pg_trigger t
JOIN pg_class c ON c.oid = t.tgrelid
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE NOT t.tgisinternal AND n.nspname NOT IN %(sys)s
ORDER BY 1, 2, 3
"""

_Q_FUNCTIONS = """
SELECT n.nspname AS schema, p.proname AS name,
       pg_get_function_identity_arguments(p.oid) AS args,
       CASE p.prokind WHEN 'f' THEN 'function' WHEN 'p' THEN 'procedure'
            WHEN 'a' THEN 'aggregate' WHEN 'w' THEN 'window' END AS kind,
       p.prosecdef AS security_definer,
       pg_get_userbyid(p.proowner) AS owner,
       l.lanname AS language,
       pg_get_function_result(p.oid) AS returns
FROM pg_proc p
JOIN pg_namespace n ON n.oid = p.pronamespace
JOIN pg_language l ON l.oid = p.prolang
WHERE n.nspname NOT IN %(sys)s
ORDER BY 1, 2, 3
"""

_Q_SEQUENCES = """
SELECT n.nspname AS schema, c.relname AS name
FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE c.relkind = 'S' AND n.nspname NOT IN %(sys)s
ORDER BY 1, 2
"""

_Q_VIEWS = """
SELECT n.nspname AS schema, c.relname AS name, c.relkind AS kind
FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE c.relkind IN ('v','m') AND n.nspname NOT IN %(sys)s
ORDER BY 1, 2
"""

_Q_TABLE_GRANTS = """
SELECT n.nspname AS schema, c.relname AS name, g.grantee, g.privilege_type
FROM information_schema.role_table_grants g
JOIN pg_class c ON c.relname = g.table_name
JOIN pg_namespace n ON n.oid = c.relnamespace AND n.nspname = g.table_schema
WHERE n.nspname NOT IN %(sys)s AND g.grantee LIKE 'minos%%'
ORDER BY 1, 2, 3, 4
"""

_Q_ROUTINE_GRANTS = """
SELECT n.nspname AS schema, p.proname AS name,
       pg_get_function_identity_arguments(p.oid) AS args,
       a.grantee, a.privilege_type
FROM pg_proc p
JOIN pg_namespace n ON n.oid = p.pronamespace
CROSS JOIN LATERAL aclexplode(COALESCE(p.proacl, acldefault('f', p.proowner))) acl
JOIN LATERAL (SELECT pg_get_userbyid(acl.grantee) AS grantee,
                     acl.privilege_type AS privilege_type) a ON true
WHERE n.nspname NOT IN %(sys)s AND a.grantee LIKE 'minos%%'
ORDER BY 1, 2, 4, 5
"""

_Q_POLICIES = """
SELECT schemaname AS schema, tablename AS table, policyname AS name,
       permissive, roles::text AS roles, cmd, qual, with_check
FROM pg_policies
WHERE schemaname NOT IN %(sys)s
ORDER BY 1, 2, 3
"""

_Q_ROLES = """
SELECT rolname AS name, rolsuper AS superuser, rolcanlogin AS can_login,
       rolinherit AS inherits, rolconnlimit AS connection_limit
FROM pg_roles WHERE rolname LIKE 'minos%%' ORDER BY 1
"""

_Q_MEMBERSHIPS = """
SELECT r.rolname AS member, g.rolname AS granted_role
FROM pg_auth_members m
JOIN pg_roles r ON r.oid = m.member
JOIN pg_roles g ON g.oid = m.roleid
WHERE r.rolname LIKE 'minos%%' OR g.rolname LIKE 'minos%%'
ORDER BY 1, 2
"""


def _rows(conn: Any, sql: str) -> list[dict[str, Any]]:
    from sqlalchemy import text

    rendered = sql.replace("%(sys)s", "(" + ", ".join(f"'{s}'" for s in _SYSTEM_SCHEMAS) + ")")
    rendered = rendered.replace("%%", "%")
    result = conn.execute(text(rendered))
    return [dict(r) for r in result.mappings().all()]


def _row_counts(conn: Any, tables: list[dict[str, Any]]) -> dict[str, int]:
    from sqlalchemy import text

    counts: dict[str, int] = {}
    for entry in tables:
        if entry["kind"] != "r":
            continue
        ident = f'"{entry["schema"]}"."{entry["name"]}"'
        try:
            counts[f"{entry['schema']}.{entry['name']}"] = int(
                conn.execute(text(f"SELECT count(*) FROM {ident}")).scalar_one()  # noqa: S608
            )
        except Exception:  # pragma: no cover - a table we cannot read is reported as unknown
            counts[f"{entry['schema']}.{entry['name']}"] = -1
    return counts


def introspect(database_url: str) -> dict[str, Any]:
    """Read-only introspection. Never issues DDL/DML and never records credentials."""
    from sqlalchemy import create_engine, text

    engine = create_engine(database_url)
    try:
        with engine.connect() as conn:
            database = conn.execute(text("SELECT current_database()")).scalar_one()
            server = conn.execute(text("SHOW server_version")).scalar_one()
            try:
                revision = conn.execute(
                    text("SELECT version_num FROM public.alembic_version")
                ).scalar_one()
            except Exception:  # pragma: no cover - a database without alembic_version
                revision = None
            tables = _rows(conn, _Q_TABLES)
            payload: dict[str, Any] = {
                "database": database,
                "server_version": str(server),
                "alembic_revision": revision,
                "schemas": _rows(conn, _Q_SCHEMAS),
                "tables": tables,
                "columns": _rows(conn, _Q_COLUMNS),
                "constraints": _rows(conn, _Q_CONSTRAINTS),
                "indexes": _rows(conn, _Q_INDEXES),
                "triggers": _rows(conn, _Q_TRIGGERS),
                "functions": _rows(conn, _Q_FUNCTIONS),
                "sequences": _rows(conn, _Q_SEQUENCES),
                "views": _rows(conn, _Q_VIEWS),
                "table_grants": _rows(conn, _Q_TABLE_GRANTS),
                "row_level_policies": _rows(conn, _Q_POLICIES),
                "roles": _rows(conn, _Q_ROLES),
                "role_memberships": _rows(conn, _Q_MEMBERSHIPS),
                "row_counts": _row_counts(conn, tables),
            }
    finally:
        engine.dispose()
    return payload


# --------------------------------------------------------------------------- #
# static source scan (read-only)
# --------------------------------------------------------------------------- #
_ACCESS_PATTERNS: dict[str, str] = {
    "engine_creation": r"create_engine\(|create_db_engine\(",
    "dsn_selection": r"MINOS_DATABASE_URL|normalize_database_url",
    "set_role_session": r"\bSET ROLE\b",
    "set_role_local": r"SET LOCAL ROLE",
    "direct_insert": r"INSERT INTO ",
    "direct_update": r"\bUPDATE [a-z_]+\.[a-z_]+ ",
    "direct_delete": r"DELETE FROM ",
    "security_definer_call": r"minos_l2f_[a-z_]+\(",
    "advisory_lock": r"pg_advisory",
    "skip_locked": r"SKIP LOCKED",
    "file_uri": r"file://",
    "path_read": r"\.read_bytes\(|\.read_text\(",
    "path_write": r"\.write_bytes\(|\.write_text\(",
    "env_root": r"os\.environ\.get\(\s*ENV_|MINOS_L2F_[A-Z_]+",
    "raw_fd": r"os\.open\(",
}


def scan_source(root: Path) -> dict[str, Any]:
    """Static scan of the repository's database and filesystem access paths."""
    findings: dict[str, list[dict[str, Any]]] = {k: [] for k in _ACCESS_PATTERNS}
    compiled = {k: re.compile(v) for k, v in _ACCESS_PATTERNS.items()}
    for path in sorted((root / "src").rglob("*.py")):
        rel = path.relative_to(root).as_posix()
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:  # pragma: no cover - unreadable source
            continue
        for number, line in enumerate(lines, start=1):
            for key, pattern in compiled.items():
                if pattern.search(line):
                    findings[key].append({"file": rel, "line": number})
    return {
        "patterns": {k: len(v) for k, v in findings.items()},
        "occurrences": findings,
    }


def scan_migrations(root: Path) -> list[dict[str, Any]]:
    """Byte identity of every Alembic migration, in revision order."""
    out: list[dict[str, Any]] = []
    for path in sorted((root / "migrations" / "versions").glob("[0-9]*.py")):
        data = path.read_bytes()
        text = data.decode("utf-8")
        down = re.search(r'down_revision[^=]*=\s*(?:"([^"]*)"|None)', text)
        out.append(
            {
                "file": path.relative_to(root).as_posix(),
                "revision": path.stem,
                "down_revision": (down.group(1) if down and down.group(1) else None),
                "sha256": hashlib.sha256(data).hexdigest(),
                "bytes": len(data),
            }
        )
    return out


# --------------------------------------------------------------------------- #
# cross-document validation
# --------------------------------------------------------------------------- #
def validate(reports: Path, root: Path = REPO_ROOT) -> list[str]:
    """Cross-reference the reports. Returns the list of problems (empty means valid)."""
    problems: list[str] = []
    inventory_path = reports / "MINOS_DATABASE_V1_INVENTORY.json"
    contract_path = reports / "MINOS_DATABASE_V2_CONTRACT.json"
    mapping_path = reports / "MINOS_DATABASE_V2_CURRENT_TO_TARGET.json"

    for path in (inventory_path, contract_path, mapping_path):
        if not path.is_file():
            problems.append(f"missing report: {path.name}")
    if problems:
        return problems

    documents: dict[str, Any] = {}
    for path in (inventory_path, contract_path, mapping_path):
        try:
            documents[path.name] = load_strict(path)
        except ValueError as exc:
            problems.append(f"{path.name}: {exc}")
    if problems:
        return problems
    inventory = documents[inventory_path.name]
    contract = documents[contract_path.name]
    mapping = documents[mapping_path.name]

    # 1) the contract hash must recompute
    recomputed = contract_hash(contract)
    if contract.get(CONTRACT_HASH_FIELD) != recomputed:
        problems.append(
            f"contract hash mismatch: stored {contract.get(CONTRACT_HASH_FIELD)!r} "
            f"!= recomputed {recomputed!r}"
        )

    # 2) every target table named in the contract must be unique and schema-qualified
    target_tables: set[str] = set()
    for schema in contract.get("schemas", []):
        name = schema["schema"]
        for table in schema.get("tables", []):
            ident = f"{name}.{table['table']}"
            if ident in target_tables:
                problems.append(f"duplicate target table: {ident}")
            target_tables.add(ident)
            for column in table.get("columns", []):
                for field in ("name", "type", "nullable"):
                    if field not in column:
                        problems.append(f"{ident}: column missing {field}: {column}")
            if not table.get("primary_key"):
                problems.append(f"{ident}: no primary_key declared")

    # 3) every foreign key must reference a declared target table
    for schema in contract.get("schemas", []):
        for table in schema.get("tables", []):
            ident = f"{schema['schema']}.{table['table']}"
            for fk in table.get("foreign_keys", []):
                if fk["references"] not in target_tables:
                    problems.append(f"{ident}: FK -> unknown target {fk['references']}")

    # 4) every live V1 table must appear exactly once in the mapping
    live_tables = {
        f"{t['schema']}.{t['name']}" for t in inventory["live"]["tables"] if t["kind"] == "r"
    }
    live_views = {
        f"{t['schema']}.{t['name']}" for t in inventory["live"]["tables"] if t["kind"] == "v"
    }
    mapped_sources: dict[str, int] = {}
    for entry in mapping.get("mappings", []):
        mapped_sources[entry["source"]] = mapped_sources.get(entry["source"], 0) + 1
    for ident in sorted(live_tables | live_views):
        if ident not in mapped_sources:
            problems.append(f"live object not mapped: {ident}")
    for ident, count in sorted(mapped_sources.items()):
        if count != 1:
            problems.append(f"object mapped {count} times: {ident}")
        if ident not in live_tables | live_views and not ident.startswith("(planned)"):
            problems.append(f"mapping references unknown live object: {ident}")

    # 5) every mapping target must exist in the contract (or be an explicit terminal action)
    terminal = {"DROP AFTER VERIFICATION", "ARCHIVE"}
    for entry in mapping.get("mappings", []):
        action = entry["action"]
        target = entry.get("target")
        if action in terminal:
            continue
        if target not in target_tables:
            problems.append(f"{entry['source']}: {action} -> unknown target {target!r}")

    # 6) declared action totals must equal the actual counts
    actual: dict[str, int] = {}
    for entry in mapping.get("mappings", []):
        actual[entry["action"]] = actual.get(entry["action"], 0) + 1
    declared = mapping.get("action_totals", {})
    if declared != actual:
        problems.append(f"action_totals {declared} != actual {actual}")

    # 7) declared table counts per schema must equal the actual counts
    declared_counts = contract.get("table_counts", {})
    actual_counts = {
        schema["schema"]: len(schema.get("tables", [])) for schema in contract.get("schemas", [])
    }
    if declared_counts != actual_counts:
        problems.append(f"table_counts {declared_counts} != actual {actual_counts}")

    # 8) every critical query must name an index that the contract declares
    declared_indexes: set[str] = set()
    for schema in contract.get("schemas", []):
        for table in schema.get("tables", []):
            for index in table.get("indexes", []):
                declared_indexes.add(index["name"])
            for unique in table.get("unique_constraints", []):
                declared_indexes.add(unique["name"])
            pk = table.get("primary_key")
            if isinstance(pk, dict) and pk.get("name"):
                declared_indexes.add(pk["name"])
    for query in contract.get("critical_queries", []):
        for index in query.get("indexes", []):
            if index not in declared_indexes and index != "(sequential scan acceptable)":
                problems.append(f"query {query['id']}: unknown index {index!r}")

    # 9) the physical-deployment contract: shadow namespace, counts, revision path, inverses
    problems.extend(_validate_physical_deployment(reports, inventory, target_tables))

    # 9a) DB-V2 D1.3: the recovery contract must be externally stored and byte-verifiable
    physical_path = reports / "MINOS_DATABASE_V2_PHYSICAL_DEPLOYMENT.json"
    if physical_path.is_file():
        try:
            physical = load_strict(physical_path)
        except ValueError as exc:
            problems.append(f"{physical_path.name}: {exc}")
        else:
            problems.extend(_validate_recovery_storage(physical))
            problems.extend(_validate_recovery_bindings(contract, physical))

    # 9c) DB-V2 D1.4: snapshot eligibility, row shapes, the database API and the ACL matrix
    api_path = reports / "MINOS_DATABASE_V2_DATABASE_API.json"
    if not api_path.is_file():
        problems.append(f"missing report: {api_path.name}")
    elif physical_path.is_file():
        try:
            api = load_strict(api_path)
            physical = load_strict(physical_path)
        except ValueError as exc:
            problems.append(f"{api_path.name}: {exc}")
        else:
            problems.extend(_validate_artifact_snapshot(contract, physical))
            problems.extend(_validate_backup_set_shapes(contract))
            problems.extend(_validate_database_api(contract, physical, api))
            problems.extend(_validate_acl(contract, api))
            problems.extend(_validate_d2_acl(contract, physical, api))
            problems.extend(_validate_role_provisioning(api))
            problems.extend(_validate_declared_mutations(root, physical, api))
            problems.extend(_validate_recovery_sequence(contract, physical))
            for document, label in ((contract, "logical contract"), (physical, "physical report")):
                declared = document.get("database_api", {}).get(CONTRACT_HASH_FIELD)
                if declared != api.get(CONTRACT_HASH_FIELD):
                    problems.append(f"the {label} pins a stale database API hash")

    # 9b) stale rollback text, migration 0009 absence, documentation links, report integrity
    problems.extend(_validate_docs_and_migrations(root))
    problems.extend(_validate_report_integrity(root, reports))

    # 10) no secret-shaped material anywhere in the reports
    report_paths = [inventory_path, contract_path, mapping_path]
    physical_path = reports / "MINOS_DATABASE_V2_PHYSICAL_DEPLOYMENT.json"
    if physical_path.is_file():
        report_paths.append(physical_path)
    blob = "\n".join(path.read_text(encoding="utf-8") for path in report_paths)
    for pattern, label in (
        (r"postgresql(\+\w+)?://[^\s\"]*:[^\s\"@]*@", "DSN with credentials"),
        (r"(?i)\bpassword\b\s*[:=]\s*\S", "password literal"),
        (r"(?i)\bsecret\b\s*[:=]\s*\S", "secret literal"),
    ):
        if re.search(pattern, blob):
            problems.append(f"report contains a {label}")

    return problems


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def _validate_recovery_and_retirement(doc: dict[str, Any]) -> list[str]:
    """DB-V2 D1.2: recovery-set ordering, rollback boundaries and retirement targets."""
    problems: list[str] = []

    # --- R1/R2 ordering ------------------------------------------------------------------
    protocol = doc.get("recovery_set_protocol", {})
    phases = {p.get("phase"): p for p in protocol.get("phases", [])}
    if set(phases) != {"R1", "R2"}:
        problems.append(f"recovery protocol must define exactly R1 and R2, got {sorted(phases)}")
    else:
        r1, r2 = phases["R1"], phases["R2"]
        if r1.get("occurs_before_revision") != EXPECTED_REVISION_PATH[-1]:
            problems.append("R1 must occur before the planned D2 revision")
        storage = r1.get("storage", "")
        if RECOVERY_ROOT_ENV not in storage or "outside PostgreSQL" not in storage:
            problems.append(
                f"R1 must be an external file beneath {RECOVERY_ROOT_ENV}, got {storage!r}"
            )
        artifact_path = r1.get("artifact_path", "")
        if not artifact_path.startswith(f"<{RECOVERY_ROOT_ENV}>/"):
            problems.append(f"R1 manifest path must be under the recovery root: {artifact_path!r}")
        if not artifact_path.endswith(".recovery.json"):
            problems.append("R1 must name a content-addressed external manifest file")
        if r2.get("occurs_after_revision") != EXPECTED_REVISION_PATH[-1]:
            problems.append("R2 must occur after the planned D2 revision")
        if r2.get("storage") != "dbv2_catalog.backup_sets":
            problems.append(
                f"R2 must register into dbv2_catalog.backup_sets, got {r2.get('storage')!r}"
            )
        if r2.get("forbidden_target") != "catalog.backup_sets":
            problems.append("R2 must explicitly forbid writing to catalog.backup_sets")
        if not r2.get("retention_of_external_manifest"):
            problems.append("R2 must state how the external R1 manifest is retained")
    invariants = " ".join(protocol.get("ordering_invariants", [])).lower()
    if "strictly precedes revision 0009" not in invariants:
        problems.append("ordering invariants must state R1 strictly precedes 0009")
    if "strictly precedes every business-table transformation beyond b0" not in invariants:
        problems.append("ordering invariants must state R2 precedes transformation beyond B0")
    if "strictly follows revision 0009 and the artifact-catalog bootstrap" not in invariants:
        problems.append("ordering invariants must state R2 follows the artifact-catalog bootstrap")

    # --- rollback boundaries -------------------------------------------------------------
    boundaries = {b.get("boundary"): b for b in doc.get("rollback_boundaries", [])}
    if set(boundaries) != {"B1", "B2", "B3"}:
        problems.append(f"exactly three rollback boundaries required, got {sorted(boundaries)}")
    else:
        for name, boundary in sorted(boundaries.items()):
            if not boundary.get("procedure"):
                problems.append(f"{name}: no procedure declared")
            if len(boundary.get("applicable_actions", [])) < 1:
                problems.append(f"{name}: no applicable action declared")
        if boundaries["B2"].get("touches_v1") is not False:
            problems.append("B2 must not touch any V1 object")
        if boundaries["B2"].get("drops_v2") is not True:
            problems.append("B2 is the only boundary where dropping the shadow schema is correct")
        if boundaries["B3"].get("drops_v2") is not False:
            problems.append("B3 must never drop a V2 object")
        b3_actions = boundaries["B3"].get("applicable_actions", [])
        for required in ("rename_canonical_back_to_shadow", "rename_retired_back_to_canonical"):
            if required not in b3_actions:
                problems.append(f"B3 must include {required}")
        if any("drop" in a for a in b3_actions):
            problems.append("B3 must not contain a drop action")

    # the withdrawn statements must be recorded, not silently deleted
    withdrawn = " ".join(w.get("statement", "") for w in doc.get("withdrawn_statements", []))
    for fragment in (
        "dropping the shadow tables",
        "catalog.backup_sets",
        "canonical objects",
        "reports/database/recovery/R1_RECOVERY_MANIFEST.json",
        "Point the application back",
    ):
        if fragment not in withdrawn:
            problems.append(f"withdrawn_statements must record {fragment!r}")

    # --- retirement ----------------------------------------------------------------------
    retirement = doc.get("retirement", {})
    eligible = retirement.get("eligible_targets", {})
    targets = [
        *eligible.get("tables", []),
        *eligible.get("views", []),
        *eligible.get("archived_source_objects", []),
    ]
    if not targets:
        problems.append("retirement declares no eligible target")
    for target in targets:
        schema = target.split(".", 1)[0]
        if not schema.startswith(RETIRED_SCHEMA_PREFIX):
            problems.append(f"retirement target is not in the retired namespace: {target}")
        if schema in CANONICAL_SCHEMAS:
            problems.append(f"retirement target names a canonical ACTIVE schema: {target}")
    survivors = retirement.get("must_survive_retirement", [])
    for schema in CANONICAL_SCHEMAS:
        if f"{schema}.*" not in survivors:
            problems.append(f"{schema}.* must be declared as surviving retirement")
    if not retirement.get("per_object_checks_before_removal"):
        problems.append("retirement must require per-object verification before removal")

    # --- qualification-period semantics ---------------------------------------------------
    semantics = doc.get("qualification_period_semantics", {})
    if not semantics.get("canonical_schemas_mean", "").startswith("V2"):
        problems.append("qualification semantics must state canonical schemas mean V2")
    if not semantics.get("writes_to_retired_v1", "").lower().startswith("none"):
        problems.append("qualification semantics must forbid writing retired V1 objects")
    if not semantics.get("deletions_of_v2", "").lower().startswith("none"):
        problems.append("qualification semantics must forbid deleting V2 objects")
    return problems


def _strings(value: Any) -> list[str]:
    """Every string reachable inside a JSON-shaped value."""
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [s for item in value.values() for s in _strings(item)]
    if isinstance(value, list):
        return [s for item in value for s in _strings(item)]
    return []


def _backup_sets_table(contract: dict[str, Any]) -> dict[str, Any] | None:
    for schema in contract.get("schemas", []):
        if schema.get("schema") != "catalog":
            continue
        for table in schema.get("tables", []):
            if table.get("table") == "backup_sets":
                return cast("dict[str, Any]", table)
    return None


def _artifacts_table(contract: dict[str, Any]) -> dict[str, Any] | None:
    for schema in contract.get("schemas", []):
        if schema.get("schema") != "catalog":
            continue
        for table in schema.get("tables", []):
            if table.get("table") == "artifacts":
                return cast("dict[str, Any]", table)
    return None


def _validate_recovery_storage(physical: dict[str, Any]) -> list[str]:
    """G1-G2: the recovery root is external, has no default, and no repository path survives."""
    problems: list[str] = []
    storage = physical.get("recovery_storage_contract")
    if not isinstance(storage, dict):
        return ["physical report declares no recovery_storage_contract"]

    if storage.get("env_var") != RECOVERY_ROOT_ENV:
        problems.append(
            f"recovery root must be {RECOVERY_ROOT_ENV}, got {storage.get('env_var')!r}"
        )
    if storage.get("has_default") is not False:
        problems.append(f"{RECOVERY_ROOT_ENV} must have no default")
    git_rules = storage.get("git_rules", {})
    if git_rules.get("repository_relative_fallback") is not False:
        problems.append("no repository-relative recovery fallback may exist")
    if git_rules.get("never_committed") is not True:
        problems.append("recovery files must never be committed to Git")
    for requirement in ("must already exist", "must be an absolute path", "must not be a symlink"):
        if not any(requirement in item for item in storage.get("root_requirements", [])):
            problems.append(f"recovery root requirements omit {requirement!r}")
    separation = storage.get("durability_separation", {}).get("must_differ_from", [])
    for needed in ("Git checkout", "PostgreSQL data directory", "artifact payload root"):
        if not any(needed in item for item in separation):
            problems.append(f"recovery root must be declared separate from the {needed}")
    for step in ("fsync the file", "no-clobber", "fsync the destination directory"):
        if not any(step in item for item in storage.get("publication_protocol", [])):
            problems.append(f"recovery publication protocol omits {step!r}")

    # G1: no live recovery path may point inside the repository. The recorded defect
    # (``supersedes``) and the withdrawn statements deliberately quote the old path.
    live = {k: v for k, v in storage.items() if k != "supersedes"}
    phases = physical.get("recovery_set_protocol", {}).get("phases", [])
    for text in _strings(live) + _strings(phases):
        match = REPO_RELATIVE_RE.search(text)
        if match:
            problems.append(f"recovery path points inside the repository: {text!r}")
    return problems


def _validate_recovery_bindings(contract: dict[str, Any], physical: dict[str, Any]) -> list[str]:
    """G3-G13: catalog.backup_sets can persist and independently recover the exact R1 manifest."""
    problems: list[str] = []
    table = _backup_sets_table(contract)
    if table is None:
        return ["contract declares no catalog.backup_sets table"]
    artifacts = _artifacts_table(contract)
    if artifacts is None:
        return ["contract declares no catalog.artifacts table"]

    columns = {column["name"]: column for column in table.get("columns", [])}
    phases = {
        p.get("phase"): p for p in physical.get("recovery_set_protocol", {}).get("phases", [])
    }
    bindings = phases.get("R2", {}).get("artifact_bindings", {})

    # G3 / G6-G8: all three artifacts are bound by id + digest + exact media type.
    for role, media_type in sorted(RECOVERY_MEDIA_TYPES.items()):
        binding = bindings.get(role)
        if not isinstance(binding, dict):
            problems.append(f"R2 declares no artifact binding for {role}")
            continue
        if binding.get("media_type") != media_type:
            problems.append(f"{role} media type {binding.get('media_type')!r} != {media_type!r}")
        for key in ("id_column", "digest_column"):
            name = binding.get(key)
            if name not in columns:
                problems.append(f"backup_sets has no {key} {name!r} for {role}")
            elif (
                role != "artifact_snapshot_manifest" and columns[name].get("nullable") is not False
            ):
                problems.append(f"backup_sets.{name} must be NOT NULL")
            elif role == "artifact_snapshot_manifest" and columns[name].get("nullable") is not True:
                problems.append(
                    f"backup_sets.{name} must be nullable: a database_only recovery set has no "
                    "artifact snapshot"
                )
        if physical.get("recovery_media_types", {}).get(role) != media_type:
            problems.append(f"physical recovery_media_types[{role}] != {media_type!r}")
        if contract.get("recovery_media_types", {}).get(role) != media_type:
            problems.append(f"contract recovery_media_types[{role}] != {media_type!r}")

    # G4 / G5: each foreign key resolves to catalog.artifacts and carries the digest AND the
    # media type in the SAME key, so a digest can never name a different artifact than its id.
    artifact_columns = {column["name"] for column in artifacts.get("columns", [])}
    unique_targets = {
        tuple(unique["columns"]) for unique in artifacts.get("unique_constraints", [])
    }
    pk = artifacts.get("primary_key")
    if isinstance(pk, dict):
        unique_targets.add(tuple(pk.get("columns", [])))
    by_id_column = {}
    for fk in table.get("foreign_keys", []):
        if fk.get("references") != "catalog.artifacts":
            problems.append(f"backup_sets FK {fk.get('name')} does not reference catalog.artifacts")
            continue
        referenced = tuple(fk.get("referenced_columns", []))
        for name in referenced:
            if name not in artifact_columns:
                problems.append(f"{fk['name']}: catalog.artifacts has no column {name!r}")
        if referenced not in unique_targets:
            problems.append(
                f"{fk['name']}: referenced columns {list(referenced)} are not a declared "
                "UNIQUE target on catalog.artifacts"
            )
        local = tuple(fk.get("columns", []))
        if len(local) != 3:
            problems.append(f"{fk['name']}: must bind id, digest and media type, got {list(local)}")
            continue
        by_id_column[local[0]] = (fk["name"], local)
    for role, binding in sorted(bindings.items()):
        if not isinstance(binding, dict):
            continue
        entry = by_id_column.get(binding.get("id_column"))
        if entry is None:
            problems.append(f"{role}: no composite FK binds {binding.get('id_column')!r}")
            continue
        _, local = entry
        if local[1] != binding.get("digest_column"):
            problems.append(
                f"{role}: FK binds digest {local[1]!r}, protocol declares "
                f"{binding.get('digest_column')!r} - the digest is not bound to the same artifact"
            )

    # the additive UNIQUE target must be declared explicitly in the logical contract
    declared_target = contract.get("artifacts_unique_targets_required_by_backup_sets", {})
    if tuple(declared_target.get("columns", [])) not in unique_targets:
        problems.append("the additive UNIQUE target on catalog.artifacts is not declared")

    # G10: every immutable R1 field maps to exactly one existing column.
    mapping = table.get("r1_field_to_column", {})
    if set(mapping) != set(REQUIRED_R1_FIELDS):
        missing = sorted(set(REQUIRED_R1_FIELDS) - set(mapping))
        extra = sorted(set(mapping) - set(REQUIRED_R1_FIELDS))
        problems.append(f"r1_field_to_column missing {missing}, unexpected {extra}")
    seen: dict[str, str] = {}
    for field, column in sorted(mapping.items()):
        if column not in columns:
            problems.append(f"R1 field {field!r} maps to absent column {column!r}")
        if column in seen:
            problems.append(f"columns {column!r} represents both {seen[column]!r} and {field!r}")
        seen[column] = field
    if phases.get("R2", {}).get("r1_field_to_column") != mapping:
        problems.append("the physical R2 phase and the logical contract disagree on the R1 mapping")
    for field in REQUIRED_R1_FIELDS:
        if field not in physical.get("r1_manifest_fields", []):
            problems.append(f"r1_manifest_fields omits {field!r}")
        if field not in phases.get("R1", {}).get("bound_fields", []):
            problems.append(f"R1 bound_fields omits {field!r}")

    # G11: completeness may only become 'complete' with all three artifacts verified.
    rule = table.get("completeness_rule", "")
    if "three" not in rule.lower() or "verified" not in rule.lower():
        problems.append("backup_sets declares no all-three-verified completeness rule")
    checks = {c["name"]: c["expression"] for c in table.get("check_constraints", [])}
    if "ck_backup_sets_completeness" not in checks:
        problems.append("backup_sets declares no completeness check constraint")

    # G12 / G13: idempotent re-registration; conflicting immutable metadata fails closed.
    for where, idempotency in (
        ("the logical contract", table.get("idempotency_rule", "")),
        ("the R2 phase", phases.get("R2", {}).get("idempotency", "")),
    ):
        if "no-op" not in idempotency:
            problems.append(f"{where} does not declare idempotent re-registration")
        if "fails closed" not in idempotency or "never overwrites" not in idempotency:
            problems.append(
                f"{where} does not declare that conflicting immutable metadata fails closed"
            )
    uniques = {tuple(u["columns"]) for u in table.get("unique_constraints", [])}
    for column in ("backup_key", "recovery_set_id", "recovery_manifest_sha256"):
        if (column,) not in uniques:
            problems.append(f"backup_sets must declare UNIQUE({column}) to fail closed on conflict")

    # G9: the R1 canonical-byte rule must be the whole-manifest digest, and it must be
    # independently reproducible. Prove it here rather than asserting the sentence.
    rule = phases.get("R1", {}).get("canonical_bytes_rule", "")
    if "sha256(canonical_json_bytes" not in rule.replace(" ", ""):
        problems.append("R1 does not define recovery_manifest_sha256 over the canonical bytes")
    specimen = {field: f"value-of-{field}" for field in REQUIRED_R1_FIELDS}
    reordered = dict(reversed(list(specimen.items())))
    first = hashlib.sha256(canonical_bytes(specimen)).hexdigest()
    if first != hashlib.sha256(canonical_bytes(reordered)).hexdigest():
        problems.append("the R1 canonical encoding is not key-order independent")
    perturbed = dict(specimen, artifact_count="value-of-artifact_count!")
    if first == hashlib.sha256(canonical_bytes(perturbed)).hexdigest():
        problems.append("the R1 canonical digest does not cover every manifest field")
    if len(first) != 64 or not re.fullmatch(r"[0-9a-f]{64}", first):
        problems.append("the R1 digest is not a 64-character lowercase hex string")
    return problems


def _validate_docs_and_migrations(root: Path) -> list[str]:
    """G14, G16, G19: no stale rollback instruction, no 0009, every doc link resolves."""
    problems: list[str] = []
    docs = sorted((root / "docs" / "database").glob("*.md"))
    if not docs:
        return [f"no DB-V2 documentation found under {root / 'docs' / 'database'}"]

    for doc in docs:
        text = doc.read_text(encoding="utf-8")
        # G14: paragraph-scoped, so a paragraph that records the withdrawal is not a defect.
        for paragraph in re.split(r"\n\s*\n", text):
            if not DROP_SHADOW_RE.search(paragraph):
                continue
            lowered = paragraph.lower()
            if not any(marker in lowered for marker in WITHDRAWAL_MARKERS):
                first = " ".join(paragraph.split())[:120]
                problems.append(f"{doc.name}: stale drop-the-shadow rollback text: {first!r}")
        # G19: every relative markdown link must resolve.
        for target in re.findall(r"\]\(([^)]+)\)", text):
            if EXTERNAL_LINK_RE.match(target):
                continue
            relative = target.split("#", 1)[0]
            if not relative:
                continue
            if not (doc.parent / relative).resolve().exists():
                problems.append(f"{doc.name}: broken link {target!r}")

    # DB-V2 D2: migration 0009 now EXISTS, and there is exactly one of it. A missing versions
    # directory is itself a failure - the check must never pass by finding nothing to look at.
    versions = root / "migrations" / "versions"
    if not versions.is_dir():
        problems.append(f"no migrations directory at {versions}")
        return problems
    found = sorted(p.name for p in versions.glob("0009*.py"))
    if found != [f"{EXPECTED_REVISION_PATH[-1]}.py"]:
        problems.append(
            f"migration 0009 must be exactly {EXPECTED_REVISION_PATH[-1]}.py, got {found}"
        )
        return problems
    source = (versions / found[0]).read_text(encoding="utf-8")
    for needed in (
        f'revision: str = "{EXPECTED_REVISION_PATH[-1]}"',
        f'down_revision: str | None = "{EXPECTED_REVISION_PATH[-2]}"',
    ):
        if needed not in source:
            problems.append(f"migration 0009 does not declare {needed}")
    return problems


def _validate_report_integrity(root: Path, reports: Path) -> list[str]:
    """G17-G18: every committed JSON report parses strictly and its embedded hash recomputes."""
    problems: list[str] = []
    paths = sorted(reports.glob("*.json"))
    inventory = root / "reports" / "testing" / "MINOS_TEST_INVENTORY.json"
    if inventory.is_file():
        paths.append(inventory)
    for path in paths:
        try:
            document = load_strict(path)
        except ValueError as exc:
            problems.append(f"{path.name}: {exc}")
            continue
        if isinstance(document, dict) and CONTRACT_HASH_FIELD in document:
            recomputed = contract_hash(document)
            if document[CONTRACT_HASH_FIELD] != recomputed:
                problems.append(
                    f"{path.name}: stored {document[CONTRACT_HASH_FIELD]!r} != "
                    f"recomputed {recomputed!r}"
                )
    return problems


# --------------------------------------------------------------------------- #
# DB-V2 D1.4: snapshot eligibility, row shapes, the database API, the ACL matrix
# --------------------------------------------------------------------------- #
def _contract_table(contract: dict[str, Any], ident: str) -> dict[str, Any] | None:
    schema_name, table_name = ident.split(".", 1)
    for schema in contract.get("schemas", []):
        if schema.get("schema") != schema_name:
            continue
        for table in schema.get("tables", []):
            if table.get("table") == table_name:
                return cast("dict[str, Any]", table)
    return None


def _check_domain(table: dict[str, Any], column: str) -> set[str]:
    """The exact value set a ``col IN ('a','b')`` CHECK constrains ``column`` to."""
    for check in table.get("check_constraints", []):
        expression = check["expression"]
        match = re.fullmatch(rf"{re.escape(column)} IN \(([^)]*)\)", expression.strip())
        if match:
            return {v.strip().strip("'") for v in match.group(1).split(",")}
        match = re.fullmatch(
            rf"{re.escape(column)} IS NULL OR {re.escape(column)} IN \(([^)]*)\)",
            expression.strip(),
        )
        if match:
            return {v.strip().strip("'") for v in match.group(1).split(",")} | {NULL_STATE}
    return set()


def _validate_artifact_snapshot(contract: dict[str, Any], physical: dict[str, Any]) -> list[str]:
    """K1-K3: recovery artifacts can never enter the snapshot they describe."""
    problems: list[str] = []
    predicate = contract.get("artifact_snapshot_predicate")
    digest = contract.get("artifact_snapshot_digest")
    if not isinstance(predicate, dict) or not isinstance(digest, dict):
        return ["the logical contract declares no artifact snapshot predicate or digest"]
    if physical.get("artifact_snapshot_predicate") != predicate:
        problems.append("the two contracts disagree on the artifact snapshot predicate")
    if physical.get("artifact_snapshot_digest") != digest:
        problems.append("the two contracts disagree on the artifact snapshot digest")

    artifacts = _contract_table(contract, "catalog.artifacts")
    if artifacts is None:
        return [*problems, "contract declares no catalog.artifacts table"]
    scope = next((c for c in artifacts["columns"] if c["name"] == "backup_scope"), None)
    if scope is None:
        problems.append("catalog.artifacts declares no backup_scope column")
    else:
        if scope.get("nullable") is not False:
            problems.append("catalog.artifacts.backup_scope must be NOT NULL")
        if scope.get("mutability") != "immutable":
            problems.append(
                "catalog.artifacts.backup_scope must be immutable: a reclassifiable scope would "
                "let an artifact enter or leave a snapshot that was already taken"
            )
    domain = _check_domain(artifacts, "backup_scope")
    if domain != BACKUP_SCOPES:
        problems.append(f"backup_scope domain {sorted(domain)} != {sorted(BACKUP_SCOPES)}")

    # K1: the predicate selects operational artifacts only, and says so in one exact form.
    if predicate.get("table") != "catalog.artifacts":
        problems.append("the snapshot predicate must select from catalog.artifacts")
    where = predicate.get("where", "")
    if where != SNAPSHOT_WHERE:
        problems.append(f"snapshot predicate WHERE {where!r} != {SNAPSHOT_WHERE!r}")
    if predicate.get("excluded_by_construction") != "backup_scope = 'recovery'":
        problems.append("the predicate must state that recovery artifacts are excluded")
    if tuple(predicate.get("sort_order", ())) != SNAPSHOT_SORT:
        problems.append(f"snapshot sort order must be {list(SNAPSHOT_SORT)}")
    if tuple(predicate.get("columns_selected", ())) != SNAPSHOT_SORT:
        problems.append(f"snapshot columns must be {list(SNAPSHOT_SORT)}")
    expected_sql = (
        f"SELECT {', '.join(SNAPSHOT_SORT)} FROM catalog.artifacts "
        f"WHERE {SNAPSHOT_WHERE} ORDER BY {', '.join(SNAPSHOT_SORT)}"
    )
    if predicate.get("sql") != expected_sql:
        problems.append(f"snapshot SQL is not the exact predicate: {predicate.get('sql')!r}")

    # K1: the supporting index must match the predicate exactly (K3's "exact" half).
    index = next(
        (i for i in artifacts.get("indexes", []) if i["name"] == predicate.get("supporting_index")),
        None,
    )
    if index is None:
        problems.append(
            f"no index named {predicate.get('supporting_index')!r} on catalog.artifacts"
        )
    else:
        if index.get("where") != SNAPSHOT_WHERE:
            problems.append("the supporting index predicate does not equal the snapshot predicate")
        if tuple(index.get("columns", ())) != SNAPSHOT_SORT:
            problems.append("the supporting index does not cover the snapshot sort order")

    # K3: the digest formula is domain-separated, total, and independently reproducible.
    if digest.get("domain") != SNAPSHOT_DIGEST_DOMAIN:
        problems.append(f"snapshot digest domain must be {SNAPSHOT_DIGEST_DOMAIN!r}")
    if "canonical_json_bytes" not in digest.get("formula", "").replace(" ", ""):
        problems.append("the snapshot digest formula must be over canonical JSON bytes")
    if "DOMAIN_bytes" not in digest.get("formula", ""):
        problems.append("the snapshot digest formula must be domain-separated")
    if digest.get("domain") == CONTRACT_HASH_DOMAIN.decode("utf-8"):
        problems.append("the snapshot digest domain collides with the contract hash domain")
    if tuple(digest.get("entry_fields", ())) != SNAPSHOT_SORT:
        problems.append(f"snapshot entry fields must be {list(SNAPSHOT_SORT)}")
    for field in ("artifact_count", "artifact_total_bytes", "entries", "predicate"):
        if field not in digest.get("manifest_fields", []):
            problems.append(f"the snapshot manifest must carry {field!r}")
    if "fails closed" not in digest.get("ambiguity_rule", ""):
        problems.append("duplicate or unresolvable snapshot entries must fail closed")
    domain_bytes = SNAPSHOT_DIGEST_DOMAIN.encode("utf-8")
    entries = [
        {"artifact_kind": "vcf", "content_sha256": "a" * 64, "size_bytes": 3},
        {"artifact_kind": "vcf", "content_sha256": "b" * 64, "size_bytes": 5},
    ]
    manifest = {
        "artifact_count": 2,
        "artifact_total_bytes": 8,
        "entries": entries,
        "predicate": SNAPSHOT_WHERE,
        "recovery_set_id": "r",
        "schema_version": "v1",
    }
    first = hashlib.sha256(domain_bytes + canonical_bytes(manifest)).hexdigest()
    if first == hashlib.sha256(canonical_bytes(manifest)).hexdigest():
        problems.append("the snapshot digest is not actually domain-separated")
    swapped = dict(manifest, entries=list(reversed(entries)))
    if first == hashlib.sha256(domain_bytes + canonical_bytes(swapped)).hexdigest():
        problems.append("the snapshot digest does not depend on the frozen entry order")
    if not re.fullmatch(r"[0-9a-f]{64}", first):
        problems.append("the snapshot digest is not a 64-character lowercase hex string")
    return problems


def _shape_disjuncts(expression: str) -> list[dict[str, Any]]:
    """Parse ``(completeness = 'x' AND ... IS [NOT] NULL ...) OR (...)`` into per-shape facts."""
    parts = re.findall(r"\(([^()]*)\)", expression)
    shapes: list[dict[str, Any]] = []
    for part in parts:
        state = re.search(r"completeness = '([a-z_]+)'", part)
        if not state:
            continue
        nullness: dict[str, bool] = {}
        for column in SNAPSHOT_SHAPE_COLUMNS:
            if re.search(rf"\b{re.escape(column)} IS NOT NULL\b", part):
                nullness[column] = False
            elif re.search(rf"\b{re.escape(column)} IS NULL\b", part):
                nullness[column] = True
        shapes.append({"completeness": state.group(1), "nullness": nullness, "text": part})
    return shapes


def _validate_backup_set_shapes(contract: dict[str, Any]) -> list[str]:
    """K4-K5: the two row shapes are exclusive, exhaustive, and completeness is immutable."""
    problems: list[str] = []
    table = _contract_table(contract, "catalog.backup_sets")
    if table is None:
        return ["contract declares no catalog.backup_sets table"]
    columns = {c["name"]: c for c in table["columns"]}
    checks = {c["name"]: c["expression"] for c in table.get("check_constraints", [])}

    domain = _check_domain(table, "completeness")
    if domain != COMPLETENESS_STATES:
        problems.append(f"completeness domain {sorted(domain)} != {sorted(COMPLETENESS_STATES)}")
    if columns.get("completeness", {}).get("mutability") != "immutable":
        problems.append("completeness must be immutable - no in-place database_only -> complete")

    shape = checks.get("ck_backup_sets_shape")
    if shape is None:
        return [*problems, "backup_sets declares no ck_backup_sets_shape constraint"]
    shapes = _shape_disjuncts(shape)
    if len(shapes) != 2:
        return [
            *problems,
            f"ck_backup_sets_shape must have exactly two disjuncts, got {len(shapes)}",
        ]
    states = {s["completeness"] for s in shapes}
    if states != COMPLETENESS_STATES:
        problems.append(f"the two shapes cover {sorted(states)}, not {sorted(COMPLETENESS_STATES)}")
    by_state = {s["completeness"]: s for s in shapes}
    for state, want_null in (("complete", False), ("database_only", True)):
        found = by_state.get(state, {}).get("nullness", {})
        for column in SNAPSHOT_SHAPE_COLUMNS:
            if column not in found:
                problems.append(f"{state}: ck_backup_sets_shape says nothing about {column}")
            elif found[column] is not want_null:
                problems.append(f"{state}: {column} must be {'NULL' if want_null else 'NOT NULL'}")
    if len(shapes) == 2 and shapes[0]["completeness"] == shapes[1]["completeness"]:
        problems.append("the two shapes are not mutually exclusive: same completeness value")
    if "artifact_count >= 0" not in by_state.get("complete", {}).get("text", ""):
        problems.append("a complete row must require artifact_count >= 0")
    if "artifact_total_bytes >= 0" not in by_state.get("complete", {}).get("text", ""):
        problems.append("a complete row must require artifact_total_bytes >= 0")

    for column in SNAPSHOT_SHAPE_COLUMNS:
        entry = columns.get(column)
        if entry is None:
            problems.append(f"backup_sets has no column {column}")
        elif entry.get("nullable") is not True:
            problems.append(
                f"backup_sets.{column} must be nullable: a NOT NULL column makes the declared "
                "database_only shape structurally impossible"
            )
        elif "default" in entry:
            problems.append(
                f"backup_sets.{column} must have no default: a default fills the "
                "database_only shape silently"
            )

    row_shapes = table.get("row_shapes", {})
    if row_shapes.get("mutually_exclusive") is not True:
        problems.append("row_shapes must declare the two shapes mutually exclusive")
    if row_shapes.get("exhaustive") is not True:
        problems.append("row_shapes must declare the two shapes exhaustive")
    if row_shapes.get("complete", {}).get("may_authorize_migration") is not True:
        problems.append("a complete recovery set must be able to authorize a migration")
    if row_shapes.get("database_only", {}).get("may_authorize_migration") is not False:
        problems.append("a database_only recovery set must NOT authorize a migration")
    if "never upgraded" not in row_shapes.get("no_in_place_upgrade", ""):
        problems.append("row_shapes must forbid an in-place database_only -> complete upgrade")
    return problems


def _validate_database_api(
    contract: dict[str, Any], physical: dict[str, Any], api: dict[str, Any]
) -> list[str]:
    """K6-K12, K19: functions, triggers, transitions and cutover recreation."""
    problems: list[str] = []
    functions = {f["name"]: f for f in api.get("functions", [])}
    triggers = api.get("triggers", [])
    if len(functions) != len(api.get("functions", [])):
        problems.append("two declared functions share a name - every function name must be unique")
    names = [t["name"] for t in triggers]
    if len(set(names)) != len(names):
        duplicates = sorted({n for n in names if names.count(n) > 1})
        problems.append(f"duplicate trigger names: {duplicates}")

    # K10: every function carries an exact signature and a complete security contract.
    for name, function in sorted(functions.items()):
        for field in FUNCTION_REQUIRED_FIELDS:
            if field not in function:
                problems.append(f"{name}: function record omits {field!r}")
        if not str(function.get("signature", "")).startswith(f"{name}("):
            problems.append(f"{name}: signature {function.get('signature')!r} does not name it")
        if function.get("security_mode") not in {"INVOKER", "DEFINER"}:
            problems.append(f"{name}: security_mode must be INVOKER or DEFINER")
        if function.get("configured_search_path") != SAFE_SEARCH_PATH:
            problems.append(f"{name}: search_path must be pinned to {SAFE_SEARCH_PATH!r}")
        if "PUBLIC" not in function.get("revoked_roles", []):
            problems.append(f"{name}: EXECUTE must be revoked from PUBLIC")
        if not function.get("return_type"):
            problems.append(f"{name}: no return type")
        if not function.get("sqlstates"):
            problems.append(f"{name}: declares no SQLSTATE")
        if not function.get("downgrade_behavior"):
            problems.append(f"{name}: declares no downgrade behaviour")
        overlap = set(function.get("executable_roles", [])) & set(function.get("revoked_roles", []))
        if overlap:
            problems.append(f"{name}: {sorted(overlap)} both granted and revoked")
        # K19: a plpgsql body is stored as text, so a schema rename does not follow it.
        if function.get("cutover_recreation_required") is not True:
            problems.append(f"{name}: must be re-created in the cutover transaction")
        if not function.get("cutover_note"):
            problems.append(f"{name}: no cutover note")

    # K11: every trigger names a declared function.
    for trigger in triggers:
        if trigger.get("function") not in functions:
            problems.append(
                f"trigger {trigger.get('name')} names undeclared function "
                f"{trigger.get('function')!r}"
            )
        if _contract_table(contract, trigger.get("table", "")) is None:
            problems.append(
                f"trigger {trigger.get('name')} names undeclared table {trigger.get('table')!r}"
            )

    by_table: dict[str, list[dict[str, Any]]] = {}
    for trigger in triggers:
        by_table.setdefault(trigger.get("table", ""), []).append(trigger)

    # K6: cross-table completeness enforcement is declared, as a constraint trigger.
    gate = functions.get(BACKUP_SET_GATE)
    if gate is None:
        problems.append(f"no cross-table completeness gate {BACKUP_SET_GATE}")
    else:
        if "catalog.artifacts" not in gate.get("tables_read", []):
            problems.append(f"{BACKUP_SET_GATE} must read catalog.artifacts")
        if "catalog.artifact_locations" not in gate.get("tables_read", []):
            problems.append(f"{BACKUP_SET_GATE} must check artifact locations")
        declared = " | ".join(gate.get("sqlstates", []))
        for phrase in GATE_REQUIRED_CHECKS:
            if phrase not in declared:
                problems.append(f"{BACKUP_SET_GATE} does not reject: {phrase}")
        if "no-op" not in gate.get("idempotency", ""):
            problems.append(f"{BACKUP_SET_GATE} must declare an idempotent exact replay")
        if "fails closed" not in gate.get("idempotency", ""):
            problems.append(f"{BACKUP_SET_GATE} must reject conflicting immutable metadata")
        if "FOR UPDATE" not in gate.get("concurrency", ""):
            problems.append(f"{BACKUP_SET_GATE} must lock the artifacts it verifies")
    gate_triggers = [
        t for t in by_table.get("catalog.backup_sets", []) if t.get("function") == BACKUP_SET_GATE
    ]
    if not gate_triggers:
        problems.append("catalog.backup_sets carries no completeness-gate trigger")
    for trigger in gate_triggers:
        if trigger.get("constraint_trigger") is not True:
            problems.append(f"{trigger['name']} must be a CONSTRAINT trigger")
        if "INSERT" not in trigger.get("event", ""):
            problems.append(f"{trigger['name']} must fire on INSERT")

    # K7 / K12: every immutable column and every protected table is covered by a trigger.
    inventory = api.get("immutable_column_inventory", {})
    mutable_rules = api.get("mutable_column_rules", {})
    for schema in contract.get("schemas", []):
        for table in schema.get("tables", []):
            ident = f"{schema['schema']}.{table['table']}"
            if ident == SHARED_ALEMBIC_TABLE:
                continue
            immutable = [c["name"] for c in table["columns"] if c.get("mutability") == "immutable"]
            mutable = [c["name"] for c in table["columns"] if c.get("mutability") == "mutable"]
            if inventory.get(ident) != immutable:
                problems.append(f"{ident}: immutable column inventory does not match the contract")
            attached = by_table.get(ident, [])
            functions_used = {t.get("function") for t in attached}
            if not any(t.get("event") == "DELETE" for t in attached):
                problems.append(f"{ident}: no DELETE protection trigger")
            if mutable:
                if set(mutable_rules.get(ident, {})) != set(mutable):
                    problems.append(f"{ident}: mutable column rules do not match the contract")
                guards = functions_used & IMMUTABILITY_FUNCTIONS
                if not guards:
                    problems.append(
                        f"{ident}: {len(immutable)} immutable columns, no guard trigger"
                    )
                for trigger in attached:
                    if (
                        trigger.get("function") == GENERIC_IMMUTABILITY_FUNCTION
                        and trigger.get("arguments") != immutable
                    ):
                        problems.append(
                            f"{trigger['name']}: arguments do not equal the immutable columns"
                        )
            else:
                if ident in mutable_rules:
                    problems.append(f"{ident}: has no mutable column but declares update rules")
                if GENERIC_NO_UPDATE_FUNCTION not in functions_used:
                    problems.append(f"{ident}: fully immutable but accepts UPDATE")

    # K8 / K9: every declared transition is reachable, and forbidden ones are disjoint from it.
    for key, machine in sorted(api.get("state_machines", {}).items()):
        enforcer = machine.get("enforced_by", "")
        for candidate in enforcer.split(" / "):
            if candidate and candidate not in functions:
                problems.append(f"state machine {key}: enforcer {candidate!r} is not declared")
        allowed = {tuple(t) for t in machine.get("transitions", [])}
        forbidden = {tuple(t) for t in machine.get("forbidden", [])}
        if not forbidden:
            problems.append(f"state machine {key}: declares no forbidden transition")
        overlap = allowed & forbidden
        if overlap:
            problems.append(f"state machine {key}: {sorted(overlap)} both allowed and forbidden")
        table_ident = machine.get("table", "")
        column = machine.get("column", "")
        table = _contract_table(contract, table_ident)
        if table is None or not column.startswith("("):
            if table is None:
                continue
            domain = _check_domain(table, column) | {NULL_STATE}
            if not domain - {NULL_STATE}:
                continue
            for source, target in sorted(allowed):
                for state in (source, target):
                    if state not in domain:
                        problems.append(
                            f"state machine {key}: {state!r} is not in the CHECK domain of "
                            f"{table_ident}.{column}"
                        )
            reachable = set(machine.get("initial", []))
            for _ in range(len(allowed) + 1):
                reachable |= {t for s, t in allowed if s in reachable}
            for state in domain - {NULL_STATE}:
                if state not in reachable:
                    problems.append(
                        f"state machine {key}: {state!r} is declared by a CHECK but unreachable "
                        "through any allowed transition"
                    )

    # K19: the physical deployment maps every function, and both directions re-create bodies.
    mapping = {m["canonical_function"]: m for m in physical.get("function_deployment_mapping", [])}
    if set(mapping) != set(functions):
        missing = sorted(set(functions) - set(mapping))
        extra = sorted(set(mapping) - set(functions))
        problems.append(f"function deployment mapping missing {missing}, unexpected {extra}")
    shadow = physical.get("schema_mapping", {}).get("canonical_to_shadow", {})
    for name, entry in sorted(mapping.items()):
        schema_name, bare = name.split(".", 1)
        expected = f"{shadow.get(schema_name)}.{bare}"
        if entry.get("d2_physical_function") != expected:
            problems.append(
                f"{name}: shadow function {entry.get('d2_physical_function')!r} != {expected!r}"
            )
        if entry.get("post_cutover_function") != name:
            problems.append(f"{name}: post-cutover identity must equal the canonical name")
    for key in ("cutover_mapping", "rollback_mapping"):
        actions = [s["action"] for s in physical.get(key, {}).get("steps", [])]
        if "recreate_function_bodies" not in actions:
            problems.append(f"{key} does not re-create function bodies")
        elif "revalidate" in actions and actions.index("recreate_function_bodies") > actions.index(
            "revalidate"
        ):
            problems.append(f"{key} re-validates before re-creating the function bodies")
    return problems


def _validate_acl(contract: dict[str, Any], api: dict[str, Any]) -> list[str]:
    """K13-K16: the ACL matrix resolves, is unambiguous, closed, and DDL-free for runtime roles."""
    problems: list[str] = []
    acl = api.get("acl")
    if not isinstance(acl, dict):
        return ["the database API declares no ACL matrix"]
    principals = list(acl.get("principals", []))
    if "PUBLIC" not in principals:
        problems.append("the ACL matrix must carry an explicit PUBLIC principal")
    objects = acl.get("objects", {})
    schemas = {s["schema"] for s in contract.get("schemas", [])}
    tables = {
        f"{s['schema']}.{t['table']}"
        for s in contract.get("schemas", [])
        for t in s.get("tables", [])
    }
    functions = {f["name"] for f in api.get("functions", [])}
    universe = {
        "schema": (set(objects.get("schemas", [])), schemas),
        "table": (set(objects.get("tables", [])), tables),
        "function": (set(objects.get("functions", [])), functions),
    }
    # K13: every ACL object resolves to a declared object, and none is missing.
    for kind, (declared, actual) in sorted(universe.items()):
        for ident in sorted(declared - actual):
            problems.append(f"ACL names an undeclared {kind}: {ident}")
        for ident in sorted(actual - declared):
            problems.append(f"ACL omits the {kind} {ident}")

    # K14: exactly one record per (object, principal) pair, and the matrix is complete.
    seen: dict[tuple[str, str, str], int] = {}
    for record in acl.get("records", []):
        key = (record.get("object_type", ""), record.get("object", ""), record.get("principal", ""))
        seen[key] = seen.get(key, 0) + 1
        if set(record.get("privileges", {})) != set(ACL_PRIVILEGE_KEYS):
            problems.append(f"{key}: privilege record does not carry every privilege key")
        if "grant_option" not in record:
            problems.append(f"{key}: no grant option recorded")
    for key, count in sorted(seen.items()):
        if count != 1:
            problems.append(f"{key}: {count} privilege records - the matrix is ambiguous")
    expected_pairs = {
        (kind, ident, principal)
        for kind, (declared, _) in universe.items()
        for ident in declared
        for principal in principals
    }
    for key in sorted(expected_pairs - set(seen)):
        problems.append(f"{key}: no privilege record - the matrix is not exhaustive")
    if acl.get("counts_note") is None and api.get("counts", {}).get("acl_records") != len(seen):
        problems.append("the declared ACL record count does not equal the number of records")

    records = {(r["object_type"], r["object"], r["principal"]): r for r in acl.get("records", [])}
    # K15: PUBLIC holds nothing anywhere, and the default grant is revoked.
    for key, record in sorted(records.items()):
        if key[2] != "PUBLIC":
            continue
        granted = sorted(p for p, held in record["privileges"].items() if held)
        if granted:
            problems.append(f"PUBLIC holds {granted} on {key[1]}")
        if record.get("grant_option"):
            problems.append(f"PUBLIC holds a grant option on {key[1]}")
    if "REVOKE ALL ON SCHEMA public FROM PUBLIC" not in acl.get("public_revocation", ""):
        problems.append("the ACL must revoke PostgreSQL's default grant on schema public")

    # K16: no runtime role holds any DDL privilege, anywhere.
    ddl_holders = set(acl.get("create_privilege", {}).get("granted_to", []))
    for role in RUNTIME_ROLES:
        if role in ddl_holders:
            problems.append(f"{role} holds CREATE - runtime roles have no DDL privilege")
    for key, record in sorted(records.items()):
        if key[0] != "table" or key[2] not in RUNTIME_ROLES:
            continue
        for privilege in DDL_TABLE_PRIVILEGES:
            if record["privileges"].get(privilege):
                problems.append(f"{key[2]} holds {privilege} on {key[1]} - that is a DDL privilege")
    if set(ddl_holders) - {MIGRATION_ROLE, DEFINER_PRINCIPAL}:
        problems.append(f"only {MIGRATION_ROLE} and {DEFINER_PRINCIPAL} may hold CREATE")

    # the frozen role-specific rules
    truth = records.get(("table", TRUTH_BINDINGS, "minos_evaluator"))
    if truth is None or not truth["privileges"].get("SELECT"):
        problems.append(f"minos_evaluator must be able to read {TRUTH_BINDINGS}")
    for key, record in sorted(records.items()):
        if (
            key[0] == "table"
            and key[1] == TRUTH_BINDINGS
            and key[2] not in {"minos_evaluator", DEFINER_PRINCIPAL}
            and record["privileges"].get("SELECT")
        ):
            problems.append(f"{key[2]} may not read {TRUTH_BINDINGS}")
    runner_jobs = records.get(("table", "experiments.experiment_jobs", "minos_runner"))
    if runner_jobs is not None:
        for privilege in ("INSERT", "UPDATE", "DELETE"):
            if runner_jobs["privileges"].get(privilege):
                problems.append(f"minos_runner must have no direct {privilege} on experiment_jobs")
    for key, record in sorted(records.items()):
        if key[0] != "table" or key[2] != "minos_verifier":
            continue
        for privilege in ("INSERT", "UPDATE", "DELETE", "TRUNCATE"):
            if record["privileges"].get(privilege):
                problems.append(f"the verifier must be read-only: {privilege} on {key[1]}")
    return problems


def _validate_d2_acl(
    contract: dict[str, Any], physical: dict[str, Any], api: dict[str, Any]
) -> list[str]:
    """D2 B2: migration 0009 applies the shadow-scoped ACL only, and touches nothing shared."""
    problems: list[str] = []
    d2 = api.get("d2_physical_acl")
    logical = api.get("acl", {})
    if not isinstance(d2, dict):
        return ["the database API declares no D2 physical ACL"]

    if logical.get("applies_at") != "after cutover":
        problems.append("the logical ACL must declare that it applies after cutover, not in D2")
    if d2.get("applies_at") != "migration 0009":
        problems.append("the D2 physical ACL must declare that it applies in migration 0009")

    shadow = physical.get("schema_mapping", {}).get("canonical_to_shadow", {})
    inverse = {v: k for k, v in shadow.items()}
    records = d2.get("records", [])
    principals = list(d2.get("principals", []))
    counts = d2.get("counts", {})
    if len(records) != D2_ACL_RECORDS:
        problems.append(f"the D2 ACL has {len(records)} records, not {D2_ACL_RECORDS}")
    if counts.get("records") != len(records):
        problems.append("the declared D2 record count does not equal the number of records")
    if counts.get("objects") != D2_ACL_OBJECTS:
        problems.append(f"the D2 ACL must cover exactly {D2_ACL_OBJECTS} objects")
    if len(principals) != 10:
        problems.append(f"the D2 ACL must cover 10 principals, got {len(principals)}")
    if counts.get("schemas") != len(shadow):
        problems.append(f"the D2 ACL must cover the {len(shadow)} new shadow schemas")

    # every D2 object is a NEW dbv2_* object, and nothing shared or V1 appears
    logical_records = {
        (r["object_type"], r["object"], r["principal"]): r for r in logical.get("records", [])
    }
    seen: dict[tuple[str, str, str], int] = {}
    for record in records:
        object_type, ident = record.get("object_type", ""), record.get("object", "")
        key = (object_type, ident, record.get("principal", ""))
        seen[key] = seen.get(key, 0) + 1
        if ident == "public" or ident.startswith("public."):
            problems.append(f"the D2 ACL names a shared object: {ident}")
            continue
        schema_name = ident.split(".", 1)[0]
        if not schema_name.startswith(SHADOW_SCHEMA_PREFIX):
            problems.append(f"the D2 ACL names an object outside the shadow namespace: {ident}")
            continue
        canonical_schema = inverse.get(schema_name)
        if canonical_schema is None:
            problems.append(f"{ident}: {schema_name} is not a declared shadow schema")
            continue
        canonical = (
            canonical_schema
            if object_type == "schema"
            else f"{canonical_schema}.{ident.split('.', 1)[1]}"
        )
        source = logical_records.get((object_type, canonical, key[2]))
        if source is None:
            problems.append(f"{ident}: no logical ACL record for {canonical}")
        elif source["privileges"] != record.get("privileges"):
            problems.append(f"{ident}/{key[2]}: D2 privileges differ from the logical final ACL")
        elif source["grant_option"] != record.get("grant_option"):
            problems.append(f"{ident}/{key[2]}: D2 grant option differs from the logical final ACL")
    for key, count in sorted(seen.items()):
        if count != 1:
            problems.append(f"{key}: {count} D2 privilege records - the matrix is ambiguous")

    objects = d2.get("objects", {})
    expected_pairs = {
        (kind, ident, principal)
        for kind, key in (("schema", "schemas"), ("table", "tables"), ("function", "functions"))
        for ident in objects.get(key, [])
        for principal in principals
    }
    for key in sorted(expected_pairs - set(seen)):
        problems.append(f"{key}: no D2 privilege record - the matrix is not exhaustive")

    # the two shared objects are named as excluded, on purpose
    excluded = " | ".join(d2.get("excluded_objects", {}).get("objects", []))
    for shared in ("public (schema)", "public.alembic_version"):
        if shared not in excluded:
            problems.append(f"the D2 ACL must record {shared!r} as deliberately excluded")
    forbidden = " | ".join(d2.get("forbidden_statements", []))
    for statement in D2_FORBIDDEN_ACL_TARGETS:
        if statement not in forbidden:
            problems.append(f"the D2 ACL must forbid statements {statement!r}")
    if "ALTER DEFAULT PRIVILEGES" not in forbidden:
        problems.append("the D2 ACL must forbid ALTER DEFAULT PRIVILEGES on canonical schemas")

    # PUBLIC holds nothing, and the revoke is scoped to newly created objects
    for record in records:
        if record.get("principal") != "PUBLIC":
            continue
        granted = sorted(p for p, held in record.get("privileges", {}).items() if held)
        if granted:
            problems.append(f"PUBLIC holds {granted} on the new object {record['object']}")
    revocation = d2.get("public_revocation", "")
    if "dbv2_" not in revocation or "0009 created" not in revocation:
        problems.append("the D2 PUBLIC revoke must be scoped to objects 0009 itself created")
    if "ON SCHEMA public FROM PUBLIC" in revocation:
        problems.append("D2 must not revoke on the shared public schema")
    if "IN SCHEMA <each new dbv2_* schema>" not in d2.get("default_privileges", ""):
        problems.append("D2 default privileges must be scoped to the new shadow schemas")

    # runtime roles hold no DDL privilege on any new object either
    for role in RUNTIME_ROLES:
        if role in d2.get("create_privilege", {}).get("granted_to", []):
            problems.append(f"{role} holds CREATE on a new shadow schema")
    for record in records:
        if record.get("object_type") != "table" or record.get("principal") not in RUNTIME_ROLES:
            continue
        for privilege in DDL_TABLE_PRIVILEGES:
            if record["privileges"].get(privilege):
                problems.append(f"{record['principal']} holds {privilege} on {record['object']}")
    return problems


def _validate_recovery_sequence(contract: dict[str, Any], physical: dict[str, Any]) -> list[str]:
    """D2.2 C: the five phases, in order, with B0 and B1 declared as later work."""
    problems: list[str] = []
    sequence = contract.get("recovery_sequence")
    if not isinstance(sequence, dict):
        return ["the logical contract declares no recovery sequence"]
    if physical.get("recovery_sequence") != sequence:
        problems.append("the two contracts disagree on the recovery sequence")

    phases = sequence.get("phases", [])
    order = [phase.get("phase") for phase in phases]
    if tuple(order) != RECOVERY_SEQUENCE:
        problems.append(f"the recovery sequence is {order}, not {list(RECOVERY_SEQUENCE)}")
        return problems
    by_phase = {phase["phase"]: phase for phase in phases}
    for phase in RECOVERY_SEQUENCE:
        if not by_phase[phase].get("steps"):
            problems.append(f"{phase} declares no steps")
        if not by_phase[phase].get("implemented_in"):
            problems.append(f"{phase} does not say where it is implemented")
    for phase in ("B0", "B1"):
        if "NOT implemented" not in by_phase[phase]["implemented_in"]:
            problems.append(f"{phase} must be declared as later work, not as implemented")
    if not by_phase["R2"].get("occurs", "").startswith("after B0"):
        problems.append("R2 must occur after the artifact-catalog bootstrap")
    if "0009 creates the 37 shadow tables EMPTY" not in " ".join(by_phase["S1"]["steps"]):
        problems.append("S1 must state that 0009 creates the shadow tables empty")

    rules = " ".join(sequence.get("safety_rules", []))
    for rule in (
        "complete R1 is required before any upgrade",
        "complete R2 is required before any transformation beyond B0",
        "cutover requires BOTH",
    ):
        if rule not in rules:
            problems.append(f"the safety rules omit {rule!r}")

    withdrawn = " ".join(w.get("statement", "") for w in physical.get("withdrawn_statements", []))
    if "R2 strictly precedes any data transformation" not in withdrawn:
        problems.append("the unexecutable R2-precedes-everything statement must be withdrawn")

    protocol = physical.get("recovery_set_protocol", {})
    r2: dict[str, Any] = next((p for p in protocol.get("phases", []) if p.get("phase") == "R2"), {})
    if "bootstrap" not in r2.get("occurs_after_step", ""):
        problems.append("the R2 phase must record that it follows the artifact-catalog bootstrap")
    if not r2.get("bootstrap_precondition"):
        problems.append("the R2 phase must state the bootstrap precondition")
    return problems


def _validate_declared_mutations(
    root: Path, physical: dict[str, Any], api: dict[str, Any]
) -> list[str]:
    """A STATIC COVERAGE CHECK on the declared tables_mutated inventory.

    It regex-scans the committed migration for the INSERT/UPDATE targets inside each function
    body and compares them with what the function declares. It executes nothing and proves nothing
    about runtime behaviour: it catches a declaration that no statement could possibly satisfy,
    which is exactly the defect D2 shipped. The BEHAVIOURAL verification - execute the function,
    observe which tables actually changed - lives in
    tests/integration/layer2_dbv2/test_d22_recovery_sequence.py.
    """
    problems: list[str] = []
    migration = root / "migrations" / "versions" / f"{EXPECTED_REVISION_PATH[-1]}.py"
    if not migration.is_file():
        return [f"cannot audit declared mutations: {migration.name} is missing"]
    source = migration.read_text(encoding="utf-8")
    shadow = physical.get("schema_mapping", {}).get("canonical_to_shadow", {})
    inverse = {v: k for k, v in shadow.items()}

    bodies: dict[str, str] = {}
    for match in re.finditer(r"CREATE FUNCTION (dbv2_\w+)\.(\w+)\((.*?)\n\$minos\$;", source, re.S):
        bodies[f"{match.group(1)}.{match.group(2)}"] = match.group(3)

    for function in sorted(api.get("functions", []), key=lambda f: f["name"]):
        if function.get("kind") != "api_function":
            continue
        canonical = function["name"]
        schema_name, bare = canonical.split(".", 1)
        physical_name = f"{shadow.get(schema_name)}.{bare}"
        body = bodies.get(physical_name)
        if body is None:
            problems.append(f"{canonical}: no generated body found in the migration")
            continue
        observed: set[str] = set()
        for statement in re.finditer(r"(?:INSERT INTO|UPDATE)\s+(dbv2_\w+)\.(\w+)", body):
            mapped = inverse.get(statement.group(1))
            if mapped:
                observed.add(f"{mapped}.{statement.group(2)}")
        declared = set(function.get("tables_mutated", []))
        for table in sorted(declared - observed):
            problems.append(
                f"{canonical} declares it mutates {table} but the generated body never writes it"
            )
        for table in sorted(observed - declared):
            problems.append(f"{canonical} writes {table} but does not declare it in tables_mutated")
    return problems


def _validate_role_provisioning(api: dict[str, Any]) -> list[str]:
    """K17-K18: roles are preflighted, never created by 0009, never dropped by a downgrade."""
    problems: list[str] = []
    provisioning = api.get("role_provisioning")
    if not isinstance(provisioning, dict):
        return ["the database API declares no role provisioning contract"]
    if provisioning.get("roles_created_by_0009") != []:
        problems.append("migration 0009 must create no cluster role")
    required = provisioning.get("required_roles", [])
    if set(required) != set(REQUIRED_ROLES):
        problems.append(f"required roles {sorted(required)} != {sorted(REQUIRED_ROLES)}")
    order = provisioning.get("preflight_order", [])
    for phrase in PREFLIGHT_REQUIRED_STEPS:
        if not any(phrase in step for step in order):
            problems.append(f"the preflight order omits {phrase!r}")
    if order and "only then" in order[0]:
        problems.append("object creation must not be the first preflight step")

    def _index(fragment: str) -> int:
        return next((i for i, step in enumerate(order) if fragment in step), -1)

    elevate = _index(ELEVATION_STATEMENT)
    create = _index("only then: CREATE SCHEMA")
    for fragment in PREFLIGHT_CHECKS_BEFORE_ELEVATION:
        position = _index(fragment)
        if 0 <= elevate < position:
            problems.append(f"the elevation precedes the check {fragment!r}")
    if elevate < 0:
        problems.append(f"the preflight must elevate with {ELEVATION_STATEMENT!r}")
    elif 0 <= create <= elevate:
        problems.append("objects may not be created before the elevation")
    if any("SET ROLE" in step and "SET LOCAL ROLE" not in step for step in order):
        problems.append(
            "the migration must elevate with SET LOCAL ROLE, which the transaction undoes, "
            "never with SET ROLE, which outlives it"
        )
    elevation = provisioning.get("elevation", {})
    if elevation.get("statement") != ELEVATION_STATEMENT:
        problems.append(f"the declared elevation must be {ELEVATION_STATEMENT!r}")
    for field in ("leaks_after_commit", "leaks_after_rollback", "manual_reset_issued"):
        if elevation.get(field) is not False:
            problems.append(f"the elevation contract must declare {field} false")
    if "raises BEFORE the first CREATE, ALTER or GRANT" not in provisioning.get("failure_mode", ""):
        problems.append("0009 must fail before its first DDL or GRANT when a role is incompatible")
    if provisioning.get("roles_altered_by_0009") != []:
        problems.append("migration 0009 must alter no cluster role")
    if provisioning.get("roles_dropped_by_0009") != []:
        problems.append("migration 0009 must drop no cluster role")
    attributes = provisioning.get("role_attribute_contract", {})
    if set(attributes) != set(REQUIRED_ROLES):
        problems.append("the role attribute contract does not cover exactly the required roles")
    for role, declared in sorted(attributes.items()):
        for attribute in ("superuser", "createrole", "createdb"):
            if declared.get(attribute) is not False:
                problems.append(f"{role} must not hold {attribute.upper()}")
    if attributes.get(DEFINER_PRINCIPAL, {}).get("login") is not False:
        problems.append(f"{DEFINER_PRINCIPAL} must be NOLOGIN")
    if DEFINER_PRINCIPAL not in attributes.get(MIGRATION_ROLE, {}).get("member_of", []):
        problems.append(f"{MIGRATION_ROLE} must be a member of {DEFINER_PRINCIPAL}")
    downgrade = provisioning.get("downgrade_rule", "")
    if "NEVER drops" not in downgrade:
        problems.append("a downgrade must never drop a cluster role")
    if not provisioning.get("scratch_test_rule"):
        problems.append("scratch tests must provision the required roles before Alembic")
    if not provisioning.get("operational_provisioning"):
        problems.append("a separate operational provisioning step must be declared")
    if "No password" not in provisioning.get("no_credentials", ""):
        problems.append("the provisioning contract must forbid credentials in migrations")
    return problems


def _validate_physical_deployment(
    reports: Path, inventory: Any, target_tables: set[str]
) -> list[str]:
    """Validate the D1.1 physical-deployment contract against the logical contract and V1."""
    problems: list[str] = []
    path = reports / "MINOS_DATABASE_V2_PHYSICAL_DEPLOYMENT.json"
    if not path.is_file():
        return [f"missing report: {path.name}"]
    try:
        doc = load_strict(path)
    except ValueError as exc:
        return [f"{path.name}: {exc}"]

    # its own hash must recompute
    recomputed = contract_hash(doc)
    if doc.get(CONTRACT_HASH_FIELD) != recomputed:
        problems.append(
            f"physical-deployment hash mismatch: stored {doc.get(CONTRACT_HASH_FIELD)!r} "
            f"!= recomputed {recomputed!r}"
        )

    mapping = doc.get("deployment_mapping", [])
    logical_seen: dict[str, int] = {}
    physical_seen: dict[str, int] = {}
    for entry in mapping:
        logical_seen[entry["logical_table"]] = logical_seen.get(entry["logical_table"], 0) + 1
        physical_seen[entry["d2_physical_table"]] = (
            physical_seen.get(entry["d2_physical_table"], 0) + 1
        )

    # every logical table has exactly one deployment mapping, and vice versa
    for ident in sorted(target_tables):
        if logical_seen.get(ident, 0) != 1:
            problems.append(f"logical table mapped {logical_seen.get(ident, 0)} times: {ident}")
    for ident in sorted(set(logical_seen) - target_tables):
        problems.append(f"deployment maps a table the contract does not declare: {ident}")

    # no two logical tables share a physical table
    for ident, count in sorted(physical_seen.items()):
        if count != 1:
            problems.append(f"physical table claimed by {count} logical tables: {ident}")

    # no shadow table collides with the frozen V1 inventory
    v1 = {
        f"{t['schema']}.{t['name']}" for t in inventory["live"]["tables"] if t["kind"] in {"r", "v"}
    }
    for ident in doc.get("physical_shadow_tables", []):
        if ident in v1:
            problems.append(f"shadow table collides with a V1 relation: {ident}")
        schema = ident.split(".", 1)[0]
        if not schema.startswith(SHADOW_SCHEMA_PREFIX):
            problems.append(f"shadow table is not in the dbv2_ namespace: {ident}")

    # the shared Alembic table is shared, never shadowed
    shared = doc.get("shared_table")
    if shared != SHARED_ALEMBIC_TABLE:
        problems.append(f"shared table must be {SHARED_ALEMBIC_TABLE}, got {shared!r}")
    if shared in doc.get("physical_shadow_tables", []):
        problems.append("the shared Alembic table must not be duplicated into a shadow schema")

    # counts are internally consistent
    counts: dict[str, Any] = doc.get("counts", {})
    actual = {
        "logical_tables": len(doc.get("logical_tables", [])),
        "physical_shadow_tables": len(doc.get("physical_shadow_tables", [])),
        "shared_tables": 1,
        "d2_tables_created": len(doc.get("physical_shadow_tables", [])),
    }
    if counts != actual:
        problems.append(f"counts {counts} != actual {actual}")
    if actual["logical_tables"] != actual["physical_shadow_tables"] + 1:
        problems.append(
            f"{actual['logical_tables']} logical tables must be "
            f"{actual['physical_shadow_tables']} shadow + 1 shared"
        )

    # every foreign-key target translates consistently through the same schema map
    schema_map: dict[str, str] = doc.get("schema_mapping", {}).get("canonical_to_shadow", {})
    physical_by_logical = {e["logical_table"]: e["d2_physical_table"] for e in mapping}
    for entry in mapping:
        logical, physical = entry["logical_table"], entry["d2_physical_table"]
        if entry.get("disposition") == "shared":
            continue
        canon_schema, table = logical.split(".", 1)
        expected = f"{schema_map.get(canon_schema)}.{table}"
        if physical != expected:
            problems.append(f"{logical}: physical target {physical} != mapped {expected}")
        if entry.get("post_cutover_table") != logical:
            problems.append(
                f"{logical}: post-cutover target {entry.get('post_cutover_table')!r} must equal "
                "the logical identity"
            )
    del physical_by_logical

    # the revision path is exactly the frozen one
    revision: dict[str, Any] = doc.get("revision_path", {})
    actual_path = tuple(revision.get("operational_preparation_path", []))
    if actual_path != EXPECTED_REVISION_PATH:
        problems.append(f"revision path {actual_path} != {EXPECTED_REVISION_PATH}")
    if revision.get("source_revision") != EXPECTED_REVISION_PATH[0]:
        problems.append("source_revision must be the current operational revision")
    if revision.get("planned_d2_down_revision") != EXPECTED_REVISION_PATH[-2]:
        problems.append("the planned D2 revision must descend from 0008")
    if revision.get("authorized_in_d1_1") is not False:
        problems.append("no operational migration may be authorized in D1.1")

    # cutover and rollback are exact inverses
    cutover: dict[str, Any] = {
        s["action"]: s.get("mapping") for s in doc.get("cutover_mapping", {}).get("steps", [])
    }
    rollback: dict[str, Any] = {
        s["action"]: s.get("mapping") for s in doc.get("rollback_mapping", {}).get("steps", [])
    }
    forward: dict[str, str] = {}
    for name in ("rename_v1_to_retired", "rename_shadow_to_canonical"):
        forward.update(cutover.get(name) or {})
    backward: dict[str, str] = {}
    for name in ("rename_canonical_back_to_shadow", "rename_retired_back_to_canonical"):
        backward.update(rollback.get(name) or {})
    if not forward or not backward:
        problems.append("cutover or rollback declares no schema rename mapping")
    else:
        composed = {src: backward.get(dst) for src, dst in forward.items()}
        for src, result in sorted(composed.items()):
            if result != src:
                problems.append(f"rollback is not the inverse of cutover for {src}: -> {result}")
        if set(backward) != set(forward.values()):
            problems.append("rollback does not cover exactly the schemas cutover renames")

    problems.extend(_validate_recovery_and_retirement(doc))

    # no forbidden shortcut may be described as part of the plan
    forbidden = doc.get("forbidden_migration_shortcuts", [])
    for required in ("stamp", "skipping", "rewriting", "multiple-head"):
        if not any(required in item.lower() for item in forbidden):
            problems.append(f"forbidden_migration_shortcuts does not name {required!r}")
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="DB-V2 D1 audit and report validator")
    sub = parser.add_subparsers(dest="command", required=True)

    inv = sub.add_parser("inventory", help="emit the V1 inventory (read-only introspection)")
    inv.add_argument("--database-url", required=True, help="read-only DSN (never recorded)")
    inv.add_argument("--out", required=True, type=Path)

    ch = sub.add_parser("contract-hash", help="recompute a contract's canonical hash")
    ch.add_argument("--contract", required=True, type=Path)
    ch.add_argument("--write", action="store_true", help="write the hash back into the document")

    val = sub.add_parser("validate", help="strictly validate and cross-reference the reports")
    val.add_argument("--reports", type=Path, default=REPO_ROOT / "reports" / "database")

    args = parser.parse_args(argv)

    if args.command == "inventory":
        payload = {
            "report": "MINOS_DATABASE_V1_INVENTORY",
            "schema_version": "minos-db-v1-inventory-v1",
            "live": introspect(args.database_url),
            "migrations": scan_migrations(REPO_ROOT),
            "source_access": scan_source(REPO_ROOT),
        }
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_bytes(
            json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False).encode("utf-8")
            + b"\n"
        )
        live = cast("dict[str, Any]", payload["live"])
        print(
            f"inventory: {len(live['tables'])} relations, {len(live['columns'])} columns, "
            f"{len(live['constraints'])} constraints, {len(live['indexes'])} indexes, "
            f"{len(live['functions'])} functions -> {args.out}"
        )
        return 0

    if args.command == "contract-hash":
        document = load_strict(args.contract)
        digest = contract_hash(document)
        if args.write:
            document[CONTRACT_HASH_FIELD] = digest
            args.contract.write_bytes(
                json.dumps(document, indent=2, sort_keys=True, ensure_ascii=False).encode("utf-8")
                + b"\n"
            )
        print(digest)
        return 0

    problems = validate(args.reports, REPO_ROOT)
    for problem in problems:
        print(f"FAIL: {problem}", file=sys.stderr)
    print(f"validate: {len(problems)} problem(s)")
    return 1 if problems else 0


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    raise SystemExit(main())
