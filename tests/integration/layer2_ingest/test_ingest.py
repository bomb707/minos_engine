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


@pytest.fixture(scope="module")
def admitted_b(l2d_env: dict[str, Any], l2d_engine: Engine):
    b = l2d_env["b"]
    return _ingest(
        l2d_env,
        l2d_engine,
        profile_json_path=b["profile_path"],
        manifest_json_path=b["manifest_path"],
        windows_parquet_path=b["windows_path"],
        attestation=l2d_env["attestation_b"],
    )


def _rewrite_artifacts(env: dict[str, Any], tmp: Path, *, source: str, new_profile_id: str):
    """Copy an artifact set with a NEW profile_id, consistently re-hashed (valid set)."""
    import hashlib

    import pyarrow as pa
    import pyarrow.parquet as pq

    src = env if source == "a" else env["b"]
    doc = json.loads(Path(src["profile_path"]).read_text(encoding="utf-8"))
    doc["profile_id"] = new_profile_id
    profile = tmp / "profile.json"
    profile.write_text(json.dumps(doc), encoding="utf-8")
    table = pq.read_table(src["windows_path"])
    idx = table.schema.get_field_index("profile_id")
    table = table.set_column(
        idx, "profile_id", pa.array([new_profile_id] * table.num_rows, pa.string())
    )
    windows = tmp / "windows.parquet"
    pq.write_table(table, windows)
    man = json.loads(Path(src["manifest_path"]).read_text(encoding="utf-8"))
    man["profile_id"] = new_profile_id
    man["profile_sha256"] = hashlib.sha256(profile.read_bytes()).hexdigest()
    man["windows_sha256"] = hashlib.sha256(windows.read_bytes()).hexdigest()
    manifest = tmp / "manifest.json"
    manifest.write_text(json.dumps(man), encoding="utf-8")
    return profile, manifest, windows


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
    """Same ingestion key with DIFFERENT (independently valid) content -> typed conflict."""
    import hashlib

    doc = json.loads(Path(l2d_env["profile_path"]).read_text(encoding="utf-8"))
    doc["warnings"] = ["variant-bytes"]  # changes bytes, keeps profile_id/identity
    mutated = tmp_path / "profile.json"
    mutated.write_text(json.dumps(doc), encoding="utf-8")
    man = json.loads(Path(l2d_env["manifest_path"]).read_text(encoding="utf-8"))
    man["profile_sha256"] = hashlib.sha256(mutated.read_bytes()).hexdigest()
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps(man), encoding="utf-8")
    with pytest.raises(ContentConflictError):  # strictly typed — never a generic reject
        _ingest(l2d_env, l2d_engine, profile_json_path=mutated, manifest_json_path=manifest)


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


def _content_hash(engine: Engine, row_id: str) -> str:
    with engine.connect() as c:
        return c.execute(
            text("SELECT content_hash FROM profiling.bam_profiles WHERE id = :i"),
            {"i": row_id},
        ).scalar_one()


def test_new_immutable_version_appends(
    admitted, l2d_env: dict[str, Any], l2d_engine: Engine, tmp_path: Path
) -> None:
    """Version B (new profile_id) for the SAME identity appends; both rows retained."""
    profile, manifest, windows = _rewrite_artifacts(
        l2d_env, tmp_path, source="a", new_profile_id="b" * 32
    )
    out = _ingest(
        l2d_env,
        l2d_engine,
        profile_json_path=profile,
        manifest_json_path=manifest,
        windows_parquet_path=windows,
    )
    assert out.idempotent is False and out.row_id != admitted.row_id
    with l2d_engine.connect() as c:
        versions = c.execute(
            text(
                "SELECT count(*) FROM profiling.bam_profiles bp "
                "JOIN catalog.dataset_registry dr ON dr.id = bp.dataset_registry_id "
                "WHERE dr.dataset_id = :d"
            ),
            {
                "d": __import__(
                    "tests.integration.layer2_ingest.conftest", fromlist=["DATASET_ID"]
                ).DATASET_ID
            },
        ).scalar()
    assert versions == 2  # A and B, append-only


