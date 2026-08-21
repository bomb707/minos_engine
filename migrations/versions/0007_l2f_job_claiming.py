"""L2-F F4 job claiming + pre-execution state transitions (additive to 0006).

Adds the F4 claim/state machinery WITHOUT touching the five L2-F tables created by ``0006``
(which stays byte-identical) or any scientific identity column. Two things are added:

1. A strict transition guard trigger on ``experiments.l2f_experiment_jobs`` permitting ONLY the
   three F4 transitions ``PENDING -> CLAIMED``, ``CLAIMED -> PENDING`` and
   ``CLAIMED -> RUNNING``, with per-status claim invariants. Direct transitions to
   ``SUCCEEDED``/``FAILED``/``CANCELLED`` remain unavailable (those are F5). The 0006 identity
   immutability trigger and the delete-rejection trigger remain in force.

2. Three narrowly scoped ``SECURITY DEFINER`` functions (claim / start / release) that run under
   ``minos_admin`` authority with a fixed secure ``search_path``. ``minos_runner`` receives
   EXECUTE on exactly those three functions and **no** direct INSERT/UPDATE/DELETE grant on the
   job table; ``minos_live``/``minos_trainer``/``minos_evaluator`` and PUBLIC are denied.

Downgrade removes every F4 function, trigger and grant, restoring exact 0006 behavior.
"""

from __future__ import annotations

from alembic import op

revision: str = "0007_l2f_job_claiming"
down_revision: str | None = "0006_l2f_experiment_plan"
branch_labels = None
depends_on = None

_JOBS = "experiments.l2f_experiment_jobs"
_PLANS = "experiments.l2f_experiment_plans"

_GUARD = "experiments.minos_l2f_job_transition_guard"
_CLAIM_FN = "experiments.minos_l2f_claim_next_job"
_START_FN = "experiments.minos_l2f_start_job"
_RELEASE_FN = "experiments.minos_l2f_release_job"

#: signatures used for GRANT/REVOKE/DROP (functions are overload-addressed by argument types).
_CLAIM_SIG = f"{_CLAIM_FN}(text, text)"
_START_SIG = f"{_START_FN}(text, uuid, text)"
_RELEASE_SIG = f"{_RELEASE_FN}(text, uuid, text)"
_F4_FUNCTION_SIGS = (_CLAIM_SIG, _START_SIG, _RELEASE_SIG)

_DENIED_ROLES = ("minos_live", "minos_trainer", "minos_evaluator")

#: stable SQLSTATEs so the Python boundary maps failures to typed errors (never string matching).
_SQLSTATE_INVALID_WORKER = "MN001"
_SQLSTATE_PLAN_ABSENT = "MN002"
_SQLSTATE_NOT_OWNED = "MN003"
_SQLSTATE_CLAIM_INVARIANT = "MN010"
_SQLSTATE_CLAIM_METADATA = "MN011"
_SQLSTATE_TRANSITION = "MN012"

#: the columns every F4 function returns (identity + mutable claim state; never truth/score data).
_RETURN_COLS = (
    "job_id uuid, job_key character(64), plan_id uuid, plan_member_id uuid, "
    "plan_config_id uuid, status text, claimed_by text, claimed_at timestamptz"
)
#: alias-qualified: the RETURNS TABLE OUT parameters otherwise shadow the table's own columns.
_RETURNING = (
    "j.id, j.job_key, j.plan_id, j.plan_member_id, j.plan_config_id, "
    "j.status, j.claimed_by, j.claimed_at"
)


