"""Artifact repository (L2-B) — URI + SHA-256 identity, never binary payloads.

Repository methods never commit; the caller owns the transaction boundary (see
``session_scope``). ``flush`` is used so database constraints (unique sha256, hex
shape, non-empty URI, non-negative size) surface deterministically.
"""

from __future__ import annotations

import re

from sqlalchemy import select
from sqlalchemy.orm import Session

from minos_engine.common.errors import ContractValidationError

from ..models.catalog import Artifact

__all__ = ["ArtifactRepository"]

_HEX64 = re.compile(r"^[0-9a-f]{64}$")


class ArtifactRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def add(
        self,
        *,
        uri: str,
        sha256: str,
        media_type: str | None = None,
        size_bytes: int | None = None,
        provenance: str | None = None,
    ) -> Artifact:
        if not uri or not uri.strip():
            raise ContractValidationError("artifact uri must be non-empty")
        if not _HEX64.match(sha256):
            raise ContractValidationError("artifact sha256 must be 64 lowercase hex characters")
        if size_bytes is not None and size_bytes < 0:
            raise ContractValidationError("artifact size_bytes must be non-negative")
        artifact = Artifact(
            uri=uri,
            sha256=sha256,
            media_type=media_type,
            size_bytes=size_bytes,
            provenance=provenance,
        )
        self._session.add(artifact)
        self._session.flush()
        return artifact

    def get_by_sha256(self, sha256: str) -> Artifact | None:
        return self._session.scalars(select(Artifact).where(Artifact.sha256 == sha256)).first()
