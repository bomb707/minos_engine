"""DB-V2 D2.1: artifact identity, inline integrity, the artifact-control APIs and audit.

Every assertion executes real SQL against the created objects. The corrective exists because the
D2 schema let direct SQL forge an inline artifact's identity, bound a domain-separated scientific
hash to a raw content digest, offered no legal path to publish the inline artifacts a recovery set
needs, demanded external location evidence for inline bytes, and declared audit writes that no
function made.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Iterator
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


def _audit_count(connection: Connection, action: str | None = None) -> int:
    if action is None:
        return int(_sql(connection, "SELECT count(*) FROM dbv2_audit.events").scalar_one())
    return int(
        _sql(
            connection, "SELECT count(*) FROM dbv2_audit.events WHERE action = :a", a=action
        ).scalar_one()
    )


def _publish_inline(connection: Connection, payload: bytes, **overrides: Any) -> uuid.UUID:
    params = {
        "kind": "manifest",
        "media": "application/json",
        "scope": "operational",
        "retention": "standard",
        "schema_version": "v1",
    }
    params.update(overrides)
    return _sql(
        connection,
        "SELECT dbv2_catalog.get_or_verify_inline_artifact(:p, :media, :kind, :scope, "
        ":retention, :schema_version, '{}'::jsonb)",
        p=payload,
        **params,
    ).scalar_one()


def _register_external(
    connection: Connection, digest: str, size: int, **overrides: Any
) -> uuid.UUID:
    params = {
        "kind": "dump",
        "media": BACKUP_MEDIA,
        "scope": "operational",
        "retention": "standard",
        "schema_version": "v1",
    }
    params.update(overrides)
    return _sql(
        connection,
        "SELECT dbv2_catalog.get_or_verify_external_artifact(:d, :s, :media, :kind, :scope, "
        ":retention, :schema_version, '{}'::jsonb)",
        d=digest,
        s=size,
        **params,
    ).scalar_one()


def _backend(connection: Connection, key: str | None = None) -> str:
    backend_key = key or f"backend-{uuid.uuid4()}"
    _sql(
        connection,
        "INSERT INTO dbv2_catalog.storage_backends (backend_key, backend_type, logical_root) "
        "VALUES (:k, 'local_fs', '/srv/minos')",
        k=backend_key,
    )
    return backend_key


# --------------------------------------------------------------------------- #
# H1-H3: canonical inline identity, enforced by PostgreSQL itself
# --------------------------------------------------------------------------- #
def test_a_forged_inline_content_hash_is_rejected_by_direct_sql(conn: Connection) -> None:
    """H1: not by the API function - by the table, against a plain INSERT."""
    payload = b"the exact stored bytes"
    with pytest.raises(Exception, match="ck_artifacts_inline_bounded"):
        _sql(
            conn,
            "INSERT INTO dbv2_catalog.artifacts (artifact_kind, content_sha256, size_bytes, "
            "media_type, storage_mode, inline_payload, retention_class, provenance) "
            "VALUES ('forged', :d, :s, 'application/json', 'inline', :p, 'standard', "
            "'{}'::jsonb)",
            d="0" * 64,
            s=len(payload),
            p=payload,
        )


def test_a_forged_inline_size_is_rejected_by_direct_sql(conn: Connection) -> None:
    """H2."""
    payload = b"the exact stored bytes"
    with pytest.raises(Exception, match="ck_artifacts_inline_bounded"):
        _sql(
            conn,
            "INSERT INTO dbv2_catalog.artifacts (artifact_kind, content_sha256, size_bytes, "
            "media_type, storage_mode, inline_payload, retention_class, provenance) "
            "VALUES ('forged', :d, 1, 'application/json', 'inline', :p, 'standard', '{}'::jsonb)",
            d=hashlib.sha256(payload).hexdigest(),
            p=payload,
        )


def test_equal_bytes_always_produce_the_raw_content_hash(conn: Connection) -> None:
    """H3: the database derives the identity; the caller cannot influence it."""
    payload = b'{"a":1}\n'
    artifact_id = _publish_inline(conn, payload)
    row = _sql(
        conn,
        "SELECT content_sha256, size_bytes, storage_mode, verification_state "
        "FROM dbv2_catalog.artifacts WHERE id = :i",
        i=artifact_id,
    ).one()
    assert row[0] == hashlib.sha256(payload).hexdigest()
    assert row[1] == len(payload)
    assert row[2] == "inline"
    assert row[3] == "verified"


# --------------------------------------------------------------------------- #
# H4-H7: two distinct identities, each bound where it belongs
# --------------------------------------------------------------------------- #
def _snapshot(entries: list[dict[str, Any]], recovery_set_id: str) -> tuple[bytes, str, str]:
    manifest = {
        "artifact_count": len(entries),
        "artifact_total_bytes": sum(int(e["size_bytes"]) for e in entries),
        "entries": sorted(
            entries, key=lambda e: (e["content_sha256"], e["size_bytes"], e["artifact_kind"])
        ),
        "predicate": PREDICATE,
        "recovery_set_id": recovery_set_id,
        "schema_version": "minos-artifact-snapshot-v1",
    }
    payload = _canonical(manifest)
    return (
        payload,
        hashlib.sha256(payload).hexdigest(),
        hashlib.sha256(SNAPSHOT_DOMAIN + payload).hexdigest(),
    )


def test_the_raw_and_domain_separated_snapshot_hashes_are_distinct(conn: Connection) -> None:
    """H4: both recompute, and they can never be equal."""
    payload, raw, scientific = _snapshot([], str(uuid.uuid4()))
    assert raw != scientific
    computed = _sql(
        conn,
        "SELECT encode(sha256(:p), 'hex'), "
        "       encode(sha256(convert_to(E'minos:db-v2-artifact-snapshot:v1\\n', 'UTF8') || :p), "
        "              'hex')",
        p=payload,
    ).one()
    assert computed[0] == raw
    assert computed[1] == scientific


def test_the_snapshot_composite_fk_binds_the_raw_manifest_hash() -> None:
    """H5: read from the frozen migration contract, then from the live catalog in H5b."""
    declared = dict(contract.TABLE_CONSTRAINTS["dbv2_catalog.backup_sets"]["foreign_keys"])
    assert declared["fk_backup_sets_artifact_snapshot_manifest"] == "dbv2_catalog.artifacts"


def test_the_live_snapshot_fk_names_the_raw_digest_column(conn: Connection) -> None:
    """H5b: the catalog definition itself, not the contract that describes it."""
    definition = _sql(
        conn,
        "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
        "WHERE conname = 'fk_backup_sets_artifact_snapshot_manifest'",
    ).scalar_one()
    assert "artifact_snapshot_manifest_sha256" in definition
    assert "(artifact_snapshot_manifest_artifact_id, artifact_snapshot_manifest_sha256" in (
        definition
    )
    # the scientific identity participates in no foreign key at all
    others = [
        row[0]
        for row in _sql(
            conn,
            "SELECT conname FROM pg_constraint con "
            "JOIN pg_class c ON c.oid = con.conrelid "
            "JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = 'dbv2_catalog' AND c.relname = 'backup_sets' AND con.contype = 'f' "
            "  AND pg_get_constraintdef(con.oid) LIKE '%artifact_snapshot_sha256,%'",
        )
    ]
    assert others == []


def test_a_domain_separated_hash_cannot_be_a_raw_content_hash(conn: Connection) -> None:
    """H6: substituting the scientific identity for the artifact's own digest is refused."""
    payload, _, scientific = _snapshot([], str(uuid.uuid4()))
    with pytest.raises(Exception, match="ck_artifacts_inline_bounded"):
        _sql(
            conn,
            "INSERT INTO dbv2_catalog.artifacts (artifact_kind, content_sha256, size_bytes, "
            "media_type, storage_mode, inline_payload, retention_class, provenance) "
            "VALUES ('snapshot', :d, :s, :m, 'inline', :p, 'standard', '{}'::jsonb)",
            d=scientific,
            s=len(payload),
            m=SNAPSHOT_MEDIA,
            p=payload,
        )


