"""L2-C v2 epoch-2 growth on real PG16: grandfathering, new-only assignment, rejection.

Uses its own scratch database (not the session epoch-1 fixture) because it persists a
second epoch; proves on the real store that parent allocations are immutable, only new
samples are policy-assigned, and removal/replacement attempts fail closed.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterator

import pytest
from sqlalchemy import Engine, create_engine, text

from minos_engine.common.errors import ContractValidationError
from minos_engine.layer2.split_v2.generator import build_next_epoch_manifest
from minos_engine.layer2.split_v2.policy import SUPPORTED_CHROMOSOMES
from minos_engine.storage.database import normalize_database_url
from minos_engine.storage.dataset_split import persist_manifest
from minos_engine.storage.dataset_split_v2 import persist_epoch
from tests.integration.layer2_db.conftest import alembic_upgrade, scratch_database
from tests.integration.layer2_split_v2.conftest import (
    synthetic_epoch1_manifest,
    synthetic_v1_manifest,
)

_CONTIG_LEN = {
    "chr18": 80373285,
    "chr19": 58617616,
    "chr20": 64444167,
    "chr21": 46709983,
    "chr22": 50818468,
}


def _h(*parts: str) -> str:
    return hashlib.sha256(":".join(parts).encode()).hexdigest()


def _new_samples(per_chrom: int) -> list[dict]:
    out = []
    for ci, c in enumerate(SUPPORTED_CHROMOSOMES):
        for i in range(per_chrom):
            rid = f"e2{ci:x}{i:012x}"  # 16 hex chars, disjoint from synthetic e1 rounds
            out.append(
                {
                    "dataset_id": f"minos-{c}-{rid}",
                    "round_id": rid,
                    "chromosome": c,
                    "identity_tuple_hash": _h("identity", rid, c),
                }
            )
    return out


def _register_new_rounds(conn, samples: list[dict]) -> None:
    """Append the new rounds' full identity rows to catalog.dataset_registry."""
    from minos_engine.layer2.split.contracts import region_hash_for

    for s in samples:
        c = s["chromosome"]
        length = _CONTIG_LEN[c]
        conn.execute(
            text(
                "INSERT INTO catalog.dataset_registry "
                "(dataset_id, round_id, chromosome, region_source, region_start0, "
                " region_end0_exclusive, region_length_bp, region_coordinate_system, "
                " region_hash, bam_sha256, bai_sha256, reference_sha256, fai_sha256, "
                " bam_size_bytes, parameter_space_hash, feature_registry_hash, "
                " identity_tuple_hash, manifest_hash, split_algorithm_version, split_salt, "
                " allocation_digest) VALUES "
                "(:d, :r, :c, :src, 0, :end, :end, 'zero_based_half_open', :rh, :bam, :bai, "
                " :ref, :fai, 1000000, :ps, :fr, :ith, :mh, 'layer2-dataset-split-v2', "
                " 'minos-l2-split-v2', :ad)"
            ),
            {
                "d": s["dataset_id"],
                "r": s["round_id"],
                "c": c,
                "src": f"{c}:0-{length}",
                "end": length,
                "rh": region_hash_for(c, 0, length),
                "bam": _h("bam", s["round_id"]),
                "bai": _h("bai", s["round_id"]),
                "ref": _h("ref", c),
                "fai": _h("fai", c),
                "ps": _h("ps"),
                "fr": _h("fr"),
                "ith": s["identity_tuple_hash"],
                "mh": _h("mh", s["round_id"]),
                "ad": _h("ad", s["round_id"]),
            },
        )


@pytest.fixture(scope="module")
def growth_engine(pg_base_url: str) -> Iterator[Engine]:
    """Scratch DB with synthetic registry + epoch 1 + 25 new registered rounds + epoch 2."""
    from tests.layer2c_synth import synthetic_manifest

    with scratch_database(pg_base_url, "minos_l2c_v2_growth") as url:
        alembic_upgrade(url, "head")
        eng = create_engine(normalize_database_url(url))
        try:
            new = _new_samples(5)
            m1 = synthetic_epoch1_manifest()
            m2 = build_next_epoch_manifest(m1, new)
            with eng.begin() as conn:
                persist_manifest(conn, synthetic_manifest())
                persist_epoch(conn, m1, v1_manifest=synthetic_v1_manifest())
                _register_new_rounds(conn, new)
                persist_epoch(conn, m2)
            yield eng
        finally:
            eng.dispose()


