"""``evaluation`` schema — isolated offline evaluation evidence (L2-B).

This schema is inaccessible to ``minos_live`` (no USAGE grant). It holds identity
shells only; L2-B loads no truth, mutations, or scores.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import ForeignKey, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from ..metadata import Base, created_at_col, hex64_check, sha256_col, uuid_pk

__all__ = ["Evaluation"]

_SCHEMA = "evaluation"


class Evaluation(Base):
    """Append-only offline evaluation evidence shell (evaluator-only)."""

    __tablename__ = "evaluations"
    __table_args__ = (
        UniqueConstraint("evaluation_hash", name="uq_evaluations_evaluation_hash"),
        hex64_check("evaluation_hash", "evaluation_hash_hex"),
        {"schema": _SCHEMA},
    )

    id: Mapped[str] = uuid_pk()
    experiment_result_id: Mapped[str] = mapped_column(
        ForeignKey("experiments.results.id", name="fk_evaluations_experiment_result_id_results"),
        nullable=False,
    )
    evaluation_hash: Mapped[str] = sha256_col()
    created_at: Mapped[datetime] = created_at_col()
