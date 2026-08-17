"""RoundProtocolSnapshot: deterministic identity, required identities fail closed."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from minos_engine.common.errors import SnapshotIncompleteError
from minos_engine.protocol.snapshot import build_snapshot
from tests.conftest import make_raw_payload, make_raw_response


def test_snapshot_id_is_deterministic():
    s1 = build_snapshot(make_raw_response())
    s2 = build_snapshot(make_raw_response())
    assert s1.snapshot_id == s2.snapshot_id
    assert len(s1.snapshot_id) == 64
    assert s1.compute_id() == s1.snapshot_id


def test_snapshot_is_frozen():
    s = build_snapshot(make_raw_response())
    with pytest.raises(ValidationError):
        s.round_id = "changed"  # type: ignore[misc]


@pytest.mark.parametrize(
    "identity", ["minos_upstream_commit", "scorer_hash", "gatk_image_digest", "reference_sha256"]
)
def test_missing_required_identity_fails_closed(identity):
    payload = make_raw_payload()
    del payload["provenance"][identity]
    with pytest.raises(SnapshotIncompleteError):
        build_snapshot(make_raw_response(payload))


def test_empty_scorer_identity_fails_closed():
    payload = make_raw_payload()
    payload["provenance"]["scorer_hash"] = "   "
    with pytest.raises(SnapshotIncompleteError):
        build_snapshot(make_raw_response(payload))


def test_missing_round_section_fails_closed():
    payload = make_raw_payload()
    del payload["round"]
    with pytest.raises(SnapshotIncompleteError):
        build_snapshot(make_raw_response(payload))


def test_stale_flag_is_explicit():
    payload = make_raw_payload()
    payload["stale"] = True
    s = build_snapshot(make_raw_response(payload))
    assert s.stale is True
