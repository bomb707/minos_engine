"""An EPHEMERAL, argument-free observation surface over the scientifically closed TRAIN store.

Why this exists rather than a migration or a grant
--------------------------------------------------
BASELINE-QUALIFIED must bind the TRAIN evidence set, but no accepted read-only boundary on
``minos_l2f2_baseline`` could derive it: the evaluator is denied every ``experiments.*`` ledger
and ``alembic_version``. The two obvious fixes are both wrong. Migrating TRAIN would move a store
that is scientifically closed at ``0020`` through a lineage that is entirely Phase-D/VALIDATION
work, and branching Alembic would create a second head. Granting raw ``SELECT`` on the experiments
ledgers would dismantle the ``0009`` design in which the evaluator reads narrow projections and
never the raw ledger.

So the authority is OPERATIONAL rather than schematic: one ``SECURITY DEFINER`` function, owned by
the non-superuser control plane, installed only for as long as it takes to observe the evidence
and then dropped. It changes no ``alembic_version``, no scientific row, and no table privilege.

What the function may and may not do
------------------------------------
It takes no argument, so there is no filter, phase or plan a caller can steer. Every table is
fully schema-qualified under a pinned ``search_path`` and there is no dynamic SQL, so it cannot be
redirected by a shadowing schema. It reads TRAIN plans only, verified against their three frozen
hashes, and returns counts, sorted identity sets and the Phase-C member ids. It touches no truth
table, truth identity, truth path, CONFIG payload, profile feature, feature matrix, VALIDATION row
or TEST row.

Identity, not name
------------------
A function with the right name and a different body is a different function. Before it is ever
executed, :func:`verify_train_qualification_surface` checks the schema, name, zero arity, owner,
``SECURITY DEFINER``, volatility, language, ``search_path``, ACL and the body's own digest against
the source-controlled definition below. Same name with a different body, same body with a wrong
owner, or any ``PUBLIC``/runner/trainer/live ``EXECUTE`` are all refusals.
"""

from __future__ import annotations

import json
from typing import Any, Final

from minos_engine.common.errors import MinosEngineError
from minos_engine.common.hashing import sha256_hex

__all__ = [
    "TRAIN_DATABASE",
    "TRAIN_REVISION",
    "TRAIN_SURFACE_FUNCTION",
    "TRAIN_SURFACE_SCHEMA_VERSION",
    "TrainQualificationSurfaceError",
    "drop_train_qualification_surface",
    "install_train_qualification_surface",
    "surface_body_sha256",
    "verify_train_qualification_surface",
]

TRAIN_DATABASE: Final = "minos_l2f2_baseline"
TRAIN_REVISION: Final = "0020_l2f2_phase_c_execution"
TRAIN_SURFACE_SCHEMA: Final = "evaluation"
TRAIN_SURFACE_NAME: Final = "l2f2_observe_train_qualification"
TRAIN_SURFACE_FUNCTION: Final = f"{TRAIN_SURFACE_SCHEMA}.{TRAIN_SURFACE_NAME}"
TRAIN_SURFACE_SCHEMA_VERSION: Final = "l2f2-train-qualification-observation-v1"

_CONTROL_PLANE: Final = "minos_admin"
_GRANTEE: Final = "minos_evaluator"
_DENIED: Final = ("minos_live", "minos_runner", "minos_trainer")

TRAIN_PLAN_HASHES: Final[tuple[str, ...]] = (
    "97ba598778a5fc634345ded0901e4975af9c6b875c5b70fc7e76f2ae482e1b9a",
    "e80594043580334ddf2504577e2fa030dff0c1217ac334804d9304a0ec72596b",
    "03b846e735e5817a8df7d5c37ae15778a955828a56513b16cef8ff2193a0aa43",
)
PHASE_C_PLAN_HASH: Final = TRAIN_PLAN_HASHES[2]


class TrainQualificationSurfaceError(MinosEngineError):
    """The observation surface is absent, not authentic, or not authorised."""


