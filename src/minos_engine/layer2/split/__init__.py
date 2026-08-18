"""Layer 2 stage L2-C — immutable dataset registry and deterministic 50/10/15 split.

This package establishes a leakage-resistant, reproducible dataset-split foundation
only. It does NOT implement profiling ingestion, feature ingestion, experiments,
optimizers, model training, prediction, controller logic, configuration selection, or
feedback (later Layer 2 stages). ``Layer2Service.select_config`` remains blocked.
"""

from __future__ import annotations

__all__ = ["SPLIT_POLICY_VERSION", "SALT"]

from .policy import SALT, SPLIT_POLICY_VERSION
