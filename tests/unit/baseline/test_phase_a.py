"""The frozen Phase-A execution authority and its canary identity. Pure — committed bytes only."""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

import pytest

from minos_engine.baseline.phase_a import (
    PHASE_A_AUTHORITY_MANIFEST,
    PHASE_A_CANDIDATE_COUNT,
    PHASE_A_LOGICAL_JOB_COUNT,
    PHASE_A_MEMBER_COUNT,
    PhaseAError,
    build_phase_a_authority,
    build_phase_a_plan,
    load_committed_phase_a_authority,
)
from minos_engine.baseline.schedule import CHROMOSOMES, build_train_schedule
from minos_engine.experiments.candidates import generate_accepted_candidate_set
from minos_engine.experiments.plan import iter_logical_jobs

_CANARY_DATASET = "minos-chr18-028662fb934529d7"
_CANARY_ROUND = "028662fb934529d7"


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def _clone(tmp_path: Path, name: str) -> Path:
    root = tmp_path / name
    (root / "manifests").mkdir(parents=True)
    for manifest in sorted((_repo_root() / "manifests").glob("*.json")):
        shutil.copy2(manifest, root / "manifests" / manifest.name)
    return root


# --------------------------------------------------------------------------- #
# plan shape
# --------------------------------------------------------------------------- #
def test_phase_a_is_five_members_by_thirty_nine_candidates() -> None:
    plan = build_phase_a_plan()
    assert plan.train_member_count == PHASE_A_MEMBER_COUNT == 5
    assert plan.candidate_count == PHASE_A_CANDIDATE_COUNT == 39
    assert plan.logical_job_count == PHASE_A_LOGICAL_JOB_COUNT == 195
    assert plan.partition == "train"


def test_phase_a_members_are_batch_zero_in_chromosome_order() -> None:
    plan = build_phase_a_plan()
    batch = build_train_schedule().batches[0]
    assert [m.dataset_id for m in plan.members] == [m.dataset_id for m in batch]
    assert [m.chromosome for m in batch] == list(CHROMOSOMES)
    assert [m.member_index for m in plan.members] == [0, 1, 2, 3, 4]


def test_phase_a_member_science_is_verbatim_from_the_accepted_plan() -> None:
    """Only the local member_index is renumbered; no scientific identity is invented."""
    from minos_engine.experiments.accepted_plan import build_accepted_experiment_plan

    accepted = {m.dataset_id: m for m in build_accepted_experiment_plan().members}
    for member in build_phase_a_plan().members:
        source = accepted[member.dataset_id]
        assert member.profile_id == source.profile_id
        assert member.content_hash == source.content_hash
        assert member.feature_values_hash == source.feature_values_hash
        assert member.vector_hash == source.vector_hash


def test_phase_a_uses_the_immutable_accepted_candidate_set() -> None:
    plan = build_phase_a_plan()
    candidate_set = generate_accepted_candidate_set()
    assert plan.candidate_set_hash == candidate_set.candidate_set_hash
    assert plan.candidate_set_hash == (
        "50d5f36918758de204e4b34cdd3fc8560a14debfcdb25869f713690c6085057d"
    )
    assert [c.config_hash for c in plan.configs] == list(candidate_set.ordered_config_hashes)


def test_no_validation_or_test_member_can_reach_phase_a() -> None:
    document = json.loads(
        (_repo_root() / "manifests" / "layer2_dataset_split_v2_epoch1.json").read_text()
    )
    closed = {s["dataset_id"] for s in document["samples"] if s["partition"] != "train"}
    assert closed
    assert {m.dataset_id for m in build_phase_a_plan().members}.isdisjoint(closed)


# --------------------------------------------------------------------------- #
# the canary is structurally logical job 0
# --------------------------------------------------------------------------- #
def test_the_canary_is_exactly_phase_a_logical_job_zero() -> None:
    authority = build_phase_a_authority()
    first = next(iter_logical_jobs(authority.plan))
    assert authority.canary.logical_index == 0
    assert authority.canary.job_key == first.job_key
    assert authority.canary.member_index == first.member_index == 0
    assert authority.canary.config_index == first.config_index == 0


def test_the_canary_is_the_first_chromosome_and_the_accepted_seed() -> None:
    authority = build_phase_a_authority()
    assert authority.canary.dataset_id == _CANARY_DATASET
    assert authority.canary.round_id == _CANARY_ROUND
    assert authority.canary.chromosome == "chr18"
    assert authority.canary.config_hash == generate_accepted_candidate_set().seed_config_hash
    assert len(authority.canary.config_hash) == 64


