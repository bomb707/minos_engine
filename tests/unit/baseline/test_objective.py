"""The frozen L2-F2 objective — aggregation utility, J, failure vs missing, total ordering.

Pure computation: no database, no filesystem, no scores from any real evaluation.
"""

from __future__ import annotations

import math

import pytest
from pydantic import ValidationError

from minos_engine.baseline.objective import (
    CVAR_ALPHA,
    CVAR_WEIGHT,
    FAILURE_PENALTY,
    FLOOR_WEIGHT,
    MEAN_WEIGHT,
    BaselineObjectiveError,
    BaselineObservation,
    aggregate_candidate,
    classify_failure_code,
    objective_value,
    rank_candidates,
)

_H = "a" * 64
_OTHER = "b" * 64
_CHROMOSOMES = ("chr18", "chr19", "chr20", "chr21", "chr22")


def _members(per_chromosome: int = 1) -> tuple[tuple[str, str], ...]:
    return tuple((f"minos-{c}-{i}", c) for c in _CHROMOSOMES for i in range(per_chromosome))


def _admitted(
    dataset_id: str, chromosome: str, score: float, *, runtime: int = 1000, config: str = _H
):
    return BaselineObservation(
        config_hash=config,
        dataset_id=dataset_id,
        chromosome=chromosome,
        minos_score=score,
        admitted=True,
        gatk_runtime_ms=runtime,
    )


def _failed(dataset_id: str, chromosome: str, code: str = "GATK_NONZERO_EXIT", config: str = _H):
    return BaselineObservation(
        config_hash=config,
        dataset_id=dataset_id,
        chromosome=chromosome,
        admitted=False,
        failure_code=code,
        gatk_runtime_ms=10,
    )


# --------------------------------------------------------------------------- #
# the frozen constants and the equation
# --------------------------------------------------------------------------- #
def test_the_frozen_constants_are_exactly_the_protocol_values() -> None:
    assert (CVAR_ALPHA, CVAR_WEIGHT, FLOOR_WEIGHT, MEAN_WEIGHT, FAILURE_PENALTY) == (
        0.25,
        0.50,
        0.30,
        0.20,
        1.00,
    )
    assert pytest.approx(1.0) == CVAR_WEIGHT + FLOOR_WEIGHT + MEAN_WEIGHT


def test_all_admitted_at_one_scores_exactly_one() -> None:
    members = _members()
    aggregate = aggregate_candidate(
        config_hash=_H,
        observations=[_admitted(d, c, 1.0) for d, c in members],
        required_members=members,
    )
    assert aggregate.objective == pytest.approx(1.0)
    assert aggregate.complete and aggregate.failure_count == 0


def test_all_admitted_at_one_half_scores_exactly_one_half() -> None:
    members = _members()
    aggregate = aggregate_candidate(
        config_hash=_H,
        observations=[_admitted(d, c, 0.5) for d, c in members],
        required_members=members,
    )
    assert aggregate.objective == pytest.approx(0.5)


def test_the_objective_is_the_frozen_weighted_combination() -> None:
    assert objective_value(cvar=0.4, floor=0.2, mean=0.6, failure_rate=0.1) == pytest.approx(
        0.50 * 0.4 + 0.30 * 0.2 + 0.20 * 0.6 - 1.00 * 0.1
    )


# --------------------------------------------------------------------------- #
# failure vs missing vs a genuine zero score
# --------------------------------------------------------------------------- #
def test_a_known_failure_contributes_zero_utility_and_a_penalty() -> None:
    members = _members()
    observations = [_admitted(d, c, 1.0) for d, c in members[:-1]]
    observations.append(_failed(*members[-1]))
    aggregate = aggregate_candidate(
        config_hash=_H, observations=observations, required_members=members
    )
    assert aggregate.failure_count == 1
    assert aggregate.failure_rate == pytest.approx(1 / 5)
    assert aggregate.objective < 1.0


def test_a_failure_is_distinguishable_from_a_genuine_zero_score() -> None:
    """A configuration that scored 0.0 is NOT the same object as one that failed."""
    members = _members()
    scored_zero = aggregate_candidate(
        config_hash=_H,
        observations=[
            *[_admitted(d, c, 1.0) for d, c in members[:-1]],
            _admitted(*members[-1], 0.0),
        ],
        required_members=members,
    )
    failed = aggregate_candidate(
        config_hash=_H,
        observations=[
            *[_admitted(d, c, 1.0) for d, c in members[:-1]],
            _failed(*members[-1]),
        ],
        required_members=members,
    )
    assert scored_zero.failure_count == 0
    assert failed.failure_count == 1
    # identical utility vectors, but the failure is penalised
    assert scored_zero.cvar == pytest.approx(failed.cvar)
    assert scored_zero.objective > failed.objective
    assert scored_zero.objective - failed.objective == pytest.approx(FAILURE_PENALTY * (1 / 5))


