"""Parse and hash the raw network-configuration payload.

The raw network config is preserved verbatim in the snapshot
(``network_config_raw``); this module derives a small typed view and a stable
content hash for provenance.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict

from minos_engine.common.errors import ProtocolError
from minos_engine.common.hashing import canonical_hash

__all__ = ["NetworkConfig", "parse_network_config"]


class NetworkConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    network: str | None = None
    api_base_url: str | None = None
    raw_hash: str


def parse_network_config(raw: dict[str, Any]) -> NetworkConfig:
    if not isinstance(raw, dict):
        raise ProtocolError("network_config payload must be a mapping")
    return NetworkConfig(
        network=raw.get("network"),
        api_base_url=raw.get("api_base_url"),
        raw_hash=canonical_hash(raw),
    )
