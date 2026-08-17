"""Layer 1 service — stable API, explicit not-ready behavior.

Stage 0 does not implement BAM profiling. ``analyze`` raises
:class:`StageNotReadyError`. Do not add fake adaptive behavior to make this look
complete (assignment §11).
"""

from __future__ import annotations

from minos_engine.common.errors import StageNotReadyError

from .contracts import ProfileRequest, ProfileResult

__all__ = ["Layer1Service"]


class Layer1Service:
    """Public Layer 1 entry point (Layer 1 spec §1)."""

    def analyze(self, request: ProfileRequest) -> ProfileResult:
        raise StageNotReadyError(
            "Layer 1 is not implemented (Stage 0). It is built in Stage 2 and "
            "qualified in Stage 3 before L1-READY is issued."
        )
