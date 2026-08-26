"""Phase B against REAL PostgreSQL, coexisting with a complete Phase A in one store.

Everything here runs on the production boundaries: the Phase-A screen is completed through the
real runner and the real evaluator seams, the Phase-B authority is derived from that ledger, the
plan is persisted through the accepted persistence core, and batches are materialized through the
bounded control boundary. No plan, job, execution or evaluation row is inserted by hand.

Two plans share this database for the whole module, which is the state that used to be
unrepresentable: the Phase-A readers counted rows globally and would have absorbed Phase-B
executions. The controls below pin the scoping in both directions.

No GATK and no MINOS_SUBNET: executions use ``FakeGatkRunner`` through the private test seam and
scores are recorded upstream results, exactly as the Phase-A suites do.

The module is stateful by design — a complete Phase A, then Phase B's plan and its first batch —
so the fixtures, not the test order, own every state transition.

It also pins the two gates between a materialized Phase-B job and an executed one: the store must
be at ``0016``, and a ``PHASE_B`` execution authority must have been prepared for this exact plan.
Neither is a side effect of filling a queue.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DatabaseError

from minos_engine.baseline.phase_a_analysis import derive_completed_phase_a_analysis
from minos_engine.baseline.phase_a_observations import load_phase_a_observations
from minos_engine.baseline.phase_b import (
    PHASE_B_CANDIDATE_COUNT,
    PHASE_B_LOGICAL_JOB_COUNT,
    PHASE_B_MEMBER_COUNT,
    build_l2f2_phase_b_authority,
)
from minos_engine.baseline.phase_b_observations import load_phase_b_observations
from minos_engine.storage.l2f2_phase_a_control import read_l2f2_phase_a_progress
from minos_engine.storage.l2f2_phase_b_control import (
    PhaseBExpansionError,
    eligible_batch_jobs,
    expand_l2f2_phase_b_batch,
    race_l2f2_phase_b_batch0,
    read_l2f2_phase_b_progress,
    select_l2f2_phase_c_candidates,
)
from tests.integration.layer2_db.l2f2_phase_a_env import (
    TEST_EXECUTION_ENVIRONMENT,
    close_the_canary,
    phase_a_store,
)

_BATCH0_JOBS = 240


def _score_for(config_index: int, member_index: int) -> float:
    """A deterministic synthetic upstream score.

    The seed is deliberately the WORST candidate in the screen. Nothing in the protocol may let
    that remove it: it is never eliminated by racing and it is always promoted, because every
    later phase has to stay comparable against the configuration in use today.
    """
    if config_index == 0:
        return round(0.05 + member_index * 0.001, 12)
    raw = ((config_index * 37 + member_index * 11) % 83) / 100.0
    return round(0.20 + raw * 0.79, 12)


def _index_map(plan: Any) -> dict[str, tuple[int, int]]:
    from minos_engine.experiments.plan import iter_logical_jobs

    return {j.job_key: (j.member_index, j.config_index) for j in iter_logical_jobs(plan)}


def _run(env: Any, authority: Any, *, worker_id: str) -> Any:
    """Execute the next PENDING job OF THIS PLAN through the least-privilege runner."""
    from minos_engine.storage.l2f2_runner import _execute_l2f2_job
    from minos_engine.storage.l2f_gatk_runner import FakeGatkRunner

    return _execute_l2f2_job(
        env.service,
        authority,
        worker_id=worker_id,
        runner=FakeGatkRunner(),
        dataset_root=env.dataset_root,
        publisher=env.publisher,
        work_root=env.work_root,
        execution_environment=TEST_EXECUTION_ENVIRONMENT,
    )


def _drain(env: Any, authority: Any, *, prefix: str) -> tuple[int, set[str]]:
    """Run every PENDING job of one plan to a decided observation. Returns count and job keys."""
    index_map = _index_map(authority.plan)
    claimed: set[str] = set()
    while True:
        dispatched = _run(env, authority, worker_id=f"{prefix}-{len(claimed) % 64}")
        if dispatched is None:
            return len(claimed), claimed
        assert dispatched.status == "SUCCEEDED"
        assert dispatched.job_key not in claimed
        claimed.add(dispatched.job_key)
        member_index, config_index = index_map[dispatched.job_key]
        env.evaluate(dispatched, minos_score=_score_for(config_index, member_index))


def _complete_phase_a(env: Any) -> None:
    """Drive the frozen 195-job Phase-A screen to completion through the production seams."""
    from minos_engine.storage.l2f2_phase_a_control import expand_l2f2_phase_a_jobs

    close_the_canary(env, minos_score=_score_for(0, 0))
    for start, count in ((1, 64), (65, 64), (129, 64), (193, 2)):
        expand_l2f2_phase_a_jobs(env.engine, start=start, count=count)
    executed, _keys = _drain(env, env.authority, prefix="ci-phase-a")
    assert executed == 194, "the canary was already decided; the other 194 are executed here"
    progress = read_l2f2_phase_a_progress(env.engine)
    assert progress.complete, f"Phase A did not complete: {progress}"


@pytest.fixture(scope="module")
def campaign(isolated_pg_base_url: str, tmp_path_factory: Any) -> Any:
    """ONE store holding a COMPLETE Phase A. Built once — 195 executions is not free."""
    tmp_path = tmp_path_factory.mktemp("phase_b_campaign")
    with phase_a_store(isolated_pg_base_url, tmp_path) as env:
        _complete_phase_a(env)
        yield env


@pytest.fixture(scope="module")
def persisted(campaign: Any) -> Any:
    """Phase B's plan persisted beside the completed Phase A."""
    from minos_engine.storage.l2f_plan_store import _persist_l2f2_phase_b_plan_with_trust
    from tests.integration.layer2_db.test_l2f_plan_store import _publisher

    result = _persist_l2f2_phase_b_plan_with_trust(
        campaign.engine, publisher=_publisher(campaign.config_root)
    )
    return campaign, result


