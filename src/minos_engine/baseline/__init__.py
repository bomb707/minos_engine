"""L2-F2 baseline search protocol — pure, deterministic protocol logic.

Nothing in this package runs a process, touches truth on disk, writes to PostgreSQL, or invokes
GATK or hap.py. It decides *how scored executions are compared and promoted*, and it is frozen
and hashed before the first real score exists so the experiment is genuinely pre-registered.
"""

from __future__ import annotations

from minos_engine.baseline.protocol import (
    BASELINE_PROTOCOL_VERSION,
    BaselineProtocol,
    build_baseline_protocol,
    compute_protocol_hash,
)

__all__ = [
    "BASELINE_PROTOCOL_VERSION",
    "BaselineProtocol",
    "build_baseline_protocol",
    "compute_protocol_hash",
]
