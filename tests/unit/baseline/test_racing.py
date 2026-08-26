"""Frozen racing bounds, strict-inequality elimination and seed-controlled promotion."""

from __future__ import annotations

import pytest

from minos_engine.baseline.objective import BaselineObservation, aggregate_candidate
from minos_engine.baseline.racing import (
    PHASE_B_SURVIVOR_COUNT,
    VALIDATION_FINALIST_COUNT,
    RacingError,
    eliminate,
    racing_bounds,
    select_finalists,
    select_survivors,
)

_CHROMOSOMES = ("chr18", "chr19", "chr20", "chr21", "chr22")
_TEN = tuple((f"minos-{c}-{i}", c) for i in range(2) for c in _CHROMOSOMES)
_FIVE = _TEN[:5]


def _hash(index: int) -> str:
    return f"{index:064d}"


def _admitted(config: str, dataset_id: str, chromosome: str, score: float, runtime: int = 1000):
    return BaselineObservation(
        config_hash=config,
        dataset_id=dataset_id,
        chromosome=chromosome,
        minos_score=score,
        admitted=True,
        gatk_runtime_ms=runtime,
    )


def _complete(config: str, score: float, members=_TEN, runtime: int = 1000):
    return aggregate_candidate(
        config_hash=config,
        observations=[_admitted(config, d, c, score, runtime) for d, c in members],
        required_members=members,
    )


# --------------------------------------------------------------------------- #
# bounds
# --------------------------------------------------------------------------- #
def test_bounds_bracket_the_eventual_objective() -> None:
    config = _hash(1)
    observations = [_admitted(config, d, c, 0.5) for d, c in _FIVE]
    bound = racing_bounds(config_hash=config, observations=observations, required_members=_TEN)
    assert bound.observed_count == 5 and bound.required_count == 10
    assert bound.complete is False
    assert bound.optimistic > bound.pessimistic
    # the eventual value under any real completion must lie inside the bracket
    actual = _complete(config, 0.5).objective
    assert bound.pessimistic <= actual <= bound.optimistic


def test_a_complete_candidates_bounds_collapse_onto_its_objective() -> None:
    config = _hash(2)
    observations = [_admitted(config, d, c, 0.7) for d, c in _TEN]
    bound = racing_bounds(config_hash=config, observations=observations, required_members=_TEN)
    aggregate = _complete(config, 0.7)
    assert bound.complete is True
    assert bound.optimistic == pytest.approx(aggregate.objective)
    assert bound.pessimistic == pytest.approx(aggregate.objective)


def test_a_missing_member_is_not_silently_treated_as_a_failure() -> None:
    """The pessimistic bound ASSUMES failure; the optimistic one does not. Missing is undecided."""
    config = _hash(3)
    observations = [_admitted(config, d, c, 1.0) for d, c in _FIVE]
    bound = racing_bounds(config_hash=config, observations=observations, required_members=_TEN)
    assert bound.optimistic == pytest.approx(1.0), "a perfect completion is still reachable"
    assert bound.pessimistic < 1.0


def test_bound_computation_refuses_duplicates_and_strangers() -> None:
    config = _hash(4)
    with pytest.raises(RacingError, match="duplicate observation"):
        racing_bounds(
            config_hash=config,
            observations=[_admitted(config, *_FIVE[0], 1.0), _admitted(config, *_FIVE[0], 0.1)],
            required_members=_TEN,
        )
    with pytest.raises(RacingError, match="not a required member"):
        racing_bounds(
            config_hash=config,
            observations=[_admitted(config, "stranger", "chr18", 1.0)],
            required_members=_TEN,
        )


