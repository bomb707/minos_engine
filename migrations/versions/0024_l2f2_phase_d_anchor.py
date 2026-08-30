"""Anchor the Phase-D boundary to THIS campaign's constants, not to a self-consistent story.

``0023`` closed a real hole: it made the bootstrap read the actual persisted
``l2f_experiment_plan_configs`` rows and compare them, in ``config_index`` order, against the
binding's ``ordered_config_hashes``. A plan whose configurations disagreed with its own binding
stopped being executable.

It left a narrower one. Every identity except the baseline protocol is read *out of the binding
row* and compared only to the plan or to itself. So a binding that names four different
configurations, together with a four-row plan graph that matches it, is internally consistent —
and internally consistent is exactly what the check tests. The forgery passes not because a
constraint was missed but because nothing in the database knew which campaign this was supposed to
be.

This migration supplies that knowledge. The frozen constants are written into the bootstrap itself,
where no row can contradict them:

* the finalist-freeze digest and the Phase-C closure digest — the two artifacts that make the four
  auditable outside the database;
* the exact ordered four, and their inherited Phase-B indices ``{42, 25, 36, 0}``;
* the seed;
* the parameter space, execution environment, scoring contract and MINOS_SUBNET commit.

``0023``'s persisted-row comparison is kept, not replaced. The two checks answer different
questions — "do the plan's configurations match its binding" and "is that binding this campaign" —
and a forgery has to survive both.

The split manifest is bound differently, and deliberately. A database cannot hash a file on a
filesystem it does not have, so the bootstrap requires the binding to carry a digest and the
PRODUCTION PREPARATION path verifies that digest against the manifest it actually read before the
binding is ever written. The constant lives where the bytes are.

Two defects inherited from ``0021`` are repaired here, because the anchor cannot be proven without
them — a bootstrap that cannot return cannot be tested for what it returns:

* **The execution environment was read from a column that does not exist.** ``0021`` wrote
  ``v_d.execution_environment_hash`` against ``experiments.l2f2_execution_authorities``; ``0015``
  added that identity to the two OUTCOME ledgers and to nothing else, and no migration ever added
  it to the authority table. In PL/pgSQL an absent field on a ``record`` raises at run time, not at
  ``CREATE FUNCTION`` time, so the error was latent — and every test written against ``0021`` and
  ``0023`` exercised a refusal, each of which raises earlier. The PHASE_D bootstrap has therefore
  never once returned. The environment is now read from the BINDING, which carries it ``NOT NULL``
  and which this migration pins to the frozen literal. That needs no new column and is stricter
  than the authority column would have been: the binding's value is anchored, not merely recorded.
* **A validation member's snapshot was unconstrained.** ``fk_l2f_pm_plan_lineage`` is MATCH SIMPLE
  over ``(plan_id, profile_snapshot_id, feature_matrix_id)``, and ``0022`` requires a validation
  member's ``feature_matrix_id`` to be NULL, so the FK is satisfied vacuously for exactly the rows
  Phase D uses. The plan-to-member snapshot agreement it enforces for TRAIN is asserted here.

The bootstrap is unchanged in every other respect: argument-free, truth-free, SECURITY DEFINER
owned by the non-superuser control plane, executable by the runner and nobody else.

``downgrade`` restores ``0023``'s bootstrap verbatim — including both defects above, which are
``0023``'s actual behaviour and are not this migration's to rewrite. It is safe unconditionally:
this migration owns no table and no row, only a function body, so unlike ``0022`` and ``0023`` it
has no scientific state to orphan and does not refuse.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0024_l2f2_phase_d_anchor"
down_revision: str | None = "0023_l2f2_phase_d_binding"
branch_labels = None
depends_on = None

_AUTHORITIES = "experiments.l2f2_execution_authorities"
_BINDING = "experiments.l2f2_phase_d_binding"
_PLANS = "experiments.l2f_experiment_plans"
_PLAN_CONFIGS = "experiments.l2f_experiment_plan_configs"
_PLAN_MEMBERS = "experiments.l2f_experiment_plan_members"

_BOOTSTRAP_FN = "experiments.l2f2_resolve_phase_d_runner_bootstrap"
_BOOTSTRAP_SIG = f"{_BOOTSTRAP_FN}()"

_CONTROL_PLANE = "minos_admin"
_DENIED_ROLES = ("minos_live", "minos_trainer", "minos_evaluator")

_VALIDATION = "validation"
_PHASE_D_MEMBERS = 10
_PHASE_D_CANDIDATES = 4
_PHASE_D_JOBS = _PHASE_D_MEMBERS * _PHASE_D_CANDIDATES

_SQLSTATE_AUTHORITY = "MN030"

# --------------------------------------------------------------------------------------------
# THIS campaign. Every one of these is frozen upstream of the database and is written here so a
# binding row cannot assert otherwise.
# --------------------------------------------------------------------------------------------
_PROTOCOL_HASH = "c548e190571f5e964560cf30021a520ea8aad6674569fa3202af880d7dff77d1"
_FREEZE_SHA = "540aeca0640871ca91e3ec771ec66d2df4b96d38210ec3265f944dee3e0433f3"
_CLOSURE_SHA = "5de368eec327b66c868737d1819cc1b1a590eaf185b28e53d1cfecae59b593ca"
_PARAMETER_SPACE = "b2d401918084d64023305d9262baf5011a89fe517bee4e0bd33af79fb14aee2e"
_EXECUTION_ENVIRONMENT = "71e14a49833ac77bb9dc576345fb89c4dd68f4a3ad3673eb098d38593c1ef4d3"
_SCORING_CONTRACT = "b24a07e208ce8e2fff6672102ae4e61aed93c6f352a5af46ba81c4789adb76d6"
_MINOS_SUBNET = "649bb92c6abccebde58a736a2b2af7fd77a701c1"

_FINALISTS = (
    "157d88d1587c13be395c62d60e27d1becdada78fad45e65d883bc1190e51acea",
    "0972930f8d8c562be15382203e123b2909094e7eac46e84321d36c67abf8345e",
    "22a1f1fd9ddf02a97776d991f11280b3982673693a4f357479098a99fb411a16",
    "4251cb85e5cd58b7eabfe530b9df23ea7d1d14fd882114b488d67cbd81b751b8",
)
_INHERITED_INDICES = (42, 25, 36, 0)
_SEED = _FINALISTS[3]


def _sql_text_array(values: tuple[str, ...]) -> str:
    joined = ", ".join(f"'{v}'" for v in values)
    return f"ARRAY[{joined}]::text[]"


def _sql_int_array(values: tuple[int, ...]) -> str:
    joined = ", ".join(str(v) for v in values)
    return f"ARRAY[{joined}]::integer[]"


def _anchored_bootstrap() -> str:
    """0023's body, plus the campaign anchor. Both checks are kept; neither replaces the other."""
    return (
        f"CREATE OR REPLACE FUNCTION {_BOOTSTRAP_FN}() "
        "RETURNS TABLE(plan_hash text, execution_environment_hash text) "
        "LANGUAGE plpgsql SECURITY DEFINER STABLE "
        "SET search_path = pg_catalog, public AS $bootstrap$ "
        "DECLARE v_d record; v_b record; v_plan record; v_members bigint; v_nonval bigint; "
        "        v_configs bigint; v_persisted text[]; v_indices integer[]; BEGIN "
        # ---- exactly one PHASE_D authority, under the frozen protocol ----------------------
        f"SELECT a.* INTO v_d FROM {_AUTHORITIES} a "
        f"  WHERE a.phase = 'PHASE_D' AND a.baseline_protocol_hash = '{_PROTOCOL_HASH}'; "
        "IF NOT FOUND THEN "
        "  RAISE EXCEPTION 'no PHASE_D execution authority under the frozen baseline protocol' "
        f"    USING ERRCODE = '{_SQLSTATE_AUTHORITY}'; END IF; "
        f"IF (SELECT count(*) FROM {_AUTHORITIES} a WHERE a.phase = 'PHASE_D') <> 1 THEN "
        "  RAISE EXCEPTION 'more than one PHASE_D execution authority exists; refusing to choose' "
        f"    USING ERRCODE = '{_SQLSTATE_AUTHORITY}'; END IF; "
        # ---- the authority binds its OWN persisted plan, and it is a VALIDATION plan -------
        f"SELECT p.* INTO v_plan FROM {_PLANS} p "
        "  WHERE p.id = v_d.plan_id AND p.plan_hash = v_d.plan_hash; "
        "IF NOT FOUND THEN "
        "  RAISE EXCEPTION 'the PHASE_D authority does not bind its persisted plan' "
        f"    USING ERRCODE = '{_SQLSTATE_AUTHORITY}'; END IF; "
        f"IF v_plan.partition <> '{_VALIDATION}' THEN "
        "  RAISE EXCEPTION 'the PHASE_D plan is partition %, not validation', v_plan.partition "
        f"    USING ERRCODE = '{_SQLSTATE_AUTHORITY}'; END IF; "
        # ---- exactly ten members, none of them non-VALIDATION ------------------------------
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
        # ---- the campaign binding must exist ------------------------------------------------
        f"SELECT b.* INTO v_b FROM {_BINDING} b "
        "  WHERE b.authority_id = v_d.id AND b.plan_id = v_d.plan_id "
        "    AND b.plan_hash = v_d.plan_hash; "
        "IF NOT FOUND THEN "
        "  RAISE EXCEPTION 'the PHASE_D authority carries no Phase-D scientific binding; the "
        "four configurations it would execute are unattributed' "
        f"    USING ERRCODE = '{_SQLSTATE_AUTHORITY}'; END IF; "
        # ---- THE ANCHOR: the binding must describe THIS campaign, not merely itself ---------
        f"IF v_b.baseline_protocol_hash <> '{_PROTOCOL_HASH}' THEN "
        "  RAISE EXCEPTION 'the Phase-D binding does not cite the frozen baseline protocol' "
        f"    USING ERRCODE = '{_SQLSTATE_AUTHORITY}'; END IF; "
        f"IF v_b.finalist_freeze_sha256 <> '{_FREEZE_SHA}' THEN "
        "  RAISE EXCEPTION 'the Phase-D binding cites finalist freeze %, not this campaign''s', "
        "    v_b.finalist_freeze_sha256 "
        f"    USING ERRCODE = '{_SQLSTATE_AUTHORITY}'; END IF; "
        f"IF v_b.phase_c_closure_sha256 <> '{_CLOSURE_SHA}' THEN "
        "  RAISE EXCEPTION 'the Phase-D binding cites Phase-C closure %, not this campaign''s', "
        "    v_b.phase_c_closure_sha256 "
        f"    USING ERRCODE = '{_SQLSTATE_AUTHORITY}'; END IF; "
        f"IF v_b.parameter_space_hash <> '{_PARAMETER_SPACE}' THEN "
        "  RAISE EXCEPTION 'the Phase-D binding cites a different parameter space' "
        f"    USING ERRCODE = '{_SQLSTATE_AUTHORITY}'; END IF; "
        f"IF v_b.execution_environment_hash <> '{_EXECUTION_ENVIRONMENT}' THEN "
        "  RAISE EXCEPTION 'the Phase-D binding cites a different execution environment' "
        f"    USING ERRCODE = '{_SQLSTATE_AUTHORITY}'; END IF; "
        f"IF v_b.scoring_contract_hash <> '{_SCORING_CONTRACT}' THEN "
        "  RAISE EXCEPTION 'the Phase-D binding cites a different scoring contract' "
        f"    USING ERRCODE = '{_SQLSTATE_AUTHORITY}'; END IF; "
        f"IF v_b.minos_subnet_sha <> '{_MINOS_SUBNET}' THEN "
        "  RAISE EXCEPTION 'the Phase-D binding cites a different MINOS_SUBNET commit' "
        f"    USING ERRCODE = '{_SQLSTATE_AUTHORITY}'; END IF; "
        f"IF v_b.ordered_config_hashes IS DISTINCT FROM {_sql_text_array(_FINALISTS)} THEN "
        "  RAISE EXCEPTION 'the Phase-D binding does not name the frozen four in frozen order' "
        f"    USING ERRCODE = '{_SQLSTATE_AUTHORITY}'; END IF; "
        f"IF v_b.inherited_candidate_indices IS DISTINCT FROM "
        f"   {_sql_int_array(_INHERITED_INDICES)} THEN "
        "  RAISE EXCEPTION 'the Phase-D binding does not carry the frozen inherited indices' "
        f"    USING ERRCODE = '{_SQLSTATE_AUTHORITY}'; END IF; "
        f"IF v_b.seed_config_hash <> '{_SEED}' THEN "
        "  RAISE EXCEPTION 'the Phase-D binding names a different seed' "
        f"    USING ERRCODE = '{_SQLSTATE_AUTHORITY}'; END IF; "
        "IF v_b.split_manifest_sha256 IS NULL THEN "
        "  RAISE EXCEPTION 'the Phase-D binding carries no split-manifest identity' "
        f"    USING ERRCODE = '{_SQLSTATE_AUTHORITY}'; END IF; "
        # ---- 0023's check, kept: do the PLAN's configurations match that binding? -----------
        f"SELECT count(*) INTO v_configs FROM {_PLAN_CONFIGS} pc WHERE pc.plan_id = v_d.plan_id; "
        f"IF v_configs <> {_PHASE_D_CANDIDATES} THEN "
        "  RAISE EXCEPTION 'the PHASE_D plan holds % configurations, the frozen protocol fixes %',"
        f"    v_configs, {_PHASE_D_CANDIDATES} "
        f"    USING ERRCODE = '{_SQLSTATE_AUTHORITY}'; END IF; "
        "SELECT array_agg(pc.config_index ORDER BY pc.config_index) INTO v_indices "
        f"  FROM {_PLAN_CONFIGS} pc WHERE pc.plan_id = v_d.plan_id; "
        "IF v_indices <> ARRAY[0,1,2,3] THEN "
        "  RAISE EXCEPTION 'the PHASE_D plan config_index inventory is %, expected {0,1,2,3}', "
        f"    v_indices USING ERRCODE = '{_SQLSTATE_AUTHORITY}'; END IF; "
        "SELECT array_agg(pc.config_hash ORDER BY pc.config_index) INTO v_persisted "
        f"  FROM {_PLAN_CONFIGS} pc WHERE pc.plan_id = v_d.plan_id; "
        "IF v_persisted IS DISTINCT FROM v_b.ordered_config_hashes THEN "
        "  RAISE EXCEPTION 'the PHASE_D plan does not persist the frozen four in frozen order' "
        f"    USING ERRCODE = '{_SQLSTATE_AUTHORITY}'; END IF; "
        f"IF EXISTS (SELECT 1 FROM {_PLAN_CONFIGS} pc WHERE pc.plan_id = v_d.plan_id "
        f"           AND pc.parameter_space_hash <> '{_PARAMETER_SPACE}') THEN "
        "  RAISE EXCEPTION 'a PHASE_D configuration binds a different parameter space' "
        f"    USING ERRCODE = '{_SQLSTATE_AUTHORITY}'; END IF; "
        # ---- the frozen shape, agreed by authority, binding and plan ------------------------
        f"IF v_d.member_count <> {_PHASE_D_MEMBERS} OR v_d.candidate_count <> "
        f"   {_PHASE_D_CANDIDATES} OR v_d.logical_job_count <> {_PHASE_D_JOBS} THEN "
        "  RAISE EXCEPTION 'the PHASE_D authority is not the frozen 4 x 10 = 40 validation' "
        f"    USING ERRCODE = '{_SQLSTATE_AUTHORITY}'; END IF; "
        "IF v_b.member_count <> v_d.member_count "
        "   OR v_b.candidate_count <> v_d.candidate_count "
        "   OR v_b.logical_job_count <> v_d.logical_job_count THEN "
        "  RAISE EXCEPTION 'the Phase-D binding and authority disagree about the campaign shape' "
        f"    USING ERRCODE = '{_SQLSTATE_AUTHORITY}'; END IF; "
        # ---- every member belongs to the PLAN'S snapshot -----------------------------------
        # fk_l2f_pm_plan_lineage is MATCH SIMPLE over (plan_id, profile_snapshot_id,
        # feature_matrix_id), so a validation member — whose feature_matrix_id is NULL by 0022's
        # conditional check — satisfies it vacuously. The plan-to-member snapshot agreement that
        # the FK enforces for TRAIN is therefore unenforced for validation, and is asserted here.
        f"IF EXISTS (SELECT 1 FROM {_PLAN_MEMBERS} pm WHERE pm.plan_id = v_d.plan_id "
        "           AND pm.profile_snapshot_id IS DISTINCT FROM v_plan.profile_snapshot_id) THEN "
        "  RAISE EXCEPTION 'a PHASE_D plan member belongs to a different profile snapshot than "
        "its plan' "
        f"    USING ERRCODE = '{_SQLSTATE_AUTHORITY}'; END IF; "
        # ---- the two strings the runner may know --------------------------------------------
        # The execution environment is read from the BINDING, which carries it NOT NULL (0023) and
        # is anchored to the frozen literal above. experiments.l2f2_execution_authorities has no
        # execution_environment_hash column — 0015 added that identity to the two OUTCOME ledgers
        # only — so 0021's and 0023's `v_d.execution_environment_hash` names a field that does not
        # exist and raises at runtime on every path that reaches it. No test had reached one: the
        # PHASE_D bootstrap has never returned since it was introduced. Sourcing it from the
        # binding needs no new column, and is stricter than the authority column would have been,
        # because the binding's value is pinned to a literal rather than accepted from a writer.
        "RETURN QUERY SELECT v_d.plan_hash::text, v_b.execution_environment_hash::text; "
        "END; $bootstrap$;"
    )


