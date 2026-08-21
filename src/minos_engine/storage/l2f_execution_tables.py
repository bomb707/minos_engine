"""Private SQLAlchemy Core mappings for the L2-F F5 execution-outcome tables (0008).

Migration ``0008_l2f_execution_results`` is the AUTHORITATIVE DDL. These Core ``Table`` objects
mirror it 1:1 on a DEDICATED private ``l2f_execution_metadata`` — they are **never** added to the
L2-B declarative ``Base.metadata`` (that would silently change the accepted DB-READY storage
fingerprint). ``create_all`` / ``drop_all`` are **never** called on a production or operational
path; ``create_all`` is invoked only by the isolated mapping-parity scratch test.
"""

from __future__ import annotations

from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from minos_engine.storage.l2f_execution_contract import (
    L2F_EXECUTION_FAILURE_CODES,
    L2F_RESULT_MANIFEST_MEDIA_TYPE,
    L2F_VCF_MEDIA_TYPE,
)

__all__ = [
    "l2f_execution_metadata",
    "L2F_EXECUTION_OWNED_TABLES",
    "l2f_execution_results",
    "l2f_execution_failures",
]

l2f_execution_metadata = sa.MetaData()
_HEX = "^[0-9a-f]{64}$"


def _uuid_pk() -> sa.Column[Any]:
    return sa.Column(
        "id",
        postgresql.UUID(as_uuid=False),
        nullable=False,
        server_default=sa.text("gen_random_uuid()"),
    )


def _uuid(name: str, *, nullable: bool = False) -> sa.Column[Any]:
    return sa.Column(name, postgresql.UUID(as_uuid=False), nullable=nullable)


def _sha(name: str, *, nullable: bool = False) -> sa.Column[Any]:
    return sa.Column(name, sa.CHAR(64), nullable=nullable)


def _ts() -> sa.Column[Any]:
    return sa.Column(
        "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")
    )


def _hex_ck(col: str, name: str, *, nullable: bool = False) -> sa.CheckConstraint:
    expr = f"{col} ~ '{_HEX}'" if not nullable else f"{col} IS NULL OR {col} ~ '{_HEX}'"
    return sa.CheckConstraint(expr, name=name)


l2f_execution_results = sa.Table(
    "l2f_execution_results",
    l2f_execution_metadata,
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
    sa.Column("vcf_media_type", sa.Text(), nullable=False, server_default=L2F_VCF_MEDIA_TYPE),
    _uuid("result_manifest_artifact_id"),
    _sha("result_manifest_sha256"),
    sa.Column(
        "result_manifest_media_type",
        sa.Text(),
        nullable=False,
        server_default=L2F_RESULT_MANIFEST_MEDIA_TYPE,
    ),
    _sha("result_hash"),
    sa.Column("runtime_ms", sa.BigInteger(), nullable=False),
    _ts(),
    sa.PrimaryKeyConstraint("id", name="pk_l2f_execution_results"),
    sa.UniqueConstraint("job_id", name="uq_l2f_exec_results_job"),
    sa.UniqueConstraint("job_key", name="uq_l2f_exec_results_job_key"),
    sa.UniqueConstraint("result_hash", name="uq_l2f_exec_results_result_hash"),
    sa.CheckConstraint(
        f"vcf_media_type = '{L2F_VCF_MEDIA_TYPE}'", name="ck_l2f_exec_results_vcf_media"
    ),
    sa.CheckConstraint(
        f"result_manifest_media_type = '{L2F_RESULT_MANIFEST_MEDIA_TYPE}'",
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
    sa.Index("ix_l2f_exec_results_plan_id", "plan_id"),
    schema="experiments",
)

l2f_execution_failures = sa.Table(
    "l2f_execution_failures",
    l2f_execution_metadata,
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
    sa.CheckConstraint(
        "failure_code IN (" + ", ".join(f"'{c}'" for c in L2F_EXECUTION_FAILURE_CODES) + ")",
        name="ck_l2f_exec_failures_code_bounded",
    ),
    sa.CheckConstraint("length(worker_id) > 0", name="ck_l2f_exec_failures_worker_nonempty"),
    _hex_ck("job_key", "ck_l2f_exec_failures_job_key_hex"),
    _hex_ck("stderr_sha256", "ck_l2f_exec_failures_stderr_hex", nullable=True),
    sa.Index("ix_l2f_exec_failures_plan_id", "plan_id"),
    schema="experiments",
)

#: the two F5-owned tables (external composite-FK targets are the migration's responsibility).
L2F_EXECUTION_OWNED_TABLES: tuple[str, ...] = (
    "l2f_execution_results",
    "l2f_execution_failures",
)
