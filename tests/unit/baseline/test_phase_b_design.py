"""The Phase-B design, its identity and its verifier — pure, no database.

Phase B inherits everything scientific from a completed Phase A, so the controls here are about
one question: can any of it be nominated, substituted or quietly reshaped? The answers must all be
no — including for the two anchors Phase A left as total failures, which are preserved on purpose.
"""

from __future__ import annotations

import pytest

from minos_engine.baseline.design import (
    INFLUENTIAL_DIMENSION_COUNT,
    InfluentialDimension,
    build_phase_b_design,
    dimension_of_alternative,
    phase_b_candidate_configs,
    select_anchors,
    select_influential_dimensions,
)
from minos_engine.baseline.objective import CandidateAggregate
from minos_engine.baseline.phase_b import (
    PHASE_B_ANCHOR_COUNT,
    PHASE_B_BATCH_COUNT,
    PHASE_B_BATCH_SIZE,
    PHASE_B_CANDIDATE_COUNT,
    PHASE_B_CANDIDATE_SET_DOMAIN,
    PHASE_B_LHS_COUNT,
    PHASE_B_LOGICAL_JOB_COUNT,
    PHASE_B_MEMBER_COUNT,
    PhaseBError,
    compute_phase_b_candidate_set_hash,
    verify_phase_b_candidates,
)
from minos_engine.experiments.candidates import generate_accepted_candidate_set

_H = {c: c * 64 for c in "0123456789abcdef"}


def _seed():
    return generate_accepted_candidate_set().configs[0]


def _dimensions_and_anchors():
    """Six dimensions and six anchors derived from the ACCEPTED candidate set, deterministically.

    Impacts are synthetic; the anchors are real accepted alternatives, one per dimension, which is
    the shape the frozen selector produces.
    """
    cs = generate_accepted_candidate_set()
    seed = cs.configs[0]
    by_dimension: dict[str, str] = {}
    for config in cs.configs[1:]:
        by_dimension.setdefault(dimension_of_alternative(config, seed), config.config_hash)
    names = sorted(by_dimension)[:INFLUENTIAL_DIMENSION_COUNT]
    from minos_engine.experiments.gatk_live_space import live_gatk_parameter_space

    order = {n: i for i, n in enumerate(live_gatk_parameter_space().names())}
    dimensions = tuple(
        InfluentialDimension(name=n, impact=1.0 - i / 100, live_parameter_index=order[n])
        for i, n in enumerate(names)
    )
    return dimensions, tuple(by_dimension[n] for n in names), seed


def _design():
    dimensions, anchors, seed = _dimensions_and_anchors()
    return build_phase_b_design(
        dimensions=dimensions, seed=seed, anchor_config_hashes=anchors
    ), seed


# --------------------------------------------------------------------------- #
# the design and its configurations are ONE sequence
# --------------------------------------------------------------------------- #
def test_the_reconstructed_configs_reproduce_the_design_exactly() -> None:
    """A design naming configurations nobody can rebuild would be unexecutable.

    The hashes and the payloads come from one generator, so they cannot drift — the case that used
    to be able to diverge is an LHS proposal colliding with an anchor, which one caller skipped
    and the other would have kept.
    """
    design, seed = _design()
    cs = generate_accepted_candidate_set()
    by_hash = {c.config_hash: c for c in cs.configs}

    configs = phase_b_candidate_configs(
        design=design, seed=seed, anchors={h: by_hash[h] for h in design.anchor_config_hashes}
    )

    assert len(configs) == PHASE_B_CANDIDATE_COUNT == 48
    assert tuple(c.config_hash for c in configs) == design.ordered_config_hashes
    assert configs[0].config_hash == design.seed_config_hash
    assert tuple(c.config_hash for c in configs[1:7]) == design.anchor_config_hashes
    assert len(configs[7:]) == PHASE_B_LHS_COUNT == 41


def test_the_reconstruction_refuses_an_anchor_it_was_not_given() -> None:
    design, seed = _design()
    with pytest.raises(Exception, match="anchor"):
        phase_b_candidate_configs(design=design, seed=seed, anchors={})


