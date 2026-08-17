"""GATK parameter registry: 25 params, states, coupling, deterministic hash."""

from __future__ import annotations

from minos_engine.callers.contracts import ParameterState
from minos_engine.callers.gatk.parameter_registry import REGISTRY, GatkParameterRegistry


def test_exactly_25_parameters():
    assert len(REGISTRY) == 25
    assert len(REGISTRY.names()) == 25


def test_protocol_params_fixed_rest_experimental():
    fixed = {p.name for p in REGISTRY.all() if p.state is ParameterState.FIXED}
    assert fixed == {"emit_ref_confidence", "sample_ploidy"}
    for p in REGISTRY.all():
        if p.state is not ParameterState.FIXED:
            assert p.state is ParameterState.EXPERIMENTAL
        # No parameter is live ACTIVE merely for having a legal range.
        assert p.state is not ParameterState.ACTIVE


def test_changeable_matches_fixed():
    for p in REGISTRY.all():
        assert p.changeable == (p.state is not ParameterState.FIXED)
        assert p.runtime_adaptive is False


def test_assembly_coupling_present_and_defaults_satisfy():
    lo = REGISTRY.get("min_assembly_region_size")
    hi = REGISTRY.get("max_assembly_region_size")
    assert "min_assembly_region_size < max_assembly_region_size" in lo.coupling_rules
    assert "min_assembly_region_size < max_assembly_region_size" in hi.coupling_rules
    assert lo.official_default < hi.official_default


def test_registry_hash_deterministic():
    assert REGISTRY.registry_hash() == GatkParameterRegistry().registry_hash()
    assert len(REGISTRY.registry_hash()) == 64


def test_documented_parameter_space_roundtrips():
    ps = REGISTRY.documented_parameter_space(retrieved_at="2026-08-17T00:00:00+00:00")
    assert ps.caller == "gatk"
    assert len(ps.parameters) == 25
    # hash is stable for identical content
    ps2 = REGISTRY.documented_parameter_space(retrieved_at="2027-01-01T00:00:00+00:00")
    assert ps.parameter_space_hash == ps2.parameter_space_hash  # fetch time excluded
