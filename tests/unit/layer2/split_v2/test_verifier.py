"""SPLIT-FROZEN v2 verifier: inheritance proof, parent immutability, tamper rejection."""

from __future__ import annotations

import copy
import json
from pathlib import Path

from minos_engine.layer2.split_v2.generator import (
    build_next_epoch_manifest,
    epoch1_from_v1_manifest,
)
from minos_engine.layer2.split_v2.policy import SUPPORTED_CHROMOSOMES
from minos_engine.layer2.split_v2.verifier import (
    verify_epoch_against_parent,
    verify_epoch_manifest,
)
from tests.conftest import REPO_ROOT

_V1 = Path(REPO_ROOT) / "manifests/layer2_dataset_split_v1.json"


def _v1() -> dict:
    return json.loads(_V1.read_text(encoding="utf-8"))


def _m1() -> dict:
    return epoch1_from_v1_manifest(_v1())


def _new_samples(per_chrom: int, tag: str) -> list[dict]:
    return [
        {
            "dataset_id": f"minos-{c}-{tag}{i:02d}",
            "round_id": f"{tag}{c[3:]}{i:04x}",
            "chromosome": c,
            "identity_tuple_hash": f"{i:02x}{ord(c[-1]):02x}" + "cd" * 30,
        }
        for c in SUPPORTED_CHROMOSOMES
        for i in range(per_chrom)
    ]


def _m2() -> tuple[dict, dict]:
    m1 = _m1()
    return m1, build_next_epoch_manifest(m1, _new_samples(5, "e2"))


# --------------------------------------------------------------------------- #
# epoch 1 verification
# --------------------------------------------------------------------------- #
def test_epoch1_verifies_with_v1_binding() -> None:
    r = verify_epoch_manifest(_m1(), v1_manifest=_v1())
    assert r.ok, [k for k, v in r.checks.items() if not v]
    for k in (
        "epoch1_inherits_v1_partitions_exactly",
        "epoch1_zero_transitions",
        "epoch1_test_cohort_preserved",
        "epoch1_validation_cohort_preserved",
        "epoch1_parent_fields_null",
        "ancestor_v1_dataset_registry_hash_bound",
        "registry_snapshot_hash_bound",
    ):
        assert r.checks[k] is True, k


def test_epoch1_with_moved_partition_rejected() -> None:
    """The exact defect from review: an accepted sample assigned differently than v1."""
    m = _m1()
    bad = copy.deepcopy(m)
    # move one accepted test sample to train (and keep counts consistent-looking)
    victim = next(s for s in bad["samples"] if s["partition"] == "test")
    victim["partition"] = "train"
    bad["counts"] = {"train": 51, "validation": 10, "test": 14}
    r = verify_epoch_manifest(bad, v1_manifest=_v1())
    assert not r.ok
    assert r.checks["epoch1_inherits_v1_partitions_exactly"] is False
    assert r.checks["epoch1_test_cohort_preserved"] is False


def test_epoch1_nonzero_transition_count_rejected() -> None:
    bad = copy.deepcopy(_m1())
    bad["transition_count"] = 1
    r = verify_epoch_manifest(bad, v1_manifest=_v1())
    assert not r.ok
    assert r.checks["epoch1_zero_transitions"] is False


def test_epoch1_tampered_registry_snapshot_rejected() -> None:
    bad = copy.deepcopy(_m1())
    bad["registry_snapshot_hash"] = "0" * 64
    r = verify_epoch_manifest(bad, v1_manifest=_v1())
    assert not r.ok
    assert r.checks["registry_snapshot_hash_bound"] is False


def test_epoch1_wrong_ancestor_hash_rejected() -> None:
    bad = copy.deepcopy(_m1())
    bad["ancestor_v1_dataset_registry_hash"] = "0" * 64
    r = verify_epoch_manifest(bad, v1_manifest=_v1())
    assert not r.ok
    assert r.checks["ancestor_v1_dataset_registry_hash_bound"] is False


# --------------------------------------------------------------------------- #
# epoch ≥2 parent verification
# --------------------------------------------------------------------------- #
def test_epoch2_verifies_against_parent() -> None:
    m1, m2 = _m2()
    r = verify_epoch_manifest(m2, parent_manifest=m1)
    assert r.ok, [k for k, v in r.checks.items() if not v]
    for k in (
        "parent_manifest_hash_bound",
        "parent_registry_snapshot_hash_bound",
        "parent_samples_immutable",
        "growth_new_samples_only",
        "no_parent_removed",
        "no_round_id_replacement",
        "child_transition_count_zero",
    ):
        assert r.checks[k] is True, k


def test_parent_partition_change_rejected() -> None:
    m1, m2 = _m2()
    bad = copy.deepcopy(m2)
    victim = next(s for s in bad["samples"] if s["origin_epoch"] == 1 and s["partition"] == "test")
    victim["partition"] = "train"
    checks = verify_epoch_against_parent(m1, bad)
    assert checks["parent_samples_immutable"] is False


def test_parent_removal_rejected() -> None:
    m1, m2 = _m2()
    bad = copy.deepcopy(m2)
    removed = bad["samples"][0]["dataset_id"]
    bad["samples"] = [s for s in bad["samples"] if s["dataset_id"] != removed]
    checks = verify_epoch_against_parent(m1, bad)
    assert checks["no_parent_removed"] is False


def test_parent_identity_replacement_rejected() -> None:
    m1, m2 = _m2()
    bad = copy.deepcopy(m2)
    victim = next(s for s in bad["samples"] if s["origin_epoch"] == 1)
    victim["identity_tuple_hash"] = "f" * 64  # same id, different identity tuple
    checks = verify_epoch_against_parent(m1, bad)
    assert checks["parent_samples_immutable"] is False


def test_wrong_parent_manifest_hash_rejected() -> None:
    m1, m2 = _m2()
    bad = copy.deepcopy(m2)
    bad["parent_manifest_hash"] = "0" * 64
    checks = verify_epoch_against_parent(m1, bad)
    assert checks["parent_manifest_hash_bound"] is False


def test_new_sample_with_wrong_origin_rejected() -> None:
    m1, m2 = _m2()
    bad = copy.deepcopy(m2)
    new = next(s for s in bad["samples"] if s["assignment_source"] == "v2-policy")
    new["origin_epoch"] = 1  # masquerading as inherited
    checks = verify_epoch_against_parent(m1, bad)
    assert checks["growth_new_samples_only"] is False