@pytest.fixture(scope="module")
def authority(persisted: Any) -> Any:
    env, _result = persisted
    return build_l2f2_phase_b_authority(env.engine)


# --------------------------------------------------------------------------- #
# the Phase-A result boundary Phase B is derived from
# --------------------------------------------------------------------------- #
def test_the_phase_a_analysis_is_derived_from_the_ledger_and_is_deterministic(
    campaign: Any,
) -> None:
    first, first_hash = derive_completed_phase_a_analysis(campaign.engine)
    second, second_hash = derive_completed_phase_a_analysis(campaign.engine)

    assert first_hash == second_hash, "the same immutable ledger must re-derive the same identity"
    assert first.design == second.design
    assert len(first.aggregates) == 39
    assert len(first.dimensions) == 6
    assert len(first.anchors) == 6
    assert len(first.design.ordered_config_hashes) == PHASE_B_CANDIDATE_COUNT


def test_the_analysis_identity_binds_runtime_as_well_as_score(campaign: Any) -> None:
    """Anchor ties break on mean GATK runtime, so runtime can move the design and must be bound."""
    from minos_engine.baseline.phase_a_analysis import compute_phase_a_analysis_hash

    snapshot = load_phase_a_observations(campaign.engine)
    kwargs = {
        "plan_hash": "a" * 64,
        "protocol_hash": "b" * 64,
        "scoring_contract_hash": "c" * 64,
        "execution_environment_hash": "d" * 64,
    }
    base = compute_phase_a_analysis_hash(snapshot, **kwargs)

    moved = snapshot.observations[0].model_copy(
        update={"gatk_runtime_ms": snapshot.observations[0].gatk_runtime_ms + 1}
    )
    perturbed = replace(snapshot, observations=(moved, *snapshot.observations[1:]))
    assert compute_phase_a_analysis_hash(perturbed, **kwargs) != base


def test_an_authority_over_an_unpersisted_plan_authorizes_nothing(campaign: Any) -> None:
    """Preparation never persists a plan as a side effect of authorizing one."""
    from minos_engine.storage.l2f2_phase_b_prepare import (
        PhaseBAuthorityPreparationError,
        prepare_l2f2_phase_b_execution_authority,
    )

    with pytest.raises(PhaseBAuthorityPreparationError, match="not persisted"):
        prepare_l2f2_phase_b_execution_authority(campaign.engine)


