"""Append-only helpers + the worker-claim primitive (L2-B).

``claim_next_job`` realizes the ``SELECT ... FOR UPDATE SKIP LOCKED`` contract inside
the caller's transaction: a claimed row is locked and skipped by other concurrent
sessions, so no job is claimed twice; an uncommitted claim is released on rollback,
making the job available again. This is storage concurrency behavior only — it does
not start the experiment harness.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models.experiments import PENDING, Job

__all__ = ["claim_next_job"]

_CLAIMED = "CLAIMED"


def claim_next_job(session: Session, worker_id: str) -> Job | None:
    """Atomically claim the oldest PENDING job for ``worker_id`` (or None if none free).

    Must run inside an open transaction; the caller commits to keep the claim or
    rolls back to release it. Uses ``FOR UPDATE SKIP LOCKED`` so concurrent workers
    never contend for or duplicate a claim.
    """
    stmt = (
        select(Job)
        .where(Job.status == PENDING)
        .order_by(Job.created_at, Job.id)
        .with_for_update(skip_locked=True)
        .limit(1)
    )
    job = session.scalars(stmt).first()
    if job is None:
        return None
    job.status = _CLAIMED
    job.claimed_by = worker_id
    job.claimed_at = datetime.now(UTC)
    job.updated_at = datetime.now(UTC)
    session.flush()
    return job
