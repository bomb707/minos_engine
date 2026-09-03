"""Phase-D closure: the frozen objective, the frozen order, and nothing of its own.

The interesting tests are not "does it rank" but "can anything else get in": a substituted
finalist index, a seed preference, a second scoring contract, an infrastructure incident, or a
thirty-ninth observation standing in for a fortieth.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from minos_engine.baseline.finalist_freeze import load_finalist_freeze
from minos_engine.baseline.phase_d import build_l2f2_phase_d_authority
from minos_engine.baseline.phase_d_observations import (
    ACCEPTED_EXECUTION_ENVIRONMENT_HASH,
    ACCEPTED_SCORING_CONTRACT_HASH,
    PHASE_D_CLOSURE_DOMAIN,
    PhaseDClosureError,
    build_phase_d_closure,
    compute_phase_d_closure_hash,
    derive_phase_d_observations,
)
from minos_engine.baseline.phase_d_selection import (
    INHERITED_CANDIDATE_INDEX,
    ORDERED_FINALISTS,
    SEED_CONFIG_HASH,
)
from minos_engine.storage.l2f2_validation_prepare import (
    ACCEPTED_FINALIST_FREEZE_SHA256,
    ACCEPTED_PHASE_C_CLOSURE_SHA256,
)
from tests.l2f2_phase_d_fixture import FIXTURE_FREEZE_PATH

_PROTOCOL_HASH = "c548e190571f5e964560cf30021a520ea8aad6674569fa3202af880d7dff77d1"


@pytest.fixture(scope="module")
def authority() -> Any:
    return build_l2f2_phase_d_authority(
        load_finalist_freeze(
            FIXTURE_FREEZE_PATH,
            expected_artifact_sha256=ACCEPTED_FINALIST_FREEZE_SHA256,
            expected_phase_c_closure_sha256=ACCEPTED_PHASE_C_CLOSURE_SHA256,
        )
    )


def _row(
    authority: Any,
    *,
    member_index: int,
    config_index: int,
    score: float | None = 0.7,
    runtime: int = 1000,
    admitted: bool = True,
    execution_failure: str | None = None,
    evaluation_failure: str | None = None,
    contract: str = ACCEPTED_SCORING_CONTRACT_HASH,
    environment: str = ACCEPTED_EXECUTION_ENVIRONMENT_HASH,
    **overrides: Any,
) -> dict[str, Any]:
    """One synthetic closure row, shaped exactly like the 0026 view's output."""
    member = authority.schedule.members[member_index]
    config_hash = authority.ordered_config_hashes[config_index]
    tag = f"{member_index}-{config_index}"
    row: dict[str, Any] = {
        "plan_hash": authority.plan_hash,
        "job_id": f"job-{tag}",
        "job_key": f"{member_index:02d}{config_index:02d}".ljust(64, "a"),
        "job_status": "SUCCEEDED" if execution_failure is None else "FAILED",
        "member_index": member_index,
        "config_index": config_index,
        "config_hash": config_hash,
        "dataset_id": member.dataset_id,
        "round_id": member.round_id,
        "chromosome": member.chromosome,
        "execution_result_id": None if execution_failure else f"exec-{tag}",
        "execution_result_hash": None if execution_failure else f"eh{tag}".ljust(64, "b"),
        "execution_runtime_ms": None if execution_failure else runtime,
        "execution_environment_hash": None if execution_failure else environment,
        "execution_failure_id": f"execfail-{tag}" if execution_failure else None,
        "execution_failure_code": execution_failure,
        "execution_failure_runtime_ms": runtime if execution_failure else None,
        "execution_failure_environment_hash": environment if execution_failure else None,
        "evaluation_id": None,
        "evaluation_hash": None,
        "scoring_contract_hash": None,
        "minos_score": None,
        "admitted": None,
        "admission_code": None,
        "evaluation_failure_id": None,
        "evaluation_failure_code": None,
        "evaluation_failure_scoring_contract_hash": None,
    }
    if execution_failure is None:
        if evaluation_failure is not None:
            row["evaluation_failure_id"] = f"evalfail-{tag}"
            row["evaluation_failure_code"] = evaluation_failure
            row["evaluation_failure_scoring_contract_hash"] = contract
        else:
            row["evaluation_id"] = f"eval-{tag}"
            row["evaluation_hash"] = f"vh{tag}".ljust(64, "c")
            row["scoring_contract_hash"] = contract
            row["admitted"] = admitted
            row["minos_score"] = score if admitted else None
            row["admission_code"] = "ADMITTED" if admitted else "NONPOSITIVE_SCORE"
    row.update(overrides)
    return row