# --------------------------------------------------------------------------- #
# persistence — and the proof that 0015 already represents Phase B
# --------------------------------------------------------------------------- #
def test_phase_b_persists_beside_phase_a_without_a_new_migration(
    persisted: Any, authority: Any
) -> None:
    """The generic plan schema already represents a second plan; 0015 needed no change."""
    env, result = persisted

    with env.engine.connect() as conn:
        conn.execute(text("SET TRANSACTION READ ONLY"))
        revision = conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
        plans = {
            str(r["plan_hash"]): dict(r)
            for r in conn.execute(
                text(
                    "SELECT p.plan_hash, p.candidate_set_hash, p.train_member_count, "
                    "       p.candidate_count, p.logical_job_count "
                    "  FROM experiments.l2f_experiment_plans p"
                )
            ).mappings()
        }
    assert revision == "0016_l2f2_phase_b_execution"
    assert len(plans) == 2, "Phase A and Phase B now coexist"
    row = plans[authority.plan_hash]
    assert int(row["train_member_count"]) == PHASE_B_MEMBER_COUNT == 10
    assert int(row["candidate_count"]) == PHASE_B_CANDIDATE_COUNT == 48
    assert int(row["logical_job_count"]) == PHASE_B_LOGICAL_JOB_COUNT == 480
    assert row["candidate_set_hash"] == authority.phase_b_candidate_set_hash
    assert result.plan_created is True
    assert result.replay is False
    assert (result.member_count, result.config_count) == (10, 48)
    assert result.jobs_count == 0, "persisting a plan never creates jobs"


def test_the_seed_and_anchor_payloads_are_reused_by_content_identity(
    persisted: Any, authority: Any
) -> None:
    """Seven of the 48 are Phase-A configurations; only the 41 novel LHS payloads are new."""
    env, _result = persisted
    shared = {authority.seed_config_hash, *authority.anchor_config_hashes}

    with env.engine.connect() as conn:
        conn.execute(text("SET TRANSACTION READ ONLY"))
        payload_rows = {
            str(r["config_hash"]): int(r["n"])
            for r in conn.execute(
                text(
                    "SELECT config_hash, count(*) AS n FROM experiments.l2f_config_payloads "
                    " GROUP BY config_hash"
                )
            ).mappings()
        }
    for config_hash in shared:
        assert payload_rows[config_hash] == 1, "a shared CONFIG payload was duplicated"
    assert len(payload_rows) == 39 + 41, "only the novel LHS payloads are new"


def test_persistence_replay_inserts_nothing(persisted: Any, authority: Any) -> None:
    from minos_engine.storage.l2f_plan_store import _persist_l2f2_phase_b_plan_with_trust
    from tests.integration.layer2_db.test_l2f_plan_store import _publisher

    env, _result = persisted
    replay = _persist_l2f2_phase_b_plan_with_trust(
        env.engine, publisher=_publisher(env.config_root)
    )
    assert replay.plan_created is False
    assert replay.replay is True
    assert replay.artifacts_created == 0
    assert replay.plan_hash == authority.plan_hash


# --------------------------------------------------------------------------- #
# plan scoping, in BOTH directions, with both plans persisted
# --------------------------------------------------------------------------- #
def test_phase_a_readers_ignore_phase_b_entirely(persisted: Any) -> None:
    """The defect this stage had to fix before Phase B could exist at all."""
    env, _result = persisted
    progress = read_l2f2_phase_a_progress(env.engine)
    observations = load_phase_a_observations(env.engine)

    assert (progress.enqueued_count, progress.decided_observation_count) == (195, 195)
    assert progress.complete is True
    assert len(observations.observations) == 195
    assert observations.execution_result_count == 195
    assert observations.evaluation_result_count == 195


def test_phase_b_readers_ignore_phase_a_entirely(persisted: Any) -> None:
    """Phase A holds 195 decided observations. None of them is a Phase-B observation."""
    env, _result = persisted
    snapshot = load_phase_b_observations(env.engine)

    assert snapshot.observations == ()
    assert snapshot.execution_result_count == 0
    assert snapshot.evaluation_result_count == 0


