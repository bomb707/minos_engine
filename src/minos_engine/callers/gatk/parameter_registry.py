"""Canonical GATK HaplotypeCaller parameter registry (25 parameters).

Ranges/defaults are the DOCUMENTED snapshot from the Layer 2 spec §6, reviewed
2026-08-09. These are the *static documented* legal ranges. At runtime a
versioned :class:`ParameterSpaceSnapshot` may override them (see
``callers.gatk.config``); the static registry is the fallback and the source of
types, defaults, control groups, states, and coupling rules.

Initial states are conservative (assignment §7):
  * protocol-determined parameters -> FIXED (``emit_ref_confidence``,
    ``sample_ploidy``);
  * all others -> EXPERIMENTAL (never live ACTIVE merely for having a range).
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from minos_engine.callers.contracts import (
    ACTIVE_CALLER,
    ControlGroup,
    ParameterRange,
    ParameterSpaceSnapshot,
    ParameterState,
    ParameterType,
)
from minos_engine.common.errors import ConfigValidationError
from minos_engine.common.hashing import canonical_hash

__all__ = [
    "GatkParameter",
    "GatkParameterRegistry",
    "REGISTRY",
    "SPEC_SOURCE",
    "SPEC_SOURCE_VERSION",
]

SPEC_SOURCE = "MINOS_ENGINE_Layer2_Exact_Build_Specification_v2 §6"
SPEC_SOURCE_VERSION = "documented-2026-08-09"

# Cross-parameter coupling rule shared by the active-region size pair.
_ASSEMBLY_COUPLING = ("min_assembly_region_size < max_assembly_region_size",)


class GatkParameter(BaseModel):
    """One registry entry with full provenance and activation metadata."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(min_length=1)
    type: ParameterType
    official_default: Any
    documented_min: float | int | None = None
    documented_max: float | int | None = None
    enum_values: tuple[str, ...] | None = None
    control_group: ControlGroup
    state: ParameterState
    changeable: bool
    runtime_adaptive: bool
    coupling_rules: tuple[str, ...] = ()
    source: str = SPEC_SOURCE
    source_version: str = SPEC_SOURCE_VERSION

    def documented_range(self) -> ParameterRange:
        return ParameterRange(
            type=self.type,
            minimum=self.documented_min,
            maximum=self.documented_max,
            enum_values=self.enum_values,
            default=self.official_default,
        )


def _p(
    name: str,
    ptype: ParameterType,
    default: Any,
    group: ControlGroup,
    *,
    minimum: float | int | None = None,
    maximum: float | int | None = None,
    enum_values: tuple[str, ...] | None = None,
    fixed: bool = False,
    coupling: tuple[str, ...] = (),
) -> GatkParameter:
    state = ParameterState.FIXED if fixed else ParameterState.EXPERIMENTAL
    return GatkParameter(
        name=name,
        type=ptype,
        official_default=default,
        documented_min=minimum,
        documented_max=maximum,
        enum_values=enum_values,
        control_group=group,
        state=state,
        changeable=not fixed,
        runtime_adaptive=False,
        coupling_rules=coupling,
    )


_I = ParameterType.INT
_F = ParameterType.FLOAT
_B = ParameterType.BOOL
_E = ParameterType.ENUM
_G = ControlGroup