def _matrix(authority: Any, scores: dict[int, list[float]] | None = None, **kw: Any) -> list[dict]:
    """A complete synthetic 4x10, with per-config utilities chosen BEFORE closure runs."""
    rows = []
    for ci in range(4):
        for mi in range(10):
            score = scores[ci][mi] if scores else 0.5 + 0.01 * ci
            rows.append(_row(authority, member_index=mi, config_index=ci, score=score, **kw))
    return rows


def _closure(authority: Any, rows: list[dict]) -> Any:
    return build_phase_d_closure(rows, authority=authority, baseline_protocol_hash=_PROTOCOL_HASH)


# --------------------------------------------------------------------------------------------
# the objective and the order are delegated, not reimplemented
# --------------------------------------------------------------------------------------------
def test_the_frozen_objective_constants_are_still_the_ones_in_force() -> None:
    from minos_engine.baseline import objective as obj

    assert (
        obj.CVAR_ALPHA,
        obj.CVAR_WEIGHT,
        obj.FLOOR_WEIGHT,
        obj.MEAN_WEIGHT,
        obj.FAILURE_PENALTY,
    ) == (0.25, 0.50, 0.30, 0.20, 1.00)


def test_the_closure_module_reimplements_no_mathematics() -> None:
    from pathlib import Path

    source = Path("src/minos_engine/baseline/phase_d_observations.py").read_text(encoding="utf-8")
    assert "aggregate_candidate(" in source and "rank_candidates(" in source
    for reinvented in (
        "CVAR_ALPHA",
        "0.25",
        "0.50",
        "0.30",
        "0.20",
        "def _cvar",
        "def objective_value",
        "tie_break_key",
        "key=lambda",
    ):
        assert reinvented not in source, reinvented
    # sorted() appears only for deterministic iteration and error text -- never over aggregates.
    assert "sorted(aggregates" not in source
    assert "sorted(ranked" not in source


def test_the_aggregates_match_the_existing_implementation_exactly(authority: Any) -> None:
    from minos_engine.baseline.objective import BaselineObservation, aggregate_candidate

    scores = {ci: [0.10 * (i + 1) for i in range(10)] for ci in range(4)}
    closure = _closure(authority, _matrix(authority, scores))
    for ci, config_hash in enumerate(authority.ordered_config_hashes):
        expected = aggregate_candidate(
            config_hash=config_hash,
            observations=[
                BaselineObservation(
                    config_hash=config_hash,
                    dataset_id=m.dataset_id,
                    chromosome=m.chromosome,
                    minos_score=scores[ci][mi],
                    admitted=True,
                    gatk_runtime_ms=1000,
                )
                for mi, m in enumerate(authority.schedule.members)
            ],
            required_members=authority.required_pairs(),
        )
        got = next(c for c in closure.candidates if c.config_hash == config_hash)
        assert (got.cvar, got.floor, got.mean, got.objective) == (
            expected.cvar,
            expected.floor,
            expected.mean,
            expected.objective,
        )


# --------------------------------------------------------------------------------------------
# §21 — every tie-break level, forced
# --------------------------------------------------------------------------------------------
def test_level_one_higher_objective_wins(authority: Any) -> None:
    scores = {0: [0.5] * 10, 1: [0.9] * 10, 2: [0.5] * 10, 3: [0.5] * 10}
    closure = _closure(authority, _matrix(authority, scores))
    assert closure.ordered_ranking[0] == ORDERED_FINALISTS[1]
    assert closure.selected_config_hash == ORDERED_FINALISTS[1]


