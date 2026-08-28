"""Phase C against REAL PostgreSQL, as the THIRD plan in one store.

This is the whole TRAIN chain end to end on the production boundaries: a complete Phase-A screen,
a complete 480-pair Phase-B screen derived from it, ten candidates promoted from that ledger, a
Phase-C plan persisted beside both, a ``PHASE_C`` authority prepared, ten balanced batches raced to
a TRAIN-complete confirmation, and four finalists frozen. No plan, job, execution or evaluation
row is ever inserted by hand.

Three plans share this database, which is the state Phase B first made real and Phase C now has to
survive: a reader that counted rows globally would absorb two foreign screens instead of one. The
controls below pin the scoping in all three directions.

The load-bearing rule proved here is the tie-break index. Phase C carries TWO orderings — the
promotion order 0..9 and the INHERITED Phase-B design position 0..47 — and only the second is
scientific. :mod:`tests.unit.baseline.test_phase_c_candidate_index` proves the two disagree; this
module proves the authority, the ledger and the finalists all carry the inherited one.

No GATK and no MINOS_SUBNET: executions use ``FakeGatkRunner`` through the private test seam and
scores are recorded upstream results, exactly as the Phase-A and Phase-B suites do.

The module is stateful by design, so the fixtures — never the test order — own every transition.
It is also the most expensive suite in the tree, because what it proves is that the chain holds
when it is COMPLETE, and a nearly-complete chain proves nothing.
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import create_engine, text

from minos_engine.baseline.phase_b import build_l2f2_phase_b_authority
from minos_engine.baseline.phase_b_completion import derive_completed_phase_b_result
from minos_engine.baseline.phase_b_observations import load_phase_b_observations
from minos_engine.baseline.phase_c import (
    PHASE_C_BATCH_COUNT,
    PHASE_C_BATCH_SIZE,
    PHASE_C_CANDIDATE_COUNT,
    PHASE_C_LOGICAL_JOB_BUDGET,
    PHASE_C_MEMBER_COUNT,
    PhaseCError,
    build_l2f2_phase_c_authority,
)
from minos_engine.baseline.phase_c_observations import load_phase_c_observations
from minos_engine.baseline.racing import (
    PHASE_B_SURVIVOR_COUNT,
    VALIDATION_FINALIST_COUNT,
)
from minos_engine.storage.l2f2_phase_a_control import read_l2f2_phase_a_progress
from minos_engine.storage.l2f2_phase_b_control import (
    expand_l2f2_phase_b_batch,
    read_l2f2_phase_b_progress,
    select_l2f2_phase_c_candidates,
)
from minos_engine.storage.l2f2_phase_c_control import (
    PhaseCExpansionError,
    eligible_phase_c_batch_jobs,
    expand_l2f2_phase_c_batch,
    race_l2f2_phase_c_batch,
    read_l2f2_phase_c_progress,
    select_l2f2_validation_finalists,
)
from tests.integration.layer2_db.l2f2_phase_a_env import phase_a_store
from tests.integration.layer2_db.test_l2f2_phase_b_control import (
    _complete_phase_a,
    _drain,
    _index_map,
    _run,
    _score_for,
)

_MAX_SLICE = 64


def _job_identity(plan: Any) -> dict[str, tuple[int, str]]:
    """``job_key -> (member_index, config_hash)`` straight off the frozen plan.

    Deliberately NOT ``job_key -> config_index``: the plan's config index is its own plan-local
    namespace, and resolving a candidate by position in some other tuple is exactly the confusion
    this whole stage exists to keep out of the tie-break.
    """
    from minos_engine.experiments.plan import iter_logical_jobs

    return {j.job_key: (j.member_index, j.config_hash) for j in iter_logical_jobs(plan)}


def _slices(total: int) -> tuple[tuple[int, int], ...]:
    """``(start, count)`` pairs covering ``total`` eligible jobs under the bounded-slice cap."""
    return tuple((start, min(_MAX_SLICE, total - start)) for start in range(0, total, _MAX_SLICE))


def _complete_phase_b(env: Any, authority: Any) -> None:
    """Drive the frozen 480-pair Phase-B screen to completion through the production seams."""
    for batch_index in (0, 1):
        for start, count in _slices(240):
            expand_l2f2_phase_b_batch(env.engine, batch_index=batch_index, start=start, count=count)
        _drain(env, authority, prefix=f"ci-phase-b{batch_index}")
    progress = read_l2f2_phase_b_progress(env.engine)
    assert progress.decided_observation_count == 480, f"Phase B did not complete: {progress}"
    assert progress.infrastructure_incident_count == 0


def _survivors_for(env: Any, authority: Any, batch_index: int) -> tuple[str, ...] | None:
    """Whom the frozen rule still permits into ``batch_index``. ``None`` means 'everyone'."""
    if batch_index == 0:
        return None
    decision = race_l2f2_phase_c_batch(env.engine, batch_index=batch_index - 1, authority=authority)
    return decision.surviving_config_hashes


def _drain_phase_c_batch(env: Any, authority: Any, batch_index: int) -> int:
    """Materialize and decide ONE complete balanced Phase-C batch. Returns the pairs decided."""
    eligible = eligible_phase_c_batch_jobs(
        authority,
        batch_index=batch_index,
        survivors=_survivors_for(env, authority, batch_index),
    )
    for start, count in _slices(len(eligible)):
        expand_l2f2_phase_c_batch(env.engine, batch_index=batch_index, start=start, count=count)
    executed, _keys = _drain(env, authority, prefix=f"ci-phase-c{batch_index}")
    return executed


# --------------------------------------------------------------------------- #
# fixtures — one store, three plans, the whole TRAIN chain
# --------------------------------------------------------------------------- #
@pytest.fixture(scope="module")
def campaign(isolated_pg_base_url: str, tmp_path_factory: Any) -> Any:
    """ONE store holding a COMPLETE Phase A and a COMPLETE Phase B. Built once."""
    from minos_engine.storage.l2f2_phase_b_prepare import (
        prepare_l2f2_phase_b_execution_authority,
    )
    from minos_engine.storage.l2f_plan_store import _persist_l2f2_phase_b_plan_with_trust
    from tests.integration.layer2_db.test_l2f_plan_store import _publisher

    tmp_path = tmp_path_factory.mktemp("phase_c_campaign")
    with phase_a_store(isolated_pg_base_url, tmp_path) as env:
        _complete_phase_a(env)
        _persist_l2f2_phase_b_plan_with_trust(env.engine, publisher=_publisher(env.config_root))
        prepare_l2f2_phase_b_execution_authority(env.engine)
        # Phase B brings five members Phase A never referenced, so they are TRAIN-allocated and
        # given truth only now — the projection reads members, and they did not exist before.
        env.allocate_train_members()
        env.register_truth()
        phase_b = build_l2f2_phase_b_authority(env.engine)
        _complete_phase_b(env, phase_b)
        env.phase_b_authority = phase_b
        yield env


@pytest.fixture(scope="module")
def persisted(campaign: Any) -> Any:
    """Phase C's plan persisted beside the two completed screens."""
    from minos_engine.storage.l2f_plan_store import _persist_l2f2_phase_c_plan_with_trust
    from tests.integration.layer2_db.test_l2f_plan_store import _publisher

    result = _persist_l2f2_phase_c_plan_with_trust(
        campaign.engine, publisher=_publisher(campaign.config_root)
    )
    campaign.allocate_train_members()  # forty more members, none of them allocated before
    campaign.register_truth()
    return campaign, result


