"""Phase-A sensitivity analysis and the FROZEN Phase-B candidate design.

Phase A is a **screen**, not an optimisation. The accepted 39 L2-F1 candidates are one seed plus
38 one-at-a-time alternatives, so a single Phase-A pass measures how much each live GATK
dimension moves the objective at all. Phase B then spends its budget only where movement was
observed, instead of sampling 25 dimensions blindly.

Phase B is a deterministic **mixed-domain Latin hypercube** over exactly the six most influential
dimensions. Latin hypercube rather than a grid because 6 dimensions cannot be gridded inside the
frozen budget; mixed-domain because the live space is a mixture of bounded ints, bounded floats,
booleans and enums, and a coordinate in [0, 1) must be mapped into each of those differently.

Determinism is structural, not conventional: stratum permutations come from a domain-separated
SHA-256 stream keyed by the parameter name, so the design depends only on which dimensions were
selected. There is no system RNG, no clock, no ``hash()``, no hostname and no PID anywhere in
this module — the same six dimensions always yield byte-identical candidates on any host.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from minos_engine.baseline.objective import (
    BaselineObjectiveError,
    BaselineObservation,
    CandidateAggregate,
)
from minos_engine.callers.gatk.config import CanonicalConfig
from minos_engine.common.errors import MinosEngineError
from minos_engine.experiments.gatk_live_space import (
    LiveParameter,
    canonicalize_live_gatk_config,
    live_gatk_parameter_space,
)

__all__ = [
    "INFLUENTIAL_DIMENSION_COUNT",
    "LHS_DOMAIN",
    "LHS_PROPOSAL_CEILING",
    "PHASE_B_ANCHOR_COUNT",
    "PHASE_B_CANDIDATE_COUNT",
    "PHASE_B_LHS_COUNT",
    "DesignError",
    "InfluentialDimension",
    "PhaseBDesign",
    "dimension_of_alternative",
    "parameter_impacts",
    "select_anchors",
    "select_influential_dimensions",
    "build_phase_b_design",
]

#: D8 — exactly six dimensions vary in Phase B; everything else stays at seed.
INFLUENTIAL_DIMENSION_COUNT = 6

PHASE_B_CANDIDATE_COUNT = 48
PHASE_B_ANCHOR_COUNT = INFLUENTIAL_DIMENSION_COUNT
PHASE_B_LHS_COUNT = PHASE_B_CANDIDATE_COUNT - 1 - PHASE_B_ANCHOR_COUNT  # 41

#: domain separation for the Phase-B stratum permutations.
LHS_DOMAIN = "minos:l2f2-phase-b-lhs:v1"

#: frozen bounded proposal stream. A design that cannot yield 41 valid novel configurations
#: within this many strata FAILS CLOSED rather than silently shrinking the phase.
LHS_PROPOSAL_CEILING = 256

_STRICT = ConfigDict(extra="forbid", frozen=True, strict=True)


class DesignError(MinosEngineError):
    """The Phase-A analysis or Phase-B design cannot be produced under the frozen protocol."""


class InfluentialDimension(BaseModel):
    """One selected live GATK dimension and the impact that selected it."""

    model_config = _STRICT

    name: str = Field(min_length=1)
    impact: float = Field(ge=0.0)
    live_parameter_index: int = Field(ge=0)


class PhaseBDesign(BaseModel):
    """The frozen 48-candidate Phase-B set: seed, six anchors, 41 LHS configurations."""

    model_config = _STRICT

    dimensions: tuple[InfluentialDimension, ...]
    ordered_config_hashes: tuple[str, ...]
    seed_config_hash: str = Field(min_length=64, max_length=64)
    anchor_config_hashes: tuple[str, ...]
    lhs_config_hashes: tuple[str, ...]

    @property
    def candidate_index(self) -> dict[str, int]:
        return {h: i for i, h in enumerate(self.ordered_config_hashes)}


def dimension_of_alternative(alternative: CanonicalConfig, seed: CanonicalConfig) -> str:
    """The single live dimension a one-at-a-time alternative moves away from the seed."""
    differing = sorted(
        name
        for name, value in alternative.effective_config.items()
        if seed.effective_config.get(name) != value
    )
    if len(differing) != 1:
        raise DesignError(
            f"candidate {alternative.config_hash} differs from the seed in {len(differing)} "
            "dimensions; Phase-A alternatives are one-at-a-time by construction"
        )
    return differing[0]


def parameter_impacts(
    *,
    observations: Iterable[BaselineObservation],
    seed_config_hash: str,
    dimension_by_config: Mapping[str, str],
) -> dict[str, float]:
    """Per-dimension Phase-A impact.

    ``delta_i(a) = |u_i(a) - u_i(seed)|`` per member, ``impact(a)`` is its mean over the Phase-A
    members, and ``impact(p)`` is the maximum over the alternatives belonging to ``p``. Utilities
    are the aggregation utilities of :mod:`~minos_engine.baseline.objective`, so a candidate
    failure legitimately reads as a large move away from a good seed.
    """
    by_config: dict[str, dict[str, float]] = {}
    for observation in observations:
        by_config.setdefault(observation.config_hash, {})
        if observation.dataset_id in by_config[observation.config_hash]:
            raise DesignError(
                f"duplicate Phase-A observation for {observation.config_hash} / "
                f"{observation.dataset_id}"
            )
        by_config[observation.config_hash][observation.dataset_id] = observation.utility

    seed_utilities = by_config.get(seed_config_hash)
    if not seed_utilities:
        raise DesignError("Phase-A analysis requires seed observations")

    impacts: dict[str, float] = {}
    for config_hash, utilities in by_config.items():
        if config_hash == seed_config_hash:
            continue
        dimension = dimension_by_config.get(config_hash)
        if dimension is None:
            raise DesignError(f"candidate {config_hash} has no Phase-A dimension")
        shared = sorted(set(utilities) & set(seed_utilities))
        if not shared:
            raise DesignError(f"candidate {config_hash} shares no member with the seed")
        deltas = [abs(utilities[m] - seed_utilities[m]) for m in shared]
        impact = sum(deltas) / len(deltas)
        impacts[dimension] = max(impacts.get(dimension, 0.0), impact)
    return impacts


def select_influential_dimensions(
    impacts: Mapping[str, float], *, count: int = INFLUENTIAL_DIMENSION_COUNT
) -> tuple[InfluentialDimension, ...]:
    """The frozen K=6 rule: descending impact, then live-parameter index, then name.

    The ordering is total, so the selection cannot be nudged after Phase-A scores exist.
    """
    names = live_gatk_parameter_space().names()
    index_of = {name: index for index, name in enumerate(names)}
    for name in impacts:
        if name not in index_of:
            raise DesignError(f"{name!r} is not a live GATK parameter")
    if len(impacts) < count:
        raise DesignError(f"only {len(impacts)} dimensions were screened; {count} are required")
    ordered = sorted(impacts.items(), key=lambda kv: (-kv[1], index_of[kv[0]], kv[0]))
    return tuple(
        InfluentialDimension(name=name, impact=impact, live_parameter_index=index_of[name])
        for name, impact in ordered[:count]
    )


def select_anchors(
    *,
    dimensions: Sequence[InfluentialDimension],
    aggregates: Mapping[str, CandidateAggregate],
    dimension_by_config: Mapping[str, str],
    accepted_index: Mapping[str, int],
) -> tuple[str, ...]:
    """One anchor per selected dimension: its best Phase-A alternative.

    Ordered by higher Phase-A J, lower mean GATK runtime, lower accepted candidate index, then
    lexicographically smaller config_hash — the same total order used everywhere else.
    """
    anchors: list[str] = []
    for dimension in dimensions:
        candidates = [
            config_hash
            for config_hash, name in dimension_by_config.items()
            if name == dimension.name and config_hash in aggregates
        ]
        if not candidates:
            raise DesignError(f"dimension {dimension.name!r} has no Phase-A alternative")
        best = min(
            candidates,
            key=lambda h: (
                -aggregates[h].objective,
                aggregates[h].mean_gatk_runtime_ms,
                accepted_index.get(h, len(accepted_index)),
                h,
            ),
        )
        anchors.append(best)
    if len(set(anchors)) != len(anchors):
        raise DesignError("two dimensions selected the same anchor candidate")
    return tuple(anchors)


def _permutation(name: str, size: int) -> tuple[int, ...]:
    """A deterministic stratum permutation from a domain-separated SHA-256 stream.

    Fisher-Yates driven by ``sha256(LHS_DOMAIN | name | counter)``. No system RNG, no clock, no
    ``hash()``: the permutation is a pure function of the parameter name and the design size.
    """
    order = list(range(size))
    stream = bytearray()
    counter = 0
    for i in range(size - 1, 0, -1):
        while len(stream) < 8:
            block = hashlib.sha256(f"{LHS_DOMAIN}|{name}|{size}|{counter}".encode()).digest()
            stream.extend(block)
            counter += 1
        draw = int.from_bytes(bytes(stream[:8]), "big")
        del stream[:8]
        j = draw % (i + 1)
        order[i], order[j] = order[j], order[i]
    return tuple(order)


def _map_coordinate(parameter: LiveParameter, coordinate: float) -> Any:
    """Map a [0, 1) coordinate into ONE legal value of a live parameter's domain."""
    if parameter.allowed_values is not None:
        values = parameter.allowed_values
        if len(values) == 1:  # a singleton legal domain is fixed and never varies
            return parameter.normalized(values[0])
        index = min(int(coordinate * len(values)), len(values) - 1)
        return parameter.normalized(values[index])
    if parameter.type == "bool":
        return coordinate >= 0.5
    if parameter.minimum is None or parameter.maximum is None:
        raise DesignError(f"parameter {parameter.name!r} has no bounded domain to sample")
    if parameter.type == "int":
        low_i, high_i = int(parameter.minimum), int(parameter.maximum)
        span = high_i - low_i + 1
        return min(low_i + int(coordinate * span), high_i)
    if parameter.type == "float":
        low_f, high_f = float(parameter.minimum), float(parameter.maximum)
        return float(low_f + coordinate * (high_f - low_f))
    raise DesignError(f"unsupported live parameter type {parameter.type!r}")


