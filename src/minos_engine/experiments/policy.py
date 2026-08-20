"""L2-F experiment parameter-policy contract (owner-approved offline exploration).

The authoritative registry states are UNCHANGED (2 FIXED, 23 EXPERIMENTAL, 0 ACTIVE).
This module defines the separate, owner-approved authority that permits the EXPERIMENTAL
parameters to be varied by the OFFLINE harness only — it never promotes them to live
ACTIVE, never qualifies a baseline, and never authorizes ``select_config``.

Identities are derived from repository-owned contracts (the accepted GATK registry and
the accepted config canonicalizer), never from caller input. ``config_hash`` and
``parameter_space_hash`` keep their existing accepted semantics and remain separate; L2-F
identities cross-bind both.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from minos_engine.callers.contracts import ACTIVE_CALLER, ParameterSpaceSnapshot, ParameterState
from minos_engine.callers.gatk.config import CanonicalConfig, canonicalize_config
from minos_engine.callers.gatk.parameter_registry import (
    REGISTRY,
    SPEC_SOURCE_VERSION,
    GatkParameterRegistry,
)
from minos_engine.common.hashing import canonical_hash

__all__ = [
    "EXPERIMENT_POLICY_SCHEMA",
    "GENERATION_POLICY_VERSION",
    "EXPLORABLE_REGISTRY_STATE",
    "ExperimentParameterPolicy",
    "documented_parameter_space",
    "experiment_seed_v1",
    "build_experiment_parameter_policy",
]

EXPERIMENT_POLICY_SCHEMA = "l2f-experiment-parameter-policy-v1"
#: TRUTHFUL, deterministic snapshot metadata derived from the accepted documented-review
#: identity ``SPEC_SOURCE_VERSION`` (``documented-2026-08-09``). It is EXCLUDED from
#: ``parameter_space_hash`` (which hashes only caller + ranges), so it never enters any
#: L2-F scientific identity — but it must still be truthful, not fabricated.
_DOCUMENTED_RETRIEVED_AT = SPEC_SOURCE_VERSION.replace("documented-", "") + "T00:00:00+00:00"
#: Owner-approved deterministic one-at-a-time documented-endpoint generation policy.
GENERATION_POLICY_VERSION = "L2F-OAT-ENDPOINT-v1"
#: The ONLY registry state the offline harness may vary. FIXED never varies; there are no
#: ACTIVE/CONDITIONAL/DISABLED parameters, and none are created here.
EXPLORABLE_REGISTRY_STATE = ParameterState.EXPERIMENTAL.value

#: Canonical description of the generation MECHANICS (not scientific preferences), stored
#: as an IMMUTABLE tuple of (key, value) pairs so the frozen policy exposes no mutable
#: mapping after its hash is computed. Any change here changes the policy hash.
_GENERATION_MECHANICS: tuple[tuple[str, str | bool], ...] = (
    ("kind", "one-at-a-time-documented-endpoint"),
    ("seed", "EXPERIMENT_SEED_V1 = all authoritative official_default values"),
    ("one_factor_at_a_time", True),
    ("cartesian_product", False),
    ("randomness", False),
    ("int_float", "try documented_min then documented_max, excluding the official_default"),
    ("bool", "try the single value not equal to the official_default"),
    ("enum", "try every enum value except the official_default, in registry enum order"),
    ("fixed_parameters", "never varied"),
    ("explorable_state", EXPLORABLE_REGISTRY_STATE),
    ("cross_parameter", "propose -> canonicalize -> include iff valid; deterministically omit invalid"),
    ("dedup", "by config_hash, first occurrence wins; final semantic order preserved"),
    ("candidate_order", "seed first, then parameter name ascending, then min-max / enum-order / bool"),
)


def _mechanics_dict() -> dict[str, str | bool]:
    """Materialize the mechanics mapping for canonical hashing / reporting (the frozen
    policy never stores or exposes this mutable dict)."""
    return dict(_GENERATION_MECHANICS)


@dataclass(frozen=True)
class ExperimentParameterPolicy:
    """The frozen, repository-derived authority for offline EXPERIMENTAL exploration."""

    schema_version: str
    caller: str
    registry_hash: str
    parameter_space_hash: str
    seed_config_hash: str
    explorable_registry_state: str
    generation_policy_version: str
    #: immutable (key, value) pairs — no mutable mapping is exposed after hashing.
    generation_mechanics: tuple[tuple[str, str | bool], ...]
    experiment_parameter_policy_hash: str


def documented_parameter_space(
    registry: GatkParameterRegistry = REGISTRY,
) -> ParameterSpaceSnapshot:
    """The accepted documented parameter space (legal offline exploration envelope).

    ``retrieved_at`` is operational metadata excluded from ``parameter_space_hash``
    (which hashes only caller + ranges), so the identity is deterministic."""
    return registry.documented_parameter_space(retrieved_at=_DOCUMENTED_RETRIEVED_AT)


def experiment_seed_v1(registry: GatkParameterRegistry = REGISTRY) -> CanonicalConfig:
    """EXPERIMENT_SEED_V1 — the canonical CONFIG of all authoritative official defaults.

    Built through the accepted canonicalizer with an empty request (omitted non-FIXED
    parameters resolve to their official defaults). NOT a SAFE_BASELINE, not
    BASELINE-QUALIFIED, not a production winner."""
    return canonicalize_config(
        {}, registry=registry, parameter_space=documented_parameter_space(registry)
    )


def _policy_content(
    *, registry_hash: str, parameter_space_hash: str, seed_config_hash: str
) -> dict[str, Any]:
    return {
        "schema_version": EXPERIMENT_POLICY_SCHEMA,
        "caller": ACTIVE_CALLER,
        "registry_hash": registry_hash,
        "parameter_space_hash": parameter_space_hash,
        "seed_config_hash": seed_config_hash,
        "explorable_registry_state": EXPLORABLE_REGISTRY_STATE,
        "generation_policy_version": GENERATION_POLICY_VERSION,
        # materialize the dict ONLY for canonical hashing (identity unchanged vs v1).
        "generation_mechanics": _mechanics_dict(),
    }


def build_experiment_parameter_policy(
    registry: GatkParameterRegistry = REGISTRY,
) -> ExperimentParameterPolicy:
    """Derive the offline experiment parameter policy from repository-owned contracts."""
    space = documented_parameter_space(registry)
    seed = experiment_seed_v1(registry)
    content = _policy_content(
        registry_hash=registry.registry_hash(),
        parameter_space_hash=space.parameter_space_hash,
        seed_config_hash=seed.config_hash,
    )
    return ExperimentParameterPolicy(
        schema_version=EXPERIMENT_POLICY_SCHEMA,
        caller=ACTIVE_CALLER,
        registry_hash=registry.registry_hash(),
        parameter_space_hash=space.parameter_space_hash,
        seed_config_hash=seed.config_hash,
        explorable_registry_state=EXPLORABLE_REGISTRY_STATE,
        generation_policy_version=GENERATION_POLICY_VERSION,
        generation_mechanics=_GENERATION_MECHANICS,
        experiment_parameter_policy_hash=canonical_hash(content),
    )
