#!/usr/bin/env python
"""Build the epoch-1 profile corpus: L1 profile -> attest -> ingest for all 75 rounds.

Operational pipeline (resume-safe; skips completed work). Uses a PERSISTENT local
PostgreSQL 16 (pgserver) at DB_DIR as the operational store, migrated to head, with the
accepted v1 registry + v2 epoch-1 split persisted from the committed manifests. After all
epoch members are ingested it writes the explicit version-selection file and freezes
PROFILE-SNAPSHOT-FROZEN-1, emitting the snapshot evidence JSON for owner review.
"""

from __future__ import annotations

import json
import sys
import traceback
from pathlib import Path

import pgserver
from sqlalchemy import create_engine, text

ROOT = Path(__file__).resolve().parents[1]
DATASET_ROOT = Path("/home/hr/bittensor/minos_subnet/datasets")
DB_DIR = Path("/home/hr/bittensor/minos_l2d_db")
OUT_DIR = Path("/home/hr/bittensor/minos_l2d_corpus")
EPOCH = 1

sys.path.insert(0, str(ROOT / "src"))

from minos_engine.common.canonical_json import canonical_json_str  # noqa: E402
from minos_engine.intake.attestation import attest_input  # noqa: E402
from minos_engine.layer1.config import load_layer1_config  # noqa: E402
from minos_engine.layer1.contracts import ProfileRequest, ProfileStatus  # noqa: E402
from minos_engine.layer1.service import Layer1Service  # noqa: E402
from minos_engine.layer2.split.contracts import DatasetSplitManifest  # noqa: E402
from minos_engine.storage.database import normalize_database_url  # noqa: E402
from minos_engine.storage.dataset_split import persist_manifest  # noqa: E402
from minos_engine.storage.dataset_split_v2 import persist_epoch  # noqa: E402
from minos_engine.storage.profile_ingest import (  # noqa: E402
    freeze_profile_snapshot,
    ingest_profile,
)


def log(msg: str) -> None:
    print(msg, flush=True)


def db_engine():
    DB_DIR.mkdir(parents=True, exist_ok=True)
    server = pgserver.get_server(str(DB_DIR))
    url = server.get_uri()
    return create_engine(normalize_database_url(url)), server


def ensure_schema(engine) -> None:
    import subprocess

    with engine.connect() as c:
        have = c.execute(
            text("SELECT to_regclass('catalog.split_snapshots') IS NOT NULL")
        ).scalar()
    if not have:
        url = engine.url.render_as_string(hide_password=False)
        subprocess.run(
            [sys.executable, "-m", "alembic", "upgrade", "head"],
            cwd=ROOT,
            env={"MINOS_DATABASE_URL": url, "PATH": "/usr/bin:/bin"},
            check=True,
        )
        log("schema: migrated to head")
    else:
        log("schema: already at head")


def ensure_split(engine) -> dict:
    v1 = json.loads((ROOT / "manifests/layer2_dataset_split_v1.json").read_text())
    v2 = json.loads((ROOT / "manifests/layer2_dataset_split_v2_epoch1.json").read_text())
    with engine.begin() as c:
        n = c.execute(text("SELECT count(*) FROM catalog.dataset_registry")).scalar()
        if n == 0:
            persist_manifest(c, DatasetSplitManifest.model_validate(v1))
            log("split: v1 registry persisted (75)")
        e = c.execute(text("SELECT count(*) FROM catalog.split_snapshots")).scalar()
        if e == 0:
            persist_epoch(c, v2, v1_manifest=v1)
            log("split: v2 epoch-1 persisted")
    return {"v1": v1, "v2": v2}


def round_paths(sample: dict) -> dict[str, Path]:
    rid, chrom = sample["round_id"], sample["chromosome"]
    return {
        "bam": DATASET_ROOT / f"practice/round_{rid}/input.bam",
        "bai": DATASET_ROOT / f"practice/round_{rid}/input.bam.bai",
        "reference": DATASET_ROOT / f"reference/{chrom}/{chrom}.fa",
        "fai": DATASET_ROOT / f"reference/{chrom}/{chrom}.fa.fai",
    }


