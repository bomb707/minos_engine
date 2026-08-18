"""Unit tests for the fixed L2-C split policy."""

from __future__ import annotations

import hashlib

import pytest

from minos_engine.layer2.split.policy import (
    PARTITION_TOTALS,
    SALT,
    SAMPLES_PER_CHROMOSOME,
    SplitPolicyError,
    allocation_digest,
    assign_partitions,
    split_policy,
    split_policy_hash,
)


def _rounds(n=SAMPLES_PER_CHROMOSOME):
    return [f"{i:016x}" for i in range(n)]


def test_totals_are_50_10_15():
    assert PARTITION_TOTALS == {"train": 50, "validation": 10, "test": 15}


def test_allocation_digest_is_salted_sha256():
    rid = "abc123"
    assert allocation_digest(rid) == hashlib.sha256(f"{SALT}:{rid}".encode()).hexdigest()


def test_assign_partitions_10_2_3_and_ordered():
    result = assign_partitions(_rounds())
    parts = [p for _, p, _, _ in result]
    assert parts.count("train") == 10
    assert parts.count("validation") == 2
    assert parts.count("test") == 3
    # sort_order is a contiguous 0..14 range in digest order
    assert [o for _, _, o, _ in result] == list(range(15))
    # first 10 are train, next 2 validation, last 3 test
    assert parts[:10] == ["train"] * 10
    assert parts[10:12] == ["validation"] * 2
    assert parts[12:] == ["test"] * 3


def test_bytewise_hex_ordering_matches_integer_ordering():
    rounds = _rounds()
    order_hex = [r for r, _, _, _ in assign_partitions(rounds)]
    order_int = sorted(rounds, key=lambda r: int(allocation_digest(r), 16))
    assert order_hex == order_int


def test_shuffled_input_same_assignment():
    rounds = _rounds()
    a = assign_partitions(list(rounds))
    b = assign_partitions(list(reversed(rounds)))
    assert a == b


def test_wrong_count_rejected():
    with pytest.raises(SplitPolicyError):
        assign_partitions(_rounds(14))


def test_duplicate_round_rejected():
    rounds = _rounds(14) + ["0000000000000000"]
    rounds.append("0000000000000000")
    with pytest.raises(SplitPolicyError):
        assign_partitions(rounds)


def test_salt_change_changes_ordering():
    rounds = _rounds()
    real = [r for r, _, _, _ in assign_partitions(rounds)]
    other = sorted(rounds, key=lambda r: hashlib.sha256(f"other-salt:{r}".encode()).hexdigest())
    assert real != other  # negligibly-probable to coincide; salt defines identity


def test_policy_hash_is_deterministic_and_salt_bound():
    assert split_policy_hash() == split_policy_hash()
    assert split_policy()["salt"] == SALT