def test_a_shared_config_and_a_shared_dataset_do_not_cross_contaminate(
    persisted: Any, authority: Any
) -> None:
    """The seed, the six anchors and all five batch-0 members are in BOTH plans by construction."""
    env, _result = persisted
    phase_a = load_phase_a_observations(env.engine)

    shared_configs = {o.config_hash for o in phase_a.observations} & set(
        authority.design.ordered_config_hashes
    )
    shared_datasets = {o.dataset_id for o in phase_a.observations} & {
        m.dataset_id for m in authority.plan.members
    }
    assert len(shared_configs) == 7, "the seed and the six anchors really are in both plans"
    assert len(shared_datasets) == 5, "batch 0's members really are the Phase-A members"
    assert load_phase_b_observations(env.engine).observations == ()


def test_a_foreign_plans_jobs_enter_neither_observation_set(
    persisted: Any, authority: Any, tmp_path: Path
) -> None:
    """A third, unrelated plan in the same store is simply not either screen's business."""
    from minos_engine.experiments.accepted_plan import build_accepted_experiment_plan
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

    env, _result = persisted
    before_a = read_l2f2_phase_a_progress(env.engine)
    before_b = load_phase_b_observations(env.engine, authority=authority)

    other = _synthetic_plan(_SNAPSHOT_A)
    assert other.plan_hash not in {env.plan.plan_hash, authority.plan_hash}
    with env.engine.connect() as conn, conn.begin():
        seed_upstream_for_plan(
            conn,
            other,
            variant=1,
            parent_split_snapshot_id=split_snapshot_id_for(build_accepted_experiment_plan()),
        )
    _persist_experiment_plan_with_trust(
        env.engine, other, _CS, publisher=_publisher(env.config_root)
    )
    _enqueue_experiment_jobs_with_trust(env.engine, other, _CS, start=0, count=2)

    assert read_l2f2_phase_a_progress(env.engine) == before_a
    assert load_phase_b_observations(env.engine, authority=authority) == before_b
    assert load_phase_a_observations(env.engine).execution_result_count == 195


def test_the_phase_a_runner_will_not_reach_into_phase_b(persisted: Any, authority: Any) -> None:
    """Phase A is complete and Phase B is about to hold every PENDING job in the database."""
    env, _result = persisted
    expansion = expand_l2f2_phase_b_batch(env.engine, batch_index=0, start=0, count=8)
    assert expansion.created == 8

    assert _run(env, env.authority, worker_id="ci-phase-a-intruder") is None
    assert read_l2f2_phase_a_progress(env.engine).execution_result_count == 195


# --------------------------------------------------------------------------- #
# bounded batch materialization
# --------------------------------------------------------------------------- #
def test_batch_0_is_every_candidate_times_the_five_batch_0_members(authority: Any) -> None:
    eligible = eligible_batch_jobs(authority, batch_index=0)

    assert len(eligible) == _BATCH0_JOBS == PHASE_B_CANDIDATE_COUNT * 5
    assert len(set(eligible)) == _BATCH0_JOBS
    assert authority.batch_members(0) == (0, 1, 2, 3, 4)
    assert authority.batch_members(1) == (5, 6, 7, 8, 9)
    assert [c for _d, c in authority.required_pairs(1)] == [
        "chr18",
        "chr19",
        "chr20",
        "chr21",
        "chr22",
    ]
    assert [c for _d, c in authority.required_pairs()][5:] == [
        "chr18",
        "chr19",
        "chr20",
        "chr21",
        "chr22",
    ]


@pytest.mark.parametrize(
    ("start", "count"),
    [(0, 0), (0, -1), (0, 65), (-1, 1), (239, 2), (240, 1)],
)
def test_an_out_of_contract_slice_is_refused(persisted: Any, start: int, count: int) -> None:
    env, _result = persisted
    with pytest.raises(PhaseBExpansionError):
        expand_l2f2_phase_b_batch(env.engine, batch_index=0, start=start, count=count)


