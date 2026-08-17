"""``runtime`` schema — decision identity/manifest shell (append-only) (L2-B).

L2-B provides the decision identity table only; no live decision is emitted and
``Layer2Service.select_config`` remains blocked.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import CheckConstraint, ForeignKey, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from ..metadata import Base, created_at_col, hex64_check, sha256_col, uuid_pk

__all__ = ["Decision"]

_SCHEMA = "runtime"


class Decision(Base):
    """Append-only decision identity. Unique per (round_id, decision_hash)."""

    __tablename__ = "decisions"
    __table_args__ = (
        UniqueConstraint("round_id", "decision_hash", name="uq_decisions_round_id_decision_hash"),
        CheckConstraint("length(round_id) > 0", name="round_id_nonempty"),
        hex64_check("decision_hash", "decision_hash_hex"),
        hex64_check("decision_manifest_hash", "decision_manifest_hash_hex"),
        {"schema": _SCHEMA},
    )

    id: Mapped[str] = uuid_pk()
    round_id: Mapped[str] = mapped_column(Text, nullable=False)
    decision_hash: Mapped[str] = sha256_col()
    decision_manifest_hash: Mapped[str] = sha256_col()
    config_id: Mapped[str | None] = mapped_column(
        ForeignKey("catalog.gatk_configs.id", name="fk_decisions_config_id_gatk_configs"),
        nullable=True,
    )
    model_bundle_id: Mapped[str | None] = mapped_column(
        ForeignKey("models.model_bundles.id", name="fk_decisions_model_bundle_id_model_bundles"),
        nullable=True,
    )
    profile_id: Mapped[str | None] = mapped_column(
        ForeignKey("profiling.profiles.id", name="fk_decisions_profile_id_profiles"), nullable=True
    )
    created_at: Mapped[datetime] = created_at_col()