@pytest.fixture(scope="module")
def authority(persisted: Any) -> Any:
    env, _result = persisted
    return build_l2f2_phase_c_authority(env.engine)


@pytest.fixture(scope="module")
def authorized(persisted: Any, authority: Any) -> Any:
    from minos_engine.storage.l2f2_phase_c_prepare import (
        prepare_l2f2_phase_c_execution_authority,
    )

    env, _result = persisted
    return env, prepare_l2f2_phase_c_execution_authority(env.engine)


@pytest.fixture(scope="module")
def confirmed(authorized: Any, authority: Any) -> Any:
    """A TRAIN-COMPLETE Phase C: every batch materialized, raced and decided."""
    env, _prepared = authorized
    for batch_index in range(PHASE_C_BATCH_COUNT):
        _drain_phase_c_batch(env, authority, batch_index)
    return env


# --------------------------------------------------------------------------- #
# the Phase-B result boundary Phase C is derived from
# --------------------------------------------------------------------------- #
def test_the_phase_b_result_is_derived_from_the_ledger_and_is_deterministic(
    campaign: Any,
) -> None:
    first = derive_completed_phase_b_result(campaign.engine)
    second = derive_completed_phase_b_result(campaign.engine)

    assert first.completion_hash == second.completion_hash
    assert first.selected_config_hashes == second.selected_config_hashes
    assert len(first.selected_config_hashes) == PHASE_B_SURVIVOR_COUNT == 10
    assert len(set(first.selected_config_hashes)) == 10
    assert campaign.phase_b_authority.seed_config_hash in first.selected_config_hashes