@pytest.mark.parametrize("batch_index", [-1, 2, 7])
def test_a_batch_outside_the_frozen_two_is_refused(authority: Any, batch_index: int) -> None:
    with pytest.raises(PhaseBExpansionError, match="outside"):
        eligible_batch_jobs(authority, batch_index=batch_index)


def test_nothing_downstream_of_batch_0_may_happen_while_it_is_incomplete(persisted: Any) -> None:
    """Not after a chromosome, not after most of the batch — only after all five, for all 48."""
    env, _result = persisted
    with pytest.raises(PhaseBExpansionError, match="batch 0 is not complete"):
        race_l2f2_phase_b_batch0(env.engine)
    with pytest.raises(PhaseBExpansionError, match="batch 0 is not complete"):
        expand_l2f2_phase_b_batch(env.engine, batch_index=1, start=0, count=1)
    with pytest.raises(PhaseBExpansionError, match="batch 0 is not complete"):
        select_l2f2_phase_c_candidates(env.engine)

    progress = read_l2f2_phase_b_progress(env.engine)
    assert progress.logical_job_count == PHASE_B_LOGICAL_JOB_COUNT
    assert progress.batch0_complete is False
    assert progress.complete is False
    assert progress.batch1_eligible_candidate_count == 0
    assert read_l2f2_phase_a_progress(env.engine).decided_observation_count == 195


def test_materialization_is_idempotent_and_plan_scoped(persisted: Any) -> None:
    env, _result = persisted
    first = expand_l2f2_phase_b_batch(env.engine, batch_index=0, start=0, count=64)
    replay = expand_l2f2_phase_b_batch(env.engine, batch_index=0, start=0, count=64)

    assert first.existing + first.created == 64
    assert (replay.created, replay.existing) == (0, 64)
    assert replay.eligible_total == _BATCH0_JOBS
    assert read_l2f2_phase_a_progress(env.engine).enqueued_count == 195


# --------------------------------------------------------------------------- #
# the runner boundary: where Phase B actually stops today
# --------------------------------------------------------------------------- #
def test_a_phase_b_job_cannot_be_resolved_before_its_authority_is_prepared(
    persisted: Any, authority: Any
) -> None:
    """Materializing a job is not authorizing it — 0016 admits Phase B, it does not assume it.

    The queue is filled by the control plane; the right to execute from it comes from a separate
    administrative act. Until that act happens the runner claims a job and then cannot resolve it,
    which is exactly the failure a missing authority should produce.
    """
    env, _result = persisted
    before = read_l2f2_phase_b_progress(env.engine)
    expand_l2f2_phase_b_batch(env.engine, batch_index=0, start=0, count=8)

    with pytest.raises(DatabaseError, match="has no PHASE_B L2-F2 execution authority"):
        _run(env, authority, worker_id="ci-phase-b-unauthorized")

    after = read_l2f2_phase_b_progress(env.engine)
    assert after.execution_result_count == before.execution_result_count == 0
    assert after.decided_observation_count == 0
    assert read_l2f2_phase_a_progress(env.engine).decided_observation_count == 195


def test_the_full_batch_0_materializes_and_stays_pending(persisted: Any, authority: Any) -> None:
    """The control plane can put every batch-0 job in the queue; only execution is gated."""
    env, _result = persisted
    eligible = eligible_batch_jobs(authority, batch_index=0)
    for start in range(0, len(eligible), 64):
        expand_l2f2_phase_b_batch(
            env.engine, batch_index=0, start=start, count=min(64, len(eligible) - start)
        )

    progress = read_l2f2_phase_b_progress(env.engine)
    assert progress.enqueued_count == _BATCH0_JOBS
    assert progress.pending_count + progress.claimed_count == _BATCH0_JOBS
    assert progress.batch0_decided_count == 0
    assert progress.batch0_complete is False
    assert progress.complete is False
    assert read_l2f2_phase_a_progress(env.engine).enqueued_count == 195


