"""E5 feature-view verifier cross-binding + tamper suite (real PostgreSQL 16 fixtures).

Exercises the pure cross-binding (``feature_view_cross_binding_checks``) against real
synthetic matrices built through the trust boundary, so tamper/consistent-rehash attacks
run in CI without the production corpus. The full production ``verify_feature_view``
positive path (accepted snapshot + canonical DB) is proven at operational verification.
"""

from __future__ import annotations

import hashlib
import inspect

from minos_engine.layer2.features.contracts import build_feature_set_manifest
from minos_engine.layer2.features.extraction import (
    build_partition_matrix,
    load_member_manifest_with_trust,
    verify_matrix,
)
from minos_engine.layer2.features.matrix_parquet import serialize_matrix
from minos_engine.storage import feature_view_verify as fvv
from minos_engine.storage.feature_view_verify import feature_view_cross_binding_checks


def _build(snap, partition="train"):
    snapshot = load_member_manifest_with_trust(snap.manifest_bytes, snap.trust)
    build = build_partition_matrix(
        snapshot, partition, lambda m: snap.payload_paths[m.dataset_id].read_bytes()
    )
    payload = serialize_matrix(build.matrix, build.vectors)
    sha = hashlib.sha256(payload).hexdigest()
    return snapshot, build, payload, sha


def _honest_db(build, sha):
    vec = {v.dataset_id: v for v in build.vectors}
    row = {
        "matrix_hash": build.matrix.matrix_hash,
        "artifact_sha256": sha,
        "row_count": len(build.matrix.members),
        "column_count": 129,
    }
    members = [
        {
            "dataset_id": m.dataset_id,
            "member_index": i,
            "vector_hash": m.vector_hash,
            "feature_values_hash": vec[m.dataset_id].feature_values_hash,
        }
        for i, m in enumerate(build.matrix.members)
    ]
    return row, members


def _checks(snap, *, row=None, members=None, artifact=None, feature_set=None, snap_hash=None):
    snapshot, build, payload, sha = _build(snap)
    r, m = _honest_db(build, sha)
    return feature_view_cross_binding_checks(
        feature_set=feature_set or build_feature_set_manifest(),
        snapshot=snapshot,
        build=build,
        derived_sha=sha,
        logical_ok=verify_matrix(build.matrix, snapshot, build.vectors).ok,
        db_row=row or r,
        db_members=members if members is not None else m,
        db_snapshot_hash=snap_hash or snapshot.snapshot_hash,
        artifact_bytes=artifact if artifact is not None else payload,
    )


def test_honest_feature_view_passes(snap_a) -> None:
    assert all(_checks(snap_a).values())


def test_production_verifier_has_no_caller_feature_set_override() -> None:
    # verify_feature_view derives the feature set internally — no caller override exists.
    params = set(inspect.signature(fvv.verify_feature_view).parameters)
    assert "feature_set" not in params and "columns" not in params and "artifact_root" not in params


def test_wrong_feature_set_rejected(snap_a) -> None:
    bad = build_feature_set_manifest().model_copy(update={"feature_set_hash": "f" * 64})
    checks = _checks(snap_a, feature_set=bad)
    assert not checks["feature_set_hash_pinned"]


def test_db_matrix_hash_tamper_rejected(snap_a) -> None:
    _, build, _, sha = _build(snap_a)
    r, m = _honest_db(build, sha)
    r["matrix_hash"] = "0" * 64  # tampered matrix_hash
    assert not _checks(snap_a, row=r)["db_matrix_hash_matches_derived"]


def test_artifact_byte_tamper_rejected(snap_a) -> None:
    _, _, payload, _ = _build(snap_a)
    tampered = bytearray(payload)
    tampered[-1] ^= 0x01  # flip a byte
    checks = _checks(snap_a, artifact=bytes(tampered))
    assert not checks["physical_artifact_sha_matches"] or not checks["physical_artifact_reverifies"]


def test_member_reorder_rejected(snap_a) -> None:
    _, build, _, sha = _build(snap_a)
    _, m = _honest_db(build, sha)
    if len(m) >= 2:
        m[0], m[1] = m[1], m[0]  # reorder members
    assert not _checks(snap_a, members=m)["member_order_and_hashes_bound"]


def test_member_substitution_rejected(snap_a, snap_b) -> None:
    # substitute a member row with a foreign (but valid-looking) member's hashes
    _, build_b, _, sha_b = _build(snap_b)
    _, mb = _honest_db(build_b, sha_b)
    _, build_a, _, sha_a = _build(snap_a)
    _, ma = _honest_db(build_a, sha_a)
    ma[0] = mb[0]  # foreign member substituted at index 0
    assert not _checks(snap_a, members=ma)["member_order_and_hashes_bound"]


def test_consistently_rehashed_attack_rejected(snap_a, snap_b) -> None:
    """A fully internally-consistent WRONG matrix (built honestly from snap_b) presented
    as snap_a's operational state is rejected: the verifier reconstructs snap_a's identity
    independently from the accepted snapshot + real payloads, so every attacker hash
    (matrix_hash, artifact_sha256, member vector/value hashes, artifact bytes) — although
    self-consistent — fails the cross-binding."""
    _, build_b, payload_b, sha_b = _build(snap_b)
    row_b, members_b = _honest_db(build_b, sha_b)  # all hashes internally consistent
    checks = _checks(snap_a, row=row_b, members=members_b, artifact=payload_b)
    # multiple independent bindings reject it (not merely one hash field)
    assert not checks["db_matrix_hash_matches_derived"]
    assert not checks["db_artifact_sha_matches_derived"]
    assert not checks["physical_artifact_sha_matches"]
    assert not checks["member_order_and_hashes_bound"]
    assert not all(checks.values())
