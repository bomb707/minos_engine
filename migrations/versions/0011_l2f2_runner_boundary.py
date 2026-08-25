"""L2-F2 least-privilege runner boundary — execution authority, resolution, artifact registrar.

``0008`` deliberately gives ``minos_runner`` **no direct table privilege** on any L2-F table: it
writes only through ``SECURITY DEFINER`` functions. But the historical Python execution path
reads the plan/member/config graph with direct ``SELECT`` and persists artifacts under
``SET LOCAL ROLE minos_admin``. An external ``minos_runner_svc`` whose only membership is
``minos_runner`` therefore cannot use that path at all, and the correct answer is emphatically
NOT to hand the runner service ``minos_admin``.

This migration closes that gap additively, without redesigning the experiment schema and without
touching ``0001``–``0010``:

* **An execution authority.** ``experiments.l2f2_execution_authorities`` binds one persisted plan
  to the frozen L2-F2-B protocol hash, the TRAIN schedule, the candidate set, the parameter space
  and the canary job key. It is append-only and no application role may write it — a control
  plane creates it, and the runner may only be *checked against* it.
* **A narrow resolution function.** One ``SECURITY DEFINER`` call returns exactly the truth-free
  scientific identity needed to run GATK for a job this worker already owns. Truth digests,
  mutation digests, evaluation rows and any non-TRAIN member are structurally unreachable
  through it.
* **A narrow artifact registrar.** The runner may register exactly two artifact kinds, and the
  media type and provenance are fixed inside the function — the caller supplies content identity
  only, so this path cannot be used to register some other kind of artifact.

The existing ``0007``/``0008`` claim, start, release, complete-success and fail functions are
reused unchanged; nothing is duplicated.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0011_l2f2_runner_boundary"
down_revision: str | None = "0010_l2f2_evaluation_corrective"
branch_labels = None
depends_on = None

_SCHEMA = "experiments"
_AUTHORITIES = "experiments.l2f2_execution_authorities"
_PLANS = "experiments.l2f_experiment_plans"
_JOBS = "experiments.l2f_experiment_jobs"

_RESOLVE_FN = "experiments.l2f2_resolve_claimed_execution"
_REGISTER_FN = "experiments.l2f2_register_execution_artifact"

#: reused from 0001 — the shared append-only rejection trigger function.
_REJECT_MUTATION = "audit.minos_reject_mutation"

_DENIED_ROLES = ("minos_live", "minos_trainer", "minos_evaluator")

#: the ONLY phase 0011 admits. A later phase is a later migration, never a looser CHECK.
_PHASES = ("PHASE_A",)

#: the frozen L2-F2-B protocol this boundary executes under.
_PROTOCOL_HASH = "c548e190571f5e964560cf30021a520ea8aad6674569fa3202af880d7dff77d1"

#: exactly the two artifact kinds an execution produces, with their accepted identities.
_VCF_MEDIA = "application/vnd.ga4gh.vcf"
_VCF_PROVENANCE = "l2f:gatk-vcf"
_MANIFEST_MEDIA = "application/vnd.minos.l2f-execution-result+json"
_MANIFEST_PROVENANCE = "l2f:execution-result-json"

_SQLSTATE_INVALID_WORKER = "MN001"
_SQLSTATE_PLAN_ABSENT = "MN002"
_SQLSTATE_NOT_OWNED = "MN003"
_SQLSTATE_AUTHORITY = "MN030"
_SQLSTATE_ARTIFACT_CONFLICT = "MN031"
_SQLSTATE_INVALID = "22023"

_RESOLVE_COLS = (
    "job_id uuid, job_key text, plan_id uuid, plan_member_id uuid, plan_config_id uuid, "
    "member_index integer, partition text, dataset_id text, round_id text, chromosome text, "
    "region_hash text, region_start0 bigint, region_end0_exclusive bigint, "
    "bam_sha256 text, bai_sha256 text, reference_sha256 text, fai_sha256 text, "
    "bam_size_bytes bigint, profile_id text, content_hash text, feature_values_hash text, "
    "config_index integer, config_hash text, parameter_space_hash text, "
    "config_media_type text, config_uri text, config_sha256 text, config_size_bytes integer"
)


def _hex_ck(column: str, name: str) -> sa.CheckConstraint:
    return sa.CheckConstraint(f"{column} ~ '^[0-9a-f]{{64}}$'", name=name)


_PLAN_COMPOSITE = "uq_l2f_experiment_plans_id_hash"


def _create_authority_table() -> None:
    # the composite FK target the authority binds against. Following the 0009 precedent: `id`
    # is already the primary key, so this adds no new uniqueness — it only makes the existing
    # (id, plan_hash) pair addressable as a foreign key, which is what binds an authority to
    # the EXACT persisted plan rather than merely to a plan id.
    op.create_unique_constraint(
        _PLAN_COMPOSITE, "l2f_experiment_plans", ["id", "plan_hash"], schema=_SCHEMA
    )
    op.create_table(
        "l2f2_execution_authorities",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=False),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("baseline_protocol_hash", sa.CHAR(64), nullable=False),
        sa.Column("phase", sa.Text(), nullable=False),
        sa.Column("plan_id", postgresql.UUID(as_uuid=False), nullable=False),
        sa.Column("plan_hash", sa.CHAR(64), nullable=False),
        sa.Column("train_schedule_sha256", sa.CHAR(64), nullable=False),
        sa.Column("candidate_set_hash", sa.CHAR(64), nullable=False),
        sa.Column("parameter_space_hash", sa.CHAR(64), nullable=False),
        sa.Column("member_count", sa.Integer(), nullable=False),
        sa.Column("candidate_count", sa.Integer(), nullable=False),
        sa.Column("logical_job_count", sa.Integer(), nullable=False),
        sa.Column("canary_job_key", sa.CHAR(64), nullable=False),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="pk_l2f2_execution_authorities"),
        # the authority binds the EXACT persisted plan, by id AND by hash together.
        sa.ForeignKeyConstraint(
            ["plan_id", "plan_hash"],
            [f"{_PLANS}.id", f"{_PLANS}.plan_hash"],
            name="fk_l2f2_authority_plan",
        ),
        sa.UniqueConstraint("plan_id", "phase", name="uq_l2f2_authority_plan_phase"),
        sa.UniqueConstraint("plan_hash", "phase", name="uq_l2f2_authority_plan_hash_phase"),
        _hex_ck("baseline_protocol_hash", "ck_l2f2_authority_protocol_hex"),
        _hex_ck("plan_hash", "ck_l2f2_authority_plan_hash_hex"),
        _hex_ck("train_schedule_sha256", "ck_l2f2_authority_schedule_hex"),
        _hex_ck("candidate_set_hash", "ck_l2f2_authority_candidate_hex"),
        _hex_ck("parameter_space_hash", "ck_l2f2_authority_space_hex"),
        _hex_ck("canary_job_key", "ck_l2f2_authority_canary_hex"),
        sa.CheckConstraint(
            "phase IN (" + ", ".join(f"'{p}'" for p in _PHASES) + ")",
            name="ck_l2f2_authority_phase",
        ),
        sa.CheckConstraint(
            f"baseline_protocol_hash = '{_PROTOCOL_HASH}'",
            name="ck_l2f2_authority_frozen_protocol",
        ),
        sa.CheckConstraint("member_count > 0", name="ck_l2f2_authority_members_positive"),
        sa.CheckConstraint("candidate_count > 0", name="ck_l2f2_authority_candidates_positive"),
        sa.CheckConstraint(
            "logical_job_count = member_count * candidate_count",
            name="ck_l2f2_authority_job_product",
        ),
        schema=_SCHEMA,
    )
    # append-only: an execution authority is history, never edited.
    op.execute(
        f"CREATE TRIGGER trg_l2f2_execution_authorities_append_only "
        f"BEFORE UPDATE OR DELETE ON {_AUTHORITIES} "
        f"FOR EACH ROW EXECUTE FUNCTION {_REJECT_MUTATION}();"
    )


def _create_functions() -> None:
    """Truth-free resolution and a narrow artifact registrar, both SECURITY DEFINER."""
    op.execute(
        f"CREATE OR REPLACE FUNCTION {_RESOLVE_FN}"
        "(p_plan_hash text, p_job_id uuid, p_worker_id text) "
        f"RETURNS TABLE({_RESOLVE_COLS}) LANGUAGE plpgsql SECURITY DEFINER "
        "SET search_path = pg_catalog, public AS $resolve$ "
        "DECLARE v_auth record; BEGIN "
        "IF p_worker_id IS NULL OR btrim(p_worker_id) = '' THEN "
        "  RAISE EXCEPTION 'worker_id must be a non-empty identifier' "
        f"    USING ERRCODE = '{_SQLSTATE_INVALID_WORKER}'; END IF; "
        # the plan must carry a FROZEN-protocol PHASE_A execution authority.
        f"SELECT a.* INTO v_auth FROM {_AUTHORITIES} a "
        "  WHERE a.plan_hash = p_plan_hash AND a.phase = 'PHASE_A'; "
        "IF NOT FOUND THEN "
        "  RAISE EXCEPTION 'plan % has no PHASE_A L2-F2 execution authority', p_plan_hash "
        f"    USING ERRCODE = '{_SQLSTATE_AUTHORITY}'; END IF; "
        f"IF v_auth.baseline_protocol_hash <> '{_PROTOCOL_HASH}' THEN "
        "  RAISE EXCEPTION 'execution authority does not cite the frozen baseline protocol' "
        f"    USING ERRCODE = '{_SQLSTATE_AUTHORITY}'; END IF; "
        # the job must belong to that plan and be owned by THIS worker. CLAIMED is admitted as
        # well as RUNNING so a preparation failure still recovers to PENDING rather than being
        # forced into a durable FAILED row, exactly as the historical F5 contract requires.
        "RETURN QUERY "
        # every char(n) column is cast to text so the returned structure matches the declared
        # result type exactly; a mismatch would surface only at call time.
        "SELECT j.id, j.job_key::text, j.plan_id, j.plan_member_id, j.plan_config_id, "
        "       pm.member_index::integer, pm.partition::text, dr.dataset_id::text, "
        "       dr.round_id::text, "
        "       dr.chromosome::text, dr.region_hash::text, dr.region_start0::bigint, "
        "       dr.region_end0_exclusive::bigint, dr.bam_sha256::text, dr.bai_sha256::text, "
        "       dr.reference_sha256::text, dr.fai_sha256::text, dr.bam_size_bytes::bigint, "
        "       bp.profile_id::text, bp.content_hash::text, pm.feature_values_hash::text, "
        "       pc.config_index::integer, pc.config_hash::text, pc.parameter_space_hash::text, "
        "       cp.media_type::text, a.uri::text, a.sha256::text, a.size_bytes::integer "
        f"  FROM {_JOBS} j "
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

    op.execute(
        f"CREATE OR REPLACE FUNCTION {_REGISTER_FN}"
        "(p_kind text, p_sha256 char(64), p_uri text, p_size_bytes integer) "
        "RETURNS TABLE(artifact_id uuid, created boolean) LANGUAGE plpgsql SECURITY DEFINER "
        "SET search_path = pg_catalog, public AS $reg$ "
        "DECLARE v_media text; v_prov text; v_row record; v_id uuid; BEGIN "
        "IF p_kind = 'vcf' THEN "
        f"  v_media := '{_VCF_MEDIA}'; v_prov := '{_VCF_PROVENANCE}'; "
        "ELSIF p_kind = 'result_manifest' THEN "
        f"  v_media := '{_MANIFEST_MEDIA}'; v_prov := '{_MANIFEST_PROVENANCE}'; "
        "ELSE "
        "  RAISE EXCEPTION 'unsupported execution artifact kind %', p_kind "
        f"    USING ERRCODE = '{_SQLSTATE_INVALID}'; END IF; "
        "IF p_sha256 IS NULL OR p_sha256 !~ '^[0-9a-f]{64}$' THEN "
        "  RAISE EXCEPTION 'execution artifact sha256 must be canonical lowercase hex' "
        f"    USING ERRCODE = '{_SQLSTATE_INVALID}'; END IF; "
        "IF p_uri IS NULL OR length(btrim(p_uri)) = 0 THEN "
        "  RAISE EXCEPTION 'execution artifact uri must be non-empty' "
        f"    USING ERRCODE = '{_SQLSTATE_INVALID}'; END IF; "
        "IF p_size_bytes IS NULL OR p_size_bytes < 0 THEN "
        "  RAISE EXCEPTION 'execution artifact size_bytes must be non-negative' "
        f"    USING ERRCODE = '{_SQLSTATE_INVALID}'; END IF; "
        "SELECT * INTO v_row FROM catalog.artifacts a WHERE a.sha256 = p_sha256; "
        "IF FOUND THEN "
        "  IF v_row.uri IS DISTINCT FROM p_uri "
        "     OR v_row.media_type IS DISTINCT FROM v_media "
        "     OR v_row.size_bytes IS DISTINCT FROM p_size_bytes "
        "     OR v_row.provenance IS DISTINCT FROM v_prov THEN "
        "    RAISE EXCEPTION 'artifact % is already registered with different metadata', "
        "      p_sha256 "
        f"      USING ERRCODE = '{_SQLSTATE_ARTIFACT_CONFLICT}'; END IF; "
        "  RETURN QUERY SELECT v_row.id, false; RETURN; END IF; "
        "INSERT INTO catalog.artifacts (uri, sha256, media_type, size_bytes, provenance) "
        "VALUES (p_uri, p_sha256, v_media, p_size_bytes, v_prov) RETURNING id INTO v_id; "
        "RETURN QUERY SELECT v_id, true; END; $reg$;"
    )


_FUNCTION_SIGS = (
    f"{_RESOLVE_FN}(text, uuid, text)",
    f"{_REGISTER_FN}(text, char, text, integer)",
)


def _apply_least_privilege() -> None:
    """The runner gets EXECUTE on exactly two functions and no table privilege anywhere."""
    op.execute(f"REVOKE ALL ON {_AUTHORITIES} FROM PUBLIC;")
    for role in ("minos_runner", *_DENIED_ROLES):
        op.execute(f"REVOKE ALL ON {_AUTHORITIES} FROM {role};")
    # the CONTROL PLANE (minos_admin) creates and reads authority rows; UPDATE/DELETE are
    # withheld and additionally refused by the append-only trigger. No APPLICATION role — runner,
    # evaluator, trainer or live — receives any privilege on this table at all.
    op.execute(f"GRANT SELECT, INSERT ON {_AUTHORITIES} TO minos_admin;")

    # the runner boundary refuses any database at the wrong revision, so it must be able to
    # READ the revision. alembic_version carries no scientific data; SELECT is the whole grant.
    op.execute("GRANT SELECT ON public.alembic_version TO minos_runner;")

    for sig in _FUNCTION_SIGS:
        op.execute(f"REVOKE ALL ON FUNCTION {sig} FROM PUBLIC;")
        for role in _DENIED_ROLES:
            op.execute(f"REVOKE ALL ON FUNCTION {sig} FROM {role};")
        op.execute(f"GRANT EXECUTE ON FUNCTION {sig} TO minos_runner;")
        op.execute(f"GRANT EXECUTE ON FUNCTION {sig} TO minos_admin;")


def upgrade() -> None:
    _create_authority_table()
    _create_functions()
    _apply_least_privilege()


def downgrade() -> None:
    """Restore 0010 exactly: drop only the objects and grants 0011 introduced."""
    for sig in _FUNCTION_SIGS:
        op.execute(f"DROP FUNCTION IF EXISTS {sig};")
    op.execute(
        f"DROP TRIGGER IF EXISTS trg_l2f2_execution_authorities_append_only ON {_AUTHORITIES};"
    )
    op.execute("REVOKE SELECT ON public.alembic_version FROM minos_runner;")
    op.drop_table("l2f2_execution_authorities", schema=_SCHEMA)
    op.drop_constraint(_PLAN_COMPOSITE, "l2f_experiment_plans", schema=_SCHEMA, type_="unique")
