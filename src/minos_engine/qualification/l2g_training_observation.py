"""The ephemeral, read-only TRAIN observation surface for the L2-G training-data freeze.

TRAIN is sealed: ``minos_admin`` cannot log in, and no role holds a standing SELECT on the
scientific tables. That seal is deliberate and stays. So the dataset freeze uses the same pattern
the BASELINE-QUALIFIED run used — install a narrowly-scoped SECURITY DEFINER function, take ONE
consistent read, drop it, and prove the scientific state and the privilege set are exactly as
they were.

What makes this trustworthy is not that a function reads the tables; it is WHERE the campaign
comes from. The three TRAIN plans are resolved through ``experiments.l2f2_execution_authorities``
— the authority ``0020`` itself treats as authoritative — with each phase's frozen shape asserted
individually. Discovering "whichever plans happen to carry the right hashes" would let a fourth
plan appear beside the campaign and be absorbed silently.

The surface returns per-cell EVIDENCE, never a training label: outcome classes are derived in
Python from the ledgers, because a class decided inside SQL is a scientific decision hidden in a
string.
"""

from __future__ import annotations

from typing import Any, Final

from minos_engine.common.errors import MinosEngineError
from minos_engine.common.hashing import sha256_hex
from minos_engine.qualification.l2f2_train_qualification_surface import (
    BASELINE_PROTOCOL_HASH,
    SCORING_CONTRACT_HASH,
    TRAIN_DATABASE,
    TRAIN_PLAN_HASHES,
    TRAIN_REVISION,
)

__all__ = [
    "L2G_SURFACE_FUNCTION",
    "L2G_SURFACE_SCHEMA_VERSION",
    "L2gTrainingObservationError",
    "drop_l2g_training_surface",
    "install_l2g_training_surface",
    "observe_l2g_training_evidence",
    "surface_body_sha256",
]

L2G_SURFACE_SCHEMA: Final = "evaluation"
L2G_SURFACE_NAME: Final = "l2g_observe_training_evidence"
L2G_SURFACE_FUNCTION: Final = f"{L2G_SURFACE_SCHEMA}.{L2G_SURFACE_NAME}"
L2G_SURFACE_SCHEMA_VERSION: Final = "l2g-training-evidence-observation-v1"

_CONTROL_PLANE: Final = "minos_admin"
_GRANTEE: Final = "minos_evaluator"
_DENIED: Final = ("minos_live", "minos_runner", "minos_trainer")

_PHASE_SHAPES: Final = (
    ("PHASE_A", TRAIN_PLAN_HASHES[0], 5, 39, 195),
    ("PHASE_B", TRAIN_PLAN_HASHES[1], 10, 48, 480),
    ("PHASE_C", TRAIN_PLAN_HASHES[2], 50, 10, 500),
)


class L2gTrainingObservationError(MinosEngineError):
    """The TRAIN training-evidence surface refused."""