def test_a_non_admitted_observation_may_not_carry_a_score() -> None:
    with pytest.raises(ValidationError, match="not a low score"):
        BaselineObservation(
            config_hash=_H,
            dataset_id="d",
            chromosome="chr18",
            minos_score=0.4,
            admitted=False,
            failure_code="GATK_TIMEOUT",
            gatk_runtime_ms=1,
        )


def test_an_admitted_observation_must_carry_a_score_and_no_failure_code() -> None:
    with pytest.raises(ValidationError):
        BaselineObservation(
            config_hash=_H, dataset_id="d", chromosome="chr18", admitted=True, gatk_runtime_ms=1
        )
    with pytest.raises(ValidationError):
        BaselineObservation(
            config_hash=_H,
            dataset_id="d",
            chromosome="chr18",
            minos_score=0.5,
            admitted=True,
            failure_code="GATK_TIMEOUT",
            gatk_runtime_ms=1,
        )


def test_a_missing_observation_leaves_the_candidate_incomplete_not_zero() -> None:
    members = _members()
    aggregate = aggregate_candidate(
        config_hash=_H,
        observations=[_admitted(d, c, 1.0) for d, c in members[:3]],
        required_members=members,
    )
    assert aggregate.observed_count == 3
    assert aggregate.complete is False
    assert aggregate.failure_count == 0, "a missing member is not a failure"
    # the observed utilities are all 1.0, so nothing was silently zero-filled
    assert aggregate.mean == pytest.approx(1.0)


# --------------------------------------------------------------------------- #
# the robustness terms actually do something
# --------------------------------------------------------------------------- #
def test_the_worst_chromosome_drives_the_floor() -> None:
    members = _members()
    good = aggregate_candidate(
        config_hash=_H,
        observations=[_admitted(d, c, 0.9) for d, c in members],
        required_members=members,
    )
    uneven = aggregate_candidate(
        config_hash=_H,
        observations=[_admitted(d, c, 0.9 if c != "chr22" else 0.1) for d, c in members],
        required_members=members,
    )
    assert good.floor == pytest.approx(0.9)
    assert uneven.floor == pytest.approx(0.1)
    assert uneven.objective < good.objective


def test_the_lower_tail_drives_cvar() -> None:
    members = _members(per_chromosome=2)  # 10 members -> CVaR takes the lowest ceil(2.5)=3
    utilities = [0.1, 0.2, 0.3, *[0.9] * 7]
    observations = [_admitted(d, c, u) for (d, c), u in zip(members, utilities, strict=True)]
    aggregate = aggregate_candidate(
        config_hash=_H, observations=observations, required_members=members
    )
    assert aggregate.cvar == pytest.approx((0.1 + 0.2 + 0.3) / 3)
    assert aggregate.mean == pytest.approx(sum(utilities) / len(utilities))
    assert aggregate.cvar < aggregate.mean


def test_a_high_mean_candidate_can_lose_to_a_robust_one() -> None:
    """THE reason the objective is not a mean: one catastrophic chromosome must cost."""
    members = _members()
    spiky = aggregate_candidate(
        config_hash=_H,
        observations=[_admitted(d, c, 1.0 if c != "chr22" else 0.0) for d, c in members],
        required_members=members,
    )
    robust = aggregate_candidate(
        config_hash=_OTHER,
        observations=[_admitted(d, c, 0.75, config=_OTHER) for d, c in members],
        required_members=members,
    )
    assert spiky.mean == pytest.approx(0.8)
    assert robust.mean == pytest.approx(0.75)
    assert robust.objective > spiky.objective, "the robust candidate must win"


# --------------------------------------------------------------------------- #
# hygiene
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf"), -0.1, 1.1])
def test_non_finite_or_out_of_range_scores_are_refused(bad: float) -> None:
    with pytest.raises(ValidationError):
        BaselineObservation(
            config_hash=_H,
            dataset_id="d",
            chromosome="chr18",
            minos_score=bad,
            admitted=True,
            gatk_runtime_ms=1,
        )
    assert not (math.isnan(bad) and False)


def test_negative_runtime_is_refused() -> None:
    with pytest.raises(ValidationError):
        BaselineObservation(
            config_hash=_H,
            dataset_id="d",
            chromosome="chr18",
            minos_score=0.5,
            admitted=True,
            gatk_runtime_ms=-1,
        )


def test_a_duplicate_member_observation_is_refused() -> None:
    members = _members()
    with pytest.raises(BaselineObjectiveError, match="duplicate observation"):
        aggregate_candidate(
            config_hash=_H,
            observations=[_admitted(*members[0], 1.0), _admitted(*members[0], 0.2)],
            required_members=members,
        )


