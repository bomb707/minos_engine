"""Protocol client interface and implementations.

``ProtocolClient`` is the seam between the engine and the Minos platform. Two
implementations ship in Stage 0:

  * :class:`FixtureProtocolClient` — deterministic, reads a saved API fixture.
    All tests use this; no test depends on the live API.
  * :class:`LiveProtocolClient` — the production seam. Because the live endpoint
    schema is not authoritatively available in Stage 0, it raises a typed
    :class:`UnavailableError` rather than fabricating a response.

Raw and parsed forms are stored separately: the client returns a
:class:`RawProtocolResponse` (verbatim payload) and the builder produces the
immutable parsed :class:`RoundProtocolSnapshot`.
"""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from minos_engine.common.errors import ProtocolError, UnavailableError

from .contracts import RoundContext, RoundProtocolSnapshot
from .snapshot import build_round_context, build_snapshot

__all__ = [
    "RawProtocolResponse",
    "ProtocolClient",
    "FixtureProtocolClient",
    "LiveProtocolClient",
]


class RawProtocolResponse(BaseModel):
    """Verbatim protocol payload plus retrieval metadata (unparsed)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    payload: dict[str, Any]
    retrieved_at: str
    source_endpoints: dict[str, str] = Field(default_factory=dict)


class ProtocolClient(ABC):
    """Abstract protocol client. Subclasses implement only :meth:`fetch_raw`."""

    @abstractmethod
    def fetch_raw(self) -> RawProtocolResponse:
        """Fetch the raw, unparsed protocol payload."""

    def load_snapshot(self) -> RoundProtocolSnapshot:
        """Fetch and parse into an immutable snapshot (fail closed on missing state)."""
        return build_snapshot(self.fetch_raw())

    def load_round_context(self, snapshot: RoundProtocolSnapshot | None = None) -> RoundContext:
        raw = self.fetch_raw()
        snap = snapshot or build_snapshot(raw)
        return build_round_context(raw, snap)


class FixtureProtocolClient(ProtocolClient):
    """Deterministic client backed by a saved JSON API fixture."""

    def __init__(self, fixture_path: str | Path) -> None:
        self._path = Path(fixture_path)

    def fetch_raw(self) -> RawProtocolResponse:
        if not self._path.exists():
            raise ProtocolError(f"protocol fixture not found: {self._path}")
        with self._path.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
        if not isinstance(data, dict):
            raise ProtocolError(f"protocol fixture {self._path} is not a JSON object")
        payload = data.get("payload", data)
        retrieved_at = data.get("retrieved_at") or payload.get("retrieved_at")
        if not retrieved_at:
            raise ProtocolError("protocol fixture missing 'retrieved_at'")
        source_endpoints = data.get("source_endpoints") or payload.get("source_endpoints") or {}
        return RawProtocolResponse(
            payload=payload,
            retrieved_at=retrieved_at,
            source_endpoints=source_endpoints,
        )


class LiveProtocolClient(ProtocolClient):
    """Production seam for the live Minos platform.

    Not wired in Stage 0: the authoritative live endpoint schema is not available
    (see audit §7). This fails closed with a typed error instead of fabricating a
    response. Implement ``fetch_raw`` against the real API in a later stage.
    """

    def __init__(self, base_url: str, *, timeout_seconds: float = 10.0) -> None:
        self._base_url = base_url
        self._timeout = timeout_seconds

    def fetch_raw(self) -> RawProtocolResponse:
        raise UnavailableError(
            "LiveProtocolClient is not enabled in Stage 0: the live Minos endpoint "
            "schema is not authoritatively available. Use FixtureProtocolClient for "
            "deterministic behavior, or implement this against the real API in a "
            "later stage."
        )
