"""Exact-connection operational-identity guard (real PostgreSQL 16).

The accepted production builder must verify the canonical operational identity on the
EXACT connections it reads and writes through — not on a throwaway engine connection
whose result cannot vouch for a later, different connection. These regression tests pin
that: a value observed on one connection never authorizes another; the read connection
is verified before any payload access; a rejected transaction connection writes nothing;
the builder verifies every connection it uses; and the synthetic/test builder stays
name-independent.
"""

from __future__ import annotations

import hashlib

import pytest
from sqlalchemy import Engine, text

from minos_engine.layer2.features.extraction import (
    build_partition_matrix,
    load_member_manifest_with_trust,
)
from minos_engine.layer2.features.matrix_parquet import serialize_matrix
from minos_engine.storage import feature_matrix as fm
from minos_engine.storage.database import (
    OperationalDatabaseIdentityError,
    connected_database_name,
)
from tests.conftest import REPO_ROOT
from tests.integration.layer2_features.conftest import make_publisher

_ACCEPTED_MANIFEST = REPO_ROOT / "manifests" / "profile_snapshot_epoch1_members.json"


def _matrix_rows(engine: Engine, epoch: int, partition: str) -> int:
    with engine.connect() as conn:
        return int(
            conn.execute(
                text(
                    "SELECT count(*) FROM profiling.feature_matrices fm "
                    "JOIN profiling.profile_snapshots ps ON ps.id = fm.profile_snapshot_id "
                    "WHERE ps.epoch = :e AND fm.partition = :p"
                ),
                {"e": epoch, "p": partition},
            ).scalar_one()
        )


def _member_rows(engine: Engine, sha256: str) -> int:
    with engine.connect() as conn:
        return int(
            conn.execute(
                text(
                    "SELECT count(*) FROM profiling.feature_matrix_members mm "
                    "JOIN profiling.feature_matrices fm ON fm.id = mm.feature_matrix_id "
                    "WHERE fm.artifact_sha256 = :h"
                ),
                {"h": sha256},
            ).scalar_one()
        )


def _artifact_rows(engine: Engine, sha256: str) -> int:
    with engine.connect() as conn:
        return int(
            conn.execute(
                text("SELECT count(*) FROM catalog.artifacts WHERE sha256 = :h"), {"h": sha256}
            ).scalar_one()
        )


# --------------------------------------------------------------------------- #
# read connection: identity is verified BEFORE snapshot read or payload access
# --------------------------------------------------------------------------- #
def test_accepted_builder_read_connection_guard_precedes_payload_access(
    l2e_engine, matrix_publisher, monkeypatch
) -> None:
    """The accepted builder, pointed at a NON-canonical scratch database, must fail on
    the exact read connection — before ``_verify_operational_snapshot`` or any
    ``_payload_source`` call."""
    committed = _ACCEPTED_MANIFEST.read_bytes()
    calls = {"snapshot": 0, "payload": 0}
    real_vs = fm._verify_operational_snapshot
    real_ps = fm._payload_source

    def spy_vs(*a, **k):  # pragma: no cover - must NOT run
        calls["snapshot"] += 1
        return real_vs(*a, **k)

    def spy_ps(*a, **k):  # pragma: no cover - must NOT run
        calls["payload"] += 1
        return real_ps(*a, **k)

    monkeypatch.setattr(fm, "_verify_operational_snapshot", spy_vs)
    monkeypatch.setattr(fm, "_payload_source", spy_ps)

    with pytest.raises(OperationalDatabaseIdentityError):
        fm.build_accepted_epoch1_feature_matrix(
            l2e_engine, committed, "train", publisher=matrix_publisher
        )
    assert calls == {"snapshot": 0, "payload": 0}  # rejected before any read/payload access