def test_the_promoted_ten_are_exactly_what_the_frozen_selection_returns(campaign: Any) -> None:
    """Promotion has ONE implementation; the completion result may not re-decide it."""
    assert derive_completed_phase_b_result(
        campaign.engine
    ).selected_config_hashes == select_l2f2_phase_c_candidates(campaign.engine)


def test_the_completion_identity_binds_runtime_as_well_as_score(campaign: Any) -> None:
    """Promotion ties break on mean GATK runtime, so runtime can move the ten and must be bound."""
    from minos_engine.baseline.phase_b_completion import compute_phase_b_completion_hash

    observations = load_phase_b_observations(campaign.engine).observations
    kwargs = {
        "protocol_hash": "a" * 64,
        "plan_hash": "b" * 64,
        "candidate_set_hash": "c" * 64,
        "parameter_space_hash": "d" * 64,
        "execution_environment_hash": "e" * 64,
    }
    base = compute_phase_b_completion_hash(observations, **kwargs)

    moved = observations[0].model_copy(
        update={"gatk_runtime_ms": observations[0].gatk_runtime_ms + 1}
    )
    assert compute_phase_b_completion_hash((moved, *observations[1:]), **kwargs) != base


def test_an_authority_over_an_unpersisted_plan_authorizes_nothing(campaign: Any) -> None:
    """Preparation never persists a plan as a side effect of authorizing one."""
    from minos_engine.storage.l2f2_phase_c_prepare import (
        PhaseCAuthorityPreparationError,
        prepare_l2f2_phase_c_execution_authority,
    )

    with pytest.raises(PhaseCAuthorityPreparationError, match="not persisted"):
        prepare_l2f2_phase_c_execution_authority(campaign.engine)


# --------------------------------------------------------------------------- #
# THE tie-break index, at the database boundary
# --------------------------------------------------------------------------- #
def test_the_authority_carries_the_inherited_phase_b_index_not_the_promotion_position(
    campaign: Any, authority: Any
) -> None:
    """The clarified rule: a candidate's tie-break number is its ORIGINAL Phase-B position."""
    design = build_l2f2_phase_b_authority(campaign.engine).design
    inherited = authority.inherited_candidate_index

    assert set(inherited) == set(authority.ordered_config_hashes)
    assert all(inherited[h] == design.candidate_index[h] for h in inherited)
    assert all(0 <= v < 48 for v in inherited.values())
    assert len(set(inherited.values())) == PHASE_C_CANDIDATE_COUNT

    promotion = {h: i for i, h in enumerate(authority.ordered_config_hashes)}
    assert inherited != promotion, (
        "the inherited index coincided with the promotion position, so this campaign cannot "
        "distinguish the two readings — the unit suite pins the rule that separates them"
    )


def test_the_candidate_set_identity_binds_both_orderings(authority: Any) -> None:
    """Bookkeeping order and tie-break order are different facts; both are in the identity."""
    from minos_engine.baseline.phase_c import compute_phase_c_candidate_set_hash

    kwargs = {
        "protocol_hash": "a" * 64,
        "source_phase_b_plan_hash": "b" * 64,
        "phase_b_completion_hash": "c" * 64,
        "parameter_space_hash": "d" * 64,
        "experiment_parameter_policy_hash": "e" * 64,
        "seed_config_hash": authority.seed_config_hash,
    }
    base = compute_phase_c_candidate_set_hash(
        ordered_config_hashes=authority.ordered_config_hashes,
        inherited_candidate_index=authority.inherited_candidate_index,
        **kwargs,
    )
    swapped = (
        authority.ordered_config_hashes[1],
        authority.ordered_config_hashes[0],
        *authority.ordered_config_hashes[2:],
    )
    assert (
        compute_phase_c_candidate_set_hash(
            ordered_config_hashes=swapped,
            inherited_candidate_index=authority.inherited_candidate_index,
            **kwargs,
        )
        != base
    )
    moved = dict(authority.inherited_candidate_index)
    moved[authority.ordered_config_hashes[0]] += 100
    assert (
        compute_phase_c_candidate_set_hash(
            ordered_config_hashes=authority.ordered_config_hashes,
            inherited_candidate_index=moved,
            **kwargs,
        )
        != base
    )


