"""Phase-A sensitivity and the frozen Phase-B design, on SYNTHETIC observations only.

No real score is used anywhere: every utility here is fabricated by the test so the selection
and generation rules can be exercised before any evaluation exists.
"""

from __future__ import annotations

import os
from collections import Counter
from typing import Any

import pytest

from minos_engine.baseline.design import (
    INFLUENTIAL_DIMENSION_COUNT,
    LHS_PROPOSAL_CEILING,
    PHASE_B_ANCHOR_COUNT,
    PHASE_B_CANDIDATE_COUNT,
    PHASE_B_LHS_COUNT,
    DesignError,
    build_phase_b_configs,
    build_phase_b_design,
    dimension_of_alternative,
    parameter_impacts,
    select_anchors,
    select_influential_dimensions,
)
from minos_engine.baseline.objective import BaselineObservation, aggregate_candidate
from minos_engine.experiments.candidates import generate_accepted_candidate_set
from minos_engine.experiments.gatk_live_space import live_gatk_parameter_space

_MEMBERS = tuple((f"minos-{c}-0", c) for c in ("chr18", "chr19", "chr20", "chr21", "chr22"))


def _candidate_world() -> tuple[Any, Any, dict[str, str], dict[str, int]]:
    candidate_set = generate_accepted_candidate_set()
    seed = next(c for c in candidate_set.configs if c.config_hash == candidate_set.seed_config_hash)
    dimension_by_config = {
        c.config_hash: dimension_of_alternative(c, seed)
        for c in candidate_set.configs
        if c.config_hash != seed.config_hash
    }
    accepted_index = {h: i for i, h in enumerate(candidate_set.ordered_config_hashes)}
    return candidate_set, seed, dimension_by_config, accepted_index


def _synthetic_observations(
    seed_hash: str, dimension_by_config: dict[str, str]
) -> list[BaselineObservation]:
    """Seed scores 0.60 everywhere; each alternative is offset by a deterministic amount."""
    names = live_gatk_parameter_space().names()
    observations = [
        BaselineObservation(
            config_hash=seed_hash,
            dataset_id=d,
            chromosome=c,
            minos_score=0.60,
            admitted=True,
            gatk_runtime_ms=1000,
        )
        for d, c in _MEMBERS
    ]
    for config_hash, dimension in sorted(dimension_by_config.items()):
        offset = (len(names) - names.index(dimension)) / 100.0
        for d, c in _MEMBERS:
            observations.append(
                BaselineObservation(
                    config_hash=config_hash,
                    dataset_id=d,
                    chromosome=c,
                    minos_score=min(1.0, 0.60 + offset),
                    admitted=True,
                    gatk_runtime_ms=1000 + names.index(dimension),
                )
            )
    return observations


# --------------------------------------------------------------------------- #
# the Phase-A authority is untouched
# --------------------------------------------------------------------------- #
def test_the_phase_a_candidate_authority_is_unchanged() -> None:
    candidate_set = generate_accepted_candidate_set()
    assert candidate_set.candidate_count == 39
    assert candidate_set.candidate_set_hash == (
        "50d5f36918758de204e4b34cdd3fc8560a14debfcdb25869f713690c6085057d"
    )


def test_every_phase_a_alternative_moves_exactly_one_dimension() -> None:
    _cs, seed, dimension_by_config, _index = _candidate_world()
    assert len(dimension_by_config) == 38, "39 candidates = seed + 38 alternatives"
    counts = Counter(dimension_by_config.values())
    assert all(v >= 1 for v in counts.values())
    assert set(counts) <= set(live_gatk_parameter_space().names())
    _ = seed