def _body() -> str:
    shapes = ",\n             ".join(
        f"('{phase}', '{plan}', {members}, {candidates}, {jobs})"
        for phase, plan, members, candidates, jobs in _PHASE_SHAPES
    )
    return f"""
DECLARE
    v_database text;
    v_revision text;
    v_plan_ids uuid[];
    v_authorities int;
    v_result jsonb;
BEGIN
    SELECT pg_catalog.current_database() INTO v_database;
    IF v_database <> '{TRAIN_DATABASE}' THEN
        RAISE EXCEPTION 'l2g training surface refuses database %', v_database;
    END IF;

    SELECT a.version_num INTO v_revision FROM public.alembic_version a;
    IF v_revision IS DISTINCT FROM '{TRAIN_REVISION}' THEN
        RAISE EXCEPTION 'TRAIN store revision is %, expected {TRAIN_REVISION}', v_revision;
    END IF;

    -- exactly three campaigns, each proven through the accepted execution authority with its
    -- exact frozen shape. 195 + 480 + 500 = 1175 also holds for shapes that are wrong
    -- phase-by-phase, so the aggregate is never the check.
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
             {shapes});
    IF v_authorities <> 3 THEN
        RAISE EXCEPTION 'expected 3 authority-bound TRAIN campaigns, found %', v_authorities;
    END IF;
    IF (SELECT pg_catalog.count(*) FROM experiments.l2f2_execution_authorities) <> 3 THEN
        RAISE EXCEPTION 'the TRAIN store carries an unexpected execution authority';
    END IF;
    IF (SELECT pg_catalog.count(*) FROM experiments.l2f_experiment_plans
         WHERE partition = 'train') <> 3 THEN
        RAISE EXCEPTION 'the TRAIN store carries an unexpected TRAIN plan';
    END IF;

    SELECT pg_catalog.array_agg(a.plan_id ORDER BY a.phase) INTO v_plan_ids
      FROM experiments.l2f2_execution_authorities a
     WHERE a.baseline_protocol_hash = '{BASELINE_PROTOCOL_HASH}';

    SELECT pg_catalog.jsonb_build_object(
        'schema_version', '{L2G_SURFACE_SCHEMA_VERSION}',
        'database_name', v_database,
        'revision', v_revision,
        'phase_plan_map', (
            SELECT coalesce(pg_catalog.jsonb_object_agg(a.phase, a.plan_hash), '{{}}'::jsonb)
              FROM experiments.l2f2_execution_authorities a),
        'parameter_space_hashes', (
            SELECT coalesce(pg_catalog.jsonb_agg(DISTINCT p.parameter_space_hash), '[]'::jsonb)
              FROM experiments.l2f_experiment_plans p WHERE p.id = ANY (v_plan_ids)),
        'terminal_job_count', (
            SELECT pg_catalog.count(*) FROM experiments.l2f_experiment_jobs j
             WHERE j.plan_id = ANY (v_plan_ids) AND j.status IN ('SUCCEEDED', 'FAILED')),
        'nonterminal_job_count', (
            SELECT pg_catalog.count(*) FROM experiments.l2f_experiment_jobs j
             WHERE j.plan_id = ANY (v_plan_ids) AND j.status NOT IN ('SUCCEEDED', 'FAILED')),
        -- an evaluation-side incident is OUR defect and must never become a label; it is
        -- surfaced so the builder can refuse the freeze outright.
        'evaluation_failure_count', (
            SELECT pg_catalog.count(*) FROM evaluation.l2f_evaluation_failures e
              JOIN experiments.l2f_execution_results r ON r.id = e.execution_result_id
             WHERE r.plan_id = ANY (v_plan_ids)),
        'foreign_scoring_contract_count', (
            SELECT pg_catalog.count(*) FROM evaluation.l2f_evaluation_results v
              JOIN experiments.l2f_execution_results r ON r.id = v.execution_result_id
             WHERE r.plan_id = ANY (v_plan_ids)
               AND v.scoring_contract_hash <> '{SCORING_CONTRACT_HASH}'),
        'members', (
            SELECT coalesce(pg_catalog.jsonb_agg(x ORDER BY x->>'dataset_id'), '[]'::jsonb)
              FROM (SELECT DISTINCT pg_catalog.jsonb_build_object(
                        'dataset_id', g.dataset_id,
                        'feature_values_hash', m.feature_values_hash) AS x
                      FROM experiments.l2f_experiment_plan_members m
                      JOIN catalog.dataset_registry g ON g.id = m.dataset_registry_id
                     WHERE m.plan_id = ANY (v_plan_ids) AND m.partition = 'train') y),
        -- ONE row per terminal job. No grouping, no dedup, no outcome class: the caller sees
        -- the raw evidence and derives the science in reviewable Python.
        'cells', (
            SELECT coalesce(pg_catalog.jsonb_agg(x ORDER BY x->>'job_key'), '[]'::jsonb)
              FROM (SELECT pg_catalog.jsonb_build_object(
                        'job_key', j.job_key,
                        'plan_hash', p.plan_hash,
                        'phase', a.phase,
                        'dataset_id', g.dataset_id,
                        'feature_values_hash', m.feature_values_hash,
                        'config_hash', c.config_hash,
                        'parameter_space_hash', c.parameter_space_hash,
                        'status', j.status,
                        'execution_environment_hash', coalesce(
                            r.execution_environment_hash, f.execution_environment_hash),
                        'execution_failure_code', f.failure_code,
                        'has_execution_result', (r.id IS NOT NULL),
                        'evaluation_hash', v.evaluation_hash,
                        'scoring_contract_hash', v.scoring_contract_hash,
                        'admitted', v.admitted,
                        'admission_code', v.admission_code,
                        'minos_score', v.minos_score,
                        'has_evaluation_failure', (e.id IS NOT NULL)) AS x
                      FROM experiments.l2f_experiment_jobs j
                      JOIN experiments.l2f_experiment_plans p ON p.id = j.plan_id
                      JOIN experiments.l2f2_execution_authorities a ON a.plan_id = j.plan_id
                      JOIN experiments.l2f_experiment_plan_members m ON m.id = j.plan_member_id
                      JOIN catalog.dataset_registry g ON g.id = m.dataset_registry_id
                      JOIN experiments.l2f_experiment_plan_configs c ON c.id = j.plan_config_id
                      LEFT JOIN experiments.l2f_execution_results r ON r.job_key = j.job_key
                      LEFT JOIN experiments.l2f_execution_failures f ON f.job_key = j.job_key
                      LEFT JOIN evaluation.l2f_evaluation_results v
                             ON v.execution_result_id = r.id
                            AND v.scoring_contract_hash = '{SCORING_CONTRACT_HASH}'
                      LEFT JOIN evaluation.l2f_evaluation_failures e
                             ON e.execution_result_id = r.id
                     WHERE j.plan_id = ANY (v_plan_ids)
                       AND j.status IN ('SUCCEEDED', 'FAILED')) y)
    ) INTO v_result;

    RETURN v_result;
END;
"""