def test_a_raw_hash_cannot_be_the_snapshot_scientific_identity(conn: Connection) -> None:
    """H7: and the two columns may never hold the same value."""
    payload, raw, _ = _snapshot([], str(uuid.uuid4()))
    del payload
    row = _sql(
        conn,
        "SELECT expression FROM (SELECT pg_get_constraintdef(oid) AS expression "
        "FROM pg_constraint WHERE conname = 'ck_backup_sets_snapshot_identities_differ') AS c",
    ).scalar_one()
    assert "artifact_snapshot_sha256 <> artifact_snapshot_manifest_sha256" in row
    assert len(raw) == 64


# --------------------------------------------------------------------------- #
# H8-H9: inline needs no location, external does
# --------------------------------------------------------------------------- #
def _complete_recovery_set(connection: Connection, *, give_dump_location: bool = True) -> None:
    recovery_set_id = str(uuid.uuid4())
    operational = b"an operational payload"
    operational_id = _publish_inline(connection, operational, kind="vcf")
    del operational_id
    entries = [
        {
            "artifact_kind": "vcf",
            "content_sha256": hashlib.sha256(operational).hexdigest(),
            "size_bytes": len(operational),
        }
    ]
    snapshot_bytes, snapshot_raw, snapshot_scientific = _snapshot(entries, recovery_set_id)
    snapshot_id = _publish_inline(
        connection,
        snapshot_bytes,
        kind="artifact_snapshot",
        media=SNAPSHOT_MEDIA,
        scope="recovery",
    )
    dump_digest = hashlib.sha256(f"dump-{recovery_set_id}".encode()).hexdigest()
    dump_id = _register_external(
        connection, dump_digest, 4096, kind="database_backup", media=BACKUP_MEDIA, scope="recovery"
    )
    if give_dump_location:
        backend_key = _backend(connection)
        location_id = _sql(
            connection,
            "SELECT dbv2_catalog.get_or_verify_artifact_location(:a, :b, :k, true)",
            a=dump_id,
            b=backend_key,
            k=f"backups/{dump_digest}.dump",
        ).scalar_one()
        _sql(
            connection,
            "SELECT dbv2_catalog.record_artifact_verification(:a, :d, :s, :l)",
            a=dump_id,
            d=dump_digest,
            s=4096,
            l=location_id,
        )
    r1 = {
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
    manifest_bytes = _canonical(r1)
    manifest_id = _publish_inline(
        connection,
        manifest_bytes,
        kind="recovery_manifest",
        media=RECOVERY_MEDIA,
        scope="recovery",
    )
    _sql(
        connection,
        "INSERT INTO dbv2_catalog.backup_sets (backup_key, recovery_set_id, alembic_revision, "
        "quiesce_started_at, quiesce_ended_at, manifest_schema_version, database_name, "
        "recovery_manifest_artifact_id, recovery_manifest_sha256, database_backup_kind, "
        "database_backup_artifact_id, database_backup_sha256, database_backup_size_bytes, "
        "wal_start_lsn, wal_end_lsn, artifact_snapshot_manifest_artifact_id, "
        "artifact_snapshot_manifest_sha256, artifact_snapshot_sha256, "
        "artifact_snapshot_manifest_media_type, artifact_count, artifact_total_bytes, "
        "postgresql_version, backup_tool_version, artifact_verification_tool_version, "
        "completeness, created_at) "
        "VALUES (:bk, :rs, :rev, :qs, :qe, :sv, :db, :mid, :md, 'pg_dump', :bid, :bd, 4096, "
        "        :ws, :we, :sid, :sraw, :ssci, :sm, :ac, :at, '16.2', 'pg_dump-16.2', 'verify-1', "
        "        'complete', :created)",
        bk=f"backup-{recovery_set_id}",
        rs=recovery_set_id,
        rev=contract.REVISION,
        qs=r1["quiesce_started_at"],
        qe=r1["quiesce_ended_at"],
        sv=r1["schema_version"],
        db=r1["database_name"],
        mid=manifest_id,
        md=hashlib.sha256(manifest_bytes).hexdigest(),
        bid=dump_id,
        bd=dump_digest,
        ws=r1["wal_start_lsn"],
        we=r1["wal_end_lsn"],
        sid=snapshot_id,
        sraw=snapshot_raw,
        ssci=snapshot_scientific,
        sm=SNAPSHOT_MEDIA,
        ac=r1["artifact_count"],
        at=r1["artifact_total_bytes"],
        created=r1["created_at"],
    )


def test_inline_manifests_need_no_artifact_location(conn: Connection) -> None:
    """H8: the whole recovery set registers with locations only on the external dump."""
    _complete_recovery_set(conn)
    assert int(_sql(conn, "SELECT count(*) FROM dbv2_catalog.backup_sets").scalar_one()) == 1
    inline_with_location = int(
        _sql(
            conn,
            "SELECT count(*) FROM dbv2_catalog.artifacts a "
            "JOIN dbv2_catalog.artifact_locations l ON l.artifact_id = a.id "
            "WHERE a.storage_mode = 'inline'",
        ).scalar_one()
    )
    assert inline_with_location == 0


def test_the_external_dump_requires_a_present_location_and_verified_state(
    conn: Connection,
) -> None:
    """H9."""
    with pytest.raises(Exception, match="is not verification_state = verified"):
        _complete_recovery_set(conn, give_dump_location=False)


# --------------------------------------------------------------------------- #
# H10-H14: the artifact-control APIs
# --------------------------------------------------------------------------- #
def test_inline_publication_is_idempotent(conn: Connection) -> None:
    """H10."""
    payload = b"replayed bytes"
    first = _publish_inline(conn, payload)
    second = _publish_inline(conn, payload)
    assert first == second
    assert int(_sql(conn, "SELECT count(*) FROM dbv2_catalog.artifacts").scalar_one()) == 1


def test_conflicting_inline_metadata_fails_closed(conn: Connection) -> None:
    """H11."""
    payload = b"conflicting bytes"
    _publish_inline(conn, payload, kind="vcf")
    savepoint = conn.begin_nested()
    with pytest.raises(Exception, match="different immutable metadata"):
        _publish_inline(conn, payload, kind="bam")
    savepoint.rollback()
    savepoint = conn.begin_nested()
    with pytest.raises(Exception, match="different immutable metadata"):
        _publish_inline(conn, payload, kind="vcf", scope="recovery")
    savepoint.rollback()
    # different bytes are simply a different artifact
    assert _publish_inline(conn, payload + b"!", kind="vcf") is not None


def test_external_registration_remains_unverified(conn: Connection) -> None:
    """H12."""
    digest = hashlib.sha256(b"external").hexdigest()
    artifact_id = _register_external(conn, digest, 99)
    state = _sql(
        conn,
        "SELECT verification_state, storage_mode, inline_payload IS NULL "
        "FROM dbv2_catalog.artifacts WHERE id = :i",
        i=artifact_id,
    ).one()
    assert state == ("unverified", "external", True)


def test_verification_succeeds_only_on_a_matching_digest_and_size(conn: Connection) -> None:
    """H13."""
    digest = hashlib.sha256(b"external-two").hexdigest()
    artifact_id = _register_external(conn, digest, 12)
    backend_key = _backend(conn)
    location_id = _sql(
        conn,
        "SELECT dbv2_catalog.get_or_verify_artifact_location(:a, :b, :k, true)",
        a=artifact_id,
        b=backend_key,
        k="objects/external-two",
    ).scalar_one()
    outcome = _sql(
        conn,
        "SELECT dbv2_catalog.record_artifact_verification(:a, :d, 12, :l)",
        a=artifact_id,
        d=digest,
        l=location_id,
    ).scalar_one()
    assert outcome == "verified"
    assert (
        _sql(
            conn,
            "SELECT verification_state FROM dbv2_catalog.artifacts WHERE id = :i",
            i=artifact_id,
        ).scalar_one()
        == "verified"
    )


@pytest.mark.parametrize(
    ("observed_digest", "observed_size", "expected"),
    [("wrong", 12, "corrupt"), ("right", 13, "corrupt"), (None, None, "missing")],
)
def test_a_wrong_digest_or_size_never_verifies(
    conn: Connection, observed_digest: str | None, observed_size: int | None, expected: str
) -> None:
    """H14: the mismatch paths, each reaching only its declared state."""
    digest = hashlib.sha256(f"external-{observed_digest}-{observed_size}".encode()).hexdigest()
    artifact_id = _register_external(conn, digest, 12)
    backend_key = _backend(conn)
    location_id = _sql(
        conn,
        "SELECT dbv2_catalog.get_or_verify_artifact_location(:a, :b, :k, true)",
        a=artifact_id,
        b=backend_key,
        k=f"objects/{digest}",
    ).scalar_one()
    supplied = digest if observed_digest == "right" else ("f" * 64 if observed_digest else None)
    outcome = _sql(
        conn,
        "SELECT dbv2_catalog.record_artifact_verification(:a, :d, :s, :l)",
        a=artifact_id,
        d=supplied,
        s=observed_size,
        l=location_id,
    ).scalar_one()
    assert outcome == expected


def test_an_ambiguous_or_missing_location_fails_closed(conn: Connection) -> None:
    """H14: an external artifact with no location, and one with two."""
    digest = hashlib.sha256(b"ambiguous").hexdigest()
    artifact_id = _register_external(conn, digest, 5)
    savepoint = conn.begin_nested()
    with pytest.raises(Exception, match="exactly one named location"):
        _sql(
            conn,
            "SELECT dbv2_catalog.record_artifact_verification(:a, :d, 5, NULL)",
            a=artifact_id,
            d=digest,
        )
    savepoint.rollback()
    for index in range(2):
        _sql(
            conn,
            "SELECT dbv2_catalog.get_or_verify_artifact_location(:a, :b, :k, :p)",
            a=artifact_id,
            b=_backend(conn),
            k=f"objects/{digest}-{index}",
            p=index == 0,
        )
    with pytest.raises(Exception, match="exactly one named location"):
        _sql(
            conn,
            "SELECT dbv2_catalog.record_artifact_verification(:a, :d, 5, NULL)",
            a=artifact_id,
            d=digest,
        )


@pytest.mark.parametrize(
    "object_key",
    ["/absolute/key", "file:///etc/passwd", "a/../../escape", "a//b", "trailing/", "./relative"],
)
def test_an_unclean_object_key_is_refused(conn: Connection, object_key: str) -> None:
    """F3: no absolute path, URI, '..', empty component or symlink-derived identity."""
    artifact_id = _register_external(conn, hashlib.sha256(b"key-test").hexdigest(), 1)
    with pytest.raises(Exception, match="not a clean relative key|must be a non-empty"):
        _sql(
            conn,
            "SELECT dbv2_catalog.get_or_verify_artifact_location(:a, :b, :k, true)",
            a=artifact_id,
            b=_backend(conn),
            k=object_key,
        )


def test_location_registration_is_idempotent_and_conflicts_fail_closed(conn: Connection) -> None:
    """F3."""
    artifact_id = _register_external(conn, hashlib.sha256(b"loc").hexdigest(), 1)
    backend_key = _backend(conn)
    first = _sql(
        conn,
        "SELECT dbv2_catalog.get_or_verify_artifact_location(:a, :b, :k, true)",
        a=artifact_id,
        b=backend_key,
        k="objects/loc",
    ).scalar_one()
    second = _sql(
        conn,
        "SELECT dbv2_catalog.get_or_verify_artifact_location(:a, :b, :k, true)",
        a=artifact_id,
        b=backend_key,
        k="objects/loc",
    ).scalar_one()
    assert first == second
    other = _register_external(conn, hashlib.sha256(b"loc-other").hexdigest(), 1)
    with pytest.raises(Exception, match="already registered with a different identity"):
        _sql(
            conn,
            "SELECT dbv2_catalog.get_or_verify_artifact_location(:a, :b, :k, true)",
            a=other,
            b=backend_key,
            k="objects/loc",
        )


# --------------------------------------------------------------------------- #
# H15: the recovery-scope boundary
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("role", ["minos_runner", "minos_trainer", "minos_evaluator"])
def test_a_runtime_role_cannot_create_a_recovery_scope_artifact(dbv2_url: str, role: str) -> None:
    """H15: proved by LOGGING IN as the role, so session_user really is that identity."""
    from sqlalchemy.engine import make_url

    url = make_url(normalize_database_url(dbv2_url)).set(username=role, password=None)
    engine = create_engine(url)
    try:
        with engine.connect() as connection:
            assert connection.execute(text("SELECT session_user")).scalar_one() == role
            with pytest.raises(Exception, match="may not create a recovery-scope artifact"):
                connection.execute(
                    text(
                        "SELECT dbv2_catalog.get_or_verify_inline_artifact(:p, "
                        "'application/json', 'k', 'recovery', 'standard', 'v1', '{}'::jsonb)"
                    ),
                    {"p": b"runtime-recovery-attempt"},
                )
            connection.rollback()
            # the same role may publish an OPERATIONAL artifact
            published = connection.execute(
                text(
                    "SELECT dbv2_catalog.get_or_verify_inline_artifact(:p, 'application/json', "
                    "'k', 'operational', 'standard', 'v1', '{}'::jsonb)"
                ),
                {"p": f"runtime-operational-{role}".encode()},
            ).scalar_one()
            assert published is not None
            connection.rollback()
    finally:
        engine.dispose()


# --------------------------------------------------------------------------- #
# H16-H19: audit behaviour
# --------------------------------------------------------------------------- #
def test_each_successful_artifact_api_writes_exactly_one_audit_row(conn: Connection) -> None:
    """H16."""
    assert _audit_count(conn) == 0
    payload = b"audited inline bytes"
    _publish_inline(conn, payload)
    assert _audit_count(conn, "artifact.published_inline") == 1

    digest = hashlib.sha256(b"audited external").hexdigest()
    artifact_id = _register_external(conn, digest, 7)
    assert _audit_count(conn, "artifact.registered_external") == 1

    location_id = _sql(
        conn,
        "SELECT dbv2_catalog.get_or_verify_artifact_location(:a, :b, :k, true)",
        a=artifact_id,
        b=_backend(conn),
        k="objects/audited",
    ).scalar_one()
    assert _audit_count(conn, "artifact_location.registered") == 1

    _sql(
        conn,
        "SELECT dbv2_catalog.record_artifact_verification(:a, :d, 7, :l)",
        a=artifact_id,
        d=digest,
        l=location_id,
    )
    assert _audit_count(conn, "artifact.verification_recorded") == 1
    assert _audit_count(conn) == 4


def test_the_recorded_actor_is_the_invoking_login_identity(conn: Connection) -> None:
    """G: session_user, never the SECURITY DEFINER principal."""
    _publish_inline(conn, b"actor bytes")
    actor = _sql(conn, "SELECT actor_role FROM dbv2_audit.events").scalar_one()
    session_identity = _sql(conn, "SELECT session_user").scalar_one()
    assert actor == session_identity
    assert actor != contract.DEFINER_PRINCIPAL


def test_a_replay_creates_no_duplicate_audit_row(conn: Connection) -> None:
    """H17."""
    payload = b"replayed audited bytes"
    _publish_inline(conn, payload)
    _publish_inline(conn, payload)
    _publish_inline(conn, payload)
    assert _audit_count(conn, "artifact.published_inline") == 1

    digest = hashlib.sha256(b"replayed external").hexdigest()
    artifact_id = _register_external(conn, digest, 3)
    _register_external(conn, digest, 3)
    assert _audit_count(conn, "artifact.registered_external") == 1

    backend_key = _backend(conn)
    location_id = _sql(
        conn,
        "SELECT dbv2_catalog.get_or_verify_artifact_location(:a, :b, :k, true)",
        a=artifact_id,
        b=backend_key,
        k="objects/replayed",
    ).scalar_one()
    _sql(
        conn,
        "SELECT dbv2_catalog.get_or_verify_artifact_location(:a, :b, :k, true)",
        a=artifact_id,
        b=backend_key,
        k="objects/replayed",
    )
    assert _audit_count(conn, "artifact_location.registered") == 1

    for _ in range(3):
        _sql(
            conn,
            "SELECT dbv2_catalog.record_artifact_verification(:a, :d, 3, :l)",
            a=artifact_id,
            d=digest,
            l=location_id,
        )
    assert _audit_count(conn, "artifact.verification_recorded") == 1


def test_a_failed_operation_leaves_no_audit_row(conn: Connection) -> None:
    """H18: the audit write shares the caller's transaction; a failure takes it with it."""
    payload = b"failing bytes"
    _publish_inline(conn, payload, kind="vcf")
    assert _audit_count(conn) == 1
    savepoint = conn.begin_nested()
    with pytest.raises(Exception, match="different immutable metadata"):
        _publish_inline(conn, payload, kind="bam")
    savepoint.rollback()
    assert _audit_count(conn) == 1


def test_a_rolled_back_operation_leaves_no_audit_row(dbv2_url: str) -> None:
    """H18: an explicit rollback, observed on a fresh connection."""
    engine = create_engine(normalize_database_url(dbv2_url))
    try:
        with engine.connect() as connection:
            connection.execute(
                text(
                    "SELECT dbv2_catalog.get_or_verify_inline_artifact(:p, 'application/json', "
                    "'k', 'operational', 'standard', 'v1', '{}'::jsonb)"
                ),
                {"p": b"rolled back bytes"},
            )
            assert (
                int(connection.execute(text("SELECT count(*) FROM dbv2_audit.events")).scalar_one())
                == 1
            )
            connection.rollback()
        with engine.connect() as connection:
            assert (
                int(connection.execute(text("SELECT count(*) FROM dbv2_audit.events")).scalar_one())
                == 0
            )
    finally:
        engine.dispose()


def test_backup_set_registration_writes_its_administrative_audit_row(conn: Connection) -> None:
    """H19: catalog.register_backup_set, in the same transaction as the row it registers."""
    body = _sql(
        conn,
        "SELECT prosrc FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace "
        "WHERE n.nspname = 'dbv2_catalog' AND p.proname = 'register_backup_set'",
    ).scalar_one()
    assert "INSERT INTO dbv2_audit.admin_operations" in body
    assert "'migration'" in body and "'succeeded'" in body
    assert int(_sql(conn, "SELECT count(*) FROM dbv2_audit.admin_operations").scalar_one()) == 0


def test_every_declared_audit_mutation_is_actually_performed(conn: Connection) -> None:
    """G: the declared inventory, checked against what the live functions really write."""
    import importlib.util
    from pathlib import Path

    root = Path(__file__).resolve().parents[3]
    spec = importlib.util.spec_from_file_location("dbv2_audit", root / "scripts" / "dbv2_audit.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    api = module.load_strict(root / "reports" / "database" / "MINOS_DATABASE_V2_DATABASE_API.json")
    for function in api["functions"]:
        if function["kind"] != "api_function":
            continue
        schema, bare = function["name"].split(".", 1)
        source = _sql(
            conn,
            "SELECT prosrc FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace "
            "WHERE n.nspname = :s AND p.proname = :p",
            s=f"dbv2_{schema}",
            p=bare,
        ).scalar_one()
        for table in function["tables_mutated"]:
            target_schema, target_table = table.split(".", 1)
            assert f"dbv2_{target_schema}.{target_table}" in source, (
                f"{function['name']} declares it mutates {table} but never writes it"
            )