# --------------------------------------------------------------------------- #
# impact and the K = 6 rule
# --------------------------------------------------------------------------- #
def test_impact_is_the_mean_absolute_move_from_the_seed() -> None:
    _cs, seed, dimension_by_config, _index = _candidate_world()
    observations = _synthetic_observations(seed.config_hash, dimension_by_config)
    impacts = parameter_impacts(
        observations=observations,
        seed_config_hash=seed.config_hash,
        dimension_by_config=dimension_by_config,
    )
    names = live_gatk_parameter_space().names()
    for dimension, impact in impacts.items():
        expected = min(1.0, 0.60 + (len(names) - names.index(dimension)) / 100.0) - 0.60
        assert impact == pytest.approx(expected)


def test_exactly_six_dimensions_are_selected_and_the_choice_is_deterministic() -> None:
    _cs, seed, dimension_by_config, _index = _candidate_world()
    observations = _synthetic_observations(seed.config_hash, dimension_by_config)
    impacts = parameter_impacts(
        observations=observations,
        seed_config_hash=seed.config_hash,
        dimension_by_config=dimension_by_config,
    )
    first = select_influential_dimensions(impacts)
    second = select_influential_dimensions(dict(reversed(list(impacts.items()))))
    assert len(first) == INFLUENTIAL_DIMENSION_COUNT == 6
    assert [d.name for d in first] == [d.name for d in second], "input order is irrelevant"
    # strictly descending impact, ties broken by live-parameter index
    for earlier, later in zip(first, first[1:], strict=False):
        assert (earlier.impact, -earlier.live_parameter_index) >= (
            later.impact,
            -later.live_parameter_index,
        )


def test_equal_impacts_are_broken_by_live_parameter_index_then_name() -> None:
    names = live_gatk_parameter_space().names()
    impacts = dict.fromkeys(names[:8], 0.5)
    selected = select_influential_dimensions(impacts)
    assert [d.name for d in selected] == list(names[:6])


def test_too_few_screened_dimensions_fails_closed() -> None:
    names = live_gatk_parameter_space().names()
    with pytest.raises(DesignError, match="dimensions were screened"):
        select_influential_dimensions(dict.fromkeys(names[:3], 0.5))


def test_an_unknown_dimension_is_refused() -> None:
    with pytest.raises(DesignError, match="not a live GATK parameter"):
        select_influential_dimensions({"not_a_parameter": 1.0})


# --------------------------------------------------------------------------- #
# anchors
# --------------------------------------------------------------------------- #
def _selected_world():
    _cs, seed, dimension_by_config, accepted_index = _candidate_world()
    observations = _synthetic_observations(seed.config_hash, dimension_by_config)
    impacts = parameter_impacts(
        observations=observations,
        seed_config_hash=seed.config_hash,
        dimension_by_config=dimension_by_config,
    )
    dimensions = select_influential_dimensions(impacts)
    aggregates = {}
    by_config: dict[str, list[BaselineObservation]] = {}
    for observation in observations:
        by_config.setdefault(observation.config_hash, []).append(observation)
    for config_hash, group in by_config.items():
        aggregates[config_hash] = aggregate_candidate(
            config_hash=config_hash, observations=group, required_members=_MEMBERS
        )
    return seed, dimensions, aggregates, dimension_by_config, accepted_index


def test_exactly_one_anchor_per_selected_dimension() -> None:
    seed, dimensions, aggregates, dimension_by_config, accepted_index = _selected_world()
    anchors = select_anchors(
        dimensions=dimensions,
        aggregates=aggregates,
        dimension_by_config=dimension_by_config,
        accepted_index=accepted_index,
    )
    assert len(anchors) == PHASE_B_ANCHOR_COUNT == 6
    assert len(set(anchors)) == 6
    assert seed.config_hash not in anchors
    assert [dimension_by_config[a] for a in anchors] == [d.name for d in dimensions]


def test_anchor_selection_is_deterministic() -> None:
    _seed, dimensions, aggregates, dimension_by_config, accepted_index = _selected_world()
    kwargs = {
        "dimensions": dimensions,
        "aggregates": aggregates,
        "dimension_by_config": dimension_by_config,
        "accepted_index": accepted_index,
    }
    assert select_anchors(**kwargs) == select_anchors(**kwargs)