def test_level_two_equal_objective_lower_runtime_wins(authority: Any) -> None:
    rows = []
    for ci in range(4):
        for mi in range(10):
            rows.append(
                _row(
                    authority,
                    member_index=mi,
                    config_index=ci,
                    score=0.6,
                    runtime=500 if ci == 2 else 900,
                )
            )
    closure = _closure(authority, rows)
    objectives = {c.config_hash: c.objective for c in closure.candidates}
    assert len(set(objectives.values())) == 1, objectives
    assert closure.ordered_ranking[0] == ORDERED_FINALISTS[2]


def test_level_three_equal_objective_and_runtime_lower_inherited_index_wins(
    authority: Any,
) -> None:
    """Inherited 42/25/36/0 — the SEED holds index 0 and therefore wins this level on merit."""
    closure = _closure(authority, _matrix(authority, {ci: [0.6] * 10 for ci in range(4)}))
    assert len({c.objective for c in closure.candidates}) == 1
    assert len({c.mean_gatk_runtime_ms for c in closure.candidates}) == 1
    assert closure.ordered_ranking[0] == ORDERED_FINALISTS[3]
    assert INHERITED_CANDIDATE_INDEX[closure.ordered_ranking[0]] == 0


def test_substituting_finalist_positions_for_inherited_indices_changes_the_winner(
    authority: Any,
) -> None:
    """§21's decisive regression: 0..3 and 42/25/36/0 disagree, and the frozen one must win.

    Under a full objective+runtime tie the third key decides. Inherited indices put the SEED
    (index 0) first; finalist positions would put ORDERED_FINALISTS[0] (position 0) first. If
    someone later swaps the mapping, this fails rather than silently re-selecting the baseline.
    """
    from minos_engine.baseline.objective import rank_candidates

    rows = _matrix(authority, {ci: [0.6] * 10 for ci in range(4)})
    closure = _closure(authority, rows)
    frozen_winner = closure.ordered_ranking[0]

    aggregates = [
        type("A", (), {})  # placeholder replaced below; we rebuild real aggregates from the closure
    ]
    del aggregates
    from minos_engine.baseline.objective import BaselineObservation, aggregate_candidate

    real = [
        aggregate_candidate(
            config_hash=h,
            observations=[
                BaselineObservation(
                    config_hash=h,
                    dataset_id=m.dataset_id,
                    chromosome=m.chromosome,
                    minos_score=0.6,
                    admitted=True,
                    gatk_runtime_ms=1000,
                )
                for m in authority.schedule.members
            ],
            required_members=authority.required_pairs(),
        )
        for h in authority.ordered_config_hashes
    ]
    positional = rank_candidates(
        real, candidate_index={h: i for i, h in enumerate(ORDERED_FINALISTS)}
    )
    assert positional[0].config_hash != frozen_winner, (
        "the two index mappings agree here, so this regression proves nothing"
    )
    assert frozen_winner == SEED_CONFIG_HASH
    assert positional[0].config_hash == ORDERED_FINALISTS[0]


def test_level_four_full_tie_falls_to_the_smaller_config_hash(authority: Any) -> None:
    """With every earlier key equal INCLUDING the index, order is the hash — total, never random."""
    from minos_engine.baseline.objective import (
        BaselineObservation,
        aggregate_candidate,
        rank_candidates,
    )

    real = [
        aggregate_candidate(
            config_hash=h,
            observations=[
                BaselineObservation(
                    config_hash=h,
                    dataset_id=m.dataset_id,
                    chromosome=m.chromosome,
                    minos_score=0.6,
                    admitted=True,
                    gatk_runtime_ms=1000,
                )
                for m in authority.schedule.members
            ],
            required_members=authority.required_pairs(),
        )
        for h in authority.ordered_config_hashes
    ]
    ranked = rank_candidates(real, candidate_index=dict.fromkeys(ORDERED_FINALISTS, 7))
    assert [a.config_hash for a in ranked] == sorted(ORDERED_FINALISTS)