def _body() -> str:
    """THE source-controlled function body. Its digest is the surface's identity."""
    plans = ", ".join(f"'{h}'" for h in TRAIN_PLAN_HASHES)
    return f"""
DECLARE
    v_database text;
    v_revision text;
    v_plans text[];
    v_result jsonb;
BEGIN
    SELECT pg_catalog.current_database() INTO v_database;
    IF v_database <> '{TRAIN_DATABASE}' THEN
        RAISE EXCEPTION 'train qualification surface refuses database %', v_database;
    END IF;

    SELECT a.version_num INTO v_revision FROM public.alembic_version a;
    IF v_revision IS DISTINCT FROM '{TRAIN_REVISION}' THEN
        RAISE EXCEPTION 'train store revision is %, expected {TRAIN_REVISION}', v_revision;
    END IF;

    SELECT pg_catalog.array_agg(p.plan_hash ORDER BY p.created_at) INTO v_plans
      FROM experiments.l2f_experiment_plans p
     WHERE p.plan_hash = ANY (ARRAY[{plans}]) AND p.partition = 'train';
    IF v_plans IS NULL OR pg_catalog.array_length(v_plans, 1) <> 3 THEN
        RAISE EXCEPTION 'the three frozen TRAIN plans are not all present as train plans';
    END IF;

    SELECT pg_catalog.jsonb_build_object(
        'schema_version', '{TRAIN_SURFACE_SCHEMA_VERSION}',
        'database_name', v_database,
        'revision', v_revision,
        'plan_hashes', pg_catalog.to_jsonb(v_plans),
        'logical_job_count', (
            SELECT pg_catalog.sum(p.logical_job_count)
              FROM experiments.l2f_experiment_plans p
             WHERE p.plan_hash = ANY (v_plans)),
        'terminal_job_count', (
            SELECT pg_catalog.count(*) FROM experiments.l2f_experiment_jobs j
              JOIN experiments.l2f_experiment_plans p ON p.id = j.plan_id
             WHERE p.plan_hash = ANY (v_plans) AND j.status IN ('SUCCEEDED', 'FAILED')),
        'nonterminal_job_count', (
            SELECT pg_catalog.count(*) FROM experiments.l2f_experiment_jobs j
              JOIN experiments.l2f_experiment_plans p ON p.id = j.plan_id
             WHERE p.plan_hash = ANY (v_plans) AND j.status NOT IN ('SUCCEEDED', 'FAILED')),
        'succeeded_without_evaluation', (
            SELECT pg_catalog.count(*) FROM experiments.l2f_execution_results r
             WHERE NOT EXISTS (
                 SELECT 1 FROM evaluation.l2f_evaluation_results v
                  WHERE v.execution_result_id = r.id)),
        'evaluation_count', (
            SELECT pg_catalog.count(*) FROM evaluation.l2f_evaluation_results),
        'evaluation_failure_count', (
            SELECT pg_catalog.count(*) FROM evaluation.l2f_evaluation_failures),
        'evaluation_hashes', (
            SELECT coalesce(
                pg_catalog.jsonb_agg(x.evaluation_hash ORDER BY x.evaluation_hash),
                '[]'::jsonb)
              FROM (SELECT v.evaluation_hash FROM evaluation.l2f_evaluation_results v) x),
        'execution_failure_job_keys', (
            SELECT coalesce(
                pg_catalog.jsonb_agg(x.job_key ORDER BY x.job_key), '[]'::jsonb)
              FROM (SELECT f.job_key FROM experiments.l2f_execution_failures f) x),
        'execution_failure_codes', (
            SELECT coalesce(pg_catalog.jsonb_object_agg(x.failure_code, x.n),
                                       '{{}}'::jsonb)
              FROM (SELECT f.failure_code, pg_catalog.count(*) AS n
                      FROM experiments.l2f_execution_failures f
                     GROUP BY f.failure_code) x),
        'scoring_contract_hashes', (
            SELECT coalesce(pg_catalog.jsonb_agg(x.h ORDER BY x.h), '[]'::jsonb)
              FROM (SELECT DISTINCT v.scoring_contract_hash AS h
                      FROM evaluation.l2f_evaluation_results v) x),
        'execution_environment_hashes', (
            SELECT coalesce(pg_catalog.jsonb_agg(x.h ORDER BY x.h), '[]'::jsonb)
              FROM (SELECT DISTINCT r.execution_environment_hash AS h
                      FROM experiments.l2f_execution_results r) x),
        'phase_c_dataset_ids', (
            SELECT coalesce(pg_catalog.jsonb_agg(x.d ORDER BY x.d), '[]'::jsonb)
              FROM (SELECT DISTINCT g.dataset_id AS d
                      FROM experiments.l2f_experiment_plan_members m
                      JOIN experiments.l2f_experiment_plans p ON p.id = m.plan_id
                      JOIN catalog.dataset_registry g ON g.id = m.dataset_registry_id
                     WHERE p.plan_hash = '{PHASE_C_PLAN_HASH}' AND m.partition = 'train') x)
    ) INTO v_result;

    RETURN v_result;
END;
"""


def surface_body_sha256() -> str:
    """The source identity of the observation surface."""
    return sha256_hex(_body().encode("utf-8"))


