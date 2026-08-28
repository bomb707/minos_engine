"""Admit PHASE_C to the runner boundary — the third and last TRAIN phase, and nothing wider.

``0011`` admitted one phase, ``0016`` a second. This is the third, and it stays exactly as narrow
as its predecessors: the authority table's phase vocabulary gains ``PHASE_C`` and nothing else
(validation and test are not execution phases of this boundary at all), the canary rule extends by
one arm — Phase C, like Phase B, must not carry one — and two functions are added, each fixed to
``PHASE_C`` internally so no caller ever names a phase.

* ``experiments.l2f2_resolve_claimed_phase_c_execution`` resolves the truth-free scientific
  identity of a job this worker already owns, exactly as its Phase-A and Phase-B counterparts do.
* ``experiments.l2f2_resolve_phase_c_runner_bootstrap`` tells a truth-free worker the only two
  things it needs: which plan it may claim within, and the runtime that plan's science was chosen
  under. That runtime is derived from the COMPLETE Phase-B EXECUTION ledgers — never from the
  ``evaluation`` schema, which the runner is denied and does not need: runtime lineage is a
  question about execution, not about results.

The Phase-A and Phase-B resolvers are not redefined. Each phase keeps its own privileged
interface, which is what makes "the Phase-A interface is Phase-A-only" a regression others can
rely on. Nothing about claiming changes; ``minos_l2f_claim_next_job`` has been plan-scoped since
``0007``.

The upgrade is additive over a populated store and inserts no Phase-C row of any kind. The
downgrade REFUSES while a ``PHASE_C`` authority exists, because squeezing that row back into an
A/B-only CHECK would mean deleting or relabelling append-only scientific lineage.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0020_l2f2_phase_c_execution"
down_revision: str | None = "0019_l2f2_phase_b_bootstrap"
branch_labels = None
depends_on = None

_SCHEMA = "experiments"
_AUTHORITIES_TABLE = "l2f2_execution_authorities"
_AUTHORITIES = f"{_SCHEMA}.{_AUTHORITIES_TABLE}"
_PLANS = "experiments.l2f_experiment_plans"
_JOBS = "experiments.l2f_experiment_jobs"
_RESULTS = "experiments.l2f_execution_results"
_FAILURES = "experiments.l2f_execution_failures"

_RESOLVE_C_FN = "experiments.l2f2_resolve_claimed_phase_c_execution"
_RESOLVE_C_SIG = f"{_RESOLVE_C_FN}(text, uuid, text)"
_BOOTSTRAP_FN = "experiments.l2f2_resolve_phase_c_runner_bootstrap"
_BOOTSTRAP_SIG = f"{_BOOTSTRAP_FN}()"

_PHASE_CK = "ck_l2f2_authority_phase"
_CANARY_PHASE_CK = "ck_l2f2_authority_canary_phase"
_CANARY_COLUMN = "canary_job_key"

#: what 0016 admitted, and what 0020 admits. A fourth phase would be a fourth migration.
_PHASES_0019 = ("PHASE_A", "PHASE_B")
_PHASES_0020 = ("PHASE_A", "PHASE_B", "PHASE_C")

_CONTROL_PLANE = "minos_admin"
_DENIED_ROLES = ("minos_live", "minos_trainer", "minos_evaluator")

#: the frozen L2-F2-B protocol this boundary executes under — identical to 0011's.
_PROTOCOL_HASH = "c548e190571f5e964560cf30021a520ea8aad6674569fa3202af880d7dff77d1"

#: the frozen Phase-C shape. Checked, never returned: the runner has no use for it.
_PHASE_C_MEMBERS = 50
_PHASE_C_CANDIDATES = 10
_PHASE_C_JOBS = _PHASE_C_MEMBERS * _PHASE_C_CANDIDATES

_SQLSTATE_INVALID_WORKER = "MN001"
_SQLSTATE_NOT_OWNED = "MN003"
_SQLSTATE_AUTHORITY = "MN030"

_RESOLVE_COLS = (
    "job_id uuid, job_key text, plan_id uuid, plan_member_id uuid, plan_config_id uuid, "
    "member_index integer, partition text, dataset_id text, round_id text, chromosome text, "
    "region_hash text, region_start0 bigint, region_end0_exclusive bigint, "
    "bam_sha256 text, bai_sha256 text, reference_sha256 text, fai_sha256 text, "
    "bam_size_bytes bigint, profile_id text, content_hash text, feature_values_hash text, "
    "config_index integer, config_hash text, parameter_space_hash text, "
    "config_media_type text, config_uri text, config_sha256 text, config_size_bytes integer"
)


def _phase_c_resolver() -> str:
    """0011's resolver body with ONE difference: the phase it will accept an authority for.

    Everything else is deliberately identical — the authority must cite the frozen protocol, the
    job must belong to that authority's plan, be CLAIMED or RUNNING, be owned by this worker, and
    be a TRAIN member. No truth digest, no mutation digest, no evaluation row and no non-TRAIN
    member is reachable through this interface, and the phase is fixed here rather than passed in.
    """
    return (
        f"CREATE OR REPLACE FUNCTION {_RESOLVE_C_FN}"
        "(p_plan_hash text, p_job_id uuid, p_worker_id text) "
        f"RETURNS TABLE({_RESOLVE_COLS}) LANGUAGE plpgsql SECURITY DEFINER "
        "SET search_path = pg_catalog, public AS $resolve$ "
        "DECLARE v_auth record; BEGIN "
        "IF p_worker_id IS NULL OR btrim(p_worker_id) = '' THEN "
        "  RAISE EXCEPTION 'worker_id must be a non-empty identifier' "
        f"    USING ERRCODE = '{_SQLSTATE_INVALID_WORKER}'; END IF; "
        # the plan must carry a FROZEN-protocol PHASE_C execution authority. There is no fallback
        # to PHASE_A: a Phase-A plan resolved here is simply not found.
        f"SELECT a.* INTO v_auth FROM {_AUTHORITIES} a "
        "  WHERE a.plan_hash = p_plan_hash AND a.phase = 'PHASE_C'; "
        "IF NOT FOUND THEN "
        "  RAISE EXCEPTION 'plan % has no PHASE_C L2-F2 execution authority', p_plan_hash "
        f"    USING ERRCODE = '{_SQLSTATE_AUTHORITY}'; END IF; "
        f"IF v_auth.baseline_protocol_hash <> '{_PROTOCOL_HASH}' THEN "
        "  RAISE EXCEPTION 'execution authority does not cite the frozen baseline protocol' "
        f"    USING ERRCODE = '{_SQLSTATE_AUTHORITY}'; END IF; "
        "RETURN QUERY "
        "SELECT j.id, j.job_key::text, j.plan_id, j.plan_member_id, j.plan_config_id, "
        "       pm.member_index::integer, pm.partition::text, dr.dataset_id::text, "
        "       dr.round_id::text, "
        "       dr.chromosome::text, dr.region_hash::text, dr.region_start0::bigint, "
        "       dr.region_end0_exclusive::bigint, dr.bam_sha256::text, dr.bai_sha256::text, "
        "       dr.reference_sha256::text, dr.fai_sha256::text, dr.bam_size_bytes::bigint, "
        "       bp.profile_id::text, bp.content_hash::text, pm.feature_values_hash::text, "
        "       pc.config_index::integer, pc.config_hash::text, pc.parameter_space_hash::text, "
        "       cp.media_type::text, a.uri::text, a.sha256::text, a.size_bytes::integer "
        "  FROM experiments.l2f_experiment_jobs j "
        "  JOIN experiments.l2f_experiment_plan_members pm ON pm.id = j.plan_member_id "
        "  JOIN experiments.l2f_experiment_plan_configs pc ON pc.id = j.plan_config_id "
        "  JOIN experiments.l2f_config_payloads cp ON cp.id = pc.config_payload_id "
        "  JOIN catalog.artifacts a ON a.id = cp.artifact_id "
        "  JOIN catalog.dataset_registry dr ON dr.id = pm.dataset_registry_id "
        "  JOIN profiling.bam_profiles bp ON bp.id = pm.bam_profile_id "
        " WHERE j.id = p_job_id AND j.plan_id = v_auth.plan_id "
        "   AND j.status IN ('CLAIMED', 'RUNNING') AND j.claimed_by = p_worker_id "
        # TRAIN only: a non-train member is structurally unreachable through this interface.
        "   AND pm.partition = 'train'; "
        "IF NOT FOUND THEN "
        "  RAISE EXCEPTION 'job % of plan % is not an owned TRAIN job for worker %', "
        f"    p_job_id, p_plan_hash, p_worker_id USING ERRCODE = '{_SQLSTATE_NOT_OWNED}'; "
        "END IF; END; $resolve$;"
    )


def _bootstrap_function() -> str:
    """The complete function body. Every check is inside; the caller supplies nothing."""
    return (
        f"CREATE OR REPLACE FUNCTION {_BOOTSTRAP_FN}() "
        "RETURNS TABLE(plan_hash text, execution_environment_hash text) "
        "LANGUAGE plpgsql SECURITY DEFINER STABLE "
        "SET search_path = pg_catalog, public AS $bootstrap$ "
        "DECLARE v_c record; v_b record; v_plan record; v_jobs bigint; v_terminal bigint; "
        "        v_results bigint; v_failures bigint; v_env text; v_envs bigint; BEGIN "
        # ---- exactly one PHASE_C authority, under the frozen protocol ---------------------
        f"SELECT a.* INTO v_c FROM {_AUTHORITIES} a "
        f"  WHERE a.phase = 'PHASE_C' AND a.baseline_protocol_hash = '{_PROTOCOL_HASH}'; "
        "IF NOT FOUND THEN "
        "  RAISE EXCEPTION 'no PHASE_C execution authority under the frozen baseline protocol' "
        f"    USING ERRCODE = '{_SQLSTATE_AUTHORITY}'; END IF; "
        f"IF (SELECT count(*) FROM {_AUTHORITIES} a WHERE a.phase = 'PHASE_C') <> 1 THEN "
        "  RAISE EXCEPTION 'more than one PHASE_C execution authority exists; refusing to choose' "
        f"    USING ERRCODE = '{_SQLSTATE_AUTHORITY}'; END IF; "
        # ---- it must bind its EXACT persisted plan --------------------------------------
        f"SELECT p.* INTO v_plan FROM {_PLANS} p "
        "  WHERE p.id = v_c.plan_id AND p.plan_hash = v_c.plan_hash; "
        "IF NOT FOUND THEN "
        "  RAISE EXCEPTION 'the PHASE_C authority does not bind its persisted plan' "
        f"    USING ERRCODE = '{_SQLSTATE_AUTHORITY}'; END IF; "
        "IF v_plan.partition <> 'train' THEN "
        "  RAISE EXCEPTION 'the PHASE_C plan is not a TRAIN plan' "
        f"    USING ERRCODE = '{_SQLSTATE_AUTHORITY}'; END IF; "
        "IF v_plan.candidate_set_hash IS DISTINCT FROM v_c.candidate_set_hash "
        "   OR v_plan.parameter_space_hash IS DISTINCT FROM v_c.parameter_space_hash "
        "   OR v_plan.train_member_count IS DISTINCT FROM v_c.member_count "
        "   OR v_plan.candidate_count IS DISTINCT FROM v_c.candidate_count "
        "   OR v_plan.logical_job_count IS DISTINCT FROM v_c.logical_job_count THEN "
        "  RAISE EXCEPTION 'the PHASE_C authority disagrees with its persisted plan' "
        f"    USING ERRCODE = '{_SQLSTATE_AUTHORITY}'; END IF; "
        f"IF v_c.member_count <> {_PHASE_C_MEMBERS} OR v_c.candidate_count <> "
        f"   {_PHASE_C_CANDIDATES} OR v_c.logical_job_count <> {_PHASE_C_JOBS} THEN "
        "  RAISE EXCEPTION 'the PHASE_C authority is not the frozen 10 x 50 = 500 confirmation' "
        f"    USING ERRCODE = '{_SQLSTATE_AUTHORITY}'; END IF; "
        # ---- exactly one PHASE_B authority, whose campaign supplies the runtime ------------
        f"SELECT a.* INTO v_b FROM {_AUTHORITIES} a "
        f"  WHERE a.phase = 'PHASE_B' AND a.baseline_protocol_hash = '{_PROTOCOL_HASH}'; "
        "IF NOT FOUND THEN "
        "  RAISE EXCEPTION 'no PHASE_B execution authority under the frozen baseline protocol' "
        f"    USING ERRCODE = '{_SQLSTATE_AUTHORITY}'; END IF; "
        f"IF (SELECT count(*) FROM {_AUTHORITIES} a WHERE a.phase = 'PHASE_B') <> 1 THEN "
        "  RAISE EXCEPTION 'more than one PHASE_B execution authority exists; refusing to choose' "
        f"    USING ERRCODE = '{_SQLSTATE_AUTHORITY}'; END IF; "
        # ---- that campaign must be durably COMPLETE and terminal --------------------------
        f"SELECT count(*), count(*) FILTER (WHERE j.status IN ('SUCCEEDED','FAILED')) "
        f"  INTO v_jobs, v_terminal FROM {_JOBS} j WHERE j.plan_id = v_b.plan_id; "
        "IF v_jobs <> v_b.logical_job_count THEN "
        "  RAISE EXCEPTION 'PHASE_B holds % of % logical jobs; its runtime lineage is incomplete',"
        "    v_jobs, v_b.logical_job_count "
        f"    USING ERRCODE = '{_SQLSTATE_AUTHORITY}'; END IF; "
        "IF v_terminal <> v_jobs THEN "
        "  RAISE EXCEPTION 'PHASE_B has % non-terminal job(s); the campaign is still running', "
        "    v_jobs - v_terminal "
        f"    USING ERRCODE = '{_SQLSTATE_AUTHORITY}'; END IF; "
        f"SELECT count(*) INTO v_results FROM {_RESULTS} r "
        f"  JOIN {_JOBS} j ON j.id = r.job_id WHERE j.plan_id = v_b.plan_id; "
        f"SELECT count(*) INTO v_failures FROM {_FAILURES} f "
        f"  JOIN {_JOBS} j ON j.id = f.job_id WHERE j.plan_id = v_b.plan_id; "
        "IF v_results + v_failures <> v_b.logical_job_count THEN "
        "  RAISE EXCEPTION 'PHASE_B has % execution outcome(s) for % jobs', "
        "    v_results + v_failures, v_b.logical_job_count "
        f"    USING ERRCODE = '{_SQLSTATE_AUTHORITY}'; END IF; "
        # ---- exactly ONE runtime produced them. A failure carries an identity too. --------
        "SELECT count(DISTINCT t.h), min(t.h) INTO v_envs, v_env FROM ("
        f"  SELECT r.execution_environment_hash AS h FROM {_RESULTS} r "
        f"    JOIN {_JOBS} j ON j.id = r.job_id WHERE j.plan_id = v_b.plan_id "
        "  UNION ALL "
        f"  SELECT f.execution_environment_hash AS h FROM {_FAILURES} f "
        f"    JOIN {_JOBS} j ON j.id = f.job_id WHERE j.plan_id = v_b.plan_id) t; "
        "IF v_envs <> 1 OR v_env IS NULL THEN "
        "  RAISE EXCEPTION 'PHASE_B outcomes carry % distinct execution environments; Phase C "
        "explores a design chosen from one runtime''s numbers', v_envs "
        f"    USING ERRCODE = '{_SQLSTATE_AUTHORITY}'; END IF; "
        # ---- the two strings the runner may know -----------------------------------------
        "RETURN QUERY SELECT v_c.plan_hash::text, v_env::text; END; $bootstrap$;"
    )


def _phase_check(phases: tuple[str, ...]) -> str:
    return "phase IN (" + ", ".join(f"'{p}'" for p in phases) + ")"


def _canary_phase_check(phases: tuple[str, ...]) -> str:
    """Phase A must carry its canary; every later phase must not carry one at all."""
    without = " OR ".join(
        f"(phase = '{p}' AND {_CANARY_COLUMN} IS NULL)" for p in phases if p != "PHASE_A"
    )
    return f"(phase = 'PHASE_A' AND {_CANARY_COLUMN} IS NOT NULL) OR {without}"


def _grant(signature: str) -> None:
    """The runner and the control plane. Nobody else, and no table privilege anywhere."""
    op.execute(f"REVOKE ALL ON FUNCTION {signature} FROM PUBLIC;")
    for role in _DENIED_ROLES:
        op.execute(f"REVOKE ALL ON FUNCTION {signature} FROM {role};")
    op.execute(f"GRANT EXECUTE ON FUNCTION {signature} TO minos_runner;")
    op.execute(f"GRANT EXECUTE ON FUNCTION {signature} TO {_CONTROL_PLANE};")


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
    op.drop_constraint(_PHASE_CK, _AUTHORITIES_TABLE, schema=_SCHEMA, type_="check")
    op.create_check_constraint(
        _PHASE_CK, _AUTHORITIES_TABLE, _phase_check(_PHASES_0020), schema=_SCHEMA
    )
    op.drop_constraint(_CANARY_PHASE_CK, _AUTHORITIES_TABLE, schema=_SCHEMA, type_="check")
    op.create_check_constraint(
        _CANARY_PHASE_CK, _AUTHORITIES_TABLE, _canary_phase_check(_PHASES_0020), schema=_SCHEMA
    )
    # a SECURITY DEFINER function executes with its OWNER's authority; both are created as the
    # control plane, which is what 0017 and 0018 had to correct for their predecessors.
    op.execute(f"SET ROLE {_CONTROL_PLANE}")
    op.execute(_phase_c_resolver())
    op.execute(_bootstrap_function())
    op.execute("RESET ROLE")
    _grant(_RESOLVE_C_SIG)
    _grant(_BOOTSTRAP_SIG)


def downgrade() -> None:
    """Restore 0019 exactly — but REFUSE while a Phase-C authority exists."""
    conn = op.get_bind()
    # checked BEFORE anything is altered, so a refusal leaves the database exactly as it was.
    phase_c = conn.execute(
        sa.text(f"SELECT count(*) FROM {_AUTHORITIES} WHERE phase = 'PHASE_C'")  # noqa: S608
    ).scalar_one()
    if phase_c:
        raise RuntimeError(
            f"cannot downgrade {_AUTHORITIES} to an A/B-only boundary: {phase_c} PHASE_C "
            "execution authority row(s) exist. That table is append-only scientific lineage, so "
            "there is no honest way back — dropping the row or changing its phase would falsify "
            "the record of a Phase-C confirmation."
        )
    op.execute(f"SET ROLE {_CONTROL_PLANE}")
    op.execute(f"DROP FUNCTION IF EXISTS {_BOOTSTRAP_SIG};")
    op.execute(f"DROP FUNCTION IF EXISTS {_RESOLVE_C_SIG};")
    op.execute("RESET ROLE")
    op.drop_constraint(_CANARY_PHASE_CK, _AUTHORITIES_TABLE, schema=_SCHEMA, type_="check")
    op.create_check_constraint(
        _CANARY_PHASE_CK, _AUTHORITIES_TABLE, _canary_phase_check(_PHASES_0019), schema=_SCHEMA
    )
    op.drop_constraint(_PHASE_CK, _AUTHORITIES_TABLE, schema=_SCHEMA, type_="check")
    op.create_check_constraint(
        _PHASE_CK, _AUTHORITIES_TABLE, _phase_check(_PHASES_0019), schema=_SCHEMA
    )
