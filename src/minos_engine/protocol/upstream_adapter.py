"""Adapter for pinned upstream Minos identities.

Dynamic Minos state (upstream commit, scorer hash, image digests, reference
hash) is versioned runtime data, never hard-coded truth (assignment §2.4). This
adapter takes a raw provenance mapping and yields typed identities, each with an
explicit :class:`IdentityStatus`. An absent required identity is reported as
``UNAVAILABLE`` (fail closed) rather than invented.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict

from minos_engine.common.versions import IdentityStatus

__all__ = ["UpstreamIdentity", "REQUIRED_PROVENANCE_KEYS", "extract_provenance"]

REQUIRED_PROVENANCE_KEYS = (
    "minos_upstream_commit",
    "scorer_hash",
    "gatk_image_digest",
    "happy_image_digest",
    "reference_sha256",
)


class UpstreamIdentity(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    status: IdentityStatus
    value: str | None = None


def extract_provenance(raw: dict[str, Any]) -> dict[str, UpstreamIdentity]:
    """Return a typed identity per required provenance key.

    A missing or empty value becomes ``IdentityStatus.UNAVAILABLE`` with no
    fabricated value.
    """
    out: dict[str, UpstreamIdentity] = {}
    for key in REQUIRED_PROVENANCE_KEYS:
        value = raw.get(key)
        if isinstance(value, str) and value.strip():
            out[key] = UpstreamIdentity(name=key, status=IdentityStatus.AVAILABLE, value=value.strip())
        else:
            out[key] = UpstreamIdentity(name=key, status=IdentityStatus.UNAVAILABLE, value=None)
    return out