def build_phase_b_design(
    *,
    dimensions: Sequence[InfluentialDimension],
    seed: CanonicalConfig,
    anchor_config_hashes: Sequence[str],
) -> PhaseBDesign:
    """The frozen 48-candidate Phase-B set. Deterministic, canonical, fail-closed."""
    if len(dimensions) != INFLUENTIAL_DIMENSION_COUNT:
        raise DesignError(
            f"Phase B varies exactly {INFLUENTIAL_DIMENSION_COUNT} dimensions, "
            f"got {len(dimensions)}"
        )
    if len(anchor_config_hashes) != PHASE_B_ANCHOR_COUNT:
        raise DesignError(f"Phase B takes exactly {PHASE_B_ANCHOR_COUNT} anchors")

    space = live_gatk_parameter_space()
    names = [d.name for d in dimensions]
    permutations = {name: _permutation(name, LHS_PROPOSAL_CEILING) for name in names}

    seen: set[str] = {seed.config_hash, *anchor_config_hashes}
    lhs: list[str] = []
    for stratum in range(LHS_PROPOSAL_CEILING):
        if len(lhs) == PHASE_B_LHS_COUNT:
            break
        requested = dict(seed.effective_config)
        for name in names:
            # centred Latin hypercube: one sample per stratum per dimension, midpoint valued.
            coordinate = (permutations[name][stratum] + 0.5) / LHS_PROPOSAL_CEILING
            requested[name] = _map_coordinate(space.get(name), coordinate)
        try:
            config = canonicalize_live_gatk_config(requested)
        except Exception:  # noqa: BLE001 - an invalid proposal is skipped deterministically
            continue
        if config.config_hash in seen:
            continue
        seen.add(config.config_hash)
        lhs.append(config.config_hash)

    if len(lhs) != PHASE_B_LHS_COUNT:
        raise DesignError(
            f"only {len(lhs)} valid novel LHS configurations within the frozen ceiling of "
            f"{LHS_PROPOSAL_CEILING} strata; Phase B requires {PHASE_B_LHS_COUNT} and never "
            "silently shrinks"
        )

    ordered = (seed.config_hash, *anchor_config_hashes, *lhs)
    if len(set(ordered)) != PHASE_B_CANDIDATE_COUNT:
        raise DesignError("the Phase-B design contains a duplicate configuration")
    return PhaseBDesign(
        dimensions=tuple(dimensions),
        ordered_config_hashes=ordered,
        seed_config_hash=seed.config_hash,
        anchor_config_hashes=tuple(anchor_config_hashes),
        lhs_config_hashes=tuple(lhs),
    )


def build_phase_b_configs(
    *,
    dimensions: Sequence[InfluentialDimension],
    seed: CanonicalConfig,
) -> tuple[CanonicalConfig, ...]:
    """The LHS configurations themselves, in design order (seed and anchors excluded)."""
    space = live_gatk_parameter_space()
    names = [d.name for d in dimensions]
    permutations = {name: _permutation(name, LHS_PROPOSAL_CEILING) for name in names}
    seen: set[str] = {seed.config_hash}
    configs: list[CanonicalConfig] = []
    for stratum in range(LHS_PROPOSAL_CEILING):
        if len(configs) == PHASE_B_LHS_COUNT:
            break
        requested = dict(seed.effective_config)
        for name in names:
            coordinate = (permutations[name][stratum] + 0.5) / LHS_PROPOSAL_CEILING
            requested[name] = _map_coordinate(space.get(name), coordinate)
        try:
            config = canonicalize_live_gatk_config(requested)
        except Exception:  # noqa: BLE001 - deterministic skip
            continue
        if config.config_hash in seen:
            continue
        seen.add(config.config_hash)
        configs.append(config)
    return tuple(configs)


_ = BaselineObjectiveError  # the failure type callers see when aggregation refuses an input
