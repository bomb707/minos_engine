"""SPLIT-FROZEN v2 generator: epoch-1 INHERITS the accepted v1 partitions exactly.

Proves the central leakage claim: zero assignment transitions versus v1 (no accepted
test/validation sample moves), the committed epoch-1 artifact is byte-reproducible, and
epoch ≥2 growth grandfathers every prior assignment while assigning only new samples.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from minos_engine.common.canonical_json import canonical_json_str
from minos_engine.common.errors import ContractValidationError
from minos_engine.layer2.split_v2.generator import (
    MANIFEST_SCHEMA_VERSION,
    build_next_epoch_manifest,
    epoch1_from_v1_manifest,
    registry_snapshot_hash,
)
from minos_engine.layer2.split_v2.policy import SUPPORTED_CHROMOSOMES, split_policy_hash
from tests.conftest import REPO_ROOT

_V1_MANIFEST = Path(REPO_ROOT) / "manifests/layer2_dataset_split_v1.json"
_V2_EPOCH1 = Path(REPO_ROOT) / "manifests/layer2_dataset_split_v2_epoch1.json"


def _v1() -> dict:
    return json.loads(_V1_MANIFEST.read_text(encoding="utf-8"))


def _new_samples(per_chrom: int, tag: str) -> list[dict]:
    return [
        {
            "dataset_id": f"minos-{c}-{tag}{i:02d}",
            "round_id": f"{tag}{c[3:]}{i:04x}",
            "chromosome": c,
            "identity_tuple_hash": f"{i:02x}{ord(c[-1]):02x}" + "ab" * 30,
        }
        for c in SUPPORTED_CHROMOSOMES
        for i in range(per_chrom)
    ]


# --------------------------------------------------------------------------- #
# epoch 1 — EXACT inheritance (the corrected central claim)
# --------------------------------------------------------------------------- #
def test_epoch1_zero_assignment_transitions_vs_v1() -> None:
    v1 = _v1()
    m = epoch1_from_v1_manifest(v1)
    v1p = {s["dataset_id"]: s["partition"] for s in v1["samples"]}
    mp = {s["dataset_id"]: s["partition"] for s in m["samples"]}
    assert set(v1p) == set(mp)
    moved = [d for d in v1p if v1p[d] != mp[d]]
    assert moved == []  # ZERO transitions — every v1 partition inherited verbatim
    assert m["transition_count"] == 0


def test_epoch1_test_and_validation_cohorts_preserved() -> None:
    v1 = _v1()
    m = epoch1_from_v1_manifest(v1)
    v1p = {s["dataset_id"]: s["partition"] for s in v1["samples"]}
    mp = {s["dataset_id"]: s["partition"] for s in m["samples"]}
    for part in ("test", "validation"):
        v1_cohort = {d for d, p in v1p.items() if p == part}
        m_cohort = {d for d, p in mp.items() if p == part}
        assert v1_cohort == m_cohort, part  # no accepted sample left its cohort


def test_epoch1_counts_and_stratification_inherited() -> None:
    m = epoch1_from_v1_manifest(_v1())
    assert m["epoch"] == 1
    assert m["parent_epoch"] is None
    assert m["counts"] == {"train": 50, "validation": 10, "test": 15}
    for chrom, counts in m["per_chromosome"].items():
        assert counts == {"train": 10, "validation": 2, "test": 3}, chrom
    assert len(m["samples"]) == 75
    assert m["inherited_count"] == 75
    assert m["new_count"] == 0


def test_epoch1_all_samples_marked_inherited() -> None:
    m = epoch1_from_v1_manifest(_v1())
    assert all(s["assignment_source"] == "v1-inherited" for s in m["samples"])
    assert all(s["origin_epoch"] == 1 for s in m["samples"])


def test_epoch1_bindings() -> None:
    v1 = _v1()
    m = epoch1_from_v1_manifest(v1)
    assert m["schema_version"] == MANIFEST_SCHEMA_VERSION
    assert m["ancestor_v1_dataset_registry_hash"] == v1["dataset_registry_hash"]
    assert m["split_policy_hash"] == split_policy_hash()
    assert m["registry_snapshot_hash"] == registry_snapshot_hash(m["samples"])
    assert m["parent_manifest_hash"] is None
    assert m["parent_registry_snapshot_hash"] is None


def test_epoch1_is_deterministic() -> None:
    v1 = _v1()
    a = epoch1_from_v1_manifest(v1)
    b = epoch1_from_v1_manifest(v1)
    assert a == b


def test_committed_epoch1_artifact_matches_regeneration() -> None:
    """The checked-in artifact is byte-identical to a fresh canonical regeneration."""
    m = epoch1_from_v1_manifest(_v1())
    regenerated = canonical_json_str(m) + "\n"
    assert _V2_EPOCH1.read_text(encoding="utf-8") == regenerated


def test_committed_epoch1_manifest_hash_self_consistent() -> None:
    from minos_engine.common.hashing import canonical_hash

    stored = json.loads(_V2_EPOCH1.read_text(encoding="utf-8"))
    manifest_hash = stored.pop("manifest_hash")
    assert canonical_hash(stored) == manifest_hash


def test_duplicate_v1_identity_rejected() -> None:
    v1 = _v1()
    v1["samples"] = [*v1["samples"], dict(v1["samples"][0])]
    with pytest.raises(ContractValidationError):
        epoch1_from_v1_manifest(v1)


# --------------------------------------------------------------------------- #
# epoch ≥2 — grandfathered growth
# --------------------------------------------------------------------------- #
def test_epoch2_grandfathers_every_prior_assignment() -> None:
    m1 = epoch1_from_v1_manifest(_v1())
    m2 = build_next_epoch_manifest(m1, _new_samples(5, "e2"))
    p1 = {s["dataset_id"]: s["partition"] for s in m1["samples"]}
    p2 = {s["dataset_id"]: s["partition"] for s in m2["samples"]}
    assert all(p2[d] == p1[d] for d in p1)  # no prior sample moved
    assert m2["transition_count"] == 0
    assert m2["epoch"] == 2
    assert m2["parent_epoch"] == 1
    assert m2["parent_manifest_hash"] == m1["manifest_hash"]
    assert m2["parent_registry_snapshot_hash"] == m1["registry_snapshot_hash"]
    assert m2["inherited_count"] == 75
    assert m2["new_count"] == 25


def test_epoch2_test_set_monotonic() -> None:
    m1 = epoch1_from_v1_manifest(_v1())
    m2 = build_next_epoch_manifest(m1, _new_samples(5, "e2"))
    t1 = {s["dataset_id"] for s in m1["samples"] if s["partition"] == "test"}
    t2 = {s["dataset_id"] for s in m2["samples"] if s["partition"] == "test"}
    assert t1 <= t2


def test_epoch2_new_samples_marked_v2_policy() -> None:
    m1 = epoch1_from_v1_manifest(_v1())
    m2 = build_next_epoch_manifest(m1, _new_samples(5, "e2"))
    new = [s for s in m2["samples"] if s["assignment_source"] == "v2-policy"]
    assert len(new) == 25
    assert all(s["origin_epoch"] == 2 for s in new)


def test_epoch2_colliding_new_sample_rejected() -> None:
    m1 = epoch1_from_v1_manifest(_v1())
    collide = dict(m1["samples"][0])  # same identity as a parent sample
    with pytest.raises(ContractValidationError):
        build_next_epoch_manifest(m1, [collide])


def test_registry_snapshot_hash_grows_with_corpus() -> None:
    m1 = epoch1_from_v1_manifest(_v1())
    m2 = build_next_epoch_manifest(m1, _new_samples(5, "e2"))
    assert m1["registry_snapshot_hash"] != m2["registry_snapshot_hash"]
    assert m2["ancestor_v1_dataset_registry_hash"] == m1["ancestor_v1_dataset_registry_hash"]