# --------------------------------------------------------------------------- #
# the administrative act that authorizes execution
# --------------------------------------------------------------------------- #
@pytest.fixture(scope="module")
def authorized(persisted: Any, authority: Any) -> Any:
    """The Phase-B execution authority, recorded through the production control-plane boundary."""
    from minos_engine.storage.l2f2_phase_b_prepare import (
        prepare_l2f2_phase_b_execution_authority,
    )

    env, _result = persisted
    return env, prepare_l2f2_phase_b_execution_authority(env.engine)


def test_the_authority_is_derived_and_carries_no_canary(authorized: Any, authority: Any) -> None:
    """Every value comes from the ledger; the one Phase-A concept it must NOT invent is absent."""
    env, result = authorized

    assert result.created is True
    assert result.phase == "PHASE_B"
    assert result.plan_hash == authority.plan_hash
    assert result.candidate_set_hash == authority.phase_b_candidate_set_hash
    assert (result.member_count, result.candidate_count) == (10, 48)
    assert result.logical_job_count == PHASE_B_LOGICAL_JOB_COUNT
    assert result.canary_job_key is None

    with env.engine.connect() as conn:
        conn.execute(text("SET TRANSACTION READ ONLY"))
        rows = [
            dict(r)
            for r in conn.execute(
                text(
                    "SELECT phase, plan_hash, candidate_set_hash, member_count, candidate_count, "
                    "       logical_job_count, canary_job_key, baseline_protocol_hash "
                    "  FROM experiments.l2f2_execution_authorities ORDER BY phase"
                )
            ).mappings()
        ]
    assert [r["phase"] for r in rows] == ["PHASE_A", "PHASE_B"]
    phase_a, phase_b = rows
    assert phase_a["canary_job_key"] is not None, "Phase A keeps its canary"
    assert phase_b["canary_job_key"] is None
    assert phase_b["plan_hash"] == authority.plan_hash
    assert phase_b["candidate_set_hash"] == authority.phase_b_candidate_set_hash
    assert phase_b["baseline_protocol_hash"] == phase_a["baseline_protocol_hash"]
    assert (phase_b["member_count"], phase_b["candidate_count"]) == (10, 48)


def test_preparing_the_authority_again_creates_nothing(authorized: Any) -> None:
    from minos_engine.storage.l2f2_phase_b_prepare import (
        prepare_l2f2_phase_b_execution_authority,
    )

    env, first = authorized
    replay = prepare_l2f2_phase_b_execution_authority(env.engine)

    assert replay.created is False
    assert replay.authority_id == first.authority_id
    assert env.count("SELECT count(*) FROM experiments.l2f2_execution_authorities") == 2


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("baseline_protocol_hash", "f" * 64),
        ("train_schedule_sha256", "e" * 64),
        ("candidate_set_hash", "d" * 64),
        ("parameter_space_hash", "c" * 64),
        ("member_count", 9),
        ("candidate_count", 47),
        ("logical_job_count", 479),
        ("canary_job_key", "a" * 64),
    ],
)
def test_a_disagreeing_authority_is_a_conflict_never_a_repair(
    authorized: Any, authority: Any, field: str, value: Any
) -> None:
    """Append-only lineage: a row that disagrees is raised on, never updated into agreement."""
    from minos_engine.storage.l2f2_phase_b_prepare import (
        PhaseBAuthorityConflictError,
        _verify_existing,
    )

    env, _result = authorized
    with env.engine.connect() as conn:
        conn.execute(text("SET TRANSACTION READ ONLY"))
        row = dict(
            conn.execute(
                text("SELECT * FROM experiments.l2f2_execution_authorities WHERE phase = 'PHASE_B'")
            )
            .mappings()
            .one()
        )
    _verify_existing(row, authority)  # the real row agrees

    row[field] = value
    with pytest.raises(PhaseBAuthorityConflictError, match=field):
        _verify_existing(row, authority)


# --------------------------------------------------------------------------- #
# one resolver per phase, each fixed to its own
# --------------------------------------------------------------------------- #
def _claim(env: Any, plan_hash: str, worker: str) -> Any:
    """Claim through the EXISTING plan-scoped 0007 function — no new claim path exists."""
    with env.service.connect() as conn, conn.begin():
        return (
            conn.execute(
                text("SELECT job_id, job_key FROM experiments.minos_l2f_claim_next_job(:h, :w)"),
                {"h": plan_hash, "w": worker},
            )
            .mappings()
            .one_or_none()
        )


