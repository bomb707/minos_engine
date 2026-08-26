"""Read the Phase-B screen out of the immutable ledgers as ``BaselineObservation``s. Plan-scoped.

Every semantic rule is the shared plan-scoped reader's; this module supplies the derived Phase-B
plan and one extra requirement of its own — Phase B must have run under the SAME runtime as the
completed Phase-A campaign it descends from. A baseline search whose two phases ran on different
interpreters or JVMs is not one experiment, and the design Phase B is exploring was chosen from
Phase-A numbers produced under that specific runtime.

**No cross-plan reuse.** A Phase-A execution of the seed on chr18 is not reused as a Phase-B
observation even though the config hash and the dataset are identical. The two plans are separate
scientific questions asked over different member sets, and silently importing rows across them
would make the Phase-B budget and the Phase-B aggregates depend on what Phase A happened to have
run. Phase B therefore consumes only Phase-B rows, well inside its frozen maximum of 48 × 10.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from minos_engine.baseline.plan_observations import (
    PlanObservationError,
    PlanObservationSnapshot,
    load_plan_observations,
)

if TYPE_CHECKING:
    from sqlalchemy import Engine

    from minos_engine.baseline.phase_b import PhaseBAuthority

__all__ = [
    "PhaseBObservationError",
    "PhaseBObservationSnapshot",
    "load_phase_b_observations",
]

PhaseBObservationError = PlanObservationError
PhaseBObservationSnapshot = PlanObservationSnapshot


def load_phase_b_observations(
    engine: Engine, *, authority: PhaseBAuthority | None = None
) -> PhaseBObservationSnapshot:
    """Derive every DECIDED Phase-B observation from the immutable ledgers. Plan-scoped.

    ``authority`` exists so a caller already holding one avoids re-deriving it; it is never a way
    to supply a different plan — a mismatch against the derived authority is refused.
    """
    from minos_engine.baseline.phase_b import build_l2f2_phase_b_authority

    derived = build_l2f2_phase_b_authority(engine)
    if authority is not None and authority.plan_hash != derived.plan_hash:
        raise PhaseBObservationError(
            "the supplied Phase-B authority is not the one this ledger derives"
        )

    snapshot = load_plan_observations(engine, plan=derived.plan, label="Phase-B")
    if (
        snapshot.execution_environment_hash is not None
        and snapshot.execution_environment_hash != derived.execution_environment_hash
    ):
        raise PhaseBObservationError(
            f"Phase B ran under runtime {snapshot.execution_environment_hash}, but the completed "
            f"Phase-A campaign it descends from ran under {derived.execution_environment_hash}; "
            "one baseline search is one runtime"
        )
    return snapshot
