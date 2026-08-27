"""Let the RUNNER learn which Phase-B plan it may consume, without reading any science.

The first real Phase-B invocation failed before it claimed anything, and it failed for the right
reason: ``execute_next_l2f2_phase_b_job`` opened the store as ``minos_runner_svc`` and then called
``build_l2f2_phase_b_authority``, whose derivation reads the completed Phase-A **scientific**
ledger — evaluations, scores, dataset identities. The runner is denied all of that on purpose, so
the runner boundary had accidentally taken a dependency on the control plane's derivation.

Granting the runner those reads would trade the whole point of the boundary for convenience. The
runner does not need the Phase-B authority; it needs two facts:

* **which plan** it is authorized to claim within, and
* **which runtime** that plan's science was chosen under, so a worker on a different JVM or
  interpreter refuses before it consumes an observation.

``experiments.l2f2_resolve_phase_b_runner_bootstrap()`` returns exactly those two strings and
takes no arguments — the caller nominates nothing, so there is no parameter through which a worker
could point itself at another plan or another runtime. Everything the answer depends on is checked
inside: exactly one ``PHASE_B`` authority under the frozen protocol, bound to its exact persisted
TRAIN plan with matching identities and the frozen 10 × 48 = 480 shape; exactly one ``PHASE_A``
authority; that Phase-A campaign durably complete and terminal; and its execution outcomes
carrying exactly one execution-environment hash.

Two things it deliberately does NOT do. It reads **nothing** in the ``evaluation`` schema — not a
score, not an admission, not a truth identity — because runtime lineage is a question about
execution, not about results; that is what makes it safe to hand to a truth-free principal. And it
does not decide Phase-B completeness or admit any Phase-A *evaluation* state: a Phase-A campaign
with legitimate candidate execution failures (the real one has five) is complete for this purpose,
because a failure carries a runtime identity exactly as a success does.

No table, column, constraint, trigger or grant on any relation changes.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0019_l2f2_phase_b_bootstrap"
down_revision: str | None = "0018_l2f2_eval_owner_fix"
branch_labels = None
depends_on = None

_CONTROL_PLANE = "minos_admin"
_BOOTSTRAP_FN = "experiments.l2f2_resolve_phase_b_runner_bootstrap"
_BOOTSTRAP_SIG = f"{_BOOTSTRAP_FN}()"

_AUTHORITIES = "experiments.l2f2_execution_authorities"
_PLANS = "experiments.l2f_experiment_plans"
_JOBS = "experiments.l2f_experiment_jobs"
_RESULTS = "experiments.l2f_execution_results"
_FAILURES = "experiments.l2f_execution_failures"

#: the frozen L2-F2-B protocol both authorities must cite — identical to 0011's constant.
_PROTOCOL_HASH = "c548e190571f5e964560cf30021a520ea8aad6674569fa3202af880d7dff77d1"

#: the frozen Phase-B shape. Checked, never returned: the runner has no use for it.
_PHASE_B_MEMBERS = 10
_PHASE_B_CANDIDATES = 48
_PHASE_B_JOBS = _PHASE_B_MEMBERS * _PHASE_B_CANDIDATES

_DENIED_ROLES = ("minos_live", "minos_trainer", "minos_evaluator")

#: 0011's stable authority SQLSTATE, reused so the Python boundary maps it identically.
_SQLSTATE_AUTHORITY = "MN030"


def _bootstrap_function() -> str:
    """The complete function body. Every check is inside; the caller supplies nothing."""
    return (
        f"CREATE OR REPLACE FUNCTION {_BOOTSTRAP_FN}() "
        "RETURNS TABLE(plan_hash text, execution_environment_hash text) "
        "LANGUAGE plpgsql SECURITY DEFINER STABLE "
        "SET search_path = pg_catalog, public AS $bootstrap$ "
        "DECLARE v_b record; v_a record; v_plan record; v_jobs bigint; v_terminal bigint; "
        "        v_results bigint; v_failures bigint; v_env text; v_envs bigint; BEGIN "
        # ---- exactly one PHASE_B authority, under the frozen protocol ---------------------
        f"SELECT a.* INTO v_b FROM {_AUTHORITIES} a "
        f"  WHERE a.phase = 'PHASE_B' AND a.baseline_protocol_hash = '{_PROTOCOL_HASH}'; "
        "IF NOT FOUND THEN "
        "  RAISE EXCEPTION 'no PHASE_B execution authority under the frozen baseline protocol' "
        f"    USING ERRCODE = '{_SQLSTATE_AUTHORITY}'; END IF; "
        f"IF (SELECT count(*) FROM {_AUTHORITIES} a WHERE a.phase = 'PHASE_B') <> 1 THEN "
        "  RAISE EXCEPTION 'more than one PHASE_B execution authority exists; refusing to choose' "
        f"    USING ERRCODE = '{_SQLSTATE_AUTHORITY}'; END IF; "
        # ---- it must bind its EXACT persisted plan --------------------------------------
        f"SELECT p.* INTO v_plan FROM {_PLANS} p "
        "  WHERE p.id = v_b.plan_id AND p.plan_hash = v_b.plan_hash; "
        "IF NOT FOUND THEN "
        "  RAISE EXCEPTION 'the PHASE_B authority does not bind its persisted plan' "
        f"    USING ERRCODE = '{_SQLSTATE_AUTHORITY}'; END IF; "
        "IF v_plan.partition <> 'train' THEN "
        "  RAISE EXCEPTION 'the PHASE_B plan is not a TRAIN plan' "
        f"    USING ERRCODE = '{_SQLSTATE_AUTHORITY}'; END IF; "
        "IF v_plan.candidate_set_hash IS DISTINCT FROM v_b.candidate_set_hash "
        "   OR v_plan.parameter_space_hash IS DISTINCT FROM v_b.parameter_space_hash "
        "   OR v_plan.train_member_count IS DISTINCT FROM v_b.member_count "
        "   OR v_plan.candidate_count IS DISTINCT FROM v_b.candidate_count "
        "   OR v_plan.logical_job_count IS DISTINCT FROM v_b.logical_job_count THEN "
        "  RAISE EXCEPTION 'the PHASE_B authority disagrees with its persisted plan' "
        f"    USING ERRCODE = '{_SQLSTATE_AUTHORITY}'; END IF; "
        f"IF v_b.member_count <> {_PHASE_B_MEMBERS} OR v_b.candidate_count <> "
        f"   {_PHASE_B_CANDIDATES} OR v_b.logical_job_count <> {_PHASE_B_JOBS} THEN "
        "  RAISE EXCEPTION 'the PHASE_B authority is not the frozen 10 x 48 = 480 screen' "
        f"    USING ERRCODE = '{_SQLSTATE_AUTHORITY}'; END IF; "
        # ---- exactly one PHASE_A authority, whose campaign supplies the runtime ------------
        f"SELECT a.* INTO v_a FROM {_AUTHORITIES} a "
        f"  WHERE a.phase = 'PHASE_A' AND a.baseline_protocol_hash = '{_PROTOCOL_HASH}'; "
        "IF NOT FOUND THEN "
        "  RAISE EXCEPTION 'no PHASE_A execution authority under the frozen baseline protocol' "
        f"    USING ERRCODE = '{_SQLSTATE_AUTHORITY}'; END IF; "
        f"IF (SELECT count(*) FROM {_AUTHORITIES} a WHERE a.phase = 'PHASE_A') <> 1 THEN "
        "  RAISE EXCEPTION 'more than one PHASE_A execution authority exists; refusing to choose' "
        f"    USING ERRCODE = '{_SQLSTATE_AUTHORITY}'; END IF; "
        # ---- that campaign must be durably COMPLETE and terminal --------------------------
        f"SELECT count(*), count(*) FILTER (WHERE j.status IN ('SUCCEEDED','FAILED')) "
        f"  INTO v_jobs, v_terminal FROM {_JOBS} j WHERE j.plan_id = v_a.plan_id; "
        "IF v_jobs <> v_a.logical_job_count THEN "
        "  RAISE EXCEPTION 'PHASE_A holds % of % logical jobs; its runtime lineage is incomplete',"
        "    v_jobs, v_a.logical_job_count "
        f"    USING ERRCODE = '{_SQLSTATE_AUTHORITY}'; END IF; "
        "IF v_terminal <> v_jobs THEN "
        "  RAISE EXCEPTION 'PHASE_A has % non-terminal job(s); the campaign is still running', "
        "    v_jobs - v_terminal "
        f"    USING ERRCODE = '{_SQLSTATE_AUTHORITY}'; END IF; "
        f"SELECT count(*) INTO v_results FROM {_RESULTS} r "
        f"  JOIN {_JOBS} j ON j.id = r.job_id WHERE j.plan_id = v_a.plan_id; "
        f"SELECT count(*) INTO v_failures FROM {_FAILURES} f "
        f"  JOIN {_JOBS} j ON j.id = f.job_id WHERE j.plan_id = v_a.plan_id; "
        "IF v_results + v_failures <> v_a.logical_job_count THEN "
        "  RAISE EXCEPTION 'PHASE_A has % execution outcome(s) for % jobs', "
        "    v_results + v_failures, v_a.logical_job_count "
        f"    USING ERRCODE = '{_SQLSTATE_AUTHORITY}'; END IF; "
        # ---- exactly ONE runtime produced them. A failure carries an identity too. --------
        "SELECT count(DISTINCT t.h), min(t.h) INTO v_envs, v_env FROM ("
        f"  SELECT r.execution_environment_hash AS h FROM {_RESULTS} r "
        f"    JOIN {_JOBS} j ON j.id = r.job_id WHERE j.plan_id = v_a.plan_id "
        "  UNION ALL "
        f"  SELECT f.execution_environment_hash AS h FROM {_FAILURES} f "
        f"    JOIN {_JOBS} j ON j.id = f.job_id WHERE j.plan_id = v_a.plan_id) t; "
        "IF v_envs <> 1 OR v_env IS NULL THEN "
        "  RAISE EXCEPTION 'PHASE_A outcomes carry % distinct execution environments; Phase B "
        "explores a design chosen from one runtime''s numbers', v_envs "
        f"    USING ERRCODE = '{_SQLSTATE_AUTHORITY}'; END IF; "
        # ---- the two strings the runner may know -----------------------------------------
        "RETURN QUERY SELECT v_b.plan_hash::text, v_env::text; END; $bootstrap$;"
    )


def _grant() -> None:
    """The runner and the control plane. Nobody else, and no table privilege anywhere."""
    op.execute(f"REVOKE ALL ON FUNCTION {_BOOTSTRAP_SIG} FROM PUBLIC;")
    for role in _DENIED_ROLES:
        op.execute(f"REVOKE ALL ON FUNCTION {_BOOTSTRAP_SIG} FROM {role};")
    op.execute(f"GRANT EXECUTE ON FUNCTION {_BOOTSTRAP_SIG} TO minos_runner;")
    op.execute(f"GRANT EXECUTE ON FUNCTION {_BOOTSTRAP_SIG} TO {_CONTROL_PLANE};")


def _require_control_plane(conn: sa.Connection) -> None:
    row = (
        conn.execute(
            sa.text("SELECT rolsuper, rolcanlogin FROM pg_roles WHERE rolname = :r"),
            {"r": _CONTROL_PLANE},
        )
        .mappings()
        .one_or_none()
    )
    if row is None:
        raise RuntimeError(f"role {_CONTROL_PLANE!r} does not exist; the control plane is absent")
    if row["rolsuper"] or row["rolcanlogin"]:
        raise RuntimeError(
            f"role {_CONTROL_PLANE!r} is a superuser or can log in; a SECURITY DEFINER function "
            "must not execute with that authority"
        )


def upgrade() -> None:
    _require_control_plane(op.get_bind())
    # a SECURITY DEFINER function executes with its OWNER's authority, so it is created as the
    # control plane — never as the migration login, which 0017 and 0018 had to correct.
    op.execute(f"SET ROLE {_CONTROL_PLANE}")
    op.execute(_bootstrap_function())
    op.execute("RESET ROLE")
    _grant()


def downgrade() -> None:
    """Drop exactly what this migration added. No row, grant or relation is touched."""
    op.execute(f"SET ROLE {_CONTROL_PLANE}")
    op.execute(f"DROP FUNCTION IF EXISTS {_BOOTSTRAP_SIG};")
    op.execute("RESET ROLE")