def _resolve(env: Any, function: str, *, plan_hash: str, job_id: str, worker: str) -> Any:
    with env.service.connect() as conn:
        return (
            conn.execute(
                text(f"SELECT job_key FROM experiments.{function}(:h, :j, :w)"),
                {"h": plan_hash, "j": job_id, "w": worker},
            )
            .mappings()
            .one_or_none()
        )


@pytest.fixture(scope="module")
def claimed_phase_b(authorized: Any, authority: Any) -> Any:
    """One Phase-B job, claimed by a known worker through the shared 0007 claim function."""
    env, _result = authorized
    claimed = _claim(env, authority.plan_hash, "ci-phase-b-resolver")
    assert claimed is not None
    return env, str(claimed["job_id"]), str(claimed["job_key"])


def test_the_phase_b_resolver_resolves_its_own_owned_train_job(
    claimed_phase_b: Any, authority: Any
) -> None:
    env, job_id, job_key = claimed_phase_b
    row = _resolve(
        env,
        "l2f2_resolve_claimed_phase_b_execution",
        plan_hash=authority.plan_hash,
        job_id=job_id,
        worker="ci-phase-b-resolver",
    )
    assert row is not None and str(row["job_key"]) == job_key


def test_neither_resolver_answers_for_the_other_phases_plan(
    claimed_phase_b: Any, authority: Any
) -> None:
    """The privileged Phase-A interface stays Phase-A-only, and Phase B's stays Phase-B-only."""
    env, job_id, _job_key = claimed_phase_b

    with pytest.raises(DatabaseError, match="has no PHASE_A L2-F2 execution authority"):
        _resolve(
            env,
            "l2f2_resolve_claimed_execution",
            plan_hash=authority.plan_hash,
            job_id=job_id,
            worker="ci-phase-b-resolver",
        )
    with pytest.raises(DatabaseError, match="has no PHASE_B L2-F2 execution authority"):
        _resolve(
            env,
            "l2f2_resolve_claimed_phase_b_execution",
            plan_hash=env.plan.plan_hash,
            job_id=job_id,
            worker="ci-phase-b-resolver",
        )


def test_the_phase_b_resolver_refuses_a_job_that_is_not_this_workers_own(
    claimed_phase_b: Any, authority: Any
) -> None:
    env, job_id, _job_key = claimed_phase_b

    # someone else's claim
    with pytest.raises(DatabaseError, match="is not an owned TRAIN job"):
        _resolve(
            env,
            "l2f2_resolve_claimed_phase_b_execution",
            plan_hash=authority.plan_hash,
            job_id=job_id,
            worker="ci-somebody-else",
        )

    # a PENDING job nobody has claimed
    with env.engine.connect() as conn:
        pending = str(
            conn.execute(
                text(
                    "SELECT j.id FROM experiments.l2f_experiment_jobs j "
                    "  JOIN experiments.l2f_experiment_plans p ON p.id = j.plan_id "
                    " WHERE p.plan_hash = :h AND j.status = 'PENDING' LIMIT 1"
                ),
                {"h": authority.plan_hash},
            ).scalar_one()
        )
    with pytest.raises(DatabaseError, match="is not an owned TRAIN job"):
        _resolve(
            env,
            "l2f2_resolve_claimed_phase_b_execution",
            plan_hash=authority.plan_hash,
            job_id=pending,
            worker="ci-phase-b-resolver",
        )

    # a job of the Phase-A plan, offered under the Phase-B plan's authority
    with env.engine.connect() as conn:
        phase_a_job = str(
            conn.execute(
                text(
                    "SELECT j.id FROM experiments.l2f_experiment_jobs j "
                    "  JOIN experiments.l2f_experiment_plans p ON p.id = j.plan_id "
                    " WHERE p.plan_hash = :h LIMIT 1"
                ),
                {"h": env.plan.plan_hash},
            ).scalar_one()
        )
    with pytest.raises(DatabaseError, match="is not an owned TRAIN job"):
        _resolve(
            env,
            "l2f2_resolve_claimed_phase_b_execution",
            plan_hash=authority.plan_hash,
            job_id=phase_a_job,
            worker="ci-phase-b-resolver",
        )


