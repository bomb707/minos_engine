"""L2-F2-A: additive offline truth-aware evaluation ledger.

Strictly additive over ``0008_l2f_execution_results``. Nothing in ``0001``-``0008`` is altered,
and the legacy ``evaluation.evaluations`` placeholder (which references ``experiments.results``)
is left untouched — L2-F2 gets its own explicit tables rather than being forced into that shape.

The design keeps the GATK runner truth-free. Execution stays in ``experiments`` under
``minos_runner``; scoring lives in ``evaluation`` under ``minos_evaluator``, reading executions
only through a narrow projection and writing only through ``SECURITY DEFINER`` functions that
derive dataset/truth identity themselves. A caller therefore cannot score execution A against
truth B: it never supplies those values.

``minos_evaluator`` remains a NOLOGIN group role. An external service principal
(``minos_evaluator_svc``) is granted membership outside Git — see
``docs/layer2/EVALUATOR_SERVICE_PROVISIONING.md``.

Revision ID: 0009_l2f_evaluation_results
Revises: 0008_l2f_execution_results
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0009_l2f_evaluation_results"
down_revision: str | None = "0008_l2f_execution_results"
branch_labels = None
depends_on = None

_SCHEMA = "evaluation"
_RESULTS = "evaluation.l2f_evaluation_results"
_FAILURES = "evaluation.l2f_evaluation_failures"
_TRAIN_TARGETS = "evaluation.l2f_train_truth_registration_targets"
_EXEC_INPUTS = "evaluation.l2f_completed_execution_inputs"

#: reused from 0001 — the shared append-only rejection trigger function.
_REJECT_MUTATION = "audit.minos_reject_mutation"

_EXCLUSIVE = "evaluation.l2f_evaluation_exclusive_outcome"
_REGISTER_TRUTH = "evaluation.l2f_register_train_truth_identity"
_RECORD_RESULT = "evaluation.l2f_record_evaluation_result"
_RECORD_FAILURE = "evaluation.l2f_record_evaluation_failure"

#: distinct SQLSTATEs so callers can tell the failure modes apart without parsing text.
_SQLSTATE_DUAL_OUTCOME = "23514"
_SQLSTATE_CONFLICT = "23505"
_SQLSTATE_PARTITION = "42501"

_DENIED_ROLES = ("minos_live", "minos_runner", "minos_trainer")

_ADMISSION_CODES = (
    "ADMITTED",
    "NONPOSITIVE_SCORE",
    "OUT_OF_RANGE_SCORE",
    "ZERO_INPUT_FINGERPRINT",
)

_FAILURE_CODES = (
    "TRUTH_IDENTITY_MISSING",
    "TRUTH_BYTES_MISMATCH",
    "VCF_BYTES_MISMATCH",
    "HAPPY_NONZERO_EXIT",
    "HAPPY_TIMEOUT",
    "HAPPY_OUTPUT_INVALID",
    "SCORER_OUTPUT_INVALID",
    "ARTIFACT_PUBLISH_FAILED",
    "EVALUATION_ERROR",
)


def _uuid_pk() -> sa.Column:
    return sa.Column(
        "id",
        postgresql.UUID(as_uuid=False),
        primary_key=True,
        server_default=sa.text("gen_random_uuid()"),
        nullable=False,
    )


def _uuid(name: str, *, nullable: bool = False) -> sa.Column:
    return sa.Column(name, postgresql.UUID(as_uuid=False), nullable=nullable)


def _sha(name: str, *, nullable: bool = False) -> sa.Column:
    return sa.Column(name, sa.CHAR(64), nullable=nullable)


def _ts(name: str = "created_at") -> sa.Column:
    return sa.Column(
        name, sa.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False
    )


def _hex_ck(col: str, name: str, *, nullable: bool = False) -> sa.CheckConstraint:
    expr = f"{col} ~ '^[0-9a-f]{{64}}$'"
    return sa.CheckConstraint(f"{col} IS NULL OR {expr}" if nullable else expr, name=name)


def _in_ck(col: str, values: tuple[str, ...], name: str) -> sa.CheckConstraint:
    rendered = ", ".join(f"'{v}'" for v in values)
    return sa.CheckConstraint(f"{col} IN ({rendered})", name=name)


# --------------------------------------------------------------------------- #
# composite UNIQUE targets added HERE (never by editing accepted migrations) so the
# evaluation tables can bind execution -> dataset declaratively rather than on trust.
# --------------------------------------------------------------------------- #
def _create_composite_targets() -> None:
    op.create_unique_constraint(
        "uq_l2f_execution_results_id_job",
        "l2f_execution_results",
        ["id", "job_id"],
        schema="experiments",
    )
    op.create_unique_constraint(
        "uq_l2f_experiment_jobs_id_member",
        "l2f_experiment_jobs",
        ["id", "plan_member_id"],
        schema="experiments",
    )
    op.create_unique_constraint(
        "uq_l2f_plan_members_id_registry",
        "l2f_experiment_plan_members",
        ["id", "dataset_registry_id"],
        schema="experiments",
    )
    op.create_unique_constraint(
        "uq_dataset_eval_identity_id_registry",
        "dataset_evaluation_identity",
        ["id", "dataset_registry_id"],
        schema="evaluation",
    )


def _drop_composite_targets() -> None:
    op.drop_constraint(
        "uq_dataset_eval_identity_id_registry",
        "dataset_evaluation_identity",
        schema="evaluation",
        type_="unique",
    )
    op.drop_constraint(
        "uq_l2f_plan_members_id_registry",
        "l2f_experiment_plan_members",
        schema="experiments",
        type_="unique",
    )
    op.drop_constraint(
        "uq_l2f_experiment_jobs_id_member",
        "l2f_experiment_jobs",
        schema="experiments",
        type_="unique",
    )
    op.drop_constraint(
        "uq_l2f_execution_results_id_job",
        "l2f_execution_results",
        schema="experiments",
        type_="unique",
    )


def _create_results() -> None:
    op.create_table(
        "l2f_evaluation_results",
        _uuid_pk(),
        # --- what was scored -------------------------------------------------------------
        _uuid("execution_result_id"),
        _sha("execution_result_hash"),
        _uuid("dataset_registry_id"),
        sa.Column("partition", sa.Text(), nullable=False),
        # --- the truth it was scored against --------------------------------------------
        _uuid("dataset_evaluation_identity_id"),
        _sha("truth_vcf_sha256"),
        _sha("truth_tbi_sha256"),
        _sha("mutations_vcf_sha256"),
        _sha("mutations_tbi_sha256"),
        # --- the semantics under which it was scored ------------------------------------
        _sha("scoring_contract_hash"),
        sa.Column("scorer_upstream_commit", sa.Text(), nullable=False),
        _sha("scoring_py_sha256"),
        _sha("validator_py_sha256"),
        sa.Column("happy_image_digest", sa.Text(), nullable=False),
        sa.Column("bcftools_image_digest", sa.Text(), nullable=False),
        # --- the canonical metrics document ---------------------------------------------
        _uuid("metrics_artifact_id"),
        _sha("metrics_artifact_sha256"),
        sa.Column("metrics_media_type", sa.Text(), nullable=False),
        # --- typed, query-worthy score components ---------------------------------------
        sa.Column("core_score", sa.Float(), nullable=False),
        sa.Column("completeness_score", sa.Float(), nullable=False),
        sa.Column("fp_score", sa.Float(), nullable=False),
        sa.Column("quality_score", sa.Float(), nullable=False),
        sa.Column("overcall_penalty", sa.Float(), nullable=False),
        sa.Column("minos_score_100", sa.Float(), nullable=False),
        sa.Column("minos_score", sa.Float(), nullable=False),
        # --- what the validator would have done with it ---------------------------------
        sa.Column("admitted", sa.Boolean(), nullable=False),
        sa.Column("admission_code", sa.Text(), nullable=False),
        _sha("evaluation_hash"),
        _ts(),
        sa.PrimaryKeyConstraint("id", name="pk_l2f_evaluation_results"),
        # the execution result and its job/member/dataset lineage are bound declaratively:
        # a caller cannot score execution A against dataset B, because the (id, dataset) pair
        # must already exist together upstream.
        sa.ForeignKeyConstraint(
            ["execution_result_id"],
            ["experiments.l2f_execution_results.id"],
            name="fk_l2f_eval_results_execution",
        ),
        sa.ForeignKeyConstraint(
            ["dataset_registry_id"],
            ["catalog.dataset_registry.id"],
            name="fk_l2f_eval_results_registry",
        ),
        sa.ForeignKeyConstraint(
            ["dataset_evaluation_identity_id", "dataset_registry_id"],
            [
                "evaluation.dataset_evaluation_identity.id",
                "evaluation.dataset_evaluation_identity.dataset_registry_id",
            ],
            name="fk_l2f_eval_results_truth_identity",
        ),
        sa.ForeignKeyConstraint(
            ["metrics_artifact_id"],
            ["catalog.artifacts.id"],
            name="fk_l2f_eval_results_metrics_artifact",
        ),
        # a historical execution may be RESCORED under a NEW scoring contract without
        # overwriting the old evaluation, so the execution id alone is deliberately not unique.
        sa.UniqueConstraint(
            "execution_result_id",
            "scoring_contract_hash",
            name="uq_l2f_eval_results_execution_contract",
        ),
        sa.UniqueConstraint("evaluation_hash", name="uq_l2f_eval_results_evaluation_hash"),
        _hex_ck("execution_result_hash", "ck_l2f_eval_results_exec_hash_hex"),
        _hex_ck("truth_vcf_sha256", "ck_l2f_eval_results_truth_vcf_hex"),
        _hex_ck("truth_tbi_sha256", "ck_l2f_eval_results_truth_tbi_hex"),
        _hex_ck("mutations_vcf_sha256", "ck_l2f_eval_results_mut_vcf_hex"),
        _hex_ck("mutations_tbi_sha256", "ck_l2f_eval_results_mut_tbi_hex"),
        _hex_ck("scoring_contract_hash", "ck_l2f_eval_results_contract_hex"),
        _hex_ck("scoring_py_sha256", "ck_l2f_eval_results_scoring_py_hex"),
        _hex_ck("validator_py_sha256", "ck_l2f_eval_results_validator_py_hex"),
        _hex_ck("metrics_artifact_sha256", "ck_l2f_eval_results_metrics_hex"),
        _hex_ck("evaluation_hash", "ck_l2f_eval_results_evaluation_hash_hex"),
        _in_ck("admission_code", _ADMISSION_CODES, "ck_l2f_eval_results_admission_code"),
        _in_ck("partition", ("train", "validation", "test"), "ck_l2f_eval_results_partition"),
        sa.CheckConstraint(
            "minos_score_100 >= 0 AND minos_score_100 <= 100",
            name="ck_l2f_eval_results_score_100_range",
        ),
        sa.CheckConstraint(
            "minos_score >= 0 AND minos_score <= 1", name="ck_l2f_eval_results_score_range"
        ),
        # the validator's /100 normalization, enforced in the ledger. The tolerance is a float
        # representation allowance only - it can never hide a different score.
        sa.CheckConstraint(
            "abs(minos_score - (minos_score_100 / 100.0)) <= 1e-9",
            name="ck_l2f_eval_results_score_normalization",
        ),
        # admitted is the DERIVED consequence of the code, never an independent claim.
        sa.CheckConstraint(
            "admitted = (admission_code = 'ADMITTED')",
            name="ck_l2f_eval_results_admitted_matches_code",
        ),
        sa.CheckConstraint(
            "core_score >= 0 AND core_score <= 1 "
            "AND completeness_score >= 0 AND completeness_score <= 1 "
            "AND fp_score >= 0 AND fp_score <= 1 "
            "AND quality_score >= 0 AND quality_score <= 1 "
            "AND overcall_penalty >= 0",
            name="ck_l2f_eval_results_components_range",
        ),
        schema=_SCHEMA,
    )
    op.create_index(
        "ix_l2f_eval_results_contract_partition",
        "l2f_evaluation_results",
        ["scoring_contract_hash", "partition"],
        schema=_SCHEMA,
    )
    op.create_index(
        "ix_l2f_eval_results_registry",
        "l2f_evaluation_results",
        ["dataset_registry_id"],
        schema=_SCHEMA,
    )


def _create_failures() -> None:
    op.create_table(
        "l2f_evaluation_failures",
        _uuid_pk(),
        _uuid("execution_result_id"),
        _sha("execution_result_hash"),
        _uuid("dataset_registry_id"),
        _sha("scoring_contract_hash"),
        sa.Column("failure_code", sa.Text(), nullable=False),
        sa.Column("tool_exit_code", sa.Integer(), nullable=True),
        _sha("stderr_sha256", nullable=True),
        _ts(),
        sa.PrimaryKeyConstraint("id", name="pk_l2f_evaluation_failures"),
        sa.ForeignKeyConstraint(
            ["execution_result_id"],
            ["experiments.l2f_execution_results.id"],
            name="fk_l2f_eval_failures_execution",
        ),
        sa.ForeignKeyConstraint(
            ["dataset_registry_id"],
            ["catalog.dataset_registry.id"],
            name="fk_l2f_eval_failures_registry",
        ),
        sa.UniqueConstraint(
            "execution_result_id",
            "scoring_contract_hash",
            name="uq_l2f_eval_failures_execution_contract",
        ),
        _hex_ck("execution_result_hash", "ck_l2f_eval_failures_exec_hash_hex"),
        _hex_ck("scoring_contract_hash", "ck_l2f_eval_failures_contract_hex"),
        _hex_ck("stderr_sha256", "ck_l2f_eval_failures_stderr_hex", nullable=True),
        _in_ck("failure_code", _FAILURE_CODES, "ck_l2f_eval_failures_code"),
        schema=_SCHEMA,
    )


def _create_triggers() -> None:
    """Success and failure are mutually exclusive per (execution, scoring contract).

    The check takes a row lock on the execution result first, so two concurrent transactions
    cannot both observe "no other outcome" and both commit.
    """
    op.execute(
        f"CREATE OR REPLACE FUNCTION {_EXCLUSIVE}() RETURNS trigger LANGUAGE plpgsql AS $excl$ "
        "DECLARE v_other integer; BEGIN "
        "PERFORM 1 FROM experiments.l2f_execution_results r "
        "  WHERE r.id = NEW.execution_result_id FOR SHARE; "
        "IF TG_TABLE_NAME = 'l2f_evaluation_results' THEN "
        f"  SELECT count(*) INTO v_other FROM {_FAILURES} f "
        "    WHERE f.execution_result_id = NEW.execution_result_id "
        "      AND f.scoring_contract_hash = NEW.scoring_contract_hash; "
        "  IF v_other > 0 THEN "
        "    RAISE EXCEPTION 'evaluation % already failed under this scoring contract', "
        "      NEW.execution_result_id USING ERRCODE = "
        f"      '{_SQLSTATE_DUAL_OUTCOME}'; "
        "  END IF; "
        "ELSE "
        f"  SELECT count(*) INTO v_other FROM {_RESULTS} e "
        "    WHERE e.execution_result_id = NEW.execution_result_id "
        "      AND e.scoring_contract_hash = NEW.scoring_contract_hash; "
        "  IF v_other > 0 THEN "
        "    RAISE EXCEPTION 'evaluation % already succeeded under this scoring contract', "
        "      NEW.execution_result_id USING ERRCODE = "
        f"      '{_SQLSTATE_DUAL_OUTCOME}'; "
        "  END IF; "
        "END IF; "
        "RETURN NEW; END; $excl$;"
    )
    for table in ("l2f_evaluation_results", "l2f_evaluation_failures"):
        op.execute(
            f"CREATE TRIGGER trg_{table}_exclusive_outcome "
            f"BEFORE INSERT ON {_SCHEMA}.{table} "
            f"FOR EACH ROW EXECUTE FUNCTION {_EXCLUSIVE}();"
        )
        # both ledgers are fully append-only, reusing the 0001 rejection function.
        op.execute(
            f"CREATE TRIGGER trg_{table}_append_only "
            f"BEFORE UPDATE OR DELETE ON {_SCHEMA}.{table} "
            f"FOR EACH ROW EXECUTE FUNCTION {_REJECT_MUTATION}();"
        )


def _create_projections() -> None:
    """Narrow evaluator-facing projections.

    ``minos_evaluator`` gets SELECT on exactly these, never broad direct SELECT on the L2-F
    experiment ledger: 0008 deliberately grants application roles no direct table privileges
    there, and that boundary is preserved.
    """
    op.execute(
        f"CREATE VIEW {_EXEC_INPUTS} AS "
        "SELECT r.id AS execution_result_id, r.result_hash AS execution_result_hash, "
        "       r.job_key, r.config_hash, r.parameter_space_hash, "
        "       m.dataset_registry_id, m.partition, "
        "       dr.dataset_id, dr.round_id, dr.chromosome, "
        "       dr.region_start0, dr.region_end0_exclusive, dr.reference_sha256, "
        "       r.vcf_artifact_id, r.vcf_sha256, a.uri AS vcf_uri, a.media_type AS vcf_media_type "
        "  FROM experiments.l2f_execution_results r "
        "  JOIN experiments.l2f_experiment_jobs j ON j.id = r.job_id "
        "  JOIN experiments.l2f_experiment_plan_members m ON m.id = j.plan_member_id "
        "  JOIN catalog.dataset_registry dr ON dr.id = m.dataset_registry_id "
        "  JOIN catalog.artifacts a ON a.id = r.vcf_artifact_id;"
    )
    # TRAIN-only registration surface. Validation and test are structurally absent, so the
    # evaluator cannot enumerate them through this interface at all.
    op.execute(
        f"CREATE VIEW {_TRAIN_TARGETS} AS "
        "SELECT dr.id AS dataset_registry_id, dr.dataset_id, dr.round_id, dr.chromosome "
        "  FROM catalog.split_allocations sa "
        "  JOIN catalog.dataset_registry dr ON dr.id = sa.dataset_registry_id "
        " WHERE sa.partition = 'train';"
    )


def _create_functions() -> None:
    """SECURITY DEFINER persistence. Identity is DERIVED here, never accepted from the caller."""
    op.execute(
        f"CREATE OR REPLACE FUNCTION {_REGISTER_TRUTH}("
        "p_dataset_registry_id uuid, p_truth_vcf char(64), p_truth_tbi char(64), "
        "p_mut_vcf char(64), p_mut_tbi char(64)) "
        "RETURNS TABLE(identity_id uuid, created boolean) LANGUAGE plpgsql SECURITY DEFINER "
        "SET search_path = pg_catalog, public AS $reg$ "
        "DECLARE v_partition text; v_id uuid; v_row record; BEGIN "
        # the partition is read from the authoritative split, never supplied by the caller.
        "SELECT sa.partition INTO v_partition FROM catalog.split_allocations sa "
        "  WHERE sa.dataset_registry_id = p_dataset_registry_id; "
        "IF v_partition IS NULL THEN "
        "  RAISE EXCEPTION 'dataset % is not in the accepted split', p_dataset_registry_id "
        f"    USING ERRCODE = '{_SQLSTATE_PARTITION}'; END IF; "
        "IF v_partition <> 'train' THEN "
        "  RAISE EXCEPTION 'L2-F2-A registers TRAIN truth only; dataset % is %', "
        "    p_dataset_registry_id, v_partition "
        f"    USING ERRCODE = '{_SQLSTATE_PARTITION}'; END IF; "
        "SELECT * INTO v_row FROM evaluation.dataset_evaluation_identity d "
        "  WHERE d.dataset_registry_id = p_dataset_registry_id; "
        "IF FOUND THEN "
        "  IF v_row.truth_vcf_sha256 IS DISTINCT FROM p_truth_vcf "
        "     OR v_row.truth_tbi_sha256 IS DISTINCT FROM p_truth_tbi "
        "     OR v_row.mutations_vcf_sha256 IS DISTINCT FROM p_mut_vcf "
        "     OR v_row.mutations_tbi_sha256 IS DISTINCT FROM p_mut_tbi THEN "
        "    RAISE EXCEPTION 'truth identity for dataset % already registered with different "
        "bytes', p_dataset_registry_id "
        f"      USING ERRCODE = '{_SQLSTATE_CONFLICT}'; END IF; "
        "  RETURN QUERY SELECT v_row.id, false; RETURN; END IF; "
        "INSERT INTO evaluation.dataset_evaluation_identity "
        "  (dataset_registry_id, truth_vcf_sha256, truth_tbi_sha256, "
        "   mutations_vcf_sha256, mutations_tbi_sha256) "
        "VALUES (p_dataset_registry_id, p_truth_vcf, p_truth_tbi, p_mut_vcf, p_mut_tbi) "
        "RETURNING id INTO v_id; "
        "RETURN QUERY SELECT v_id, true; END; $reg$;"
    )

    op.execute(
        f"CREATE OR REPLACE FUNCTION {_RECORD_RESULT}("
        "p_execution_result_id uuid, p_scoring_contract_hash char(64), "
        "p_scorer_upstream_commit text, p_scoring_py char(64), p_validator_py char(64), "
        "p_happy_image text, p_bcftools_image text, p_metrics_artifact_id uuid, "
        "p_metrics_sha char(64), p_metrics_media_type text, "
        "p_core double precision, p_completeness double precision, p_fp double precision, "
        "p_quality double precision, p_overcall double precision, "
        "p_score_100 double precision, p_score double precision, "
        "p_admission_code text, p_evaluation_hash char(64)) "
        "RETURNS TABLE(evaluation_id uuid, created boolean) LANGUAGE plpgsql SECURITY DEFINER "
        "SET search_path = pg_catalog, public AS $rec$ "
        "DECLARE v_exec record; v_ident record; v_row record; v_id uuid; BEGIN "
        # dataset + result hash are DERIVED from the execution's own lineage.
        "SELECT r.result_hash, m.dataset_registry_id, m.partition "
        "  INTO v_exec "
        "  FROM experiments.l2f_execution_results r "
        "  JOIN experiments.l2f_experiment_jobs j ON j.id = r.job_id "
        "  JOIN experiments.l2f_experiment_plan_members m ON m.id = j.plan_member_id "
        " WHERE r.id = p_execution_result_id; "
        "IF NOT FOUND THEN "
        "  RAISE EXCEPTION 'execution result % does not exist', p_execution_result_id "
        f"    USING ERRCODE = '{_SQLSTATE_PARTITION}'; END IF; "
        # truth identity is READ from the authoritative row for that dataset.
        "SELECT * INTO v_ident FROM evaluation.dataset_evaluation_identity d "
        "  WHERE d.dataset_registry_id = v_exec.dataset_registry_id; "
        "IF NOT FOUND THEN "
        "  RAISE EXCEPTION 'no registered truth identity for dataset %', "
        "    v_exec.dataset_registry_id "
        f"    USING ERRCODE = '{_SQLSTATE_PARTITION}'; END IF; "
        "SELECT * INTO v_row FROM evaluation.l2f_evaluation_results e "
        "  WHERE e.execution_result_id = p_execution_result_id "
        "    AND e.scoring_contract_hash = p_scoring_contract_hash; "
        "IF FOUND THEN "
        "  IF v_row.evaluation_hash IS DISTINCT FROM p_evaluation_hash THEN "
        "    RAISE EXCEPTION 'evaluation % already exists with a different identity', "
        "      p_execution_result_id "
        f"      USING ERRCODE = '{_SQLSTATE_CONFLICT}'; END IF; "
        "  RETURN QUERY SELECT v_row.id, false; RETURN; END IF; "
        "INSERT INTO evaluation.l2f_evaluation_results ("
        "  execution_result_id, execution_result_hash, dataset_registry_id, partition, "
        "  dataset_evaluation_identity_id, truth_vcf_sha256, truth_tbi_sha256, "
        "  mutations_vcf_sha256, mutations_tbi_sha256, scoring_contract_hash, "
        "  scorer_upstream_commit, scoring_py_sha256, validator_py_sha256, happy_image_digest, "
        "  bcftools_image_digest, metrics_artifact_id, metrics_artifact_sha256, "
        "  metrics_media_type, core_score, completeness_score, fp_score, quality_score, "
        "  overcall_penalty, minos_score_100, minos_score, admitted, admission_code, "
        "  evaluation_hash) "
        "VALUES (p_execution_result_id, v_exec.result_hash, v_exec.dataset_registry_id, "
        "  v_exec.partition, v_ident.id, v_ident.truth_vcf_sha256, v_ident.truth_tbi_sha256, "
        "  v_ident.mutations_vcf_sha256, v_ident.mutations_tbi_sha256, p_scoring_contract_hash, "
        "  p_scorer_upstream_commit, p_scoring_py, p_validator_py, p_happy_image, "
        "  p_bcftools_image, p_metrics_artifact_id, p_metrics_sha, p_metrics_media_type, "
        "  p_core, p_completeness, p_fp, p_quality, p_overcall, p_score_100, p_score, "
        "  (p_admission_code = 'ADMITTED'), p_admission_code, p_evaluation_hash) "
        "RETURNING id INTO v_id; "
        "RETURN QUERY SELECT v_id, true; END; $rec$;"
    )

    op.execute(
        f"CREATE OR REPLACE FUNCTION {_RECORD_FAILURE}("
        "p_execution_result_id uuid, p_scoring_contract_hash char(64), p_failure_code text, "
        "p_tool_exit_code integer, p_stderr_sha char(64)) "
        "RETURNS TABLE(failure_id uuid, created boolean) LANGUAGE plpgsql SECURITY DEFINER "
        "SET search_path = pg_catalog, public AS $fail$ "
        "DECLARE v_exec record; v_row record; v_id uuid; BEGIN "
        "SELECT r.result_hash, m.dataset_registry_id INTO v_exec "
        "  FROM experiments.l2f_execution_results r "
        "  JOIN experiments.l2f_experiment_jobs j ON j.id = r.job_id "
        "  JOIN experiments.l2f_experiment_plan_members m ON m.id = j.plan_member_id "
        " WHERE r.id = p_execution_result_id; "
        "IF NOT FOUND THEN "
        "  RAISE EXCEPTION 'execution result % does not exist', p_execution_result_id "
        f"    USING ERRCODE = '{_SQLSTATE_PARTITION}'; END IF; "
        "SELECT * INTO v_row FROM evaluation.l2f_evaluation_failures f "
        "  WHERE f.execution_result_id = p_execution_result_id "
        "    AND f.scoring_contract_hash = p_scoring_contract_hash; "
        "IF FOUND THEN "
        "  IF v_row.failure_code IS DISTINCT FROM p_failure_code "
        "     OR v_row.tool_exit_code IS DISTINCT FROM p_tool_exit_code "
        "     OR v_row.stderr_sha256 IS DISTINCT FROM p_stderr_sha THEN "
        "    RAISE EXCEPTION 'evaluation failure % already recorded differently', "
        "      p_execution_result_id "
        f"      USING ERRCODE = '{_SQLSTATE_CONFLICT}'; END IF; "
        "  RETURN QUERY SELECT v_row.id, false; RETURN; END IF; "
        "INSERT INTO evaluation.l2f_evaluation_failures ("
        "  execution_result_id, execution_result_hash, dataset_registry_id, "
        "  scoring_contract_hash, failure_code, tool_exit_code, stderr_sha256) "
        "VALUES (p_execution_result_id, v_exec.result_hash, v_exec.dataset_registry_id, "
        "  p_scoring_contract_hash, p_failure_code, p_tool_exit_code, p_stderr_sha) "
        "RETURNING id INTO v_id; "
        "RETURN QUERY SELECT v_id, true; END; $fail$;"
    )


def _apply_grants() -> None:
    """REVOKE PUBLIC everywhere, then grant only what the evaluator genuinely needs."""
    for obj in (_RESULTS, _FAILURES, _EXEC_INPUTS, _TRAIN_TARGETS):
        op.execute(f"REVOKE ALL ON {obj} FROM PUBLIC;")
        for role in _DENIED_ROLES:
            op.execute(f"REVOKE ALL ON {obj} FROM {role};")

    # narrow read surface: projections only, never the L2-F experiment tables directly.
    op.execute(f"GRANT SELECT ON {_EXEC_INPUTS} TO minos_evaluator;")
    op.execute(f"GRANT SELECT ON {_TRAIN_TARGETS} TO minos_evaluator;")
    op.execute(f"GRANT SELECT ON {_RESULTS} TO minos_evaluator;")
    op.execute(f"GRANT SELECT ON {_FAILURES} TO minos_evaluator;")
    op.execute("GRANT SELECT ON evaluation.dataset_evaluation_identity TO minos_evaluator;")
    op.execute("GRANT USAGE ON SCHEMA evaluation TO minos_evaluator;")

    signatures = (
        f"{_REGISTER_TRUTH}(uuid, char, char, char, char)",
        f"{_RECORD_RESULT}(uuid, char, text, char, char, text, text, uuid, char, text, "
        "double precision, double precision, double precision, double precision, "
        "double precision, double precision, double precision, text, char)",
        f"{_RECORD_FAILURE}(uuid, char, text, integer, char)",
    )
    for sig in signatures:
        op.execute(f"REVOKE ALL ON FUNCTION {sig} FROM PUBLIC;")
        for role in _DENIED_ROLES:
            op.execute(f"REVOKE ALL ON FUNCTION {sig} FROM {role};")
        op.execute(f"GRANT EXECUTE ON FUNCTION {sig} TO minos_evaluator;")
        op.execute(f"GRANT EXECUTE ON FUNCTION {sig} TO minos_admin;")


def upgrade() -> None:
    _create_composite_targets()
    _create_results()
    _create_failures()
    _create_triggers()
    _create_projections()
    _create_functions()
    _apply_grants()


def downgrade() -> None:
    """Remove EVERY 0009-owned object and leave the 0008 inventory exactly as it was."""
    op.execute(f"DROP FUNCTION IF EXISTS {_RECORD_FAILURE}(uuid, char, text, integer, char);")
    op.execute(
        f"DROP FUNCTION IF EXISTS {_RECORD_RESULT}(uuid, char, text, char, char, text, text, "
        "uuid, char, text, double precision, double precision, double precision, "
        "double precision, double precision, double precision, double precision, text, char);"
    )
    op.execute(f"DROP FUNCTION IF EXISTS {_REGISTER_TRUTH}(uuid, char, char, char, char);")
    op.execute(f"DROP VIEW IF EXISTS {_TRAIN_TARGETS};")
    op.execute(f"DROP VIEW IF EXISTS {_EXEC_INPUTS};")
    for table in ("l2f_evaluation_results", "l2f_evaluation_failures"):
        op.execute(f"DROP TRIGGER IF EXISTS trg_{table}_append_only ON {_SCHEMA}.{table};")
        op.execute(f"DROP TRIGGER IF EXISTS trg_{table}_exclusive_outcome ON {_SCHEMA}.{table};")
    op.execute(f"DROP FUNCTION IF EXISTS {_EXCLUSIVE}();")
    op.drop_table("l2f_evaluation_failures", schema=_SCHEMA)
    op.drop_index(
        "ix_l2f_eval_results_registry", table_name="l2f_evaluation_results", schema=_SCHEMA
    )
    op.drop_index(
        "ix_l2f_eval_results_contract_partition",
        table_name="l2f_evaluation_results",
        schema=_SCHEMA,
    )
    op.drop_table("l2f_evaluation_results", schema=_SCHEMA)
    _drop_composite_targets()