# --------------------------------------------------------------------------- #
# persistence — three plans in one store
# --------------------------------------------------------------------------- #
def test_phase_c_persists_beside_both_screens_without_a_new_plan_schema(
    persisted: Any, authority: Any
) -> None:
    env, result = persisted

    with env.engine.connect() as conn:
        conn.execute(text("SET TRANSACTION READ ONLY"))
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
    assert len(plans) == 3, "Phase A, Phase B and Phase C now coexist"
    row = plans[authority.plan_hash]
    assert int(row["train_member_count"]) == PHASE_C_MEMBER_COUNT == 50
    assert int(row["candidate_count"]) == PHASE_C_CANDIDATE_COUNT == 10
    assert int(row["logical_job_count"]) == PHASE_C_LOGICAL_JOB_BUDGET == 500
    assert row["candidate_set_hash"] == authority.phase_c_candidate_set_hash
    assert (result.plan_created, result.replay) == (True, False)
    assert (result.member_count, result.config_count) == (50, 10)
    assert result.jobs_count == 0, "persisting a plan never creates jobs"


def test_every_phase_c_config_payload_is_reused_never_re_minted(
    persisted: Any, authority: Any
) -> None:
    """All ten are Phase-B configurations already in this store; Phase C mints no new payload."""
    env, _result = persisted
    with env.engine.connect() as conn:
        conn.execute(text("SET TRANSACTION READ ONLY"))
        payloads = {
            str(r["config_hash"]): int(r["n"])
            for r in conn.execute(
                text(
                    "SELECT config_hash, count(*) AS n FROM experiments.l2f_config_payloads "
                    " GROUP BY config_hash"
                )
            ).mappings()
        }
    assert len(payloads) == 39 + 41, "Phase C introduced a config payload of its own"
    for config_hash in authority.ordered_config_hashes:
        assert payloads[config_hash] == 1


def test_persistence_replay_inserts_nothing(persisted: Any, authority: Any) -> None:
    from minos_engine.storage.l2f_plan_store import _persist_l2f2_phase_c_plan_with_trust
    from tests.integration.layer2_db.test_l2f_plan_store import _publisher

    env, _result = persisted
    replay = _persist_l2f2_phase_c_plan_with_trust(
        env.engine, publisher=_publisher(env.config_root)
    )
    assert (replay.plan_created, replay.replay, replay.artifacts_created) == (False, True, 0)
    assert replay.plan_hash == authority.plan_hash


# --------------------------------------------------------------------------- #
# plan scoping, in all three directions
# --------------------------------------------------------------------------- #
def test_each_screens_readers_ignore_the_other_two_entirely(persisted: Any) -> None:
    env, _result = persisted
    phase_a = read_l2f2_phase_a_progress(env.engine)
    phase_b = load_phase_b_observations(env.engine)
    phase_c = load_phase_c_observations(env.engine)

    assert (phase_a.enqueued_count, phase_a.decided_observation_count) == (195, 195)
    assert phase_a.complete is True
    assert len(phase_b.observations) == 480
    assert phase_b.execution_result_count == 480
    assert phase_c.observations == ()
    assert phase_c.execution_result_count == 0
    assert phase_c.evaluation_result_count == 0


def test_the_shared_configs_and_members_do_not_cross_contaminate(
    persisted: Any, authority: Any
) -> None:
    """All ten Phase-C candidates and its first members are in Phase B by construction."""
    env, _result = persisted
    phase_b = load_phase_b_observations(env.engine)

    assert set(authority.ordered_config_hashes) <= {o.config_hash for o in phase_b.observations}
    shared_datasets = {o.dataset_id for o in phase_b.observations} & {
        m.dataset_id for m in authority.plan.members
    }
    assert len(shared_datasets) == 10, "Phase B's ten members really are Phase C's first ten"
    assert load_phase_c_observations(env.engine).observations == ()


# --------------------------------------------------------------------------- #
# the execution authority
# --------------------------------------------------------------------------- #
def test_the_authority_is_derived_and_carries_no_canary(authorized: Any, authority: Any) -> None:
    env, prepared = authorized
    with env.engine.connect() as conn:
        conn.execute(text("SET TRANSACTION READ ONLY"))
        rows = {
            str(r["phase"]): dict(r)
            for r in conn.execute(
                text(
                    "SELECT phase, plan_hash, candidate_set_hash, member_count, candidate_count, "
                    "       logical_job_count, canary_job_key "
                    "  FROM experiments.l2f2_execution_authorities"
                )
            ).mappings()
        }
    assert sorted(rows) == ["PHASE_A", "PHASE_B", "PHASE_C"]
    row = rows["PHASE_C"]
    assert row["plan_hash"] == authority.plan_hash
    assert row["candidate_set_hash"] == authority.phase_c_candidate_set_hash
    assert (int(row["member_count"]), int(row["candidate_count"])) == (50, 10)
    assert int(row["logical_job_count"]) == 500
    assert row["canary_job_key"] is None, "Phase C inherits a proven chain; it re-proves nothing"
    assert prepared.created is True


