"""Bind the EXECUTION ENVIRONMENT identity into every durable execution outcome.

A Phase-A campaign was lost to a runtime, not to science. The pinned GATK launcher is a
``#!/usr/bin/env python`` script; one worker's PATH had no ``python``; ``env`` exited 127 before a
single argument was parsed; and five jobs were recorded as ``GATK_NONZERO_EXIT`` — a code the
frozen objective reads as CANDIDATE_FAILURE — for configurations GATK never saw. Nothing in the
ledger could have revealed that: a row said which BAM, which CONFIG and which GATK bundle, but
nothing about the interpreter or the JVM that actually had to start.

So both outcome ledgers gain ``execution_environment_hash``: the domain-separated identity of the
launcher, the scientific payload bundle, the explicit interpreter, the JVM and the child-
environment policy version. It is ``NOT NULL`` on success and on failure alike, because a failure
that cannot say which runtime produced it is exactly the row that misled us.

The upgrade REFUSES on any database that already holds an execution result or an execution
failure. Those rows predate the identity and there is no honest value to give them: a default
would be a lie, a backfill would be a guess, and either would relabel the contaminated campaign
as a corrected one. A store that holds them must be quarantined and a fresh campaign built —
which is the deliberate consequence this refusal exists to force.

The two narrow ``SECURITY DEFINER`` writers widen by one argument each and their older signatures
are dropped, so no caller can persist an outcome without saying which runtime produced it. No
table is created or dropped, no role gains or loses anything, and no grant changes.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0015_l2f2_exec_environment"
down_revision: str | None = "0014_l2f2_exec_failure_runtime"
branch_labels = None
depends_on = None

_SCHEMA = "experiments"
_RESULTS_TABLE = "l2f_execution_results"
_FAILURES_TABLE = "l2f_execution_failures"
_RESULTS = f"{_SCHEMA}.{_RESULTS_TABLE}"
_FAILURES = f"{_SCHEMA}.{_FAILURES_TABLE}"
_JOBS = "experiments.l2f_experiment_jobs"
_PLANS = "experiments.l2f_experiment_plans"

_COMPLETE_FN = "experiments.minos_l2f_complete_job_success"
_FAIL_FN = "experiments.minos_l2f_fail_job"

_ENV = "execution_environment_hash"
_RESULTS_ENV_HEX = "ck_l2f_exec_results_env_hash_hex"
_FAILURES_ENV_HEX = "ck_l2f_exec_failures_env_hash_hex"
_HEX64 = "^[0-9a-f]{64}$"

#: the STABLE MINOS SQLSTATEs from 0008, reused verbatim — the Python boundary maps these to
#: typed errors, so this migration must not re-code them.
_SQLSTATE_INVALID_WORKER = "MN001"
_SQLSTATE_PLAN_ABSENT = "MN002"
_SQLSTATE_NOT_OWNED = "MN003"
_SQLSTATE_TRANSITION = "MN012"
_SQLSTATE_DUAL_OUTCOME = "MN021"
_SQLSTATE_RESULT_CONFLICT = "MN022"

#: media types fixed by 0008 and reproduced verbatim by the widened success writer.
VCF_MEDIA_TYPE = "application/vnd.ga4gh.vcf"
RESULT_MANIFEST_MEDIA_TYPE = "application/vnd.minos.l2f-execution-result+json"

_OLD_COMPLETE_SIG = (
    f"{_COMPLETE_FN}(text, uuid, text, text, text, text, text, text, text, text, "
    "uuid, text, uuid, text, text, bigint)"
)
_NEW_COMPLETE_SIG = (
    f"{_COMPLETE_FN}(text, uuid, text, text, text, text, text, text, text, text, "
    "uuid, text, uuid, text, text, bigint, text)"
)
_OLD_FAIL_SIG = f"{_FAIL_FN}(text, uuid, text, text, integer, text, bigint)"
_NEW_FAIL_SIG = f"{_FAIL_FN}(text, uuid, text, text, integer, text, bigint, text)"

_DENIED_ROLES = ("minos_live", "minos_trainer", "minos_evaluator")


def _complete_function(*, with_environment: bool) -> str:
    """The 0008 success writer, optionally carrying the execution-environment identity.

    Everything else is reproduced exactly: the same worker check, the same plan resolution, the
    same ``FOR UPDATE`` job lock taken BEFORE either outcome table is read, the same idempotency
    comparison over every immutable stored column, and the same single ``RUNNING -> SUCCEEDED``
    transition.
    """
    env_param = ", p_execution_environment_hash text" if with_environment else ""
    env_guard = (
        "IF p_execution_environment_hash IS NULL "
        f"   OR p_execution_environment_hash !~ '{_HEX64}' THEN "
        "  RAISE EXCEPTION 'execution_environment_hash must be a lowercase sha256' "
        f"    USING ERRCODE = '{_SQLSTATE_INVALID_WORKER}'; "
        "END IF; "
        if with_environment
        else ""
    )
    env_compare = (
        f"     OR v_existing.{_ENV} IS DISTINCT FROM p_execution_environment_hash "
        if with_environment
        else ""
    )
    env_column = f", {_ENV}" if with_environment else ""
    env_value = ", p_execution_environment_hash" if with_environment else ""
    return (
        f"CREATE OR REPLACE FUNCTION {_COMPLETE_FN}"
        "(p_plan_hash text, p_job_id uuid, p_worker_id text, p_job_key text, "
        " p_config_hash text, p_parameter_space_hash text, p_input_identity_hash text, "
        " p_logical_argv_hash text, p_gatk_executable_sha256 text, p_gatk_version text, "
        " p_vcf_artifact_id uuid, p_vcf_sha256 text, p_manifest_artifact_id uuid, "
        f" p_manifest_sha256 text, p_result_hash text, p_runtime_ms bigint{env_param}) "
        "RETURNS TABLE(result_id uuid, created boolean) LANGUAGE plpgsql SECURITY DEFINER "
        "SET search_path = pg_catalog AS $complete$ "
        "DECLARE v_plan_id uuid; v_member uuid; v_config uuid; v_status text; "
        "        v_existing record; v_id uuid; v_created boolean := false; v_rows integer; "
        "BEGIN "
        "IF p_worker_id IS NULL OR btrim(p_worker_id) = '' THEN "
        "  RAISE EXCEPTION 'worker_id must be a non-empty identifier' "
        f"    USING ERRCODE = '{_SQLSTATE_INVALID_WORKER}'; "
        "END IF; "
        f"{env_guard}"
        f"SELECT p.id INTO v_plan_id FROM {_PLANS} p WHERE p.plan_hash = p_plan_hash; "
        "IF v_plan_id IS NULL THEN "
        "  RAISE EXCEPTION 'accepted L2-F plan is not persisted' "
        f"    USING ERRCODE = '{_SQLSTATE_PLAN_ABSENT}'; "
        "END IF; "
        "SELECT j.plan_member_id, j.plan_config_id, j.status "
        "  INTO v_member, v_config, v_status "
        f"  FROM {_JOBS} j "
        " WHERE j.id = p_job_id AND j.plan_id = v_plan_id AND j.job_key = p_job_key "
        "   AND j.claimed_by = p_worker_id AND j.status IN ('RUNNING', 'SUCCEEDED') "
        "   FOR UPDATE; "
        "IF NOT FOUND THEN "
        "  RAISE EXCEPTION 'job % of plan % is not completable by worker %', "
        f"    p_job_id, p_plan_hash, p_worker_id USING ERRCODE = '{_SQLSTATE_NOT_OWNED}'; "
        "END IF; "
        f"IF EXISTS (SELECT 1 FROM {_FAILURES} f WHERE f.job_id = p_job_id) THEN "
        "  RAISE EXCEPTION 'L2-F job % already has a failure record', p_job_id "
        f"    USING ERRCODE = '{_SQLSTATE_DUAL_OUTCOME}'; "
        "END IF; "
        f"SELECT * INTO v_existing FROM {_RESULTS} r WHERE r.job_id = p_job_id; "
        "IF FOUND THEN "
        "  IF v_existing.plan_id IS DISTINCT FROM v_plan_id "
        "     OR v_existing.job_id IS DISTINCT FROM p_job_id "
        "     OR v_existing.job_key IS DISTINCT FROM p_job_key "
        "     OR v_existing.plan_member_id IS DISTINCT FROM v_member "
        "     OR v_existing.plan_config_id IS DISTINCT FROM v_config "
        "     OR v_existing.config_hash IS DISTINCT FROM p_config_hash "
        "     OR v_existing.parameter_space_hash IS DISTINCT FROM p_parameter_space_hash "
        "     OR v_existing.input_identity_hash IS DISTINCT FROM p_input_identity_hash "
        "     OR v_existing.logical_argv_hash IS DISTINCT FROM p_logical_argv_hash "
        "     OR v_existing.gatk_executable_sha256 IS DISTINCT FROM p_gatk_executable_sha256 "
        "     OR v_existing.gatk_version IS DISTINCT FROM p_gatk_version "
        "     OR v_existing.vcf_artifact_id IS DISTINCT FROM p_vcf_artifact_id "
        "     OR v_existing.vcf_sha256 IS DISTINCT FROM p_vcf_sha256 "
        f"     OR v_existing.vcf_media_type IS DISTINCT FROM '{VCF_MEDIA_TYPE}' "
        "     OR v_existing.result_manifest_artifact_id IS DISTINCT FROM p_manifest_artifact_id "
        "     OR v_existing.result_manifest_sha256 IS DISTINCT FROM p_manifest_sha256 "
        "     OR v_existing.result_manifest_media_type IS DISTINCT FROM "
        f"        '{RESULT_MANIFEST_MEDIA_TYPE}' "
        "     OR v_existing.result_hash IS DISTINCT FROM p_result_hash "
        f"{env_compare}"
        "     OR v_existing.runtime_ms IS DISTINCT FROM p_runtime_ms "
        "  THEN "
        "    RAISE EXCEPTION 'an existing L2-F execution result for job % differs', p_job_id "
        f"      USING ERRCODE = '{_SQLSTATE_RESULT_CONFLICT}'; "
        "  END IF; "
        "  v_id := v_existing.id; "
        "ELSE "
        f"  INSERT INTO {_RESULTS} "
        "    (plan_id, job_id, job_key, plan_member_id, plan_config_id, config_hash, "
        "     parameter_space_hash, input_identity_hash, logical_argv_hash, "
        "     gatk_executable_sha256, gatk_version, vcf_artifact_id, vcf_sha256, "
        "     result_manifest_artifact_id, result_manifest_sha256, result_hash, runtime_ms"
        f"{env_column}) "
        "  VALUES (v_plan_id, p_job_id, p_job_key, v_member, v_config, p_config_hash, "
        "          p_parameter_space_hash, p_input_identity_hash, p_logical_argv_hash, "
        "          p_gatk_executable_sha256, p_gatk_version, p_vcf_artifact_id, p_vcf_sha256, "
        f"          p_manifest_artifact_id, p_manifest_sha256, p_result_hash, p_runtime_ms"
        f"{env_value}) "
        "  RETURNING id INTO v_id; "
        "  v_created := true; "
        "END IF; "
        "IF v_status = 'RUNNING' THEN "
        f"  UPDATE {_JOBS} j SET status = 'SUCCEEDED', updated_at = now() "
        "   WHERE j.id = p_job_id AND j.status = 'RUNNING'; "
        "  GET DIAGNOSTICS v_rows = ROW_COUNT; "
        "  IF v_rows <> 1 THEN "
        "    RAISE EXCEPTION 'terminal SUCCEEDED transition affected % rows, expected 1', v_rows "
        f"      USING ERRCODE = '{_SQLSTATE_TRANSITION}'; "
        "  END IF; "
        "END IF; "
        "RETURN QUERY SELECT v_id, v_created; "
        "END; $complete$;"
    )


def _fail_function(*, with_environment: bool) -> str:
    """The 0014 failure writer, optionally carrying the execution-environment identity."""
    env_param = ", p_execution_environment_hash text" if with_environment else ""
    env_guard = (
        "IF p_execution_environment_hash IS NULL "
        f"   OR p_execution_environment_hash !~ '{_HEX64}' THEN "
        "  RAISE EXCEPTION 'execution_environment_hash must be a lowercase sha256' "
        f"    USING ERRCODE = '{_SQLSTATE_INVALID_WORKER}'; "
        "END IF; "
        if with_environment
        else ""
    )
    env_compare = (
        f"     OR v_existing.{_ENV} IS DISTINCT FROM p_execution_environment_hash "
        if with_environment
        else ""
    )
    env_column = f", {_ENV}" if with_environment else ""
    env_value = ", p_execution_environment_hash" if with_environment else ""
    return (
        f"CREATE OR REPLACE FUNCTION {_FAIL_FN}"
        "(p_plan_hash text, p_job_id uuid, p_worker_id text, p_failure_code text, "
        f" p_exit_code integer, p_stderr_sha256 text, p_runtime_ms bigint{env_param}) "
        "RETURNS TABLE(failure_id uuid, created boolean) LANGUAGE plpgsql SECURITY DEFINER "
        "SET search_path = pg_catalog AS $fail$ "
        "DECLARE v_plan_id uuid; v_job_key text; v_status text; v_existing record; v_id uuid; "
        "        v_created boolean := false; v_rows integer; "
        "BEGIN "
        "IF p_worker_id IS NULL OR btrim(p_worker_id) = '' THEN "
        "  RAISE EXCEPTION 'worker_id must be a non-empty identifier' "
        f"    USING ERRCODE = '{_SQLSTATE_INVALID_WORKER}'; "
        "END IF; "
        "IF p_runtime_ms IS NULL OR p_runtime_ms < 0 THEN "
        "  RAISE EXCEPTION 'runtime_ms must be a non-negative elapsed measurement' "
        f"    USING ERRCODE = '{_SQLSTATE_INVALID_WORKER}'; "
        "END IF; "
        f"{env_guard}"
        f"SELECT p.id INTO v_plan_id FROM {_PLANS} p WHERE p.plan_hash = p_plan_hash; "
        "IF v_plan_id IS NULL THEN "
        "  RAISE EXCEPTION 'accepted L2-F plan is not persisted' "
        f"    USING ERRCODE = '{_SQLSTATE_PLAN_ABSENT}'; "
        "END IF; "
        "SELECT j.job_key, j.status INTO v_job_key, v_status "
        f"  FROM {_JOBS} j "
        " WHERE j.id = p_job_id AND j.plan_id = v_plan_id AND j.claimed_by = p_worker_id "
        "   AND j.status IN ('RUNNING', 'FAILED') "
        "   FOR UPDATE; "
        "IF NOT FOUND THEN "
        "  RAISE EXCEPTION 'job % of plan % is not failable by worker %', "
        f"    p_job_id, p_plan_hash, p_worker_id USING ERRCODE = '{_SQLSTATE_NOT_OWNED}'; "
        "END IF; "
        f"IF EXISTS (SELECT 1 FROM {_RESULTS} r WHERE r.job_id = p_job_id) THEN "
        "  RAISE EXCEPTION 'L2-F job % already has a success result', p_job_id "
        f"    USING ERRCODE = '{_SQLSTATE_DUAL_OUTCOME}'; "
        "END IF; "
        f"SELECT * INTO v_existing FROM {_FAILURES} f WHERE f.job_id = p_job_id; "
        "IF FOUND THEN "
        "  IF v_existing.plan_id IS DISTINCT FROM v_plan_id "
        "     OR v_existing.job_key IS DISTINCT FROM v_job_key "
        "     OR v_existing.failure_code IS DISTINCT FROM p_failure_code "
        "     OR v_existing.worker_id IS DISTINCT FROM p_worker_id "
        "     OR v_existing.exit_code IS DISTINCT FROM p_exit_code "
        "     OR v_existing.runtime_ms IS DISTINCT FROM p_runtime_ms "
        f"{env_compare}"
        "     OR v_existing.stderr_sha256 IS DISTINCT FROM p_stderr_sha256 THEN "
        "    RAISE EXCEPTION 'an existing L2-F execution failure for job % differs', p_job_id "
        f"      USING ERRCODE = '{_SQLSTATE_RESULT_CONFLICT}'; "
        "  END IF; "
        "  v_id := v_existing.id; "
        "ELSE "
        f"  INSERT INTO {_FAILURES} "
        "    (plan_id, job_id, job_key, worker_id, failure_code, exit_code, stderr_sha256"
        f", runtime_ms{env_column}) "
        "  VALUES (v_plan_id, p_job_id, v_job_key, p_worker_id, p_failure_code, p_exit_code, "
        f"          p_stderr_sha256, p_runtime_ms{env_value}) RETURNING id INTO v_id; "
        "  v_created := true; "
        "END IF; "
        "IF v_status = 'RUNNING' THEN "
        f"  UPDATE {_JOBS} j SET status = 'FAILED', updated_at = now() "
        "   WHERE j.id = p_job_id AND j.status = 'RUNNING'; "
        "  GET DIAGNOSTICS v_rows = ROW_COUNT; "
        "  IF v_rows <> 1 THEN "
        "    RAISE EXCEPTION 'terminal FAILED transition affected % rows, expected 1', v_rows "
        f"      USING ERRCODE = '{_SQLSTATE_TRANSITION}'; "
        "  END IF; "
        "END IF; "
        "RETURN QUERY SELECT v_id, v_created; "
        "END; $fail$;"
    )


def _grant(signature: str) -> None:
    """Exactly the 0008 privilege shape: the runner may EXECUTE, and holds no table DML."""
    op.execute(f"REVOKE ALL ON FUNCTION {signature} FROM PUBLIC;")
    for role in (*_DENIED_ROLES, "minos_runner"):
        op.execute(f"REVOKE ALL ON FUNCTION {signature} FROM {role};")
    op.execute(f"GRANT EXECUTE ON FUNCTION {signature} TO minos_runner;")
    op.execute(f"GRANT EXECUTE ON FUNCTION {signature} TO minos_admin;")


def upgrade() -> None:
    conn = op.get_bind()
    # checked BEFORE anything is altered, so a refusal leaves the database exactly as it was.
    results = conn.execute(sa.text(f"SELECT count(*) FROM {_RESULTS}")).scalar_one()  # noqa: S608
    failures = conn.execute(sa.text(f"SELECT count(*) FROM {_FAILURES}")).scalar_one()  # noqa: S608
    if results or failures:
        raise RuntimeError(
            f"cannot bind an execution environment to {_RESULTS} / {_FAILURES}: {results} "
            f"result(s) and {failures} failure(s) already exist and were recorded before the "
            "runtime that produced them was part of their identity. There is no honest value to "
            "give those rows — a default would be a lie and a backfill a guess — and relabelling "
            "them would present a contaminated campaign as a corrected one. Quarantine this "
            "database and build the new campaign on a fresh store."
        )

    # every 0008 object is owned by minos_admin, and a SECURITY DEFINER function executes with its
    # OWNER's authority; creating these as the migration login would silently widen both writers.
    op.execute("SET ROLE minos_admin")
    for table, check in ((_RESULTS_TABLE, _RESULTS_ENV_HEX), (_FAILURES_TABLE, _FAILURES_ENV_HEX)):
        op.add_column(table, sa.Column(_ENV, sa.CHAR(64), nullable=False), schema=_SCHEMA)
        op.create_check_constraint(check, table, f"{_ENV} ~ '{_HEX64}'", schema=_SCHEMA)

    # widen BOTH writers, then drop the narrower signatures so no caller can persist an outcome
    # that cannot say which runtime produced it.
    op.execute(_complete_function(with_environment=True))
    op.execute(f"DROP FUNCTION IF EXISTS {_OLD_COMPLETE_SIG};")
    _grant(_NEW_COMPLETE_SIG)
    op.execute(_fail_function(with_environment=True))
    op.execute(f"DROP FUNCTION IF EXISTS {_OLD_FAIL_SIG};")
    _grant(_NEW_FAIL_SIG)
    op.execute("RESET ROLE")


def downgrade() -> None:
    """Restore 0014 exactly: the narrower writers, their 0008 ownership, and no identity column."""
    op.execute("SET ROLE minos_admin")
    op.execute(_complete_function(with_environment=False))
    op.execute(f"DROP FUNCTION IF EXISTS {_NEW_COMPLETE_SIG};")
    _grant(_OLD_COMPLETE_SIG)
    op.execute(_fail_function(with_environment=False))
    op.execute(f"DROP FUNCTION IF EXISTS {_NEW_FAIL_SIG};")
    _grant(_OLD_FAIL_SIG)
    for table, check in ((_RESULTS_TABLE, _RESULTS_ENV_HEX), (_FAILURES_TABLE, _FAILURES_ENV_HEX)):
        op.drop_constraint(check, table, schema=_SCHEMA, type_="check")
        op.drop_column(table, _ENV, schema=_SCHEMA)
    op.execute("RESET ROLE")
