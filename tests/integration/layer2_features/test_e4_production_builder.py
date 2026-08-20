"""E4 production-builder SOURCE proofs on real PostgreSQL 16.

Exercises the operational materialization surface end-to-end against a real server for
the guarantees that need a live database: canonical-DB enforcement on every production
connection, root validation, verbatim membership (no fixed 50/10/15), physical/role
isolation, idempotency, typed conflict, zero-partial-rows on failure, and hashes-only
metadata. The full accepted-snapshot success path is proven at operational
materialization (it requires the real 75-profile corpus + canonical operational DB).
"""

from __future__ import annotations

import hashlib

import pytest
from sqlalchemy import text

from minos_engine.common.errors import MatrixAccessError, MatrixConflictError
from minos_engine.layer2.features.extraction import (
    build_partition_matrix,
    load_member_manifest_with_trust,
)
from minos_engine.layer2.features.matrix_parquet import serialize_matrix
from minos_engine.storage import feature_matrix as fm
from minos_engine.storage.database import OperationalDatabaseIdentityError
from minos_engine.storage.feature_matrix import build_feature_matrix_with_trust
from minos_engine.storage.feature_matrix_production import (
    build_operational_feature_matrices,
    matrix_metadata,
)
from minos_engine.storage.matrix_access import PartitionArtifactPublisher
from tests.conftest import REPO_ROOT
from tests.integration.layer2_features.conftest import make_publisher, provision_test_roots

_ACCEPTED_MANIFEST = REPO_ROOT / "manifests" / "profile_snapshot_epoch1_members.json"


def _rows_for_snapshot_hash(engine, snapshot_hash: str, partition: str) -> int:
    with engine.connect() as conn:
        return int(
            conn.execute(
                text(
                    "SELECT count(*) FROM profiling.feature_matrices fm "
                    "JOIN profiling.profile_snapshots ps ON ps.id = fm.profile_snapshot_id "
                    "WHERE ps.snapshot_hash = :h AND fm.partition = :p"
                ),
                {"h": snapshot_hash, "p": partition},
            ).scalar_one()
        )


# (proof 2) canonical DB required on EVERY production connection — a non-canonical DB is
# rejected on the read connection before any payload access, and nothing is materialized.
def test_production_builder_requires_canonical_db(l2e_engine, artifact_root) -> None:
    from minos_engine.layer2.prerequisites import PROFILE_SNAPSHOT_1_HASH

    committed = _ACCEPTED_MANIFEST.read_bytes()
    with pytest.raises(OperationalDatabaseIdentityError):
        build_operational_feature_matrices(
            l2e_engine,  # scratch DB named minos_l2e_features (NOT canonical)
            member_manifest_bytes=committed,
            train_root=artifact_root / "l2e" / "train",
            validation_root=artifact_root / "l2e" / "validation",
        )
    # nothing materialized for the accepted snapshot on the non-canonical database
    assert _rows_for_snapshot_hash(l2e_engine, PROFILE_SNAPSHOT_1_HASH, "train") == 0
    assert _rows_for_snapshot_hash(l2e_engine, PROFILE_SNAPSHOT_1_HASH, "validation") == 0


# root validation: missing and mis-permissioned roots are refused (publisher boundary)
def test_production_builder_refuses_missing_root(l2e_engine, artifact_root, tmp_path) -> None:
    with pytest.raises(MatrixAccessError, match="does not exist"):
        build_operational_feature_matrices(
            l2e_engine,
            member_manifest_bytes=b"{}",
            train_root=tmp_path / "nonexistent" / "train",
            validation_root=artifact_root / "l2e" / "validation",
        )


def test_production_builder_refuses_wrong_mode_root(l2e_engine, artifact_root, tmp_path) -> None:
    bad = tmp_path / "wrongmode"
    bad.mkdir()
    bad.chmod(0o755)  # not 0o2750
    with pytest.raises(MatrixAccessError, match="mode"):
        build_operational_feature_matrices(
            l2e_engine,
            member_manifest_bytes=b"{}",
            train_root=bad,
            validation_root=artifact_root / "l2e" / "validation",
        )


# (proof 3) train/validation membership consumed verbatim (identity + order)
def test_membership_consumed_verbatim(snap_a) -> None:
    snapshot = load_member_manifest_with_trust(snap_a.manifest_bytes, snap_a.trust)
    for partition in ("train", "validation"):
        build = build_partition_matrix(
            snapshot, partition, lambda m: snap_a.payload_paths[m.dataset_id].read_bytes()
        )
        assert [mm.dataset_id for mm in build.matrix.members] == [
            m.dataset_id for m in snapshot.members_for(partition)
        ]


# (proof 4) row counts derive from membership — NO fixed 50/10/15 invariant
def test_no_fixed_50_10_15_invariant(built) -> None:
    counts = {k: built[k].row_count for k in built}
    # uneven synthetic snapshots: a=4 train/2 validation, b=1 train/3 validation (verbatim)
    assert counts[("a", "train")] == 4
    assert counts[("a", "validation")] == 2
    assert counts[("b", "train")] == 1
    assert counts[("b", "validation")] == 3
    assert 50 not in counts.values() and 10 not in counts.values() and 15 not in counts.values()