def _create_transition_guard() -> None:
    op.execute(
        f"CREATE OR REPLACE FUNCTION {_GUARD}() RETURNS trigger LANGUAGE plpgsql "
        "SET search_path = pg_catalog AS $guard$ "
        "BEGIN "
        # ---- per-status claim invariants (apply to every permitted transition) ----
        "IF NEW.status = 'PENDING' "
        "   AND (NEW.claimed_by IS NOT NULL OR NEW.claimed_at IS NOT NULL) THEN "
        "  RAISE EXCEPTION 'L2-F job PENDING requires claimed_by IS NULL and claimed_at IS NULL' "
        f"    USING ERRCODE = '{_SQLSTATE_CLAIM_INVARIANT}'; "
        "END IF; "
        "IF NEW.status IN ('CLAIMED', 'RUNNING') "
        "   AND (NEW.claimed_by IS NULL OR btrim(NEW.claimed_by) = '' "
        "        OR NEW.claimed_at IS NULL) THEN "
        "  RAISE EXCEPTION 'L2-F job % requires a non-empty claimed_by and a claimed_at', "
        f"    NEW.status USING ERRCODE = '{_SQLSTATE_CLAIM_INVARIANT}'; "
        "END IF; "
        # ---- a status-preserving update may not silently move the claim ----
        "IF NEW.status = OLD.status THEN "
        "  IF NEW.claimed_by IS DISTINCT FROM OLD.claimed_by "
        "     OR NEW.claimed_at IS DISTINCT FROM OLD.claimed_at THEN "
        "    RAISE EXCEPTION 'L2-F job claim metadata may not change without a status transition' "
        f"      USING ERRCODE = '{_SQLSTATE_CLAIM_METADATA}'; "
        "  END IF; "
        "  RETURN NEW; "
        "END IF; "
        # ---- exactly three permitted F4 transitions ----
        "IF OLD.status = 'PENDING' AND NEW.status = 'CLAIMED' THEN "
        "  RETURN NEW; "
        "ELSIF OLD.status = 'CLAIMED' AND NEW.status = 'PENDING' THEN "
        "  RETURN NEW; "  # the PENDING invariant above already forced both claim fields NULL
        "ELSIF OLD.status = 'CLAIMED' AND NEW.status = 'RUNNING' THEN "
        "  IF NEW.claimed_by IS DISTINCT FROM OLD.claimed_by "
        "     OR NEW.claimed_at IS DISTINCT FROM OLD.claimed_at THEN "
        "    RAISE EXCEPTION 'L2-F CLAIMED -> RUNNING must preserve claimed_by and claimed_at' "
        f"      USING ERRCODE = '{_SQLSTATE_CLAIM_METADATA}'; "
        "  END IF; "
        "  RETURN NEW; "
        "END IF; "
        "RAISE EXCEPTION 'L2-F job transition % -> % is not permitted in F4', "
        f"  OLD.status, NEW.status USING ERRCODE = '{_SQLSTATE_TRANSITION}'; "
        "END; $guard$;"
    )
    op.execute(
        "CREATE TRIGGER trg_l2f_jobs_transition_guard "
        f"BEFORE UPDATE ON {_JOBS} "
        f"FOR EACH ROW EXECUTE FUNCTION {_GUARD}();"
    )


def _create_claim_functions() -> None:
    # 1) claim the next PENDING job of an accepted plan (FOR UPDATE SKIP LOCKED).
    op.execute(
        f"CREATE OR REPLACE FUNCTION {_CLAIM_FN}(p_plan_hash text, p_worker_id text) "
        f"RETURNS TABLE({_RETURN_COLS}) LANGUAGE plpgsql SECURITY DEFINER "
        "SET search_path = pg_catalog AS $claim$ "
        "DECLARE v_plan_id uuid; v_job_id uuid; "
        "BEGIN "
        "IF p_worker_id IS NULL OR btrim(p_worker_id) = '' THEN "
        "  RAISE EXCEPTION 'worker_id must be a non-empty identifier' "
        f"    USING ERRCODE = '{_SQLSTATE_INVALID_WORKER}'; "
        "END IF; "
        f"SELECT p.id INTO v_plan_id FROM {_PLANS} p WHERE p.plan_hash = p_plan_hash; "
        "IF v_plan_id IS NULL THEN "
        "  RAISE EXCEPTION 'accepted L2-F plan is not persisted' "
        f"    USING ERRCODE = '{_SQLSTATE_PLAN_ABSENT}'; "
        "END IF; "
        # deterministic order (created_at, id); already-locked rows are skipped, never waited on.
        "SELECT j.id INTO v_job_id "
        f"  FROM {_JOBS} j "
        " WHERE j.plan_id = v_plan_id AND j.status = 'PENDING' "
        " ORDER BY j.created_at, j.id "
        " LIMIT 1 FOR UPDATE SKIP LOCKED; "
        "IF v_job_id IS NULL THEN RETURN; END IF; "
        "RETURN QUERY "
        f"  UPDATE {_JOBS} AS j "
        "     SET status = 'CLAIMED', claimed_by = p_worker_id, "
        "         claimed_at = now(), updated_at = now() "
        "   WHERE j.id = v_job_id "
        f"  RETURNING {_RETURNING}; "
        "END; $claim$;"
    )

    # 2) start a CLAIMED job (same worker only): CLAIMED -> RUNNING.
    op.execute(
        f"CREATE OR REPLACE FUNCTION {_START_FN}"
        "(p_plan_hash text, p_job_id uuid, p_worker_id text) "
        f"RETURNS TABLE({_RETURN_COLS}) LANGUAGE plpgsql SECURITY DEFINER "
        "SET search_path = pg_catalog AS $start$ "
        "DECLARE v_found boolean; v_plan_id uuid; "
        "BEGIN "
        "IF p_worker_id IS NULL OR btrim(p_worker_id) = '' THEN "
        "  RAISE EXCEPTION 'worker_id must be a non-empty identifier' "
        f"    USING ERRCODE = '{_SQLSTATE_INVALID_WORKER}'; "
        "END IF; "
        f"SELECT p.id INTO v_plan_id FROM {_PLANS} p WHERE p.plan_hash = p_plan_hash; "
        "IF v_plan_id IS NULL THEN "
        "  RAISE EXCEPTION 'accepted L2-F plan is not persisted' "
        f"    USING ERRCODE = '{_SQLSTATE_PLAN_ABSENT}'; "
        "END IF; "
        "RETURN QUERY "
        f"  UPDATE {_JOBS} AS j "
        "     SET status = 'RUNNING', updated_at = now() "
        "   WHERE j.id = p_job_id AND j.plan_id = v_plan_id "
        "     AND j.status = 'CLAIMED' AND j.claimed_by = p_worker_id "
        f"  RETURNING {_RETURNING}; "
        "GET DIAGNOSTICS v_found = ROW_COUNT; "
        "IF NOT v_found THEN "
        "  RAISE EXCEPTION 'job % of plan % is not CLAIMED by worker %', "
        "    p_job_id, p_plan_hash, p_worker_id "
        f"    USING ERRCODE = '{_SQLSTATE_NOT_OWNED}'; "
        "END IF; "
        "END; $start$;"
    )

    # 3) release a CLAIMED job (same worker only): CLAIMED -> PENDING, clearing both claim fields.
    op.execute(
        f"CREATE OR REPLACE FUNCTION {_RELEASE_FN}"
        "(p_plan_hash text, p_job_id uuid, p_worker_id text) "
        f"RETURNS TABLE({_RETURN_COLS}) LANGUAGE plpgsql SECURITY DEFINER "
        "SET search_path = pg_catalog AS $release$ "
        "DECLARE v_found boolean; v_plan_id uuid; "
        "BEGIN "
        "IF p_worker_id IS NULL OR btrim(p_worker_id) = '' THEN "
        "  RAISE EXCEPTION 'worker_id must be a non-empty identifier' "
        f"    USING ERRCODE = '{_SQLSTATE_INVALID_WORKER}'; "
        "END IF; "
        f"SELECT p.id INTO v_plan_id FROM {_PLANS} p WHERE p.plan_hash = p_plan_hash; "
        "IF v_plan_id IS NULL THEN "
        "  RAISE EXCEPTION 'accepted L2-F plan is not persisted' "
        f"    USING ERRCODE = '{_SQLSTATE_PLAN_ABSENT}'; "
        "END IF; "
        "RETURN QUERY "
        f"  UPDATE {_JOBS} AS j "
        "     SET status = 'PENDING', claimed_by = NULL, claimed_at = NULL, updated_at = now() "
        "   WHERE j.id = p_job_id AND j.plan_id = v_plan_id "
        "     AND j.status = 'CLAIMED' AND j.claimed_by = p_worker_id "
        f"  RETURNING {_RETURNING}; "
        "GET DIAGNOSTICS v_found = ROW_COUNT; "
        "IF NOT v_found THEN "
        "  RAISE EXCEPTION 'job % of plan % is not CLAIMED by worker %', "
        "    p_job_id, p_plan_hash, p_worker_id "
        f"    USING ERRCODE = '{_SQLSTATE_NOT_OWNED}'; "
        "END IF; "
        "END; $release$;"
    )


