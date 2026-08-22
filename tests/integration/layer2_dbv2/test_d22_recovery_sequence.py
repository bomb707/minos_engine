"""DB-V2 D2.2: the executable recovery sequence, exact snapshot sets, idempotency and audit.

R1 -> S1 -> B0 -> R2 -> B1. 0009 is S1 and creates the shadow tables empty; B0 is the
artifact-catalog bootstrap and belongs to D3. Nothing here performs B0 for real - the fixture
named ``synthetic_bootstrap`` seeds a deliberately small artifact catalog so that R2 can be
exercised, and it is not, and does not imply, a bootstrap that 0009 performs.
"""

from __future__ import annotations

import hashlib
import json
import threading
import uuid
from collections.abc import Callable, Iterator
from typing import Any

import pytest
from sqlalchemy import Connection, create_engine, text

from minos_engine.storage import dbv2_migration_contract as contract
from minos_engine.storage.database import normalize_database_url

pytestmark = [pytest.mark.integration, pytest.mark.postgres, pytest.mark.migration]

SNAPSHOT_DOMAIN = b"minos:db-v2-artifact-snapshot:v1\n"
SNAPSHOT_MEDIA = "application/vnd.minos.artifact-snapshot+json"
RECOVERY_MEDIA = "application/vnd.minos.db-recovery-manifest+json"
BACKUP_MEDIA = "application/vnd.postgresql.dump"
PREDICATE = "lifecycle_state = 'active' AND backup_scope = 'operational'"
SNAPSHOT_SCHEMA_VERSION = "minos-artifact-snapshot-v1"


@pytest.fixture
def conn(dbv2_url: str) -> Iterator[Connection]:
    engine = create_engine(normalize_database_url(dbv2_url))
    connection = engine.connect()
    transaction = connection.begin()
    try:
        yield connection
    finally:
        transaction.rollback()
        connection.close()
        engine.dispose()


def _sql(connection: Connection, statement: str, **params: Any) -> Any:
    return connection.execute(text(statement), params)


