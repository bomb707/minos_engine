"""L2-D end-to-end ingestion on real PG16 with REAL Layer 1 artifacts."""

from __future__ import annotations

import copy
from typing import Any

import pytest
from sqlalchemy import Engine, text

from minos_engine.common.errors import AdmissionRejectedError, IngestionError
from minos_engine.layer2.ingest.contracts import (
    canonical_feature_values_hash,
    extract_eligible_feature_values,
)
from minos_engine.storage.profile_ingest import freeze_profile_snapshot, ingest_profile


def _ingest(env: dict[str, Any], engine: Engine, **overrides: Any) -> str:
    kwargs: dict[str, Any] = {
        "epoch": 1,
        "profile_document": env["profile_document"],
        "manifest_document": env["manifest_document"],
        "attestation": env["attestation"],
        "profile_artifact_uri": "file://profile.json",
        "profile_artifact_sha256": env["profile_sha256"],
        "windows_artifact_uri": "file://windows.parquet",
        "windows_parquet_path": env["windows_path"],
    }
    kwargs.update(overrides)
    return ingest_profile(engine, **kwargs)


@pytest.fixture(scope="module")
def admitted_row_id(l2d_env: dict[str, Any], l2d_engine: Engine) -> str:
    """Admit the real profile once (session corpus for later tests)."""
    return _ingest(l2d_env, l2d_engine)


def test_real_profile_admitted(admitted_row_id: str, l2d_engine: Engine) -> None:
    with l2d_engine.connect() as c:
        row = c.execute(
            text(
                "SELECT profile_status, m5_status, integrity_degraded, feature_values_hash, "
                " l1_feature_values_hash, eligible_value_count, content_hash "
                "FROM profiling.bam_profiles WHERE id = :i"
            ),
            {"i": admitted_row_id},
        ).one()
    assert row.profile_status == "COMPLETE"
    # synthetic fixture BAM header carries no @SQ M5 -> ABSENT admits with the flag.
    assert row.m5_status == "ABSENT" and row.integrity_degraded is True
    assert len(row.feature_values_hash) == 64 and len(row.l1_feature_values_hash) == 64
    assert row.eligible_value_count > 0


def test_feature_hash_rederivable_from_stored_jsonb(
    admitted_row_id: str, l2d_engine: Engine
) -> None:
    """4-way equality: the typed column equals a fresh recompute over the stored JSONB."""
    with l2d_engine.connect() as c:
        doc, stored = c.execute(
            text(
                "SELECT profile_document, feature_values_hash "
                "FROM profiling.bam_profiles WHERE id = :i"
            ),
            {"i": admitted_row_id},
        ).one()
    assert canonical_feature_values_hash(extract_eligible_feature_values(doc)) == stored


def test_admitted_attempt_recorded(admitted_row_id: str, l2d_engine: Engine) -> None:
    with l2d_engine.connect() as c:
        outcomes = (
            c.execute(text("SELECT outcome FROM profiling.profile_ingest_attempts")).scalars().all()
        )
    assert "ADMITTED" in outcomes


def test_duplicate_content_rejected_and_attempt_logged(
    admitted_row_id: str, l2d_env: dict[str, Any], l2d_engine: Engine
) -> None:
    from sqlalchemy.exc import DBAPIError

    with pytest.raises((DBAPIError, IngestionError)):
        _ingest(l2d_env, l2d_engine)  # same content_hash -> UNIQUE violation
    with l2d_engine.connect() as c:
        rejected = c.execute(
            text(
                "SELECT count(*) FROM profiling.profile_ingest_attempts WHERE outcome = 'REJECTED'"
            )
        ).scalar()
    assert rejected is not None and rejected >= 1


def test_tampered_document_rejected(
    admitted_row_id: str, l2d_env: dict[str, Any], l2d_engine: Engine
) -> None:
    """A mutated profile document breaks artifact byte-binding -> rejected."""
    doc = copy.deepcopy(l2d_env["profile_document"])
    doc["coverage"]["mean_depth_reads_per_base"] = 999.0
    with pytest.raises((AdmissionRejectedError, IngestionError)):
        _ingest(l2d_env, l2d_engine, profile_document=doc)


def test_incomplete_profile_rejected(
    admitted_row_id: str, l2d_env: dict[str, Any], l2d_engine: Engine
) -> None:
    doc = copy.deepcopy(l2d_env["profile_document"])
    doc["status"] = "PARTIAL"
    doc["completion"]["status"] = "PARTIAL"
    with pytest.raises((AdmissionRejectedError, IngestionError)):
        _ingest(l2d_env, l2d_engine, profile_document=doc)


def test_freeze_profile_snapshot_epoch1(admitted_row_id: str, l2d_engine: Engine) -> None:
    with l2d_engine.begin() as c:
        snapshot_id = freeze_profile_snapshot(c, 1)
    with l2d_engine.connect() as c:
        snap = c.execute(
            text(
                "SELECT epoch, member_count, split_manifest_hash, registry_snapshot_hash "
                "FROM profiling.profile_snapshots WHERE id = :i"
            ),
            {"i": snapshot_id},
        ).one()
        members = c.execute(
            text("SELECT count(*) FROM profiling.profile_snapshot_members")
        ).scalar()
    # member_count derives from the epoch's sample_count (1 here) — never hardcoded.
    assert tuple(snap)[:2] == (1, 1) and members == 1


def test_refreeze_same_epoch_rejected(admitted_row_id: str, l2d_engine: Engine) -> None:
    from sqlalchemy.exc import DBAPIError

    with l2d_engine.begin() as c, pytest.raises(DBAPIError):
        freeze_profile_snapshot(c, 1)  # UNIQUE(epoch) forbids a second freeze


def test_snapshot_tables_append_only(admitted_row_id: str, l2d_engine: Engine) -> None:
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
