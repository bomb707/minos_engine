"""The frozen chromosome-balanced TRAIN schedule. Pure — committed bytes only."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from minos_engine.baseline.schedule import (
    BATCH_COUNT,
    CHROMOSOMES,
    SPLIT_MANIFEST_PATH,
    TRAIN_COUNT,
    TRAIN_PER_CHROMOSOME,
    ScheduleError,
    build_train_schedule,
    split_manifest_sha256,
)


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def _closed_partition_ids() -> set[str]:
    document = json.loads((_repo_root() / SPLIT_MANIFEST_PATH).read_text(encoding="utf-8"))
    return {s["dataset_id"] for s in document["samples"] if s["partition"] != "train"}


def test_the_schedule_is_exactly_fifty_train_members() -> None:
    schedule = build_train_schedule()
    members = schedule.members
    assert len(members) == TRAIN_COUNT == 50
    assert len({m.dataset_id for m in members}) == 50


def test_every_chromosome_contributes_exactly_ten_members() -> None:
    schedule = build_train_schedule()
    for chromosome in CHROMOSOMES:
        assert sum(1 for m in schedule.members if m.chromosome == chromosome) == (
            TRAIN_PER_CHROMOSOME
        )


def test_there_are_ten_balanced_batches_of_five() -> None:
    schedule = build_train_schedule()
    assert len(schedule.batches) == BATCH_COUNT == 10
    for batch in schedule.batches:
        assert len(batch) == 5
        assert tuple(m.chromosome for m in batch) == CHROMOSOMES, "one per chromosome, in order"


def test_no_member_appears_in_two_batches() -> None:
    schedule = build_train_schedule()
    seen: set[str] = set()
    for batch in schedule.batches:
        for member in batch:
            assert member.dataset_id not in seen
            seen.add(member.dataset_id)


def test_the_phase_slices_are_the_frozen_prefixes() -> None:
    schedule = build_train_schedule()
    assert len(schedule.phase_members(1)) == 5, "Phase A: batch 0"
    assert len(schedule.phase_members(2)) == 10, "Phase B: batches 0-1"
    assert len(schedule.phase_members(BATCH_COUNT)) == 50, "Phase C: all batches"
    # every phase prefix is itself chromosome-balanced, which the floor term depends on
    for count in (1, 2, 5, BATCH_COUNT):
        members = schedule.phase_members(count)
        for chromosome in CHROMOSOMES:
            assert sum(1 for m in members if m.chromosome == chromosome) == count


def test_phase_b_is_exactly_the_phase_a_members_plus_one_more_batch() -> None:
    schedule = build_train_schedule()
    assert schedule.phase_members(1) == schedule.phase_members(2)[:5]


@pytest.mark.parametrize("bad", [0, -1, BATCH_COUNT + 1])
def test_an_out_of_range_phase_slice_is_refused(bad: int) -> None:
    with pytest.raises(ScheduleError, match="outside"):
        build_train_schedule().phase_members(bad)


def test_no_validation_or_test_identity_can_reach_the_schedule() -> None:
    schedule = build_train_schedule()
    identifiers = {m.dataset_id for m in schedule.members}
    closed = _closed_partition_ids()
    assert len(closed) == 25, "10 validation + 15 test exist in the manifest"
    assert identifiers.isdisjoint(closed)


def test_the_schedule_is_byte_identical_on_repeated_generation() -> None:
    first = build_train_schedule()
    second = build_train_schedule()
    assert first.content() == second.content()
    assert json.dumps(first.content(), sort_keys=True) == json.dumps(
        second.content(), sort_keys=True
    )


def test_the_schedule_binds_the_committed_split_manifest_identity() -> None:
    schedule = build_train_schedule()
    assert schedule.split_manifest_sha256 == split_manifest_sha256()
    assert len(schedule.split_manifest_sha256) == 64


def test_a_manifest_missing_train_members_fails_closed(tmp_path: Path) -> None:
    document = json.loads((_repo_root() / SPLIT_MANIFEST_PATH).read_text(encoding="utf-8"))
    document["samples"] = [s for s in document["samples"] if s["partition"] == "train"][:-1]
    root = tmp_path / "repo"
    (root / "manifests").mkdir(parents=True)
    (root / SPLIT_MANIFEST_PATH).write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(ScheduleError, match="expected 50 TRAIN samples"):
        build_train_schedule(root)


def test_an_unbalanced_manifest_fails_closed(tmp_path: Path) -> None:
    """A 50-member manifest that is NOT ten per chromosome must not silently pass."""
    document = json.loads((_repo_root() / SPLIT_MANIFEST_PATH).read_text(encoding="utf-8"))
    train = [s for s in document["samples"] if s["partition"] == "train"]
    train[0] = {**train[0], "chromosome": "chr19"}
    document["samples"] = train
    root = tmp_path / "repo"
    (root / "manifests").mkdir(parents=True)
    (root / SPLIT_MANIFEST_PATH).write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(ScheduleError, match="TRAIN members, expected"):
        build_train_schedule(root)


def test_a_missing_manifest_fails_closed(tmp_path: Path) -> None:
    with pytest.raises(ScheduleError, match="missing"):
        build_train_schedule(tmp_path)
