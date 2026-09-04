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
BASELINE_PROTOCOL_HASH: Final = "c548e190571f5e964560cf30021a520ea8aad6674569fa3202af880d7dff77d1"
SCORING_CONTRACT_HASH: Final = "b24a07e208ce8e2fff6672102ae4e61aed93c6f352a5af46ba81c4789adb76d6"


class TrainQualificationSurfaceError(MinosEngineError):
    """The observation surface is absent, not authentic, or not authorised."""


def _body() -> str:
    """THE source-controlled function body. Its digest is the surface's identity.

    Campaigns are derived through ``experiments.l2f2_execution_authorities`` — the accepted
    execution authority that ``0020`` itself treats as authoritative — not by discovering plan
    rows that happen to carry the right hashes. Exactly one authority per phase, each under the
    frozen baseline protocol, each agreeing with its persisted plan on id, hash, partition and
    scientific shape.

    Every fact is then scoped to those three authorised ``plan_id`` values. Whole-database counts
    would silently absorb any row that appeared beside the campaign.

    The execution environment is taken from BOTH terminal ledgers. ``0015`` stores
    ``execution_environment_hash`` on results AND failures precisely because a campaign's terminal
    outcomes are both; reading only the results would leave the 35 execution failures unchecked,
    and a divergent environment on one of them would pass unnoticed.
    """
    return f"""
DECLARE
    v_database text;
    v_revision text;
    v_plan_ids uuid[];
    v_plans text[];
    v_authorities int;
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

    -- exactly three TRAIN campaigns, each proven through the execution authority.
    -- The per-phase shape is asserted individually: 195 + 480 + 500 = 1175 also holds for
    -- shapes that are wrong phase-by-phase, so an aggregate check would hide a mutation.
    -- candidate_set_hash and parameter_space_hash are part of 0020's authority binding and are
    -- required to agree too; without them an authority could cite a different candidate set.
    SELECT pg_catalog.count(*) INTO v_authorities
      FROM experiments.l2f2_execution_authorities a
      JOIN experiments.l2f_experiment_plans p ON p.id = a.plan_id
     WHERE a.baseline_protocol_hash = '{BASELINE_PROTOCOL_HASH}'
       AND p.partition = 'train'
       AND a.plan_hash = p.plan_hash
       AND a.candidate_set_hash = p.candidate_set_hash
       AND a.parameter_space_hash = p.parameter_space_hash
       AND a.member_count = p.train_member_count
       AND a.candidate_count = p.candidate_count
       AND a.logical_job_count = p.logical_job_count
       AND (a.phase, a.plan_hash, a.member_count, a.candidate_count, a.logical_job_count) IN (
             ('PHASE_A', '{TRAIN_PLAN_HASHES[0]}', 5, 39, 195),
             ('PHASE_B', '{TRAIN_PLAN_HASHES[1]}', 10, 48, 480),
             ('PHASE_C', '{TRAIN_PLAN_HASHES[2]}', 50, 10, 500));
    IF v_authorities <> 3 THEN
        RAISE EXCEPTION
            'expected 3 authority-bound TRAIN campaigns with their exact frozen phase shapes, '
            'found %', v_authorities;
    END IF;

    IF (SELECT pg_catalog.count(*) FROM experiments.l2f2_execution_authorities) <> 3 THEN
        RAISE EXCEPTION 'the TRAIN store carries an unexpected execution authority';
    END IF;
    IF (SELECT pg_catalog.count(*) FROM experiments.l2f_experiment_plans
         WHERE partition = 'train') <> 3 THEN
        RAISE EXCEPTION 'the TRAIN store carries an unexpected TRAIN plan';
    END IF;

    SELECT pg_catalog.array_agg(a.plan_id ORDER BY a.phase),
           pg_catalog.array_agg(a.plan_hash ORDER BY a.phase)
      INTO v_plan_ids, v_plans
      FROM experiments.l2f2_execution_authorities a
     WHERE a.baseline_protocol_hash = '{BASELINE_PROTOCOL_HASH}';

    SELECT pg_catalog.jsonb_build_object(
        'schema_version', '{TRAIN_SURFACE_SCHEMA_VERSION}',
        'database_name', v_database,
        'revision', v_revision,
        'authority_count', v_authorities,
        'plan_hashes', pg_catalog.to_jsonb(v_plans),
        'phase_plan_map', (
            SELECT coalesce(pg_catalog.jsonb_object_agg(a.phase, a.plan_hash), '{{}}'::jsonb)
              FROM experiments.l2f2_execution_authorities a),
        'phase_shapes', (
            SELECT coalesce(pg_catalog.jsonb_object_agg(a.phase, pg_catalog.jsonb_build_object(
                       'members', a.member_count,
                       'candidates', a.candidate_count,
                       'logical_jobs', a.logical_job_count,
                       'candidate_set_hash', a.candidate_set_hash,
                       'parameter_space_hash', a.parameter_space_hash)), '{{}}'::jsonb)
              FROM experiments.l2f2_execution_authorities a),
        'logical_job_count', (
            SELECT pg_catalog.sum(p.logical_job_count)
              FROM experiments.l2f_experiment_plans p WHERE p.id = ANY (v_plan_ids)),
        'terminal_job_count', (
            SELECT pg_catalog.count(*) FROM experiments.l2f_experiment_jobs j
             WHERE j.plan_id = ANY (v_plan_ids) AND j.status IN ('SUCCEEDED', 'FAILED')),
        'nonterminal_job_count', (
            SELECT pg_catalog.count(*) FROM experiments.l2f_experiment_jobs j
             WHERE j.plan_id = ANY (v_plan_ids) AND j.status NOT IN ('SUCCEEDED', 'FAILED')),
        'execution_result_count', (
            SELECT pg_catalog.count(*) FROM experiments.l2f_execution_results r
             WHERE r.plan_id = ANY (v_plan_ids)),
        'execution_failure_count', (
            SELECT pg_catalog.count(*) FROM experiments.l2f_execution_failures f
             WHERE f.plan_id = ANY (v_plan_ids)),
        -- a SUCCEEDED execution with no evaluation UNDER THE FROZEN CONTRACT. An evaluation
        -- under some other contract must not satisfy this.
        'succeeded_without_evaluation', (
            SELECT pg_catalog.count(*) FROM experiments.l2f_execution_results r
             WHERE r.plan_id = ANY (v_plan_ids)
               AND NOT EXISTS (
                 SELECT 1 FROM evaluation.l2f_evaluation_results v
                  WHERE v.execution_result_id = r.id
                    AND v.scoring_contract_hash = '{SCORING_CONTRACT_HASH}')),
        'evaluation_count', (
            SELECT pg_catalog.count(*) FROM evaluation.l2f_evaluation_results v
              JOIN experiments.l2f_execution_results r ON r.id = v.execution_result_id
             WHERE r.plan_id = ANY (v_plan_ids)),
        'evaluation_failure_count', (
            SELECT pg_catalog.count(*) FROM evaluation.l2f_evaluation_failures e
              JOIN experiments.l2f_execution_results r ON r.id = e.execution_result_id
             WHERE r.plan_id = ANY (v_plan_ids)),
        'evaluation_hashes', (
            SELECT coalesce(
                pg_catalog.jsonb_agg(x.h ORDER BY x.h), '[]'::jsonb)
              FROM (SELECT v.evaluation_hash AS h
                      FROM evaluation.l2f_evaluation_results v
                      JOIN experiments.l2f_execution_results r
                        ON r.id = v.execution_result_id
                     WHERE r.plan_id = ANY (v_plan_ids)) x),
        'execution_failure_job_keys', (
            SELECT coalesce(pg_catalog.jsonb_agg(x.k ORDER BY x.k), '[]'::jsonb)
              FROM (SELECT f.job_key AS k FROM experiments.l2f_execution_failures f
                     WHERE f.plan_id = ANY (v_plan_ids)) x),
        'execution_failure_codes', (
            SELECT coalesce(pg_catalog.jsonb_object_agg(x.failure_code, x.n), '{{}}'::jsonb)
              FROM (SELECT f.failure_code, pg_catalog.count(*) AS n
                      FROM experiments.l2f_execution_failures f
                     WHERE f.plan_id = ANY (v_plan_ids)
                     GROUP BY f.failure_code) x),
        'scoring_contract_hashes', (
            SELECT coalesce(pg_catalog.jsonb_agg(x.h ORDER BY x.h), '[]'::jsonb)
              FROM (SELECT DISTINCT v.scoring_contract_hash AS h
                      FROM evaluation.l2f_evaluation_results v
                      JOIN experiments.l2f_execution_results r
                        ON r.id = v.execution_result_id
                     WHERE r.plan_id = ANY (v_plan_ids)) x),
        -- BOTH terminal ledgers. 0015 stores the environment on results and failures alike.
        'execution_environment_outcome_count', (
            SELECT pg_catalog.count(*) FROM (
                SELECT r.execution_environment_hash AS h
                  FROM experiments.l2f_execution_results r
                 WHERE r.plan_id = ANY (v_plan_ids)
                UNION ALL
                SELECT f.execution_environment_hash
                  FROM experiments.l2f_execution_failures f
                 WHERE f.plan_id = ANY (v_plan_ids)) u
             WHERE u.h IS NOT NULL),
        'execution_environment_null_count', (
            SELECT pg_catalog.count(*) FROM (
                SELECT r.execution_environment_hash AS h
                  FROM experiments.l2f_execution_results r
                 WHERE r.plan_id = ANY (v_plan_ids)
                UNION ALL
                SELECT f.execution_environment_hash
                  FROM experiments.l2f_execution_failures f
                 WHERE f.plan_id = ANY (v_plan_ids)) u
             WHERE u.h IS NULL),
        'execution_environment_hashes', (
            SELECT coalesce(pg_catalog.jsonb_agg(x.h ORDER BY x.h), '[]'::jsonb)
              FROM (SELECT DISTINCT u.h FROM (
                        SELECT r.execution_environment_hash AS h
                          FROM experiments.l2f_execution_results r
                         WHERE r.plan_id = ANY (v_plan_ids)
                        UNION ALL
                        SELECT f.execution_environment_hash
                          FROM experiments.l2f_execution_failures f
                         WHERE f.plan_id = ANY (v_plan_ids)) u
                     WHERE u.h IS NOT NULL) x),
        'phase_c_dataset_ids', (
            SELECT coalesce(pg_catalog.jsonb_agg(x.d ORDER BY x.d), '[]'::jsonb)
              FROM (SELECT DISTINCT g.dataset_id AS d
                      FROM experiments.l2f_experiment_plan_members m
                      JOIN experiments.l2f2_execution_authorities a ON a.plan_id = m.plan_id
                      JOIN catalog.dataset_registry g ON g.id = m.dataset_registry_id
                     WHERE a.phase = 'PHASE_C' AND m.partition = 'train') x)
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
    # The temporary grant below must not silently remove a PRE-EXISTING privilege on drop.
    # If minos_admin already holds it, something granted it for a reason this provisioner does
    # not know, and guessing why is worse than stopping.
    already = bool(
        conn.execute(
            text("SELECT has_table_privilege(:r, 'public.alembic_version', 'SELECT')"),
            {"r": _CONTROL_PLANE},
        ).scalar_one()
    )
    if already:
        raise TrainQualificationSurfaceError(
            f"{_CONTROL_PLANE} already holds SELECT on public.alembic_version; this provisioner "
            "grants it only temporarily and would revoke a privilege it did not create"
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
    restored = bool(
        conn.execute(
            text("SELECT has_table_privilege(:r, 'public.alembic_version', 'SELECT')"),
            {"r": _CONTROL_PLANE},
        ).scalar_one()
    )
    if restored:
        raise TrainQualificationSurfaceError(
            f"{_CONTROL_PLANE} still holds SELECT on public.alembic_version after the drop"
        )
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
