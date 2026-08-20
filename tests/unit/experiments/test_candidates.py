"""L2-F deterministic candidate generation + policy identity (unit)."""

from __future__ import annotations

import pytest

from minos_engine.callers.contracts import ParameterState
from minos_engine.callers.gatk.parameter_registry import REGISTRY, GatkParameterRegistry
from minos_engine.common.errors import ConfigValidationError
from minos_engine.experiments.candidates import (
    CandidateSetVerificationError,
    _verify_candidate_set_against_registry,
    candidate_set_hash,
    generate_accepted_candidate_set,
    generate_candidate_set,
    verify_accepted_candidate_set,
)
from minos_engine.experiments.policy import (
    EXPLORABLE_REGISTRY_STATE,
    GENERATION_POLICY_VERSION,
    build_experiment_parameter_policy,
    experiment_seed_v1,
)

_FIXED = {"emit_ref_confidence", "sample_ploidy"}


def test_registry_states_unchanged_no_active_promotion() -> None:
    states = [p.state for p in REGISTRY.all()]
    assert sum(s is ParameterState.FIXED for s in states) == 2
    assert sum(s is ParameterState.EXPERIMENTAL for s in states) == 23
    assert sum(s is ParameterState.ACTIVE for s in states) == 0
    assert EXPLORABLE_REGISTRY_STATE == "EXPERIMENTAL"


def test_generation_is_deterministic() -> None:
    a, b = generate_candidate_set(), generate_candidate_set()
    assert a.candidate_set_hash == b.candidate_set_hash
    assert a.ordered_config_hashes == b.ordered_config_hashes
    assert a.policy.generation_policy_version == GENERATION_POLICY_VERSION


def test_seed_first_unique_and_derived_count() -> None:
    cs = generate_candidate_set()
    seed = experiment_seed_v1()
    assert cs.ordered_config_hashes[0] == seed.config_hash  # seed first
    assert len(set(cs.ordered_config_hashes)) == cs.candidate_count  # unique / dedup
    assert cs.candidate_count >= 1  # derived, not hardcoded


def test_one_factor_at_a_time() -> None:
    cs = generate_candidate_set()
    seed = experiment_seed_v1()
    for cand in cs.configs[1:]:
        diff = [k for k in cand.effective_config if cand.effective_config[k] != seed.effective_config[k]]
        assert len(diff) == 1  # exactly one EXPERIMENTAL parameter changed
        assert diff[0] not in _FIXED  # never a FIXED parameter


def test_fixed_parameters_never_varied() -> None:
    cs = generate_candidate_set()
    seed = experiment_seed_v1()
    for cand in cs.configs:
        for f in _FIXED:
            assert cand.effective_config[f] == seed.effective_config[f]


def test_invalid_coupling_probe_deterministically_omitted() -> None:
    cs = generate_candidate_set()
    # min_assembly_region_size=300 violates 300 < max(default 300); it is skipped, not replaced.
    skipped = {(s.parameter, s.value) for s in cs.skipped}
    assert ("min_assembly_region_size", 300) in skipped
    # no candidate carries an invalid coupling
    for cand in cs.configs:
        assert cand.effective_config["min_assembly_region_size"] < cand.effective_config["max_assembly_region_size"]


def test_honest_accepted_candidate_set_verifies() -> None:
    assert all(verify_accepted_candidate_set(generate_accepted_candidate_set()).values())


def _forged_registry_and_set() -> tuple[GatkParameterRegistry, object]:
    """Clone the registry, alter ONE EXPERIMENTAL documented bound, and generate a fully
    self-consistent candidate set under that forged registry (all hashes recomputed)."""
    forged_params = tuple(
        p.model_copy(update={"documented_max": 49}) if p.name == "min_base_quality_score" else p
        for p in REGISTRY.all()
    )
    forged_registry = GatkParameterRegistry(forged_params)
    forged_set = generate_candidate_set(forged_registry)  # internally self-consistent
    return forged_registry, forged_set


def test_genuine_consistent_rehash_forgery_rejected_by_accepted_verifier() -> None:
    """A1/A4: a fully internally-consistent forged set (registry cloned + one bound
    altered + ALL dependent hashes recomputed) is rejected by the ACCEPTED no-override
    verifier, which independently derives the repository-owned registry."""
    forged_registry, forged_set = _forged_registry_and_set()
    # the forgery really is internally consistent under its own (forged) registry:
    assert all(_verify_candidate_set_against_registry(forged_set, forged_registry).values())
    # but the accepted verifier (repository registry, no override) rejects it:
    with pytest.raises(CandidateSetVerificationError):
        verify_accepted_candidate_set(forged_set)


def test_caller_cannot_make_forged_registry_authoritative() -> None:
    """A1 regression: there is no override on the accepted verifier/builder — a caller
    cannot substitute a forged registry into the accepted trust boundary."""
    import inspect

    from minos_engine.experiments import candidates as C

    assert set(inspect.signature(C.verify_accepted_candidate_set).parameters) == {"candidate_set"}
    assert inspect.signature(C.generate_accepted_candidate_set).parameters == {}
    # the forged set differs from the accepted set (proves the registry actually mattered)
    _, forged_set = _forged_registry_and_set()
    assert forged_set.candidate_set_hash != generate_accepted_candidate_set().candidate_set_hash


def test_policy_nested_content_immutable() -> None:
    """A2: the frozen policy exposes no mutable mapping after its hash is computed."""
    pol = generate_accepted_candidate_set().policy
    assert isinstance(pol.generation_mechanics, tuple)
    with pytest.raises((TypeError, AttributeError)):
        pol.generation_mechanics[0] = ("x", "y")  # type: ignore[index]
    with pytest.raises((AttributeError, Exception)):
        pol.registry_hash = "0" * 64  # type: ignore[misc]  # frozen dataclass


def test_config_tamper_rejected_by_canonicalizer() -> None:
    from minos_engine.callers.gatk.config import canonicalize_config
    from minos_engine.experiments.policy import documented_parameter_space

    space = documented_parameter_space()
    with pytest.raises(ConfigValidationError):
        canonicalize_config({"not_a_real_param": 1}, parameter_space=space)  # unknown param
    with pytest.raises(ConfigValidationError):
        canonicalize_config({"min_base_quality_score": 9999}, parameter_space=space)  # out of bounds
    with pytest.raises(ConfigValidationError):
        canonicalize_config({"sample_ploidy": 4}, parameter_space=space)  # FIXED changed


def test_policy_hash_deterministic_and_binds_ordered_hashes() -> None:
    pol = build_experiment_parameter_policy()
    assert len(pol.experiment_parameter_policy_hash) == 64
    truth = generate_candidate_set()
    # candidate_set_hash is a pure function of policy identities + ordered config hashes:
    # recomputing over the same inputs reproduces it; changing the order changes it.
    same = candidate_set_hash(
        policy=truth.policy, ordered_config_hashes=truth.ordered_config_hashes
    )
    assert same == truth.candidate_set_hash
    reordered = (
        truth.ordered_config_hashes[1],
        truth.ordered_config_hashes[0],
        *truth.ordered_config_hashes[2:],
    )
    assert (
        candidate_set_hash(policy=truth.policy, ordered_config_hashes=reordered)
        != truth.candidate_set_hash
    )