# --------------------------------------------------------------------------------------------
# §23 — no seed override, in either direction
# --------------------------------------------------------------------------------------------
def test_the_seed_ranking_last_is_not_rescued(authority: Any) -> None:
    scores = {0: [0.9] * 10, 1: [0.8] * 10, 2: [0.7] * 10, 3: [0.1] * 10}
    closure = _closure(authority, _matrix(authority, scores))
    assert closure.selected_config_hash == ORDERED_FINALISTS[0]
    assert closure.seed_config_hash == SEED_CONFIG_HASH
    assert closure.seed_rank == 3
    assert closure.ordered_ranking[-1] == SEED_CONFIG_HASH


def test_the_seed_ranking_first_is_selected_normally(authority: Any) -> None:
    scores = {0: [0.2] * 10, 1: [0.3] * 10, 2: [0.4] * 10, 3: [0.95] * 10}
    closure = _closure(authority, _matrix(authority, scores))
    assert closure.selected_config_hash == SEED_CONFIG_HASH
    assert closure.seed_rank == 0


# --------------------------------------------------------------------------------------------
# §15/§16/§17/§11 — refusals
# --------------------------------------------------------------------------------------------
def test_thirty_nine_observations_refuse(authority: Any) -> None:
    rows = _matrix(authority)[:-1]
    with pytest.raises(PhaseDClosureError, match="frozen cross product"):
        _closure(authority, rows)


def test_a_duplicate_frozen_pair_refuses(authority: Any) -> None:
    rows = _matrix(authority)
    rows.append(_row(authority, member_index=0, config_index=0, score=0.99))
    with pytest.raises(PhaseDClosureError, match="more than one terminal outcome"):
        _closure(authority, rows)


def test_a_row_outside_the_frozen_pairs_refuses(authority: Any) -> None:
    rows = _matrix(authority)
    rows.append({**_row(authority, member_index=0, config_index=0), "member_index": 11})
    with pytest.raises(PhaseDClosureError, match="disagree with the frozen identities"):
        _closure(authority, rows)


def test_an_unknown_finalist_refuses(authority: Any) -> None:
    rows = _matrix(authority)
    rows.append({**_row(authority, member_index=0, config_index=0), "config_hash": "f" * 64})
    with pytest.raises(PhaseDClosureError, match="not a frozen Phase-D finalist"):
        _closure(authority, rows)


def test_an_unknown_member_refuses(authority: Any) -> None:
    rows = _matrix(authority)
    rows.append({**_row(authority, member_index=0, config_index=0), "dataset_id": "minos-chrX-0"})
    with pytest.raises(PhaseDClosureError, match="not a frozen VALIDATION member"):
        _closure(authority, rows)


def test_a_wrong_plan_refuses(authority: Any) -> None:
    rows = _matrix(authority)
    rows[0] = {**rows[0], "plan_hash": "a" * 64}
    with pytest.raises(PhaseDClosureError, match="not the frozen Phase-D campaign"):
        _closure(authority, rows)


def test_a_mixed_execution_environment_refuses(authority: Any) -> None:
    rows = _matrix(authority)
    rows[7] = {**rows[7], "execution_environment_hash": "e" * 64}
    with pytest.raises(PhaseDClosureError, match="execution environments"):
        _closure(authority, rows)


def test_an_infrastructure_incident_refuses(authority: Any) -> None:
    """Our defect, not the candidate's. Ranking over it would charge a machine's failure to it."""
    rows = _matrix(authority)
    rows[3] = _row(authority, member_index=3, config_index=0, evaluation_failure="HAPPY_TIMEOUT")
    with pytest.raises(PhaseDClosureError, match="INFRASTRUCTURE_INCIDENT"):
        _closure(authority, rows)


