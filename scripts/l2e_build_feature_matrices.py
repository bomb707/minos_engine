#!/usr/bin/env python
"""Owner-run E4 operational feature-matrix builder (source-bound, resume-safe).

Materializes ONLY the accepted epoch-1 ``train`` and ``validation`` feature matrices into
the canonical operational database ``minos_engine_db`` and writes HASHES-ONLY metadata
manifests for owner review. It never materializes ``test``, never invents production
paths/groups, refuses the credential-proof fixture directory, and emits no plaintext
vectors, credentials, or retrievable artifact URIs.

Fail-closed / resume-safe:
  * connection comes only from ``MINOS_DATABASE_URL`` (or ``--database-url``); the
    canonical operational database is verified on the exact read + transaction
    connections by the accepted builder;
  * ``--train-root`` / ``--validation-root`` are REQUIRED and validated by the owner-side
    publisher (existence, non-symlink, exact ``0o2750``, writer ownership, disjoint,
    distinct gids); the credential-proof fixture ``/var/lib/minos/l2e_cred_proof`` is
    refused;
  * the build is idempotent — re-running against an already-materialized store replays
    and re-verifies the stored artifacts (``idempotent_replay: true``);
  * the run is source-bound: it refuses to proceed unless the Git worktree is clean and
    records the exact ``git_head`` / ``git_tree`` in every metadata manifest.

Owner-side provisioning (deliberate deployment step — NOT done by this script):

    sudo install -d -m 2750 -o <writer> -g minos_train_grp      <TRAIN_ROOT>/l2e/train
    sudo install -d -m 2750 -o <writer> -g minos_validation_grp <VALIDATION_ROOT>/l2e/validation

Run:

    MINOS_DATABASE_URL='postgresql://postgres@127.0.0.1:5433/minos_engine_db' \
    python scripts/l2e_build_feature_matrices.py \
        --train-root <TRAIN_ROOT>/l2e/train \
        --validation-root <VALIDATION_ROOT>/l2e/validation \
        --output-dir reports/e4
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO / "src"))

from minos_engine.storage.database import (  # noqa: E402
    database_url,
    normalize_database_url,
)
from minos_engine.storage.feature_matrix_production import (  # noqa: E402
    PRODUCTION_PARTITIONS,
    build_operational_feature_matrices,
    matrix_metadata,
)
from minos_engine.storage.matrix_access import PartitionArtifactPublisher  # noqa: E402

_ACCEPTED_MANIFEST = _REPO / "manifests" / "profile_snapshot_epoch1_members.json"


def _git(*args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(_REPO), *args], capture_output=True, text=True, check=True
    ).stdout.strip()


def _canonical(obj: object) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def log(msg: str) -> None:
    print(msg, flush=True)


def main() -> int:
    parser = argparse.ArgumentParser(description="E4 operational feature-matrix builder")
    parser.add_argument("--train-root", required=True, type=Path)
    parser.add_argument("--validation-root", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path, help="hashes-only metadata dir")
    parser.add_argument("--database-url", default=None, help="defaults to MINOS_DATABASE_URL")
    args = parser.parse_args()

    if _git("status", "--porcelain") != "":
        log("STATUS: REFUSED — Git worktree is not clean; cannot source-bind E4 evidence.")
        return 2

    from sqlalchemy import create_engine

    url = normalize_database_url(args.database_url) if args.database_url else database_url()
    engine = create_engine(url)
    manifest_bytes = _ACCEPTED_MANIFEST.read_bytes()

    git_head = _git("rev-parse", "HEAD")
    git_tree = _git("rev-parse", "HEAD^{tree}")

    # publisher for metadata (mode/gid) — validates the SAME roots the builder uses.
    publisher = PartitionArtifactPublisher(
        train_root=args.train_root, validation_root=args.validation_root
    )

    results = build_operational_feature_matrices(
        engine,
        member_manifest_bytes=manifest_bytes,
        train_root=args.train_root,
        validation_root=args.validation_root,
    )

    args.output_dir.mkdir(parents=True, exist_ok=True)
    for partition in PRODUCTION_PARTITIONS:
        persisted = results[partition]
        meta = matrix_metadata(engine, persisted, partition=partition, publisher=publisher)
        meta["git_head"] = git_head
        meta["git_tree"] = git_tree
        out = args.output_dir / f"L2E_E4_{partition.upper()}_MATRIX.json"
        out.write_text(json.dumps(meta, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        log(
            f"{partition}: rows={meta['row_count']} matrix_hash={str(meta['matrix_hash'])[:12]}… "
            f"artifact_sha256={str(meta['artifact_sha256'])[:12]}… "
            f"idempotent={meta['idempotent_replay']} -> {out}"
        )

    engine.dispose()
    log("STATUS: E4 train + validation materialization complete (hashes-only metadata written).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