def test_preparing_the_authority_again_creates_nothing(authorized: Any) -> None:
    from minos_engine.storage.l2f2_phase_c_prepare import (
        prepare_l2f2_phase_c_execution_authority,
    )

    env, _prepared = authorized
    again = prepare_l2f2_phase_c_execution_authority(env.engine)
    assert again.created is False
    with env.engine.connect() as conn:
        conn.execute(text("SET TRANSACTION READ ONLY"))
        count = conn.execute(
            text(
                "SELECT count(*) FROM experiments.l2f2_execution_authorities "
                " WHERE phase = 'PHASE_C'"
            )
        ).scalar_one()
    assert int(count) == 1


# --------------------------------------------------------------------------- #
# bounded materialization
# --------------------------------------------------------------------------- #
def test_batch_0_is_every_promoted_candidate_times_its_five_members(authority: Any) -> None:
    keys = eligible_phase_c_batch_jobs(authority, batch_index=0)
    index_map = _index_map(authority.plan)

    assert len(keys) == PHASE_C_CANDIDATE_COUNT * PHASE_C_BATCH_SIZE == 50
    assert len(set(keys)) == 50
    assert {index_map[key][0] for key in keys} == {0, 1, 2, 3, 4}
    assert len({index_map[key][1] for key in keys}) == 10


@pytest.mark.parametrize("batch_index", [-1, PHASE_C_BATCH_COUNT, 99])
def test_a_batch_outside_the_frozen_ten_is_refused(authority: Any, batch_index: int) -> None:
    with pytest.raises((PhaseCExpansionError, PhaseCError), match="batch"):
        eligible_phase_c_batch_jobs(authority, batch_index=batch_index)


@pytest.mark.parametrize(
    ("start", "count"), [(-1, 10), (0, 0), (0, 65), (0, 51), (45, 10), (50, 1)]
)
def test_an_out_of_contract_slice_is_refused(authorized: Any, start: int, count: int) -> None:
    env, _prepared = authorized
    with pytest.raises(PhaseCExpansionError):
        expand_l2f2_phase_c_batch(env.engine, batch_index=0, start=start, count=count)


def test_nothing_downstream_of_an_incomplete_batch_may_happen(authorized: Any) -> None:
    """Racing, later batches and promotion all refuse while batch 0 is not fully decided."""
    env, _prepared = authorized
    with pytest.raises(PhaseCExpansionError, match="complete"):
        race_l2f2_phase_c_batch(env.engine, batch_index=0)
    with pytest.raises(PhaseCExpansionError, match="complete"):
        expand_l2f2_phase_c_batch(env.engine, batch_index=1, start=0, count=10)
    with pytest.raises(Exception, match="complete"):
        select_l2f2_validation_finalists(env.engine)


def test_materialization_is_idempotent_and_plan_scoped(authorized: Any) -> None:
    env, _prepared = authorized
    before = read_l2f2_phase_b_progress(env.engine)

    first = expand_l2f2_phase_c_batch(env.engine, batch_index=0, start=0, count=50)
    second = expand_l2f2_phase_c_batch(env.engine, batch_index=0, start=0, count=50)

    assert (first.created, first.existing) == (50, 0)
    assert (second.created, second.existing) == (0, 50)
    assert first.jobs_total_after == second.jobs_total_after == 50
    assert first.surviving_candidate_count == 10
    after = read_l2f2_phase_b_progress(env.engine)
    assert after.enqueued_count == before.enqueued_count, "Phase C enqueued into Phase B"
    assert after.decided_observation_count == before.decided_observation_count


def test_the_materialized_batch_stays_pending_until_a_runner_claims_it(authorized: Any) -> None:
    env, _prepared = authorized
    expand_l2f2_phase_c_batch(env.engine, batch_index=0, start=0, count=50)
    progress = read_l2f2_phase_c_progress(env.engine)

    assert progress.logical_job_budget == 500
    assert progress.enqueued_count == 50
    assert progress.pending_count == 50
    assert progress.decided_observation_count == 0
    assert progress.alive_candidate_count == 10
    assert progress.eliminated_candidate_count == 0
    assert progress.complete is False


