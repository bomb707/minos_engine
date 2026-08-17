"""Staleness detection for cached protocol snapshots.

A cached snapshot may be used only when it is explicitly marked stale AND the
fallback policy explicitly permits stale use (Overall spec §3). ``now_iso`` is
supplied by the caller so staleness evaluation is deterministic and testable.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from minos_engine.common.errors import StaleStateError
from minos_engine.common.timestamps import parse_iso8601_utc

from .contracts import RoundProtocolSnapshot

__all__ = ["FallbackPolicy", "StalenessResult", "evaluate_staleness", "assert_usable"]


class FallbackPolicy(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    allow_stale: bool = False
    max_age_seconds: float = Field(default=300.0, gt=0)


class StalenessResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    age_seconds: float
    exceeds_max_age: bool
    marked_stale: bool
    stale: bool


def evaluate_staleness(
    snapshot: RoundProtocolSnapshot, *, now_iso: str, policy: FallbackPolicy
) -> StalenessResult:
    age = (parse_iso8601_utc(now_iso) - parse_iso8601_utc(snapshot.retrieved_at)).total_seconds()
    exceeds = age > policy.max_age_seconds
    stale = bool(snapshot.stale) or exceeds
    return StalenessResult(
        age_seconds=age,
        exceeds_max_age=exceeds,
        marked_stale=bool(snapshot.stale),
        stale=stale,
    )


def assert_usable(
    snapshot: RoundProtocolSnapshot, *, now_iso: str, policy: FallbackPolicy
) -> StalenessResult:
    """Return the staleness result, or raise if a stale snapshot is not permitted."""
    result = evaluate_staleness(snapshot, now_iso=now_iso, policy=policy)
    if result.stale and not policy.allow_stale:
        raise StaleStateError(
            f"snapshot is stale (age={result.age_seconds:.1f}s, "
            f"marked_stale={result.marked_stale}) and fallback policy forbids stale use"
        )
    return result