def test_an_unknown_failure_code_refuses(authority: Any) -> None:
    rows = _matrix(authority)
    rows[3] = _row(authority, member_index=3, config_index=0, execution_failure="MADE_UP_CODE")
    with pytest.raises(Exception, match="unknown bounded failure code"):
        _closure(authority, rows)


def test_rows_under_another_scoring_contract_are_ignored_not_averaged(authority: Any) -> None:
    """A rescore under different semantics is not this campaign's evidence."""
    rows = _matrix(authority, {ci: [0.5] * 10 for ci in range(4)})
    rows.append(_row(authority, member_index=0, config_index=0, score=0.99, contract="d" * 64))
    closure = _closure(authority, rows)
    assert closure.observation_count == 40
    winner = next(c for c in closure.candidates if c.config_hash == ORDERED_FINALISTS[0])
    assert winner.mean == pytest.approx(0.5), "a foreign-contract row entered the aggregate"


def test_two_terminal_outcomes_under_the_frozen_contract_refuse(authority: Any) -> None:
    rows = _matrix(authority)
    rows.append(_row(authority, member_index=2, config_index=1, score=0.31))
    with pytest.raises(PhaseDClosureError, match="more than one terminal outcome"):
        _closure(authority, rows)


def test_a_missing_evaluation_is_neither_zero_nor_a_failure(authority: Any) -> None:
    rows = _matrix(authority)
    rows[5] = {
        **rows[5],
        "evaluation_id": None,
        "evaluation_hash": None,
        "scoring_contract_hash": None,
        "admitted": None,
        "minos_score": None,
    }
    with pytest.raises(PhaseDClosureError, match="no terminal evaluation"):
        _closure(authority, rows)


def test_a_candidate_failure_is_a_valid_penalised_observation(authority: Any) -> None:
    """§14 — a bounded GATK failure is the candidate's own outcome, and closure proceeds."""
    rows = _matrix(authority, {ci: [0.8] * 10 for ci in range(4)})
    rows[4] = _row(authority, member_index=4, config_index=0, execution_failure="GATK_TIMEOUT")
    closure = _closure(authority, rows)
    hurt = next(c for c in closure.candidates if c.config_hash == ORDERED_FINALISTS[0])
    assert hurt.candidate_failure_count == 1
    assert hurt.failure_rate == pytest.approx(0.1)
    assert hurt.infrastructure_incident_count == 0
    assert closure.selected_config_hash != ORDERED_FINALISTS[0]


def test_a_non_admitted_success_carries_no_score(authority: Any) -> None:
    rows = _matrix(authority, {ci: [0.8] * 10 for ci in range(4)})
    rows[6] = _row(authority, member_index=6, config_index=0, admitted=False, score=None)
    closure = _closure(authority, rows)
    observation = next(
        o for o in closure.observations if (o.member_index, o.config_index) == (6, 0)
    )
    assert observation.admitted is False
    assert observation.minos_score is None
    assert observation.failure_code is None


# --------------------------------------------------------------------------------------------
# §24/§25/§26/§32 — the closure identity
# --------------------------------------------------------------------------------------------
def test_the_closure_binds_all_forty_observations_in_frozen_order(authority: Any) -> None:
    closure = _closure(authority, _matrix(authority))
    assert closure.observation_count == 40
    pairs = [(o.member_index, o.config_index) for o in closure.observations]
    assert pairs == sorted(pairs)
    assert set(pairs) == {(m, c) for m in range(10) for c in range(4)}


def test_database_row_order_cannot_change_the_closure_hash(authority: Any) -> None:
    """§32 — the same evidence must close identically however the rows arrive."""
    rows = _matrix(authority, {ci: [0.1 * (i + 1) for i in range(10)] for ci in range(4)})
    forward = compute_phase_d_closure_hash(_closure(authority, list(rows)))
    reversed_hash = compute_phase_d_closure_hash(_closure(authority, list(reversed(rows))))
    shuffled = [
        rows[i]
        for i in (
            7,
            0,
            33,
            12,
            39,
            1,
            25,
            *range(2, 7),
            *range(8, 12),
            *range(13, 25),
            *range(26, 33),
            *range(34, 39),
        )
    ]
    assert len(shuffled) == 40
    assert forward == reversed_hash == compute_phase_d_closure_hash(_closure(authority, shuffled))


