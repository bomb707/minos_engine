"""L2-C v2 epoch-split persistence — SQLAlchemy-bound writes for the epoch registry.

Persists a pure v2 epoch manifest (built by ``layer2.split_v2.generator``) into the
``catalog.split_snapshots`` + ``catalog.split_epoch_allocations`` tables created by
migration ``0003_l2c_split_v2_epochs``. Persistence lives in the ``storage`` package (not
``layer2``) so the Layer 2 split modules stay free of database imports.

Fail-closed before any write:
  * the **complete verifier** runs first — ``verify_epoch_manifest`` (with the v1 manifest
    for epoch 1) and, for epoch ≥2, ``verify_epoch_against_parent`` against the parent
    reconstructed from the database — and any failure aborts the insert;
  * each sample is matched to its registry row on **all four** identity columns
    (``dataset_id``, ``round_id``, ``chromosome``, ``identity_tuple_hash``), not the tuple
    hash alone;
  * epoch ≥2 resolves and binds a real ``parent_snapshot_id``.

Rows are append-only (migration triggers) and an epoch is written exactly once
(``UNIQUE(epoch)`` / ``UNIQUE(manifest_hash)`` / ``UNIQUE(registry_snapshot_hash)``).
Persistence runs as the schema owner (``minos_admin``).
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Connection

from minos_engine.common.errors import ContractValidationError
from minos_engine.layer2.split_v2.verifier import verify_epoch_manifest

__all__ = ["persist_epoch", "load_prior_assignments", "load_epoch_manifest"]


def load_prior_assignments(conn: Connection, epoch: int) -> dict[str, str]:
    """Load ``round_id -> partition`` for the given epoch from the DB (for grandfathering)."""
    rows = conn.execute(
        text(
            "SELECT dr.round_id AS round_id, ea.partition AS partition "
            "FROM catalog.split_epoch_allocations ea "
            "JOIN catalog.split_snapshots ss ON ss.id = ea.snapshot_id "
            "JOIN catalog.dataset_registry dr ON dr.id = ea.dataset_registry_id "
            "WHERE ss.epoch = :e"
        ),
        {"e": epoch},
    ).mappings()
    return {r["round_id"]: r["partition"] for r in rows}


def load_epoch_manifest(conn: Connection, epoch: int) -> dict[str, Any] | None:
    """Reconstruct the manifest view of a persisted epoch (for parent verification)."""
    snap = (
        conn.execute(
            text(
                "SELECT epoch, manifest_hash, registry_snapshot_hash, "
                " ancestor_v1_dataset_registry_hash, parent_manifest_hash, "
                " parent_registry_snapshot_hash, parent_epoch, transition_count "
                "FROM catalog.split_snapshots WHERE epoch = :e"
            ),
            {"e": epoch},
        )
        .mappings()
        .first()
    )
    if snap is None:
        return None
    rows = conn.execute(
        text(
            "SELECT dr.dataset_id, dr.round_id, dr.chromosome, dr.identity_tuple_hash, "
            " ea.partition, ea.origin_epoch, ea.assignment_source "
            "FROM catalog.split_epoch_allocations ea "
            "JOIN catalog.split_snapshots ss ON ss.id = ea.snapshot_id "
            "JOIN catalog.dataset_registry dr ON dr.id = ea.dataset_registry_id "
            "WHERE ss.epoch = :e"
        ),
        {"e": epoch},
    ).mappings()
    return {
        "epoch": snap["epoch"],
        "manifest_hash": snap["manifest_hash"],
        "registry_snapshot_hash": snap["registry_snapshot_hash"],
        "ancestor_v1_dataset_registry_hash": snap["ancestor_v1_dataset_registry_hash"],
        "parent_manifest_hash": snap["parent_manifest_hash"],
        "parent_registry_snapshot_hash": snap["parent_registry_snapshot_hash"],
        "parent_epoch": snap["parent_epoch"],
        "transition_count": snap["transition_count"],
        "samples": [dict(r) for r in rows],
    }


def persist_epoch(
    conn: Connection,
    manifest: dict[str, Any],
    *,
    v1_manifest: dict[str, Any] | None = None,
) -> str:
    """Persist a verified epoch manifest. Returns the new snapshot id.

    The complete verifier runs before any insert; for epoch ≥2 the parent snapshot is
    loaded from the DB and grandfathering immutability is re-proven. Each sample is matched
    to its registry row on all four identity columns.
    """
    epoch = int(manifest["epoch"])

    # --- 1. full verification before any write ---------------------------------------
    parent_manifest = None
    parent_snapshot_id: str | None = None
    if epoch >= 2:
        parent_manifest = load_epoch_manifest(conn, epoch - 1)
        if parent_manifest is None:
            raise ContractValidationError(f"parent epoch {epoch - 1} not persisted")
        parent_snapshot_id = conn.execute(
            text("SELECT id FROM catalog.split_snapshots WHERE epoch = :e"), {"e": epoch - 1}
        ).scalar()
    result = verify_epoch_manifest(
        manifest, v1_manifest=v1_manifest, parent_manifest=parent_manifest
    )
    if not result.ok:
        raise ContractValidationError(f"epoch manifest failed verification: {result.reasons}")

    counts = manifest["counts"]
    snapshot_id = conn.execute(
        text(
            "INSERT INTO catalog.split_snapshots "
            "(epoch, salt, split_policy_version, policy_hash, manifest_hash, "
            " registry_snapshot_hash, ancestor_v1_dataset_registry_hash, "
            " parent_registry_snapshot_hash, parent_manifest_hash, "
            " parent_snapshot_id, parent_epoch, transition_count, sample_count, "
            " count_train, count_validation, count_test) "
            "VALUES (:epoch, :salt, :spv, :ph, :mh, :rsh, :anc, :prsh, :pmh, :psid, :pe, :tc, "
            " :sc, :ct, :cv, :cte) RETURNING id"
        ),
        {
            "epoch": epoch,
            "salt": manifest["salt"],
            "spv": manifest["split_policy_version"],
            "ph": manifest["split_policy_hash"],
            "mh": manifest["manifest_hash"],
            "rsh": manifest["registry_snapshot_hash"],
            "anc": manifest["ancestor_v1_dataset_registry_hash"],
            "prsh": manifest["parent_registry_snapshot_hash"],
            "pmh": manifest["parent_manifest_hash"],
            "psid": parent_snapshot_id,
            "pe": manifest["parent_epoch"],
            "tc": manifest["transition_count"],
            "sc": len(manifest["samples"]),
            "ct": counts["train"],
            "cv": counts["validation"],
            "cte": counts["test"],
        },
    ).scalar_one()

    for rec in manifest["samples"]:
        reg_id = conn.execute(
            text(
                "SELECT id FROM catalog.dataset_registry "
                "WHERE dataset_id = :d AND round_id = :r AND chromosome = :c "
                "AND identity_tuple_hash = :h"
            ),
            {
                "d": rec["dataset_id"],
                "r": rec["round_id"],
                "c": rec["chromosome"],
                "h": rec["identity_tuple_hash"],
            },
        ).scalar()
        if reg_id is None:
            raise ContractValidationError(
                f"no registry row matching full identity for dataset {rec['dataset_id']}"
            )
        conn.execute(
            text(
                "INSERT INTO catalog.split_epoch_allocations "
                "(snapshot_id, dataset_registry_id, partition, origin_epoch, assignment_source) "
                "VALUES (:s, :d, :p, :oe, :src)"
            ),
            {
                "s": snapshot_id,
                "d": reg_id,
                "p": rec["partition"],
                "oe": rec["origin_epoch"],
                "src": rec["assignment_source"],
            },
        )
    return str(snapshot_id)
