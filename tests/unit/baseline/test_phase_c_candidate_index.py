"""THE Phase-C tie-break index: inherited from the Phase-B design, never the promotion position.

The frozen tie-break's third key is "lower candidate index in the frozen phase design". Phase C
introduces no design of its own — its ten configurations ARE Phase-B configurations — so the index
they carry is the one the Phase-B design gave them, fixed before a single Phase-B score existed.
Renumbering the promoted ten 0..9 would invent an ordering AFTER observing outcomes, and a rule
chosen after the results is not a pre-registered rule.

These controls exist so that reading cannot be quietly swapped later: they are written to FAIL if
someone enumerates the selected candidates instead.
"""

from __future__ import annotations

import pytest

from minos_engine.baseline.design import (
    InfluentialDimension,
    PhaseBDesign,
    phase_c_inherited_candidate_index,
)
from minos_engine.baseline.objective import BaselineObservation, aggregate_candidate
from minos_engine.baseline.racing import VALIDATION_FINALIST_COUNT, select_finalists

_CHROMOSOMES = ("chr18", "chr19", "chr20", "chr21", "chr22")
#: the Phase-C member vector: ten balanced batches, two members per chromosome per five batches.
_FIFTY = tuple((f"minos-{c}-{i}", c) for i in range(10) for c in _CHROMOSOMES)


def _hash(index: int) -> str:
    return f"{index:064d}"


def _design(ordered: tuple[str, ...]) -> PhaseBDesign:
    """A Phase-B design whose ordering is deliberately NOT the promotion ordering."""
    return PhaseBDesign(
        dimensions=(InfluentialDimension(name="min_pruning", impact=0.5, live_parameter_index=0),),
        ordered_config_hashes=ordered,
        seed_config_hash=ordered[0],
        anchor_config_hashes=(),
        lhs_config_hashes=ordered[1:],
    )


def _complete(config: str, score: float, runtime: int = 1000):
    return aggregate_candidate(
        config_hash=config,
        observations=[
            BaselineObservation(
                config_hash=config,
                dataset_id=dataset_id,
                chromosome=chromosome,
                minos_score=score,
                admitted=True,
                gatk_runtime_ms=runtime,
            )
            for dataset_id, chromosome in _FIFTY
        ],
        required_members=_FIFTY,
    )


def test_the_inherited_index_is_the_phase_b_position_not_the_promotion_position() -> None:
    """Promotion order and design order are different orderings of the same ten hashes."""
    design = _design(tuple(_hash(i) for i in range(48)))
    promoted = (_hash(42), _hash(6), _hash(31), _hash(3), _hash(0))

    inherited = phase_c_inherited_candidate_index(design, promoted)

    assert [inherited[h] for h in promoted] == [42, 6, 31, 3, 0]
    assert [inherited[h] for h in promoted] != list(range(len(promoted)))
    assert set(inherited) == set(promoted), "only the promoted candidates carry an index"


def test_a_promoted_configuration_absent_from_the_design_has_no_inherited_index() -> None:
    design = _design(tuple(_hash(i) for i in range(48)))
    with pytest.raises(ValueError, match="absent from the frozen Phase-B design"):
        phase_c_inherited_candidate_index(design, (_hash(3), _hash(99)))


def test_a_duplicate_in_the_promoted_set_is_refused() -> None:
    design = _design(tuple(_hash(i) for i in range(48)))
    with pytest.raises(ValueError, match="duplicate"):
        phase_c_inherited_candidate_index(design, (_hash(3), _hash(3)))


def test_an_exact_tie_is_broken_by_the_LOWER_INHERITED_index() -> None:
    """The load-bearing control.

    Four candidates, two of them tied on BOTH objective and mean runtime — the only situation in
    which the third key decides anything. Promotion order says one of them wins; the inherited
    Phase-B index says the other does. The frozen rule must follow the inherited index.
    """
    # promotion order: A, B, C, D — inherited Phase-B indices deliberately scrambled.
    a, b, c, d = _hash(10), _hash(11), _hash(12), _hash(13)
    promoted = (a, b, c, d)
    ordered_design = (
        # design positions: D=1, B=2, C=7, A=9
        _hash(0),
        d,
        b,
        _hash(3),
        _hash(4),
        _hash(5),
        _hash(6),
        c,
        _hash(8),
        a,
    )
    design = _design(ordered_design)
    inherited = phase_c_inherited_candidate_index(design, promoted)
    assert [inherited[h] for h in promoted] == [9, 2, 7, 1]

    # A and B are exactly tied; C and D are strictly worse, so only the tie matters.
    aggregates = {
        a: _complete(a, 0.90),
        b: _complete(b, 0.90),
        c: _complete(c, 0.50),
        d: _complete(d, 0.40),
    }
    assert aggregates[a].objective == aggregates[b].objective
    assert aggregates[a].mean_gatk_runtime_ms == aggregates[b].mean_gatk_runtime_ms

    winners = select_finalists(aggregates=aggregates, candidate_index=inherited, seed_config_hash=d)
    assert len(winners) == VALIDATION_FINALIST_COUNT
    assert winners[0] == b, "B has the LOWER inherited Phase-B index (2 < 9) and must rank first"

    # and the rejected reading would have produced the other order.
    enumerated = {h: i for i, h in enumerate(promoted)}  # A=0, B=1, C=2, D=3
    wrong = select_finalists(aggregates=aggregates, candidate_index=enumerated, seed_config_hash=d)
    assert wrong[0] == a, "promotion-position numbering would rank A first"
    assert wrong[0] != winners[0], (
        "the two readings genuinely disagree, which is why the rule cannot be chosen after the "
        "results are known"
    )