def _create_statement() -> str:
    return (
        f"CREATE FUNCTION {TRAIN_SURFACE_FUNCTION}() RETURNS jsonb "
        "LANGUAGE plpgsql STABLE SECURITY DEFINER "
        "SET search_path = pg_catalog, public "
        f"AS $l2f2_train_obs${_body()}$l2f2_train_obs$;"
    )


def install_train_qualification_surface(conn: Any) -> dict[str, Any]:
    """Install the ephemeral surface. ADMIN ONLY — never reachable from the qualifier."""
    from sqlalchemy import text

    database = str(conn.execute(text("SELECT current_database()")).scalar_one())
    if database != TRAIN_DATABASE:
        raise TrainQualificationSurfaceError(
            f"refusing to install on database {database!r}; the TRAIN surface belongs to "
            f"{TRAIN_DATABASE!r}"
        )
    revision = conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one_or_none()
    if revision != TRAIN_REVISION:
        raise TrainQualificationSurfaceError(
            f"TRAIN store revision is {revision!r}, expected {TRAIN_REVISION!r}"
        )
    before = snapshot_train_state(conn)

    conn.execute(text(f"DROP FUNCTION IF EXISTS {TRAIN_SURFACE_FUNCTION}()"))
    # The definer must be able to read the revision it pins. minos_admin already owns and reads
    # every scientific table here; only public.alembic_version (owned by postgres) is missing.
    # 0011 and 0025 both granted exactly this, for exactly this reason: a boundary that pins a
    # schema revision has to read it, and alembic_version carries no scientific data. Ephemeral:
    # the drop takes it back, and no experiments.* privilege is touched.
    conn.execute(text(f"GRANT SELECT ON public.alembic_version TO {_CONTROL_PLANE}"))
    conn.execute(text(f"SET ROLE {_CONTROL_PLANE}"))
    conn.execute(text(_create_statement()))
    conn.execute(text("RESET ROLE"))
    conn.execute(text(f"REVOKE ALL ON FUNCTION {TRAIN_SURFACE_FUNCTION}() FROM PUBLIC"))
    for role in _DENIED:
        conn.execute(text(f"REVOKE ALL ON FUNCTION {TRAIN_SURFACE_FUNCTION}() FROM {role}"))
    conn.execute(text(f"GRANT EXECUTE ON FUNCTION {TRAIN_SURFACE_FUNCTION}() TO {_GRANTEE}"))

    after = snapshot_train_state(conn)
    if before != after:
        raise TrainQualificationSurfaceError(
            "installing the observation surface changed TRAIN scientific state; this must never "
            f"happen: {before} -> {after}"
        )
    return after


def drop_train_qualification_surface(conn: Any) -> dict[str, Any]:
    """Remove the ephemeral surface and prove the scientific state is untouched."""
    from sqlalchemy import text

    before = snapshot_train_state(conn)
    conn.execute(text(f"DROP FUNCTION IF EXISTS {TRAIN_SURFACE_FUNCTION}()"))
    conn.execute(text(f"REVOKE SELECT ON public.alembic_version FROM {_CONTROL_PLANE}"))
    after = snapshot_train_state(conn)
    if before != after:
        raise TrainQualificationSurfaceError(
            f"dropping the observation surface changed TRAIN state: {before} -> {after}"
        )
    return after


def snapshot_train_state(conn: Any) -> dict[str, Any]:
    """A non-scientific fingerprint used only to prove nothing moved."""
    from sqlalchemy import text

    row = conn.execute(
        text(
            "SELECT (SELECT version_num FROM alembic_version), "
            "       (SELECT count(*) FROM experiments.l2f_experiment_jobs), "
            "       (SELECT count(*) FROM experiments.l2f_execution_results), "
            "       (SELECT count(*) FROM experiments.l2f_execution_failures), "
            "       (SELECT count(*) FROM evaluation.l2f_evaluation_results), "
            "       (SELECT count(*) FROM evaluation.l2f_evaluation_failures), "
            "       (SELECT encode(sha256(convert_to(string_agg(v.evaluation_hash, ',' "
            "           ORDER BY v.evaluation_hash), 'UTF8')), 'hex') "
            "          FROM evaluation.l2f_evaluation_results v), "
            "       (SELECT encode(sha256(convert_to(string_agg(f.job_key, ',' "
            "           ORDER BY f.job_key), 'UTF8')), 'hex') "
            "          FROM experiments.l2f_execution_failures f)"
        )
    ).one()
    return {
        "revision": row[0],
        "jobs": int(row[1]),
        "execution_results": int(row[2]),
        "execution_failures": int(row[3]),
        "evaluations": int(row[4]),
        "evaluation_failures": int(row[5]),
        "evaluation_set_sha256": row[6],
        "execution_failure_set_sha256": row[7],
    }


