"""Monotonic clock and deadline / time-budget abstractions.

A Minos round lasts 72 minutes, but prediction must be fast (target ~5 min).
Every stage receives the *same* monotonic :class:`Deadline` and checks it at
bounded work units. Deadlines are computed from a monotonic clock so they are
immune to wall-clock adjustments.

Tests inject a :class:`FakeClock` and never sleep.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from .errors import MinosEngineError

__all__ = [
    "Clock",
    "SystemClock",
    "FakeClock",
    "Deadline",
    "TimeBudgetError",
    "DeadlineExpiredError",
    "InsufficientTimeError",
]


class TimeBudgetError(MinosEngineError):
    """Base for deadline/time-budget failures."""


class DeadlineExpiredError(TimeBudgetError):
    """The monotonic deadline has already passed."""


class InsufficientTimeError(TimeBudgetError):
    """Less than the required minimum time remains before the deadline."""


@runtime_checkable
class Clock(Protocol):
    """Monotonic clock protocol. ``monotonic()`` returns seconds; only deltas matter."""

    def monotonic(self) -> float: ...


class SystemClock:
    """Production clock backed by :func:`time.monotonic`."""

    def monotonic(self) -> float:
        return time.monotonic()


@dataclass
class FakeClock:
    """Deterministic clock for tests. Advance explicitly; never sleeps."""

    _now: float = 0.0

    def monotonic(self) -> float:
        return self._now

    def advance(self, seconds: float) -> None:
        if seconds < 0:
            raise ValueError("cannot advance a monotonic clock backwards")
        self._now += seconds


@dataclass(frozen=True)
class Deadline:
    """A monotonic deadline plus derived time-budget queries.

    Construct via :meth:`start`. ``budget_seconds`` is the total budget from the
    moment of construction; ``deadline_monotonic`` is the absolute monotonic
    instant at which work must stop.
    """

    clock: Clock
    deadline_monotonic: float
    budget_seconds: float = field(default=0.0)

    @classmethod
    def start(cls, clock: Clock, budget_seconds: float) -> Deadline:
        if budget_seconds < 0:
            raise ValueError("budget_seconds must be non-negative")
        return cls(
            clock=clock,
            deadline_monotonic=clock.monotonic() + budget_seconds,
            budget_seconds=budget_seconds,
        )

    def remaining_seconds(self) -> float:
        """Seconds until the deadline; may be negative if already expired."""
        return self.deadline_monotonic - self.clock.monotonic()

    def expired(self) -> bool:
        return self.remaining_seconds() <= 0.0

    def require_remaining(self, minimum_seconds: float) -> float:
        """Assert at least ``minimum_seconds`` remain; return the remaining time.

        Raises :class:`DeadlineExpiredError` if already expired, else
        :class:`InsufficientTimeError` if under the minimum.
        """
        remaining = self.remaining_seconds()
        if remaining <= 0.0:
            raise DeadlineExpiredError(
                f"deadline expired ({remaining:.3f}s past due)"
            )
        if remaining < minimum_seconds:
            raise InsufficientTimeError(
                f"insufficient time: {remaining:.3f}s remaining < {minimum_seconds:.3f}s required"
            )
        return remaining

    def child_budget(self, maximum_seconds: float, reserve_seconds: float = 0.0) -> Deadline:
        """Derive a sub-stage deadline.

        The child gets at most ``maximum_seconds``, but never so much that fewer
        than ``reserve_seconds`` remain on the parent for finalization. Raises
        :class:`InsufficientTimeError` if the reserve cannot be honored.
        """
        if maximum_seconds < 0 or reserve_seconds < 0:
            raise ValueError("child_budget arguments must be non-negative")
        remaining = self.remaining_seconds()
        allowable = remaining - reserve_seconds
        if allowable <= 0.0:
            raise InsufficientTimeError(
                f"cannot reserve {reserve_seconds:.3f}s: only {remaining:.3f}s remain"
            )
        child = min(maximum_seconds, allowable)
        return Deadline(
            clock=self.clock,
            deadline_monotonic=self.clock.monotonic() + child,
            budget_seconds=child,
        )
