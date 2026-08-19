"""L2-D end-to-end ingestion on real PG16 with REAL Layer 1 artifacts.

Covers the owner-mandated correction cases: exact-byte hashing in the trusted boundary,
idempotency + content/profile-id conflicts, artifact-metadata conflicts, epoch-membership
enforcement, parquet row-identity/coordinate violations, explicit snapshot version
selection, and atomic ADMITTED audit.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Engine, text

from minos_engine.common.errors import (
    AdmissionRejectedError,
    ArtifactMetadataConflictError,
    ContentConflictError,
    ContractValidationError,
    EpochMembershipError,
    IngestionError,
)
from minos_engine.storage.profile_ingest import freeze_profile_snapshot, ingest_profile


def _ingest(env: dict[str, Any], engine: Engine, **overrides: Any):
    kwargs: dict[str, Any] = {
        "epoch": 1,
        "profile_json_path": env["profile_path"],
        "manifest_json_path": env["manifest_path"],
        "windows_parquet_path": env["windows_path"],
        "attestation": env["attestation"],
        "profile_artifact_uri": "file://profile.json",
        "manifest_artifact_uri": "file://manifest.json",
        "windows_artifact_uri": "file://windows.parquet",
    }
    kwargs.update(overrides)
    return ingest_profile(engine, **kwargs)


@pytest.fixture(scope="module")
def admitted(l2d_env: dict[str, Any], l2d_engine: Engine):
    return _ingest(l2d_env, l2d_engine)


def test_real_profile_admitted_with_three_artifacts(admitted, l2d_engine: Engine) -> None:
    with l2d_engine.connect() as c:
        row = c.execute(
            text(
                "SELECT profile_status, m5_status, integrity_degraded, feature_values_hash,"
                " profile_sha256, profile_manifest_sha256, windows_sha256,"
                " profile_artifact_id, profile_manifest_artifact_id, windows_artifact_id,"
                " ingestion_key, content_hash FROM profiling.bam_profiles WHERE id = :i"
            ),
            {"i": admitted.row_id},
        ).one()
        arts = c.execute(
            text(
                "SELECT provenance, media_type, size_bytes FROM catalog.artifacts "
                "WHERE id IN (:a, :b, :c)"
            ),
            {
                "a": row.profile_artifact_id,
                "b": row.profile_manifest_artifact_id,
                "c": row.windows_artifact_id,
            },
        ).all()
    assert admitted.idempotent is False
    assert row.profile_status == "COMPLETE"
    assert row.m5_status == "ABSENT" and row.integrity_degraded is True
    # three-artifact contract: refs + byte hashes all non-null, kinds/media/sizes set.
    assert all(
        len(h) == 64
        for h in (
            row.profile_sha256,
            row.profile_manifest_sha256,
            row.windows_sha256,
            row.ingestion_key,
        )
    )
    assert len(arts) == 3
    kinds = {a.provenance for a in arts}
    assert kinds == {"l2d:profile-json", "l2d:profile-manifest-json", "l2d:window-parquet"}
    assert all(a.size_bytes and a.media_type for a in arts)


def test_exact_bytes_hashed_in_boundary(
    admitted, l2d_env: dict[str, Any], l2d_engine: Engine
) -> None:
    """The stored hashes equal fresh recomputation over the exact artifact bytes."""
    import hashlib

    with l2d_engine.connect() as c:
        row = c.execute(
            text(
                "SELECT profile_sha256, profile_manifest_sha256 "
                "FROM profiling.bam_profiles WHERE id = :i"
            ),
            {"i": admitted.row_id},
        ).one()
    assert (
        row.profile_sha256 == hashlib.sha256(Path(l2d_env["profile_path"]).read_bytes()).hexdigest()
    )
    assert (
        row.profile_manifest_sha256
        == hashlib.sha256(Path(l2d_env["manifest_path"]).read_bytes()).hexdigest()
    )


def test_idempotent_resubmission_returns_existing_row(
    admitted, l2d_env: dict[str, Any], l2d_engine: Engine
) -> None:
    again = _ingest(l2d_env, l2d_engine)
    assert again.idempotent is True and again.row_id == admitted.row_id
    with l2d_engine.connect() as c:
        n = c.execute(text("SELECT count(*) FROM profiling.bam_profiles")).scalar()
    assert n == 1  # no duplicate accepted row


def test_content_conflict_rejected(
    admitted, l2d_env: dict[str, Any], l2d_engine: Engine, tmp_path: Path
) -> None:
    """Same ingestion key (identity+profile_id) with different bytes -> CONTENT_CONFLICT."""
    doc = json.loads(Path(l2d_env["profile_path"]).read_text(encoding="utf-8"))
    doc["warnings"] = ["tampered-but-plausible"]  # changes bytes, keeps profile_id
    mutated = tmp_path / "profile.json"
    mutated.write_text(json.dumps(doc), encoding="utf-8")
    with pytest.raises((ContentConflictError, AdmissionRejectedError, IngestionError)):
        _ingest(l2d_env, l2d_engine, profile_json_path=mutated)


def test_artifact_metadata_conflict_rejected(
    admitted, l2d_env: dict[str, Any], l2d_engine: Engine
) -> None:
    """An existing artifact sha row with different kind/media/size is never reused."""
    from minos_engine.storage.profile_ingest import _register_artifact

    with l2d_engine.begin() as c, pytest.raises(ArtifactMetadataConflictError):
        _register_artifact(
            c,
            uri="file://other",
            sha256=l2d_env["profile_sha256"],
            size_bytes=1,  # wrong size for the existing profile-json artifact row
            media_type="application/json",
            kind="l2d:profile-json",
        )


def test_non_member_epoch_ingestion_rejected(
    admitted, l2d_env: dict[str, Any], l2d_engine: Engine
) -> None:
    """Ingesting into an epoch the dataset is not allocated in fails closed."""
    with pytest.raises((EpochMembershipError, IngestionError, ContractValidationError)):
        _ingest(l2d_env, l2d_engine, epoch=2)


def test_parquet_row_identity_violation_rejected(
    admitted, l2d_env: dict[str, Any], l2d_engine: Engine, tmp_path: Path
) -> None:
    """A parquet with a wrong per-row profile_id is rejected despite valid schema."""
    import pyarrow as pa
    import pyarrow.parquet as pq

    table = pq.read_table(l2d_env["windows_path"])
    idx = table.schema.get_field_index("profile_id")
    bad = table.set_column(
        idx, "profile_id", pa.array(["not-the-profile"] * table.num_rows, pa.string())
    )
    bad_path = tmp_path / "windows.parquet"
    pq.write_table(bad, bad_path)
    with pytest.raises((AdmissionRejectedError, IngestionError)):
        _ingest(l2d_env, l2d_engine, windows_parquet_path=bad_path)


def test_parquet_extra_column_rejected(
    admitted, l2d_env: dict[str, Any], l2d_engine: Engine, tmp_path: Path
) -> None:
    import pyarrow as pa
    import pyarrow.parquet as pq

    table = pq.read_table(l2d_env["windows_path"])
    extra = table.append_column("smuggled", pa.array([1.0] * table.num_rows))
    bad_path = tmp_path / "windows_extra.parquet"
    pq.write_table(extra, bad_path)
    with pytest.raises((AdmissionRejectedError, IngestionError)):
        _ingest(l2d_env, l2d_engine, windows_parquet_path=bad_path)


def test_admitted_audit_is_atomic_with_row(admitted, l2d_engine: Engine) -> None:
    """Every accepted row has its ADMITTED attempt record (same-transaction commit)."""
    with l2d_engine.connect() as c:
        accepted = c.execute(text("SELECT count(*) FROM profiling.bam_profiles")).scalar()
        audited = c.execute(
            text(
                "SELECT count(DISTINCT content_hash) FROM profiling.profile_ingest_attempts "
                "WHERE outcome = 'ADMITTED' AND content_hash IS NOT NULL"
            )
        ).scalar()
    assert accepted is not None and audited is not None and audited >= accepted


def test_rejected_attempts_logged(admitted, l2d_engine: Engine) -> None:
    with l2d_engine.connect() as c:
        rejected = c.execute(
            text(
                "SELECT count(*) FROM profiling.profile_ingest_attempts WHERE outcome = 'REJECTED'"
            )
        ).scalar()
    assert rejected is not None and rejected >= 1


def test_freeze_requires_explicit_version_selection(admitted, l2d_engine: Engine) -> None:
    from tests.integration.layer2_ingest.conftest import DATASET_ID

    # missing/extra selections fail closed
    with l2d_engine.begin() as c, pytest.raises(ContractValidationError):
        freeze_profile_snapshot(c, 1, {})
    with l2d_engine.begin() as c, pytest.raises(ContractValidationError):
        freeze_profile_snapshot(c, 1, {DATASET_ID: "0" * 64, "ghost": "1" * 64})
    # a selection naming a non-existent content hash fails closed
    with l2d_engine.begin() as c, pytest.raises(ContractValidationError):
        freeze_profile_snapshot(c, 1, {DATASET_ID: "0" * 64})


def test_freeze_profile_snapshot_epoch1(admitted, l2d_engine: Engine) -> None:
    from tests.integration.layer2_ingest.conftest import DATASET_ID

    with l2d_engine.connect() as c:
        content_hash = c.execute(
            text("SELECT content_hash FROM profiling.bam_profiles WHERE id = :i"),
            {"i": admitted.row_id},
        ).scalar_one()
    with l2d_engine.begin() as c:
        snapshot_id = freeze_profile_snapshot(c, 1, {DATASET_ID: content_hash})
    with l2d_engine.connect() as c:
        snap = c.execute(
            text("SELECT epoch, member_count FROM profiling.profile_snapshots WHERE id = :i"),
            {"i": snapshot_id},
        ).one()
        members = c.execute(
            text("SELECT count(*) FROM profiling.profile_snapshot_members")
        ).scalar()
    assert tuple(snap) == (1, 1) and members == 1  # count derives from epoch sample_count


def test_refreeze_same_epoch_rejected(admitted, l2d_engine: Engine) -> None:
    from sqlalchemy.exc import DBAPIError

    from tests.integration.layer2_ingest.conftest import DATASET_ID

    with l2d_engine.connect() as c:
        content_hash = c.execute(
            text("SELECT content_hash FROM profiling.bam_profiles WHERE id = :i"),
            {"i": admitted.row_id},
        ).scalar_one()
    with l2d_engine.begin() as c, pytest.raises(DBAPIError):
        freeze_profile_snapshot(c, 1, {DATASET_ID: content_hash})


def test_tables_append_only(admitted, l2d_engine: Engine) -> None:
    from sqlalchemy.exc import DatabaseError

    for sql in (
        "UPDATE profiling.bam_profiles SET profile_id = 'x'",
        "DELETE FROM profiling.bam_profiles",
        "UPDATE profiling.profile_snapshot_members SET partition = 'test'",
        "DELETE FROM profiling.profile_snapshots",
    ):
        with l2d_engine.connect() as c, pytest.raises(DatabaseError):
            c.execute(text(sql))
            c.commit()
