"""L2-F F5 GATK execution results + terminal job transitions (additive to 0007).

Adds the F5 durable-outcome machinery WITHOUT touching the five L2-F tables created by ``0006``
(byte-identical) or the F4 claim functions created by ``0007`` (byte-identical). Three things are
added:

1. ``experiments.l2f_execution_results`` — success-only, exactly one immutable row per
   SUCCEEDED job, binding the accepted plan, the exact job/job_key/member/config, the CONFIG and
   parameter-space identities, the input identity, the logical argv, the GATK executable identity
   and version, the published VCF and result-manifest artifacts, the frozen ``result_hash`` and
   the observed runtime.
2. ``experiments.l2f_execution_failures`` — failure-only, exactly one immutable row per FAILED
   job, carrying only a bounded failure code plus optional exit code / stderr digest. It carries
   NO success-result field.
3. The terminal transition guard (``RUNNING -> SUCCEEDED`` / ``RUNNING -> FAILED``) plus three
   narrowly scoped ``SECURITY DEFINER`` functions that resolve, complete or fail exactly one
   RUNNING job of the accepted plan held by the requesting worker.

A job can never hold both a success and a failure record, a terminal UPDATE without the matching
durable record fails, ``CANCELLED`` stays unreachable, and both new tables are append-only.
Downgrade removes every F5 object and restores the exact ``0007`` behavior (including the F4-only
transition guard).
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0008_l2f_execution_results"
down_revision: str | None = "0007_l2f_job_claiming"
branch_labels = None
depends_on = None

_HEX = "^[0-9a-f]{64}$"
_JOBS = "experiments.l2f_experiment_jobs"
_PLANS = "experiments.l2f_experiment_plans"
_RESULTS = "experiments.l2f_execution_results"
_FAILURES = "experiments.l2f_execution_failures"

#: media types of the two published F5 artifacts (fixed by CHECK and bound through the 0006
#: composite artifact target ``uq_l2f_artifacts_id_sha_media``).
VCF_MEDIA_TYPE = "application/vnd.ga4gh.vcf"
RESULT_MANIFEST_MEDIA_TYPE = "application/vnd.minos.l2f-execution-result+json"

#: the complete bounded failure vocabulary (no free-text failure reasons enter the database).
FAILURE_CODES = (
    "PREPARATION_FAILED",
    "GATK_NONZERO_EXIT",
    "GATK_TIMEOUT",
    "GATK_OUTPUT_INVALID",
    "GATK_OUTPUT_MISSING",
    "EXECUTION_ERROR",
)

_GUARD = "experiments.minos_l2f_job_transition_guard"
_REJECT_MUTATION = "audit.minos_reject_mutation"
_EXCLUSIVE = "experiments.minos_l2f_reject_dual_outcome"

_RESOLVE_FN = "experiments.minos_l2f_resolve_running_job"
_COMPLETE_FN = "experiments.minos_l2f_complete_job_success"
_FAIL_FN = "experiments.minos_l2f_fail_job"

_RESOLVE_SIG = f"{_RESOLVE_FN}(text, uuid, text)"
_COMPLETE_SIG = (
    f"{_COMPLETE_FN}(text, uuid, text, text, text, text, text, text, text, text, "
    "uuid, text, uuid, text, text, bigint)"
)
_FAIL_SIG = f"{_FAIL_FN}(text, uuid, text, text, integer, text)"
_F5_FUNCTION_SIGS = (_RESOLVE_SIG, _COMPLETE_SIG, _FAIL_SIG)

_DENIED_ROLES = ("minos_live", "minos_trainer", "minos_evaluator")

# stable SQLSTATEs (the Python boundary maps these to typed errors; never message matching).
_SQLSTATE_INVALID_WORKER = "MN001"
_SQLSTATE_PLAN_ABSENT = "MN002"
_SQLSTATE_NOT_OWNED = "MN003"
_SQLSTATE_CLAIM_INVARIANT = "MN010"
_SQLSTATE_CLAIM_METADATA = "MN011"
_SQLSTATE_TRANSITION = "MN012"
_SQLSTATE_MISSING_RECORD = "MN020"
_SQLSTATE_DUAL_OUTCOME = "MN021"
_SQLSTATE_RESULT_CONFLICT = "MN022"

#: additive composite-UNIQUE targets required so the F5 rows can FK the exact job identity.
_F5_COMPOSITE_TARGETS: tuple[tuple[str, str, str, tuple[str, ...]], ...] = (
    ("experiments", "l2f_experiment_jobs", "uq_l2f_jobs_id_plan", ("id", "plan_id")),
    ("experiments", "l2f_experiment_jobs", "uq_l2f_jobs_id_job_key", ("id", "job_key")),
    (
        "experiments",
        "l2f_experiment_jobs",
        "uq_l2f_jobs_id_member_config",
        ("id", "plan_member_id", "plan_config_id"),
    ),
    (
        "experiments",
        "l2f_experiment_plan_configs",
        "uq_l2f_pc_id_hash_space",
        ("id", "config_hash", "parameter_space_hash"),
    ),
)


def _uuid_pk() -> sa.Column:
    return sa.Column(
        "id",
        postgresql.UUID(as_uuid=False),
        nullable=False,
        server_default=sa.text("gen_random_uuid()"),
    )


def _uuid(name: str, *, nullable: bool = False) -> sa.Column:
    return sa.Column(name, postgresql.UUID(as_uuid=False), nullable=nullable)


def _sha(name: str, *, nullable: bool = False) -> sa.Column:
    return sa.Column(name, sa.CHAR(64), nullable=nullable)


def _ts(name: str = "created_at") -> sa.Column:
    return sa.Column(
        name, sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")
    )


def _hex_ck(col: str, name: str, *, nullable: bool = False) -> sa.CheckConstraint:
    expr = f"{col} ~ '{_HEX}'" if not nullable else f"{col} IS NULL OR {col} ~ '{_HEX}'"
    return sa.CheckConstraint(expr, name=name)


def _add_composite_targets() -> None:
    for schema, table, name, cols in _F5_COMPOSITE_TARGETS:
        op.create_unique_constraint(name, table, list(cols), schema=schema)


def _drop_composite_targets() -> None:
    for schema, table, name, _cols in _F5_COMPOSITE_TARGETS:
        op.drop_constraint(name, table, schema=schema, type_="unique")


def _create_results() -> None:
    op.create_table(
        "l2f_execution_results",
        _uuid_pk(),
        _uuid("plan_id"),
        _uuid("job_id"),
        _sha("job_key"),
        _uuid("plan_member_id"),
        _uuid("plan_config_id"),
        _sha("config_hash"),
        _sha("parameter_space_hash"),
        _sha("input_identity_hash"),
        _sha("logical_argv_hash"),
        _sha("gatk_executable_sha256"),
        sa.Column("gatk_version", sa.Text(), nullable=False),
        _uuid("vcf_artifact_id"),
        _sha("vcf_sha256"),
        sa.Column("vcf_media_type", sa.Text(), nullable=False, server_default=VCF_MEDIA_TYPE),
        _uuid("result_manifest_artifact_id"),
        _sha("result_manifest_sha256"),
        sa.Column(
            "result_manifest_media_type",
            sa.Text(),
            nullable=False,
            server_default=RESULT_MANIFEST_MEDIA_TYPE,
        ),
        _sha("result_hash"),
        sa.Column("runtime_ms", sa.BigInteger(), nullable=False),
        _ts(),
        sa.PrimaryKeyConstraint("id", name="pk_l2f_execution_results"),
        # exactly ONE success result per job / job_key / result identity.
        sa.UniqueConstraint("job_id", name="uq_l2f_exec_results_job"),
        sa.UniqueConstraint("job_key", name="uq_l2f_exec_results_job_key"),
        sa.UniqueConstraint("result_hash", name="uq_l2f_exec_results_result_hash"),
        # the job MUST belong to the recorded plan AND carry the recorded job_key.
        sa.ForeignKeyConstraint(
            ["job_id", "plan_id"],
            [f"{_JOBS}.id", f"{_JOBS}.plan_id"],
            name="fk_l2f_exec_results_job_plan",
        ),
        sa.ForeignKeyConstraint(
            ["job_id", "job_key"],
            [f"{_JOBS}.id", f"{_JOBS}.job_key"],
            name="fk_l2f_exec_results_job_key",
        ),
        # the recorded member AND config MUST be the exact pair the job itself binds: a result
        # naming any other member or config - even one of the SAME plan - fails through this FK.
        sa.ForeignKeyConstraint(
            ["job_id", "plan_member_id", "plan_config_id"],
            [f"{_JOBS}.id", f"{_JOBS}.plan_member_id", f"{_JOBS}.plan_config_id"],
            name="fk_l2f_exec_results_job_member_config",
        ),
        # the member and config MUST belong to the same plan.
        sa.ForeignKeyConstraint(
            ["plan_member_id", "plan_id"],
            [
                "experiments.l2f_experiment_plan_members.id",
                "experiments.l2f_experiment_plan_members.plan_id",
            ],
            name="fk_l2f_exec_results_member_plan",
        ),
        # the config MUST carry the recorded config_hash AND parameter_space_hash.
        sa.ForeignKeyConstraint(
            ["plan_config_id", "config_hash", "parameter_space_hash"],
            [
                "experiments.l2f_experiment_plan_configs.id",
                "experiments.l2f_experiment_plan_configs.config_hash",
                "experiments.l2f_experiment_plan_configs.parameter_space_hash",
            ],
            name="fk_l2f_exec_results_config_identity",
        ),
        # the plan MUST carry the recorded parameter_space_hash.
        sa.ForeignKeyConstraint(
            ["plan_id", "parameter_space_hash"],
            [f"{_PLANS}.id", f"{_PLANS}.parameter_space_hash"],
            name="fk_l2f_exec_results_plan_param_space",
        ),
        # both artifacts are content-addressed at their fixed media type.
        sa.ForeignKeyConstraint(
            ["vcf_artifact_id", "vcf_sha256", "vcf_media_type"],
            ["catalog.artifacts.id", "catalog.artifacts.sha256", "catalog.artifacts.media_type"],
            name="fk_l2f_exec_results_vcf_artifact",
        ),
        sa.ForeignKeyConstraint(
            [
                "result_manifest_artifact_id",
                "result_manifest_sha256",
                "result_manifest_media_type",
            ],
            ["catalog.artifacts.id", "catalog.artifacts.sha256", "catalog.artifacts.media_type"],
            name="fk_l2f_exec_results_manifest_artifact",
        ),
        sa.CheckConstraint(
            f"vcf_media_type = '{VCF_MEDIA_TYPE}'", name="ck_l2f_exec_results_vcf_media"
        ),
        sa.CheckConstraint(
            f"result_manifest_media_type = '{RESULT_MANIFEST_MEDIA_TYPE}'",
            name="ck_l2f_exec_results_manifest_media",
        ),
        sa.CheckConstraint("runtime_ms >= 0", name="ck_l2f_exec_results_runtime_nonneg"),
        sa.CheckConstraint("length(gatk_version) > 0", name="ck_l2f_exec_results_version_nonempty"),
        sa.CheckConstraint(
            "vcf_artifact_id <> result_manifest_artifact_id",
            name="ck_l2f_exec_results_distinct_artifacts",
        ),
        _hex_ck("job_key", "ck_l2f_exec_results_job_key_hex"),
        _hex_ck("config_hash", "ck_l2f_exec_results_config_hex"),
        _hex_ck("parameter_space_hash", "ck_l2f_exec_results_space_hex"),
        _hex_ck("input_identity_hash", "ck_l2f_exec_results_input_hex"),
        _hex_ck("logical_argv_hash", "ck_l2f_exec_results_argv_hex"),
        _hex_ck("gatk_executable_sha256", "ck_l2f_exec_results_exe_hex"),
        _hex_ck("vcf_sha256", "ck_l2f_exec_results_vcf_hex"),
        _hex_ck("result_manifest_sha256", "ck_l2f_exec_results_manifest_hex"),
        _hex_ck("result_hash", "ck_l2f_exec_results_result_hex"),
        schema="experiments",
    )
    op.create_index(
        "ix_l2f_exec_results_plan_id", "l2f_execution_results", ["plan_id"], schema="experiments"
    )


def _create_failures() -> None:
    op.create_table(
        "l2f_execution_failures",
        _uuid_pk(),
        _uuid("plan_id"),
        _uuid("job_id"),
        _sha("job_key"),
        sa.Column("worker_id", sa.Text(), nullable=False),
        sa.Column("failure_code", sa.Text(), nullable=False),
        sa.Column("exit_code", sa.Integer(), nullable=True),
        _sha("stderr_sha256", nullable=True),
        _ts(),
        sa.PrimaryKeyConstraint("id", name="pk_l2f_execution_failures"),
        sa.UniqueConstraint("job_id", name="uq_l2f_exec_failures_job"),
        sa.UniqueConstraint("job_key", name="uq_l2f_exec_failures_job_key"),
        sa.ForeignKeyConstraint(
            ["job_id", "plan_id"],
            [f"{_JOBS}.id", f"{_JOBS}.plan_id"],
            name="fk_l2f_exec_failures_job_plan",
        ),
        sa.ForeignKeyConstraint(
            ["job_id", "job_key"],
            [f"{_JOBS}.id", f"{_JOBS}.job_key"],
            name="fk_l2f_exec_failures_job_key",
        ),
        sa.CheckConstraint(
            "failure_code IN (" + ", ".join(f"'{c}'" for c in FAILURE_CODES) + ")",
            name="ck_l2f_exec_failures_code_bounded",
        ),
        sa.CheckConstraint("length(worker_id) > 0", name="ck_l2f_exec_failures_worker_nonempty"),
        _hex_ck("job_key", "ck_l2f_exec_failures_job_key_hex"),
        _hex_ck("stderr_sha256", "ck_l2f_exec_failures_stderr_hex", nullable=True),
        schema="experiments",
    )
    op.create_index(
        "ix_l2f_exec_failures_plan_id", "l2f_execution_failures", ["plan_id"], schema="experiments"
    )


def _create_outcome_exclusivity() -> None:
    """A job may hold a success result XOR a failure record — never both."""
    op.execute(
        f"CREATE OR REPLACE FUNCTION {_EXCLUSIVE}() RETURNS trigger LANGUAGE plpgsql "
        "SET search_path = pg_catalog AS $excl$ "
        "DECLARE v_other integer; v_job uuid; "
        "BEGIN "
        # serialize every outcome creation for this job on the job ROW LOCK, so a concurrent
        # success and failure can never both observe an empty opposite table and both commit.
        f"SELECT j.id INTO v_job FROM {_JOBS} j WHERE j.id = NEW.job_id FOR UPDATE; "
        "IF v_job IS NULL THEN "
        "  RAISE EXCEPTION 'L2-F outcome references an unknown job %', NEW.job_id "
        f"    USING ERRCODE = '{_SQLSTATE_MISSING_RECORD}'; "
        "END IF; "
        "IF TG_TABLE_NAME = 'l2f_execution_results' THEN "
        f"  SELECT count(*) INTO v_other FROM {_FAILURES} f WHERE f.job_id = NEW.job_id; "
        "  IF v_other > 0 THEN "
        "    RAISE EXCEPTION 'L2-F job % already has a failure record', NEW.job_id "
        f"      USING ERRCODE = '{_SQLSTATE_DUAL_OUTCOME}'; "
        "  END IF; "
        "ELSE "
        f"  SELECT count(*) INTO v_other FROM {_RESULTS} r WHERE r.job_id = NEW.job_id; "
        "  IF v_other > 0 THEN "
        "    RAISE EXCEPTION 'L2-F job % already has a success result', NEW.job_id "
        f"      USING ERRCODE = '{_SQLSTATE_DUAL_OUTCOME}'; "
        "  END IF; "
        "END IF; "
        "RETURN NEW; END; $excl$;"
    )
    for table in ("l2f_execution_results", "l2f_execution_failures"):
        op.execute(
            f"CREATE TRIGGER trg_{table}_exclusive_outcome "
            f"BEFORE INSERT ON experiments.{table} "
            f"FOR EACH ROW EXECUTE FUNCTION {_EXCLUSIVE}();"
        )
        # both outcome tables are fully append-only (reusing the 0001 rejection function).
        op.execute(
            f"CREATE TRIGGER trg_{table}_append_only "
            f"BEFORE UPDATE OR DELETE ON experiments.{table} "
            f"FOR EACH ROW EXECUTE FUNCTION {_REJECT_MUTATION}();"
        )


def _terminal_guard_sql() -> str:
    """The F5 transition guard: the three F4 moves PLUS the two terminal moves, each of which
    requires its matching durable record to already exist in this transaction."""
    return (
        f"CREATE OR REPLACE FUNCTION {_GUARD}() RETURNS trigger LANGUAGE plpgsql "
        "SET search_path = pg_catalog AS $guard$ "
        "DECLARE v_results integer; v_failures integer; "
        "BEGIN "
        "IF NEW.status = 'PENDING' "
        "   AND (NEW.claimed_by IS NOT NULL OR NEW.claimed_at IS NOT NULL) THEN "
        "  RAISE EXCEPTION 'L2-F job PENDING requires claimed_by IS NULL and claimed_at IS NULL' "
        f"    USING ERRCODE = '{_SQLSTATE_CLAIM_INVARIANT}'; "
        "END IF; "
        "IF NEW.status IN ('CLAIMED', 'RUNNING', 'SUCCEEDED', 'FAILED') "
        "   AND (NEW.claimed_by IS NULL OR btrim(NEW.claimed_by) = '' "
        "        OR NEW.claimed_at IS NULL) THEN "
        "  RAISE EXCEPTION 'L2-F job % requires a non-empty claimed_by and a claimed_at', "
        f"    NEW.status USING ERRCODE = '{_SQLSTATE_CLAIM_INVARIANT}'; "
        "END IF; "
        "IF NEW.status = OLD.status THEN "
        "  IF NEW.claimed_by IS DISTINCT FROM OLD.claimed_by "
        "     OR NEW.claimed_at IS DISTINCT FROM OLD.claimed_at THEN "
        "    RAISE EXCEPTION 'L2-F job claim metadata may not change without a status transition' "
        f"      USING ERRCODE = '{_SQLSTATE_CLAIM_METADATA}'; "
        "  END IF; "
        "  RETURN NEW; "
        "END IF; "
        "IF OLD.status = 'PENDING' AND NEW.status = 'CLAIMED' THEN "
        "  RETURN NEW; "
        "ELSIF OLD.status = 'CLAIMED' AND NEW.status = 'PENDING' THEN "
        "  RETURN NEW; "
        "ELSIF OLD.status = 'CLAIMED' AND NEW.status = 'RUNNING' THEN "
        "  IF NEW.claimed_by IS DISTINCT FROM OLD.claimed_by "
        "     OR NEW.claimed_at IS DISTINCT FROM OLD.claimed_at THEN "
        "    RAISE EXCEPTION 'L2-F CLAIMED -> RUNNING must preserve claimed_by and claimed_at' "
        f"      USING ERRCODE = '{_SQLSTATE_CLAIM_METADATA}'; "
        "  END IF; "
        "  RETURN NEW; "
        "ELSIF OLD.status = 'RUNNING' AND NEW.status IN ('SUCCEEDED', 'FAILED') THEN "
        # terminal states preserve the claim identity exactly.
        "  IF NEW.claimed_by IS DISTINCT FROM OLD.claimed_by "
        "     OR NEW.claimed_at IS DISTINCT FROM OLD.claimed_at THEN "
        "    RAISE EXCEPTION 'L2-F terminal transition must preserve claimed_by and claimed_at' "
        f"      USING ERRCODE = '{_SQLSTATE_CLAIM_METADATA}'; "
        "  END IF; "
        f"  SELECT count(*) INTO v_results FROM {_RESULTS} r WHERE r.job_id = NEW.id; "
        f"  SELECT count(*) INTO v_failures FROM {_FAILURES} f WHERE f.job_id = NEW.id; "
        "  IF NEW.status = 'SUCCEEDED' AND (v_results <> 1 OR v_failures <> 0) THEN "
        "    RAISE EXCEPTION 'SUCCEEDED requires exactly one success result and no failure' "
        f"      USING ERRCODE = '{_SQLSTATE_MISSING_RECORD}'; "
        "  END IF; "
        "  IF NEW.status = 'FAILED' AND (v_failures <> 1 OR v_results <> 0) THEN "
        "    RAISE EXCEPTION 'FAILED requires exactly one failure record and no success result' "
        f"      USING ERRCODE = '{_SQLSTATE_MISSING_RECORD}'; "
        "  END IF; "
        "  RETURN NEW; "
        "END IF; "
        "RAISE EXCEPTION 'L2-F job transition % -> % is not permitted', "
        f"  OLD.status, NEW.status USING ERRCODE = '{_SQLSTATE_TRANSITION}'; "
        "END; $guard$;"
    )


def _f4_guard_sql() -> str:
    """The EXACT 0007 (F4-only) guard body, restored verbatim on downgrade."""
    return (
        f"CREATE OR REPLACE FUNCTION {_GUARD}() RETURNS trigger LANGUAGE plpgsql "
        "SET search_path = pg_catalog AS $guard$ "
        "BEGIN "
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
        "IF NEW.status = OLD.status THEN "
        "  IF NEW.claimed_by IS DISTINCT FROM OLD.claimed_by "
        "     OR NEW.claimed_at IS DISTINCT FROM OLD.claimed_at THEN "
        "    RAISE EXCEPTION 'L2-F job claim metadata may not change without a status transition' "
        f"      USING ERRCODE = '{_SQLSTATE_CLAIM_METADATA}'; "
        "  END IF; "
        "  RETURN NEW; "
        "END IF; "
        "IF OLD.status = 'PENDING' AND NEW.status = 'CLAIMED' THEN "
        "  RETURN NEW; "
        "ELSIF OLD.status = 'CLAIMED' AND NEW.status = 'PENDING' THEN "
        "  RETURN NEW; "
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


_RESOLVE_COLS = (
    "job_id uuid, job_key character(64), plan_id uuid, plan_member_id uuid, "
    "plan_config_id uuid, status text, claimed_by text, claimed_at timestamptz"
)


def _create_execution_functions() -> None:
    # 1) resolve one RUNNING job of the accepted plan held by this worker.
    op.execute(
        f"CREATE OR REPLACE FUNCTION {_RESOLVE_FN}"
        "(p_plan_hash text, p_job_id uuid, p_worker_id text) "
        f"RETURNS TABLE({_RESOLVE_COLS}) LANGUAGE plpgsql SECURITY DEFINER "
        "SET search_path = pg_catalog AS $resolve$ "
        "DECLARE v_plan_id uuid; "
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
        "RETURN QUERY SELECT j.id, j.job_key, j.plan_id, j.plan_member_id, j.plan_config_id, "
        "                    j.status, j.claimed_by, j.claimed_at "
        f"  FROM {_JOBS} j "
        " WHERE j.id = p_job_id AND j.plan_id = v_plan_id "
        "   AND j.status = 'RUNNING' AND j.claimed_by = p_worker_id; "
        "IF NOT FOUND THEN "
        "  RAISE EXCEPTION 'job % of plan % is not RUNNING for worker %', "
        f"    p_job_id, p_plan_hash, p_worker_id USING ERRCODE = '{_SQLSTATE_NOT_OWNED}'; "
        "END IF; "
        "END; $resolve$;"
    )

    # 2) complete one RUNNING job as SUCCEEDED, inserting/verifying its exact result.
    op.execute(
        f"CREATE OR REPLACE FUNCTION {_COMPLETE_FN}"
        "(p_plan_hash text, p_job_id uuid, p_worker_id text, p_job_key text, "
        " p_config_hash text, p_parameter_space_hash text, p_input_identity_hash text, "
        " p_logical_argv_hash text, p_gatk_executable_sha256 text, p_gatk_version text, "
        " p_vcf_artifact_id uuid, p_vcf_sha256 text, p_manifest_artifact_id uuid, "
        " p_manifest_sha256 text, p_result_hash text, p_runtime_ms bigint) "
        "RETURNS TABLE(result_id uuid, created boolean) LANGUAGE plpgsql SECURITY DEFINER "
        "SET search_path = pg_catalog AS $complete$ "
        "DECLARE v_plan_id uuid; v_member uuid; v_config uuid; v_status text; "
        "        v_existing record; v_id uuid; v_created boolean := false; v_rows integer; "
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
        # LOCK the job row BEFORE inspecting either outcome table: this is what makes a
        # concurrent success and failure for one job mutually exclusive rather than a race.
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
        # under the lock: a durable failure forbids a success outcome for the same job.
        f"IF EXISTS (SELECT 1 FROM {_FAILURES} f WHERE f.job_id = p_job_id) THEN "
        "  RAISE EXCEPTION 'L2-F job % already has a failure record', p_job_id "
        f"    USING ERRCODE = '{_SQLSTATE_DUAL_OUTCOME}'; "
        "END IF; "
        # idempotent get-or-verify: an EXACT existing result is success; ANY difference in ANY
        # immutable stored column - including runtime_ms and the media types - is a conflict.
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
        "     result_manifest_artifact_id, result_manifest_sha256, result_hash, runtime_ms) "
        "  VALUES (v_plan_id, p_job_id, p_job_key, v_member, v_config, p_config_hash, "
        "          p_parameter_space_hash, p_input_identity_hash, p_logical_argv_hash, "
        "          p_gatk_executable_sha256, p_gatk_version, p_vcf_artifact_id, p_vcf_sha256, "
        "          p_manifest_artifact_id, p_manifest_sha256, p_result_hash, p_runtime_ms) "
        "  RETURNING id INTO v_id; "
        "  v_created := true; "
        "END IF; "
        # the terminal UPDATE must affect EXACTLY one row (an already-SUCCEEDED replay performs
        # no UPDATE at all, so no already-terminal row is silently re-stamped).
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

    # 3) fail one RUNNING job as FAILED, inserting/verifying its exact failure record.
    op.execute(
        f"CREATE OR REPLACE FUNCTION {_FAIL_FN}"
        "(p_plan_hash text, p_job_id uuid, p_worker_id text, p_failure_code text, "
        " p_exit_code integer, p_stderr_sha256 text) "
        "RETURNS TABLE(failure_id uuid, created boolean) LANGUAGE plpgsql SECURITY DEFINER "
        "SET search_path = pg_catalog AS $fail$ "
        "DECLARE v_plan_id uuid; v_job_key text; v_status text; v_existing record; v_id uuid; "
        "        v_created boolean := false; v_rows integer; "
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
        # identical serialization to the success path: LOCK the job row BEFORE looking at either
        # outcome table, so success and failure for one job can never both commit.
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
        "     OR v_existing.stderr_sha256 IS DISTINCT FROM p_stderr_sha256 THEN "
        "    RAISE EXCEPTION 'an existing L2-F execution failure for job % differs', p_job_id "
        f"      USING ERRCODE = '{_SQLSTATE_RESULT_CONFLICT}'; "
        "  END IF; "
        "  v_id := v_existing.id; "
        "ELSE "
        f"  INSERT INTO {_FAILURES} "
        "    (plan_id, job_id, job_key, worker_id, failure_code, exit_code, stderr_sha256) "
        "  VALUES (v_plan_id, p_job_id, v_job_key, p_worker_id, p_failure_code, p_exit_code, "
        "          p_stderr_sha256) RETURNING id INTO v_id; "
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


def _apply_least_privilege() -> None:
    for sig in _F5_FUNCTION_SIGS:
        op.execute(f"REVOKE ALL ON FUNCTION {sig} FROM PUBLIC;")
        for role in (*_DENIED_ROLES, "minos_runner"):
            op.execute(f"REVOKE ALL ON FUNCTION {sig} FROM {role};")
        op.execute(f"GRANT EXECUTE ON FUNCTION {sig} TO minos_runner;")
        op.execute(f"GRANT EXECUTE ON FUNCTION {sig} TO minos_admin;")
    # trigger functions are never directly executable by an application role.
    for fn in (f"{_EXCLUSIVE}()", f"{_GUARD}()"):
        op.execute(f"REVOKE ALL ON FUNCTION {fn} FROM PUBLIC;")
        for role in ("minos_runner", *_DENIED_ROLES):
            op.execute(f"REVOKE ALL ON FUNCTION {fn} FROM {role};")
    # no direct table privilege on ANY L2-F table for any application role.
    for table in (_RESULTS, _FAILURES, _JOBS):
        op.execute(f"REVOKE ALL ON {table} FROM PUBLIC;")
        for role in ("minos_runner", *_DENIED_ROLES):
            op.execute(f"REVOKE ALL ON {table} FROM {role};")


def upgrade() -> None:
    op.execute("SET ROLE minos_admin")
    _add_composite_targets()
    _create_results()
    _create_failures()
    _create_outcome_exclusivity()
    op.execute(_terminal_guard_sql())
    _create_execution_functions()
    _apply_least_privilege()
    op.execute("RESET ROLE")


def downgrade() -> None:
    op.execute("SET ROLE minos_admin")
    for sig in _F5_FUNCTION_SIGS:
        op.execute(f"DROP FUNCTION IF EXISTS {sig};")
    for table in ("l2f_execution_results", "l2f_execution_failures"):
        op.execute(f"DROP TRIGGER IF EXISTS trg_{table}_exclusive_outcome ON experiments.{table};")
        op.execute(f"DROP TRIGGER IF EXISTS trg_{table}_append_only ON experiments.{table};")
    op.drop_table("l2f_execution_results", schema="experiments")
    op.drop_table("l2f_execution_failures", schema="experiments")
    op.execute(f"DROP FUNCTION IF EXISTS {_EXCLUSIVE}();")
    # restore the EXACT F4-only transition guard from 0007.
    op.execute(_f4_guard_sql())
    _drop_composite_targets()
    for table in (_JOBS,):
        op.execute(f"REVOKE ALL ON {table} FROM PUBLIC;")
        for role in ("minos_runner", *_DENIED_ROLES):
            op.execute(f"REVOKE ALL ON {table} FROM {role};")
    op.execute("RESET ROLE")
