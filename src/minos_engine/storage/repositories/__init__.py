"""Storage repositories (L2-B)."""

from __future__ import annotations

from .append_only import claim_next_job
from .artifacts import ArtifactRepository

__all__ = ["ArtifactRepository", "claim_next_job"]
