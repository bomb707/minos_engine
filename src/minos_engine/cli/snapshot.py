"""``minos-engine protocol snapshot`` — build a snapshot from a fixture.

Thin composition root: delegates to the protocol client/builder and shapes a
serializable summary.
"""

from __future__ import annotations

from typing import Any

from minos_engine.protocol.client import FixtureProtocolClient

__all__ = ["snapshot_from_fixture"]


def snapshot_from_fixture(fixture_path: str) -> dict[str, Any]:
    client = FixtureProtocolClient(fixture_path)
    snapshot = client.load_snapshot()
    return snapshot.model_dump(mode="json")