# (proof 7) physical isolation: distinct roots + distinct partition gids are mandatory
def test_physical_partition_isolation(matrix_publisher, artifact_root) -> None:
    snapshot = matrix_publisher.credential_snapshot()
    assert snapshot.train.root != snapshot.validation.root
    assert snapshot.train.gid != snapshot.validation.gid  # distinct partition groups
    same = artifact_root / "l2e" / "train"
    with pytest.raises(MatrixAccessError, match="not be the same"):
        PartitionArtifactPublisher(train_root=same, validation_root=same)


# (proof 8) same input rebuild is idempotent (verified replay of stored artifact)
def test_idempotent_rebuild(l2e_engine, snap_a, built, artifact_root) -> None:
    r1 = build_feature_matrix_with_trust(
        l2e_engine, snap_a.manifest_bytes, snap_a.trust, "train", artifact_root=artifact_root
    )
    r2 = build_feature_matrix_with_trust(
        l2e_engine, snap_a.manifest_bytes, snap_a.trust, "train", artifact_root=artifact_root
    )
    assert r1.idempotent and r2.idempotent
    assert r1.matrix_hash == r2.matrix_hash
    assert r1.artifact_sha256 == r2.artifact_sha256
    assert r1.feature_matrix_id == r2.feature_matrix_id


# (proof 9) conflicting logical identity (relocation to a different root) fails typed
def test_conflicting_identity_fails_typed(l2e_engine, snap_a, built, tmp_path_factory) -> None:
    other = provision_test_roots(tmp_path_factory.mktemp("e4_other_root"))
    with pytest.raises(MatrixConflictError):
        build_feature_matrix_with_trust(
            l2e_engine, snap_a.manifest_bytes, snap_a.trust, "train", artifact_root=other
        )


# (proof 10) a failure leaves zero partial matrix/member/artifact rows and no artifact file
def test_failure_leaves_zero_partial_rows(
    l2e_engine, extra_snaps, artifact_root, monkeypatch
) -> None:
    from sqlalchemy.exc import DBAPIError

    snap = extra_snaps[9]
    snapshot = load_member_manifest_with_trust(snap.manifest_bytes, snap.trust)
    build = build_partition_matrix(
        snapshot, "train", lambda m: snap.payload_paths[m.dataset_id].read_bytes()
    )
    sha = hashlib.sha256(serialize_matrix(build.matrix, build.vectors)).hexdigest()
    final_path = artifact_root / "l2e" / "train" / f"{sha}.parquet"

    def _boom(*a, **k):
        raise DBAPIError("stmt", {}, Exception("injected pre-commit failure"))

    monkeypatch.setattr(fm, "_read_back_and_verify", _boom)
    with pytest.raises(DBAPIError):
        fm._persist_feature_matrix(
            l2e_engine,
            snapshot=snapshot,
            matrix=build.matrix,
            vectors=build.vectors,
            publisher=make_publisher(artifact_root),
            require_operational_identity=False,
        )
    monkeypatch.undo()
    with l2e_engine.connect() as conn:
        assert (
            conn.execute(
                text("SELECT count(*) FROM catalog.artifacts WHERE sha256=:h"), {"h": sha}
            ).scalar_one()
            == 0
        )
        assert (
            conn.execute(
                text(
                    "SELECT count(*) FROM profiling.feature_matrix_members mm "
                    "JOIN profiling.feature_matrices f ON f.id=mm.feature_matrix_id "
                    "WHERE f.artifact_sha256=:h"
                ),
                {"h": sha},
            ).scalar_one()
            == 0
        )
    assert not final_path.exists()


# metadata is HASHES-ONLY: no uri/path, no plaintext feature values, hashes + gid/mode only
def test_metadata_is_hashes_only(l2e_engine, snap_a, built, matrix_publisher) -> None:
    meta = matrix_metadata(
        l2e_engine, built[("a", "train")], partition="train", publisher=matrix_publisher
    )
    # no retrievable location anywhere
    assert "uri" not in meta and "artifact_path" not in meta and "path" not in meta
    # identity/hashes present; column count frozen at 129
    assert len(str(meta["matrix_hash"])) == 64
    assert len(str(meta["artifact_sha256"])) == 64
    assert meta["column_count"] == 129
    assert meta["partition"] == "train"
    assert str(meta["artifact_mode"]) == oct(0o640)
    assert str(meta["root_mode"]) == oct(0o2750)
    assert isinstance(meta["partition_gid"], int)
    # members carry ONLY hashes/ids — never plaintext vectors/values
    members = meta["members"]
    assert isinstance(members, list) and len(members) == 4
    for m in members:
        assert set(m) == {"dataset_id", "member_index", "vector_hash", "feature_values_hash"}
        assert len(m["vector_hash"]) == 64 and len(m["feature_values_hash"]) == 64

    # no floating-point value (a plaintext feature) appears anywhere in the metadata
    def _no_float(obj) -> bool:
        if isinstance(obj, float):
            return False
        if isinstance(obj, dict):
            return all(_no_float(v) for v in obj.values())
        if isinstance(obj, list):
            return all(_no_float(v) for v in obj)
        return True

    assert _no_float(meta)