# --------------------------------------------------------------------------- #
# elimination is STRICT
# --------------------------------------------------------------------------- #
def test_a_balanced_half_screen_cannot_eliminate_anybody_at_all() -> None:
    """A structural consequence of the frozen bounds that Phase B runs straight into.

    With five of ten members observed, the ``-1.0 * failure_rate`` term moves BOTH bounds by
    exactly 0.5: the worst reachable optimistic bound (every seen member a candidate failure,
    every unseen one perfect) and the best reachable pessimistic bound (every seen member
    perfect, every unseen one a failure) are the same number. Elimination needs a STRICT
    inequality, so a single balanced batch can never eliminate anyone, however the field is
    scored — the budget saving racing exists for cannot be realised after batch 0 alone.

    This is recorded, not adjusted: the rule is frozen, and a rule that eliminates nobody is
    conservative in the safe direction.
    """
    perfect = _hash(1)
    hopeless = _hash(2)
    best = racing_bounds(
        config_hash=perfect,
        observations=[_admitted(perfect, d, c, 1.0) for d, c in _FIVE],
        required_members=_TEN,
    )
    worst = racing_bounds(
        config_hash=hopeless,
        observations=[
            BaselineObservation(
                config_hash=hopeless,
                dataset_id=d,
                chromosome=c,
                admitted=False,
                failure_code="GATK_NONZERO_EXIT",
                gatk_runtime_ms=1000,
            )
            for d, c in _FIVE
        ],
        required_members=_TEN,
    )
    assert worst.optimistic == pytest.approx(best.pessimistic)

    field = [best, worst] + [
        racing_bounds(
            config_hash=_hash(i),
            observations=[_admitted(_hash(i), d, c, 0.9) for d, c in _FIVE],
            required_members=_TEN,
        )
        for i in range(3, 49)
    ]
    assert len(field) == 48
    assert eliminate(field, seed_config_hash=perfect, keep=PHASE_B_SURVIVOR_COUNT) == ()


def test_a_candidate_that_can_merely_tie_the_threshold_survives() -> None:
    """THE strictness property: equality is not elimination."""
    seed = _hash(0)
    rivals = [
        racing_bounds(
            config_hash=_hash(i),
            observations=[_admitted(_hash(i), d, c, 1.0) for d, c in _TEN],
            required_members=_TEN,
        )
        for i in range(1, 4)
    ]
    threshold = sorted((b.pessimistic for b in rivals), reverse=True)[1]
    tying = racing_bounds(
        config_hash=_hash(99),
        observations=[_admitted(_hash(99), d, c, 1.0) for d, c in _TEN],
        required_members=_TEN,
    )
    assert tying.optimistic == pytest.approx(threshold)
    assert _hash(99) not in eliminate([*rivals, tying], seed_config_hash=seed, keep=2)


def test_a_candidate_strictly_below_the_threshold_is_eliminated() -> None:
    seed = _hash(0)
    strong = [
        racing_bounds(
            config_hash=_hash(i),
            observations=[_admitted(_hash(i), d, c, 1.0) for d, c in _TEN],
            required_members=_TEN,
        )
        for i in range(1, 4)
    ]
    hopeless = racing_bounds(
        config_hash=_hash(50),
        observations=[_admitted(_hash(50), d, c, 0.0) for d, c in _TEN],
        required_members=_TEN,
    )
    assert hopeless.optimistic < sorted((b.pessimistic for b in strong), reverse=True)[1]
    assert _hash(50) in eliminate([*strong, hopeless], seed_config_hash=seed, keep=2)


def test_the_seed_is_never_eliminated_however_badly_it_scores() -> None:
    seed = _hash(0)
    seed_bound = racing_bounds(
        config_hash=seed,
        observations=[_admitted(seed, d, c, 0.0) for d, c in _TEN],
        required_members=_TEN,
    )
    strong = [
        racing_bounds(
            config_hash=_hash(i),
            observations=[_admitted(_hash(i), d, c, 1.0) for d, c in _TEN],
            required_members=_TEN,
        )
        for i in range(1, 4)
    ]
    assert seed not in eliminate([seed_bound, *strong], seed_config_hash=seed, keep=2)


