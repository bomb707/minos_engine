"""``audit`` schema — append-only audit event log (L2-B)."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import CheckConstraint, Text
from sqlalchemy.orm import Mapped, mapped_column

from ..metadata import Base, created_at_col, hex64_check, sha256_col, uuid_pk

__all__ = ["AuditEvent"]

_SCHEMA = "audit"


class AuditEvent(Base):
    """An append-only audit event: actor role, action, object identity, payload hash."""

    __tablename__ = "events"
    __table_args__ = (
        CheckConstraint("length(actor_role) > 0", name="actor_role_nonempty"),
        CheckConstraint("length(action) > 0", name="action_nonempty"),
        hex64_check("payload_hash", "payload_hash_hex"),
        {"schema": _SCHEMA},
    )

    id: Mapped[str] = uuid_pk()
    actor_role: Mapped[str] = mapped_column(Text, nullable=False)
    action: Mapped[str] = mapped_column(Text, nullable=False)
    object_kind: Mapped[str | None] = mapped_column(Text, nullable=True)
    object_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    payload_hash: Mapped[str] = sha256_col()
    created_at: Mapped[datetime] = created_at_col()