# --------------------------------------------------------------------------- #
# the Phase-B design
# --------------------------------------------------------------------------- #
def test_phase_b_is_exactly_seed_plus_six_anchors_plus_forty_one_lhs() -> None:
    seed, dimensions, aggregates, dimension_by_config, accepted_index = _selected_world()
    anchors = select_anchors(
        dimensions=dimensions,
        aggregates=aggregates,
        dimension_by_config=dimension_by_config,
        accepted_index=accepted_index,
    )
    design = build_phase_b_design(dimensions=dimensions, seed=seed, anchor_config_hashes=anchors)
    assert len(design.ordered_config_hashes) == PHASE_B_CANDIDATE_COUNT == 48
    assert len(set(design.ordered_config_hashes)) == 48, "every configuration is unique"
    assert design.ordered_config_hashes[0] == seed.config_hash
    assert design.ordered_config_hashes[1:7] == tuple(anchors)
    assert len(design.lhs_config_hashes) == PHASE_B_LHS_COUNT == 41
    assert design.candidate_index[seed.config_hash] == 0


def test_lhs_configurations_vary_only_the_selected_dimensions() -> None:
    seed, dimensions, _agg, _dbc, _idx = _selected_world()
    selected = {d.name for d in dimensions}
    configs = build_phase_b_configs(dimensions=dimensions, seed=seed)
    assert len(configs) == PHASE_B_LHS_COUNT
    for config in configs:
        moved = {
            name
            for name, value in config.effective_config.items()
            if seed.effective_config.get(name) != value
        }
        assert moved <= selected, f"{moved - selected} moved but was not selected"


def test_every_lhs_configuration_is_canonical_and_live_domain_valid() -> None:
    seed, dimensions, _agg, _dbc, _idx = _selected_world()
    space = live_gatk_parameter_space()
    for config in build_phase_b_configs(dimensions=dimensions, seed=seed):
        space.validate_effective_config(dict(config.effective_config))
        assert config.parameter_space_hash == (
            "b2d401918084d64023305d9262baf5011a89fe517bee4e0bd33af79fb14aee2e"
        )


def test_the_design_is_byte_identical_across_runs_and_environments(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Same six dimensions -> same 41 configurations. Host, cwd and TMPDIR are irrelevant."""
    seed, dimensions, _agg, _dbc, _idx = _selected_world()
    first = [c.config_hash for c in build_phase_b_configs(dimensions=dimensions, seed=seed)]

    monkeypatch.setenv("TMPDIR", str(tmp_path))
    monkeypatch.setenv("HOSTNAME", "a-different-host")
    monkeypatch.chdir(tmp_path)
    second = [c.config_hash for c in build_phase_b_configs(dimensions=dimensions, seed=seed)]
    assert first == second
    assert os.environ["TMPDIR"] == str(tmp_path)


def test_the_design_refuses_the_wrong_number_of_dimensions_or_anchors() -> None:
    seed, dimensions, aggregates, dimension_by_config, accepted_index = _selected_world()
    anchors = select_anchors(
        dimensions=dimensions,
        aggregates=aggregates,
        dimension_by_config=dimension_by_config,
        accepted_index=accepted_index,
    )
    with pytest.raises(DesignError, match="exactly 6 dimensions"):
        build_phase_b_design(dimensions=dimensions[:5], seed=seed, anchor_config_hashes=anchors)
    with pytest.raises(DesignError, match="exactly 6 anchors"):
        build_phase_b_design(dimensions=dimensions, seed=seed, anchor_config_hashes=anchors[:5])


def test_the_proposal_ceiling_is_frozen_and_bounded() -> None:
    assert LHS_PROPOSAL_CEILING == 256
    assert LHS_PROPOSAL_CEILING > PHASE_B_LHS_COUNT, "the stream must exceed what it must yield"