def test_parent_allocations_immutable(growth_engine: Engine) -> None:
    """Every epoch-1 identity keeps the exact same partition in epoch 2."""
    with growth_engine.connect() as c:
        rows = c.execute(
            text(
                "SELECT dr.dataset_id, ss.epoch, ea.partition "
                "FROM catalog.split_epoch_allocations ea "
                "JOIN catalog.split_snapshots ss ON ss.id = ea.snapshot_id "
                "JOIN catalog.dataset_registry dr ON dr.id = ea.dataset_registry_id"
            )
        ).all()
    by_epoch: dict[int, dict[str, str]] = {}
    for did, epoch, part in rows:
        by_epoch.setdefault(int(epoch), {})[did] = part
    e1, e2 = by_epoch[1], by_epoch[2]
    assert len(e1) == 75 and len(e2) == 100
    assert all(e2[d] == e1[d] for d in e1)  # zero movement


def test_growth_assigns_only_new(growth_engine: Engine) -> None:
    """The 25 epoch-2 additions are all v2-policy @ origin 2; inherited rows unchanged."""
    with growth_engine.connect() as c:
        rows = c.execute(
            text(
                "SELECT ea.assignment_source, ea.origin_epoch, count(*) "
                "FROM catalog.split_epoch_allocations ea "
                "JOIN catalog.split_snapshots ss ON ss.id = ea.snapshot_id "
                "WHERE ss.epoch = 2 GROUP BY 1, 2 ORDER BY 2"
            )
        ).all()
    assert [(s, int(o), int(n)) for s, o, n in rows] == [
        ("v1-inherited", 1, 75),
        ("v2-policy", 2, 25),
    ]


def test_epoch2_snapshot_binds_parent_fk(growth_engine: Engine) -> None:
    with growth_engine.connect() as c:
        row = c.execute(
            text(
                "SELECT child.epoch, parent.epoch "
                "FROM catalog.split_snapshots child "
                "JOIN catalog.split_snapshots parent ON parent.id = child.parent_snapshot_id "
                "WHERE child.epoch = 2"
            )
        ).one()
    assert tuple(row) == (2, 1)


def test_test_set_monotonic_in_store(growth_engine: Engine) -> None:
    with growth_engine.connect() as c:
        rows = c.execute(
            text(
                "SELECT ss.epoch, dr.dataset_id FROM catalog.split_epoch_allocations ea "
                "JOIN catalog.split_snapshots ss ON ss.id = ea.snapshot_id "
                "JOIN catalog.dataset_registry dr ON dr.id = ea.dataset_registry_id "
                "WHERE ea.partition = 'test'"
            )
        ).all()
    test1 = {d for e, d in rows if int(e) == 1}
    test2 = {d for e, d in rows if int(e) == 2}
    assert test1 <= test2


def test_removal_and_replacement_rejected(growth_engine: Engine) -> None:
    """An epoch-3 manifest that removes or re-identifies a prior sample fails closed."""
    from minos_engine.storage.dataset_split_v2 import load_epoch_manifest

    with growth_engine.connect() as c:
        m2 = load_epoch_manifest(c, 2)
    assert m2 is not None

    # removal: epoch 3 missing one epoch-2 sample
    removed = dict(m2)
    removed_samples = list(m2["samples"])[1:]
    removed = {
        "epoch": 3,
        "parent_epoch": 2,
        "parent_manifest_hash": m2["manifest_hash"],
        "parent_registry_snapshot_hash": m2["registry_snapshot_hash"],
        "transition_count": 0,
        "samples": removed_samples,
    }
    from minos_engine.layer2.split_v2.verifier import verify_epoch_against_parent

    checks = verify_epoch_against_parent(m2, removed)
    assert checks["no_parent_removed"] is False

    # replacement: same dataset_id, different identity tuple
    replaced_samples = [dict(s) for s in m2["samples"]]
    replaced_samples[0]["identity_tuple_hash"] = "e" * 64
    replaced = {**removed, "samples": replaced_samples}
    checks = verify_epoch_against_parent(m2, replaced)
    assert checks["parent_samples_immutable"] is False

    # and the persistence path rejects an unverifiable epoch outright
    bad = {
        **removed,
        "salt": "minos-l2-split-v2",
        "split_policy_version": "layer2-dataset-split-v2",
        "split_policy_hash": "a" * 64,
        "manifest_hash": "b" * 64,
        "registry_snapshot_hash": "c" * 64,
        "ancestor_v1_dataset_registry_hash": "d" * 64,
        "inherited_count": 0,
        "new_count": 0,
        "counts": {"train": 0, "validation": 0, "test": 0},
        "per_chromosome": {},
    }
    with growth_engine.begin() as c, pytest.raises(ContractValidationError):
        persist_epoch(c, bad)
