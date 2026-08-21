"""Seed a scratch 0006 database with the exact upstream rows an ExperimentPlan references.

Given any ``ExperimentPlan`` (accepted or synthetic), inserts the profile snapshot, feature
set, train feature matrix, and per-member dataset_registry / bam_profile / snapshot-member /
matrix-member rows keyed to the plan's **exact accepted identities**, so the F3-C1 persistence
layer can resolve every upstream UUID by complete identity. In particular the seeded
``bam_profile`` carries the plan member's own ``profile_id`` / ``content_hash`` /
``feature_values_hash`` (never fabricated values) and the matrix member carries the plan
member's ``vector_hash``. Deterministic; runs as ``minos_admin``.

For the identity-binding negative tests, ``corrupt=<field>`` deliberately breaks exactly one
upstream identity of member ``corrupt_index`` (default 0) — either by writing a wrong value
into the member's own resolved row, or (for a foreign-linkage field) by pointing the member at
a seeded decoy row — so the store must fail with a typed ``UpstreamIdentityError`` before any
payload publication. Every other row remains fully valid.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import Connection, text

from tests.integration.layer2_db.l2f_seed import H, U, _bam_row, _dataset_row, _insert

_OTHER_MEDIA = "application/octet-stream"

#: the identity fields for which an independent behavioral negative exists. ``bam_profile_status``
#: is intentionally absent: ``ck_bam_profiles_complete_only`` makes a non-COMPLETE bam_profile
#: row unconstructable at the database boundary, so the store's COMPLETE check cannot be
#: negatively exercised (it is belt-and-suspenders over a DB CHECK).
CORRUPTIONS = (
    "dataset_id",
    "snapshot_member_snapshot",
    "snapshot_member_partition",
    "snapshot_member_bam_pointer",
    "snapshot_member_feature_values_hash",
    "bam_dataset_registry_id",
    "bam_profile_id",
    "bam_content_hash",
    "bam_feature_values_hash",
    "matrix_member_matrix",
    "matrix_member_dataset_registry_id",
    "matrix_member_index",
    "matrix_member_feature_values_hash",
    "matrix_member_vector_hash",
)

#: set-level defects that break exact plan↔live train-set equality (the whole inventory, not one
#: field), used by the upstream-exact-set negatives.
SET_DEFECTS = (
    "extra_snapshot_member",
    "extra_matrix_member",
    "different_dataset_sets",
    "row_count_inconsistent",
    "member_index_mismatch",
    "missing_member",
)


def split_snapshot_id_for(plan: Any) -> str:
    """The deterministic ``catalog.split_snapshots`` id this seeder creates for ``plan``."""
    return U(f"ss:{plan.plan_hash}")


def _plan_dataset_row(dataset_id: str, idx: int, dsr_id: str) -> dict[str, Any]:
    row = _dataset_row(dataset_id, idx)
    row["id"] = dsr_id
    row["dataset_id"] = dataset_id  # exact business key (no "ds-" prefix)
    return row


def _decoy_dataset_registry(conn: Connection, tag: str) -> str:
    """A fully valid, unrelated dataset_registry row (for wrong-lineage negatives)."""
    did = U(f"decoy:dsr:{tag}")
    row = _dataset_row(f"decoy-{tag}", 3)
    row["id"] = did
    row["dataset_id"] = f"decoy-ds-{tag}"
    _insert(conn, "catalog", "dataset_registry", row)
    return did


def _decoy_bam(conn: Connection, tag: str, dsr_id: str, agen: str) -> str:
    """A fully valid bam_profile with identities that DO NOT match any plan member."""
    row = _bam_row(f"decoy-{tag}", dsr_id, agen)
    _insert(conn, "profiling", "bam_profiles", row, jsonb_cols=("profile_document",))
    return str(row["id"])


def _decoy_snapshot(conn: Connection, tag: str, parent_ss_id: str) -> str:
    """A valid, unrelated epoch-2 profile_snapshot (its split_snapshot carries a valid parent
    chain back to the plan's epoch-1 split snapshot; distinct epoch / hashes throughout)."""
    ss2 = U(f"decoy:ss:{tag}")
    _insert(
        conn,
        "catalog",
        "split_snapshots",
        {
            "id": ss2,
            "epoch": 2,
            "salt": "salt",
            "split_policy_version": "v1",
            "policy_hash": H(f"decoy:policy:{tag}"),
            "manifest_hash": H(f"decoy:ssman:{tag}"),
            "registry_snapshot_hash": H(f"decoy:ssreg:{tag}"),
            "ancestor_v1_dataset_registry_hash": H(f"decoy:anc:{tag}"),
            "parent_registry_snapshot_hash": H(f"ssreg:{tag}"),
            "parent_manifest_hash": H(f"ssman:{tag}"),
            "parent_snapshot_id": parent_ss_id,
            "parent_epoch": 1,
            "transition_count": 0,
            "sample_count": 3,
            "count_train": 1,
            "count_validation": 1,
            "count_test": 1,
        },
    )
    snap2 = U(f"decoy:snap:{tag}")
    _insert(
        conn,
        "profiling",
        "profile_snapshots",
        {
            "id": snap2,
            "epoch": 2,
            "split_snapshot_id": ss2,
            "split_manifest_hash": H(f"decoy:splitman:{tag}"),
            "registry_snapshot_hash": H(f"decoy:regsnap:{tag}"),
            "member_count": 1,
            "snapshot_hash": H(f"decoy:snaphash:{tag}"),
        },
    )
    return snap2


def _decoy_matrix(conn: Connection, tag: str, snap: str, fs: str, agen: str) -> str:
    """A valid feature_matrix on the same snapshot/feature-set but a DIFFERENT partition
    (so it does not collide with the real train matrix on the (snapshot, partition, feature_set)
    unique identity) and a distinct matrix_hash."""
    mid = U(f"decoy:mat:{tag}")
    _insert(
        conn,
        "profiling",
        "feature_matrices",
        {
            "id": mid,
            "profile_snapshot_id": snap,
            "partition": "validation",
            "feature_set_id": fs,
            "matrix_hash": H(f"decoy:matrix:{tag}"),
            "artifact_sha256": H(f"decoy:matart:{tag}"),
            "matrix_artifact_id": agen,
            "row_count": 1,
            "column_count": 3,
        },
    )
    return mid


def _seed_extra_train_dataset(
    conn: Connection,
    tag: str,
    label: str,
    agen: str,
    snap: str,
    mat: str,
    *,
    member_index: int,
    with_psm: bool,
    with_fmm: bool,
) -> None:
    """Seed a fully valid extra train dataset (dsr + bam) and optionally enrol it as an extra
    snapshot train member and/or an extra matrix member — used to inject set-level train
    inventory violations."""
    dsr = U(f"extra:dsr:{label}:{tag}")
    row = _dataset_row(f"extra-{label}-{tag}", 4)
    row["id"] = dsr
    row["dataset_id"] = f"extra-ds-{label}-{tag}"
    _insert(conn, "catalog", "dataset_registry", row)
    bam = _bam_row(f"extra-{label}-{tag}", dsr, agen)
    _insert(conn, "profiling", "bam_profiles", bam, jsonb_cols=("profile_document",))
    if with_psm:
        _insert(
            conn,
            "profiling",
            "profile_snapshot_members",
            {
                "id": U(f"extra:psm:{label}:{tag}"),
                "profile_snapshot_id": snap,
                "bam_profile_id": bam["id"],
                "dataset_registry_id": dsr,
                "partition": "train",
                "feature_values_hash": bam["feature_values_hash"],
            },
        )
    if with_fmm:
        _insert(
            conn,
            "profiling",
            "feature_matrix_members",
            {
                "id": U(f"extra:fmm:{label}:{tag}"),
                "feature_matrix_id": mat,
                "dataset_registry_id": dsr,
                "member_index": member_index,
                "vector_hash": H(f"extra:vec:{label}:{tag}"),
                "feature_values_hash": bam["feature_values_hash"],
            },
        )


def seed_upstream_for_plan(
    conn: Connection,
    plan: Any,
    *,
    corrupt: str | None = None,
    corrupt_index: int = 0,
    set_defect: str | None = None,
    variant: int = 0,
    parent_split_snapshot_id: str | None = None,
) -> None:
    """Seed one plan's upstream.

    ``variant`` lets several plans coexist in one database: ``catalog.split_snapshots.epoch`` and
    ``profiling.profile_snapshots.epoch`` are both globally UNIQUE, so a second plan must be
    seeded at ``variant=1`` (and so on), supplying ``parent_split_snapshot_id`` — the
    :func:`split_snapshot_id_for` of an already-seeded ``variant - 1`` plan — to satisfy the
    split-snapshot parent chain. ``variant=0`` reproduces the original behavior exactly. The
    ``profiling.feature_sets`` row is genuinely shared between plans built from the same accepted
    candidate set, so at ``variant > 0`` it is inserted idempotently.
    """
    if variant < 0:
        raise ValueError("variant must be >= 0")
    if variant > 0 and parent_split_snapshot_id is None:
        raise ValueError("variant > 0 requires parent_split_snapshot_id")
    if corrupt is not None and corrupt not in CORRUPTIONS:
        raise ValueError(f"unknown corruption {corrupt!r}")
    if set_defect is not None and set_defect not in SET_DEFECTS:
        raise ValueError(f"unknown set_defect {set_defect!r}")
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
    fs_row = {
        "id": fs,
        "feature_set_hash": plan.feature_set_hash,
        "registry_hash": plan.feature_registry_hash,
        "column_count": 3,
        "column_manifest": "[]",
    }
    if variant == 0:
        _insert(conn, "profiling", "feature_sets", fs_row, jsonb_cols=("column_manifest",))
    else:
        # identical row shared with the already-seeded plan(s): insert idempotently.
        conn.execute(
            text(
                "INSERT INTO profiling.feature_sets "
                "(id, feature_set_hash, registry_hash, column_count, column_manifest) "
                "VALUES (:id, :feature_set_hash, :registry_hash, :column_count, "
                "CAST(:column_manifest AS jsonb)) ON CONFLICT DO NOTHING"
            ),
            fs_row,
        )
    ss = U(f"ss:{tag}")
    _insert(
        conn,
        "catalog",
        "split_snapshots",
        {
            "id": ss,
            "epoch": 1 + variant,
            "salt": "salt",
            "split_policy_version": "v1",
            "policy_hash": H(f"policy:{tag}"),
            "manifest_hash": H(f"ssman:{tag}"),
            "registry_snapshot_hash": H(f"ssreg:{tag}"),
            "ancestor_v1_dataset_registry_hash": H(f"anc:{tag}"),
            "parent_registry_snapshot_hash": None if variant == 0 else H(f"pssreg:{tag}"),
            "parent_manifest_hash": None if variant == 0 else H(f"pssman:{tag}"),
            "parent_snapshot_id": parent_split_snapshot_id,
            "parent_epoch": None if variant == 0 else variant,
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
            "epoch": plan.epoch + variant,
            "split_snapshot_id": ss,
            "split_manifest_hash": plan.split_manifest_hash,
            "registry_snapshot_hash": plan.registry_snapshot_hash,
            "member_count": max(1, len(plan.members)),
            "snapshot_hash": plan.snapshot_hash,
        },
    )
    mat = U(f"mat:{plan.train_matrix_hash}")
    mat_row_count = len(plan.members)
    if set_defect == "row_count_inconsistent":
        mat_row_count = len(plan.members) + 1  # stored row_count disagrees with actual membership
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
            "row_count": mat_row_count,
            "column_count": 3,
        },
    )
    last = len(plan.members) - 1
    for i, m in enumerate(plan.members):
        do = corrupt if (corrupt is not None and i == corrupt_index) else None
        if set_defect == "missing_member" and i == last:
            continue  # one expected plan member has no upstream rows at all

        dsr = U(f"dsr:{m.dataset_id}")
        dsr_row = _plan_dataset_row(m.dataset_id, m.member_index, dsr)
        if do == "dataset_id":
            dsr_row["dataset_id"] = f"{m.dataset_id}::WRONG"
        _insert(conn, "catalog", "dataset_registry", dsr_row)

        # the bam_profile carries the plan member's EXACT accepted identities (never fabricated).
        bam_row = _bam_row(m.dataset_id, dsr, agen)
        bam_row["profile_id"] = m.profile_id
        bam_row["content_hash"] = m.content_hash
        bam_row["feature_values_hash"] = m.feature_values_hash
        if do == "bam_dataset_registry_id":
            bam_row["dataset_registry_id"] = _decoy_dataset_registry(conn, tag)
        if do == "bam_profile_id":
            bam_row["profile_id"] = f"WRONG-{m.profile_id}"
        if do == "bam_content_hash":
            bam_row["content_hash"] = H(f"wrong:content:{tag}")
        if do == "bam_feature_values_hash":
            bam_row["feature_values_hash"] = H(f"wrong:bam-fvh:{tag}")
        _insert(conn, "profiling", "bam_profiles", bam_row, jsonb_cols=("profile_document",))

        psm_snapshot = snap
        psm_bam = bam_row["id"]
        psm_partition = "train"
        psm_fvh = m.feature_values_hash
        if do == "snapshot_member_snapshot":
            psm_snapshot = _decoy_snapshot(conn, tag, ss)
        if do == "snapshot_member_bam_pointer":
            psm_bam = _decoy_bam(conn, tag, dsr, agen)
        if do == "snapshot_member_partition":
            psm_partition = "validation"
        if do == "snapshot_member_feature_values_hash":
            psm_fvh = H(f"wrong:sm-fvh:{tag}")
        _insert(
            conn,
            "profiling",
            "profile_snapshot_members",
            {
                "id": U(f"psm:{plan.snapshot_hash}:{m.dataset_id}"),
                "profile_snapshot_id": psm_snapshot,
                "bam_profile_id": psm_bam,
                "dataset_registry_id": dsr,
                "partition": psm_partition,
                "feature_values_hash": psm_fvh,
            },
        )

        fmm_matrix = mat
        fmm_dsr = dsr
        fmm_index = m.member_index
        fmm_fvh = m.feature_values_hash
        fmm_vec = m.vector_hash
        if do == "matrix_member_matrix":
            fmm_matrix = _decoy_matrix(conn, tag, snap, fs, agen)
        if do == "matrix_member_dataset_registry_id":
            fmm_dsr = _decoy_dataset_registry(conn, tag)
        if do == "matrix_member_index":
            fmm_index = m.member_index + 1000
        if do == "matrix_member_feature_values_hash":
            fmm_fvh = H(f"wrong:fmm-fvh:{tag}")
        if do == "matrix_member_vector_hash":
            fmm_vec = H(f"wrong:vec:{tag}")
        # different_dataset_sets: the last plan dataset gets NO matrix member (an extra dataset's
        # matrix member is added post-loop instead), so snapshot and matrix dataset sets differ.
        if not (set_defect == "different_dataset_sets" and i == last):
            _insert(
                conn,
                "profiling",
                "feature_matrix_members",
                {
                    "id": U(f"fmm:{plan.train_matrix_hash}:{m.dataset_id}"),
                    "feature_matrix_id": fmm_matrix,
                    "dataset_registry_id": fmm_dsr,
                    "member_index": fmm_index,
                    "vector_hash": fmm_vec,
                    "feature_values_hash": fmm_fvh,
                },
            )

    n = len(plan.members)
    if set_defect == "extra_snapshot_member":
        # one additional train snapshot member (not in the plan) -> snapshot count > plan.
        _seed_extra_train_dataset(
            conn, tag, "snap", agen, snap, mat, member_index=n, with_psm=True, with_fmm=False
        )
    elif set_defect == "extra_matrix_member":
        # one additional matrix member (not in the plan) -> matrix count > plan.
        _seed_extra_train_dataset(
            conn, tag, "mat", agen, snap, mat, member_index=n, with_psm=False, with_fmm=True
        )
    elif set_defect == "member_index_mismatch":
        # an extra matrix member at an out-of-range index -> matrix index set != plan index set.
        _seed_extra_train_dataset(
            conn, tag, "idx", agen, snap, mat, member_index=n + 5, with_psm=False, with_fmm=True
        )
    elif set_defect == "different_dataset_sets":
        # replacement matrix member for the withheld last dataset, under a DIFFERENT dataset.
        _seed_extra_train_dataset(
            conn, tag, "ds", agen, snap, mat, member_index=last, with_psm=False, with_fmm=True
        )
    conn.execute(text("RESET ROLE"))
