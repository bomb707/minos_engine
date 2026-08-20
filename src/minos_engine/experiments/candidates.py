"""L2-F deterministic candidate generation (policy L2F-OAT-ENDPOINT-v1).

Pure, deterministic one-at-a-time documented-endpoint generation of canonical GATK
CONFIG candidates from the accepted registry + EXPERIMENT_SEED_V1, cross-bound to the
owner-approved offline experiment parameter policy. No randomness, no optimizer, no
Cartesian product, no feature values, no scores, no timestamps/paths/UUIDs.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

from minos_engine.callers.contracts import ParameterSpaceSnapshot, ParameterState, ParameterType
from minos_engine.callers.gatk.config import CanonicalConfig, canonicalize_config
from minos_engine.callers.gatk.parameter_registry import (
    REGISTRY,
    GatkParameter,
    GatkParameterRegistry,
)
from minos_engine.common.canonical_json import canonical_json_bytes
from minos_engine.common.errors import ConfigValidationError
from minos_engine.common.hashing import canonical_hash, sha256_hex

from .policy import (
    GENERATION_POLICY_VERSION,
    ExperimentParameterPolicy,
    build_experiment_parameter_policy,
    documented_parameter_space,
    experiment_seed_v1,
)

__all__ = [
    "CANDIDATE_SET_SCHEMA",
    "SkippedProbe",
    "CandidateSet",
    "generate_accepted_candidate_set",
    "candidate_set_hash",
    "verify_accepted_candidate_set",
    "CandidateSetVerificationError",
]

#: Registry parameter names whose state is FIXED (never varied; equal to defaults).
_FIXED_NAMES = frozenset(p.name for p in REGISTRY.all() if p.state is ParameterState.FIXED)


class CandidateSetVerificationError(ConfigValidationError):
    """A candidate set does not match the repository-derived offline experiment policy."""


CANDIDATE_SET_SCHEMA = "l2f-candidate-set-v1"


@dataclass(frozen=True)
class SkippedProbe:
    """A deterministically omitted one-at-a-time probe (invalid under accepted coupling
    or bounds). Diagnostic only — NEVER part of candidate identity."""

    parameter: str
    value: Any
    reason: str


@dataclass(frozen=True)
class CandidateSet:
    """An ordered, deduplicated set of canonical candidate CONFIGs + its identity."""

    policy: ExperimentParameterPolicy
    configs: tuple[CanonicalConfig, ...]
    ordered_config_hashes: tuple[str, ...]
    candidate_count: int
    candidate_set_hash: str
    skipped: tuple[SkippedProbe, ...]

    @property
    def seed_config_hash(self) -> str:
        return self.policy.seed_config_hash


def _endpoint_values(param: GatkParameter) -> Iterator[Any]:
    """Deterministic one-at-a-time alternatives for one EXPERIMENTAL parameter."""
    default = param.official_default
    if param.type in (ParameterType.INT, ParameterType.FLOAT):
        for num in (param.documented_min, param.documented_max):  # min then max
            if num is not None and num != default:
                yield num
    elif param.type is ParameterType.BOOL:
        yield (not default)
    elif param.type is ParameterType.ENUM:
        for opt in param.enum_values or ():  # registry enum order
            if opt != default:
                yield opt
    else:  # pragma: no cover - registry types are exhaustive
        raise ConfigValidationError(f"unsupported parameter type for generation: {param.type}")


def candidate_set_hash(
    *,
    policy: ExperimentParameterPolicy,
    ordered_config_hashes: tuple[str, ...],
) -> str:
    """Canonical candidate-set identity — binds the registry/space/policy/seed identities
    and the ordered config_hashes (no timestamp/score/feature/path/worker/uuid)."""
    content = {
        "schema_version": CANDIDATE_SET_SCHEMA,
        "registry_hash": policy.registry_hash,
        "parameter_space_hash": policy.parameter_space_hash,
        "experiment_parameter_policy_hash": policy.experiment_parameter_policy_hash,
        "seed_config_hash": policy.seed_config_hash,
        "generation_policy_version": GENERATION_POLICY_VERSION,
        "ordered_config_hashes": list(ordered_config_hashes),
        "candidate_count": len(ordered_config_hashes),
    }
    return canonical_hash(content)


def _generate_candidate_set_against_registry(
    registry: GatkParameterRegistry = REGISTRY,
    *,
    space: ParameterSpaceSnapshot | None = None,
    policy: ExperimentParameterPolicy | None = None,
) -> CandidateSet:
    """Deterministically generate the L2F-OAT-ENDPOINT-v1 candidate set.

    seed first; then parameters in ascending lexicographic name order; within a parameter
    numeric min-then-max / enum-order / bool-toggle. Each non-seed candidate differs from
    the seed in exactly ONE EXPERIMENTAL parameter. Every proposal is canonicalized
    through the accepted contract; invalid proposals (e.g. coupling violations) are
    deterministically omitted (never replaced). Deduplicated by config_hash (first wins),
    preserving semantic order.
    """
    space = space or documented_parameter_space(registry)
    policy = policy or build_experiment_parameter_policy(registry)

    seed = experiment_seed_v1(registry)
    ordered: list[CanonicalConfig] = [seed]
    seen: set[str] = {seed.config_hash}
    skipped: list[SkippedProbe] = []

    for name in registry.names():  # ascending lexicographic
        param = registry.get(name)
        if param.state is ParameterState.FIXED:
            continue
        if param.state is not ParameterState.EXPERIMENTAL:  # no ACTIVE/CONDITIONAL/DISABLED exist
            continue
        for value in _endpoint_values(param):
            try:
                candidate = canonicalize_config(
                    {name: value}, registry=registry, parameter_space=space
                )
            except ConfigValidationError as exc:
                skipped.append(SkippedProbe(parameter=name, value=value, reason=str(exc)))
                continue
            if candidate.config_hash in seen:  # equals seed or an earlier candidate
                continue
            seen.add(candidate.config_hash)
            ordered.append(candidate)

    ordered_hashes = tuple(c.config_hash for c in ordered)
    return CandidateSet(
        policy=policy,
        configs=tuple(ordered),
        ordered_config_hashes=ordered_hashes,
        candidate_count=len(ordered),
        candidate_set_hash=candidate_set_hash(policy=policy, ordered_config_hashes=ordered_hashes),
        skipped=tuple(skipped),
    )


def generate_accepted_candidate_set() -> CandidateSet:
    """THE accepted offline candidate set — no caller-selected trust objects.

    Derives the registry, documented parameter space, experiment policy, and seed
    internally from repository-owned contracts. Production/HARNESS-READY paths use this;
    the override-capable ``_generate_candidate_set_against_registry`` is a private
    test/generic builder only."""
    return _generate_candidate_set_against_registry(REGISTRY)


def verify_accepted_candidate_set(candidate_set: CandidateSet) -> dict[str, bool]:
    """THE accepted verifier — NO registry/space/policy/seed override.

    The trust anchor is always the repository ``REGISTRY`` + documented parameter space +
    approved experiment policy + repository-derived seed. A caller cannot make a forged
    registry authoritative. HARNESS-READY and accepted L2-F paths use this."""
    return _verify_candidate_set_against_registry(candidate_set, REGISTRY)


def _verify_candidate_set_against_registry(
    candidate_set: CandidateSet, registry: GatkParameterRegistry
) -> dict[str, bool]:
    """TEST-ONLY generic verifier (explicit registry). Never the accepted authority — a
    caller-selected registry must not be able to certify a forged set. Accepted paths call
    :func:`verify_accepted_candidate_set` (no override)."""
    truth = _generate_candidate_set_against_registry(registry)
    space = documented_parameter_space(registry)
    seed = experiment_seed_v1(registry)
    configs = candidate_set.configs

    # (A5) independently bind the actual CONFIG PAYLOADS — never trust stored config_hash
    # or the ordered-hash list alone.
    payload_config_hash_ok = all(
        sha256_hex(canonical_json_bytes(c.effective_config)) == c.config_hash for c in configs
    )
    revalidates = True
    for c in configs:
        try:
            recanon = canonicalize_config(
                c.effective_config, registry=registry, parameter_space=space
            )
        except ConfigValidationError:
            revalidates = False
            break
        if recanon.config_hash != c.config_hash or recanon.effective_config != c.effective_config:
            revalidates = False
            break
    one_factor = all(
        sum(1 for k, v in c.effective_config.items() if v != seed.effective_config[k]) == 1
        for c in configs[1:]
    )
    fixed_ok = all(
        c.effective_config[name] == seed.effective_config[name]
        for c in configs
        for name in _FIXED_NAMES
    )

    checks = {
        "registry_hash_bound": candidate_set.policy.registry_hash == truth.policy.registry_hash,
        "parameter_space_hash_bound": candidate_set.policy.parameter_space_hash
        == truth.policy.parameter_space_hash,
        "experiment_policy_hash_bound": candidate_set.policy.experiment_parameter_policy_hash
        == truth.policy.experiment_parameter_policy_hash,
        "seed_config_hash_bound": candidate_set.policy.seed_config_hash
        == truth.policy.seed_config_hash,
        "ordered_config_hashes_bound": candidate_set.ordered_config_hashes
        == truth.ordered_config_hashes,
        "candidate_count_bound": candidate_set.candidate_count == truth.candidate_count,
        "candidate_set_hash_bound": candidate_set.candidate_set_hash == truth.candidate_set_hash,
        "candidate_set_hash_recomputed": candidate_set.candidate_set_hash
        == candidate_set_hash(
            policy=truth.policy, ordered_config_hashes=candidate_set.ordered_config_hashes
        ),
        "seed_first": bool(candidate_set.ordered_config_hashes)
        and candidate_set.ordered_config_hashes[0] == truth.policy.seed_config_hash,
        "configs_unique": len(set(candidate_set.ordered_config_hashes))
        == candidate_set.candidate_count,
        # (A5) payload binding
        "configs_length_bound": len(configs) == candidate_set.candidate_count,
        "config_hashes_match_payloads": tuple(c.config_hash for c in configs)
        == candidate_set.ordered_config_hashes,
        "config_hash_independently_recomputed": payload_config_hash_ok,
        "config_payloads_match_regenerated": tuple(c.effective_config for c in configs)
        == tuple(c.effective_config for c in truth.configs),
        "config_revalidates_under_registry": revalidates,
        "parameter_space_hash_per_candidate": all(
            c.parameter_space_hash == space.parameter_space_hash for c in configs
        ),
        "seed_equals_experiment_seed_v1": bool(configs)
        and configs[0].effective_config == seed.effective_config,
        "fixed_equal_defaults": fixed_ok,
        "one_factor_at_a_time": one_factor,
    }
    if not all(checks.values()):
        failed = ", ".join(k for k, v in checks.items() if not v)
        raise CandidateSetVerificationError(f"candidate set verification failed: {failed}")
    return checks