def test_claiming_is_plan_scoped_with_three_plans_holding_pending_jobs(
    claimed_phase_b: Any, authority: Any
) -> None:
    """Nothing about claiming changed for Phase B: 0007's function is already plan-scoped.

    All three plans in this store hold PENDING jobs at once — Phase B's materialized batch and the
    foreign plan's two — except Phase A, which is complete and therefore has none to give.
    """
    env, _job_id, _job_key = claimed_phase_b
    with env.engine.connect() as conn:
        foreign_plan = str(
            conn.execute(
                text(
                    "SELECT p.plan_hash FROM experiments.l2f_experiment_plans p "
                    " WHERE p.plan_hash NOT IN (:a, :b) LIMIT 1"
                ),
                {"a": env.plan.plan_hash, "b": authority.plan_hash},
            ).scalar_one()
        )

    by_plan = {
        plan_hash: _claim(env, plan_hash, f"ci-scope-{index}")
        for index, plan_hash in enumerate((env.plan.plan_hash, authority.plan_hash, foreign_plan))
    }
    assert by_plan[env.plan.plan_hash] is None, "a complete Phase A has nothing to claim"
    assert by_plan[authority.plan_hash] is not None
    assert by_plan[foreign_plan] is not None

    with env.engine.connect() as conn:
        for plan_hash, claimed in by_plan.items():
            if claimed is None:
                continue
            owner = str(
                conn.execute(
                    text(
                        "SELECT p.plan_hash FROM experiments.l2f_experiment_jobs j "
                        "  JOIN experiments.l2f_experiment_plans p ON p.id = j.plan_id "
                        " WHERE j.id = :i"
                    ),
                    {"i": claimed["job_id"]},
                ).scalar_one()
            )
            assert owner == plan_hash, "a claim crossed a plan boundary"


# --------------------------------------------------------------------------- #
# the whole least-privilege path, end to end
# --------------------------------------------------------------------------- #
def test_a_phase_b_job_executes_through_the_least_privilege_runner(
    authorized: Any, authority: Any
) -> None:
    """Claim, PHASE-B resolve, CLAIMED -> RUNNING, durable outcome — under minos_runner only.

    The principal here holds ``minos_runner`` and nothing else, so every step goes through the
    narrow SECURITY DEFINER interfaces. Only the resolver differs from Phase A; the execution
    core, the byte verification, the artifact registration and the outcome writers are shared.
    """
    env, _result = authorized
    before_phase_a = read_l2f2_phase_a_progress(env.engine)

    dispatched = _run(env, authority, worker_id="ci-phase-b-exec")
    assert dispatched is not None
    assert dispatched.status == "SUCCEEDED"

    index_map = _index_map(authority.plan)
    member_index, config_index = index_map[dispatched.job_key]
    assert member_index in (0, 1, 2, 3, 4), "batch 0 only"
    env.evaluate(dispatched, minos_score=_score_for(config_index, member_index))

    snapshot = load_phase_b_observations(env.engine, authority=authority)
    assert len(snapshot.observations) == 1
    observation = snapshot.observations[0]
    assert observation.config_hash in authority.design.ordered_config_hashes
    assert observation.admitted is True
    assert snapshot.execution_result_count == 1
    assert snapshot.infrastructure_incident_count == 0

    progress = read_l2f2_phase_b_progress(env.engine)
    assert progress.succeeded_count == 1
    assert progress.decided_observation_count == 1
    assert progress.batch0_complete is False, "one observation is not a balanced batch"

    # Phase A did not move, and racing still refuses on a partial batch.
    assert read_l2f2_phase_a_progress(env.engine) == before_phase_a
    with pytest.raises(PhaseBExpansionError, match="batch 0 is not complete"):
        race_l2f2_phase_b_batch0(env.engine)
