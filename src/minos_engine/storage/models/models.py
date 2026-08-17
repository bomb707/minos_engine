"""``models`` schema — model-bundle identity + future binding columns (L2-B).

L2-B stores identity and artifact references only; no model is trained or
registered. Feature-schema / split-manifest / training-provenance columns are
present (nullable) so later stages can bind them.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import ForeignKey, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from ..metadata import Base, created_at_col, hex64_check, sha256_col, uuid_pk

__all__ = ["ModelBundle"]

_SCHEMA = "models"


class ModelBundle(Base):
    """Append-only model-bundle identity referencing a catalog artifact."""

    __tablename__ = "model_bundles"
    __table_args__ = (
        UniqueConstraint("bundle_key", name="uq_model_bundles_bundle_key"),
        hex64_check("feature_schema_hash", "feature_schema_hash_hex"),
        hex64_check("split_manifest_hash", "split_manifest_hash_hex"),
        hex64_check("training_provenance_hash", "training_provenance_hash_hex"),
        hex64_check("registry_hash", "registry_hash_hex"),
        {"schema": _SCHEMA},
    )

    id: Mapped[str] = uuid_pk()
    bundle_key: Mapped[str] = mapped_column(Text, nullable=False)
    artifact_id: Mapped[str] = mapped_column(
        ForeignKey("catalog.artifacts.id", name="fk_model_bundles_artifact_id_artifacts"),
        nullable=False,
    )
    feature_schema_hash: Mapped[str | None] = sha256_col(nullable=True)
    split_manifest_hash: Mapped[str | None] = sha256_col(nullable=True)
    training_provenance_hash: Mapped[str | None] = sha256_col(nullable=True)
    registry_hash: Mapped[str | None] = sha256_col(nullable=True)
    created_at: Mapped[datetime] = created_at_col()
