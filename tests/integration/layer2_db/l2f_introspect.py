"""Reusable read-only normalized PostgreSQL introspector for F3-A (scratch DB only).

Returns deterministic, canonical (JSON-serialisable, sorted) structures built entirely from
``pg_catalog`` + ACL expansion (``aclexplode`` over ``COALESCE(acl, acldefault(...))``) — never
``information_schema`` grant views, whose PUBLIC/default handling is lossy. Used to (1) generate
the frozen L2-F static inventory for owner acceptance, (2) prove live 0006 equals that frozen
inventory, and (3) capture the exact 0005 structure/security state (every MINOS schema, all
relation kinds incl. views, functions, effective ACLs, roles and database) for the populated
lifecycle.

This is **test infrastructure**, not a production module. Every function takes a SQLAlchemy
``Connection`` on an ephemeral scratch database.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import Connection, text

# ---- pg_catalog code decoders ----
_MATCH = {"s": "SIMPLE", "f": "FULL", "p": "PARTIAL"}
_ACTION = {"a": "NO ACTION", "r": "RESTRICT", "c": "CASCADE", "n": "SET NULL", "d": "SET DEFAULT"}
_CONTYPE = {"p": "PRIMARY KEY", "u": "UNIQUE", "c": "CHECK", "f": "FOREIGN KEY", "x": "EXCLUDE"}
_PERSISTENCE = {"p": "permanent", "u": "unlogged", "t": "temporary"}
_VOLATILITY = {"i": "immutable", "s": "stable", "v": "volatile"}
_PARALLEL = {"s": "safe", "r": "restricted", "u": "unsafe"}
_REPLIDENT = {"d": "default", "n": "nothing", "f": "full", "i": "index"}
_RELKIND = {
    "r": "table",
    "p": "partitioned_table",
    "v": "view",
    "m": "materialized_view",
    "S": "sequence",
    "f": "foreign_table",
}
#: acldefault object-type chars per relkind/object family.
_ACLDEFAULT_RELATION = "r"  # tables, views, matviews, partitioned + foreign tables
_ACLDEFAULT_SEQUENCE = "s"
_ACLDEFAULT_FUNCTION = "f"
_ACLDEFAULT_SCHEMA = "n"
_ACLDEFAULT_DATABASE = "d"

#: the frozen MINOS application schema set (reviewed contract data — NOT discovered dynamically).
MINOS_SCHEMAS = (
    "catalog",
    "profiling",
    "experiments",
    "evaluation",
    "models",
    "runtime",
    "audit",
)


def _rows(conn: Connection, sql: str, **p: Any) -> list[dict[str, Any]]:
    return [dict(r._mapping) for r in conn.execute(text(sql), p).all()]


def _explode(conn: Connection, acl_expr: str, from_where: str, **p: Any) -> list[dict[str, Any]]:
    """Explode an aclitem[] expression into canonical rows (grantee 0 -> literal PUBLIC)."""
    sql = (
        "SELECT COALESCE(pg_get_userbyid(NULLIF((x).grantor, 0)), '') AS grantor, "
        "CASE WHEN (x).grantee = 0 THEN 'PUBLIC' ELSE pg_get_userbyid((x).grantee) END AS grantee, "
        "(x).privilege_type AS privilege, (x).is_grantable AS grantable "
        f"FROM (SELECT aclexplode({acl_expr}) AS x {from_where}) s "
        "ORDER BY grantee, privilege, grantor"
    )
    return _rows(conn, sql, **p)


def _acl_full(
    conn: Connection, *, acl_col: str, owner_col: str, objtype: str, from_where: str, **p: Any
) -> dict[str, Any]:
    """Both the raw ACL state and the effective privileges of an object.

    ``raw`` is the exploded stored acl (empty when the acl is NULL); ``effective`` expands
    ``COALESCE(acl, acldefault(objtype, owner))`` so PostgreSQL's implicit defaults (e.g. a
    NULL function acl's PUBLIC EXECUTE) are represented. ``acl_is_default`` records whether the
    stored acl was NULL.
    """
    is_default = _rows(conn, f"SELECT ({acl_col} IS NULL) AS d {from_where}", **p)[0]["d"]
    raw = _explode(conn, acl_col, from_where, **p)
    effective = _explode(
        conn, f"COALESCE({acl_col}, acldefault('{objtype}', {owner_col}))", from_where, **p
    )
    return {"acl_is_default": bool(is_default), "acl_raw": raw, "acl_effective": effective}


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


def _columns(conn: Connection, schema: str, name: str) -> list[dict[str, Any]]:
    cols = _rows(
        conn,
        "SELECT a.attnum AS position, a.attname AS name, "
        "format_type(a.atttypid, a.atttypmod) AS type, a.attnotnull AS notnull, "
        "pg_get_expr(d.adbin, d.adrelid) AS default, "
        "NULLIF(a.attidentity, '') AS identity, NULLIF(a.attgenerated, '') AS generated, "
        "co.collname AS collation, a.attacl IS NOT NULL AS has_col_acl "
        "FROM pg_attribute a JOIN pg_class c ON c.oid = a.attrelid "
        "JOIN pg_namespace n ON n.oid = c.relnamespace "
        "LEFT JOIN pg_attrdef d ON d.adrelid = a.attrelid AND d.adnum = a.attnum "
        "LEFT JOIN pg_collation co ON co.oid = a.attcollation AND co.collname <> 'default' "
        "WHERE n.nspname = :s AND c.relname = :t AND a.attnum > 0 AND NOT a.attisdropped "
        "ORDER BY a.attnum",
        s=schema,
        t=name,
    )
    return [
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
    ]


def _column_acls(conn: Connection, schema: str, name: str) -> list[dict[str, Any]]:
    """Explicit per-column ACLs (columns whose attacl is non-NULL)."""
    named = _rows(
        conn,
        "SELECT a.attname FROM pg_attribute a JOIN pg_class c ON c.oid = a.attrelid "
        "JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE n.nspname = :s AND c.relname = :t AND a.attnum > 0 AND NOT a.attisdropped "
        "AND a.attacl IS NOT NULL ORDER BY a.attname",
        s=schema,
        t=name,
    )
    out = []
    for r in named:
        acl = _explode(
            conn,
            "a.attacl",
            "FROM pg_attribute a JOIN pg_class c ON c.oid = a.attrelid "
            "JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = :s AND c.relname = :t AND a.attname = :col",
            s=schema,
            t=name,
            col=r["attname"],
        )
        out.append({"column": r["attname"], "acl": acl})
    return out


def _rls_policies(conn: Connection, schema: str, table: str) -> list[dict[str, Any]]:
    return _rows(
        conn,
        "SELECT pol.polname AS name, pol.polcmd AS command, pol.polpermissive AS permissive, "
        "pg_get_expr(pol.polqual, pol.polrelid) AS using_expr, "
        "pg_get_expr(pol.polwithcheck, pol.polrelid) AS check_expr "
        "FROM pg_policy pol JOIN pg_class c ON c.oid = pol.polrelid "
        "JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE n.nspname = :s AND c.relname = :t ORDER BY pol.polname",
        s=schema,
        t=table,
    )


def _reloptions(conn: Connection, schema: str, name: str) -> list[str] | None:
    r = _rows(
        conn,
        "SELECT c.reloptions AS opts FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE n.nspname = :s AND c.relname = :t",
        s=schema,
        t=name,
    )[0]
    return list(r["opts"]) if r["opts"] else None


# --------------------------------------------------------------------------- #
# tables + views (relations)
# --------------------------------------------------------------------------- #
def introspect_table(conn: Connection, schema: str, table: str) -> dict[str, Any]:
    meta = _rows(
        conn,
        "SELECT c.relkind AS relkind, pg_get_userbyid(c.relowner) AS owner, "
        "c.relpersistence AS persistence, c.relrowsecurity AS rowsecurity, "
        "c.relforcerowsecurity AS rowsecurity_forced, c.relreplident AS replident "
        "FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE n.nspname = :s AND c.relname = :t",
        s=schema,
        t=table,
    )[0]
    where = "FROM pg_class WHERE oid = to_regclass(:q)"
    acl = _acl_full(
        conn,
        acl_col="relacl",
        owner_col="relowner",
        objtype=_ACLDEFAULT_RELATION,
        from_where=where,
        q=f"{schema}.{table}",
    )
    return {
        "kind": _RELKIND[meta["relkind"]],
        "owner": meta["owner"],
        "persistence": _PERSISTENCE[meta["persistence"]],
        "rowsecurity": bool(meta["rowsecurity"]),
        "rowsecurity_forced": bool(meta["rowsecurity_forced"]),
        "replica_identity": _REPLIDENT[meta["replident"]],
        "reloptions": _reloptions(conn, schema, table),
        "rls_policies": _rls_policies(conn, schema, table),
        "column_acls": _column_acls(conn, schema, table),
        **acl,
        "columns": _columns(conn, schema, table),
    }


def introspect_view(conn: Connection, schema: str, name: str) -> dict[str, Any]:
    meta = _rows(
        conn,
        "SELECT c.relkind AS relkind, pg_get_userbyid(c.relowner) AS owner, "
        "pg_get_viewdef(c.oid, true) AS definition FROM pg_class c "
        "JOIN pg_namespace n ON n.oid = c.relnamespace WHERE n.nspname = :s AND c.relname = :t",
        s=schema,
        t=name,
    )[0]
    reloptions = _reloptions(conn, schema, name)
    opts = reloptions or []
    security_barrier = any(o == "security_barrier=true" for o in opts)
    check_option = next((o.split("=", 1)[1] for o in opts if o.startswith("check_option=")), "none")
    where = "FROM pg_class WHERE oid = to_regclass(:q)"
    acl = _acl_full(
        conn,
        acl_col="relacl",
        owner_col="relowner",
        objtype=_ACLDEFAULT_RELATION,
        from_where=where,
        q=f"{schema}.{name}",
    )
    return {
        "kind": _RELKIND[meta["relkind"]],
        "owner": meta["owner"],
        "reloptions": reloptions,
        "security_barrier": security_barrier,
        "check_option": check_option,
        "definition": meta["definition"],
        **acl,
        "columns": _columns(conn, schema, name),
    }


def introspect_relation(conn: Connection, schema: str, name: str, relkind: str) -> dict[str, Any]:
    if relkind in ("v", "m"):
        return introspect_view(conn, schema, name)
    return introspect_table(conn, schema, name)


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
# indexes (keyed by index OID — correct across identically-named indexes in two schemas)
# --------------------------------------------------------------------------- #
def introspect_indexes(conn: Connection, tables: list[tuple[str, str]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for schema, table in tables:
        rows = _rows(
            conn,
            "SELECT ic.oid AS indexrelid, ic.relname AS name, am.amname AS method, "
            "ix.indisunique AS unique, ix.indisprimary AS primary, ix.indisexclusion AS exclusion, "
            "ix.indnatts AS natts, pg_get_expr(ix.indpred, ix.indrelid) AS predicate, "
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
            keys = _rows(
                conn,
                "SELECT pg_get_indexdef(:oid, k.i, true) AS keydef "
                "FROM generate_series(1, :n) AS k(i) ORDER BY k.i",
                oid=r["indexrelid"],
                n=int(r["natts"]),
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
                    "key_definitions": [k["keydef"] for k in keys],
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
            acl = _acl_full(
                conn,
                acl_col="proacl",
                owner_col="proowner",
                objtype=_ACLDEFAULT_FUNCTION,
                from_where="FROM pg_proc WHERE oid = :oid",
                oid=r["oid"],
            )
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
                    **acl,
                }
            )
    out.sort(key=lambda e: (e["schema"], e["name"], e["identity_arguments"]))
    return out


# --------------------------------------------------------------------------- #
# security: schemas, roles, memberships, default acls, database
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
        acl = _acl_full(
            conn,
            acl_col="nspacl",
            owner_col="nspowner",
            objtype=_ACLDEFAULT_SCHEMA,
            from_where="FROM pg_namespace WHERE nspname = :s",
            s=schema,
        )
        out.append({"schema": schema, "owner": meta[0]["owner"], **acl})
    return out


def introspect_roles(conn: Connection, roles: list[str]) -> list[dict[str, Any]]:
    # deliberately excludes rolpassword.
    return _rows(
        conn,
        "SELECT rolname AS name, rolsuper AS superuser, rolinherit AS inherit, "
        "rolcreaterole AS createrole, rolcreatedb AS createdb, rolcanlogin AS login, "
        "rolreplication AS replication, rolbypassrls AS bypassrls, rolconnlimit AS connlimit, "
        "rolvaliduntil::text AS valid_until FROM pg_roles WHERE rolname = ANY(:r) ORDER BY rolname",
        r=sorted(roles),
    )


def introspect_role_memberships(conn: Connection, roles: list[str]) -> list[dict[str, Any]]:
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
        acl = _explode(conn, "defaclacl", "FROM pg_default_acl WHERE oid = :oid", oid=r["oid"])
        out.append(
            {
                "schema": r["schema"],
                "owner_role": r["owner_role"],
                "objtype": r["objtype"],
                "acl": acl,
            }
        )
    return out


def introspect_database(conn: Connection, dbname: str) -> dict[str, Any]:
    acl = _acl_full(
        conn,
        acl_col="datacl",
        owner_col="datdba",
        objtype=_ACLDEFAULT_DATABASE,
        from_where="FROM pg_database WHERE datname = :d",
        d=dbname,
    )
    owner = _rows(
        conn,
        "SELECT pg_get_userbyid(datdba) AS owner FROM pg_database WHERE datname = :d",
        d=dbname,
    )[0]["owner"]
    return {"owner": owner, **acl}


def introspect_alembic_version(conn: Connection) -> str | None:
    rows = _rows(conn, "SELECT version_num FROM public.alembic_version")
    return rows[0]["version_num"] if rows else None


def _relations(conn: Connection, schemas: list[str]) -> list[tuple[str, str, str]]:
    rows = _rows(
        conn,
        "SELECT n.nspname AS schema, c.relname AS name, c.relkind AS relkind FROM pg_class c "
        "JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE c.relkind IN ('r', 'p', 'v', 'm', 'S', 'f') AND n.nspname = ANY(:s) "
        "ORDER BY n.nspname, c.relname",
        s=sorted(schemas),
    )
    return [(r["schema"], r["name"], r["relkind"]) for r in rows]


# --------------------------------------------------------------------------- #
# L2-F scoped assembly (matches the frozen static inventory)
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
    """Assemble the canonical, exhaustive L2-F schema inventory from a live 0006 database."""
    owned_tables = {f"{s}.{t}": introspect_table(conn, s, t) for s, t in L2F_OWNED}
    composite_targets = [
        c
        for c in introspect_constraints(conn, L2F_COMPOSITE_TARGET_TABLES)
        if c["name"] in _L2F_COMPOSITE_TARGET_NAMES
    ]
    # no owned table grants any EFFECTIVE privilege to an application role or PUBLIC.
    no_app_role_grants = all(
        entry["grantee"] not in (*_APP_ROLES, "PUBLIC")
        for table in owned_tables.values()
        for entry in table["acl_effective"]
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


def full_structural_state(conn: Connection, roles: list[str], *, dbname: str) -> dict[str, Any]:
    """Exhaustive normalized structure/security capture across every MINOS schema.

    Covers every relation kind (tables, partitioned tables, views, materialised views,
    sequences, foreign tables), constraints, indexes, triggers, functions, effective ACLs,
    schema security, roles, memberships, default ACLs, the database and the alembic revision.
    """
    schemas = list(MINOS_SCHEMAS)
    rels = _relations(conn, schemas)
    tables = [(s, n) for s, n, k in rels if k in ("r", "p")]
    funcs = sorted(
        {
            (r["schema"], r["name"])
            for r in _rows(
                conn,
                "SELECT n.nspname AS schema, p.proname AS name FROM pg_proc p "
                "JOIN pg_namespace n ON n.oid = p.pronamespace WHERE n.nspname = ANY(:s) "
                "ORDER BY n.nspname, p.proname",
                s=schemas,
            )
        }
    )
    return {
        "schema_set": schemas,
        "relations": {f"{s}.{n}": introspect_relation(conn, s, n, k) for s, n, k in rels},
        "constraints": introspect_constraints(conn, tables),
        "indexes": introspect_indexes(conn, tables),
        "triggers": introspect_triggers(conn, tables),
        "functions": introspect_functions(conn, funcs),
        "schema_security": introspect_schema_security(conn, schemas),
        "roles": introspect_roles(conn, roles),
        "role_memberships": introspect_role_memberships(conn, roles),
        "default_acls": introspect_default_acls(conn, schemas),
        "database": introspect_database(conn, dbname),
        "alembic_version": introspect_alembic_version(conn),
    }
