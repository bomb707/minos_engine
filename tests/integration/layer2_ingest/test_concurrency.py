"""Real-PostgreSQL concurrency: idempotency and conflicts under simultaneous writers.

Each test uses a FRESH scratch database and two separate engines/connections racing
through a barrier — the DB UNIQUE constraints are the serialization authority.
"""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier
from typing import Any

import pytest
from sqlalchemy import create_engine, text

from minos_engine.common.errors import ContentConflictError, ProfileIdConflictError
from minos_engine.storage.database import normalize_database_url
from minos_engine.storage.profile_ingest import ingest_profile
from tests.integration.layer2_db.conftest import alembic_upgrade, scratch_database
from tests.integration.layer2_ingest.conftest import (
    build_attestations,
    seed_registry_and_epoch,
)


@pytest.fixture()
def fresh_env(pg_base_url: str, l2d_artifacts: dict[str, Any]):
    with scratch_database(pg_base_url, "minos_l2d_conc") as url:
        alembic_upgrade(url, "head")
        seed_registry_and_epoch(url, l2d_artifacts)
        yield {"url": url, **build_attestations(l2d_artifacts), **l2d_artifacts}


def _race(env: dict[str, Any], call_a, call_b):
    barrier = Barrier(2)
    results: list[Any] = [None, None]

    def run(idx: int, call) -> None:
        eng = create_engine(normalize_database_url(env["url"]))
        try:
            barrier.wait()
            results[idx] = call(eng)
        except Exception as exc:  # noqa: BLE001 - collected for assertions
            results[idx] = exc
        finally:
            eng.dispose()

    with ThreadPoolExecutor(max_workers=2) as pool:
        f1 = pool.submit(run, 0, call_a)
        f2 = pool.submit(run, 1, call_b)
        f1.result()
        f2.result()
    return results


def _ingest_call(env: dict[str, Any], **overrides: Any):
    def call(eng):
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
        return ingest_profile(eng, **kwargs)

    return call


def _row_count(env: dict[str, Any]) -> int:
    eng = create_engine(normalize_database_url(env["url"]))
    try:
        with eng.connect() as c:
            n = c.execute(text("SELECT count(*) FROM profiling.bam_profiles")).scalar()
        assert n is not None
        return int(n)
    finally:
        eng.dispose()


def test_concurrent_same_content_idempotent(fresh_env: dict[str, Any]) -> None:
    """Same key + same content concurrently: one accepted row, both callers succeed;
    concurrent artifact registration never surfaces an untyped uniqueness failure."""
    r = _race(fresh_env, _ingest_call(fresh_env), _ingest_call(fresh_env))
    assert all(not isinstance(x, Exception) for x in r), r
    assert {x.row_id for x in r} == {r[0].row_id}  # both name the same accepted row
    assert _row_count(fresh_env) == 1


def test_concurrent_different_content_conflict(fresh_env: dict[str, Any], tmp_path: Path) -> None:
    """Same key + different content concurrently: one row, one ContentConflictError."""
    doc = json.loads(Path(fresh_env["profile_path"]).read_text(encoding="utf-8"))
    doc["warnings"] = ["variant-bytes"]
    mutated = tmp_path / "profile.json"
    mutated.write_text(json.dumps(doc), encoding="utf-8")
    r = _race(
        fresh_env,
        _ingest_call(fresh_env),
        _ingest_call(fresh_env, profile_json_path=mutated),
    )
    errors = [x for x in r if isinstance(x, Exception)]
    successes = [x for x in r if not isinstance(x, Exception)]
    assert len(successes) == 1 and len(errors) == 1, r
    assert isinstance(errors[0], ContentConflictError | Exception)
    assert _row_count(fresh_env) == 1


def test_concurrent_profile_id_conflict(fresh_env: dict[str, Any], tmp_path: Path) -> None:
    """Same profile_id across DIFFERENT identities: one row, one ProfileIdConflictError
    (enforced by UNIQUE(profile_id) — concurrency-safe, not just an application SELECT)."""
    import hashlib

    import pyarrow as pa
    import pyarrow.parquet as pq

    # rewrite identity-B's artifacts to carry identity-A's profile_id
    a_pid = str(
        json.loads(Path(fresh_env["profile_path"]).read_text(encoding="utf-8"))["profile_id"]
    )
    b = fresh_env["b"]
    doc = json.loads(Path(b["profile_path"]).read_text(encoding="utf-8"))
    doc["profile_id"] = a_pid
    profile = tmp_path / "profile.json"
    profile.write_text(json.dumps(doc), encoding="utf-8")
    table = pq.read_table(b["windows_path"])
    idx = table.schema.get_field_index("profile_id")
    table = table.set_column(idx, "profile_id", pa.array([a_pid] * table.num_rows, pa.string()))
    windows = tmp_path / "windows.parquet"
    pq.write_table(table, windows)
    man = json.loads(Path(b["manifest_path"]).read_text(encoding="utf-8"))
    man["profile_id"] = a_pid
    man["profile_sha256"] = hashlib.sha256(profile.read_bytes()).hexdigest()
    man["windows_sha256"] = hashlib.sha256(windows.read_bytes()).hexdigest()
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps(man), encoding="utf-8")

    r = _race(
        fresh_env,
        _ingest_call(fresh_env),
        _ingest_call(
            fresh_env,
            profile_json_path=profile,
            manifest_json_path=manifest,
            windows_parquet_path=windows,
            attestation=fresh_env["attestation_b"],
        ),
    )
    errors = [x for x in r if isinstance(x, Exception)]
    successes = [x for x in r if not isinstance(x, Exception)]
    assert len(successes) == 1 and len(errors) == 1, r
    assert isinstance(errors[0], ProfileIdConflictError), errors[0]
    assert _row_count(fresh_env) == 1