def build_one(engine, v1s: dict, v2: dict, cfg, svc) -> str | None:
    """Profile+attest+ingest one round; returns dataset_id on success."""
    rid = v1s["round_id"]
    out = OUT_DIR / rid
    out.mkdir(parents=True, exist_ok=True)
    marker = out / "INGESTED"
    if marker.exists():
        return v1s["dataset_id"]
    paths = round_paths(v1s)
    profile_p = out / "bam-profile-v1.json"
    manifest_p = out / "profile-manifest-v1.json"
    windows_p = out / "window-profile-v1.parquet"
    if not (profile_p.exists() and manifest_p.exists() and windows_p.exists()):
        req = ProfileRequest(
            round_id=rid,
            bam_path=str(paths["bam"]),
            bai_path=str(paths["bai"]),
            reference_path=str(paths["reference"]),
            fai_path=str(paths["fai"]),
            region_source=(
                f"{v1s['chromosome']}:{v1s['region_start0'] + 1}-"
                f"{v1s['region_end0_exclusive']}"
            ),
            region_coordinate_convention="one_based_inclusive",
            budget_seconds=1800,
            cpu_limit=2,
            memory_limit_bytes=4_000_000_000,
            profiler_config_version=cfg.profiler_config_version,
            profiler_config_hash=cfg.config_hash,
        )
        result = svc.analyze(req, out)
        if result.status is not ProfileStatus.COMPLETE:
            log(f"FAILED {rid}: L1 status {result.status.value}")
            return None
        for src, dst in (
            (result.profile_path, profile_p),
            (result.manifest_path, manifest_p),
            (result.windows_path, windows_p),
        ):
            if Path(src) != dst:
                Path(dst).write_bytes(Path(src).read_bytes())
    att_p = out / "input-integrity-attestation-v1.json"
    if att_p.exists():
        att = json.loads(att_p.read_text())
    else:
        record = {
            **{k: v1s[k] for k in (
                "dataset_id", "round_id", "chromosome", "bam_sha256", "bai_sha256",
                "reference_sha256", "fai_sha256", "region_start0",
                "region_end0_exclusive", "region_hash", "identity_tuple_hash",
            )},
        }
        att = attest_input(
            bam_path=paths["bam"], bai_path=paths["bai"],
            reference_path=paths["reference"], fai_path=paths["fai"],
            registry_record=record,
            registry_snapshot_hash=v2["registry_snapshot_hash"],
        ).model_dump(mode="json")
        att_p.write_text(canonical_json_str(att) + "\n")
    outcome = ingest_profile(
        engine,
        epoch=EPOCH,
        profile_json_path=profile_p,
        manifest_json_path=manifest_p,
        windows_parquet_path=windows_p,
        attestation=att,
        profile_artifact_uri=profile_p.as_uri(),
        manifest_artifact_uri=manifest_p.as_uri(),
        windows_artifact_uri=windows_p.as_uri(),
    )
    marker.write_text(outcome.row_id)
    return v1s["dataset_id"]


def main() -> int:
    engine, _server = db_engine()
    ensure_schema(engine)
    manifests = ensure_split(engine)
    cfg = load_layer1_config()
    svc = Layer1Service(require_prerequisite=False)
    samples = manifests["v1"]["samples"]
    done: list[str] = []
    for i, s in enumerate(samples, 1):
        try:
            ds = build_one(engine, s, manifests["v2"], cfg, svc)
        except Exception as exc:  # noqa: BLE001
            log(f"FAILED {s['round_id']}: {type(exc).__name__}: {str(exc)[:200]}")
            traceback.print_exc()
            ds = None
        if ds:
            done.append(ds)
            log(f"OK {i}/75 {ds}")
    log(f"ingested: {len(done)}/75")
    if len(done) != 75:
        log("HOLD: corpus incomplete; freeze not attempted")
        return 1
    # explicit version selection: exactly one accepted version per identity at epoch 1.
    with engine.connect() as c:
        rows = c.execute(
            text(
                "SELECT dr.dataset_id, bp.content_hash FROM profiling.bam_profiles bp "
                "JOIN catalog.dataset_registry dr ON dr.id = bp.dataset_registry_id"
            )
        ).all()
    selections = {r[0]: r[1] for r in rows}
    sel_p = ROOT / "manifests/profile_snapshot_epoch1_selections.json"
    sel_p.write_text(canonical_json_str(selections) + "\n")
    with engine.begin() as c:
        snap_id = freeze_profile_snapshot(c, EPOCH, selections)
    with engine.connect() as c:
        snap = c.execute(
            text(
                "SELECT epoch, member_count, snapshot_hash, split_manifest_hash, "
                "registry_snapshot_hash FROM profiling.profile_snapshots WHERE id = :i"
            ),
            {"i": snap_id},
        ).mappings().one()
    evidence = dict(snap)
    (ROOT / "reports").mkdir(exist_ok=True)
    (ROOT / "reports/PROFILE_SNAPSHOT_FROZEN_1.json").write_text(
        canonical_json_str(evidence) + "\n"
    )
    log(f"FROZEN epoch 1: {evidence}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
