"""Read the Phase-C confirmation out of the immutable ledgers as ``BaselineObservation``s.

Every rule lives in the shared plan-scoped reader; this module supplies the one thing that is
Phase C's own — the derived Phase-C plan, whose 50 members and 10 candidates are recomputed from
:func:`~minos_engine.baseline.phase_c.build_l2f2_phase_c_authority`.

Phase C differs from A and B in one way the reader must not mistake for a defect: it is expected
to end with FEWER than its 500 logical observations. Racing eliminates candidates that can no
longer reach the top four, and an eliminated candidate correctly stops where it stopped. Absence
of an observation is absence — never a zero, never a failure.
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

    from minos_engine.baseline.phase_c import PhaseCAuthority

__all__ = [
    "PHASE_C_SCORING_CONTRACT",
    "PhaseCObservationError",
    "PhaseCObservationSnapshot",
    "load_phase_c_observations",
]

PHASE_C_SCORING_CONTRACT = PLAN_SCORING_CONTRACT
PhaseCObservationError = PlanObservationError
PhaseCObservationSnapshot = PlanObservationSnapshot


def load_phase_c_observations(
    engine: Engine, *, authority: PhaseCAuthority | None = None
) -> PhaseCObservationSnapshot:
    """Derive every DECIDED Phase-C observation from the immutable ledgers. Plan-scoped."""
    from minos_engine.baseline.phase_c import PhaseCError, build_l2f2_phase_c_authority

    resolved = authority or build_l2f2_phase_c_authority(engine)
    snapshot = load_plan_observations(engine, plan=resolved.plan, label="Phase-C")
    if (
        snapshot.execution_environment_hash
        and snapshot.execution_environment_hash != resolved.execution_environment_hash
    ):
        raise PhaseCError(
            f"Phase-C outcomes were produced under {snapshot.execution_environment_hash}, but the "
            f"completed Phase-B screen this promotion came from ran under "
            f"{resolved.execution_environment_hash}; a confirmation must not change runtime"
        )
    return snapshot