def test_the_reconstruction_refuses_a_substituted_anchor_payload() -> None:
    """An anchor is a Phase-A candidate; it is never regenerated or swapped."""
    design, seed = _design()
    cs = generate_accepted_candidate_set()
    by_hash = {c.config_hash: c for c in cs.configs}
    anchors = {h: by_hash[h] for h in design.anchor_config_hashes}
    first = design.anchor_config_hashes[0]
    anchors[first] = cs.configs[-1]  # a different real config under the right key

    with pytest.raises(Exception, match="is not"):
        phase_b_candidate_configs(design=design, seed=seed, anchors=anchors)


# --------------------------------------------------------------------------- #
# the verifier
# --------------------------------------------------------------------------- #
def test_the_verifier_accepts_the_real_design() -> None:
    design, seed = _design()
    cs = generate_accepted_candidate_set()
    by_hash = {c.config_hash: c for c in cs.configs}
    configs = phase_b_candidate_configs(
        design=design, seed=seed, anchors={h: by_hash[h] for h in design.anchor_config_hashes}
    )

    verify_phase_b_candidates(configs, design=design, seed=seed)  # must not raise

    selected = {d.name for d in design.dimensions}
    for config in configs[7:]:
        moved = {
            name
            for name, value in config.effective_config.items()
            if seed.effective_config.get(name) != value
        }
        assert moved <= selected, "an LHS config moved a dimension Phase A did not select"


@pytest.mark.parametrize("drop", [0, 1, 7, 47])
def test_the_verifier_refuses_a_reordered_or_short_set(drop: int) -> None:
    design, seed = _design()
    cs = generate_accepted_candidate_set()
    by_hash = {c.config_hash: c for c in cs.configs}
    configs = phase_b_candidate_configs(
        design=design, seed=seed, anchors={h: by_hash[h] for h in design.anchor_config_hashes}
    )
    mutilated = tuple(c for i, c in enumerate(configs) if i != drop)

    with pytest.raises(PhaseBError):
        verify_phase_b_candidates(mutilated, design=design, seed=seed)


def test_the_verifier_refuses_a_swapped_seed_position() -> None:
    design, seed = _design()
    cs = generate_accepted_candidate_set()
    by_hash = {c.config_hash: c for c in cs.configs}
    configs = list(
        phase_b_candidate_configs(
            design=design, seed=seed, anchors={h: by_hash[h] for h in design.anchor_config_hashes}
        )
    )
    configs[0], configs[1] = configs[1], configs[0]

    with pytest.raises(PhaseBError):
        verify_phase_b_candidates(tuple(configs), design=design, seed=seed)


# --------------------------------------------------------------------------- #
# failed anchors are preserved
# --------------------------------------------------------------------------- #
def test_a_total_failure_alternative_is_still_selected_as_its_dimension_s_anchor() -> None:
    """THE rule that must not be softened after seeing Phase-A data.

    Impact measures SENSITIVITY. A knob whose alternative destroys the score is exactly a knob
    worth exploring, so the frozen selector takes it as that dimension's anchor even though its
    objective is the worst possible value. Filtering it out — or demanding an anchor beat the seed
    — would be choosing the design from the data it is supposed to be independent of.
    """
    cs = generate_accepted_candidate_set()
    seed = cs.configs[0]
    alternative = cs.configs[1]
    dimension = dimension_of_alternative(alternative, seed)
    from minos_engine.experiments.gatk_live_space import live_gatk_parameter_space

    index = live_gatk_parameter_space().names().index(dimension)
    dimensions = (InfluentialDimension(name=dimension, impact=0.53, live_parameter_index=index),)

    total_failure = CandidateAggregate(
        config_hash=alternative.config_hash,
        observed_count=5,
        required_count=5,
        failure_count=5,
        failure_rate=1.0,
        mean=0.0,
        cvar=0.0,
        floor=0.0,
        mean_gatk_runtime_ms=3165.0,
        objective=-1.0,
        infrastructure_incident_count=0,
    )
    anchors = select_anchors(
        dimensions=dimensions,
        aggregates={alternative.config_hash: total_failure},
        dimension_by_config={alternative.config_hash: dimension},
        accepted_index={c.config_hash: i for i, c in enumerate(cs.configs)},
    )

    assert anchors == (alternative.config_hash,)
    assert total_failure.objective == -1.0, "the anchor really is a total failure"


