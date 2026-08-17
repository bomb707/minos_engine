"""Deadline / time-budget with a fake clock (no sleeping)."""

from __future__ import annotations

import pytest

from minos_engine.common.clock import (
    Deadline,
    DeadlineExpiredError,
    FakeClock,
    InsufficientTimeError,
)


def test_remaining_and_expiration():
    clock = FakeClock()
    deadline = Deadline.start(clock, 300)
    assert deadline.remaining_seconds() == 300
    assert not deadline.expired()
    clock.advance(299)
    assert deadline.remaining_seconds() == 1
    clock.advance(1)
    assert deadline.expired()
    clock.advance(5)
    assert deadline.remaining_seconds() == -5


def test_require_remaining_ok_and_insufficient():
    clock = FakeClock()
    deadline = Deadline.start(clock, 300)
    assert deadline.require_remaining(100) == 300
    clock.advance(250)
    with pytest.raises(InsufficientTimeError):
        deadline.require_remaining(100)


def test_require_remaining_expired():
    clock = FakeClock()
    deadline = Deadline.start(clock, 10)
    clock.advance(11)
    with pytest.raises(DeadlineExpiredError):
        deadline.require_remaining(1)


def test_child_budget_respects_reserve():
    clock = FakeClock()
    parent = Deadline.start(clock, 300)
    child = parent.child_budget(maximum_seconds=120, reserve_seconds=60)
    assert child.remaining_seconds() == 120  # capped by maximum
    # A large maximum is clipped so the parent keeps its reserve.
    child2 = parent.child_budget(maximum_seconds=1000, reserve_seconds=60)
    assert child2.remaining_seconds() == 240  # 300 - 60 reserve


def test_child_budget_rejects_when_reserve_unavailable():
    clock = FakeClock()
    parent = Deadline.start(clock, 50)
    with pytest.raises(InsufficientTimeError):
        parent.child_budget(maximum_seconds=100, reserve_seconds=60)


def test_clock_cannot_move_backwards():
    clock = FakeClock()
    with pytest.raises(ValueError):
        clock.advance(-1)