def test_elimination_is_independent_of_arrival_order() -> None:
    seed = _hash(0)
    bounds = [
        racing_bounds(
            config_hash=_hash(i),
            observations=[_admitted(_hash(i), d, c, i / 10.0) for d, c in _TEN],
            required_members=_TEN,
        )
        for i in range(1, 8)
    ]
    forward = eliminate(bounds, seed_config_hash=seed, keep=3)
    backward = eliminate(list(reversed(bounds)), seed_config_hash=seed, keep=3)
    assert forward == backward


def test_nothing_is_eliminated_while_the_field_is_no_larger_than_the_keep_count() -> None:
    bounds = [
        racing_bounds(
            config_hash=_hash(i),
            observations=[_admitted(_hash(i), d, c, 0.1) for d, c in _TEN],
            required_members=_TEN,
        )
        for i in range(1, 4)
    ]
    assert eliminate(bounds, seed_config_hash=_hash(0), keep=3) == ()


# --------------------------------------------------------------------------- #
# seed-controlled promotion
# --------------------------------------------------------------------------- #
def _field(seed_score: float, count: int = 20):
    seed = _hash(0)
    aggregates = {seed: _complete(seed, seed_score)}
    for i in range(1, count):
        aggregates[_hash(i)] = _complete(_hash(i), 0.90 - i * 0.01)
    index = {h: i for i, h in enumerate(aggregates)}
    return seed, aggregates, index


def test_phase_c_promotes_exactly_ten_with_a_naturally_ranked_seed() -> None:
    seed, aggregates, index = _field(seed_score=0.99)
    survivors = select_survivors(
        aggregates=aggregates, candidate_index=index, seed_config_hash=seed
    )
    assert len(survivors) == PHASE_B_SURVIVOR_COUNT == 10
    assert survivors[0] == seed, "a naturally top seed simply wins"
    assert len(set(survivors)) == 10


def test_phase_c_promotes_top_nine_plus_a_losing_seed() -> None:
    seed, aggregates, index = _field(seed_score=0.01)
    survivors = select_survivors(
        aggregates=aggregates, candidate_index=index, seed_config_hash=seed
    )
    assert len(survivors) == 10
    assert seed in survivors
    assert survivors[-1] == seed, "the seed is appended, never displacing a better candidate"
    ranked = sorted(
        (a for a in aggregates.values() if a.config_hash != seed),
        key=lambda a: (-a.objective, a.mean_gatk_runtime_ms, index[a.config_hash]),
    )
    assert list(survivors[:9]) == [a.config_hash for a in ranked[:9]]


def test_validation_promotes_exactly_four_with_the_same_seed_control() -> None:
    seed, aggregates, index = _field(seed_score=0.99)
    winners = select_finalists(aggregates=aggregates, candidate_index=index, seed_config_hash=seed)
    assert len(winners) == VALIDATION_FINALIST_COUNT == 4
    assert seed in winners

    seed, aggregates, index = _field(seed_score=0.01)
    losers = select_finalists(aggregates=aggregates, candidate_index=index, seed_config_hash=seed)
    assert len(losers) == 4
    assert seed in losers and losers[-1] == seed


def test_promotion_refuses_an_incomplete_field() -> None:
    seed = _hash(0)
    partial = {
        seed: aggregate_candidate(
            config_hash=seed,
            observations=[_admitted(seed, d, c, 1.0) for d, c in _FIVE],
            required_members=_TEN,
        )
    }
    with pytest.raises(RacingError, match="complete candidates"):
        select_survivors(aggregates=partial, candidate_index={seed: 0}, seed_config_hash=seed)


def test_promotion_refuses_a_field_without_the_seed() -> None:
    aggregates = {_hash(i): _complete(_hash(i), 0.5) for i in range(1, 12)}
    index = {h: i for i, h in enumerate(aggregates)}
    with pytest.raises(RacingError, match="seed must be present"):
        select_survivors(aggregates=aggregates, candidate_index=index, seed_config_hash=_hash(0))
