"""The Phase-A analysis wrapper: complete-only, deterministic, and scientifically inert.

The wrapper adds no rule of its own. It orders the already-frozen ones — aggregate, measure
impact, take the K=6 dimensions, pick one anchor each, build the 48-candidate Phase-B set — and
enforces the single property those rules assume: the screen it reads is COMPLETE.

That property is the whole point. An impact is a mean over members and the K=6 cut is a
comparison between dimensions, so analysing a partial screen would let job completion order
decide what Phase B explores. These controls prove the refusal is real, that the result does not
depend on the order observations arrive in, and that nothing here selects a baseline or reaches
validation or test data.

Pure: no database, no filesystem, no GATK, no score.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path
from typing import Any

import pytest

from minos_engine.baseline.design import INFLUENTIAL_DIMENSION_COUNT
from minos_engine.baseline.objective import BaselineObservation
from minos_engine.baseline.phase_a import build_phase_a_authority
from minos_engine.baseline.phase_a_analysis import (
    PhaseAAnalysis,
    PhaseAAnalysisError,
    analyze_completed_phase_a,
)
from minos_engine.baseline.phase_a_observations import PhaseAObservationSnapshot
from minos_engine.experiments.candidates import generate_accepted_candidate_set

_CHROMOSOME = {
    "minos-chr18-028662fb934529d7": "chr18",
    "minos-chr19-0de906231aa96ade": "chr19",
    "minos-chr20-42bdea88e6242d37": "chr20",
    "minos-chr21-0279a3b8042f848b": "chr21",
    "minos-chr22-19a4002faeaacbdf": "chr22",
}


def _score(member_index: int, config_index: int) -> float:
    """A deterministic, spread-out synthetic score. No real score is involved."""
    raw = ((config_index * 37 + member_index * 11) % 97) / 100.0
    return round(0.30 + raw * 0.60, 12)


def _observations() -> list[BaselineObservation]:
    """A COMPLETE synthetic screen: the frozen 5 members x 39 accepted candidates."""
    plan = build_phase_a_authority().plan
    configs = generate_accepted_candidate_set().configs
    out: list[BaselineObservation] = []
    for member in plan.members:
        for config_index, config in enumerate(configs):
            out.append(
                BaselineObservation(
                    config_hash=config.config_hash,
                    dataset_id=member.dataset_id,
                    chromosome=_CHROMOSOME[member.dataset_id],
                    minos_score=_score(member.member_index, config_index),
                    admitted=True,
                    failure_code=None,
                    gatk_runtime_ms=1000 + config_index * 7 + member.member_index,
                )
            )
    return out


def _snapshot(observations: list[BaselineObservation]) -> PhaseAObservationSnapshot:
    decided = len(observations)
    return PhaseAObservationSnapshot(
        observations=tuple(observations),
        execution_result_count=decided,
        execution_failure_count=0,
        evaluation_result_count=decided,
        evaluation_failure_count=0,
    )


@pytest.fixture
def complete() -> PhaseAObservationSnapshot:
    return _snapshot(_observations())


# --------------------------------------------------------------------------- #
# the complete screen
# --------------------------------------------------------------------------- #
def test_a_complete_screen_produces_the_frozen_phase_b_design(
    complete: PhaseAObservationSnapshot,
) -> None:
    analysis = analyze_completed_phase_a(complete)

    assert len(complete.observations) == 195
    assert len(analysis.aggregates) == 39, "one aggregate per accepted Phase-A candidate"
    assert all(a.complete for a in analysis.aggregates.values())
    assert len(analysis.dimensions) == INFLUENTIAL_DIMENSION_COUNT == 6
    assert len(analysis.anchors) == 6
    assert len(set(analysis.anchors)) == 6, "one distinct anchor per selected dimension"

    design = analysis.design
    assert len(design.ordered_config_hashes) == 48
    assert design.seed_config_hash == generate_accepted_candidate_set().seed_config_hash
    assert design.anchor_config_hashes == analysis.anchors
    assert len(design.lhs_config_hashes) == 41
    assert len(set(design.ordered_config_hashes)) == 48

    # every selected dimension is one the OAT screen actually measured, and the six selected are
    # the six highest impacts under the frozen total order.
    assert {d.name for d in analysis.dimensions} <= set(analysis.impacts)
    assert len(analysis.impacts) == 22, "one impact per live dimension the screen moved"
    top = sorted(analysis.impacts.values(), reverse=True)[:6]
    assert sorted((d.impact for d in analysis.dimensions), reverse=True) == top


def test_the_result_is_deterministic_and_order_independent(
    complete: PhaseAObservationSnapshot,
) -> None:
    """Rerunning, and reordering the input, must not move a single selection."""
    first = analyze_completed_phase_a(complete)
    again = analyze_completed_phase_a(complete)
    reversed_input = analyze_completed_phase_a(_snapshot(list(reversed(complete.observations))))

    for other in (again, reversed_input):
        assert other.dimensions == first.dimensions
        assert other.anchors == first.anchors
        assert other.impacts == first.impacts
        assert other.design == first.design
        assert other.aggregates == first.aggregates


def test_the_wrapper_selects_no_baseline_and_reaches_no_held_out_data(
    complete: PhaseAObservationSnapshot,
) -> None:
    """It designs Phase B. It does not choose a winner, and it cannot see validation or test.

    The observations it is given are TRAIN-only by construction, and what it returns is a design
    plus the statistics behind it — there is no selected configuration anywhere in the result.
    """
    analysis = analyze_completed_phase_a(complete)
    assert analysis.design.seed_config_hash in analysis.aggregates

    assert {f.name for f in dataclasses.fields(PhaseAAnalysis)} == {
        "aggregates",
        "impacts",
        "dimensions",
        "anchors",
        "design",
    }
    assert {o.dataset_id for o in complete.observations} == set(_CHROMOSOME)

    # an AST control, not a substring one: prose may DISCUSS validation, but no import or call
    # in this module may reach held-out data or a final-selection routine.
    import ast

    tree = ast.parse(Path("src/minos_engine/baseline/phase_a_analysis.py").read_text("utf-8"))
    imported: set[str] = set()
    called: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add(node.module or "")
            imported.update(f"{node.module}.{alias.name}" for alias in node.names)
        elif isinstance(node, ast.Call):
            target = node.func
            called.add(
                target.attr if isinstance(target, ast.Attribute) else getattr(target, "id", "")
            )
    for name in imported | called:
        lowered = name.lower()
        for forbidden in ("validation", "holdout", "test_split", "select_baseline", "final"):
            assert forbidden not in lowered, f"the wrapper reaches {name!r}"


# --------------------------------------------------------------------------- #
# completeness
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("dropped", [0, 97, 194])
def test_194_of_195_observations_is_refused(
    complete: PhaseAObservationSnapshot, dropped: int
) -> None:
    """One missing outcome is enough to refuse: the K=6 cut is a comparison, not a per-job fact."""
    partial = list(complete.observations)
    del partial[dropped]

    with pytest.raises(PhaseAAnalysisError, match="requires all 195 decided observations"):
        analyze_completed_phase_a(_snapshot(partial))


def test_an_empty_screen_is_refused(complete: PhaseAObservationSnapshot) -> None:
    with pytest.raises(PhaseAAnalysisError, match="got 0"):
        analyze_completed_phase_a(_snapshot([]))


def test_an_observation_outside_the_accepted_candidate_set_is_refused(
    complete: PhaseAObservationSnapshot,
) -> None:
    swapped = list(complete.observations)
    swapped[3] = swapped[3].model_copy(update={"config_hash": "f" * 64})

    with pytest.raises(PhaseAAnalysisError, match="not an accepted Phase-A candidate"):
        analyze_completed_phase_a(_snapshot(swapped))


def test_a_member_observed_under_two_chromosomes_is_refused(
    complete: PhaseAObservationSnapshot,
) -> None:
    """The member set is frozen; a member cannot belong to two chromosome batches at once."""
    inconsistent = list(complete.observations)
    inconsistent[1] = inconsistent[1].model_copy(update={"chromosome": "chr21"})

    with pytest.raises(PhaseAAnalysisError, match="observed as both"):
        analyze_completed_phase_a(_snapshot(inconsistent))


def test_a_screen_missing_a_whole_member_is_refused(
    complete: PhaseAObservationSnapshot,
) -> None:
    """195 observations spread over four members is still not the frozen screen."""
    absent = "minos-chr22-19a4002faeaacbdf"
    replacement: list[Any] = []
    for observation in complete.observations:
        if observation.dataset_id == absent:
            replacement.append(
                observation.model_copy(
                    update={
                        "dataset_id": "minos-chr18-028662fb934529d7",
                        "chromosome": "chr18",
                    }
                )
            )
        else:
            replacement.append(observation)

    with pytest.raises(PhaseAAnalysisError, match="has no observation at all"):
        analyze_completed_phase_a(_snapshot(replacement))
