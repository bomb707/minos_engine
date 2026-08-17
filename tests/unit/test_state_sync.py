"""Staleness detection and fallback policy."""

from __future__ import annotations

import pytest

from minos_engine.common.errors import StaleStateError
from minos_engine.protocol.snapshot import build_snapshot
from minos_engine.protocol.state_sync import FallbackPolicy, assert_usable, evaluate_staleness
from tests.conftest import make_raw_response


def test_fresh_snapshot_is_usable():
    snap = build_snapshot(make_raw_response())
    result = assert_usable(
        snap, now_iso="2026-08-17T12:01:00+00:00", policy=FallbackPolicy(max_age_seconds=300)
    )
    assert result.stale is False


def test_exceeds_max_age_is_stale_and_blocked():
    snap = build_snapshot(make_raw_response())
    with pytest.raises(StaleStateError):
        assert_usable(
            snap, now_iso="2026-08-17T12:30:00+00:00", policy=FallbackPolicy(max_age_seconds=300)
        )


def test_stale_allowed_when_policy_permits():
    snap = build_snapshot(make_raw_response())
    result = assert_usable(
        snap,
        now_iso="2026-08-17T12:30:00+00:00",
        policy=FallbackPolicy(allow_stale=True, max_age_seconds=300),
    )
    assert result.stale is True


def test_marked_stale_snapshot_flagged():
    payload = make_raw_response().payload
    payload["stale"] = True
    snap = build_snapshot(make_raw_response(payload))
    result = evaluate_staleness(
        snap, now_iso="2026-08-17T12:00:01+00:00", policy=FallbackPolicy(max_age_seconds=300)
    )
    assert result.marked_stale is True
    assert result.stale is True
