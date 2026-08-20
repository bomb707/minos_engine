"""E5 frozen count policy — counts derive from snapshot membership (NO 50/10/15).

Uses two non-75 synthetic snapshots with uneven chromosome sizes (conftest snap_a:
4 train / 2 validation; snap_b: 1 train / 3 validation) to prove matrix counts come from
actual frozen membership, that no fixed 50/10/15 assumption exists, that train/validation
matrices exactly cover their assigned members verbatim, and that no test matrix is made.
"""

from __future__ import annotations

from sqlalchemy import text

from minos_engine.layer2.features.extraction import load_member_manifest_with_trust


def _snapshot(snap):
    return load_member_manifest_with_trust(snap.manifest_bytes, snap.trust)


def test_counts_derive_from_membership_not_50_10_15(snap_a, snap_b, built) -> None:
    for snap, name, exp_train, exp_val in ((snap_a, "a", 4, 2), (snap_b, "b", 1, 3)):
        snapshot = _snapshot(snap)
        # counts derived from the frozen snapshot membership, not any constant
        assert len(snapshot.members_for("train")) == exp_train
        assert len(snapshot.members_for("validation")) == exp_val
        assert built[(name, "train")].row_count == exp_train
        assert built[(name, "validation")].row_count == exp_val
    # explicitly NOT the epoch-1 production numbers
    observed = {
        built[("a", "train")].row_count,
        built[("a", "validation")].row_count,
        built[("b", "train")].row_count,
        built[("b", "validation")].row_count,
    }
    assert 50 not in observed and 10 not in observed and 15 not in observed


def test_matrices_cover_their_members_verbatim(snap_a, l2e_engine, built) -> None:
    snapshot = _snapshot(snap_a)
    for partition in ("train", "validation"):
        expected = [m.dataset_id for m in snapshot.members_for(partition)]
        with l2e_engine.connect() as conn:
            rows = conn.execute(
                text(
                    "SELECT dr.dataset_id FROM profiling.feature_matrix_members mm "
                    "JOIN profiling.feature_matrices fm ON fm.id = mm.feature_matrix_id "
                    "JOIN catalog.dataset_registry dr ON dr.id = mm.dataset_registry_id "
                    "JOIN profiling.profile_snapshots ps ON ps.id = fm.profile_snapshot_id "
                    "WHERE fm.partition = :p AND ps.snapshot_hash = :h "
                    "ORDER BY mm.member_index"
                ),
                {"p": partition, "h": snapshot.snapshot_hash},
            ).all()
        assert [r[0] for r in rows] == expected  # verbatim membership + order


def test_grandfathered_membership_consumed_unchanged(snap_b, built) -> None:
    # snap_b's small partitions are consumed exactly as the frozen snapshot declares them
    snapshot = _snapshot(snap_b)
    assert built[("b", "train")].row_count == len(snapshot.members_for("train"))
    assert built[("b", "validation")].row_count == len(snapshot.members_for("validation"))


def test_no_test_matrix_produced(snap_a, snap_b, built, l2e_engine) -> None:
    for snap in (snap_a, snap_b):
        snapshot = _snapshot(snap)
        with l2e_engine.connect() as conn:
            n = conn.execute(
                text(
                    "SELECT count(*) FROM profiling.feature_matrices fm "
                    "JOIN profiling.profile_snapshots ps ON ps.id = fm.profile_snapshot_id "
                    "WHERE fm.partition = 'test' AND ps.snapshot_hash = :h"
                ),
                {"h": snapshot.snapshot_hash},
            ).scalar_one()
        assert n == 0
