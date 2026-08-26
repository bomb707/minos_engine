"""Admit PHASE_B to the least-privilege runner boundary — one more phase, nothing wider.

``0011`` built the runner boundary for exactly one phase and said so: *"the ONLY phase 0011
admits. A later phase is a later migration, never a looser CHECK."* That was a stage boundary, not
an oversight, and Phase B ran straight into it — a Phase-B plan can be derived, persisted and
materialized, and then no job of it can be claimed, because ``ck_l2f2_authority_phase`` refuses to
record a Phase-B authority and ``l2f2_resolve_claimed_execution`` looks one up with a hardcoded
``phase = 'PHASE_A'``.

This migration is that later migration, and it stays as narrow as the boundary it widens:

* **Two phases, not "any phase".** The CHECK becomes ``PHASE_A`` or ``PHASE_B`` and nothing else.
  Phase C remains a later migration for exactly the reason 0011 gave.
* **The Phase-A resolver is untouched.** Its signature, its body and its ``phase = 'PHASE_A'``
  predicate are left exactly as they are, so the privileged Phase-A interface stays Phase-A-only
  and remains a regression others can rely on. Phase B gets its own function with its own fixed
  ``phase = 'PHASE_B'`` predicate; no caller passes a phase in, and no resolver falls back to the
  other phase.
* **The canary is a Phase-A concept.** ``canary_job_key`` was ``NOT NULL`` because 0011 had only
  Phase A. Phase B has no canary and one must not be invented for it, so the column becomes
  nullable and a phase-semantic CHECK makes the rule explicit in both directions: Phase A must
  carry a canary, Phase B must not. (The existing hex CHECK is kept; a CHECK is satisfied by NULL,
  so it constrains the value and this new constraint constrains its presence.)

Nothing about claiming changes. ``experiments.minos_l2f_claim_next_job(plan_hash, worker_id)``
from 0007 is already plan-scoped, and each phase's authority supplies its own plan hash — so the
queue never needed to know that phases exist.

The upgrade is additive over a POPULATED store: unlike ``0015``, it must not refuse because
execution evidence already exists, since the completed Phase-A campaign is exactly such a store
and nothing here reinterprets a single row of it. The downgrade REFUSES while any Phase-B
authority exists, because squeezing back into a Phase-A-only CHECK would mean deleting or
falsifying an append-only scientific record.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0016_l2f2_phase_b_execution"
down_revision: str | None = "0015_l2f2_exec_environment"
branch_labels = None
depends_on = None

_SCHEMA = "experiments"
_AUTHORITIES_TABLE = "l2f2_execution_authorities"
_AUTHORITIES = f"{_SCHEMA}.{_AUTHORITIES_TABLE}"

#: 0011's Phase-A resolver. Named here only to be left alone — this migration never redefines it.
_RESOLVE_A_FN = "experiments.l2f2_resolve_claimed_execution"
_RESOLVE_B_FN = "experiments.l2f2_resolve_claimed_phase_b_execution"
_RESOLVE_B_SIG = f"{_RESOLVE_B_FN}(text, uuid, text)"

_PHASE_CK = "ck_l2f2_authority_phase"
_CANARY_PHASE_CK = "ck_l2f2_authority_canary_phase"
_CANARY_COLUMN = "canary_job_key"

#: what 0011 admitted, and what 0016 admits. A third phase is a third migration.
_PHASES_0015 = ("PHASE_A",)
_PHASES_0016 = ("PHASE_A", "PHASE_B")

#: the frozen L2-F2-B protocol this boundary executes under — identical to 0011's.
_PROTOCOL_HASH = "c548e190571f5e964560cf30021a520ea8aad6674569fa3202af880d7dff77d1"

_DENIED_ROLES = ("minos_live", "minos_trainer", "minos_evaluator")

#: the STABLE MINOS SQLSTATEs 0011 uses, reused verbatim so the Python boundary maps identically.
_SQLSTATE_INVALID_WORKER = "MN001"
_SQLSTATE_NOT_OWNED = "MN003"
_SQLSTATE_AUTHORITY = "MN030"

#: 0011's resolution result type, verbatim. Phase B resolves the SAME truth-free identity — a
#: different phase is not a different kind of execution, and an additive column here would be a
#: second contract for the same thing.
_RESOLVE_COLS = (
    "job_id uuid, job_key text, plan_id uuid, plan_member_id uuid, plan_config_id uuid, "
    "member_index integer, partition text, dataset_id text, round_id text, chromosome text, "
    "region_hash text, region_start0 bigint, region_end0_exclusive bigint, "
    "bam_sha256 text, bai_sha256 text, reference_sha256 text, fai_sha256 text, "
    "bam_size_bytes bigint, profile_id text, content_hash text, feature_values_hash text, "
    "config_index integer, config_hash text, parameter_space_hash text, "
    "config_media_type text, config_uri text, config_sha256 text, config_size_bytes integer"
)


def _phase_check(phases: tuple[str, ...]) -> str:
    return "phase IN (" + ", ".join(f"'{p}'" for p in phases) + ")"


def _canary_phase_check() -> str:
    """Phase A must carry its canary; Phase B must not carry one at all."""
    return (
        f"(phase = 'PHASE_A' AND {_CANARY_COLUMN} IS NOT NULL) OR "
        f"(phase = 'PHASE_B' AND {_CANARY_COLUMN} IS NULL)"
    )


def _phase_b_resolver() -> str:
    """0011's resolver body with ONE difference: the phase it will accept an authority for.

    Everything else is deliberately identical — the authority must cite the frozen protocol, the
    job must belong to that authority's plan, be CLAIMED or RUNNING, be owned by this worker, and
    be a TRAIN member. No truth digest, no mutation digest, no evaluation row and no non-TRAIN
    member is reachable through this interface, and the phase is fixed here rather than passed in.
    """
    return (
        f"CREATE OR REPLACE FUNCTION {_RESOLVE_B_FN}"
        "(p_plan_hash text, p_job_id uuid, p_worker_id text) "
        f"RETURNS TABLE({_RESOLVE_COLS}) LANGUAGE plpgsql SECURITY DEFINER "
        "SET search_path = pg_catalog, public AS $resolve$ "
        "DECLARE v_auth record; BEGIN "
        "IF p_worker_id IS NULL OR btrim(p_worker_id) = '' THEN "
        "  RAISE EXCEPTION 'worker_id must be a non-empty identifier' "
        f"    USING ERRCODE = '{_SQLSTATE_INVALID_WORKER}'; END IF; "
        # the plan must carry a FROZEN-protocol PHASE_B execution authority. There is no fallback
        # to PHASE_A: a Phase-A plan resolved here is simply not found.
        f"SELECT a.* INTO v_auth FROM {_AUTHORITIES} a "
        "  WHERE a.plan_hash = p_plan_hash AND a.phase = 'PHASE_B'; "
        "IF NOT FOUND THEN "
        "  RAISE EXCEPTION 'plan % has no PHASE_B L2-F2 execution authority', p_plan_hash "
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


def _grant_phase_b_resolver() -> None:
    """The same grant shape 0011 applies: the runner and the control plane, nobody else."""
    op.execute(f"REVOKE ALL ON FUNCTION {_RESOLVE_B_SIG} FROM PUBLIC;")
    for role in _DENIED_ROLES:
        op.execute(f"REVOKE ALL ON FUNCTION {_RESOLVE_B_SIG} FROM {role};")
    op.execute(f"GRANT EXECUTE ON FUNCTION {_RESOLVE_B_SIG} TO minos_runner;")
    op.execute(f"GRANT EXECUTE ON FUNCTION {_RESOLVE_B_SIG} TO minos_admin;")


def upgrade() -> None:
    # the phase vocabulary, widened by exactly one value.
    op.drop_constraint(_PHASE_CK, _AUTHORITIES_TABLE, schema=_SCHEMA, type_="check")
    op.create_check_constraint(
        _PHASE_CK, _AUTHORITIES_TABLE, _phase_check(_PHASES_0016), schema=_SCHEMA
    )

    # the canary stops being a column-level requirement and becomes a phase-level one. Existing
    # Phase-A rows already satisfy it: they were NOT NULL a moment ago.
    op.alter_column(
        _AUTHORITIES_TABLE, _CANARY_COLUMN, nullable=True, existing_type=sa.CHAR(64), schema=_SCHEMA
    )
    op.create_check_constraint(
        _CANARY_PHASE_CK, _AUTHORITIES_TABLE, _canary_phase_check(), schema=_SCHEMA
    )

    # a SECURITY DEFINER function executes with its OWNER's authority. 0008's writers are owned by
    # minos_admin for that reason and this one is too, so the Phase-B boundary is exactly as wide
    # as the control plane and no wider.
    op.execute("SET ROLE minos_admin")
    op.execute(_phase_b_resolver())
    op.execute("RESET ROLE")
    _grant_phase_b_resolver()


def downgrade() -> None:
    """Restore 0015 exactly — but REFUSE while a Phase-B authority exists.

    ``l2f2_execution_authorities`` is append-only scientific lineage. Restoring a Phase-A-only
    CHECK over a store that holds a Phase-B authority would require deleting that row or
    relabelling its phase, and a migration must never do either to make a schema fit.
    """
    conn = op.get_bind()
    # checked BEFORE anything is altered, so a refusal leaves the database exactly as it was.
    phase_b = conn.execute(
        sa.text(f"SELECT count(*) FROM {_AUTHORITIES} WHERE phase = 'PHASE_B'")  # noqa: S608
    ).scalar_one()
    if phase_b:
        raise RuntimeError(
            f"cannot downgrade {_AUTHORITIES} to a PHASE_A-only boundary: {phase_b} PHASE_B "
            "execution authority row(s) exist. That table is append-only scientific lineage, so "
            "there is no honest way back — dropping the row, changing its phase or inventing a "
            "canary for it would all falsify the record of a Phase-B campaign. Retire the Phase-B "
            "authority deliberately on a store where that is meaningful, never as a side effect "
            "of a schema move."
        )

    op.execute(f"DROP FUNCTION IF EXISTS {_RESOLVE_B_SIG};")
    op.drop_constraint(_CANARY_PHASE_CK, _AUTHORITIES_TABLE, schema=_SCHEMA, type_="check")
    op.alter_column(
        _AUTHORITIES_TABLE,
        _CANARY_COLUMN,
        nullable=False,
        existing_type=sa.CHAR(64),
        schema=_SCHEMA,
    )
    op.drop_constraint(_PHASE_CK, _AUTHORITIES_TABLE, schema=_SCHEMA, type_="check")
    op.create_check_constraint(
        _PHASE_CK, _AUTHORITIES_TABLE, _phase_check(_PHASES_0015), schema=_SCHEMA
    )
