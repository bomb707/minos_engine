"""``profiling`` schema — Layer 1 profile identity (append-only; no payloads) (L2-B)."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import CheckConstraint, ForeignKey, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from ..metadata import Base, created_at_col, hex64_check, sha256_col, uuid_pk

__all__ = ["Profile"]

_SCHEMA = "profiling"


class Profile(Base):
    """A Layer 1 profile referenced by identity + all mandatory input hashes.

    L2-B stores identity only (no profile payload ingestion). The complete input
    identity tuple is unique; every hash column enforces the lowercase-hex shape.
    """

    __tablename__ = "profiles"
    __table_args__ = (
        UniqueConstraint("profile_id", name="uq_profiles_profile_id"),
        UniqueConstraint("identity_tuple_hash", name="uq_profiles_identity_tuple_hash"),
        UniqueConstraint(
            "bam_sha256",
            "bai_sha256",
            "reference_sha256",
            "fai_sha256",
            "region_hash",
            name="uq_profiles_identity_tuple",
        ),
        CheckConstraint("length(profile_id) > 0", name="profile_id_nonempty"),
        hex64_check("bam_sha256", "bam_sha256_hex"),
        hex64_check("bai_sha256", "bai_sha256_hex"),
        hex64_check("reference_sha256", "reference_sha256_hex"),
        hex64_check("fai_sha256", "fai_sha256_hex"),
        hex64_check("region_hash", "region_hash_hex"),
        hex64_check("profile_manifest_hash", "profile_manifest_hash_hex"),
        hex64_check("fingerprint_hash", "fingerprint_hash_hex"),
        hex64_check("identity_tuple_hash", "identity_tuple_hash_hex"),
        {"schema": _SCHEMA},
    )

    id: Mapped[str] = uuid_pk()
    profile_id: Mapped[str] = mapped_column(Text, nullable=False)
    dataset_id: Mapped[str | None] = mapped_column(
        ForeignKey("catalog.datasets.id", name="fk_profiles_dataset_id_datasets"), nullable=True
    )
    bam_sha256: Mapped[str] = sha256_col()
    bai_sha256: Mapped[str] = sha256_col()
    reference_sha256: Mapped[str] = sha256_col()
    fai_sha256: Mapped[str] = sha256_col()
    region_hash: Mapped[str] = sha256_col()
    profile_manifest_hash: Mapped[str] = sha256_col()
    fingerprint_hash: Mapped[str] = sha256_col()
    identity_tuple_hash: Mapped[str] = sha256_col()
    created_at: Mapped[datetime] = created_at_col()