# --------------------------------------------------------------------------- #
# execution — end to end through the least-privilege boundary
# --------------------------------------------------------------------------- #
def test_a_phase_c_job_executes_and_is_scored_through_the_production_seams(
    authorized: Any, authority: Any
) -> None:
    """§32 end to end: materialized → claimed → executed → evaluated → decided observation."""
    env, _prepared = authorized
    expand_l2f2_phase_c_batch(env.engine, batch_index=0, start=0, count=50)
    index_map = _index_map(authority.plan)

    dispatched = _run(env, authority, worker_id="ci-phase-c-e2e")
    assert dispatched is not None
    assert dispatched.status == "SUCCEEDED"
    member_index, config_index = index_map[dispatched.job_key]
    env.evaluate(dispatched, minos_score=_score_for(config_index, member_index))

    snapshot = load_phase_c_observations(env.engine, authority=authority)
    assert len(snapshot.observations) == 1
    observation = snapshot.observations[0]
    assert observation.config_hash in authority.ordered_config_hashes
    assert observation.minos_score is not None
    assert snapshot.infrastructure_incident_count == 0
    assert read_l2f2_phase_c_progress(env.engine).decided_observation_count == 1


def test_the_runner_bootstrap_tells_a_truth_free_worker_two_strings_and_nothing_more(
    authorized: Any, authority: Any
) -> None:
    """What a Phase-C worker is allowed to know: its plan, and the runtime it must match."""
    from minos_engine.storage.l2f2_runner import _resolve_phase_c_runner_bootstrap

    env, _prepared = authorized
    with env.service.connect() as conn:
        ticket = _resolve_phase_c_runner_bootstrap(conn)

    assert ticket.authority.plan_hash == authority.plan_hash
    assert ticket.authority.phase == "PHASE_C"
    assert ticket.execution_environment_hash == authority.execution_environment_hash


def test_the_public_phase_c_entry_refuses_a_worker_on_another_runtime(
    authorized: Any, monkeypatch: Any
) -> None:
    """The preclaim runtime gate, against Phase C's own ticket.

    A worker whose JVM or interpreter differs from the completed Phase-B screen's must refuse
    BEFORE it claims anything: the ten configurations it would be confirming were promoted from
    that runtime's numbers, and a confirmation measured on a different one confirms nothing.
    """
    from minos_engine.experiments.execution_contract import GatkRuntimeIdentityError
    from minos_engine.storage import l2f2_runner

    env, _prepared = authorized
    before = read_l2f2_phase_c_progress(env.engine)

    class _OtherRuntime:
        def environment_hash(self) -> str:
            return "9" * 64

    class _Runner:
        @staticmethod
        def preflight() -> Any:
            return _OtherRuntime()

    # both are imported inside the entry, so they are patched at their source modules; the engine
    # is a FRESH one to the same runner-only principal, because the entry disposes what it opens.
    from minos_engine.storage import database as _database
    from minos_engine.storage import l2f_gatk_runner as _gatk

    monkeypatch.setattr(_gatk.SubprocessGatkRunner, "from_env", staticmethod(lambda: _Runner()))
    monkeypatch.setattr(
        _database, "create_db_engine", lambda: create_engine(env.service.url), raising=True
    )

    with pytest.raises(GatkRuntimeIdentityError, match="must not mix runtimes"):
        l2f2_runner.execute_next_l2f2_phase_c_job(worker_id="ci-wrong-runtime")

    assert read_l2f2_phase_c_progress(env.engine) == before, "a refused worker mutated the queue"


def test_the_phase_b_runner_will_not_reach_into_phase_c(authorized: Any, campaign: Any) -> None:
    """Phase B is finished; its authority must not pick up a pending Phase-C job."""
    env, _prepared = authorized
    expand_l2f2_phase_c_batch(env.engine, batch_index=0, start=0, count=50)
    assert _run(env, campaign.phase_b_authority, worker_id="ci-b-into-c") is None


# --------------------------------------------------------------------------- #
# racing, over all ten batches
# --------------------------------------------------------------------------- #
def test_a_complete_balanced_batch_permits_a_decision_and_the_seed_always_survives(
    confirmed: Any, authority: Any
) -> None:
    env = confirmed
    for batch_index in range(PHASE_C_BATCH_COUNT):
        decision = race_l2f2_phase_c_batch(env.engine, batch_index=batch_index)
        assert decision.batch_index == batch_index
        assert decision.seed_config_hash == authority.seed_config_hash
        assert authority.seed_config_hash in decision.surviving_config_hashes, (
            f"the seed was eliminated at batch {batch_index}"
        )
        assert authority.seed_config_hash not in decision.eliminated_config_hashes
        assert set(decision.surviving_config_hashes) <= set(authority.ordered_config_hashes)
        assert not set(decision.surviving_config_hashes) & set(decision.eliminated_config_hashes)
        assert decision.survivor_count >= VALIDATION_FINALIST_COUNT