# --------------------------------------------------------------------------- #
# transaction connection: a rejected txn conn writes zero rows / artifacts
# --------------------------------------------------------------------------- #
def test_transaction_connection_guard_writes_nothing(
    l2e_engine, extra_snaps, artifact_root
) -> None:
    """``_persist_feature_matrix`` with ``require_operational_identity=True`` against a
    non-canonical database must fail on the exact transaction connection (right after it
    begins) and leave zero matrix/member/artifact rows and no published artifact."""
    snap = extra_snaps[9]
    snapshot = load_member_manifest_with_trust(snap.manifest_bytes, snap.trust)
    build = build_partition_matrix(
        snapshot, "train", lambda m: snap.payload_paths[m.dataset_id].read_bytes()
    )
    sha = hashlib.sha256(serialize_matrix(build.matrix, build.vectors)).hexdigest()
    final_path = artifact_root / "l2e" / "train" / f"{sha}.parquet"

    before = _matrix_rows(l2e_engine, 9, "train")
    with pytest.raises(OperationalDatabaseIdentityError):
        fm._persist_feature_matrix(
            l2e_engine,
            snapshot=snapshot,
            matrix=build.matrix,
            vectors=build.vectors,
            publisher=make_publisher(artifact_root),
            require_operational_identity=True,
        )
    assert _matrix_rows(l2e_engine, 9, "train") == before
    assert _member_rows(l2e_engine, sha) == 0
    assert _artifact_rows(l2e_engine, sha) == 0
    assert not final_path.exists()


# --------------------------------------------------------------------------- #
# a value observed on one connection never authorizes another; and the builder
# verifies EVERY connection it uses (read + transaction, two distinct sessions)
# --------------------------------------------------------------------------- #
def test_builder_verifies_each_connection_and_earlier_ok_does_not_authorize_later(
    l2e_engine, extra_snaps, artifact_root, monkeypatch
) -> None:
    """Drive the exact internal production path with ``require_operational_identity=True``.
    A spy lets the READ connection appear canonical (so the flow reaches the write
    connection) while recording every verified connection. The TRANSACTION connection is
    then rejected on its own live identity — proving the earlier "OK" did not authorize
    it, and that both connections the builder uses are independently verified."""
    snap = extra_snaps[10]
    snapshot = load_member_manifest_with_trust(snap.manifest_bytes, snap.trust)

    seen: list[tuple[int, str]] = []

    def spy(conn):
        seen.append((id(conn), connected_database_name(conn)))
        if len(seen) == 1:
            return "minos_engine_db"  # first (read) connection: allow the flow to proceed
        # second (transaction) connection: enforce its real, non-canonical identity
        name = connected_database_name(conn)
        raise OperationalDatabaseIdentityError(
            f"connected database is {name!r}, not the canonical operational store"
        )

    monkeypatch.setattr(fm, "verify_operational_database_identity", spy)

    before = _matrix_rows(l2e_engine, 10, "train")
    with pytest.raises(OperationalDatabaseIdentityError):
        fm._build_feature_matrix(
            l2e_engine,
            snapshot,
            "train",
            publisher=make_publisher(artifact_root),
            require_operational_identity=True,
        )

    # exactly two verifications, on two DISTINCT connections (read + transaction)
    assert len(seen) == 2
    assert seen[0][0] != seen[1][0]
    # both are the real, same non-canonical scratch database — each was checked live
    assert seen[0][1] == seen[1][1]
    # the read connection reporting "OK" did not authorize the write connection
    assert _matrix_rows(l2e_engine, 10, "train") == before


# --------------------------------------------------------------------------- #
# the synthetic / test-only builder stays name-independent (no guard imposed)
# --------------------------------------------------------------------------- #
def test_synthetic_builder_is_name_independent(l2e_engine, snap_a, built) -> None:
    """``build_feature_matrix_with_trust`` (require_operational_identity=False) persists
    successfully against a NON-canonical scratch database (``minos_l2e_features``) — the
    ``built`` fixture already exercised it, so scratch DBs remain usable, no guard
    imposed."""
    with l2e_engine.connect() as conn:
        assert connected_database_name(conn) != "minos_engine_db"  # scratch, not canonical
    # snap_a (synthetic epoch 1) was persisted through the test-only builder here.
    assert built[("a", "train")].row_count == 4
    assert _matrix_rows(l2e_engine, 1, "train") == 1
