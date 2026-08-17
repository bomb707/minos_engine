"""``catalog`` schema — frozen artifact / GATK config / dataset identities (L2-B)."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import CheckConstraint, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from ..metadata import Base, created_at_col, hex64_check, sha256_col, uuid_pk

__all__ = ["Artifact", "GatkConfig", "Dataset"]

_SCHEMA = "catalog"


class Artifact(Base):
    """A stored artifact referenced by URI + content SHA-256 (never bytes/BLOBs)."""

    __tablename__ = "artifacts"
    __table_args__ = (
        UniqueConstraint("sha256", name="uq_artifacts_sha256"),
        hex64_check("sha256", "sha256_hex"),
        CheckConstraint("length(uri) > 0", name="uri_nonempty"),
        CheckConstraint("size_bytes IS NULL OR size_bytes >= 0", name="size_nonneg"),
        {"schema": _SCHEMA},
    )

    id: Mapped[str] = uuid_pk()
    uri: Mapped[str] = mapped_column(Text, nullable=False)
    sha256: Mapped[str] = sha256_col()
    media_type: Mapped[str | None] = mapped_column(Text, nullable=True)
    size_bytes: Mapped[int | None] = mapped_column(nullable=True)
    provenance: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = created_at_col()


class GatkConfig(Base):
    """A canonicalized GATK CONFIG identity (URI/bytes live as a catalog artifact)."""

    __tablename__ = "gatk_configs"
    __table_args__ = (
        UniqueConstraint("config_hash", name="uq_gatk_configs_config_hash"),
        hex64_check("config_hash", "config_hash_hex"),
        hex64_check("parameter_space_hash", "parameter_space_hash_hex"),
        {"schema": _SCHEMA},
    )

    id: Mapped[str] = uuid_pk()
    config_hash: Mapped[str] = sha256_col()
    parameter_space_hash: Mapped[str] = sha256_col()
    created_at: Mapped[datetime] = created_at_col()


class Dataset(Base):
    """Dataset identity shell — the future registry contract.

    L2-B defines identity only. The 75 practice samples are NOT inserted here and
    there is intentionally no partition column: train/validation/test allocation is
    a later stage (L2-C) and must not be modelled or populated now.
    """

    __tablename__ = "datasets"
    __table_args__ = (
        UniqueConstraint("dataset_id", name="uq_datasets_dataset_id"),
        CheckConstraint("length(dataset_id) > 0", name="dataset_id_nonempty"),
        {"schema": _SCHEMA},
    )

    id: Mapped[str] = uuid_pk()
    dataset_id: Mapped[str] = mapped_column(Text, nullable=False)
    chromosome: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = created_at_col()
