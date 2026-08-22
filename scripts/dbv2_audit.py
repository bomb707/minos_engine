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
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]

#: domain-separated prefix for the DB-V2 contract hash.
CONTRACT_HASH_DOMAIN = b"minos:db-v2-contract:v1\n"
#: the field the hash is written into; it is excluded from its own preimage.
CONTRACT_HASH_FIELD = "contract_sha256"


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
def validate(reports: Path) -> list[str]:
    """Cross-reference the three reports. Returns the list of problems (empty means valid)."""
    problems: list[str] = []
    inventory_path = reports / "MINOS_DATABASE_V1_INVENTORY.json"
    contract_path = reports / "MINOS_DATABASE_V2_CONTRACT.json"
    mapping_path = reports / "MINOS_DATABASE_V2_CURRENT_TO_TARGET.json"

    for path in (inventory_path, contract_path, mapping_path):
        if not path.is_file():
            problems.append(f"missing report: {path.name}")
    if problems:
        return problems

    inventory = load_strict(inventory_path)
    contract = load_strict(contract_path)
    mapping = load_strict(mapping_path)

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

    # 9) no secret-shaped material anywhere in the reports
    blob = "\n".join(
        path.read_text(encoding="utf-8") for path in (inventory_path, contract_path, mapping_path)
    )
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
        live = payload["live"]
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

    problems = validate(args.reports)
    for problem in problems:
        print(f"FAIL: {problem}", file=sys.stderr)
    print(f"validate: {len(problems)} problem(s)")
    return 1 if problems else 0


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    raise SystemExit(main())