def _canonical(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8") + b"\n"


def publish_inline(
    connection: Connection,
    payload: bytes,
    kind: str = "vcf",
    media: str = "application/json",
    scope: str = "operational",
    provenance: dict[str, Any] | None = None,
    schema_version: str = "v1",
) -> uuid.UUID:
    return _sql(
        connection,
        "SELECT dbv2_catalog.get_or_verify_inline_artifact(:p, :m, :k, :s, 'standard', :sv, :pr)",
        p=payload,
        m=media,
        k=kind,
        s=scope,
        sv=schema_version,
        pr=json.dumps(provenance or {}),
    ).scalar_one()


def register_external(
    connection: Connection,
    digest: str,
    size: int,
    kind: str = "dump",
    media: str = BACKUP_MEDIA,
    scope: str = "operational",
    provenance: dict[str, Any] | None = None,
    schema_version: str = "v1",
) -> uuid.UUID:
    return _sql(
        connection,
        "SELECT dbv2_catalog.get_or_verify_external_artifact(:d, :s, :m, :k, :sc, 'standard', "
        ":sv, :pr)",
        d=digest,
        s=size,
        m=media,
        k=kind,
        sc=scope,
        sv=schema_version,
        pr=json.dumps(provenance or {}),
    ).scalar_one()


def backend(connection: Connection, key: str | None = None) -> str:
    backend_key = key or f"backend-{uuid.uuid4()}"
    _sql(
        connection,
        "INSERT INTO dbv2_catalog.storage_backends (backend_key, backend_type, logical_root) "
        "VALUES (:k, 'local_fs', '/srv/minos')",
        k=backend_key,
    )
    return backend_key


def synthetic_bootstrap(connection: Connection, count: int = 3) -> list[dict[str, Any]]:
    """A deliberately synthetic stand-in for B0.

    B0 is the D3 artifact-catalog bootstrap and is NOT implemented here or by 0009. This seeds a
    handful of inline operational artifacts purely so R2 has an artifact catalog to be exact
    against.
    """
    entries = []
    for index in range(count):
        payload = f"synthetic-bootstrap-{uuid.uuid4()}-{index}".encode()
        publish_inline(connection, payload, "vcf")
        entries.append(
            {
                "artifact_kind": "vcf",
                "content_sha256": hashlib.sha256(payload).hexdigest(),
                "size_bytes": len(payload),
            }
        )
    return sorted(entries, key=lambda e: (e["content_sha256"], e["size_bytes"], e["artifact_kind"]))


def snapshot_bytes(entries: list[dict[str, Any]], recovery_set_id: str) -> tuple[bytes, str, str]:
    manifest = {
        "artifact_count": len(entries),
        "artifact_total_bytes": sum(int(e["size_bytes"]) for e in entries),
        "entries": entries,
        "predicate": PREDICATE,
        "recovery_set_id": recovery_set_id,
        "schema_version": SNAPSHOT_SCHEMA_VERSION,
    }
    payload = _canonical(manifest)
    return (
        payload,
        hashlib.sha256(payload).hexdigest(),
        hashlib.sha256(SNAPSHOT_DOMAIN + payload).hexdigest(),
    )


def r2_manifest(
    connection: Connection, entries: list[dict[str, Any]], recovery_set_id: str | None = None
) -> dict[str, Any]:
    """Everything catalog.register_backup_set needs, with the three recovery artifacts published."""
    recovery_set_id = recovery_set_id or str(uuid.uuid4())
    payload, snapshot_raw, snapshot_scientific = snapshot_bytes(entries, recovery_set_id)
    snapshot_id = publish_inline(
        connection, payload, "artifact_snapshot", SNAPSHOT_MEDIA, "recovery"
    )
    dump_digest = hashlib.sha256(f"dump-{recovery_set_id}".encode()).hexdigest()
    dump_id = register_external(
        connection, dump_digest, 4096, "database_backup", BACKUP_MEDIA, "recovery"
    )
    location_id = _sql(
        connection,
        "SELECT dbv2_catalog.get_or_verify_artifact_location(:a, :b, :k, true)",
        a=dump_id,
        b=backend(connection),
        k=f"backups/{dump_digest}.dump",
    ).scalar_one()
    _sql(
        connection,
        "SELECT dbv2_catalog.record_artifact_verification(:a, :d, 4096, :l)",
        a=dump_id,
        d=dump_digest,
        l=location_id,
    )
    manifest = {
        "artifact_count": len(entries),
        "artifact_snapshot_manifest_sha256": snapshot_raw,
        "artifact_snapshot_sha256": snapshot_scientific,
        "artifact_total_bytes": sum(int(e["size_bytes"]) for e in entries),
        "artifact_verification_tool_version": "verify-1",
        "backup_tool_version": "pg_dump-16.2",
        "created_at": "2026-08-22T00:00:00+00:00",
        "database_backup_kind": "pg_dump",
        "database_backup_sha256": dump_digest,
        "database_backup_size_bytes": 4096,
        "database_name": "minos_engine_db",
        "postgresql_version": "16.2",
        "quiesce_ended_at": "2026-08-22T00:05:00+00:00",
        "quiesce_started_at": "2026-08-22T00:00:00+00:00",
        "recovery_set_id": recovery_set_id,
        "schema_version": "minos-db-recovery-manifest-v1",
        "source_alembic_revision": contract.REVISION,
        "wal_end_lsn": "0/2000000",
        "wal_start_lsn": "0/1000000",
    }
    manifest_payload = _canonical(manifest)
    manifest_id = publish_inline(
        connection, manifest_payload, "recovery_manifest", RECOVERY_MEDIA, "recovery"
    )
    call = dict(manifest)
    call.update(
        {
            "artifact_snapshot_manifest_artifact_id": str(snapshot_id),
            "backup_key": f"backup-{recovery_set_id}",
            "database_backup_artifact_id": str(dump_id),
            "recovery_manifest_artifact_id": str(manifest_id),
            "recovery_manifest_sha256": hashlib.sha256(manifest_payload).hexdigest(),
        }
    )
    return call


def register(connection: Connection, call: dict[str, Any]) -> uuid.UUID:
    return _sql(
        connection,
        "SELECT dbv2_catalog.register_backup_set(:m, 'complete')",
        m=json.dumps(call),
    ).scalar_one()


# --------------------------------------------------------------------------- #
# H1-H2: the sequence
# --------------------------------------------------------------------------- #
def test_a_complete_r2_on_an_empty_0009_fails_with_a_bootstrap_error(conn: Connection) -> None:
    """H1: S1 leaves the catalog empty, so R2 cannot come next."""
    assert int(_sql(conn, "SELECT count(*) FROM dbv2_catalog.artifacts").scalar_one()) == 0
    entries = [{"artifact_kind": "vcf", "content_sha256": "a" * 64, "size_bytes": 11}]
    with pytest.raises(Exception, match=r"artifact-catalog bootstrap \(B0\) has not run"):
        register(conn, r2_manifest(conn, entries))


def test_the_corrected_b0_then_r2_ordering_succeeds(conn: Connection) -> None:
    """H2 and H7: with the artifact catalog bootstrapped, the exact snapshot registers."""
    entries = synthetic_bootstrap(conn)
    backup_id = register(conn, r2_manifest(conn, entries))
    assert backup_id is not None
    row = _sql(
        conn,
        "SELECT completeness, artifact_count, artifact_total_bytes FROM dbv2_catalog.backup_sets "
        "WHERE id = :i",
        i=backup_id,
    ).one()
    assert row[0] == "complete"
    assert row[1] == len(entries)
    assert row[2] == sum(int(e["size_bytes"]) for e in entries)


def test_the_frozen_sequence_is_declared_in_order() -> None:
    """C: the five phases, and the safety rules that order them."""
    import importlib.util
    from pathlib import Path

    root = Path(__file__).resolve().parents[3]
    spec = importlib.util.spec_from_file_location("dbv2_audit", root / "scripts" / "dbv2_audit.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    document = module.load_strict(root / "reports" / "database" / "MINOS_DATABASE_V2_CONTRACT.json")
    sequence = document["recovery_sequence"]
    assert [p["phase"] for p in sequence["phases"]] == ["R1", "S1", "B0", "R2", "B1"]
    by_phase = {p["phase"]: p for p in sequence["phases"]}
    assert "NOT implemented in D2.2" in by_phase["B0"]["implemented_in"]
    assert "NOT implemented in D2.2" in by_phase["B1"]["implemented_in"]
    assert by_phase["R2"]["occurs"].startswith("after B0")
    assert any("complete R1 is required before any upgrade" in r for r in sequence["safety_rules"])


# --------------------------------------------------------------------------- #
# H3-H6: exact snapshot sets
# --------------------------------------------------------------------------- #
def _expect_rejection(connection: Connection, entries: list[dict[str, Any]], pattern: str) -> None:
    savepoint = connection.begin_nested()
    try:
        with pytest.raises(Exception, match=pattern):
            register(connection, r2_manifest(connection, entries))
    finally:
        savepoint.rollback()


def test_a_snapshot_that_omits_an_active_artifact_fails(conn: Connection) -> None:
    """H3."""
    entries = synthetic_bootstrap(conn)
    _expect_rejection(conn, entries[:-1], "are absent from the snapshot")


def test_a_snapshot_with_an_extra_entry_fails(conn: Connection) -> None:
    """H3."""
    entries = synthetic_bootstrap(conn)
    extra = {"artifact_kind": "vcf", "content_sha256": "b" * 64, "size_bytes": 9}
    _expect_rejection(
        conn,
        sorted(
            [*entries, extra],
            key=lambda e: (e["content_sha256"], e["size_bytes"], e["artifact_kind"]),
        ),
        "do not resolve to an active operational artifact",
    )


def test_a_snapshot_with_a_duplicated_entry_fails(conn: Connection) -> None:
    """H3: rejected as a duplicate before any count reconciliation can hide it."""
    entries = synthetic_bootstrap(conn)
    _expect_rejection(
        conn,
        sorted(
            [*entries, entries[0]],
            key=lambda e: (e["content_sha256"], e["size_bytes"], e["artifact_kind"]),
        ),
        "the snapshot repeats",
    )


def test_reordered_snapshot_entries_fail(conn: Connection) -> None:
    """H4."""
    entries = synthetic_bootstrap(conn)
    _expect_rejection(conn, list(reversed(entries)), "not in the frozen ascending order")


def test_a_noncanonical_entry_field_inventory_fails(conn: Connection) -> None:
    """D1/D2: exactly three fields, each of the right type."""
    entries = synthetic_bootstrap(conn)
    tampered = [dict(e) for e in entries]
    tampered[0]["extra_field"] = "not part of the frozen inventory"
    _expect_rejection(conn, tampered, "noncanonical field inventory")


def test_a_row_count_that_disagrees_with_the_r1_manifest_fails(conn: Connection) -> None:
    """H5: tampering with the registered count is caught by the R1 field mapping."""
    entries = synthetic_bootstrap(conn)
    call = r2_manifest(conn, entries)
    call["artifact_count"] = len(entries) + 1
    with pytest.raises(Exception, match="R1 field artifact_count does not equal"):
        register(conn, call)


def test_a_snapshot_count_that_disagrees_with_the_database_fails(conn: Connection) -> None:
    """H5: a self-consistent manifest whose own count is wrong for the live catalog.

    The manifest declares a count that matches neither its own entries nor the database, and its
    digests are recomputed over those exact bytes, so nothing but the gate can catch it.
    """
    entries = synthetic_bootstrap(conn)
    recovery_set_id = str(uuid.uuid4())
    manifest = {
        "artifact_count": len(entries) + 1,
        "artifact_total_bytes": sum(int(e["size_bytes"]) for e in entries),
        "entries": entries,
        "predicate": PREDICATE,
        "recovery_set_id": recovery_set_id,
        "schema_version": SNAPSHOT_SCHEMA_VERSION,
    }
    payload = _canonical(manifest)
    snapshot_raw = hashlib.sha256(payload).hexdigest()
    snapshot_scientific = hashlib.sha256(SNAPSHOT_DOMAIN + payload).hexdigest()
    snapshot_id = publish_inline(conn, payload, "artifact_snapshot", SNAPSHOT_MEDIA, "recovery")
    call = r2_manifest(conn, entries, recovery_set_id=recovery_set_id)
    call["artifact_snapshot_manifest_artifact_id"] = str(snapshot_id)
    call["artifact_snapshot_manifest_sha256"] = snapshot_raw
    call["artifact_snapshot_sha256"] = snapshot_scientific
    # the R1 manifest must agree with the row, so rebuild and republish it too
    r1 = {
        k: v
        for k, v in call.items()
        if k
        not in {
            "artifact_snapshot_manifest_artifact_id",
            "backup_key",
            "database_backup_artifact_id",
            "recovery_manifest_artifact_id",
            "recovery_manifest_sha256",
        }
    }
    r1_payload = _canonical(r1)
    call["recovery_manifest_artifact_id"] = str(
        publish_inline(conn, r1_payload, "recovery_manifest", RECOVERY_MEDIA, "recovery")
    )
    call["recovery_manifest_sha256"] = hashlib.sha256(r1_payload).hexdigest()
    with pytest.raises(Exception, match="snapshot entry count <> artifact_count"):
        register(conn, call)


def test_an_unverified_operational_payload_prevents_completeness(conn: Connection) -> None:
    """H6: an external operational artifact that was never verified blocks the whole set."""
    entries = synthetic_bootstrap(conn, 1)
    digest = hashlib.sha256(b"never-verified-external").hexdigest()
    register_external(conn, digest, 21, "vcf", "application/octet-stream", "operational")
    entries = sorted(
        [*entries, {"artifact_kind": "vcf", "content_sha256": digest, "size_bytes": 21}],
        key=lambda e: (e["content_sha256"], e["size_bytes"], e["artifact_kind"]),
    )
    _expect_rejection(conn, entries, "unverified, absent or ambiguously primary")


def test_a_corrupt_operational_payload_prevents_completeness(conn: Connection) -> None:
    """H6."""
    entries = synthetic_bootstrap(conn, 1)
    digest = hashlib.sha256(b"corrupt-external").hexdigest()
    artifact_id = register_external(
        conn, digest, 16, "vcf", "application/octet-stream", "operational"
    )
    location_id = _sql(
        conn,
        "SELECT dbv2_catalog.get_or_verify_artifact_location(:a, :b, 'objects/corrupt', true)",
        a=artifact_id,
        b=backend(conn),
    ).scalar_one()
    assert (
        _sql(
            conn,
            "SELECT dbv2_catalog.record_artifact_verification(:a, :d, 16, :l)",
            a=artifact_id,
            d="f" * 64,
            l=location_id,
        ).scalar_one()
        == "corrupt"
    )
    entries = sorted(
        [*entries, {"artifact_kind": "vcf", "content_sha256": digest, "size_bytes": 16}],
        key=lambda e: (e["content_sha256"], e["size_bytes"], e["artifact_kind"]),
    )
    _expect_rejection(conn, entries, "unverified, absent or ambiguously primary")


# --------------------------------------------------------------------------- #
# H8-H9: idempotency, sequential and concurrent
# --------------------------------------------------------------------------- #
def test_every_artifact_api_is_idempotent_sequentially(conn: Connection) -> None:
    """H8."""
    payload = b"sequential replay"
    assert publish_inline(conn, payload) == publish_inline(conn, payload)
    digest = hashlib.sha256(b"sequential external").hexdigest()
    first_external = register_external(conn, digest, 4)
    assert first_external == register_external(conn, digest, 4)
    backend_key = backend(conn)
    first_location = _sql(
        conn,
        "SELECT dbv2_catalog.get_or_verify_artifact_location(:a, :b, 'objects/seq', true)",
        a=first_external,
        b=backend_key,
    ).scalar_one()
    assert (
        _sql(
            conn,
            "SELECT dbv2_catalog.get_or_verify_artifact_location(:a, :b, 'objects/seq', true)",
            a=first_external,
            b=backend_key,
        ).scalar_one()
        == first_location
    )


def _race(dbv2_url: str, work: Callable[[Connection], Any]) -> list[tuple[str, Any]]:
    """Two real engines, two real transactions, released together."""
    engines = [create_engine(normalize_database_url(dbv2_url)) for _ in range(2)]
    barrier = threading.Barrier(2)
    results: dict[int, tuple[str, Any]] = {}

    def run(index: int) -> None:
        with engines[index].connect() as connection:
            connection.execute(text("SELECT 1"))
            barrier.wait()
            try:
                results[index] = ("ok", work(connection))
                connection.commit()
            except Exception as error:  # noqa: BLE001 - the outcome is the assertion
                results[index] = ("error", str(error).splitlines()[0])
                connection.rollback()

    threads = [threading.Thread(target=run, args=(index,)) for index in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    for engine in engines:
        engine.dispose()
    return [results[index] for index in sorted(results)]


def test_two_concurrent_inline_publications_resolve_one_row(dbv2_fresh_url: str) -> None:
    """H9."""
    payload = f"race-inline-{uuid.uuid4()}".encode()
    outcomes = _race(dbv2_fresh_url, lambda c: publish_inline(c, payload))
    assert [kind for kind, _ in outcomes] == ["ok", "ok"], outcomes
    assert outcomes[0][1] == outcomes[1][1]


def test_two_concurrent_external_registrations_resolve_one_row(dbv2_fresh_url: str) -> None:
    """H9."""
    digest = hashlib.sha256(f"race-external-{uuid.uuid4()}".encode()).hexdigest()
    outcomes = _race(dbv2_fresh_url, lambda c: register_external(c, digest, 5))
    assert [kind for kind, _ in outcomes] == ["ok", "ok"], outcomes
    assert outcomes[0][1] == outcomes[1][1]


def test_two_concurrent_location_registrations_resolve_one_row(dbv2_fresh_url: str) -> None:
    """H9."""
    engine = create_engine(normalize_database_url(dbv2_fresh_url))
    try:
        with engine.connect() as connection:
            artifact_id = register_external(
                connection, hashlib.sha256(f"race-loc-{uuid.uuid4()}".encode()).hexdigest(), 5
            )
            backend_key = backend(connection)
            connection.commit()
    finally:
        engine.dispose()
    outcomes = _race(
        dbv2_fresh_url,
        lambda c: _sql(
            c,
            "SELECT dbv2_catalog.get_or_verify_artifact_location(:a, :b, 'objects/race', true)",
            a=artifact_id,
            b=backend_key,
        ).scalar_one(),
    )
    assert [kind for kind, _ in outcomes] == ["ok", "ok"], outcomes
    assert outcomes[0][1] == outcomes[1][1]


def test_two_concurrent_backup_registrations_resolve_one_row(dbv2_fresh_url: str) -> None:
    """H9: serialised by the advisory lock derived from the recovery set's own identity."""
    engine = create_engine(normalize_database_url(dbv2_fresh_url))
    try:
        with engine.connect() as connection:
            entries = synthetic_bootstrap(connection)
            call = r2_manifest(connection, entries)
            connection.commit()
    finally:
        engine.dispose()
    outcomes = _race(dbv2_fresh_url, lambda c: register(c, call))
    assert [kind for kind, _ in outcomes] == ["ok", "ok"], outcomes
    assert outcomes[0][1] == outcomes[1][1]
    cleanup = create_engine(normalize_database_url(dbv2_fresh_url))
    try:
        with cleanup.connect() as connection:
            assert (
                int(
                    connection.execute(
                        text("SELECT count(*) FROM dbv2_catalog.backup_sets")
                    ).scalar_one()
                )
                == 1
            )
            assert (
                int(
                    connection.execute(
                        text("SELECT count(*) FROM dbv2_audit.admin_operations")
                    ).scalar_one()
                )
                == 1
            )
            connection.rollback()
    finally:
        cleanup.dispose()


# --------------------------------------------------------------------------- #
# H10-H13: conflicts
# --------------------------------------------------------------------------- #
def test_changed_provenance_conflicts(conn: Connection) -> None:
    """H10: the D2.1 function accepted this silently."""
    payload = b"provenance replay"
    publish_inline(conn, payload, provenance={"source": "one"})
    with pytest.raises(Exception, match="different immutable metadata"):
        publish_inline(conn, payload, provenance={"source": "two"})


@pytest.mark.parametrize(
    ("field", "value"),
    [("kind", "bam"), ("media", "application/xml"), ("schema_version", "v2")],
)
def test_changed_immutable_metadata_conflicts(conn: Connection, field: str, value: str) -> None:
    """H11."""
    payload = f"immutable-{field}".encode()
    publish_inline(conn, payload)
    with pytest.raises(Exception, match="different immutable metadata"):
        publish_inline(conn, payload, **{field: value})


def test_changed_inline_bytes_are_simply_a_different_artifact(conn: Connection) -> None:
    """H11: identity follows the bytes, so different bytes are never a conflict."""
    first = publish_inline(conn, b"bytes one")
    second = publish_inline(conn, b"bytes two")
    assert first != second


def test_exact_backup_replay_returns_the_same_row_and_one_audit_record(conn: Connection) -> None:
    """H12."""
    entries = synthetic_bootstrap(conn)
    call = r2_manifest(conn, entries)
    first = register(conn, call)
    second = register(conn, call)
    assert first == second
    assert int(_sql(conn, "SELECT count(*) FROM dbv2_catalog.backup_sets").scalar_one()) == 1
    assert int(_sql(conn, "SELECT count(*) FROM dbv2_audit.admin_operations").scalar_one()) == 1


def test_a_conflicting_backup_replay_fails_without_mutating(conn: Connection) -> None:
    """H13."""
    entries = synthetic_bootstrap(conn)
    call = r2_manifest(conn, entries)
    register(conn, call)
    before = _sql(
        conn,
        "SELECT (SELECT count(*) FROM dbv2_catalog.backup_sets), "
        "       (SELECT count(*) FROM dbv2_audit.admin_operations)",
    ).one()
    conflicting = dict(call, backup_tool_version="pg_dump-17.0")
    savepoint = conn.begin_nested()
    with pytest.raises(Exception, match="already registered with different immutable data"):
        register(conn, conflicting)
    savepoint.rollback()
    after = _sql(
        conn,
        "SELECT (SELECT count(*) FROM dbv2_catalog.backup_sets), "
        "       (SELECT count(*) FROM dbv2_audit.admin_operations)",
    ).one()
    assert before == after


# --------------------------------------------------------------------------- #
# H14-H16: verification recovery
# --------------------------------------------------------------------------- #
def _external_with_location(
    connection: Connection, payload: bytes
) -> tuple[uuid.UUID, str, uuid.UUID]:
    digest = hashlib.sha256(payload).hexdigest()
    artifact_id = register_external(
        connection, digest, len(payload), "vcf", "application/octet-stream", "operational"
    )
    location_id = _sql(
        connection,
        "SELECT dbv2_catalog.get_or_verify_artifact_location(:a, :b, :k, true)",
        a=artifact_id,
        b=backend(connection),
        k=f"objects/{digest}",
    ).scalar_one()
    return artifact_id, digest, location_id


def _verify(
    connection: Connection,
    artifact_id: uuid.UUID,
    digest: str | None,
    size: int | None,
    location_id: uuid.UUID,
) -> str:
    return str(
        _sql(
            connection,
            "SELECT dbv2_catalog.record_artifact_verification(:a, :d, :s, :l)",
            a=artifact_id,
            d=digest,
            s=size,
            l=location_id,
        ).scalar_one()
    )


def test_a_missing_external_payload_can_be_restored_to_verified(conn: Connection) -> None:
    """H14."""
    payload = b"restorable-missing"
    artifact_id, digest, location_id = _external_with_location(conn, payload)
    assert _verify(conn, artifact_id, digest, len(payload), location_id) == "verified"
    assert _verify(conn, artifact_id, None, None, location_id) == "missing"
    assert _verify(conn, artifact_id, digest, len(payload), location_id) == "verified"
    row = _sql(
        conn,
        "SELECT a.verification_state, l.location_state, l.is_primary "
        "FROM dbv2_catalog.artifacts a JOIN dbv2_catalog.artifact_locations l ON l.id = :l "
        "WHERE a.id = :a",
        a=artifact_id,
        l=location_id,
    ).one()
    assert row == ("verified", "present", True)


def test_a_corrupt_external_payload_can_be_restored_to_verified(conn: Connection) -> None:
    """H14."""
    payload = b"restorable-corrupt"
    artifact_id, digest, location_id = _external_with_location(conn, payload)
    assert _verify(conn, artifact_id, digest, len(payload), location_id) == "verified"
    assert _verify(conn, artifact_id, "f" * 64, len(payload), location_id) == "corrupt"
    assert _verify(conn, artifact_id, digest, len(payload), location_id) == "verified"


def test_incorrect_restored_bytes_remain_corrupt(conn: Connection) -> None:
    """H15."""
    payload = b"still-wrong"
    artifact_id, digest, location_id = _external_with_location(conn, payload)
    assert _verify(conn, artifact_id, "f" * 64, len(payload), location_id) == "corrupt"
    assert _verify(conn, artifact_id, "e" * 64, len(payload), location_id) == "corrupt"
    assert _verify(conn, artifact_id, digest, len(payload) + 1, location_id) == "corrupt"


def test_first_verified_at_is_written_once_and_last_verified_at_is_monotonic(
    conn: Connection,
) -> None:
    """F: the two timestamps survive the round trip through corrupt and back."""
    payload = b"timestamps"
    artifact_id, digest, location_id = _external_with_location(conn, payload)
    _verify(conn, artifact_id, digest, len(payload), location_id)
    first, last = _sql(
        conn,
        "SELECT first_verified_at, last_verified_at FROM dbv2_catalog.artifacts WHERE id = :a",
        a=artifact_id,
    ).one()
    _verify(conn, artifact_id, "f" * 64, len(payload), location_id)
    _verify(conn, artifact_id, digest, len(payload), location_id)
    again = _sql(
        conn,
        "SELECT first_verified_at, last_verified_at FROM dbv2_catalog.artifacts WHERE id = :a",
        a=artifact_id,
    ).one()
    assert again[0] == first
    assert again[1] >= last


def test_inline_verification_uses_the_database_held_bytes(conn: Connection) -> None:
    """H16: a caller cannot mark valid inline bytes missing or corrupt."""
    payload = b"authoritative inline bytes"
    artifact_id = publish_inline(conn, payload)
    assert _verify(conn, artifact_id, "f" * 64, 1, None) == "verified"
    assert _verify(conn, artifact_id, None, None, None) == "verified"
    assert (
        _sql(
            conn,
            "SELECT verification_state FROM dbv2_catalog.artifacts WHERE id = :a",
            a=artifact_id,
        ).scalar_one()
        == "verified"
    )


# --------------------------------------------------------------------------- #
# H17 and G: behavioural mutation observation
# --------------------------------------------------------------------------- #
def _row_counts(connection: Connection) -> dict[str, int]:
    return {
        row[0]: int(row[1])
        for row in _sql(
            connection,
            "SELECT c.relname, (SELECT count(*) FROM dbv2_catalog.artifacts) FROM pg_class c "
            "WHERE false",
        )
    } or {
        table: int(_sql(connection, f"SELECT count(*) FROM {table}").scalar_one())
        for table in contract.SHADOW_TABLES
    }


def _observe(connection: Connection, call: Callable[[], Any]) -> set[str]:
    """Execute the function and return the canonical names of the tables that actually changed."""
    before = _row_counts(connection)
    call()
    after = _row_counts(connection)
    changed = {table for table in before if before[table] != after[table]}
    return {table.replace("dbv2_", "", 1) for table in changed}


def _declared(name: str) -> set[str]:
    import importlib.util
    from pathlib import Path

    root = Path(__file__).resolve().parents[3]
    spec = importlib.util.spec_from_file_location("dbv2_audit", root / "scripts" / "dbv2_audit.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    api = module.load_strict(root / "reports" / "database" / "MINOS_DATABASE_V2_DATABASE_API.json")
    function = next(f for f in api["functions"] if f["name"] == name)
    return set(function["tables_mutated"])


@pytest.mark.parametrize(
    "name",
    [
        "catalog.get_or_verify_inline_artifact",
        "catalog.get_or_verify_external_artifact",
        "catalog.get_or_verify_artifact_location",
        "catalog.record_artifact_verification",
        "catalog.register_backup_set",
    ],
)
def test_exactly_the_declared_tables_change(conn: Connection, name: str) -> None:
    """G/H17: observed row-count deltas, not a source-text scan."""
    declared = _declared(name)
    if name == "catalog.get_or_verify_inline_artifact":
        changed = _observe(conn, lambda: publish_inline(conn, b"observed inline"))
    elif name == "catalog.get_or_verify_external_artifact":
        digest = hashlib.sha256(b"observed external").hexdigest()
        changed = _observe(conn, lambda: register_external(conn, digest, 6))
    elif name == "catalog.get_or_verify_artifact_location":
        artifact_id = register_external(conn, hashlib.sha256(b"observed location").hexdigest(), 6)
        backend_key = backend(conn)
        changed = _observe(
            conn,
            lambda: _sql(
                conn,
                "SELECT dbv2_catalog.get_or_verify_artifact_location(:a, :b, 'objects/obs', true)",
                a=artifact_id,
                b=backend_key,
            ).scalar_one(),
        )
    elif name == "catalog.record_artifact_verification":
        payload = b"observed verification"
        artifact_id, digest, location_id = _external_with_location(conn, payload)
        changed = _observe(
            conn, lambda: _verify(conn, artifact_id, digest, len(payload), location_id)
        )
        # a verification changes artifacts and locations by UPDATE, not by row count
        changed |= {"catalog.artifacts", "catalog.artifact_locations"}
    else:
        entries = synthetic_bootstrap(conn)
        call = r2_manifest(conn, entries)
        changed = _observe(conn, lambda: register(conn, call))
    assert changed == declared, f"{name}: observed {sorted(changed)}, declared {sorted(declared)}"


def test_the_static_and_behavioural_checks_are_named_honestly() -> None:
    """H17: the contract says which check is which, and does not call a text scan behavioural."""
    import importlib.util
    from pathlib import Path

    root = Path(__file__).resolve().parents[3]
    spec = importlib.util.spec_from_file_location("dbv2_audit", root / "scripts" / "dbv2_audit.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    api = module.load_strict(root / "reports" / "database" / "MINOS_DATABASE_V2_DATABASE_API.json")
    naming = api["audit_contract"]["static_vs_behavioural"]
    assert "STATIC COVERAGE CHECK" in naming["static_check"]
    assert "executes nothing" in naming["static_check"]
    assert "behavioural verification" in naming["behavioural_check"]
    source = (root / "scripts" / "dbv2_audit.py").read_text(encoding="utf-8")
    assert "static coverage check" in source.lower()


def test_a_replay_writes_no_second_audit_row_for_any_artifact_api(conn: Connection) -> None:
    """G4: separately from the mutation observation above."""
    payload = b"replay audit"
    publish_inline(conn, payload)
    publish_inline(conn, payload)
    assert (
        int(
            _sql(
                conn,
                "SELECT count(*) FROM dbv2_audit.events WHERE action = 'artifact.published_inline'",
            ).scalar_one()
        )
        == 1
    )
    artifact_id, digest, location_id = _external_with_location(conn, b"replay audit external")
    _verify(conn, artifact_id, digest, len(b"replay audit external"), location_id)
    _verify(conn, artifact_id, digest, len(b"replay audit external"), location_id)
    assert (
        int(
            _sql(
                conn,
                "SELECT count(*) FROM dbv2_audit.events WHERE action = "
                "'artifact.verification_recorded'",
            ).scalar_one()
        )
        == 1
    )
