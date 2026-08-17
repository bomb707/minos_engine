"""``experiments`` schema — job/result identity + worker-claim state (L2-B).

The job table carries the state, timestamps, and index required by the future
``SELECT ... FOR UPDATE SKIP LOCKED`` claim protocol. L2-B provides the storage
contract only — it does not run experiments.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import CheckConstraint, ForeignKey, Index, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from ..metadata import Base, created_at_col, hex64_check, sha256_col, uuid_pk

__all__ = ["Job", "Result", "JOB_STATUSES", "PENDING"]

_SCHEMA = "experiments"

PENDING = "PENDING"
JOB_STATUSES: tuple[str, ...] = (
    PENDING,
    "CLAIMED",
    "RUNNING",
    "SUCCEEDED",
    "FAILED",
    "CANCELLED",
)
_STATUS_LIST = ", ".join(f"'{s}'" for s in JOB_STATUSES)


class Job(Base):
    """An experiment job. Identity columns are immutable; status/claim state changes."""

    __tablename__ = "jobs"
    __table_args__ = (
        UniqueConstraint("job_key", name="uq_jobs_job_key"),
        CheckConstraint("length(job_key) > 0", name="job_key_nonempty"),
        CheckConstraint(f"status IN ({_STATUS_LIST})", name="status_valid"),
        Index("ix_jobs_status_created_at", "status", "created_at"),
        {"schema": _SCHEMA},
    )

    id: Mapped[str] = uuid_pk()
    job_key: Mapped[str] = mapped_column(Text, nullable=False)
    profile_id: Mapped[str] = mapped_column(
        ForeignKey("profiling.profiles.id", name="fk_jobs_profile_id_profiles"), nullable=False
    )
    config_id: Mapped[str] = mapped_column(
        ForeignKey("catalog.gatk_configs.id", name="fk_jobs_config_id_gatk_configs"),
        nullable=False,
    )
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=PENDING)
    claimed_by: Mapped[str | None] = mapped_column(Text, nullable=True)
    claimed_at: Mapped[datetime | None] = mapped_column(nullable=True)
    created_at: Mapped[datetime] = created_at_col()
    updated_at: Mapped[datetime] = created_at_col()


class Result(Base):
    """Append-only experiment-result identity shell (one per job)."""

    __tablename__ = "results"
    __table_args__ = (
        UniqueConstraint("job_id", name="uq_results_job_id"),
        UniqueConstraint("result_hash", name="uq_results_result_hash"),
        hex64_check("result_hash", "result_hash_hex"),
        {"schema": _SCHEMA},
    )

    id: Mapped[str] = uuid_pk()
    job_id: Mapped[str] = mapped_column(
        ForeignKey("experiments.jobs.id", name="fk_results_job_id_jobs"), nullable=False
    )
    result_hash: Mapped[str] = sha256_col()
    created_at: Mapped[datetime] = created_at_col()