def _bootstrap_function_0023() -> str:
    """0023's body, restored verbatim on downgrade."""
    return (
        f"CREATE OR REPLACE FUNCTION {_BOOTSTRAP_FN}() "
        "RETURNS TABLE(plan_hash text, execution_environment_hash text) "
        "LANGUAGE plpgsql SECURITY DEFINER STABLE "
        "SET search_path = pg_catalog, public AS $bootstrap$ "
        "DECLARE v_d record; v_b record; v_plan record; v_members bigint; v_nonval bigint; "
        "        v_configs bigint; v_persisted text[]; v_indices integer[]; BEGIN "
        f"SELECT a.* INTO v_d FROM {_AUTHORITIES} a "
        f"  WHERE a.phase = 'PHASE_D' AND a.baseline_protocol_hash = '{_PROTOCOL_HASH}'; "
        "IF NOT FOUND THEN "
        "  RAISE EXCEPTION 'no PHASE_D execution authority under the frozen baseline protocol' "
        f"    USING ERRCODE = '{_SQLSTATE_AUTHORITY}'; END IF; "
        f"IF (SELECT count(*) FROM {_AUTHORITIES} a WHERE a.phase = 'PHASE_D') <> 1 THEN "
        "  RAISE EXCEPTION 'more than one PHASE_D execution authority exists; refusing to choose' "
        f"    USING ERRCODE = '{_SQLSTATE_AUTHORITY}'; END IF; "
        f"SELECT p.* INTO v_plan FROM {_PLANS} p "
        "  WHERE p.id = v_d.plan_id AND p.plan_hash = v_d.plan_hash; "
        "IF NOT FOUND THEN "
        "  RAISE EXCEPTION 'the PHASE_D authority does not bind its persisted plan' "
        f"    USING ERRCODE = '{_SQLSTATE_AUTHORITY}'; END IF; "
        f"IF v_plan.partition <> '{_VALIDATION}' THEN "
        "  RAISE EXCEPTION 'the PHASE_D plan is partition %, not validation', v_plan.partition "
        f"    USING ERRCODE = '{_SQLSTATE_AUTHORITY}'; END IF; "
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
        f"SELECT count(*) INTO v_configs FROM {_PLAN_CONFIGS} pc WHERE pc.plan_id = v_d.plan_id; "
        f"IF v_configs <> {_PHASE_D_CANDIDATES} THEN "
        "  RAISE EXCEPTION 'the PHASE_D plan holds % configurations, the frozen protocol fixes %',"
        f"    v_configs, {_PHASE_D_CANDIDATES} "
        f"    USING ERRCODE = '{_SQLSTATE_AUTHORITY}'; END IF; "
        "SELECT array_agg(pc.config_index ORDER BY pc.config_index) INTO v_indices "
        f"  FROM {_PLAN_CONFIGS} pc WHERE pc.plan_id = v_d.plan_id; "
        "IF v_indices <> ARRAY[0,1,2,3] THEN "
        "  RAISE EXCEPTION 'the PHASE_D plan config_index inventory is %, expected {0,1,2,3}', "
        f"    v_indices USING ERRCODE = '{_SQLSTATE_AUTHORITY}'; END IF; "
        "SELECT array_agg(pc.config_hash ORDER BY pc.config_index) INTO v_persisted "
        f"  FROM {_PLAN_CONFIGS} pc WHERE pc.plan_id = v_d.plan_id; "
        "IF v_persisted IS DISTINCT FROM v_b.ordered_config_hashes THEN "
        "  RAISE EXCEPTION 'the PHASE_D plan does not persist the frozen four in frozen order' "
        f"    USING ERRCODE = '{_SQLSTATE_AUTHORITY}'; END IF; "
        f"IF EXISTS (SELECT 1 FROM {_PLAN_CONFIGS} pc WHERE pc.plan_id = v_d.plan_id "
        "           AND pc.parameter_space_hash <> v_b.parameter_space_hash) THEN "
        "  RAISE EXCEPTION 'a PHASE_D configuration binds a different parameter space' "
        f"    USING ERRCODE = '{_SQLSTATE_AUTHORITY}'; END IF; "
        "IF NOT (v_b.seed_config_hash = ANY(v_b.ordered_config_hashes)) THEN "
        "  RAISE EXCEPTION 'the Phase-D binding does not include the seed' "
        f"    USING ERRCODE = '{_SQLSTATE_AUTHORITY}'; END IF; "
        "IF v_d.execution_environment_hash IS NULL "
        "   OR v_d.execution_environment_hash <> v_b.execution_environment_hash THEN "
        "  RAISE EXCEPTION 'the PHASE_D authority and its binding disagree about the execution "
        "environment' "
        f"    USING ERRCODE = '{_SQLSTATE_AUTHORITY}'; END IF; "
        f"IF v_d.member_count <> {_PHASE_D_MEMBERS} OR v_d.candidate_count <> "
        f"   {_PHASE_D_CANDIDATES} OR v_d.logical_job_count <> {_PHASE_D_JOBS} THEN "
        "  RAISE EXCEPTION 'the PHASE_D authority is not the frozen 4 x 10 = 40 validation' "
        f"    USING ERRCODE = '{_SQLSTATE_AUTHORITY}'; END IF; "
        "IF v_b.member_count <> v_d.member_count "
        "   OR v_b.candidate_count <> v_d.candidate_count "
        "   OR v_b.logical_job_count <> v_d.logical_job_count THEN "
        "  RAISE EXCEPTION 'the Phase-D binding and authority disagree about the campaign shape' "
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
    op.execute(f"SET ROLE {_CONTROL_PLANE}")
    op.execute(_anchored_bootstrap())
    op.execute("RESET ROLE")
    _grant_bootstrap()


def downgrade() -> None:
    """Restore 0023's bootstrap. Unconditionally safe: this migration owns no scientific state.

    0022 and 0023 refuse to downgrade over rows they own. This one owns none — it replaced a
    function body and created nothing — so a refusal would protect nothing and would only make a
    legitimate rollback impossible.
    """
    _require_control_plane(op.get_bind())
    op.execute(f"SET ROLE {_CONTROL_PLANE}")
    op.execute(_bootstrap_function_0023())
    op.execute("RESET ROLE")
    _grant_bootstrap()
