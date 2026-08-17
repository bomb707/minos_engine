"""Build :class:`ArtifactIdentity` records with an explicit verification strength.

A BAI mtime is never sufficient identity (Layer 1 spec §6). Stage 0 does not open
genomic files; it constructs identities from already-known content hashes (e.g.
values supplied by the protocol snapshot) and records how strongly the identity
was verified.
"""

from __future__ import annotations

from enum import Enum

from minos_engine.common.errors import ContractValidationError

from .contracts import ArtifactIdentity

__all__ = ["VerificationStrength", "build_artifact_identity"]


class VerificationStrength(str, Enum):
    """How strongly an artifact identity was established."""

    DECLARED = "declared"  # hash supplied by an authority (e.g. protocol snapshot)
    CONTENT_HASHED = "content_hashed"  # engine streamed and hashed the bytes (later stages)
    UNVERIFIED = "unverified"  # only a filename/URI is known — NOT a valid identity


def build_artifact_identity(
    *,
    uri: str,
    sha256: str,
    size_bytes: int,
    media_type: str,
    observed_at: str,
    strength: VerificationStrength = VerificationStrength.DECLARED,
) -> ArtifactIdentity:
    """Construct an :class:`ArtifactIdentity`, rejecting unverifiable inputs.

    ``UNVERIFIED`` strength is rejected: a filename alone is not an identity.
    """
    if strength is VerificationStrength.UNVERIFIED:
        raise ContractValidationError(
            "cannot build an ArtifactIdentity with UNVERIFIED strength: a filename "
            "alone is never an identity; a content sha256 is required"
        )
    return ArtifactIdentity(
        uri=uri,
        sha256=sha256,
        size_bytes=size_bytes,
        media_type=media_type,
        created_at_or_observed_at=observed_at,
    )