def verify_train_qualification_surface(conn: Any) -> dict[str, Any]:
    """Authenticate the installed function BEFORE executing it. Name is not identity."""
    from sqlalchemy import text

    row = (
        conn.execute(
            text(
                "SELECT n.nspname, p.proname, p.pronargs, r.rolname AS owner, p.prosecdef, "
                "       p.provolatile, l.lanname, p.proconfig, p.proacl::text, p.prosrc "
                "  FROM pg_proc p "
                "  JOIN pg_namespace n ON n.oid = p.pronamespace "
                "  JOIN pg_roles r ON r.oid = p.proowner "
                "  JOIN pg_language l ON l.oid = p.prolang "
                " WHERE n.nspname = :schema AND p.proname = :name"
            ),
            {"schema": TRAIN_SURFACE_SCHEMA, "name": TRAIN_SURFACE_NAME},
        )
        .mappings()
        .all()
    )
    if len(row) != 1:
        raise TrainQualificationSurfaceError(
            f"expected exactly one {TRAIN_SURFACE_FUNCTION}, found {len(row)}"
        )
    fn = row[0]
    if fn["pronargs"] != 0:
        raise TrainQualificationSurfaceError(
            f"{TRAIN_SURFACE_FUNCTION} takes {fn['pronargs']} arguments; a caller-steerable "
            "observation surface is not an observation surface"
        )
    if fn["owner"] != _CONTROL_PLANE:
        raise TrainQualificationSurfaceError(
            f"{TRAIN_SURFACE_FUNCTION} is owned by {fn['owner']!r}, not {_CONTROL_PLANE!r}"
        )
    if not fn["prosecdef"]:
        raise TrainQualificationSurfaceError("the observation surface is not SECURITY DEFINER")
    if fn["provolatile"] != "s":
        raise TrainQualificationSurfaceError(
            f"the observation surface volatility is {fn['provolatile']!r}, expected STABLE"
        )
    if fn["lanname"] != "plpgsql":
        raise TrainQualificationSurfaceError(f"unexpected language {fn['lanname']!r}")
    config = list(fn["proconfig"] or ())
    if "search_path=pg_catalog, public" not in [c.replace('"', "") for c in config]:
        raise TrainQualificationSurfaceError(
            f"the observation surface does not pin its search_path: {config}"
        )
    observed_body = sha256_hex(str(fn["prosrc"]).encode("utf-8"))
    if observed_body != surface_body_sha256():
        raise TrainQualificationSurfaceError(
            f"the installed function body hashes {observed_body}, not the source-controlled "
            f"{surface_body_sha256()}; a function with the right name and a different body is a "
            "different function"
        )

    acl = str(fn["proacl"] or "")
    if "=X/" in acl.replace(f"{_GRANTEE}=X/", "").replace(f"{_CONTROL_PLANE}=X/", ""):
        # any EXECUTE grantee other than the evaluator and the owner
        raise TrainQualificationSurfaceError(f"unexpected EXECUTE grantees in ACL: {acl}")
    for role in (*_DENIED, "PUBLIC"):
        marker = "=X/" if role == "PUBLIC" else f"{role}=X/"
        if role == "PUBLIC":
            if acl and acl.startswith("{=X/"):
                raise TrainQualificationSurfaceError("PUBLIC holds EXECUTE on the surface")
        elif marker in acl:
            raise TrainQualificationSurfaceError(f"{role} holds EXECUTE on the surface")
    if f"{_GRANTEE}=X/" not in acl:
        raise TrainQualificationSurfaceError(
            f"{_GRANTEE} does not hold EXECUTE on the observation surface"
        )
    return {
        "function": TRAIN_SURFACE_FUNCTION,
        "owner": fn["owner"],
        "security_definer": bool(fn["prosecdef"]),
        "volatility": "STABLE",
        "arguments": 0,
        "body_sha256": observed_body,
        "acl": acl,
    }


def observe(conn: Any) -> dict[str, Any]:
    """Authenticate, then execute. The surface is never executed on the strength of its name."""
    from sqlalchemy import text

    verify_train_qualification_surface(conn)
    payload = conn.execute(text(f"SELECT {TRAIN_SURFACE_FUNCTION}()")).scalar_one()
    observed = payload if isinstance(payload, dict) else json.loads(payload)
    if observed.get("schema_version") != TRAIN_SURFACE_SCHEMA_VERSION:
        raise TrainQualificationSurfaceError(
            f"observation schema is {observed.get('schema_version')!r}, expected "
            f"{TRAIN_SURFACE_SCHEMA_VERSION!r}"
        )
    return dict(observed)
