"""Seed a scratch 0006 database with the exact upstream rows an ExperimentPlan references.

Given any ``ExperimentPlan`` (accepted or synthetic), inserts the profile snapshot, feature
set, train feature matrix, and per-member dataset_registry / bam_profile / snapshot-member /
matrix-member rows keyed to the plan's identities, so the F3-C1 persistence layer can resolve
every upstream UUID by complete identity. Deterministic; runs as ``minos_admin``.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import Connection, text

from tests.integration.layer2_db.l2f_seed import H, U, _bam_row, _dataset_row, _insert

_OTHER_MEDIA = "application/octet-stream"


def _plan_dataset_row(dataset_id: str, idx: int, dsr_id: str) -> dict[str, Any]:
    row = _dataset_row(dataset_id, idx)
    row["id"] = dsr_id
    row["dataset_id"] = dataset_id  # exact business key (no "ds-" prefix)
    return row


def seed_upstream_for_plan(conn: Connection, plan: Any) -> None:
    conn.execute(text("SET ROLE minos_admin"))
    tag = plan.plan_hash
    agen = U(f"art:gen:{tag}")
    _insert(
        conn,
        "catalog",
        "artifacts",
        {
            "id": agen,
            "uri": f"mem://gen/{tag}",
            "sha256": H(f"artgen:{tag}"),
            "media_type": _OTHER_MEDIA,
        },
    )
    fs = U(f"fs:{plan.feature_set_hash}:{plan.feature_registry_hash}")
    _insert(
        conn,
        "profiling",
        "feature_sets",
        {
            "id": fs,
            "feature_set_hash": plan.feature_set_hash,
            "registry_hash": plan.feature_registry_hash,
            "column_count": 3,
            "column_manifest": "[]",
        },
        jsonb_cols=("column_manifest",),
    )
    ss = U(f"ss:{tag}")
    _insert(
        conn,
        "catalog",
        "split_snapshots",
        {
            "id": ss,
            "epoch": 1,
            "salt": "salt",
            "split_policy_version": "v1",
            "policy_hash": H(f"policy:{tag}"),
            "manifest_hash": H(f"ssman:{tag}"),
            "registry_snapshot_hash": H(f"ssreg:{tag}"),
            "ancestor_v1_dataset_registry_hash": H(f"anc:{tag}"),
            "parent_registry_snapshot_hash": None,
            "parent_manifest_hash": None,
            "parent_snapshot_id": None,
            "parent_epoch": None,
            "transition_count": 0,
            "sample_count": 3,
            "count_train": 1,
            "count_validation": 1,
            "count_test": 1,
        },
    )
    snap = U(f"snap:{plan.snapshot_hash}")
    _insert(
        conn,
        "profiling",
        "profile_snapshots",
        {
            "id": snap,
            "epoch": plan.epoch,
            "split_snapshot_id": ss,
            "split_manifest_hash": plan.split_manifest_hash,
            "registry_snapshot_hash": plan.registry_snapshot_hash,
            "member_count": max(1, len(plan.members)),
            "snapshot_hash": plan.snapshot_hash,
        },
    )
    mat = U(f"mat:{plan.train_matrix_hash}")
    _insert(
        conn,
        "profiling",
        "feature_matrices",
        {
            "id": mat,
            "profile_snapshot_id": snap,
            "partition": "train",
            "feature_set_id": fs,
            "matrix_hash": plan.train_matrix_hash,
            "artifact_sha256": H(f"matart:{tag}"),
            "matrix_artifact_id": agen,
            "row_count": len(plan.members),
            "column_count": 3,
        },
    )
    for m in plan.members:
        dsr = U(f"dsr:{m.dataset_id}")
        _insert(
            conn,
            "catalog",
            "dataset_registry",
            _plan_dataset_row(m.dataset_id, m.member_index, dsr),
        )
        bam_row = _bam_row(m.dataset_id, dsr, agen)
        _insert(conn, "profiling", "bam_profiles", bam_row, jsonb_cols=("profile_document",))
        _insert(
            conn,
            "profiling",
            "profile_snapshot_members",
            {
                "id": U(f"psm:{plan.snapshot_hash}:{m.dataset_id}"),
                "profile_snapshot_id": snap,
                "bam_profile_id": bam_row["id"],
                "dataset_registry_id": dsr,
                "partition": "train",
                "feature_values_hash": m.feature_values_hash,
            },
        )
        _insert(
            conn,
            "profiling",
            "feature_matrix_members",
            {
                "id": U(f"fmm:{plan.train_matrix_hash}:{m.dataset_id}"),
                "feature_matrix_id": mat,
                "dataset_registry_id": dsr,
                "member_index": m.member_index,
                "vector_hash": m.vector_hash,
                "feature_values_hash": m.feature_values_hash,
            },
        )
    conn.execute(text("RESET ROLE"))