def test_an_observation_from_another_candidate_is_refused() -> None:
    members = _members()
    with pytest.raises(BaselineObjectiveError, match="supplied to aggregate"):
        aggregate_candidate(
            config_hash=_H,
            observations=[_admitted(*members[0], 1.0, config=_OTHER)],
            required_members=members,
        )


def test_an_observation_on_the_wrong_chromosome_is_refused() -> None:
    members = _members()
    with pytest.raises(BaselineObjectiveError, match="claims chromosome"):
        aggregate_candidate(
            config_hash=_H,
            observations=[_admitted(members[0][0], "chr22", 1.0)],
            required_members=members,
        )


def test_an_observation_outside_the_required_set_is_refused() -> None:
    members = _members()
    with pytest.raises(BaselineObjectiveError, match="not a required member"):
        aggregate_candidate(
            config_hash=_H,
            observations=[_admitted("stranger", "chr18", 1.0)],
            required_members=members,
        )


def test_arrival_order_cannot_change_the_aggregate() -> None:
    members = _members(per_chromosome=2)
    observations = [_admitted(d, c, (index % 7) / 10.0) for index, (d, c) in enumerate(members)]
    first = aggregate_candidate(config_hash=_H, observations=observations, required_members=members)
    second = aggregate_candidate(
        config_hash=_H, observations=list(reversed(observations)), required_members=members
    )
    assert first.model_dump() == second.model_dump()


# --------------------------------------------------------------------------- #
# the total order
# --------------------------------------------------------------------------- #
def test_the_tie_break_is_total_and_deterministic() -> None:
    members = _members()

    def build(config: str, score: float, runtime: int):
        return aggregate_candidate(
            config_hash=config,
            observations=[
                _admitted(d, c, score, runtime=runtime, config=config) for d, c in members
            ],
            required_members=members,
        )

    same_score_slow = build(_OTHER, 0.5, 5000)
    same_score_fast = build(_H, 0.5, 1000)
    index = {_H: 1, _OTHER: 0}
    ordered = rank_candidates([same_score_slow, same_score_fast], candidate_index=index)
    assert [a.config_hash for a in ordered] == [_H, _OTHER], "runtime breaks the score tie"

    # identical score AND runtime -> the frozen candidate index decides
    a = build(_H, 0.5, 1000)
    b = build(_OTHER, 0.5, 1000)
    ordered = rank_candidates([a, b], candidate_index={_H: 5, _OTHER: 2})
    assert [x.config_hash for x in ordered] == [_OTHER, _H]


def test_ranking_refuses_a_candidate_with_no_frozen_index() -> None:
    members = _members()
    aggregate = aggregate_candidate(
        config_hash=_H,
        observations=[_admitted(d, c, 1.0) for d, c in members],
        required_members=members,
    )
    with pytest.raises(BaselineObjectiveError, match="no index"):
        rank_candidates([aggregate], candidate_index={})


# --------------------------------------------------------------------------- #
# candidate responsibility vs phase health
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "code", ["GATK_NONZERO_EXIT", "GATK_TIMEOUT", "GATK_OUTPUT_INVALID", "GATK_OUTPUT_MISSING"]
)
def test_gatk_outcomes_are_the_candidates_responsibility(code: str) -> None:
    assert classify_failure_code(code) == "CANDIDATE_FAILURE"


@pytest.mark.parametrize(
    "code",
    [
        "PREPARATION_FAILED",
        "EXECUTION_ERROR",
        "HAPPY_NONZERO_EXIT",
        "HAPPY_TIMEOUT",
        "HAPPY_OUTPUT_INVALID",
        "TRUTH_BYTES_MISMATCH",
        "TRUTH_IDENTITY_MISSING",
        "VCF_BYTES_MISMATCH",
        "SCORER_OUTPUT_INVALID",
        "ARTIFACT_PUBLISH_FAILED",
        "EVALUATION_ERROR",
    ],
)
def test_our_own_harness_failures_are_phase_health_not_candidate_failures(code: str) -> None:
    assert classify_failure_code(code) == "INFRASTRUCTURE_INCIDENT"


def test_an_infrastructure_incident_is_not_charged_to_the_candidate() -> None:
    members = _members()
    aggregate = aggregate_candidate(
        config_hash=_H,
        observations=[
            *[_admitted(d, c, 1.0) for d, c in members[:-1]],
            _failed(*members[-1], code="HAPPY_TIMEOUT"),
        ],
        required_members=members,
    )
    assert aggregate.failure_count == 0
    assert aggregate.infrastructure_incident_count == 1


def test_an_unknown_failure_code_is_refused_rather_than_guessed() -> None:
    with pytest.raises(BaselineObjectiveError, match="unknown bounded failure code"):
        classify_failure_code("SOMETHING_NEW")