def _apply_least_privilege() -> None:
    """EXECUTE only for minos_runner + minos_admin; PUBLIC and every other role denied. No
    direct table mutation grant is issued to any application role."""
    for sig in _F4_FUNCTION_SIGS:
        op.execute(f"REVOKE ALL ON FUNCTION {sig} FROM PUBLIC;")
        for role in _DENIED_ROLES:
            op.execute(f"REVOKE ALL ON FUNCTION {sig} FROM {role};")
        op.execute(f"REVOKE ALL ON FUNCTION {sig} FROM minos_runner;")
        op.execute(f"GRANT EXECUTE ON FUNCTION {sig} TO minos_runner;")
        op.execute(f"GRANT EXECUTE ON FUNCTION {sig} TO minos_admin;")
    # the guard is a trigger function: never directly executable by an application role.
    op.execute(f"REVOKE ALL ON FUNCTION {_GUARD}() FROM PUBLIC;")
    for role in ("minos_runner", *_DENIED_ROLES):
        op.execute(f"REVOKE ALL ON FUNCTION {_GUARD}() FROM {role};")
    # F4 grants NO direct table privilege: the functions are the only mutation path.
    op.execute(f"REVOKE ALL ON {_JOBS} FROM PUBLIC;")
    for role in ("minos_runner", *_DENIED_ROLES):
        op.execute(f"REVOKE ALL ON {_JOBS} FROM {role};")


def upgrade() -> None:
    op.execute("SET ROLE minos_admin")
    _create_transition_guard()
    _create_claim_functions()
    _apply_least_privilege()
    op.execute("RESET ROLE")


def downgrade() -> None:
    op.execute("SET ROLE minos_admin")
    op.execute(f"DROP TRIGGER IF EXISTS trg_l2f_jobs_transition_guard ON {_JOBS};")
    for sig in _F4_FUNCTION_SIGS:
        op.execute(f"DROP FUNCTION IF EXISTS {sig};")
    op.execute(f"DROP FUNCTION IF EXISTS {_GUARD}();")
    # restore exact 0006 behavior: the job table carries no application-role privilege there.
    op.execute(f"REVOKE ALL ON {_JOBS} FROM PUBLIC;")
    for role in ("minos_runner", *_DENIED_ROLES):
        op.execute(f"REVOKE ALL ON {_JOBS} FROM {role};")
    op.execute("RESET ROLE")
