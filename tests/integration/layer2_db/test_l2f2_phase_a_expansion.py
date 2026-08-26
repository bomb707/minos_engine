"""The bounded Phase-A expansion boundary, against the REAL frozen 195-job screen.

Expansion is the only way the remaining 194 logical jobs can be enqueued, and everything
scientific about them — plan, members, candidates, job keys, order — is recomputed from committed
authority. A caller chooses a contiguous slice of the frozen order and nothing else.

These controls prove the three properties that matter: the slice is bounded and cannot reach the
completed canary, replay adds and resets nothing, and the readiness gate is about the PIPELINE
being proven end to end, never about the score the canary produced.

No GATK and no MINOS_SUBNET: executions use ``FakeGatkRunner`` through the private test seam and
scores are recorded upstream results. Nothing here touches the real baseline store.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import text

from minos_engine.storage.l2f2_phase_a_control import (
    CANARY_LOGICAL_INDEX,
    PHASE_A_LOGICAL_JOB_COUNT,
    PhaseAExpansionError,
    expand_l2f2_phase_a_jobs,
    read_l2f2_phase_a_progress,
)
from minos_engine.storage.l2f_job_enqueue import MAX_ENQUEUE_BATCH
from tests.integration.layer2_db.l2f2_phase_a_env import close_the_canary, phase_a_store

#: the four explicit operator acts that cover the 194 remaining jobs. There is no enqueue-all.
_FULL_EXPANSION = ((1, 64), (65, 64), (129, 64), (193, 2))


@pytest.fixture
def phase_a(isolated_pg_base_url: str, tmp_path: Path) -> Any:
    with phase_a_store(isolated_pg_base_url, tmp_path) as env:
        yield env


@pytest.fixture
def closed(phase_a: Any) -> Any:
    """The canary executed and evaluated: the state expansion requires."""
    close_the_canary(phase_a)
    return phase_a


def _job_keys(env: Any) -> list[str]:
    with env.engine.connect() as conn:
        return [
            str(row[0])
            for row in conn.execute(
                text(
                    "SELECT j.job_key FROM experiments.l2f_experiment_jobs j "
                    "  JOIN experiments.l2f_experiment_plan_members pm ON pm.id = j.plan_member_id "
                    "  JOIN experiments.l2f_experiment_plan_configs pc ON pc.id = j.plan_config_id "
                    " ORDER BY pm.member_index, pc.config_index"
                )
            )
        ]


def _frozen_keys() -> list[str]:
    from minos_engine.baseline.phase_a import build_phase_a_authority
    from minos_engine.experiments.plan import iter_logical_jobs

    return [job.job_key for job in iter_logical_jobs(build_phase_a_authority().plan)]


def _canary_row(env: Any) -> dict[str, Any]:
    with env.engine.connect() as conn:
        return dict(
            conn.execute(
                text(
                    "SELECT status, claimed_by, claimed_at, created_at "
                    "  FROM experiments.l2f_experiment_jobs WHERE job_key = :k"
                ),
                {"k": env.authority.canary.job_key},
            )
            .mappings()
            .one()
        )


# --------------------------------------------------------------------------- #
# the bounded slice
# --------------------------------------------------------------------------- #
def test_four_bounded_slices_enqueue_exactly_the_frozen_195(closed: Any) -> None:
    """The whole screen is reachable, and only through explicit bounded operator acts."""
    before = _canary_row(closed)
    created_total = 0
    for start, count in _FULL_EXPANSION:
        result = expand_l2f2_phase_a_jobs(closed.engine, start=start, count=count)
        assert (result.start, result.count) == (start, count)
        assert result.created == count
        assert result.existing == 0
        created_total += result.created

    assert created_total == PHASE_A_LOGICAL_JOB_COUNT - 1, "the canary is never re-enqueued"
    assert _job_keys(closed) == _frozen_keys(), "enqueued identity or order is not the frozen one"
    assert closed.count("SELECT count(*) FROM experiments.l2f_experiment_jobs") == 195

    # the canary is untouched in every observable respect.
    assert _canary_row(closed) == before
    assert closed.count("SELECT count(*) FROM experiments.l2f_execution_results") == 1
    assert closed.count("SELECT count(*) FROM evaluation.l2f_evaluation_results") == 1
    # expansion is control-plane only: no plan, member, config or payload is re-created.
    assert closed.count("SELECT count(*) FROM experiments.l2f_experiment_plans") == 1
    assert closed.count("SELECT count(*) FROM experiments.l2f_experiment_plan_members") == 5
    assert closed.count("SELECT count(*) FROM experiments.l2f_experiment_plan_configs") == 39
    assert closed.count("SELECT count(*) FROM experiments.l2f2_execution_authorities") == 1

    progress = read_l2f2_phase_a_progress(closed.engine)
    assert progress.enqueued_count == 195
    assert progress.pending_count == 194
    assert progress.succeeded_count == 1
    assert progress.decided_observation_count == 1
    assert progress.missing_observation_count == 194
    assert progress.complete is False


def test_replay_and_overlap_add_nothing_twice(closed: Any) -> None:
    """Idempotent by logical identity: an overlapping slice inserts only what is missing."""
    first = expand_l2f2_phase_a_jobs(closed.engine, start=1, count=32)
    assert (first.created, first.existing) == (32, 0)

    replay = expand_l2f2_phase_a_jobs(closed.engine, start=1, count=32)
    assert (replay.created, replay.existing) == (0, 32)
    assert replay.jobs_total_after == first.jobs_total_after

    overlap = expand_l2f2_phase_a_jobs(closed.engine, start=17, count=32)
    assert (overlap.created, overlap.existing) == (16, 16), "only the unseen half is inserted"
    assert closed.count("SELECT count(*) FROM experiments.l2f_experiment_jobs") == 49
    assert _job_keys(closed) == _frozen_keys()[:49]


def test_a_replay_never_resets_a_job_that_already_ran(closed: Any) -> None:
    """A slice replayed over executed jobs leaves their terminal state exactly as it was."""
    expand_l2f2_phase_a_jobs(closed.engine, start=1, count=2)
    ran = closed.run(worker_id="ci-expand-1")
    assert ran is not None and ran.status == "SUCCEEDED"
    before = closed.status(ran.job_key)

    replayed = expand_l2f2_phase_a_jobs(closed.engine, start=1, count=2)
    assert (replayed.created, replayed.existing) == (0, 2)
    assert closed.status(ran.job_key) == before == "SUCCEEDED"
    assert closed.count("SELECT count(*) FROM experiments.l2f_execution_results") == 2


@pytest.mark.parametrize(
    ("start", "count", "message"),
    [
        (0, 1, "completed canary"),
        (-1, 1, "completed canary"),
        (1, 0, "count must be >= 1"),
        (1, -5, "count must be >= 1"),
        (1, MAX_ENQUEUE_BATCH + 1, "no enqueue-all"),
        (1, 195, "no enqueue-all"),
        (194, 2, "runs past the frozen Phase-A screen"),
        (195, 1, "runs past the frozen Phase-A screen"),
        (True, 1, "start must be an int"),
        (1, True, "count must be an int"),
    ],
)
def test_an_out_of_contract_range_is_refused_before_any_database_access(
    closed: Any, start: Any, count: Any, message: str
) -> None:
    with pytest.raises(PhaseAExpansionError, match=message):
        expand_l2f2_phase_a_jobs(closed.engine, start=start, count=count)
    assert closed.count("SELECT count(*) FROM experiments.l2f_experiment_jobs") == 1


def test_the_canary_index_is_unreachable_by_arithmetic(closed: Any) -> None:
    """``start`` begins at 1, so no slice can include logical job 0 by accident."""
    assert CANARY_LOGICAL_INDEX == 0
    result = expand_l2f2_phase_a_jobs(closed.engine, start=1, count=1)
    assert result.created == 1
    keys = _job_keys(closed)
    assert keys[0] == closed.authority.canary.job_key
    assert keys[1] == _frozen_keys()[1]
    assert len(keys) == len(set(keys)) == 2


# --------------------------------------------------------------------------- #
# the readiness gate
# --------------------------------------------------------------------------- #
def test_expansion_is_refused_while_the_canary_has_not_executed(phase_a: Any) -> None:
    with pytest.raises(PhaseAExpansionError, match="not SUCCEEDED"):
        expand_l2f2_phase_a_jobs(phase_a.engine, start=1, count=1)
    assert phase_a.count("SELECT count(*) FROM experiments.l2f_experiment_jobs") == 1


def test_expansion_is_refused_while_the_canary_is_unevaluated(phase_a: Any) -> None:
    """A GATK success alone does not prove the pipeline: scoring must have closed too."""
    dispatched = phase_a.run(worker_id="ci-unevaluated")
    assert dispatched is not None and dispatched.status == "SUCCEEDED"

    with pytest.raises(PhaseAExpansionError, match="terminal evaluation"):
        expand_l2f2_phase_a_jobs(phase_a.engine, start=1, count=1)
    assert phase_a.count("SELECT count(*) FROM experiments.l2f_experiment_jobs") == 1


def test_expansion_is_refused_when_the_canary_evaluation_failed(phase_a: Any) -> None:
    dispatched = phase_a.run(worker_id="ci-eval-failed")
    assert dispatched is not None and dispatched.status == "SUCCEEDED"
    phase_a.register_truth()
    phase_a.fail_evaluation(dispatched, failure_code="HAPPY_TIMEOUT")

    with pytest.raises(PhaseAExpansionError, match="evaluation failure"):
        expand_l2f2_phase_a_jobs(phase_a.engine, start=1, count=1)


@pytest.mark.parametrize(
    ("minos_score", "admitted", "admission_code"),
    [
        (0.0001, True, "ADMITTED"),
        (0.0, False, "NONPOSITIVE_SCORE"),
    ],
)
def test_the_gate_is_about_the_pipeline_not_the_score(
    phase_a: Any, minos_score: float, admitted: bool, admission_code: str
) -> None:
    """A near-zero score, and a refused admission, both still permit expansion.

    Conditioning the screen on the canary's own number after seeing it would be a protocol change
    made from one observation, which is exactly what the frozen design exists to prevent.
    """
    dispatched = phase_a.run(worker_id="ci-any-score")
    assert dispatched is not None and dispatched.status == "SUCCEEDED"
    phase_a.register_truth()
    phase_a.evaluate(
        dispatched, minos_score=minos_score, admitted=admitted, admission_code=admission_code
    )

    result = expand_l2f2_phase_a_jobs(phase_a.engine, start=1, count=4)
    assert result.created == 4


# --------------------------------------------------------------------------- #
# state the screen was not frozen against
# --------------------------------------------------------------------------- #
# coexistence with another plan, and forgery inside our own
# --------------------------------------------------------------------------- #
def test_a_second_legitimate_plan_does_not_disturb_phase_a(closed: Any, tmp_path: Path) -> None:
    """Phase B will live in this same store. Its rows are simply not Phase-A's business.

    The reader used to select every job in the database and refuse the moment it saw one it did
    not recognise. That was correct while Phase A was the only plan and becomes wrong the instant
    a second legitimate plan is persisted, so the scope narrowed to the Phase-A plan hash — which
    tightens nothing and relaxes nothing about what Phase-A rows must prove.
    """
    from minos_engine.baseline.phase_a_observations import load_phase_a_observations
    from minos_engine.experiments.accepted_plan import build_accepted_experiment_plan
    from minos_engine.storage.l2f2_phase_a_control import read_l2f2_phase_a_progress
    from minos_engine.storage.l2f_job_enqueue import _enqueue_experiment_jobs_with_trust
    from minos_engine.storage.l2f_plan_store import _persist_experiment_plan_with_trust
    from tests.integration.layer2_db.l2f_plan_seed import (
        seed_upstream_for_plan,
        split_snapshot_id_for,
    )
    from tests.integration.layer2_db.test_l2f_plan_store import (
        _CS,
        _SNAPSHOT_A,
        _publisher,
        _synthetic_plan,
    )

    before_progress = read_l2f2_phase_a_progress(closed.engine)
    before_observations = load_phase_a_observations(closed.engine)

    other = _synthetic_plan(_SNAPSHOT_A)
    assert other.plan_hash != closed.plan.plan_hash
    with closed.engine.connect() as conn, conn.begin():
        seed_upstream_for_plan(
            conn,
            other,
            variant=1,
            parent_split_snapshot_id=split_snapshot_id_for(build_accepted_experiment_plan()),
        )
    _persist_experiment_plan_with_trust(
        closed.engine, other, _CS, publisher=_publisher(closed.config_root)
    )
    _enqueue_experiment_jobs_with_trust(closed.engine, other, _CS, start=0, count=2)

    # Phase A is untouched by the newcomer, and can still be expanded.
    assert read_l2f2_phase_a_progress(closed.engine) == before_progress
    assert load_phase_a_observations(closed.engine) == before_observations
    result = expand_l2f2_phase_a_jobs(closed.engine, start=1, count=4)
    assert (result.created, result.existing) == (4, 0)
    assert result.jobs_total_after == 5, "the foreign plan's jobs are not counted as Phase-A jobs"

    after = read_l2f2_phase_a_progress(closed.engine)
    assert after.enqueued_count == 5
    assert after.decided_observation_count == before_progress.decided_observation_count
    assert after.execution_result_count == before_progress.execution_result_count


def test_a_forged_job_claiming_the_phase_a_plan_still_fails_closed(closed: Any) -> None:
    """Scoping narrowed WHAT is read, not what a Phase-A row must prove.

    A row inside the Phase-A plan whose key is not one of the frozen 195 is corruption, and it
    refuses exactly as it always did.
    """
    with closed.engine.connect() as conn, conn.begin():
        conn.execute(text("SET LOCAL ROLE minos_admin"))
        # a logical identity that has no job yet, so the forgery is refused by Phase-A identity
        # verification rather than by the schema's own uniqueness rule.
        conn.execute(
            text(
                "INSERT INTO experiments.l2f_experiment_jobs "
                "  (plan_id, plan_member_id, plan_config_id, job_key, status) "
                "SELECT p.id, pm.id, pc.id, :forged, 'PENDING' "
                "  FROM experiments.l2f_experiment_plans p "
                "  JOIN experiments.l2f_experiment_plan_members pm ON pm.plan_id = p.id "
                "  JOIN experiments.l2f_experiment_plan_configs pc ON pc.plan_id = p.id "
                " WHERE p.plan_hash = :plan_hash AND pm.member_index = 4 AND pc.config_index = 38"
            ),
            {"forged": "d" * 64, "plan_hash": closed.plan.plan_hash},
        )

    with pytest.raises(PhaseAExpansionError, match="not one of the frozen"):
        expand_l2f2_phase_a_jobs(closed.engine, start=1, count=1)
