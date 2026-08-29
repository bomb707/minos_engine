"""The L2-F2-F validation control plane, exercised without a database and without real truth.

Everything here runs on fixtures. No validation truth is resolved, opened or hashed, no TEST byte
is touched, and no validation database exists — the point of these tests is to prove the source
refuses the things it must refuse BEFORE any of that could happen.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest

from minos_engine.baseline.finalist_freeze import (
    FINALIST_FREEZE_SCHEMA,
    FinalistFreezeError,
    load_finalist_freeze,
    verify_finalist_freeze_document,
)
from minos_engine.baseline.objective import BaselineObservation
from minos_engine.baseline.phase_d import (
    PHASE_D_CANDIDATE_COUNT,
    PHASE_D_LOGICAL_JOB_BUDGET,
    PHASE_D_PHASE,
    PHASE_D_RACING_RULE,
    PhaseDError,
    build_l2f2_phase_d_authority,
)
from minos_engine.baseline.schedule import ScheduleError, build_train_schedule
from minos_engine.baseline.validation_members import (
    VALIDATION_COUNT,
    VALIDATION_PER_CHROMOSOME,
    build_validation_schedule,
)
from minos_engine.storage.l2f2_validation_control import (
    ValidationControlError,
    eligible_l2f2_validation_jobs,
    rank_validation_observations,
)

_SEED = "4251cb85e5cd58b7eabfe530b9df23ea7d1d14fd882114b488d67cbd81b751b8"
_F1 = "157d88d1587c13be395c62d60e27d1becdada78fad45e65d883bc1190e51acea"
_F2 = "0972930f8d8c562be15382203e123b2909094e7eac46e84321d36c67abf8345e"
_F3 = "22a1f1fd9ddf02a97776d991f11280b3982673693a4f357479098a99fb411a16"
_ELIMINATED = "b2fc30c077e93d18fb94fa54a89e27f7a3b4021e26af6468b4ce54ed6af7d2ba"
_FINALISTS = (_F1, _F2, _F3, _SEED)
_INHERITED = {_F1: 42, _F2: 25, _F3: 36, _SEED: 0}

_SHA = "a" * 64
_GIT = "b" * 40


def _document() -> dict[str, Any]:
    """A minimal, well-formed freeze. Every rejection test mutates exactly one thing in it."""
    return {
        "schema": FINALIST_FREEZE_SCHEMA,
        "phase_c_closure_artifact": {"path": "/x/phase_c_complete.json", "sha256": "c" * 64},
        "baseline_protocol_hash": _SHA,
        "phase_b_completion_hash": _SHA,
        "phase_c_candidate_set_hash": _SHA,
        "phase_c_plan_hash": _SHA,
        "parameter_space_hash": _SHA,
        "execution_environment_hash": _SHA,
        "scoring_contract_hash": _SHA,
        "minos_subnet_sha": _GIT,
        "validation_finalists_ordered": list(_FINALISTS),
        "validation_finalist_count": 4,
        "seed_config_hash": _SEED,
        "finished_ledger_alive_hashes": [*_FINALISTS, "d" * 64],
        "finished_ledger_eliminated_hashes": [_ELIMINATED],
        "finalist_detail": [
            {"config_hash": h, "inherited_phase_b_index": _INHERITED[h]} for h in _FINALISTS
        ],
    }


def _verify(document: dict[str, Any], **overrides: Any) -> Any:
    kwargs: dict[str, Any] = {
        "artifact_path": "/x/freeze.json",
        "artifact_sha256": _SHA,
        "expected_artifact_sha256": _SHA,
    }
    kwargs.update(overrides)
    return verify_finalist_freeze_document(document, **kwargs)


def _freeze() -> Any:
    return _verify(_document())


def _authority() -> Any:
    return build_l2f2_phase_d_authority(_freeze(), schedule=build_validation_schedule())


# --------------------------------------------------------------------------------------------
# 1-9: the frozen finalist outcome is accepted only when it is exactly itself
# --------------------------------------------------------------------------------------------


def test_a_well_formed_finalist_freeze_verifies() -> None:
    freeze = _freeze()
    assert freeze.ordered_finalists == _FINALISTS
    assert freeze.seed_config_hash == _SEED
    assert freeze.finalist_count == PHASE_D_CANDIDATE_COUNT
    assert freeze.inherited_index_of(_F1) == 42


def test_a_single_changed_byte_is_rejected_by_digest() -> None:
    with pytest.raises(FinalistFreezeError, match="its bytes have changed"):
        _verify(_document(), artifact_sha256="f" * 64)


def test_the_wrong_expected_artifact_digest_is_rejected() -> None:
    with pytest.raises(FinalistFreezeError, match="bytes have changed"):
        _verify(_document(), expected_artifact_sha256="e" * 64)


def test_a_foreign_schema_is_rejected() -> None:
    document = _document()
    document["schema"] = "l2f2-something-else-v9"
    with pytest.raises(FinalistFreezeError, match="declares schema"):
        _verify(document)


def test_a_reordered_finalist_tuple_is_rejected_against_an_expected_order() -> None:
    document = _document()
    document["validation_finalists_ordered"] = [_F2, _F1, _F3, _SEED]
    with pytest.raises(FinalistFreezeError, match="in value or in order"):
        _verify(document, expected_finalists=_FINALISTS)


def test_a_duplicated_finalist_is_rejected() -> None:
    document = _document()
    document["validation_finalists_ordered"] = [_F1, _F1, _F3, _SEED]
    with pytest.raises(FinalistFreezeError, match="repeats a configuration"):
        _verify(document)


def test_a_freeze_without_the_seed_is_rejected() -> None:
    document = _document()
    document["validation_finalists_ordered"] = [_F1, _F2, _F3, "9" * 64]
    document["finished_ledger_alive_hashes"].append("9" * 64)
    document["finalist_detail"].append({"config_hash": "9" * 64, "inherited_phase_b_index": 7})
    with pytest.raises(FinalistFreezeError, match="seed is not among"):
        _verify(document)


@pytest.mark.parametrize("count", [3, 5])
def test_exactly_four_finalists_are_required(count: int) -> None:
    document = _document()
    document["validation_finalists_ordered"] = list(_FINALISTS)[:count] or [_SEED]
    if count == 5:
        document["validation_finalists_ordered"] = [*_FINALISTS, "9" * 64]
    with pytest.raises(FinalistFreezeError, match="exactly 4 finalists"):
        _verify(document)


def test_a_cumulatively_eliminated_configuration_may_not_be_a_finalist() -> None:
    document = _document()
    document["validation_finalists_ordered"] = [_F1, _F2, _ELIMINATED, _SEED]
    document["finalist_detail"].append({"config_hash": _ELIMINATED, "inherited_phase_b_index": 43})
    with pytest.raises(FinalistFreezeError, match="recorded as eliminated"):
        _verify(document)


def test_a_freeze_from_another_search_is_rejected_by_identity() -> None:
    with pytest.raises(FinalistFreezeError, match="different search"):
        _verify(_document(), expected_identities={"baseline_protocol_hash": "0" * 64})


def test_a_freeze_bound_to_another_phase_c_closure_is_rejected() -> None:
    with pytest.raises(FinalistFreezeError, match="not derived"):
        _verify(_document(), expected_phase_c_closure_sha256="9" * 64)


def test_the_real_frozen_outcome_verifies_against_its_recorded_digests(tmp_path: Path) -> None:
    """The committed loader accepts the real artifact — and only at its real digest."""
    artifact = Path(
        "/home/hr/bittensor/minos_l2f2_baseline/phase_c_validation_finalists_20260830.json"
    )
    if not artifact.is_file():  # pragma: no cover - campaign evidence is not part of the repo
        pytest.skip("the campaign freeze artifact is not present in this environment")
    freeze = load_finalist_freeze(
        artifact,
        expected_artifact_sha256="540aeca0640871ca91e3ec771ec66d2df4b96d38210ec3265f944dee3e0433f3",
        expected_phase_c_closure_sha256=(
            "5de368eec327b66c868737d1819cc1b1a590eaf185b28e53d1cfecae59b593ca"
        ),
        expected_finalists=_FINALISTS,
    )
    assert freeze.ordered_finalists == _FINALISTS

    tampered = tmp_path / "tampered.json"
    document = json.loads(artifact.read_text())
    document["validation_finalists_ordered"] = [_F2, _F1, _F3, _SEED]
    tampered.write_text(json.dumps(document))
    with pytest.raises(FinalistFreezeError):
        load_finalist_freeze(
            tampered,
            expected_artifact_sha256=(
                "540aeca0640871ca91e3ec771ec66d2df4b96d38210ec3265f944dee3e0433f3"
            ),
        )


# --------------------------------------------------------------------------------------------
# 10-13: the VALIDATION member authority, and the partitions it must never admit
# --------------------------------------------------------------------------------------------


def test_the_validation_schedule_holds_exactly_ten_members() -> None:
    schedule = build_validation_schedule()
    assert len(schedule.members) == VALIDATION_COUNT == 10
    assert len(set(schedule.dataset_ids)) == 10


def test_every_chromosome_contributes_exactly_two_validation_members() -> None:
    assert build_validation_schedule().per_chromosome() == {
        "chr18": VALIDATION_PER_CHROMOSOME,
        "chr19": VALIDATION_PER_CHROMOSOME,
        "chr20": VALIDATION_PER_CHROMOSOME,
        "chr21": VALIDATION_PER_CHROMOSOME,
        "chr22": VALIDATION_PER_CHROMOSOME,
    }


def test_no_train_member_appears_in_the_validation_schedule() -> None:
    train = {m.dataset_id for m in build_train_schedule().members}
    validation = set(build_validation_schedule().dataset_ids)
    assert not (train & validation)


def test_no_test_member_appears_in_the_validation_schedule(tmp_path: Path) -> None:
    """TEST is filtered before anything reads it, and a TEST id among the ten is refused."""
    from minos_engine.baseline.schedule import SPLIT_MANIFEST_PATH

    source = Path(__file__).resolve().parents[3] / SPLIT_MANIFEST_PATH
    document = json.loads(source.read_text())
    test_ids = {s["dataset_id"] for s in document["samples"] if s["partition"] == "test"}
    validation_ids = set(build_validation_schedule().dataset_ids)
    assert test_ids
    assert not (test_ids & validation_ids)

    # and if a TEST dataset were relabelled into the validation set, the closing check refuses it
    poisoned = copy.deepcopy(document)
    a_test = next(s for s in poisoned["samples"] if s["partition"] == "test")
    a_validation = next(s for s in poisoned["samples"] if s["partition"] == "validation")
    a_validation["dataset_id"] = a_test["dataset_id"]
    root = tmp_path / "root"
    (root / Path(SPLIT_MANIFEST_PATH).parent).mkdir(parents=True)
    (root / SPLIT_MANIFEST_PATH).write_text(json.dumps(poisoned))
    with pytest.raises(ScheduleError, match="TRAIN or TEST dataset reached"):
        build_validation_schedule(root)


def test_a_short_validation_partition_is_refused_rather_than_truncated(tmp_path: Path) -> None:
    from minos_engine.baseline.schedule import SPLIT_MANIFEST_PATH

    source = Path(__file__).resolve().parents[3] / SPLIT_MANIFEST_PATH
    document = json.loads(source.read_text())
    document["samples"] = [s for s in document["samples"] if s["partition"] != "validation"][:5]
    root = tmp_path / "root"
    (root / Path(SPLIT_MANIFEST_PATH).parent).mkdir(parents=True)
    (root / SPLIT_MANIFEST_PATH).write_text(json.dumps(document))
    with pytest.raises(ScheduleError, match="expected 10 VALIDATION samples"):
        build_validation_schedule(root)


# --------------------------------------------------------------------------------------------
# 14-18: the logical plan is the complete 4 x 10 cross product, and it is deterministic
# --------------------------------------------------------------------------------------------


def test_the_validation_plan_is_exactly_forty_logical_jobs() -> None:
    authority = _authority()
    assert authority.candidate_count == PHASE_D_CANDIDATE_COUNT == 4
    assert authority.member_count == 10
    assert authority.logical_job_count == PHASE_D_LOGICAL_JOB_BUDGET == 40
    assert len(eligible_l2f2_validation_jobs(authority)) == 40


def test_every_finalist_receives_every_member_exactly_once() -> None:
    authority = _authority()
    pairs = eligible_l2f2_validation_jobs(authority)
    seen = [(p.config_hash, p.dataset_id) for p in pairs]
    assert len(seen) == len(set(seen)) == 40
    for config_hash in _FINALISTS:
        members = {p.dataset_id for p in pairs if p.config_hash == config_hash}
        assert members == set(authority.schedule.dataset_ids)
    assert len({p.logical_key for p in pairs}) == 40


def test_the_validation_plan_hash_is_deterministic() -> None:
    first = _authority().plan_hash
    second = _authority().plan_hash
    assert first == second and len(first) == 64


def test_the_plan_hash_changes_when_the_frozen_finalists_change() -> None:
    baseline = _authority().plan_hash
    document = _document()
    document["validation_finalists_ordered"] = [_F2, _F1, _F3, _SEED]
    reordered = build_l2f2_phase_d_authority(
        _verify(document), schedule=build_validation_schedule()
    )
    assert reordered.plan_hash != baseline


def test_the_plan_identity_commits_to_there_being_no_racing() -> None:
    """The absence of racing is part of what the plan hash signs, not a runtime convention."""
    from minos_engine.baseline.phase_d import compute_phase_d_plan_hash

    authority = _authority()
    common: dict[str, Any] = {
        "baseline_protocol_hash": authority.baseline_protocol_hash,
        "finalist_freeze_sha256": authority.finalist_freeze_sha256,
        "phase_c_closure_sha256": authority.phase_c_closure_sha256,
        "phase_c_plan_hash": authority.phase_c_plan_hash,
        "phase_c_candidate_set_hash": authority.phase_c_candidate_set_hash,
        "phase_b_completion_hash": authority.phase_b_completion_hash,
        "parameter_space_hash": authority.parameter_space_hash,
        "execution_environment_hash": authority.execution_environment_hash,
        "scoring_contract_hash": authority.scoring_contract_hash,
        "minos_subnet_sha": authority.minos_subnet_sha,
        "split_manifest_sha256": authority.split_manifest_sha256,
        "ordered_config_hashes": authority.ordered_config_hashes,
        "seed_config_hash": authority.seed_config_hash,
        "inherited_candidate_index": authority.inherited_candidate_index,
        "member_pairs": authority.required_pairs(),
    }
    assert compute_phase_d_plan_hash(**common) == authority.plan_hash
    assert PHASE_D_RACING_RULE == "NONE_EVERY_FINALIST_RECEIVES_EVERY_MEMBER"


def test_the_eligible_job_set_is_idempotent() -> None:
    """Asking twice yields the same forty keys in the same order — materialization is a no-op."""
    authority = _authority()
    first = [p.logical_key for p in eligible_l2f2_validation_jobs(authority)]
    second = [p.logical_key for p in eligible_l2f2_validation_jobs(authority)]
    assert first == second


def test_a_wrong_sized_member_set_cannot_produce_a_plan() -> None:
    """A nine-member schedule is refused at construction — it never reaches a plan."""
    schedule = build_validation_schedule()
    with pytest.raises(ScheduleError, match="always holds ten members"):
        type(schedule)(
            members=schedule.members[:9], split_manifest_sha256=schedule.split_manifest_sha256
        )


# --------------------------------------------------------------------------------------------
# 17: there is no racing API anywhere in the validation path
# --------------------------------------------------------------------------------------------


def test_the_validation_control_plane_exposes_no_racing_function() -> None:
    import minos_engine.baseline.phase_d as phase_d
    import minos_engine.storage.l2f2_validation_control as control

    for module in (phase_d, control):
        exported = set(getattr(module, "__all__", ()))
        assert not any("race" in name.lower() for name in exported)
        assert not any("eliminat" in name.lower() for name in exported)
        assert not any("surviv" in name.lower() for name in exported)


def test_reaching_for_an_elimination_step_is_a_named_refusal() -> None:
    from minos_engine.storage.l2f2_validation_control import _reject_racing

    with pytest.raises(PhaseDError, match="does not race"):
        _reject_racing()


# --------------------------------------------------------------------------------------------
# 25-26: the final ranking needs ALL forty, and never re-chooses the finalists
# --------------------------------------------------------------------------------------------


def _observation(config_hash: str, dataset_id: str, chromosome: str, score: float) -> Any:
    """A decided observation. A non-admitted one carries NO score: the model refuses one.

    That refusal is the committed guarantee behind "never persist a fabricated zero" — a candidate
    failure is an absence of utility, not a low number, and only the aggregate turns it into one.
    """
    admitted = score > 0.0
    return BaselineObservation(
        config_hash=config_hash,
        dataset_id=dataset_id,
        chromosome=chromosome,
        minos_score=score if admitted else None,
        admitted=admitted,
        failure_code=None if admitted else "GATK_OUTPUT_INVALID",
        gatk_runtime_ms=60_000,
    )


def _complete_observations(authority: Any, *, scores: dict[str, float] | None = None) -> list[Any]:
    scores = scores or {h: 0.4 + 0.1 * i for i, h in enumerate(authority.ordered_config_hashes)}
    return [
        _observation(p.config_hash, p.dataset_id, p.chromosome, scores[p.config_hash])
        for p in authority.pairs()
    ]


def test_a_complete_validation_ranking_orders_all_four_finalists() -> None:
    authority = _authority()
    ranking = rank_validation_observations(authority, _complete_observations(authority))
    assert ranking.observation_count == 40
    assert len(ranking.entries) == 4
    assert {e.config_hash for e in ranking.entries} == set(_FINALISTS)
    assert [e.rank for e in ranking.entries] == [1, 2, 3, 4]
    assert all(e.observed_count == 10 for e in ranking.entries)


def test_ranking_refuses_until_all_forty_observations_are_decided() -> None:
    authority = _authority()
    observations = _complete_observations(authority)[:39]
    with pytest.raises(ValidationControlError, match="requires all 40 observations"):
        rank_validation_observations(authority, observations)


def test_ranking_refuses_while_an_infrastructure_incident_exists() -> None:
    authority = _authority()
    with pytest.raises(ValidationControlError, match="infrastructure incident"):
        rank_validation_observations(
            authority, _complete_observations(authority), infrastructure_incident_count=1
        )


def test_ranking_refuses_when_a_finalist_is_short_of_its_ten_members() -> None:
    authority = _authority()
    observations = _complete_observations(authority)
    # forty observations, but one finalist gets eleven and another nine
    observations[0] = _observation(_F2, observations[0].dataset_id, observations[0].chromosome, 0.5)
    with pytest.raises(ValidationControlError, match="every finalist receives every member"):
        rank_validation_observations(authority, observations)


def test_a_candidate_failure_does_not_stop_the_ranking() -> None:
    """A finalist that fails on a member is still ranked — the campaign does not stop for it."""
    authority = _authority()
    observations = _complete_observations(authority)
    observations[0] = _observation(
        observations[0].config_hash, observations[0].dataset_id, observations[0].chromosome, 0.0
    )
    ranking = rank_validation_observations(authority, observations)
    assert ranking.observation_count == 40
    failed = next(e for e in ranking.entries if e.config_hash == observations[0].config_hash)
    assert failed.candidate_failure_count == 1


def test_the_ranking_never_changes_who_was_validated() -> None:
    """A ranking is an ORDER over the frozen four, never a membership decision."""
    authority = _authority()
    scores = {_F1: 0.1, _F2: 0.9, _F3: 0.5, _SEED: 0.2}
    ranking = rank_validation_observations(
        authority, _complete_observations(authority, scores=scores)
    )
    assert ranking.ordered_config_hashes == _FINALISTS  # the frozen input, unchanged
    assert {e.config_hash for e in ranking.entries} == set(_FINALISTS)
    assert ranking.finalist_freeze_sha256 == authority.finalist_freeze_sha256
    assert ranking.seed_config_hash == _SEED
    # the seed is ranked like everything else, and is never dropped from the reported set
    assert _SEED in {e.config_hash for e in ranking.entries}


def test_the_ranking_uses_the_committed_objective_and_tie_break() -> None:
    """No post-hoc metric: the reported order is the committed tie_break_key order."""
    from minos_engine.baseline.objective import aggregate_candidate, tie_break_key

    authority = _authority()
    scores = {_F1: 0.30, _F2: 0.80, _F3: 0.55, _SEED: 0.45}
    observations = _complete_observations(authority, scores=scores)
    ranking = rank_validation_observations(authority, observations)

    by_config: dict[str, list[Any]] = {h: [] for h in authority.ordered_config_hashes}
    for observation in observations:
        by_config[observation.config_hash].append(observation)
    aggregates = {
        h: aggregate_candidate(
            config_hash=h, observations=obs, required_members=authority.required_pairs()
        )
        for h, obs in by_config.items()
    }
    expected = sorted(
        authority.ordered_config_hashes,
        key=lambda h: tie_break_key(
            aggregates[h], candidate_index=authority.inherited_candidate_index[h]
        ),
    )
    assert [e.config_hash for e in ranking.entries] == expected
    assert ranking.leader == expected[0]


# --------------------------------------------------------------------------------------------
# 22: the evaluator gate, and the phase vocabulary
# --------------------------------------------------------------------------------------------


def test_the_validation_partition_gate_refuses_train_and_test() -> None:
    from minos_engine.evaluation.truth_registration import (
        ForbiddenPartitionError,
        refuse_non_validation_partition,
    )

    refuse_non_validation_partition("validation")  # the only accepted value
    for partition in ("train", "test", "", "VALIDATION"):
        with pytest.raises(ForbiddenPartitionError):
            refuse_non_validation_partition(partition)


def test_the_train_partition_gate_is_unchanged_and_still_refuses_validation() -> None:
    """The TRAIN path must not be weakened by the arrival of a validation one."""
    from minos_engine.evaluation.truth_registration import (
        ForbiddenPartitionError,
        refuse_non_train_partition,
    )

    refuse_non_train_partition("train")
    for partition in ("validation", "test"):
        with pytest.raises(ForbiddenPartitionError):
            refuse_non_train_partition(partition)


def test_the_validation_evaluator_defaults_to_the_validation_gate() -> None:
    """The named validation entry cannot be talked into accepting TRAIN."""
    import inspect

    from minos_engine.evaluation.validation_orchestrator import evaluate_validation_execution

    parameters = inspect.signature(evaluate_validation_execution).parameters
    assert "partition_gate" not in parameters  # no argument by which to change the partition


def test_the_train_evaluator_still_defaults_to_the_train_gate() -> None:
    import inspect

    from minos_engine.evaluation.orchestrator import evaluate_execution
    from minos_engine.evaluation.truth_registration import refuse_non_train_partition

    default = inspect.signature(evaluate_execution).parameters["partition_gate"].default
    assert default is refuse_non_train_partition


def test_phase_d_is_the_single_validation_phase_name() -> None:
    """One vocabulary. The database, the protocol budget key and the source agree."""
    from minos_engine.baseline.protocol import PHASE_D_MEMBER_COUNT, build_baseline_protocol
    from minos_engine.storage.l2f2_runner import _RESOLVE_SQL_BY_PHASE

    assert PHASE_D_PHASE == "PHASE_D"
    assert PHASE_D_MEMBER_COUNT == VALIDATION_COUNT == 10
    assert "PHASE_D" in _RESOLVE_SQL_BY_PHASE
    validation = build_baseline_protocol().content()["validation"]
    assert validation["stage"] == "L2-F2-F"
    assert validation["member_count"] == 10
    assert validation["evaluations"] == PHASE_D_LOGICAL_JOB_BUDGET == 40
    assert validation["racing"] == PHASE_D_RACING_RULE


def test_the_phase_d_resolver_is_distinct_from_every_train_resolver() -> None:
    """A validation worker cannot reach a TRAIN resolver, or the reverse."""
    from minos_engine.storage.l2f2_runner import _RESOLVE_SQL_BY_PHASE

    statements = set(_RESOLVE_SQL_BY_PHASE.values())
    assert len(statements) == len(_RESOLVE_SQL_BY_PHASE) == 4
    assert "phase_d" in _RESOLVE_SQL_BY_PHASE["PHASE_D"]
    for phase in ("PHASE_A", "PHASE_B", "PHASE_C"):
        assert "phase_d" not in _RESOLVE_SQL_BY_PHASE[phase]


def test_the_freeze_hashes_nothing_it_was_not_given() -> None:
    """Loading a freeze reads exactly one file and no truth of any kind."""
    document = _document()
    freeze = _verify(document)
    rendered = json.dumps(document)
    assert "truth" not in rendered.lower()
    assert freeze.artifact_sha256 == _SHA
