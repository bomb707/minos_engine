"""Admit PHASE_D to the runner boundary — the VALIDATION confirmation, and nothing wider.

``0011`` admitted one phase, ``0016`` a second, ``0020`` a third. This is the fourth, and the last
of this search: the authority table's phase vocabulary gains ``PHASE_D``, the canary rule extends
by one clause, and one new resolver plus one new bootstrap are created. As before, the phase is
fixed inside each function so no caller ever names one.

Two things make Phase D different from its three predecessors, and both are enforced here rather
than trusted to the caller:

* **The partition inverts.** Phases A, B and C resolve TRAIN members and refuse everything else.
  Phase D resolves ``validation`` members and refuses everything else — including TRAIN. The two
  predicates are mutually exclusive, so a Phase-C job cannot be resolved through the Phase-D
  interface and a Phase-D job cannot be resolved through the Phase-C one. TEST is reachable
  through neither.
* **There is no racing, so there is no batch.** A Phase-D authority is complete or it is not: the
  bootstrap requires exactly four configurations, exactly ten members and exactly forty logical
  jobs, and refuses any other shape.

The bootstrap stays truth-free in the same sense as 0019's and 0020's: it takes no arguments and
returns two strings — the plan hash and the execution environment the frozen search ran under. A
validation worker still cannot nominate a plan, a runtime, a partition or a truth path.

This migration is source support for a SEPARATE validation database. It is deliberately NOT applied
to the completed TRAIN baseline store, which is scientifically closed.

``downgrade`` restores 0020 exactly, and REFUSES while a ``PHASE_D`` authority exists, for the same
reason 0020 refuses while a Phase-C one does: that table is append-only scientific lineage, and
squeezing the row back into an A/B/C-only CHECK would mean deleting or relabelling it.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0021_l2f2_validation_execution"
down_revision: str | None = "0020_l2f2_phase_c_execution"
branch_labels = None
depends_on = None

_SCHEMA = "experiments"
_AUTHORITIES_TABLE = "l2f2_execution_authorities"
_AUTHORITIES = f"{_SCHEMA}.{_AUTHORITIES_TABLE}"

_RESOLVE_D_FN = "experiments.l2f2_resolve_claimed_phase_d_execution"
_RESOLVE_D_SIG = f"{_RESOLVE_D_FN}(text, uuid, text)"
_BOOTSTRAP_FN = "experiments.l2f2_resolve_phase_d_runner_bootstrap"
_BOOTSTRAP_SIG = f"{_BOOTSTRAP_FN}()"

_PHASE_CK = "ck_l2f2_authority_phase"
_CANARY_PHASE_CK = "ck_l2f2_authority_canary_phase"
_CANARY_COLUMN = "canary_job_key"

#: what 0020 admitted, and what 0021 admits. There is no fifth phase in this search.
_PHASES_0020 = ("PHASE_A", "PHASE_B", "PHASE_C")
_PHASES_0021 = ("PHASE_A", "PHASE_B", "PHASE_C", "PHASE_D")

_CONTROL_PLANE = "minos_admin"
_DENIED_ROLES = ("minos_live", "minos_trainer", "minos_evaluator")

#: the frozen L2-F2-B protocol this boundary executes under — identical to 0011's and 0020's.
_PROTOCOL_HASH = "c548e190571f5e964560cf30021a520ea8aad6674569fa3202af880d7dff77d1"

#: the frozen Phase-D shape. Checked, never returned: the runner has no use for it.
_PHASE_D_MEMBERS = 10
_PHASE_D_CANDIDATES = 4
_PHASE_D_JOBS = _PHASE_D_MEMBERS * _PHASE_D_CANDIDATES  # 40

#: the partition Phase D resolves, and the two it must never reach.
_VALIDATION = "validation"

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


def _phase_d_resolver() -> str:
    """0020's resolver body with TWO differences: the phase, and the partition.

    Everything else is deliberately identical — the authority must cite the frozen protocol, the
    job must belong to that authority's plan, be CLAIMED or RUNNING, and be owned by this worker.
    No truth digest, no mutation digest and no evaluation row is reachable through this interface.

    The partition predicate is ``validation`` rather than ``train``. That is not a relaxation: it
    is the same single-partition restriction pointed at the one partition this stage confirms, and
    it excludes TRAIN and TEST alike.
    """
    return (
        f"CREATE OR REPLACE FUNCTION {_RESOLVE_D_FN}"
        "(p_plan_hash text, p_job_id uuid, p_worker_id text) "
        f"RETURNS TABLE({_RESOLVE_COLS}) LANGUAGE plpgsql SECURITY DEFINER "
        "SET search_path = pg_catalog, public AS $resolve$ "
        "DECLARE v_auth record; BEGIN "
        "IF p_worker_id IS NULL OR btrim(p_worker_id) = '' THEN "
        "  RAISE EXCEPTION 'worker_id must be a non-empty identifier' "
        f"    USING ERRCODE = '{_SQLSTATE_INVALID_WORKER}'; END IF; "
        # the plan must carry a FROZEN-protocol PHASE_D authority. There is no fallback to any
        # TRAIN phase: a Phase-A/B/C plan resolved here is simply not found.
        f"SELECT a.* INTO v_auth FROM {_AUTHORITIES} a "
        "  WHERE a.plan_hash = p_plan_hash AND a.phase = 'PHASE_D'; "
        "IF NOT FOUND THEN "
        "  RAISE EXCEPTION 'plan % has no PHASE_D L2-F2 execution authority', p_plan_hash "
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
        # VALIDATION only: TRAIN and TEST are both structurally unreachable here.
        f"   AND pm.partition = '{_VALIDATION}'; "
        "IF NOT FOUND THEN "
        "  RAISE EXCEPTION 'job % of plan % is not an owned VALIDATION job for worker %', "
        f"    p_job_id, p_plan_hash, p_worker_id USING ERRCODE = '{_SQLSTATE_NOT_OWNED}'; "
        "END IF; END; $resolve$;"
    )


def _bootstrap_function() -> str:
    """The complete function body. Every check is inside; the caller supplies nothing.

    Phase D has no predecessor phase in ITS OWN database — the validation store is separate, and
    the TRAIN evidence that chose the finalists lives in the closed baseline store. So this
    bootstrap does not reach for a completed prior campaign the way 0020's reaches for Phase B.
    What it verifies instead is that exactly one Phase-D authority exists, that it cites the frozen
    protocol, that it binds its own persisted plan, that the plan is a VALIDATION plan, and that
    its shape is exactly the frozen 4 x 10 = 40. The execution environment is read from the
    authority rather than re-derived from outcomes, because at bootstrap time there may be none.
    """
    return (
        f"CREATE OR REPLACE FUNCTION {_BOOTSTRAP_FN}() "
        "RETURNS TABLE(plan_hash text, execution_environment_hash text) "
        "LANGUAGE plpgsql SECURITY DEFINER STABLE "
        "SET search_path = pg_catalog, public AS $bootstrap$ "
        "DECLARE v_d record; v_plan record; v_members bigint; v_nonval bigint; BEGIN "
        # ---- exactly one PHASE_D authority, under the frozen protocol ---------------------
        f"SELECT a.* INTO v_d FROM {_AUTHORITIES} a "
        f"  WHERE a.phase = 'PHASE_D' AND a.baseline_protocol_hash = '{_PROTOCOL_HASH}'; "
        "IF NOT FOUND THEN "
        "  RAISE EXCEPTION 'no PHASE_D execution authority under the frozen baseline protocol' "
        f"    USING ERRCODE = '{_SQLSTATE_AUTHORITY}'; END IF; "
        f"IF (SELECT count(*) FROM {_AUTHORITIES} a WHERE a.phase = 'PHASE_D') <> 1 THEN "
        "  RAISE EXCEPTION 'more than one PHASE_D execution authority exists; refusing to choose' "
        f"    USING ERRCODE = '{_SQLSTATE_AUTHORITY}'; END IF; "
        # ---- the authority must bind its OWN persisted plan -------------------------------
        "SELECT p.* INTO v_plan FROM experiments.l2f_experiment_plans p "
        "  WHERE p.id = v_d.plan_id AND p.plan_hash = v_d.plan_hash; "
        "IF NOT FOUND THEN "
        "  RAISE EXCEPTION 'the PHASE_D authority does not bind its persisted plan' "
        f"    USING ERRCODE = '{_SQLSTATE_AUTHORITY}'; END IF; "
        # ---- the plan must be a VALIDATION plan, wholly ------------------------------------
        "SELECT count(*) INTO v_members FROM experiments.l2f_experiment_plan_members pm "
        "  WHERE pm.plan_id = v_d.plan_id; "
        "SELECT count(*) INTO v_nonval FROM experiments.l2f_experiment_plan_members pm "
        f"  WHERE pm.plan_id = v_d.plan_id AND pm.partition <> '{_VALIDATION}'; "
        "IF v_nonval <> 0 THEN "
        "  RAISE EXCEPTION 'the PHASE_D plan holds % non-VALIDATION member(s); validation "
        "confirms on the VALIDATION partition only', v_nonval "
        f"    USING ERRCODE = '{_SQLSTATE_AUTHORITY}'; END IF; "
        f"IF v_members <> {_PHASE_D_MEMBERS} THEN "
        "  RAISE EXCEPTION 'the PHASE_D plan holds % members, the frozen protocol fixes %', "
        f"    v_members, {_PHASE_D_MEMBERS} "
        f"    USING ERRCODE = '{_SQLSTATE_AUTHORITY}'; END IF; "
        # ---- the frozen 4 x 10 = 40 shape, and no other -----------------------------------
        f"IF v_d.member_count <> {_PHASE_D_MEMBERS} OR v_d.candidate_count <> "
        f"   {_PHASE_D_CANDIDATES} OR v_d.logical_job_count <> {_PHASE_D_JOBS} THEN "
        "  RAISE EXCEPTION 'the PHASE_D authority is not the frozen 4 x 10 = 40 validation' "
        f"    USING ERRCODE = '{_SQLSTATE_AUTHORITY}'; END IF; "
        "IF v_d.execution_environment_hash IS NULL THEN "
        "  RAISE EXCEPTION 'the PHASE_D authority carries no execution environment identity' "
        f"    USING ERRCODE = '{_SQLSTATE_AUTHORITY}'; END IF; "
        # ---- the two strings the runner may know -----------------------------------------
        "RETURN QUERY SELECT v_d.plan_hash::text, v_d.execution_environment_hash::text; "
        "END; $bootstrap$;"
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
        _PHASE_CK, _AUTHORITIES_TABLE, _phase_check(_PHASES_0021), schema=_SCHEMA
    )
    op.drop_constraint(_CANARY_PHASE_CK, _AUTHORITIES_TABLE, schema=_SCHEMA, type_="check")
    op.create_check_constraint(
        _CANARY_PHASE_CK, _AUTHORITIES_TABLE, _canary_phase_check(_PHASES_0021), schema=_SCHEMA
    )
    # a SECURITY DEFINER function executes with its OWNER's authority; both are created as the
    # control plane, which is what 0017 and 0018 had to correct for their predecessors.
    op.execute(f"SET ROLE {_CONTROL_PLANE}")
    op.execute(_phase_d_resolver())
    op.execute(_bootstrap_function())
    op.execute("RESET ROLE")
    _grant(_RESOLVE_D_SIG)
    _grant(_BOOTSTRAP_SIG)


def downgrade() -> None:
    """Restore 0020 exactly — but REFUSE while a Phase-D authority exists."""
    conn = op.get_bind()
    # checked BEFORE anything is altered, so a refusal leaves the database exactly as it was.
    phase_d = conn.execute(
        sa.text(f"SELECT count(*) FROM {_AUTHORITIES} WHERE phase = 'PHASE_D'")  # noqa: S608
    ).scalar_one()
    if phase_d:
        raise RuntimeError(
            f"cannot downgrade {_AUTHORITIES} to an A/B/C-only boundary: {phase_d} PHASE_D "
            "execution authority row(s) exist. That table is append-only scientific lineage, so "
            "there is no honest way back — dropping the row or changing its phase would falsify "
            "the record of a validation confirmation."
        )
    op.execute(f"SET ROLE {_CONTROL_PLANE}")
    op.execute(f"DROP FUNCTION IF EXISTS {_BOOTSTRAP_SIG};")
    op.execute(f"DROP FUNCTION IF EXISTS {_RESOLVE_D_SIG};")
    op.execute("RESET ROLE")
    op.drop_constraint(_CANARY_PHASE_CK, _AUTHORITIES_TABLE, schema=_SCHEMA, type_="check")
    op.create_check_constraint(
        _CANARY_PHASE_CK, _AUTHORITIES_TABLE, _canary_phase_check(_PHASES_0020), schema=_SCHEMA
    )
    op.drop_constraint(_PHASE_CK, _AUTHORITIES_TABLE, schema=_SCHEMA, type_="check")
    op.create_check_constraint(
        _PHASE_CK, _AUTHORITIES_TABLE, _phase_check(_PHASES_0020), schema=_SCHEMA
    )