def test_freeze_requires_explicit_version_selection(
    admitted, admitted_b, l2d_engine: Engine
) -> None:
    from tests.integration.layer2_ingest.conftest import DATASET_ID, DATASET_ID_B

    a_hash = _content_hash(l2d_engine, admitted.row_id)
    # missing/extra selections fail closed
    with l2d_engine.begin() as c, pytest.raises(ContractValidationError):
        freeze_profile_snapshot(c, 1, {})
    with l2d_engine.begin() as c, pytest.raises(ContractValidationError):
        freeze_profile_snapshot(c, 1, {DATASET_ID: a_hash})  # missing B
    with l2d_engine.begin() as c, pytest.raises(ContractValidationError):
        freeze_profile_snapshot(
            c, 1, {DATASET_ID: a_hash, DATASET_ID_B: "0" * 64, "ghost": "1" * 64}
        )
    # a selection naming a non-existent content hash fails closed
    with l2d_engine.begin() as c, pytest.raises(ContractValidationError):
        freeze_profile_snapshot(c, 1, {DATASET_ID: "0" * 64, DATASET_ID_B: "1" * 64})


def test_freeze_profile_snapshot_epoch1(admitted, admitted_b, l2d_engine: Engine) -> None:
    """Freeze explicitly selects version A (of A/B) by content hash for identity 1."""
    from tests.integration.layer2_ingest.conftest import DATASET_ID, DATASET_ID_B

    selections = {
        DATASET_ID: _content_hash(l2d_engine, admitted.row_id),  # explicit: version A
        DATASET_ID_B: _content_hash(l2d_engine, admitted_b.row_id),
    }
    with l2d_engine.begin() as c:
        snapshot_id = freeze_profile_snapshot(c, 1, selections)
    with l2d_engine.connect() as c:
        snap = c.execute(
            text("SELECT epoch, member_count FROM profiling.profile_snapshots WHERE id = :i"),
            {"i": snapshot_id},
        ).one()
        members = (
            c.execute(
                text("SELECT bam_profile_id FROM profiling.profile_snapshot_members ORDER BY 1")
            )
            .scalars()
            .all()
        )
    assert tuple(snap) == (1, 2) and len(members) == 2  # count from epoch sample_count
    assert admitted.row_id in {str(m) for m in members}  # version A frozen, not B


def test_frozen_snapshot_remains_unchanged(
    admitted, l2d_env: dict[str, Any], l2d_engine: Engine
) -> None:
    """Later activity (idempotent resubmission) never mutates a frozen snapshot."""
    with l2d_engine.connect() as c:
        before = c.execute(
            text("SELECT snapshot_hash, member_count FROM profiling.profile_snapshots")
        ).one()
    _ingest(l2d_env, l2d_engine)  # idempotent resubmission of version A
    with l2d_engine.connect() as c:
        after = c.execute(
            text("SELECT snapshot_hash, member_count FROM profiling.profile_snapshots")
        ).one()
        members = c.execute(
            text("SELECT count(*) FROM profiling.profile_snapshot_members")
        ).scalar()
    assert tuple(before) == tuple(after) and members == 2


def test_refreeze_same_epoch_rejected(admitted, admitted_b, l2d_engine: Engine) -> None:
    from sqlalchemy.exc import DBAPIError

    from tests.integration.layer2_ingest.conftest import DATASET_ID, DATASET_ID_B

    selections = {
        DATASET_ID: _content_hash(l2d_engine, admitted.row_id),
        DATASET_ID_B: _content_hash(l2d_engine, admitted_b.row_id),
    }
    with l2d_engine.begin() as c, pytest.raises(DBAPIError):
        freeze_profile_snapshot(c, 1, selections)


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


def _mutate_parquet(env: dict[str, Any], tmp: Path, mutate):
    import pyarrow.parquet as pq

    table = pq.read_table(env["windows_path"])
    out = tmp / "windows.parquet"
    pq.write_table(mutate(table), out)
    return out


def _set_cols(t, **updates):
    import pyarrow as pa

    types = {
        "window_id": pa.int32(),
        "start0": pa.int64(),
        "end0": pa.int64(),
        "length_bp": pa.int64(),
    }
    for name, values in updates.items():
        i = t.schema.get_field_index(name)
        t = t.set_column(i, name, pa.array(values, types[name]))
    return t


def test_parquet_duplicate_rows_rejected(
    admitted, l2d_env: dict[str, Any], l2d_engine: Engine, tmp_path: Path
) -> None:
    """A duplicated row WITHIN the declared row count reaches the sequence invariant."""

    def dup_row(t):
        w = t.column("window_id").to_pylist()
        s0 = t.column("start0").to_pylist()
        e0 = t.column("end0").to_pylist()
        ln = t.column("length_bp").to_pylist()
        w[1], s0[1], e0[1], ln[1] = w[0], s0[0], e0[0], ln[0]  # row 1 := copy of row 0
        return _set_cols(t, window_id=w, start0=s0, end0=e0, length_bp=ln)

    bad = _mutate_parquet(l2d_env, tmp_path, dup_row)
    with pytest.raises(AdmissionRejectedError, match="strictly increasing"):
        _ingest(l2d_env, l2d_engine, windows_parquet_path=bad)


