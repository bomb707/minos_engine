"""Append-only / immutable-identity trigger names (L2-B).

The authoritative trigger/function DDL is the frozen migration ``0001_l2b_initial``.
This module exposes the deterministic trigger names for inspection by the storage
fingerprint and the schema-inventory tests.
"""

from __future__ import annotations

from .constants import APPEND_ONLY_TABLES, IMMUTABLE_IDENTITY_TABLES

__all__ = [
    "REJECT_MUTATION_FUNCTION",
    "REJECT_IDENTITY_FUNCTION",
    "append_only_trigger_names",
    "identity_trigger_names",
]

REJECT_MUTATION_FUNCTION = "audit.minos_reject_mutation"
REJECT_IDENTITY_FUNCTION = "experiments.minos_reject_identity_change"


def append_only_trigger_names() -> tuple[str, ...]:
    return tuple(f"trg_{s}_{t}_append_only" for (s, t) in APPEND_ONLY_TABLES)


def identity_trigger_names() -> tuple[str, ...]:
    return tuple(f"trg_{s}_{t}_identity_immutable" for (s, t) in IMMUTABLE_IDENTITY_TABLES)