def test_survivors_shrink_monotonically_and_are_recomputed_from_the_ledger(
    confirmed: Any,
) -> None:
    """Racing holds no state: two reads of the same immutable ledger must agree exactly."""
    env = confirmed
    survivors = [
        race_l2f2_phase_c_batch(env.engine, batch_index=i).surviving_config_hashes
        for i in range(PHASE_C_BATCH_COUNT)
    ]
    again = [
        race_l2f2_phase_c_batch(env.engine, batch_index=i).surviving_config_hashes
        for i in range(PHASE_C_BATCH_COUNT)
    ]
    assert survivors == again
    for earlier, later in zip(survivors[:-1], survivors[1:], strict=True):
        assert set(later) <= set(earlier), "an eliminated candidate came back"


def test_an_eliminated_candidate_stops_dead_and_is_never_resumed(
    confirmed: Any, authority: Any
) -> None:
    """The elimination has to reach the QUEUE, not merely the report.

    The invariant is about the SHAPE of what was spent, because that is what elimination is for.
    Every candidate holds a whole number of leading batches and nothing after them: a candidate
    the rule stopped at batch *k* has exactly ``5·(k+1)`` jobs, all of them in batches it was
    still alive for, and no later job appears for it — not one, and not a gap it might resume
    through. Nothing else can produce that shape by accident.

    Note what is deliberately NOT asserted. :func:`race_l2f2_phase_c_batch` is evaluated against
    the ledger AS IT STANDS, so replaying "batch 0" after the confirmation finished sees all fifty
    members and eliminates more candidates than the live campaign did at that moment. That is the
    frozen rule behaving correctly — more observation can only narrow the bounds, never widen them
    — but it means a retrospective per-batch elimination list is not a record of what the queue
    knew at the time, and asserting one against the queue would be asserting a time machine.
    """
    env = confirmed
    identity = _job_identity(authority.plan)
    with env.engine.connect() as conn:
        conn.execute(text("SET TRANSACTION READ ONLY"))
        enqueued = {
            str(r["job_key"])
            for r in conn.execute(
                text(
                    "SELECT j.job_key FROM experiments.l2f_experiment_jobs j "
                    "  JOIN experiments.l2f_experiment_plans p ON p.id = j.plan_id "
                    " WHERE p.plan_hash = :h"
                ),
                {"h": authority.plan_hash},
            ).mappings()
        }
    by_config: dict[str, set[int]] = {}
    for key in enqueued:
        member_index, config_hash = identity[key]
        by_config.setdefault(config_hash, set()).add(member_index)

    assert set(by_config) == set(authority.ordered_config_hashes)
    stopped_early = set()
    for config_hash, members in by_config.items():
        batches, remainder = divmod(len(members), PHASE_C_BATCH_SIZE)
        assert remainder == 0, f"{config_hash[:12]} holds a partial batch"
        assert 1 <= batches <= PHASE_C_BATCH_COUNT
        assert members == set(range(batches * PHASE_C_BATCH_SIZE)), (
            f"{config_hash[:12]} holds a gap or a job past where it stopped"
        )
        if batches != PHASE_C_BATCH_COUNT:
            stopped_early.add(config_hash)

    final = set(
        race_l2f2_phase_c_batch(
            env.engine, batch_index=PHASE_C_BATCH_COUNT - 1
        ).surviving_config_hashes
    )
    assert not (stopped_early & final), "a candidate that stopped early came back as a survivor"
    assert authority.seed_config_hash not in stopped_early, "the seed was stopped"
    assert not (stopped_early & set(select_l2f2_validation_finalists(env.engine)))
    assert sum(len(m) for m in by_config.values()) <= PHASE_C_LOGICAL_JOB_BUDGET


def test_a_candidate_failure_is_a_decided_observation_not_a_missing_one(confirmed: Any) -> None:
    """Non-admission is a RESULT. It must count as decided, or a batch never completes."""
    env = confirmed
    progress = read_l2f2_phase_c_progress(env.engine)
    snapshot = load_phase_c_observations(env.engine)

    assert progress.decided_observation_count == len(snapshot.observations)
    assert progress.candidate_failure_count == sum(
        1 for o in snapshot.observations if not o.admitted
    )
    assert progress.infrastructure_incident_count == 0
    assert progress.complete is True


