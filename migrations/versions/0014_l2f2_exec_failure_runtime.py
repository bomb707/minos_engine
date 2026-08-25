"""Give a failed GATK execution an authoritative elapsed runtime.

The frozen baseline objective's ``BaselineObservation`` requires ``gatk_runtime_ms >= 0`` for
every decided outcome, and ``aggregate_candidate`` uses mean GATK runtime as the frozen
tie-break statistic. A SUCCESS carries its runtime in ``l2f_execution_results``; a FAILURE
carried none at all. So a failed Phase-A execution could not be turned into a faithful
observation without inventing a number — a zero, a timeout constant, or the successful
candidates' average — and every one of those is a fabricated measurement that would flow
straight into a tie-break.

This migration adds the missing measurement rather than a placeholder for it:
``experiments.l2f_execution_failures.runtime_ms`` is ``NOT NULL``, non-negative, and supplied by
the runner's own monotonic clock through the narrow ``SECURITY DEFINER`` writer. The runner still
holds no direct DML on the failure ledger; only the function signature widens.

The column is added ``NOT NULL`` with no default and no backfill, and the upgrade REFUSES if any
pre-existing failure row is present. Such a row predates the measurement and has no authoritative
runtime; stamping one on would be exactly the fabrication this exists to prevent. The real
baseline holds zero failure rows, so it migrates cleanly, and a database that does hold some is a
deliberate operator decision rather than a silent rewrite.

Purely additive to the ledger and the writer: no table is created or dropped, no other function
is redefined, no grant is issued or revoked, and no role gains or loses anything.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0014_l2f2_exec_failure_runtime"
down_revision: str | None = "0013_l2f2_upstream_score_oracle"
branch_labels = None
depends_on = None

_SCHEMA = "experiments"
_FAILURES_TABLE = "l2f_execution_failures"
_FAILURES = f"{_SCHEMA}.{_FAILURES_TABLE}"
_JOBS = "experiments.l2f_experiment_jobs"
_PLANS = "experiments.l2f_experiment_plans"
_RESULTS = "experiments.l2f_execution_results"
_FAIL_FN = "experiments.minos_l2f_fail_job"

_RUNTIME = "runtime_ms"
_RUNTIME_NONNEG = "ck_l2f_exec_failures_runtime_nonneg"

#: the STABLE MINOS SQLSTATEs from 0008, reused verbatim. The Python boundary maps these to typed
#: errors, so this migration must not re-code them: an invalid measurement is an invalid caller
#: argument, which is what MN001 already means.
_SQLSTATE_INVALID_WORKER = "MN001"
_SQLSTATE_PLAN_ABSENT = "MN002"
_SQLSTATE_NOT_OWNED = "MN003"
_SQLSTATE_TRANSITION = "MN012"
_SQLSTATE_DUAL_OUTCOME = "MN021"
_SQLSTATE_RESULT_CONFLICT = "MN022"

_OLD_SIG = f"{_FAIL_FN}(text, uuid, text, text, integer, text)"
_NEW_SIG = f"{_FAIL_FN}(text, uuid, text, text, integer, text, bigint)"

_DENIED_ROLES = ("minos_live", "minos_trainer", "minos_evaluator")


def _fail_function(*, with_runtime: bool) -> str:
    """The 0008 failure writer, optionally carrying the elapsed attempt runtime.

    Everything else is reproduced exactly: the same worker check, the same plan resolution, the
    same ``FOR UPDATE`` job lock taken BEFORE either outcome table is read (which is what makes
    success and failure mutually exclusive), the same idempotency comparison, and the same single
    ``RUNNING -> FAILED`` transition.
    """
    runtime_param = ", p_runtime_ms bigint" if with_runtime else ""
    runtime_guard = (
        "IF p_runtime_ms IS NULL OR p_runtime_ms < 0 THEN "
        "  RAISE EXCEPTION 'runtime_ms must be a non-negative elapsed measurement' "
        f"    USING ERRCODE = '{_SQLSTATE_INVALID_WORKER}'; "
        "END IF; "
        if with_runtime
        else ""
    )
    runtime_compare = (
        "     OR v_existing.runtime_ms IS DISTINCT FROM p_runtime_ms " if with_runtime else ""
    )
    runtime_column = ", runtime_ms" if with_runtime else ""
    runtime_value = ", p_runtime_ms" if with_runtime else ""
    return (
        f"CREATE OR REPLACE FUNCTION {_FAIL_FN}"
        "(p_plan_hash text, p_job_id uuid, p_worker_id text, p_failure_code text, "
        f" p_exit_code integer, p_stderr_sha256 text{runtime_param}) "
        "RETURNS TABLE(failure_id uuid, created boolean) LANGUAGE plpgsql SECURITY DEFINER "
        "SET search_path = pg_catalog AS $fail$ "
        "DECLARE v_plan_id uuid; v_job_key text; v_status text; v_existing record; v_id uuid; "
        "        v_created boolean := false; v_rows integer; "
        "BEGIN "
        "IF p_worker_id IS NULL OR btrim(p_worker_id) = '' THEN "
        "  RAISE EXCEPTION 'worker_id must be a non-empty identifier' "
        f"    USING ERRCODE = '{_SQLSTATE_INVALID_WORKER}'; "
        "END IF; "
        f"{runtime_guard}"
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
        f"{runtime_compare}"
        "     OR v_existing.stderr_sha256 IS DISTINCT FROM p_stderr_sha256 THEN "
        "    RAISE EXCEPTION 'an existing L2-F execution failure for job % differs', p_job_id "
        f"      USING ERRCODE = '{_SQLSTATE_RESULT_CONFLICT}'; "
        "  END IF; "
        "  v_id := v_existing.id; "
        "ELSE "
        f"  INSERT INTO {_FAILURES} "
        "    (plan_id, job_id, job_key, worker_id, failure_code, exit_code, stderr_sha256"
        f"{runtime_column}) "
        "  VALUES (v_plan_id, p_job_id, v_job_key, p_worker_id, p_failure_code, p_exit_code, "
        f"          p_stderr_sha256{runtime_value}) RETURNING id INTO v_id; "
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
    existing = conn.execute(sa.text(f"SELECT count(*) FROM {_FAILURES}")).scalar_one()  # noqa: S608
    if existing:  # checked BEFORE anything is altered, so a refusal changes nothing
        raise RuntimeError(
            f"cannot add an authoritative {_RUNTIME} to {_FAILURES}: {existing} failure row(s) "
            "already exist and were written before the runner measured elapsed attempt time. "
            "They have no authoritative runtime, and inventing one would put a fabricated "
            "measurement into the frozen tie-break statistic. Remove or migrate those rows "
            "deliberately first."
        )

    # every 0008 object is owned by minos_admin, and a SECURITY DEFINER function executes with
    # its OWNER's authority. Creating this one as the migration's own (superuser) login would
    # silently widen the failure writer from minos_admin to postgres.
    op.execute("SET ROLE minos_admin")
    op.add_column(
        _FAILURES_TABLE, sa.Column(_RUNTIME, sa.BigInteger(), nullable=False), schema=_SCHEMA
    )
    op.create_check_constraint(_RUNTIME_NONNEG, _FAILURES_TABLE, f"{_RUNTIME} >= 0", schema=_SCHEMA)

    # widen the ONLY writer. The old signature is dropped so no caller can keep persisting a
    # failure without its measurement.
    op.execute(_fail_function(with_runtime=True))
    op.execute(f"DROP FUNCTION IF EXISTS {_OLD_SIG};")
    _grant(_NEW_SIG)
    op.execute("RESET ROLE")


def downgrade() -> None:
    """Restore 0013 exactly: the narrower writer, its 0008 ownership, and no runtime column."""
    op.execute("SET ROLE minos_admin")
    op.execute(_fail_function(with_runtime=False))
    op.execute(f"DROP FUNCTION IF EXISTS {_NEW_SIG};")
    _grant(_OLD_SIG)
    op.drop_constraint(_RUNTIME_NONNEG, _FAILURES_TABLE, schema=_SCHEMA, type_="check")
    op.drop_column(_FAILURES_TABLE, _RUNTIME, schema=_SCHEMA)
    op.execute("RESET ROLE")