def surface_body_sha256() -> str:
    """The source identity of this observation surface."""
    return sha256_hex(_body().encode("utf-8"))


def _create_statement() -> str:
    return (
        f"CREATE FUNCTION {L2G_SURFACE_FUNCTION}() RETURNS jsonb "
        "LANGUAGE plpgsql STABLE SECURITY DEFINER "
        "SET search_path = pg_catalog, public "
        f"AS $l2g_train_obs${_body()}$l2g_train_obs$;"
    )


def snapshot_train_state(conn: Any) -> dict[str, Any]:
    """A non-scientific fingerprint used only to prove nothing moved."""
    from minos_engine.qualification.l2f2_train_qualification_surface import (
        snapshot_train_state as _snapshot,
    )

    return _snapshot(conn)


def install_l2g_training_surface(conn: Any) -> dict[str, Any]:
    """Install the ephemeral surface. ADMIN ONLY."""
    from sqlalchemy import text

    database = str(conn.execute(text("SELECT current_database()")).scalar_one())
    if database != TRAIN_DATABASE:
        raise L2gTrainingObservationError(
            f"refusing to install on database {database!r}; this surface belongs to "
            f"{TRAIN_DATABASE!r}"
        )
    revision = conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one_or_none()
    if revision != TRAIN_REVISION:
        raise L2gTrainingObservationError(
            f"TRAIN store revision is {revision!r}, expected {TRAIN_REVISION!r}"
        )
    # If the control plane already holds this, something granted it for a reason this
    # provisioner does not know; revoking it on drop would silently remove a real privilege.
    already = bool(
        conn.execute(
            text("SELECT has_table_privilege(:r, 'public.alembic_version', 'SELECT')"),
            {"r": _CONTROL_PLANE},
        ).scalar_one()
    )
    if already:
        raise L2gTrainingObservationError(
            f"{_CONTROL_PLANE} already holds SELECT on public.alembic_version; this provisioner "
            "grants it only temporarily and would revoke a privilege it did not create"
        )
    before = snapshot_train_state(conn)

    conn.execute(text(f"DROP FUNCTION IF EXISTS {L2G_SURFACE_FUNCTION}()"))
    conn.execute(text(f"GRANT SELECT ON public.alembic_version TO {_CONTROL_PLANE}"))
    conn.execute(text(f"SET ROLE {_CONTROL_PLANE}"))
    conn.execute(text(_create_statement()))
    conn.execute(text("RESET ROLE"))
    conn.execute(text(f"REVOKE ALL ON FUNCTION {L2G_SURFACE_FUNCTION}() FROM PUBLIC"))
    for role in _DENIED:
        conn.execute(text(f"REVOKE ALL ON FUNCTION {L2G_SURFACE_FUNCTION}() FROM {role}"))
    conn.execute(text(f"GRANT EXECUTE ON FUNCTION {L2G_SURFACE_FUNCTION}() TO {_GRANTEE}"))

    after = snapshot_train_state(conn)
    if before != after:
        raise L2gTrainingObservationError(
            f"installing the observation surface changed TRAIN state: {before} -> {after}"
        )
    return after


def drop_l2g_training_surface(conn: Any) -> dict[str, Any]:
    """Remove the surface and prove both state and privileges are restored."""
    from sqlalchemy import text

    before = snapshot_train_state(conn)
    conn.execute(text(f"DROP FUNCTION IF EXISTS {L2G_SURFACE_FUNCTION}()"))
    conn.execute(text(f"REVOKE SELECT ON public.alembic_version FROM {_CONTROL_PLANE}"))
    restored = bool(
        conn.execute(
            text("SELECT has_table_privilege(:r, 'public.alembic_version', 'SELECT')"),
            {"r": _CONTROL_PLANE},
        ).scalar_one()
    )
    if restored:
        raise L2gTrainingObservationError(
            f"{_CONTROL_PLANE} still holds SELECT on public.alembic_version after the drop"
        )
    after = snapshot_train_state(conn)
    if before != after:
        raise L2gTrainingObservationError(
            f"dropping the observation surface changed TRAIN state: {before} -> {after}"
        )
    return after


def observe_l2g_training_evidence(conn: Any) -> dict[str, Any]:
    """ONE consistent read of the raw per-cell TRAIN evidence."""
    import json

    from sqlalchemy import text

    raw = conn.execute(text(f"SELECT {L2G_SURFACE_FUNCTION}()")).scalar_one()
    payload = json.loads(raw) if isinstance(raw, str) else raw
    if not isinstance(payload, dict):
        raise L2gTrainingObservationError("the observation surface returned a non-object")
    if payload.get("schema_version") != L2G_SURFACE_SCHEMA_VERSION:
        raise L2gTrainingObservationError(
            f"observation schema is {payload.get('schema_version')!r}, expected "
            f"{L2G_SURFACE_SCHEMA_VERSION!r}"
        )
    return payload