def test_the_confirmation_completes_within_the_five_hundred_job_ceiling(confirmed: Any) -> None:
    """500 is a CEILING, not a quota: elimination legitimately spends fewer."""
    env = confirmed
    progress = read_l2f2_phase_c_progress(env.engine)

    assert progress.enqueued_count <= PHASE_C_LOGICAL_JOB_BUDGET
    assert progress.completed_batch_count == PHASE_C_BATCH_COUNT
    assert progress.alive_candidate_count + progress.eliminated_candidate_count == 10
    assert progress.complete_candidate_count >= VALIDATION_FINALIST_COUNT


# --------------------------------------------------------------------------- #
# the four finalists
# --------------------------------------------------------------------------- #
def test_exactly_four_finalists_are_frozen_and_the_seed_is_one_of_them(
    confirmed: Any, authority: Any
) -> None:
    from minos_engine.baseline.validation_finalists import derive_validation_finalist_set

    env = confirmed
    finalists = select_l2f2_validation_finalists(env.engine)
    assert len(finalists) == VALIDATION_FINALIST_COUNT == 4
    assert len(set(finalists)) == 4
    assert authority.seed_config_hash in finalists
    assert set(finalists) <= set(authority.ordered_config_hashes)

    first = derive_validation_finalist_set(env.engine)
    second = derive_validation_finalist_set(env.engine)
    assert first.finalist_set_hash == second.finalist_set_hash
    assert first.ordered_config_hashes == finalists
    assert first.inherited_candidate_index == {
        h: authority.inherited_candidate_index[h] for h in finalists
    }


def test_a_partially_confirmed_candidate_is_never_ranked(confirmed: Any, authority: Any) -> None:
    """An eliminated candidate stops early; its unseen remainder is never fabricated."""
    env = confirmed
    finalists = select_l2f2_validation_finalists(env.engine)
    snapshot = load_phase_c_observations(env.engine, authority=authority)

    decided: dict[str, int] = {}
    for observation in snapshot.observations:
        decided[observation.config_hash] = decided.get(observation.config_hash, 0) + 1
    for config_hash in finalists:
        assert decided.get(config_hash) == PHASE_C_MEMBER_COUNT, (
            f"{config_hash[:12]} was promoted on {decided.get(config_hash)} of 50 members"
        )
    for config_hash, count in decided.items():
        if count != PHASE_C_MEMBER_COUNT:
            assert config_hash not in finalists


def test_the_finalist_identity_binds_the_inherited_index(confirmed: Any, authority: Any) -> None:
    from minos_engine.baseline.validation_finalists import (
        compute_validation_finalist_set_hash,
        derive_validation_finalist_set,
    )

    env = confirmed
    frozen = derive_validation_finalist_set(env.engine)
    kwargs = {
        "protocol_hash": authority.baseline_protocol_hash,
        "phase_c_plan_hash": frozen.phase_c_plan_hash,
        "phase_c_candidate_set_hash": frozen.phase_c_candidate_set_hash,
        "phase_c_result_hash": frozen.phase_c_result_hash,
        "ordered_config_hashes": frozen.ordered_config_hashes,
        "seed_config_hash": frozen.seed_config_hash,
    }
    assert (
        compute_validation_finalist_set_hash(
            inherited_candidate_index=frozen.inherited_candidate_index, **kwargs
        )
        == frozen.finalist_set_hash
    )
    moved = dict(frozen.inherited_candidate_index)
    moved[frozen.ordered_config_hashes[0]] += 100
    assert (
        compute_validation_finalist_set_hash(inherited_candidate_index=moved, **kwargs)
        != frozen.finalist_set_hash
    )


def test_a_complete_phase_c_leaves_the_two_earlier_screens_exactly_as_they_were(
    confirmed: Any,
) -> None:
    env = confirmed
    phase_a = read_l2f2_phase_a_progress(env.engine)
    phase_b = read_l2f2_phase_b_progress(env.engine)

    assert (phase_a.enqueued_count, phase_a.decided_observation_count) == (195, 195)
    assert phase_a.complete is True
    assert phase_b.decided_observation_count == 480
    assert phase_b.infrastructure_incident_count == 0
    assert derive_completed_phase_b_result(env.engine).selected_config_hashes == tuple(
        build_l2f2_phase_c_authority(env.engine).ordered_config_hashes
    )


def test_a_phase_c_reader_refuses_a_foreign_runtime(confirmed: Any, authority: Any) -> None:
    """Phase C must run on the runtime that produced the screen it inherits, or not at all."""
    from minos_engine.baseline.phase_c import PhaseCAuthority

    foreign = PhaseCAuthority.model_validate(
        {**authority.model_dump(), "execution_environment_hash": "e" * 64}
    )
    with pytest.raises(Exception, match="runtime"):
        load_phase_c_observations(confirmed.engine, authority=foreign)