def test_the_selector_ranks_by_impact_not_by_desirability() -> None:
    """A dimension whose only alternative failed has a LARGE impact and is selected for it."""
    impacts = {
        "min_base_quality_score": 0.532289,  # its alternative scored zero everywhere
        "contamination_fraction_to_filter": 0.048754,
        "heterozygosity": 0.001047,
        "indel_heterozygosity": 0.000011,
        "assembly_region_padding": 0.002719,
        "max_alternate_alleles": 0.013364,
        "min_pruning": 0.165535,
    }
    selected = select_influential_dimensions(impacts)

    assert len(selected) == INFLUENTIAL_DIMENSION_COUNT
    assert selected[0].name == "min_base_quality_score"
    assert [d.impact for d in selected] == sorted((d.impact for d in selected), reverse=True)


# --------------------------------------------------------------------------- #
# the candidate-set identity
# --------------------------------------------------------------------------- #
def _identity(design, **over):
    kwargs = {
        "protocol_hash": _H["c"],
        "source_phase_a_plan_hash": _H["9"],
        "phase_a_analysis_hash": _H["a"],
        "parameter_space_hash": _H["b"],
        "experiment_parameter_policy_hash": _H["d"],
        "design": design,
    }
    kwargs.update(over)
    return compute_phase_b_candidate_set_hash(**kwargs)


def test_the_candidate_set_identity_is_deterministic_and_domain_separated() -> None:
    design, _seed = _design()
    assert _identity(design) == _identity(design)
    assert PHASE_B_CANDIDATE_SET_DOMAIN.startswith("minos:l2f2-phase-b-candidate-set:")

    import hashlib

    from minos_engine.common.canonical_json import canonical_json_bytes

    undomained = hashlib.sha256(
        canonical_json_bytes({"ordered_config_hashes": list(design.ordered_config_hashes)})
    ).hexdigest()
    assert _identity(design) != undomained


@pytest.mark.parametrize(
    "field",
    [
        "protocol_hash",
        "source_phase_a_plan_hash",
        "phase_a_analysis_hash",
        "parameter_space_hash",
        "experiment_parameter_policy_hash",
    ],
)
def test_every_bound_input_moves_the_candidate_set_identity(field: str) -> None:
    """If an input could change without moving the hash, the identity would be decorative."""
    design, _seed = _design()
    assert _identity(design, **{field: _H["e"]}) != _identity(design)


def test_the_candidate_set_identity_is_not_the_phase_a_candidate_set_identity() -> None:
    """Phase B's 48 were analysed into existence; Phase A's 39 were probed into existence.

    Reusing Phase A's identity would claim these configurations were generated the way those were.
    """
    design, _seed = _design()
    assert _identity(design) != generate_accepted_candidate_set().candidate_set_hash


# --------------------------------------------------------------------------- #
# the frozen shape
# --------------------------------------------------------------------------- #
def test_the_phase_b_constants_are_the_frozen_ones() -> None:
    assert PHASE_B_CANDIDATE_COUNT == 48
    assert PHASE_B_ANCHOR_COUNT == 6
    assert PHASE_B_LHS_COUNT == 41
    assert 1 + PHASE_B_ANCHOR_COUNT + PHASE_B_LHS_COUNT == PHASE_B_CANDIDATE_COUNT
    assert PHASE_B_BATCH_COUNT == 2
    assert PHASE_B_BATCH_SIZE == 5
    assert PHASE_B_MEMBER_COUNT == 10
    assert PHASE_B_LOGICAL_JOB_COUNT == 480 == PHASE_B_CANDIDATE_COUNT * PHASE_B_MEMBER_COUNT
