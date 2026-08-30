"""Bind the Phase-D boundary to the FROZEN four, not merely to the number four.

``0021``'s bootstrap proves a great deal — one ``PHASE_D`` authority, the frozen protocol, the
authority bound to its own persisted plan, ten validation members, and the shape ``4 x 10 = 40``.
It does not prove *which* four. A validation plan carrying four arbitrary configurations and ten
validation members satisfies every one of those checks, because ``candidate_count = 4`` is an
integer somebody wrote down, not evidence about the configurations themselves.

That gap matters more here than anywhere else in the search. Phase D is the last measurement before
a baseline is promoted; a campaign that validated four configurations nobody chose would look
exactly like one that validated the frozen finalists, and the difference would only surface when
someone re-derived the ranking months later.

So this migration adds two things.

**An immutable Phase-D binding row.** One row per validation campaign, carrying the identities the
Python ``PhaseDAuthority.plan_hash`` already commits to — the finalist-freeze digest, the Phase-C
closure digest, the ordered four config hashes with their inherited Phase-B indices, the seed, the
split manifest, the parameter space, the execution environment, the scoring contract and the
MINOS_SUBNET commit. It is append-only: a binding is the record of which campaign this was, and a
record that can be edited is not one.

**A bootstrap that reads the persisted graph.** ``0021``'s function is replaced in place with
``CREATE OR REPLACE`` — ``0021`` itself is accepted and is not edited — and the replacement walks
the actual ``l2f_experiment_plan_configs`` rows and compares them, in ``config_index`` order,
against the binding's ordered four. It also re-checks the freeze and closure digests, the seed, the
scoring contract, the MINOS_SUBNET commit and the environment, and it still refuses any
non-VALIDATION member. A forged plan now fails on the identity of its configurations rather than
passing on their count.

The bootstrap remains what it was in every other respect: no arguments, truth-free, and returning
exactly two strings. A validation worker still cannot nominate a plan, a runtime, a partition or a
truth path.

``downgrade`` restores ``0022``'s bootstrap and drops the binding table, and REFUSES first while
any binding row exists, for the reason every earlier refusal gives: append-only scientific lineage
has no honest way back.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0023_l2f2_phase_d_binding"
down_revision: str | None = "0022_l2f2_validation_store"
branch_labels = None
depends_on = None

_SCHEMA = "experiments"
_BINDING_TABLE = "l2f2_phase_d_binding"
_BINDING = f"{_SCHEMA}.{_BINDING_TABLE}"
_AUTHORITIES = f"{_SCHEMA}.l2f2_execution_authorities"
_AUTHORITIES_TABLE = "l2f2_execution_authorities"
_PLANS = f"{_SCHEMA}.l2f_experiment_plans"
_PLAN_CONFIGS = f"{_SCHEMA}.l2f_experiment_plan_configs"
_PLAN_MEMBERS = f"{_SCHEMA}.l2f_experiment_plan_members"

_BOOTSTRAP_FN = "experiments.l2f2_resolve_phase_d_runner_bootstrap"
_BOOTSTRAP_SIG = f"{_BOOTSTRAP_FN}()"
_REJECT_MUTATION = "audit.minos_reject_mutation"

_CONTROL_PLANE = "minos_admin"
_DENIED_ROLES = ("minos_live", "minos_trainer", "minos_evaluator")

_PROTOCOL_HASH = "c548e190571f5e964560cf30021a520ea8aad6674569fa3202af880d7dff77d1"
_VALIDATION = "validation"
_PHASE_D_MEMBERS = 10
_PHASE_D_CANDIDATES = 4
_PHASE_D_JOBS = _PHASE_D_MEMBERS * _PHASE_D_CANDIDATES

_SQLSTATE_AUTHORITY = "MN030"


def _uuid(name: str, *, nullable: bool = False) -> sa.Column[str]:
    return sa.Column(name, postgresql.UUID(as_uuid=False), nullable=nullable)


def _sha(name: str) -> sa.Column[str]:
    return sa.Column(name, sa.CHAR(64), nullable=False)


def _hex_ck(column: str, name: str, *, length: int = 64) -> sa.CheckConstraint:
    return sa.CheckConstraint(f"{column} ~ '^[0-9a-f]{{{length}}}$'", name=name)


def _create_binding() -> None:
    op.create_table(
        _BINDING_TABLE,
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=False),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("phase", sa.Text(), nullable=False, server_default="PHASE_D"),
        _sha("baseline_protocol_hash"),
        _uuid("authority_id"),
        _uuid("plan_id"),
        _sha("plan_hash"),
        # the two artifact digests that make this campaign's finalists auditable
        _sha("finalist_freeze_sha256"),
        _sha("phase_c_closure_sha256"),
        # the scientific identities the Python plan hash already commits to
        _sha("parameter_space_hash"),
        _sha("execution_environment_hash"),
        _sha("scoring_contract_hash"),
        sa.Column("minos_subnet_sha", sa.CHAR(40), nullable=False),
        _sha("split_manifest_sha256"),
        _sha("seed_config_hash"),
        # the ordered four, and the inherited Phase-B index of each. Arrays rather than a child
        # table because order is part of the identity and a four-element ordered tuple is the
        # whole value — splitting it would let a row exist with three.
        sa.Column("ordered_config_hashes", postgresql.ARRAY(sa.Text()), nullable=False),
        sa.Column("inherited_candidate_indices", postgresql.ARRAY(sa.Integer()), nullable=False),
        sa.Column("member_count", sa.Integer(), nullable=False),
        sa.Column("candidate_count", sa.Integer(), nullable=False),
        sa.Column("logical_job_count", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="pk_l2f2_phase_d_binding"),
        # the binding names the EXACT authority and the EXACT plan, by id and hash together
        sa.ForeignKeyConstraint(
            ["authority_id"], [f"{_AUTHORITIES}.id"], name="fk_l2f2_phase_d_binding_authority"
        ),
        sa.ForeignKeyConstraint(
            ["plan_id", "plan_hash"],
            [f"{_PLANS}.id", f"{_PLANS}.plan_hash"],
            name="fk_l2f2_phase_d_binding_plan",
        ),
        # one binding per authority, and one per plan: a campaign has a single identity
        sa.UniqueConstraint("authority_id", name="uq_l2f2_phase_d_binding_authority"),
        sa.UniqueConstraint("plan_id", name="uq_l2f2_phase_d_binding_plan"),
        sa.CheckConstraint("phase = 'PHASE_D'", name="ck_l2f2_phase_d_binding_phase"),
        sa.CheckConstraint(
            f"member_count = {_PHASE_D_MEMBERS}", name="ck_l2f2_phase_d_binding_members"
        ),
        sa.CheckConstraint(
            f"candidate_count = {_PHASE_D_CANDIDATES}", name="ck_l2f2_phase_d_binding_candidates"
        ),
        sa.CheckConstraint(
            f"logical_job_count = {_PHASE_D_JOBS}", name="ck_l2f2_phase_d_binding_jobs"
        ),
        sa.CheckConstraint(
            f"array_length(ordered_config_hashes, 1) = {_PHASE_D_CANDIDATES}",
            name="ck_l2f2_phase_d_binding_config_count",
        ),
        sa.CheckConstraint(
            f"array_length(inherited_candidate_indices, 1) = {_PHASE_D_CANDIDATES}",
            name="ck_l2f2_phase_d_binding_index_count",
        ),
        # the seed must be one of the four. The frozen promotion rule never drops it.
        sa.CheckConstraint(
            "seed_config_hash = ANY(ordered_config_hashes)",
            name="ck_l2f2_phase_d_binding_seed_present",
        ),
        _hex_ck("baseline_protocol_hash", "ck_l2f2_phase_d_binding_protocol_hex"),
        _hex_ck("plan_hash", "ck_l2f2_phase_d_binding_plan_hex"),
        _hex_ck("finalist_freeze_sha256", "ck_l2f2_phase_d_binding_freeze_hex"),
        _hex_ck("phase_c_closure_sha256", "ck_l2f2_phase_d_binding_closure_hex"),
        _hex_ck("parameter_space_hash", "ck_l2f2_phase_d_binding_space_hex"),
        _hex_ck("execution_environment_hash", "ck_l2f2_phase_d_binding_env_hex"),
        _hex_ck("scoring_contract_hash", "ck_l2f2_phase_d_binding_contract_hex"),
        _hex_ck("split_manifest_sha256", "ck_l2f2_phase_d_binding_split_hex"),
        _hex_ck("seed_config_hash", "ck_l2f2_phase_d_binding_seed_hex"),
        _hex_ck("minos_subnet_sha", "ck_l2f2_phase_d_binding_subnet_hex", length=40),
        schema=_SCHEMA,
    )
    # append-only: a binding is the record of which campaign this was.
    op.execute(
        f"CREATE TRIGGER trg_l2f2_phase_d_binding_append_only "
        f"BEFORE UPDATE OR DELETE ON {_BINDING} "
        f"FOR EACH ROW EXECUTE FUNCTION {_REJECT_MUTATION}();"
    )


def _bootstrap_function() -> str:
    """0021's bootstrap, replaced in place, now proving WHICH four.

    Every check 0021 made is still made. What is added is the part that cannot be faked by writing
    an integer: the four persisted ``l2f_experiment_plan_configs`` rows are read in
    ``config_index`` order and compared element by element against the binding's ordered four, and
    the binding's own artifact digests, seed, contract, subnet commit and environment are required
    to match the authority's plan.
    """
    return (
        f"CREATE OR REPLACE FUNCTION {_BOOTSTRAP_FN}() "
        "RETURNS TABLE(plan_hash text, execution_environment_hash text) "
        "LANGUAGE plpgsql SECURITY DEFINER STABLE "
        "SET search_path = pg_catalog, public AS $bootstrap$ "
        "DECLARE v_d record; v_b record; v_plan record; v_members bigint; v_nonval bigint; "
        "        v_configs bigint; v_persisted text[]; v_indices integer[]; BEGIN "
        # ---- 1-2: exactly one PHASE_D authority, under the frozen protocol ------------------
        f"SELECT a.* INTO v_d FROM {_AUTHORITIES} a "
        f"  WHERE a.phase = 'PHASE_D' AND a.baseline_protocol_hash = '{_PROTOCOL_HASH}'; "
        "IF NOT FOUND THEN "
        "  RAISE EXCEPTION 'no PHASE_D execution authority under the frozen baseline protocol' "
        f"    USING ERRCODE = '{_SQLSTATE_AUTHORITY}'; END IF; "
        f"IF (SELECT count(*) FROM {_AUTHORITIES} a WHERE a.phase = 'PHASE_D') <> 1 THEN "
        "  RAISE EXCEPTION 'more than one PHASE_D execution authority exists; refusing to choose' "
        f"    USING ERRCODE = '{_SQLSTATE_AUTHORITY}'; END IF; "
        # ---- 3: the authority binds its OWN persisted plan ---------------------------------
        f"SELECT p.* INTO v_plan FROM {_PLANS} p "
        "  WHERE p.id = v_d.plan_id AND p.plan_hash = v_d.plan_hash; "
        "IF NOT FOUND THEN "
        "  RAISE EXCEPTION 'the PHASE_D authority does not bind its persisted plan' "
        f"    USING ERRCODE = '{_SQLSTATE_AUTHORITY}'; END IF; "
        # ---- 4: the plan is a VALIDATION plan ----------------------------------------------
        f"IF v_plan.partition <> '{_VALIDATION}' THEN "
        "  RAISE EXCEPTION 'the PHASE_D plan is partition %, not validation', v_plan.partition "
        f"    USING ERRCODE = '{_SQLSTATE_AUTHORITY}'; END IF; "
        # ---- 5-6: exactly ten members, none of them non-VALIDATION -------------------------
        f"SELECT count(*) INTO v_members FROM {_PLAN_MEMBERS} pm WHERE pm.plan_id = v_d.plan_id; "
        f"SELECT count(*) INTO v_nonval FROM {_PLAN_MEMBERS} pm "
        f"  WHERE pm.plan_id = v_d.plan_id AND pm.partition <> '{_VALIDATION}'; "
        "IF v_nonval <> 0 THEN "
        "  RAISE EXCEPTION 'the PHASE_D plan holds % non-VALIDATION member(s)', v_nonval "
        f"    USING ERRCODE = '{_SQLSTATE_AUTHORITY}'; END IF; "
        f"IF v_members <> {_PHASE_D_MEMBERS} THEN "
        "  RAISE EXCEPTION 'the PHASE_D plan holds % members, the frozen protocol fixes %', "
        f"    v_members, {_PHASE_D_MEMBERS} "
        f"    USING ERRCODE = '{_SQLSTATE_AUTHORITY}'; END IF; "
        # ---- the binding: the campaign's own identity --------------------------------------
        f"SELECT b.* INTO v_b FROM {_BINDING} b "
        "  WHERE b.authority_id = v_d.id AND b.plan_id = v_d.plan_id "
        "    AND b.plan_hash = v_d.plan_hash; "
        "IF NOT FOUND THEN "
        "  RAISE EXCEPTION 'the PHASE_D authority carries no Phase-D scientific binding; the "
        "four configurations it would execute are unattributed' "
        f"    USING ERRCODE = '{_SQLSTATE_AUTHORITY}'; END IF; "
        f"IF v_b.baseline_protocol_hash <> '{_PROTOCOL_HASH}' THEN "
        "  RAISE EXCEPTION 'the Phase-D binding does not cite the frozen baseline protocol' "
        f"    USING ERRCODE = '{_SQLSTATE_AUTHORITY}'; END IF; "
        # ---- 7-8: exactly four config rows, indexed 0..3 -----------------------------------
        f"SELECT count(*) INTO v_configs FROM {_PLAN_CONFIGS} pc WHERE pc.plan_id = v_d.plan_id; "
        f"IF v_configs <> {_PHASE_D_CANDIDATES} THEN "
        "  RAISE EXCEPTION 'the PHASE_D plan holds % configurations, the frozen protocol fixes %',"
        f"    v_configs, {_PHASE_D_CANDIDATES} "
        f"    USING ERRCODE = '{_SQLSTATE_AUTHORITY}'; END IF; "
        "SELECT array_agg(pc.config_index ORDER BY pc.config_index) INTO v_indices "
        f"  FROM {_PLAN_CONFIGS} pc WHERE pc.plan_id = v_d.plan_id; "
        "IF v_indices <> ARRAY[0,1,2,3] THEN "
        "  RAISE EXCEPTION 'the PHASE_D plan config_index inventory is %, expected {0,1,2,3}', "
        "    v_indices USING ERRCODE = '"
        f"{_SQLSTATE_AUTHORITY}'; END IF; "
        # ---- 9: THE check 0021 could not make — WHICH four, in WHICH order -----------------
        "SELECT array_agg(pc.config_hash ORDER BY pc.config_index) INTO v_persisted "
        f"  FROM {_PLAN_CONFIGS} pc WHERE pc.plan_id = v_d.plan_id; "
        "IF v_persisted IS DISTINCT FROM v_b.ordered_config_hashes THEN "
        "  RAISE EXCEPTION 'the PHASE_D plan does not persist the frozen four in frozen order' "
        f"    USING ERRCODE = '{_SQLSTATE_AUTHORITY}'; END IF; "
        # every persisted config must also carry the frozen parameter space
        f"IF EXISTS (SELECT 1 FROM {_PLAN_CONFIGS} pc WHERE pc.plan_id = v_d.plan_id "
        "           AND pc.parameter_space_hash <> v_b.parameter_space_hash) THEN "
        "  RAISE EXCEPTION 'a PHASE_D configuration binds a different parameter space' "
        f"    USING ERRCODE = '{_SQLSTATE_AUTHORITY}'; END IF; "
        # ---- 10: the seed is present, and is the binding's seed ----------------------------
        "IF NOT (v_b.seed_config_hash = ANY(v_b.ordered_config_hashes)) THEN "
        "  RAISE EXCEPTION 'the Phase-D binding does not include the seed' "
        f"    USING ERRCODE = '{_SQLSTATE_AUTHORITY}'; END IF; "
        # ---- 15: the environment the authority will execute under --------------------------
        "IF v_d.execution_environment_hash IS NULL "
        "   OR v_d.execution_environment_hash <> v_b.execution_environment_hash THEN "
        "  RAISE EXCEPTION 'the PHASE_D authority and its binding disagree about the execution "
        "environment' "
        f"    USING ERRCODE = '{_SQLSTATE_AUTHORITY}'; END IF; "
        # ---- 17-18: the frozen shape, agreed by authority, binding and plan -----------------
        f"IF v_d.member_count <> {_PHASE_D_MEMBERS} OR v_d.candidate_count <> "
        f"   {_PHASE_D_CANDIDATES} OR v_d.logical_job_count <> {_PHASE_D_JOBS} THEN "
        "  RAISE EXCEPTION 'the PHASE_D authority is not the frozen 4 x 10 = 40 validation' "
        f"    USING ERRCODE = '{_SQLSTATE_AUTHORITY}'; END IF; "
        "IF v_b.member_count <> v_d.member_count "
        "   OR v_b.candidate_count <> v_d.candidate_count "
        "   OR v_b.logical_job_count <> v_d.logical_job_count THEN "
        "  RAISE EXCEPTION 'the Phase-D binding and authority disagree about the campaign shape' "
        f"    USING ERRCODE = '{_SQLSTATE_AUTHORITY}'; END IF; "
        # ---- the two strings the runner may know -------------------------------------------
        "RETURN QUERY SELECT v_d.plan_hash::text, v_d.execution_environment_hash::text; "
        "END; $bootstrap$;"
    )


def _bootstrap_function_0022() -> str:
    """0021's original body, restored verbatim on downgrade (0022 did not change it)."""
    return (
        f"CREATE OR REPLACE FUNCTION {_BOOTSTRAP_FN}() "
        "RETURNS TABLE(plan_hash text, execution_environment_hash text) "
        "LANGUAGE plpgsql SECURITY DEFINER STABLE "
        "SET search_path = pg_catalog, public AS $bootstrap$ "
        "DECLARE v_d record; v_plan record; v_members bigint; v_nonval bigint; BEGIN "
        f"SELECT a.* INTO v_d FROM {_AUTHORITIES} a "
        f"  WHERE a.phase = 'PHASE_D' AND a.baseline_protocol_hash = '{_PROTOCOL_HASH}'; "
        "IF NOT FOUND THEN "
        "  RAISE EXCEPTION 'no PHASE_D execution authority under the frozen baseline protocol' "
        f"    USING ERRCODE = '{_SQLSTATE_AUTHORITY}'; END IF; "
        f"IF (SELECT count(*) FROM {_AUTHORITIES} a WHERE a.phase = 'PHASE_D') <> 1 THEN "
        "  RAISE EXCEPTION 'more than one PHASE_D execution authority exists; refusing to choose' "
        f"    USING ERRCODE = '{_SQLSTATE_AUTHORITY}'; END IF; "
        "SELECT p.* INTO v_plan FROM experiments.l2f_experiment_plans p "
        "  WHERE p.id = v_d.plan_id AND p.plan_hash = v_d.plan_hash; "
        "IF NOT FOUND THEN "
        "  RAISE EXCEPTION 'the PHASE_D authority does not bind its persisted plan' "
        f"    USING ERRCODE = '{_SQLSTATE_AUTHORITY}'; END IF; "
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
        f"IF v_d.member_count <> {_PHASE_D_MEMBERS} OR v_d.candidate_count <> "
        f"   {_PHASE_D_CANDIDATES} OR v_d.logical_job_count <> {_PHASE_D_JOBS} THEN "
        "  RAISE EXCEPTION 'the PHASE_D authority is not the frozen 4 x 10 = 40 validation' "
        f"    USING ERRCODE = '{_SQLSTATE_AUTHORITY}'; END IF; "
        "IF v_d.execution_environment_hash IS NULL THEN "
        "  RAISE EXCEPTION 'the PHASE_D authority carries no execution environment identity' "
        f"    USING ERRCODE = '{_SQLSTATE_AUTHORITY}'; END IF; "
        "RETURN QUERY SELECT v_d.plan_hash::text, v_d.execution_environment_hash::text; "
        "END; $bootstrap$;"
    )


