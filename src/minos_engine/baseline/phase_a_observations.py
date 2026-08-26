"""Read the frozen Phase-A screen out of the immutable ledgers as ``BaselineObservation``s.

Every rule lives in the shared plan-scoped reader; this module supplies the one thing that is
Phase-A's own — the frozen Phase-A plan, whose 5 members, 39 candidates and 195 logical
identities are recomputed from :func:`build_phase_a_authority`.

The read is scoped to the Phase-A ``plan_hash``. A baseline search holds more than one plan at a
time — once Phase B is persisted the same store contains both — and a reader that counted rows
globally would let Phase-B executions enter Phase-A statistics. Scoping narrows what is read; it
does not relax what is verified, so a job that claims the Phase-A plan hash is still checked
against the frozen logical identities exactly as before.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from minos_engine.baseline.plan_observations import (
    PLAN_SCORING_CONTRACT,
    PlanObservationError,
    PlanObservationSnapshot,
    load_plan_observations,
)

if TYPE_CHECKING:
    from sqlalchemy import Engine

__all__ = [
    "PHASE_A_SCORING_CONTRACT",
    "PhaseAObservationError",
    "PhaseAObservationSnapshot",
    "load_phase_a_observations",
]

#: the production scoring contract every Phase-A evaluation must have been recorded under.
PHASE_A_SCORING_CONTRACT = PLAN_SCORING_CONTRACT

#: Phase A's failures are the shared reader's failures; the type is kept for callers and tests
#: that already name it.
PhaseAObservationError = PlanObservationError
PhaseAObservationSnapshot = PlanObservationSnapshot


def load_phase_a_observations(engine: Engine) -> PhaseAObservationSnapshot:
    """Derive every DECIDED Phase-A observation from the immutable ledgers. Plan-scoped."""
    from minos_engine.baseline.phase_a import build_phase_a_authority

    return load_plan_observations(engine, plan=build_phase_a_authority().plan, label="Phase-A")
