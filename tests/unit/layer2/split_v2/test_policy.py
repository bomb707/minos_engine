"""SPLIT-FROZEN v2 policy: stratified epoch-1 = 50/10/15, growth-stable, monotonic."""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from pathlib import Path

import pytest

from minos_engine.layer2.split_v2.policy import (
    PARTITIONS,
    SUPPORTED_CHROMOSOMES,
    SplitPolicyError,
    allocation_digest,
    assign_epoch,
    partition_targets,
    split_policy_hash,
)
from tests.conftest import REPO_ROOT


def _accepted_75() -> list[tuple[str, str]]:
    man = json.loads(
        (Path(REPO_ROOT) / "manifests/layer2_dataset_split_v1.json").read_text(encoding="utf-8")
    )
    return [(s["round_id"], s["chromosome"]) for s in man["samples"]]


# --------------------------------------------------------------------------- #
# largest-remainder targets
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "n,expected",
    [
        (15, {"train": 10, "validation": 2, "test": 3}),
        (20, {"train": 13, "validation": 3, "test": 4}),
        (30, {"train": 20, "validation": 4, "test": 6}),
        (0, {"train": 0, "validation": 0, "test": 0}),
        (1, {"train": 1, "validation": 0, "test": 0}),
    ],
)
def test_partition_targets(n: int, expected: dict) -> None:
    t = partition_targets(n)
    assert t == expected
    assert sum(t.values()) == n  # always sums exactly


def test_targets_always_sum_exact() -> None:
    for n in range(0, 200):
        assert sum(partition_targets(n).values()) == n


# --------------------------------------------------------------------------- #
# epoch 1 = the accepted 75 → exactly 50/10/15, 10/2/3 per chromosome
# --------------------------------------------------------------------------- #
def test_epoch1_is_50_10_15_stratified() -> None:
    samples = _accepted_75()
    assert len(samples) == 75
    assign = assign_epoch({}, samples)
    overall = Counter(assign.values())
    assert overall == Counter(train=50, validation=10, test=15)
    per: dict[str, Counter] = defaultdict(Counter)
    for rid, chrom in samples:
        per[chrom][assign[rid]] += 1
    for chrom in SUPPORTED_CHROMOSOMES:
        assert per[chrom] == Counter(train=10, validation=2, test=3), chrom


def test_epoch1_deterministic() -> None:
    samples = _accepted_75()
    assert assign_epoch({}, samples) == assign_epoch({}, samples)


# --------------------------------------------------------------------------- #
# growth: grandfathered, monotonic test set, per-chromosome targets kept
# --------------------------------------------------------------------------- #
def _synthetic(per_chrom: int, tag: str) -> list[tuple[str, str]]:
    return [(f"{c}-{tag}-{i:04x}", c) for c in SUPPORTED_CHROMOSOMES for i in range(per_chrom)]


def test_growth_grandfathers_and_is_monotonic() -> None:
    e1_samples = _synthetic(15, "e1")
    e1 = assign_epoch({}, e1_samples)
    assert Counter(e1.values()) == Counter(train=50, validation=10, test=15)

    e2_samples = e1_samples + _synthetic(5, "e2")  # 20 per chromosome
    e2 = assign_epoch(e1, e2_samples)

    # no existing sample moved
    assert all(e2[rid] == e1[rid] for rid, _ in e1_samples)
    # test set is monotonic
    test1 = {rid for rid, _ in e1_samples if e1[rid] == "test"}
    test2 = {rid for rid, _ in e2_samples if e2[rid] == "test"}
    assert test1 <= test2
    # each chromosome hits its epoch-2 target (13/3/4 at n=20)
    per: dict[str, Counter] = defaultdict(Counter)
    for rid, chrom in e2_samples:
        per[chrom][e2[rid]] += 1
    for chrom in SUPPORTED_CHROMOSOMES:
        assert per[chrom] == Counter(train=13, validation=3, test=4), chrom


def test_growth_over_many_epochs_never_moves_existing() -> None:
    samples: list[tuple[str, str]] = []
    prior: dict[str, str] = {}
    seen: dict[str, str] = {}
    for epoch in range(1, 6):
        samples = samples + _synthetic(3, f"ep{epoch}")
        assign = assign_epoch(prior, samples)
        for rid, part in seen.items():
            assert assign[rid] == part  # never moves
        seen = assign
        prior = assign
    # ratios stay close to 66.7/13.3/20 over the whole 75
    counts = Counter(prior.values())
    assert counts["train"] + counts["validation"] + counts["test"] == 75


# --------------------------------------------------------------------------- #
# fail-closed
# --------------------------------------------------------------------------- #
def test_duplicate_round_id_rejected() -> None:
    with pytest.raises(SplitPolicyError):
        assign_epoch({}, [("r1", "chr18"), ("r1", "chr18")])


def test_unsupported_chromosome_rejected() -> None:
    with pytest.raises(SplitPolicyError):
        assign_epoch({}, [("r1", "chrX")])


def test_non_additive_prior_rejected() -> None:
    with pytest.raises(SplitPolicyError):
        assign_epoch({"gone": "train"}, [("r1", "chr18")])


def test_invalid_prior_partition_rejected() -> None:
    with pytest.raises(SplitPolicyError):
        assign_epoch({"r1": "holdout"}, [("r1", "chr18")])


def test_empty_round_id_rejected() -> None:
    with pytest.raises(SplitPolicyError):
        allocation_digest("")


# --------------------------------------------------------------------------- #
# policy hash is stable + deterministic
# --------------------------------------------------------------------------- #
def test_policy_hash_stable() -> None:
    import re

    h = split_policy_hash()
    assert re.fullmatch(r"[0-9a-f]{64}", h)
    assert h == split_policy_hash()


def test_partition_labels_are_the_three_expected() -> None:
    assert set(PARTITIONS) == {"train", "validation", "test"}