def test_the_closure_hash_is_domain_separated_and_carries_no_environment(
    authority: Any,
) -> None:
    closure = _closure(authority, _matrix(authority))
    assert PHASE_D_CLOSURE_DOMAIN == "minos:l2f2-phase-d-validation-closure:v1\n"
    blob = json.dumps(closure.content(), sort_keys=True).lower()
    for leaked in (
        "timestamp",
        "created_at",
        "/home/",
        "hostname",
        "minos_l2f2_validation",
        "postgres",
        '"utc"',
        "generated_utc",
    ):
        assert leaked not in blob, leaked


@pytest.mark.parametrize(
    "mutate",
    [
        pytest.param(lambda r: {**r, "minos_score": 0.99}, id="score"),
        pytest.param(lambda r: {**r, "execution_runtime_ms": 12345}, id="runtime"),
        pytest.param(lambda r: {**r, "evaluation_hash": "9" * 64}, id="evaluation-identity"),
        pytest.param(lambda r: {**r, "job_key": "7" * 64}, id="job-identity"),
    ],
)
def test_any_scientific_change_changes_the_closure_hash(authority: Any, mutate: Any) -> None:
    rows = _matrix(authority)
    baseline = compute_phase_d_closure_hash(_closure(authority, rows))
    changed = list(rows)
    changed[11] = mutate(changed[11])
    assert compute_phase_d_closure_hash(_closure(authority, changed)) != baseline


def test_the_closure_binds_the_selection_interpretation(authority: Any) -> None:
    closure = _closure(authority, _matrix(authority))
    assert closure.selection_interpretation_hash == (
        "4c169912f67877d6ba254fb280dbd2ff44aa4aaaf65bedfa1bca9975f1efebbd"
    )
    assert closure.selection_interpretation_status == (
        "OUTCOME_BLIND_POST_COLLECTION_CLARIFICATION"
    )
    assert closure.baseline_protocol_hash == _PROTOCOL_HASH
    assert closure.scoring_contract_hash == ACCEPTED_SCORING_CONTRACT_HASH
    assert closure.minos_subnet_sha == "649bb92c6abccebde58a736a2b2af7fd77a701c1"


def test_the_selected_config_is_always_rank_zero(authority: Any) -> None:
    for scores in (
        {0: [0.9] * 10, 1: [0.2] * 10, 2: [0.3] * 10, 3: [0.4] * 10},
        {0: [0.2] * 10, 1: [0.9] * 10, 2: [0.3] * 10, 3: [0.4] * 10},
        {0: [0.2] * 10, 1: [0.3] * 10, 2: [0.9] * 10, 3: [0.4] * 10},
        {0: [0.2] * 10, 1: [0.3] * 10, 2: [0.4] * 10, 3: [0.9] * 10},
    ):
        closure = _closure(authority, _matrix(authority, scores))
        assert closure.selected_config_hash == closure.ordered_ranking[0]
        assert closure.candidates[0].rank == 0
        assert closure.candidates[0].config_hash == closure.ordered_ranking[0]


def test_replay_produces_an_identical_closure_hash(authority: Any) -> None:
    rows = _matrix(authority)
    assert compute_phase_d_closure_hash(_closure(authority, rows)) == (
        compute_phase_d_closure_hash(_closure(authority, rows))
    )


def test_the_reader_refuses_an_authority_that_is_not_the_frozen_one(authority: Any) -> None:
    import dataclasses

    impostor = dataclasses.replace(authority, plan_hash="b" * 64)
    with pytest.raises(PhaseDClosureError, match="not the frozen Phase-D plan"):
        derive_phase_d_observations(_matrix(authority), authority=impostor)
