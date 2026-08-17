"""Layer 2 service — stable API, explicitly blocked until L1-READY.

Stage 0 keeps Layer 2 blocked. ``select_config`` raises
:class:`StageNotReadyError`. BaselineEngine is a future *fallback mode inside
this service* (mode SAFE_BASELINE), never a parallel production engine
(Overall spec §Architecture-correction; assignment §11-12).
"""

from __future__ import annotations

from minos_engine.common.errors import StageNotReadyError

from .contracts import DecisionRequest, DecisionResult

__all__ = ["Layer2Service"]


class Layer2Service:
    """Public Layer 2 entry point (Layer 2 spec §1)."""

    def select_config(self, request: DecisionRequest) -> DecisionResult:
        raise StageNotReadyError(
            "Layer 2 is blocked until L1-READY. It is built in Stage 4+ and only "
            "after l1-ready.json verifies successfully via the entry gate."
        )
