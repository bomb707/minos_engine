"""E3 builder: uneven snapshot-derived matrices, trust boundary, test-partition seals."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from minos_engine.common.hashing import canonical_hash
from minos_engine.layer2.features.errors import (
    ForbiddenPartitionError,
    MemberManifestHashMismatchError,
    SnapshotIdentityMismatchError,
)
from minos_engine.layer2.features.extraction import (
    build_partition_matrix,
    load_member_manifest_with_trust,
    verify_matrix,
)
from minos_engine.layer2.features.matrix_parquet import verify_matrix_artifact
from minos_engine.storage.feature_matrix import (
    _artifact_uri_to_path,
    build_accepted_epoch1_feature_matrix,
    build_feature_matrix_with_trust,
)
from tests.conftest import REPO_ROOT


class _ExplodingEngine:
    """A stand-in Engine that fails the test if the builder ever touches the DB."""

    def connect(self):  # pragma: no cover - reaching this IS the failure
        raise AssertionError("database must not be touched before the trust boundary")

    def begin(self):  # pragma: no cover - reaching this IS the failure
        raise AssertionError("database must not be touched before the trust boundary")


def test_uneven_snapshot_matrices_are_membership_derived(l2e_engine, snap_a, snap_b, built) -> None:
    expectations = {
        ("a", "train"): 4,
        ("a", "validation"): 2,
        ("b", "train"): 1,
        ("b", "validation"): 3,
    }
    for (name, partition), expected_rows in expectations.items():
        result = built[(name, partition)]
        assert result.row_count == expected_rows  # snapshot-derived, never 50/10/15
        payload = Path(result.artifact_path).read_bytes()
        assert hashlib.sha256(payload).hexdigest() == result.artifact_sha256
        assert f"/l2e/{partition}/" in result.artifact_path


def test_grandfathered_membership_consumed_verbatim(l2e_engine, snap_b, built) -> None:
    # validation(3) > train(1): consumed verbatim — no reallocation or re-rounding.
    with l2e_engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT fm.partition, dr.dataset_id FROM profiling.feature_matrix_members mm "
                "JOIN profiling.feature_matrices fm ON fm.id = mm.feature_matrix_id "
                "JOIN profiling.profile_snapshots ps ON ps.id = fm.profile_snapshot_id "
                "JOIN catalog.dataset_registry dr ON dr.id = mm.dataset_registry_id "
                "WHERE ps.epoch = 2 ORDER BY fm.partition, mm.member_index"
            )
        ).all()
    by_partition: dict[str, list[str]] = {}
    for row in rows:
        by_partition.setdefault(row.partition, []).append(row.dataset_id)
    assert by_partition["train"] == ["ds-b19-01"]
    assert by_partition["validation"] == ["ds-b19-02", "ds-b19-03", "ds-b19-04"]


def test_built_matrices_pass_logical_and_payload_verification(snap_a, built) -> None:
    snapshot = load_member_manifest_with_trust(snap_a.manifest_bytes, snap_a.trust)
    build = build_partition_matrix(
        snapshot, "train", lambda m: snap_a.payload_paths[m.dataset_id].read_bytes()
    )
    result = built[("a", "train")]
    assert build.matrix.matrix_hash == result.matrix_hash
    logical = verify_matrix(build.matrix, snapshot, build.vectors)
    assert logical.ok, logical.failed()
    payload = Path(result.artifact_path).read_bytes()
    checks = verify_matrix_artifact(payload, build.matrix, build.vectors, result.artifact_sha256)
    assert checks and all(checks.values()), [k for k, v in checks.items() if not v]


def test_test_partition_rejected_before_provider_filesystem_or_db(snap_a, artifact_root) -> None:
    # the exploding engine proves NO DB access happens; the artifact tree is untouched.
    with pytest.raises(ForbiddenPartitionError):
        build_feature_matrix_with_trust(
            _ExplodingEngine(),  # type: ignore[arg-type]
            snap_a.manifest_bytes,
            snap_a.trust,
            "test",
            artifact_root=artifact_root,
        )
    assert not (artifact_root / "l2e" / "test").exists()


def test_accepted_builder_rejects_test_partition_before_db(matrix_publisher) -> None:
    committed = (REPO_ROOT / "manifests" / "profile_snapshot_epoch1_members.json").read_bytes()
    with pytest.raises(ForbiddenPartitionError):
        build_accepted_epoch1_feature_matrix(
            _ExplodingEngine(),  # type: ignore[arg-type]
            committed,
            "test",
            publisher=matrix_publisher,
        )


def test_accepted_builder_rejects_alternate_manifest_before_any_access(matrix_publisher) -> None:
    """A fully self-consistent alternate manifest (rehashed member_manifest_hash AND
    snapshot_hash) is rejected by the PINNED accepted boundary before the builder
    touches the database, a payload provider, or the filesystem."""
    committed = (REPO_ROOT / "manifests" / "profile_snapshot_epoch1_members.json").read_bytes()
    raw = json.loads(committed)
    raw["members"][0]["profile_sha256"] = "0" * 64
    content = {k: v for k, v in raw.items() if k != "member_manifest_hash"}
    raw["member_manifest_hash"] = canonical_hash(content)
    with pytest.raises(MemberManifestHashMismatchError):
        build_accepted_epoch1_feature_matrix(
            _ExplodingEngine(),  # type: ignore[arg-type]
            json.dumps(raw).encode(),
            "train",
            publisher=matrix_publisher,
        )


def test_accepted_builder_refuses_synthetic_snapshots(snap_a, matrix_publisher) -> None:
    with pytest.raises(MemberManifestHashMismatchError):
        build_accepted_epoch1_feature_matrix(
            _ExplodingEngine(),  # type: ignore[arg-type]
            snap_a.manifest_bytes,
            "train",
            publisher=matrix_publisher,
        )


def test_builder_requires_operational_snapshot_identity(
    l2e_engine, snap_a, artifact_root, tmp_path_factory
) -> None:
    """A verified manifest whose snapshot is NOT reproduced by the operational store
    is rejected before any payload read."""
    from tests.integration.layer2_features.conftest import build_synthetic_snapshot

    unseeded = build_synthetic_snapshot(
        (("ds-x9-01", "chr18", "train"), ("ds-x9-02", "chr19", "validation")),
        epoch=9,
        payload_dir=tmp_path_factory.mktemp("l2e_unseeded"),
    )
    with pytest.raises(SnapshotIdentityMismatchError):
        build_feature_matrix_with_trust(
            l2e_engine,
            unseeded.manifest_bytes,
            unseeded.trust,
            "train",
            artifact_root=artifact_root,
        )


def test_no_test_matrix_rows_or_objects(l2e_engine, built) -> None:
    with l2e_engine.connect() as conn:
        count = conn.execute(
            text("SELECT count(*) FROM profiling.feature_matrices WHERE partition = 'test'")
        ).scalar()
    assert count == 0
    # the CHECK constraint makes a test row structurally impossible, even as admin.
    with pytest.raises(DBAPIError), l2e_engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO profiling.feature_matrices (profile_snapshot_id, partition, "
                " feature_set_id, matrix_hash, artifact_sha256, matrix_artifact_id, "
                " row_count, column_count) "
                "SELECT fm.profile_snapshot_id, 'test', fm.feature_set_id, repeat('9', 64), "
                " fm.artifact_sha256, fm.matrix_artifact_id, fm.row_count, fm.column_count "
                "FROM profiling.feature_matrices fm LIMIT 1"
            )
        )


def test_builder_reads_exact_artifact_bytes_not_jsonb(l2e_engine, snap_a, built) -> None:
    """The persisted vectors derive from the exact artifact bytes: the recomputed
    member hashes in DB equal the manifest's, and the profile artifact URIs resolve to
    files whose bytes hash to the manifest-bound profile_sha256."""
    with l2e_engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT dr.dataset_id, bp.profile_sha256, art.uri "
                "FROM profiling.bam_profiles bp "
                "JOIN catalog.dataset_registry dr ON dr.id = bp.dataset_registry_id "
                "JOIN catalog.artifacts art ON art.id = bp.profile_artifact_id "
                "JOIN profiling.profile_snapshot_members m ON m.bam_profile_id = bp.id "
                "JOIN profiling.profile_snapshots ps ON ps.id = m.profile_snapshot_id "
                "WHERE ps.epoch = 1 AND m.partition = 'train'"
            )
        ).all()
    assert rows
    for row in rows:
        payload = _artifact_uri_to_path(row.uri).read_bytes()
        assert hashlib.sha256(payload).hexdigest() == row.profile_sha256


# --------------------------------------------------------------------------- #
# item 5 — zero-row matrices persist, are exposed, and remain retrievable
# --------------------------------------------------------------------------- #
def test_zero_row_matrices_persist_and_derive_zero_counts(
    l2e_engine, built_zero_row, extra_snaps
) -> None:
    # epoch 7: validation empty; epoch 8: train empty.
    assert built_zero_row["e7_train"].row_count == 2
    assert built_zero_row["e7_validation"].row_count == 0
    assert built_zero_row["e8_train"].row_count == 0
    assert built_zero_row["e8_validation"].row_count == 2
    with l2e_engine.connect() as conn:
        for epoch, partition in ((7, "validation"), (8, "train")):
            matrices = conn.execute(
                text(
                    "SELECT count(*) FROM profiling.feature_matrices fm "
                    "JOIN profiling.profile_snapshots ps ON ps.id = fm.profile_snapshot_id "
                    "WHERE ps.epoch = :e AND fm.partition = :p"
                ),
                {"e": epoch, "p": partition},
            ).scalar_one()
            members = conn.execute(
                text(
                    "SELECT count(*) FROM profiling.feature_matrix_members mm "
                    "JOIN profiling.feature_matrices fm ON fm.id = mm.feature_matrix_id "
                    "JOIN profiling.profile_snapshots ps ON ps.id = fm.profile_snapshot_id "
                    "WHERE ps.epoch = :e AND fm.partition = :p"
                ),
                {"e": epoch, "p": partition},
            ).scalar_one()
            assert matrices == 1  # the zero-row matrix persists
            assert members == 0  # with no member rows


def test_zero_row_matrix_exposed_by_authorized_view_only(
    l2e_engine, built_zero_row, matrix_broker
) -> None:
    val_empty = built_zero_row["e7_validation"]  # zero-row validation matrix
    train_empty = built_zero_row["e8_train"]  # zero-row train matrix
    # the authorized partition view exposes ONE matrix-level row (NULL member columns).
    with l2e_engine.connect() as conn:
        conn.execute(text("SET ROLE minos_evaluator"))
        rows = conn.execute(
            text(
                "SELECT matrix_hash, artifact_sha256, dataset_id, member_index "
                "FROM evaluation.validation_matrix WHERE matrix_hash = :h"
            ),
            {"h": val_empty.matrix_hash},
        ).all()
        assert len(rows) == 1
        assert rows[0].dataset_id is None and rows[0].member_index is None
        assert rows[0].artifact_sha256 == val_empty.artifact_sha256
        # a zero-row artifact is retrievable + verifiable through the reader.
        reader = matrix_broker.reader_for(conn)
        payload = reader.fetch_matrix_payload(conn, val_empty.matrix_hash)
        assert hashlib.sha256(payload).hexdigest() == val_empty.artifact_sha256
        # the opposite role cannot resolve it.
        with pytest.raises(Exception, match="permission denied|does not exist"):
            conn.execute(
                text("SELECT 1 FROM profiling.training_matrix WHERE matrix_hash = :h"),
                {"h": train_empty.matrix_hash},
            )
    with l2e_engine.connect() as conn:
        conn.execute(text("SET ROLE minos_trainer"))
        reader = matrix_broker.reader_for(conn)
        payload = reader.fetch_matrix_payload(conn, train_empty.matrix_hash)
        assert hashlib.sha256(payload).hexdigest() == train_empty.artifact_sha256
        # a trainer cannot resolve the zero-row VALIDATION matrix.
        with pytest.raises(Exception, match="not visible"):
            reader.fetch_matrix_payload(conn, val_empty.matrix_hash)
    # no test matrix anywhere.
    with l2e_engine.connect() as conn:
        n = conn.execute(
            text("SELECT count(*) FROM profiling.feature_matrices WHERE partition = 'test'")
        ).scalar_one()
    assert n == 0


def test_select_config_remains_blocked() -> None:
    from minos_engine.common.errors import StageNotReadyError
    from minos_engine.layer2.service import Layer2Service

    with pytest.raises(StageNotReadyError):
        Layer2Service().select_config(None)  # type: ignore[arg-type]
