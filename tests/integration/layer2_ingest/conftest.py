"""Real PostgreSQL 16 + real Layer 1 artifacts for L2-D ingestion integration tests.

Builds ONE genuine synthetic dataset (pysam BAM/FASTA on chr18), runs the frozen Layer 1
service to produce the real 3-artifact set (profile JSON + manifest JSON + windows
parquet), registers the identity with the REAL file hashes, persists a 1-sample split
epoch, and produces a REAL intake attestation. Ingestion is therefore tested end-to-end
with authentic COMPLETE profiles — no mocked documents.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Engine, create_engine, text

from minos_engine.common.hashing import canonical_hash, sha256_hex
from minos_engine.storage.database import create_db_engine, normalize_database_url
from tests.integration.layer2_db.conftest import alembic_upgrade, scratch_database
from tests.integration.layer2_split.conftest import pg_base_url  # noqa: F401  (session fixture)

CONTIG = "chr18"
DATASET_ID = "minos-chr18-00000000000000aa"
ROUND_ID = "00000000000000aa"
CONTIG_B = "chr19"
DATASET_ID_B = "minos-chr19-00000000000000bb"
ROUND_ID_B = "00000000000000bb"
_H = "9" * 64  # placeholder identities not derived from files (parameter space etc.)


@pytest.fixture(scope="session")
def l2d_artifacts(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    """Real Layer 1 artifacts for one synthetic chr18 dataset (full-contig region)."""
    from minos_engine.layer1.config import load_layer1_config
    from minos_engine.layer1.contracts import ProfileRequest, ProfileStatus
    from minos_engine.layer1.service import Layer1Service
    from tests.layer1_fixtures import build_dataset, simple_reads

    tmp = tmp_path_factory.mktemp("l2d_corpus")
    contig_len = 300_000  # >= 3 primary windows (100kbp)
    ds = build_dataset(
        tmp, simple_reads(contig_len, n_pairs=40), contig=CONTIG, contig_len=contig_len
    )
    cfg = load_layer1_config()
    request = ProfileRequest(
        round_id=ROUND_ID,
        bam_path=str(ds.bam),
        bai_path=str(ds.bai),
        reference_path=str(ds.reference),
        fai_path=str(ds.fai),
        region_source=f"{CONTIG}:1-{contig_len}",
        region_coordinate_convention="one_based_inclusive",
        budget_seconds=120,
        cpu_limit=1,
        memory_limit_bytes=1_000_000_000,
        profiler_config_version=cfg.profiler_config_version,
        profiler_config_hash=cfg.config_hash,
    )
    out = tmp / "artifacts"
    result = Layer1Service(require_prerequisite=False).analyze(request, out)
    assert result.status is ProfileStatus.COMPLETE, result
    profile_path = Path(result.profile_path)
    manifest_path = Path(result.manifest_path)
    windows_path = Path(result.windows_path)
    # second identity (chr19) for multi-identity / conflict / freeze-coverage tests
    ds_b = build_dataset(
        tmp / "b", simple_reads(contig_len, n_pairs=40), contig=CONTIG_B, contig_len=contig_len
    )
    request_b = request.model_copy(
        update={
            "round_id": ROUND_ID_B,
            "bam_path": str(ds_b.bam),
            "bai_path": str(ds_b.bai),
            "reference_path": str(ds_b.reference),
            "fai_path": str(ds_b.fai),
            "region_source": f"{CONTIG_B}:1-{contig_len}",
        }
    )
    result_b = Layer1Service(require_prerequisite=False).analyze(request_b, tmp / "artifacts_b")
    assert result_b.status is ProfileStatus.COMPLETE, result_b
    return {
        "b": {
            "bam": ds_b.bam,
            "bai": ds_b.bai,
            "reference": ds_b.reference,
            "fai": ds_b.fai,
            "length": contig_len,
            "profile_path": Path(result_b.profile_path),
            "manifest_path": Path(result_b.manifest_path),
            "windows_path": Path(result_b.windows_path),
            "bam_sha256": sha256_hex(ds_b.bam.read_bytes()),
            "bai_sha256": sha256_hex(ds_b.bai.read_bytes()),
            "reference_sha256": sha256_hex(ds_b.reference.read_bytes()),
            "fai_sha256": sha256_hex(ds_b.fai.read_bytes()),
        },
        "bam": ds.bam,
        "bai": ds.bai,
        "reference": ds.reference,
        "fai": ds.fai,
        "length": contig_len,
        "profile_path": profile_path,
        "manifest_path": manifest_path,
        "windows_path": windows_path,
        "profile_document": json.loads(profile_path.read_text(encoding="utf-8")),
        "manifest_document": json.loads(manifest_path.read_text(encoding="utf-8")),
        "profile_sha256": sha256_hex(profile_path.read_bytes()),
        "bam_sha256": sha256_hex(ds.bam.read_bytes()),
        "bai_sha256": sha256_hex(ds.bai.read_bytes()),
        "reference_sha256": sha256_hex(ds.reference.read_bytes()),
        "fai_sha256": sha256_hex(ds.fai.read_bytes()),
    }


def registry_record(
    art: dict[str, Any],
    *,
    contig: str = CONTIG,
    dataset_id: str = DATASET_ID,
    round_id: str = ROUND_ID,
) -> dict[str, Any]:
    """The registered identity record (real file hashes + canonical region/tuple)."""
    from minos_engine.layer2.split.contracts import region_hash_for

    region_hash = region_hash_for(contig, 0, art["length"])
    identity_tuple_hash = canonical_hash(
        {
            "bam_sha256": art["bam_sha256"],
            "bai_sha256": art["bai_sha256"],
            "reference_sha256": art["reference_sha256"],
            "fai_sha256": art["fai_sha256"],
            "region_hash": region_hash,
        }
    )
    return {
        "dataset_id": dataset_id,
        "round_id": round_id,
        "chromosome": contig,
        "bam_sha256": art["bam_sha256"],
        "bai_sha256": art["bai_sha256"],
        "reference_sha256": art["reference_sha256"],
        "fai_sha256": art["fai_sha256"],
        "region_start0": 0,
        "region_end0_exclusive": art["length"],
        "region_hash": region_hash,
        "identity_tuple_hash": identity_tuple_hash,
    }


def seed_registry_and_epoch(url: str, art: dict[str, Any]) -> None:
    """Register BOTH identities + the 2-sample epoch-1 split (owner connection)."""
    records = [
        registry_record(art),
        registry_record(art["b"], contig=CONTIG_B, dataset_id=DATASET_ID_B, round_id=ROUND_ID_B),
    ]
    eng = create_engine(normalize_database_url(url))
    try:
        with eng.begin() as c:
            snap_id = c.execute(
                text(
                    "INSERT INTO catalog.split_snapshots (epoch, salt, split_policy_version,"
                    " policy_hash, manifest_hash, registry_snapshot_hash,"
                    " ancestor_v1_dataset_registry_hash, transition_count, sample_count,"
                    " count_train, count_validation, count_test) VALUES"
                    " (1, 's', 'v2', :h, :mh, :rsh, :h, 0, 2, 2, 0, 0) RETURNING id"
                ),
                {"h": _H, "mh": "8" * 64, "rsh": "7" * 64},
            ).scalar_one()
            for rec in records:
                reg_id = c.execute(
                    text(
                        "INSERT INTO catalog.dataset_registry (dataset_id, round_id, chromosome,"
                        " region_source, region_start0, region_end0_exclusive, region_length_bp,"
                        " region_coordinate_system, region_hash, bam_sha256, bai_sha256,"
                        " reference_sha256, fai_sha256, bam_size_bytes, parameter_space_hash,"
                        " feature_registry_hash, identity_tuple_hash, manifest_hash,"
                        " split_algorithm_version, split_salt, allocation_digest) VALUES"
                        " (:d, :r, :c, :src, 0, :end, :end, 'zero_based_half_open', :rh, :bam,"
                        "  :bai, :ref, :fai, :sz, :h, :h, :ith, :h, 'v2', 's', :h)"
                        " RETURNING id"
                    ),
                    {
                        "d": rec["dataset_id"],
                        "r": rec["round_id"],
                        "c": rec["chromosome"],
                        "src": f"{rec['chromosome']}:0-{art['length']}",
                        "end": art["length"],
                        "rh": rec["region_hash"],
                        "bam": rec["bam_sha256"],
                        "bai": rec["bai_sha256"],
                        "ref": rec["reference_sha256"],
                        "fai": rec["fai_sha256"],
                        "sz": 1,
                        "h": _H,
                        "ith": rec["identity_tuple_hash"],
                    },
                ).scalar_one()
                c.execute(
                    text(
                        "INSERT INTO catalog.split_epoch_allocations (snapshot_id,"
                        " dataset_registry_id, partition, origin_epoch, assignment_source)"
                        " VALUES (:s, :d, 'train', 1, 'v1-inherited')"
                    ),
                    {"s": snap_id, "d": reg_id},
                )
    finally:
        eng.dispose()


def build_attestations(art: dict[str, Any]) -> dict[str, Any]:
    from minos_engine.intake.attestation import attest_input

    a = attest_input(
        bam_path=art["bam"],
        bai_path=art["bai"],
        reference_path=art["reference"],
        fai_path=art["fai"],
        registry_record=registry_record(art),
        registry_snapshot_hash="7" * 64,
    )
    b = attest_input(
        bam_path=art["b"]["bam"],
        bai_path=art["b"]["bai"],
        reference_path=art["b"]["reference"],
        fai_path=art["b"]["fai"],
        registry_record=registry_record(
            art["b"], contig=CONTIG_B, dataset_id=DATASET_ID_B, round_id=ROUND_ID_B
        ),
        registry_snapshot_hash="7" * 64,
    )
    return {"attestation": a, "attestation_b": b}


@pytest.fixture(scope="session")
def l2d_env(pg_base_url: str, l2d_artifacts: dict[str, Any]) -> Iterator[dict[str, Any]]:  # noqa: F811
    with scratch_database(pg_base_url, "minos_l2d_main") as url:
        alembic_upgrade(url, "head")
        seed_registry_and_epoch(url, l2d_artifacts)
        yield {"url": url, **build_attestations(l2d_artifacts), **l2d_artifacts}


@pytest.fixture(scope="session")
def l2d_engine(l2d_env: dict[str, Any]) -> Iterator[Engine]:
    eng = create_db_engine(l2d_env["url"])
    try:
        yield eng
    finally:
        eng.dispose()