_PARAMETERS: tuple[GatkParameter, ...] = (
    _p("min_base_quality_score", _I, 10, _G.EVIDENCE_FILTERS, minimum=10, maximum=50),
    _p("min_mapping_quality_score", _I, 20, _G.EVIDENCE_FILTERS, minimum=0, maximum=60),
    _p("base_quality_score_threshold", _I, 18, _G.EVIDENCE_FILTERS, minimum=0, maximum=50),
    _p(
        "standard_min_confidence_threshold_for_calling",
        _F,
        30.0,
        _G.EVIDENCE_FILTERS,
        minimum=10.0,
        maximum=100.0,
    ),
    _p(
        "emit_ref_confidence",
        _E,
        "NONE",
        _G.PROTOCOL,
        enum_values=("NONE", "GVCF", "BP_RESOLUTION"),
        fixed=True,
    ),
    _p(
        "pcr_indel_model",
        _E,
        "CONSERVATIVE",
        _G.LIBRARY,
        enum_values=("NONE", "HOSTILE", "AGGRESSIVE", "CONSERVATIVE"),
    ),
    _p("min_pruning", _I, 2, _G.ASSEMBLY_GRAPH, minimum=2, maximum=10),
    _p("max_alternate_alleles", _I, 6, _G.ASSEMBLY_GRAPH, minimum=1, maximum=20),
    _p("min_dangling_branch_length", _I, 4, _G.ASSEMBLY_GRAPH, minimum=2, maximum=20),
    _p("recover_all_dangling_branches", _B, False, _G.ASSEMBLY_GRAPH),
    _p("max_num_haplotypes_in_population", _I, 128, _G.ASSEMBLY_GRAPH, minimum=8, maximum=128),
    _p(
        "adaptive_pruning_initial_error_rate",
        _F,
        0.001,
        _G.ASSEMBLY_GRAPH,
        minimum=0.0001,
        maximum=0.1,
    ),
    _p("pruning_lod_threshold", _F, 2.302585, _G.ASSEMBLY_GRAPH, minimum=0.5, maximum=10.0),
    _p("active_probability_threshold", _F, 0.002, _G.ACTIVE_REGION, minimum=0.001, maximum=0.05),
    _p(
        "min_assembly_region_size",
        _I,
        50,
        _G.ACTIVE_REGION,
        minimum=1,
        maximum=300,
        coupling=_ASSEMBLY_COUPLING,
    ),
    _p(
        "max_assembly_region_size",
        _I,
        300,
        _G.ACTIVE_REGION,
        minimum=100,
        maximum=700,
        coupling=_ASSEMBLY_COUPLING,
    ),
    _p("assembly_region_padding", _I, 100, _G.ACTIVE_REGION, minimum=0, maximum=500),
    _p("pair_hmm_gap_continuation_penalty", _I, 10, _G.LIKELIHOOD, minimum=1, maximum=30),
    _p(
        "phred_scaled_global_read_mismapping_rate",
        _I,
        45,
        _G.LIKELIHOOD,
        minimum=10,
        maximum=60,
    ),
    _p("heterozygosity", _F, 0.001, _G.PRIOR, minimum=0.0001, maximum=0.01),
    _p("indel_heterozygosity", _F, 0.000125, _G.PRIOR, minimum=0.00001, maximum=0.001),
    _p("sample_ploidy", _I, 2, _G.PROTOCOL, minimum=1, maximum=10, fixed=True),
    _p("contamination_fraction_to_filter", _F, 0.0, _G.EVIDENCE, minimum=0.0, maximum=0.5),
    _p("max_reads_per_alignment_start", _I, 50, _G.READ_RETENTION, minimum=25, maximum=300),
    _p("dont_use_soft_clipped_bases", _B, False, _G.READ_RETENTION),
)


class GatkParameterRegistry:
    """Immutable lookup over the 25 canonical GATK parameters."""

    def __init__(self, parameters: tuple[GatkParameter, ...] = _PARAMETERS) -> None:
        self._params: dict[str, GatkParameter] = {p.name: p for p in parameters}
        if len(self._params) != len(parameters):
            raise ConfigValidationError("duplicate parameter name in GATK registry")
        self._validate_static_coupling()

    def _validate_static_coupling(self) -> None:
        # The documented defaults must themselves satisfy the coupling rule.
        lo = self._params["min_assembly_region_size"].official_default
        hi = self._params["max_assembly_region_size"].official_default
        if not (lo < hi):
            raise ConfigValidationError(
                f"registry defaults violate coupling: min_assembly_region_size "
                f"({lo}) !< max_assembly_region_size ({hi})"
            )

    def __len__(self) -> int:
        return len(self._params)

    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self._params))

    def get(self, name: str) -> GatkParameter:
        try:
            return self._params[name]
        except KeyError as exc:
            raise ConfigValidationError(f"unknown GATK parameter: {name!r}") from exc

    def all(self) -> tuple[GatkParameter, ...]:
        return tuple(self._params[name] for name in self.names())

    def defaults(self) -> dict[str, Any]:
        return {name: self._params[name].official_default for name in self.names()}

    def registry_hash(self) -> str:
        payload = [p.model_dump(mode="json") for p in self.all()]
        return canonical_hash(payload)

    def documented_parameter_space(
        self, *, source: str = SPEC_SOURCE, retrieved_at: str, stale: bool = False
    ) -> ParameterSpaceSnapshot:
        """Build a ParameterSpaceSnapshot from the documented static ranges."""
        ranges = {name: self._params[name].documented_range() for name in self.names()}
        phash = ParameterSpaceSnapshot.compute_hash(ACTIVE_CALLER, ranges)
        return ParameterSpaceSnapshot(
            caller=ACTIVE_CALLER,
            parameters=ranges,
            source=source,
            retrieved_at=retrieved_at,
            parameter_space_hash=phash,
            stale=stale,
        )


REGISTRY = GatkParameterRegistry()
