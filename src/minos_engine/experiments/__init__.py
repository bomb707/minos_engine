"""L2-F offline experiment harness (deterministic GATK CONFIG candidate generation).

Offline-only. Does NOT score, rank, select, optimize, or activate ``select_config``.
The 23 EXPERIMENTAL registry parameters are owner-authorized for OFFLINE exploration
only (``offline_exploration_allowed = true``, ``live_production_allowed = false``); their
registry state is unchanged and none are promoted to live ACTIVE.
"""

from __future__ import annotations

__all__: list[str] = []