def _grant_bootstrap() -> None:
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
    _create_binding()
    # the binding is control-plane state: the control plane writes it during preparation, and the
    # runner reads it only through the bootstrap — never directly.
    op.execute(f"REVOKE ALL ON {_BINDING} FROM PUBLIC;")
    for role in ("minos_live", "minos_trainer", "minos_evaluator", "minos_runner"):
        op.execute(f"REVOKE ALL ON {_BINDING} FROM {role};")
    # SELECT and INSERT only: the append-only trigger already refuses UPDATE and DELETE, and not
    # granting them means the control plane cannot even attempt an edit.
    op.execute(f"GRANT SELECT, INSERT ON {_BINDING} TO {_CONTROL_PLANE};")

    op.execute(f"SET ROLE {_CONTROL_PLANE}")
    op.execute(_bootstrap_function())
    op.execute("RESET ROLE")
    _grant_bootstrap()


def downgrade() -> None:
    """Restore 0022's bootstrap — but REFUSE while a Phase-D binding exists."""
    conn = op.get_bind()
    bindings = conn.execute(
        sa.text(f"SELECT count(*) FROM {_BINDING}")  # noqa: S608
    ).scalar_one()
    if bindings:
        raise RuntimeError(
            f"cannot downgrade away the Phase-D binding: {bindings} row(s) exist. That table is "
            "append-only scientific lineage — it is the record of WHICH four configurations a "
            "validation campaign ran — so there is no honest way back; dropping it would leave "
            "the campaign unattributed."
        )
    op.execute(f"SET ROLE {_CONTROL_PLANE}")
    op.execute(_bootstrap_function_0022())
    op.execute("RESET ROLE")
    _grant_bootstrap()
    op.execute(f"DROP TRIGGER IF EXISTS trg_l2f2_phase_d_binding_append_only ON {_BINDING};")
    op.drop_table(_BINDING_TABLE, schema=_SCHEMA)
