"""GATK CONFIG canonicalization and validation."""

from __future__ import annotations

import pytest

from minos_engine.callers.contracts import ParameterState, ParameterType
from minos_engine.callers.gatk.config import canonicalize_config
from minos_engine.callers.gatk.parameter_registry import REGISTRY
from minos_engine.common.errors import ConfigValidationError

# The assembly-region size pair is coupled (min < max); sweeping one to its
# extreme collides with the other's default, so they are tested explicitly.
_COUPLED = {"min_assembly_region_size", "max_assembly_region_size"}

# Numeric (non-fixed, non-coupled) parameters with documented bounds.
_NUMERIC = [
    p
    for p in REGISTRY.all()
    if p.type in (ParameterType.INT, ParameterType.FLOAT)
    and p.state is not ParameterState.FIXED
    and p.documented_min is not None
    and p.documented_max is not None
    and p.name not in _COUPLED
]


def test_default_config_is_legal():
    cc = canonicalize_config({})
    assert len(cc.effective_config) == 25
    assert len(cc.config_hash) == 64


@pytest.mark.parametrize("p", _NUMERIC, ids=lambda p: p.name)
def test_min_and_max_boundaries_accepted(p):
    canonicalize_config({p.name: p.documented_min})
    canonicalize_config({p.name: p.documented_max})


@pytest.mark.parametrize("p", _NUMERIC, ids=lambda p: p.name)
def test_below_min_and_above_max_rejected(p):
    step = 1 if p.type is ParameterType.INT else p.documented_min / 10 or 1e-9
    with pytest.raises(ConfigValidationError):
        canonicalize_config({p.name: p.documented_min - step})
    with pytest.raises(ConfigValidationError):
        canonicalize_config({p.name: p.documented_max + step})


def test_invalid_enum_rejected():
    with pytest.raises(ConfigValidationError):
        canonicalize_config({"pcr_indel_model": "BOGUS"})


def test_valid_enum_accepted():
    cc = canonicalize_config({"pcr_indel_model": "AGGRESSIVE"})
    assert cc.effective_config["pcr_indel_model"] == "AGGRESSIVE"


def test_wrong_json_types_rejected():
    with pytest.raises(ConfigValidationError):
        canonicalize_config({"min_pruning": "5"})  # no str->number
    with pytest.raises(ConfigValidationError):
        canonicalize_config({"min_pruning": True})  # bool is not int
    with pytest.raises(ConfigValidationError):
        canonicalize_config({"min_pruning": 5.0})  # float is not int
    with pytest.raises(ConfigValidationError):
        canonicalize_config({"dont_use_soft_clipped_bases": 1})  # int is not bool


def test_float_accepts_int_value():
    cc = canonicalize_config({"standard_min_confidence_threshold_for_calling": 40})
    assert cc.effective_config["standard_min_confidence_threshold_for_calling"] == 40.0


def test_unknown_parameter_rejected():
    with pytest.raises(ConfigValidationError):
        canonicalize_config({"totally_unknown": 1})


def test_fixed_parameter_cannot_change():
    with pytest.raises(ConfigValidationError):
        canonicalize_config({"sample_ploidy": 3})
    with pytest.raises(ConfigValidationError):
        canonicalize_config({"emit_ref_confidence": "GVCF"})
    # equal-to-default is accepted
    canonicalize_config({"sample_ploidy": 2})


def test_assembly_region_relationship_violation():
    with pytest.raises(ConfigValidationError):
        canonicalize_config({"min_assembly_region_size": 300, "max_assembly_region_size": 100})


def test_coupled_assembly_boundaries_with_compatible_partner():
    # Each coupled param at its documented extreme, paired so min < max holds.
    canonicalize_config({"min_assembly_region_size": 1, "max_assembly_region_size": 100})
    canonicalize_config({"min_assembly_region_size": 299, "max_assembly_region_size": 700})
    # Below/above the documented bounds still rejected.
    with pytest.raises(ConfigValidationError):
        canonicalize_config({"min_assembly_region_size": 0})
    with pytest.raises(ConfigValidationError):
        canonicalize_config({"max_assembly_region_size": 701})


def test_stable_hash_and_key_order_equivalence():
    a = canonicalize_config({"min_pruning": 5, "max_alternate_alleles": 4})
    b = canonicalize_config({"max_alternate_alleles": 4, "min_pruning": 5})
    assert a.config_hash == b.config_hash
    assert a.effective_bytes() == b.effective_bytes()
    assert canonicalize_config({}).config_hash == canonicalize_config({}).config_hash