def test_the_canary_job_key_is_recomputed_not_asserted() -> None:
    """Recompute the key independently through the historical formula."""
    from minos_engine.experiments.plan import compute_job_key

    authority = build_phase_a_authority()
    plan = authority.plan
    member = plan.members[0]
    recomputed = compute_job_key(
        plan_hash=plan.plan_hash,
        member_index=member.member_index,
        dataset_id=member.dataset_id,
        profile_id=member.profile_id,
        content_hash=member.content_hash,
        feature_values_hash=member.feature_values_hash,
        config_index=0,
        config_hash=plan.configs[0].config_hash,
    )
    assert authority.canary.job_key == recomputed


def test_every_logical_job_key_is_unique() -> None:
    keys = [job.job_key for job in iter_logical_jobs(build_phase_a_plan())]
    assert len(keys) == PHASE_A_LOGICAL_JOB_COUNT
    assert len(set(keys)) == PHASE_A_LOGICAL_JOB_COUNT


# --------------------------------------------------------------------------- #
# determinism
# --------------------------------------------------------------------------- #
def test_the_authority_is_identical_from_independent_directories(tmp_path: Path) -> None:
    first = build_phase_a_authority(_clone(tmp_path, "one"))
    second = build_phase_a_authority(_clone(tmp_path, "two"))
    assert first.plan_hash == second.plan_hash
    assert first.canary.job_key == second.canary.job_key
    assert first.authority_hash == second.authority_hash
    assert json.dumps(first.content(), sort_keys=True) == json.dumps(
        second.content(), sort_keys=True
    )


def test_runtime_environment_cannot_move_the_authority(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    baseline = build_phase_a_authority().authority_hash
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    monkeypatch.setenv("HOSTNAME", "another-host")
    monkeypatch.chdir(tmp_path)
    assert build_phase_a_authority(_repo_root()).authority_hash == baseline
    assert os.environ["HOSTNAME"] == "another-host"


def test_the_authority_binds_the_frozen_protocol_and_schedule() -> None:
    from minos_engine.baseline.protocol import build_baseline_protocol

    authority = build_phase_a_authority()
    assert authority.baseline_protocol_hash == build_baseline_protocol().protocol_hash
    assert authority.split_manifest_sha256 == build_train_schedule().split_manifest_sha256
    content = authority.content()
    assert content["phase"] == "PHASE_A"
    assert content["plan"]["logical_job_count"] == 195


def test_a_changed_member_changes_the_authority(tmp_path: Path) -> None:
    """Swapping a scheduled member must move plan_hash, job_key and authority_hash."""
    root = _clone(tmp_path, "swapped")
    path = root / "manifests" / "layer2_dataset_split_v2_epoch1.json"
    document = json.loads(path.read_text())
    train = [s for s in document["samples"] if s["partition"] == "train"]
    chr18 = [s for s in train if s["chromosome"] == "chr18"]
    order = {s["dataset_id"]: i for i, s in enumerate(document["samples"])}
    a, b = order[chr18[0]["dataset_id"]], order[chr18[1]["dataset_id"]]
    document["samples"][a], document["samples"][b] = (
        document["samples"][b],
        document["samples"][a],
    )
    path.write_text(json.dumps(document), encoding="utf-8")

    swapped = build_phase_a_authority(root)
    baseline = build_phase_a_authority()
    assert swapped.plan_hash != baseline.plan_hash
    assert swapped.canary.job_key != baseline.canary.job_key
    assert swapped.authority_hash != baseline.authority_hash


# --------------------------------------------------------------------------- #
# the committed manifest
# --------------------------------------------------------------------------- #
def test_the_committed_authority_manifest_matches_the_code() -> None:
    document = load_committed_phase_a_authority()
    authority = build_phase_a_authority()
    assert document["authority_hash"] == authority.authority_hash
    assert document["content"] == authority.content()


def test_a_tampered_committed_authority_is_refused(tmp_path: Path) -> None:
    root = _clone(tmp_path, "tampered")
    path = root / PHASE_A_AUTHORITY_MANIFEST
    document = json.loads(path.read_text())
    document["content"]["canary"]["config_index"] = 1
    path.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(PhaseAError, match="does not match"):
        load_committed_phase_a_authority(root)


def test_a_missing_committed_authority_fails_closed(tmp_path: Path) -> None:
    with pytest.raises(PhaseAError, match="missing"):
        load_committed_phase_a_authority(tmp_path / "absent")


def test_the_committed_authority_carries_no_truth_score_or_runtime_identity() -> None:
    body = (_repo_root() / PHASE_A_AUTHORITY_MANIFEST).read_text().lower()
    for forbidden in ("truth", "mutation", "score", "worker", "timestamp", "/home/"):
        assert forbidden not in body, forbidden
