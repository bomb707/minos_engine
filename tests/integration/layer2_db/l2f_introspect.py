"""Reusable read-only normalized PostgreSQL introspector for F3-A (scratch DB only).

Returns deterministic, canonical (JSON-serialisable, sorted) structures built entirely from
``pg_catalog`` + ACL expansion (``aclexplode``) — never ``information_schema`` grant views,
whose PUBLIC handling is lossy. Used to (1) generate the frozen static F3-A inventory once for
owner review, (2) prove live 0006 equals that frozen inventory, and (3) capture the exact
0005 structural/security state for the populated lifecycle.

Every function takes a SQLAlchemy ``Connection`` on an ephemeral scratch database.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import Connection, text

# ---- FK/constraint code decoders (pg_constraint) ----
_MATCH = {"s": "SIMPLE", "f": "FULL", "p": "PARTIAL"}
_ACTION = {"a": "NO ACTION", "r": "RESTRICT", "c": "CASCADE", "n": "SET NULL", "d": "SET DEFAULT"}
_CONTYPE = {"p": "PRIMARY KEY", "u": "UNIQUE", "c": "CHECK", "f": "FOREIGN KEY", "x": "EXCLUDE"}
_PERSISTENCE = {"p": "permanent", "u": "unlogged", "t": "temporary"}
_VOLATILITY = {"i": "immutable", "s": "stable", "v": "volatile"}
_PARALLEL = {"s": "safe", "r": "restricted", "u": "unsafe"}


def _rows(conn: Connection, sql: str, **p: Any) -> list[dict[str, Any]]:
    return [dict(r._mapping) for r in conn.execute(text(sql), p).all()]


def _acl(conn: Connection, acl_expr: str, from_where: str, **p: Any) -> list[dict[str, Any]]:
    """Explode an aclitem[] into canonical {grantor, grantee, privilege, grantable} rows.

    ``grantee = 0`` becomes the literal ``PUBLIC``. A NULL acl (owner-implicit default) yields
    an empty list; the ``acl_is_default`` flag distinguishes that from an explicit empty acl.
    """
    sql = (
        "SELECT COALESCE(pg_get_userbyid(NULLIF((x).grantor, 0)), '') AS grantor, "
        "CASE WHEN (x).grantee = 0 THEN 'PUBLIC' ELSE pg_get_userbyid((x).grantee) END AS grantee, "
        "(x).privilege_type AS privilege, (x).is_grantable AS grantable "
        f"FROM (SELECT aclexplode({acl_expr}) AS x {from_where}) s "
        "ORDER BY grantee, privilege, grantor"
    )
    return _rows(conn, sql, **p)


def _column_map(conn: Connection, schema: str, table: str) -> dict[int, str]:
    rows = _rows(
        conn,
        "SELECT a.attnum, a.attname FROM pg_attribute a "
        "JOIN pg_class c ON c.oid = a.attrelid JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE n.nspname = :s AND c.relname = :t AND a.attnum > 0 AND NOT a.attisdropped",
        s=schema,
        t=table,
    )
    return {int(r["attnum"]): r["attname"] for r in rows}


# --------------------------------------------------------------------------- #
# tables + columns
# --------------------------------------------------------------------------- #
def introspect_table(conn: Connection, schema: str, table: str) -> dict[str, Any]:
    meta = _rows(
        conn,
        "SELECT pg_get_userbyid(c.relowner) AS owner, c.relpersistence AS persistence, "
        "c.relrowsecurity AS rowsecurity, c.relacl IS NULL AS acl_is_default "
        "FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE n.nspname = :s AND c.relname = :t",
        s=schema,
        t=table,
    )[0]
    cols = _rows(
        conn,
        "SELECT a.attnum AS position, a.attname AS name, "
        "format_type(a.atttypid, a.atttypmod) AS type, a.attnotnull AS notnull, "
        "pg_get_expr(d.adbin, d.adrelid) AS default, "
        "NULLIF(a.attidentity, '') AS identity, NULLIF(a.attgenerated, '') AS generated, "
        "co.collname AS collation "
        "FROM pg_attribute a JOIN pg_class c ON c.oid = a.attrelid "
        "JOIN pg_namespace n ON n.oid = c.relnamespace "
        "LEFT JOIN pg_attrdef d ON d.adrelid = a.attrelid AND d.adnum = a.attnum "
        "LEFT JOIN pg_collation co ON co.oid = a.attcollation AND co.collname <> 'default' "
        "WHERE n.nspname = :s AND c.relname = :t AND a.attnum > 0 AND NOT a.attisdropped "
        "ORDER BY a.attnum",
        s=schema,
        t=table,
    )
    return {
        "owner": meta["owner"],
        "persistence": _PERSISTENCE[meta["persistence"]],
        "rowsecurity": bool(meta["rowsecurity"]),
        "acl_is_default": bool(meta["acl_is_default"]),
        "acl": _acl(
            conn, "relacl", "FROM pg_class WHERE oid = to_regclass(:q)", q=f"{schema}.{table}"
        ),
        "columns": [
            {
                "position": int(c["position"]),
                "name": c["name"],
                "type": c["type"],
                "notnull": bool(c["notnull"]),
                "default": c["default"],
                "identity": c["identity"],
                "generated": c["generated"],
                "collation": c["collation"],
            }
            for c in cols
        ],
    }


# --------------------------------------------------------------------------- #
# constraints
# --------------------------------------------------------------------------- #
def introspect_constraints(conn: Connection, tables: list[tuple[str, str]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for schema, table in tables:
        colmap = _column_map(conn, schema, table)
        rows = _rows(
            conn,
            "SELECT c.conname AS name, c.contype AS contype, c.conkey AS conkey, "
            "c.confkey AS confkey, rn.nspname AS ref_schema, rc.relname AS ref_table, "
            "c.confrelid AS confrelid, c.confmatchtype AS matchtype, c.confupdtype AS updtype, "
            "c.confdeltype AS deltype, c.condeferrable AS deferrable, c.condeferred AS deferred, "
            "c.convalidated AS validated, pg_get_constraintdef(c.oid, true) AS definition "
            "FROM pg_constraint c JOIN pg_class t ON t.oid = c.conrelid "
            "JOIN pg_namespace n ON n.oid = t.relnamespace "
            "LEFT JOIN pg_class rc ON rc.oid = c.confrelid "
            "LEFT JOIN pg_namespace rn ON rn.oid = rc.relnamespace "
            "WHERE n.nspname = :s AND t.relname = :t "
            "ORDER BY c.conname",
            s=schema,
            t=table,
        )
        for r in rows:
            local = [colmap[int(k)] for k in (r["conkey"] or [])]
            ref_cols: list[str] = []
            if r["confrelid"]:
                refmap = _column_map(conn, r["ref_schema"], r["ref_table"])
                ref_cols = [refmap[int(k)] for k in (r["confkey"] or [])]
            entry: dict[str, Any] = {
                "schema": schema,
                "table": table,
                "name": r["name"],
                "type": _CONTYPE[r["contype"]],
                "columns": local,
                "definition": r["definition"],
            }
            if r["contype"] == "f":
                entry.update(
                    {
                        "referred_schema": r["ref_schema"],
                        "referred_table": r["ref_table"],
                        "referred_columns": ref_cols,
                        "match": _MATCH[r["matchtype"]],
                        "on_update": _ACTION[r["updtype"]],
                        "on_delete": _ACTION[r["deltype"]],
                        "deferrable": bool(r["deferrable"]),
                        "deferred": bool(r["deferred"]),
                        "validated": bool(r["validated"]),
                    }
                )
            out.append(entry)
    out.sort(key=lambda e: (e["schema"], e["table"], e["name"]))
    return out


# --------------------------------------------------------------------------- #
# indexes
# --------------------------------------------------------------------------- #
def introspect_indexes(conn: Connection, tables: list[tuple[str, str]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for schema, table in tables:
        rows = _rows(
            conn,
            "SELECT ic.relname AS name, am.amname AS method, ix.indisunique AS unique, "
            "ix.indisprimary AS primary, ix.indisexclusion AS exclusion, "
            "pg_get_expr(ix.indpred, ix.indrelid) AS predicate, "
            "pg_get_indexdef(ix.indexrelid) AS definition "
            "FROM pg_index ix JOIN pg_class ic ON ic.oid = ix.indexrelid "
            "JOIN pg_class tc ON tc.oid = ix.indrelid "
            "JOIN pg_namespace n ON n.oid = tc.relnamespace "
            "JOIN pg_am am ON am.oid = ic.relam "
            "WHERE n.nspname = :s AND tc.relname = :t "
            "ORDER BY ic.relname",
            s=schema,
            t=table,
        )
        for r in rows:
            cols = _rows(
                conn,
                "SELECT pg_get_indexdef(ix.indexrelid, k.i, true) AS keydef "
                "FROM pg_index ix JOIN pg_class ic ON ic.oid = ix.indexrelid "
                "CROSS JOIN generate_series(1, ix.indnatts) AS k(i) "
                "WHERE ic.relname = :name ORDER BY k.i",
                name=r["name"],
            )
            out.append(
                {
                    "schema": schema,
                    "table": table,
                    "name": r["name"],
                    "method": r["method"],
                    "unique": bool(r["unique"]),
                    "primary": bool(r["primary"]),
                    "exclusion": bool(r["exclusion"]),
                    "predicate": r["predicate"],
                    "key_definitions": [c["keydef"] for c in cols],
                    "definition": r["definition"],
                }
            )
    out.sort(key=lambda e: (e["schema"], e["table"], e["name"]))
    return out


# --------------------------------------------------------------------------- #
# triggers
# --------------------------------------------------------------------------- #
def introspect_triggers(
    conn: Connection, tables: list[tuple[str, str]], *, include_internal: bool = False
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for schema, table in tables:
        rows = _rows(
            conn,
            "SELECT tg.tgname AS name, tg.tgenabled AS enabled, tg.tgisinternal AS internal, "
            "fn.nspname AS fn_schema, p.proname AS fn_name, "
            "pg_get_triggerdef(tg.oid, true) AS definition "
            "FROM pg_trigger tg JOIN pg_class c ON c.oid = tg.tgrelid "
            "JOIN pg_namespace n ON n.oid = c.relnamespace "
            "JOIN pg_proc p ON p.oid = tg.tgfoid JOIN pg_namespace fn ON fn.oid = p.pronamespace "
            "WHERE n.nspname = :s AND c.relname = :t "
            "AND (:inc OR NOT tg.tgisinternal) ORDER BY tg.tgname",
            s=schema,
            t=table,
            inc=include_internal,
        )
        for r in rows:
            out.append(
                {
                    "schema": schema,
                    "table": table,
                    "name": r["name"],
                    "enabled": r["enabled"],
                    "internal": bool(r["internal"]),
                    "function": f"{r['fn_schema']}.{r['fn_name']}",
                    "definition": r["definition"],
                }
            )
    out.sort(key=lambda e: (e["schema"], e["table"], e["name"]))
    return out


# --------------------------------------------------------------------------- #
# functions
# --------------------------------------------------------------------------- #
def introspect_functions(conn: Connection, funcs: list[tuple[str, str]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for schema, name in funcs:
        rows = _rows(
            conn,
            "SELECT p.oid AS oid, pg_get_function_identity_arguments(p.oid) AS args, "
            "pg_get_function_result(p.oid) AS result, l.lanname AS language, "
            "pg_get_userbyid(p.proowner) AS owner, p.prosecdef AS secdef, "
            "p.provolatile AS volatility, p.proisstrict AS strict, p.proparallel AS parallel, "
            "p.proconfig AS config, md5(p.prosrc) AS body_md5 "
            "FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace "
            "JOIN pg_language l ON l.oid = p.prolang "
            "WHERE n.nspname = :s AND p.proname = :n ORDER BY p.oid",
            s=schema,
            n=name,
        )
        for r in rows:
            acl = _acl(
                conn,
                "proacl",
                "FROM pg_proc WHERE oid = :oid",
                oid=r["oid"],
            )
            acl_default = _rows(
                conn, "SELECT proacl IS NULL AS d FROM pg_proc WHERE oid = :oid", oid=r["oid"]
            )[0]["d"]
            out.append(
                {
                    "schema": schema,
                    "name": name,
                    "identity_arguments": r["args"],
                    "result_type": r["result"],
                    "language": r["language"],
                    "owner": r["owner"],
                    "security_definer": bool(r["secdef"]),
                    "volatility": _VOLATILITY[r["volatility"]],
                    "strict": bool(r["strict"]),
                    "parallel": _PARALLEL[r["parallel"]],
                    "config": list(r["config"]) if r["config"] else None,
                    "body_md5": r["body_md5"],
                    "acl_is_default": bool(acl_default),
                    "acl": acl,
                }
            )
    out.sort(key=lambda e: (e["schema"], e["name"], e["identity_arguments"]))
    return out


# --------------------------------------------------------------------------- #
# security: schemas, roles, memberships, default acls
# --------------------------------------------------------------------------- #
def introspect_schema_security(conn: Connection, schemas: list[str]) -> list[dict[str, Any]]:
    out = []
    for schema in sorted(schemas):
        meta = _rows(
            conn,
            "SELECT pg_get_userbyid(nspowner) AS owner FROM pg_namespace WHERE nspname = :s",
            s=schema,
        )
        if not meta:
            continue
        out.append(
            {
                "schema": schema,
                "owner": meta[0]["owner"],
                "acl": _acl(conn, "nspacl", "FROM pg_namespace WHERE nspname = :s", s=schema),
            }
        )
    return out


def introspect_roles(conn: Connection, roles: list[str]) -> list[dict[str, Any]]:
    return _rows(
        conn,
        "SELECT rolname AS name, rolsuper AS super, rolinherit AS inherit, "
        "rolcanlogin AS canlogin FROM pg_roles WHERE rolname = ANY(:r) ORDER BY rolname",
        r=sorted(roles),
    )


def introspect_role_memberships(conn: Connection, roles: list[str]) -> list[dict[str, Any]]:
    # PG16: pg_auth_members exposes admin_option, inherit_option, set_option.
    return _rows(
        conn,
        "SELECT pg_get_userbyid(m.roleid) AS role, pg_get_userbyid(m.member) AS member, "
        "pg_get_userbyid(m.grantor) AS grantor, m.admin_option AS admin_option, "
        "m.inherit_option AS inherit_option, m.set_option AS set_option "
        "FROM pg_auth_members m "
        "WHERE pg_get_userbyid(m.roleid) = ANY(:r) OR pg_get_userbyid(m.member) = ANY(:r) "
        "ORDER BY role, member, grantor",
        r=sorted(roles),
    )


def introspect_default_acls(conn: Connection, schemas: list[str]) -> list[dict[str, Any]]:
    rows = _rows(
        conn,
        "SELECT COALESCE(n.nspname, '') AS schema, pg_get_userbyid(d.defaclrole) AS owner_role, "
        "d.defaclobjtype AS objtype, d.oid AS oid FROM pg_default_acl d "
        "LEFT JOIN pg_namespace n ON n.oid = d.defaclnamespace "
        "WHERE n.nspname = ANY(:s) OR n.nspname IS NULL ORDER BY schema, owner_role, objtype",
        s=sorted(schemas),
    )
    out = []
    for r in rows:
        acl = _acl(conn, "defaclacl", "FROM pg_default_acl WHERE oid = :oid", oid=r["oid"])
        out.append(
            {
                "schema": r["schema"],
                "owner_role": r["owner_role"],
                "objtype": r["objtype"],
                "acl": acl,
            }
        )
    return out


# --------------------------------------------------------------------------- #
# L2-F scoped assembly (matches the frozen static inventory) + full-state capture
# --------------------------------------------------------------------------- #
L2F_OWNED = [
    ("experiments", "l2f_experiment_plans"),
    ("experiments", "l2f_experiment_plan_members"),
    ("experiments", "l2f_config_payloads"),
    ("experiments", "l2f_experiment_plan_configs"),
    ("experiments", "l2f_experiment_jobs"),
]
L2F_COMPOSITE_TARGET_TABLES = [
    ("profiling", "feature_matrices"),
    ("profiling", "profile_snapshots"),
    ("profiling", "feature_sets"),
    ("profiling", "profile_snapshot_members"),
    ("profiling", "feature_matrix_members"),
    ("catalog", "artifacts"),
]
_L2F_COMPOSITE_TARGET_NAMES = {
    "uq_l2f_feature_matrices_composite",
    "uq_l2f_profile_snapshots_composite",
    "uq_l2f_feature_sets_composite",
    "uq_l2f_psm_composite",
    "uq_l2f_fmm_composite",
    "uq_l2f_artifacts_id_sha_media",
}
L2F_JOB_FUNCTION = ("experiments", "minos_l2f_reject_job_identity_change")
_APP_ROLES = ("minos_live", "minos_runner", "minos_trainer", "minos_evaluator")


def l2f_live_inventory(conn: Connection) -> dict[str, Any]:
    """Assemble the canonical, exhaustive L2-F schema inventory from a live 0006 database.

    Deterministic and comparable to (and hashed identically to) the frozen static inventory.
    """
    owned_tables = {f"{s}.{t}": introspect_table(conn, s, t) for s, t in L2F_OWNED}
    composite_targets = [
        c
        for c in introspect_constraints(conn, L2F_COMPOSITE_TARGET_TABLES)
        if c["name"] in _L2F_COMPOSITE_TARGET_NAMES
    ]
    # exact-grant assertion: no owned table grants any privilege to an application role.
    no_app_role_grants = all(
        entry["grantee"] not in _APP_ROLES
        for table in owned_tables.values()
        for entry in table["acl"]
    )
    return {
        "owned_tables": owned_tables,
        "owned_constraints": introspect_constraints(conn, L2F_OWNED),
        "composite_targets": composite_targets,
        "owned_indexes": introspect_indexes(conn, L2F_OWNED),
        "owned_triggers": introspect_triggers(conn, L2F_OWNED),
        "job_function": introspect_functions(conn, [L2F_JOB_FUNCTION]),
        "experiments_schema": introspect_schema_security(conn, ["experiments"]),
        "no_app_role_grants": no_app_role_grants,
    }


def full_structural_state(conn: Connection, schemas: list[str], roles: list[str]) -> dict[str, Any]:
    """Exhaustive normalized structure/security capture for the populated lifecycle test."""
    tables = _rows(
        conn,
        "SELECT n.nspname AS schema, c.relname AS tbl FROM pg_class c "
        "JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE c.relkind = 'r' AND n.nspname = ANY(:s) ORDER BY n.nspname, c.relname",
        s=sorted(schemas),
    )
    tbl_list = [(r["schema"], r["tbl"]) for r in tables]
    funcs = _rows(
        conn,
        "SELECT n.nspname AS schema, p.proname AS name FROM pg_proc p "
        "JOIN pg_namespace n ON n.oid = p.pronamespace "
        "WHERE n.nspname = ANY(:s) ORDER BY n.nspname, p.proname",
        s=sorted([*schemas, "audit"]),
    )
    fn_list = sorted({(r["schema"], r["name"]) for r in funcs})
    return {
        "tables": {f"{s}.{t}": introspect_table(conn, s, t) for s, t in tbl_list},
        "constraints": introspect_constraints(conn, tbl_list),
        "indexes": introspect_indexes(conn, tbl_list),
        "triggers": introspect_triggers(conn, tbl_list),
        "functions": introspect_functions(conn, fn_list),
        "schema_security": introspect_schema_security(conn, [*schemas, "audit"]),
        "roles": introspect_roles(conn, roles),
        "role_memberships": introspect_role_memberships(conn, roles),
        "default_acls": introspect_default_acls(conn, [*schemas, "audit"]),
    }
