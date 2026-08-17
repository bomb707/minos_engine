"""Validator Twin — a deterministic, auditable, fixture-backed model of the
Minos validator's *observable* evaluation process (Overall spec §7).

The Twin reproduces, as far as authoritative evidence permits:
round protocol observation → CONFIG validation → GATK execution-plan
construction → (fixture/adapter) comparison-result ingestion → Minos scoring
inputs → parity assessment → an immutable run manifest.

It is NOT Layer 1 BAM profiling and NOT Layer 2 optimization. It never executes
real GATK or hap.py in Stage 1, never accesses production truth data, and never
claims a higher parity level than it achieves. Where authoritative validator
behavior (notably the pinned AdvancedScorer formula) is not available in the
specifications or repository, the Twin returns a typed *unavailable* result
rather than inventing a value.
"""

from .contracts import DECLARED_PARITY_LEVEL, ParityLevel  # noqa: F401

TWIN_TOOL_VERSION = "twin-v1"