def test_parquet_shuffled_order_rejected(
    admitted, l2d_env: dict[str, Any], l2d_engine: Engine, tmp_path: Path
) -> None:
    """Reversed rows (same count) reach the window_id ordering invariant."""
    bad = _mutate_parquet(
        l2d_env,
        tmp_path,
        lambda t: t.take(list(range(t.num_rows - 1, -1, -1))),
    )
    with pytest.raises(AdmissionRejectedError, match="strictly increasing"):
        _ingest(l2d_env, l2d_engine, windows_parquet_path=bad)


def test_parquet_overlap_rejected(
    admitted, l2d_env: dict[str, Any], l2d_engine: Engine, tmp_path: Path
) -> None:
    """Overlapping coordinates with CONSISTENT length_bp reject specifically as overlap."""

    def overlap(t):
        s0 = t.column("start0").to_pylist()
        e0 = t.column("end0").to_pylist()
        ln = t.column("length_bp").to_pylist()
        # window 1 restarts inside window 0; end unchanged; length kept consistent.
        s0[1] = s0[0] + max(1, (e0[0] - s0[0]) // 2)
        ln[1] = e0[1] - s0[1]
        return _set_cols(t, start0=s0, end0=e0, length_bp=ln)

    bad = _mutate_parquet(l2d_env, tmp_path, overlap)
    with pytest.raises(AdmissionRejectedError, match="overlap"):
        _ingest(l2d_env, l2d_engine, windows_parquet_path=bad)


def test_parquet_decreasing_coordinates_rejected(
    admitted, l2d_env: dict[str, Any], l2d_engine: Engine, tmp_path: Path
) -> None:
    """Coordinates that go BACKWARD while window_id stays strictly increasing."""

    def backward(t):
        s0 = t.column("start0").to_pylist()
        e0 = t.column("end0").to_pylist()
        ln = t.column("length_bp").to_pylist()
        # rows 0/1 swap coordinate blocks; window_id untouched (still increasing).
        s0[0], s0[1] = s0[1], s0[0]
        e0[0], e0[1] = e0[1], e0[0]
        ln[0], ln[1] = ln[1], ln[0]
        return _set_cols(t, start0=s0, end0=e0, length_bp=ln)

    bad = _mutate_parquet(l2d_env, tmp_path, backward)
    with pytest.raises(AdmissionRejectedError, match="overlap or are unsorted"):
        _ingest(l2d_env, l2d_engine, windows_parquet_path=bad)


def test_parquet_bad_window_id_rejected(
    admitted, l2d_env: dict[str, Any], l2d_engine: Engine, tmp_path: Path
) -> None:
    """A duplicate window index (coordinates untouched) reaches the id invariant."""

    def dup_ids(t):
        ids = t.column("window_id").to_pylist()
        ids[1] = ids[0]
        return _set_cols(t, window_id=ids)

    bad = _mutate_parquet(l2d_env, tmp_path, dup_ids)
    with pytest.raises(AdmissionRejectedError, match="strictly increasing"):
        _ingest(l2d_env, l2d_engine, windows_parquet_path=bad)


def test_fasta_length_bounds_rejected(admitted, l2d_env: dict[str, Any], tmp_path: Path) -> None:
    """Intake refuses a truncated FASTA whose contig no longer fits @SQ/region."""
    from minos_engine.common.errors import AttestationMismatchError
    from minos_engine.intake.attestation import attest_input
    from tests.integration.layer2_ingest.conftest import registry_record

    truncated = tmp_path / "chr18.fa"
    lines = Path(l2d_env["reference"]).read_text(encoding="utf-8").splitlines()
    truncated.write_text("\n".join(lines[: max(2, len(lines) // 2)]) + "\n", encoding="utf-8")
    record = dict(registry_record(l2d_env))
    record["reference_sha256"] = __import__("hashlib").sha256(truncated.read_bytes()).hexdigest()
    with pytest.raises((AttestationMismatchError, IngestionError)):
        attest_input(
            bam_path=l2d_env["bam"],
            bai_path=l2d_env["bai"],
            reference_path=truncated,
            fai_path=l2d_env["fai"],
            registry_record=record,
            registry_snapshot_hash="7" * 64,
        )
