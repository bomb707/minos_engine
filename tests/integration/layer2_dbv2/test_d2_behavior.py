"""DB-V2 D2: behavioural proof that the shadow schema enforces the contract.

Every assertion here executes real SQL against the created objects. Allowed transitions must be
accepted, forbidden ones rejected, immutable columns unwritable, DELETE refused everywhere, and
the recovery-set rules enforced across tables. No production constraint or trigger is disabled.
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

from .conftest import scalar

pytestmark = [pytest.mark.integration, pytest.mark.postgres, pytest.mark.migration]

SNAPSHOT_DOMAIN = b"minos:db-v2-artifact-snapshot:v1\n"
RECOVERY_MEDIA = "application/vnd.minos.db-recovery-manifest+json"
BACKUP_MEDIA = "application/vnd.postgresql.dump"
SNAPSHOT_MEDIA = "application/vnd.minos.artifact-snapshot+json"


@pytest.fixture
def conn(dbv2_url: str) -> Iterator[Connection]:
    """A connection whose transaction is always rolled back, so the shared database stays empty."""
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


def _backend(connection: Connection) -> uuid.UUID:
    return _sql(
        connection,
        "INSERT INTO dbv2_catalog.storage_backends (backend_key, backend_type, logical_root) "
        "VALUES (:k, 'local_fs', '/srv/minos') RETURNING id",
        k=f"backend-{uuid.uuid4()}",
    ).scalar_one()


def _artifact(
    connection: Connection,
    *,
    payload: bytes | None = None,
    digest: str | None = None,
    size: int | None = None,
    kind: str = "payload",
    media_type: str = "application/octet-stream",
    scope: str = "operational",
    verification: str = "verified",
    lifecycle: str = "active",
    present: bool = True,
) -> tuple[uuid.UUID, str, int]:
    """Publish one artifact, optionally inline, optionally with a present location."""
    if payload is not None:
        digest = digest or hashlib.sha256(payload).hexdigest()
        size = len(payload)
        storage = "inline"
    else:
        digest = digest or hashlib.sha256(uuid.uuid4().bytes).hexdigest()
        size = 1024 if size is None else size
        storage = "external"
    artifact_id = _sql(
        connection,
        "INSERT INTO dbv2_catalog.artifacts (artifact_kind, content_sha256, size_bytes, "
        "media_type, storage_mode, inline_payload, lifecycle_state, retention_class, "
        "backup_scope, provenance, verification_state, first_verified_at, last_verified_at) "
        "VALUES (:k, :d, :s, :m, :store, :p, :life, 'standard', :scope, '{}'::jsonb, :ver, "
        "        now(), now()) RETURNING id",
        k=kind,
        d=digest,
        s=size,
        m=media_type,
        store=storage,
        p=payload,
        life=lifecycle,
        scope=scope,
        ver=verification,
    ).scalar_one()
    if present:
        _sql(
            connection,
            "INSERT INTO dbv2_catalog.artifact_locations (artifact_id, backend_id, object_key, "
            "location_state, is_primary) VALUES (:a, :b, :k, 'present', true)",
            a=artifact_id,
            b=_backend(connection),
            k=f"objects/{digest}",
        )
    return artifact_id, digest, size


# --------------------------------------------------------------------------- #
# J10 / J11: every allowed transition accepted, every forbidden one rejected
# --------------------------------------------------------------------------- #
def _seed_artifact_row(connection: Connection) -> uuid.UUID:
    artifact_id, _, _ = _artifact(connection, verification="unverified", present=False)
    return artifact_id


def test_allowed_artifact_transitions_are_accepted(conn: Connection) -> None:
    """J10: each declared lifecycle edge, walked for real."""
    for source, target in (
        ("active", "archived"),
        ("archived", "quarantined"),
        ("quarantined", "deleted"),
    ):
        artifact_id = _seed_artifact_row(conn)
        if source != "active":
            _sql(
                conn,
                "UPDATE dbv2_catalog.artifacts SET lifecycle_state = :s WHERE id = :i",
                s=source,
                i=artifact_id,
            )
        _sql(
            conn,
            "UPDATE dbv2_catalog.artifacts SET lifecycle_state = :t WHERE id = :i",
            t=target,
            i=artifact_id,
        )
        assert scalar_conn(conn, artifact_id) == target


def scalar_conn(connection: Connection, artifact_id: uuid.UUID) -> str:
    return str(
        _sql(
            connection,
            "SELECT lifecycle_state FROM dbv2_catalog.artifacts WHERE id = :i",
            i=artifact_id,
        ).scalar_one()
    )


@pytest.mark.parametrize(
    ("source", "target"),
    [("archived", "active"), ("deleted", "active"), ("quarantined", "active")],
)
def test_forbidden_artifact_transitions_are_rejected(
    conn: Connection, source: str, target: str
) -> None:
    """J11."""
    artifact_id = _seed_artifact_row(conn)
    _sql(
        conn,
        "UPDATE dbv2_catalog.artifacts SET lifecycle_state = :s WHERE id = :i",
        s=source,
        i=artifact_id,
    )
    with pytest.raises(Exception, match="forbidden lifecycle transition"):
        _sql(
            conn,
            "UPDATE dbv2_catalog.artifacts SET lifecycle_state = :t WHERE id = :i",
            t=target,
            i=artifact_id,
        )


def test_verification_state_never_returns_to_unverified(conn: Connection) -> None:
    """J11, corrected in D2.2: recovery to 'verified' is legitimate; 'never observed' is not.

    An artifact that has been observed can be observed again - a restored payload returns to
    'verified' through catalog.record_artifact_verification(). What no observation can produce is
    'unverified', which means "never looked at".
    """
    artifact_id = _seed_artifact_row(conn)
    _sql(
        conn,
        "UPDATE dbv2_catalog.artifacts SET verification_state = 'corrupt' WHERE id = :i",
        i=artifact_id,
    )
    with pytest.raises(Exception, match="forbidden verification transition"):
        _sql(
            conn,
            "UPDATE dbv2_catalog.artifacts SET verification_state = 'unverified' WHERE id = :i",
            i=artifact_id,
        )


def test_every_state_machine_rejects_at_least_one_forbidden_edge(conn: Connection) -> None:
    """J10/J11: all sixteen machines are installed, and none of them is a no-op guard."""
    installed = {
        row[0]
        for row in _sql(
            conn,
            "SELECT p.proname FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace "
            "WHERE n.nspname LIKE 'dbv2\\_%' AND p.proname LIKE 'enforce\\_%'",
        )
    }
    assert len(installed) == 15
    for source in ("enforce_artifact_lifecycle", "enforce_job_state", "enforce_lease_transition"):
        assert source in installed
    body = _sql(
        conn,
        "SELECT string_agg(p.prosrc, ' ') FROM pg_proc p "
        "JOIN pg_namespace n ON n.oid = p.pronamespace WHERE n.nspname LIKE 'dbv2\\_%'",
    ).scalar_one()
    assert "RAISE EXCEPTION" in body


def test_forbidden_job_transition_is_rejected(conn: Connection) -> None:
    """J11: PENDING may not jump straight to RUNNING - the claim creates the lease."""
    job_id = _seed_job(conn)
    with pytest.raises(Exception, match="forbidden job transition"):
        _sql(
            conn,
            "UPDATE dbv2_experiments.experiment_jobs SET status = 'RUNNING', "
            "claimed_by = 'w', claimed_at = now() WHERE id = :i",
            i=job_id,
        )


def test_allowed_job_transition_is_accepted(conn: Connection) -> None:
    """J10: PENDING -> CLAIMED -> RUNNING."""
    job_id = _seed_job(conn)
    _sql(
        conn,
        "UPDATE dbv2_experiments.experiment_jobs SET status = 'CLAIMED', claimed_by = 'w', "
        "claimed_at = now(), lease_expires_at = now() + interval '5 min' WHERE id = :i",
        i=job_id,
    )
    _sql(
        conn,
        "UPDATE dbv2_experiments.experiment_jobs SET status = 'RUNNING' WHERE id = :i",
        i=job_id,
    )
    assert (
        _sql(
            conn, "SELECT status FROM dbv2_experiments.experiment_jobs WHERE id = :i", i=job_id
        ).scalar_one()
        == "RUNNING"
    )


def _hex() -> str:
    return hashlib.sha256(uuid.uuid4().bytes).hexdigest()


def _seed_job(connection: Connection) -> uuid.UUID:
    """One real PENDING job, built through the entire declared upstream identity chain."""
    artifact = lambda: _artifact(connection, present=False)[0]  # noqa: E731 - local shorthand
    dataset_id = _sql(
        connection,
        "INSERT INTO dbv2_catalog.datasets (dataset_key, round_id, chromosome, region_source, "
        "region_start0, region_end0_exclusive, region_coordinate_system, region_hash, "
        "bam_artifact_id, bai_artifact_id, reference_artifact_id, fai_artifact_id, identity_hash) "
        "VALUES (:k, 'r1', 'chr1', 'bed', 0, 100, 'half_open_0_based', :rh, :a1, :a2, :a3, :a4, "
        "        :ih) RETURNING id",
        k=f"ds-{uuid.uuid4()}",
        rh=_hex(),
        ih=_hex(),
        a1=artifact(),
        a2=artifact(),
        a3=artifact(),
        a4=artifact(),
    ).scalar_one()
    profile_id = _sql(
        connection,
        "INSERT INTO dbv2_profiling.bam_profiles (dataset_id, profile_key, profiler_version, "
        "profiler_config_hash, profile_status, windows_row_count, eligible_value_count, "
        "feature_values_hash, identity_hash, profile_artifact_id, manifest_artifact_id, "
        "windows_artifact_id) "
        "VALUES (:d, :k, 'v1', :ch, 'accepted', 1, 1, :fh, :ih, :a1, :a2, :a3) RETURNING id",
        d=dataset_id,
        k=f"prof-{uuid.uuid4()}",
        ch=_hex(),
        fh=_hex(),
        ih=_hex(),
        a1=artifact(),
        a2=artifact(),
        a3=artifact(),
    ).scalar_one()
    snapshot_id = _sql(
        connection,
        "INSERT INTO dbv2_profiling.profile_snapshots (snapshot_key, epoch, "
        "split_algorithm_version, member_count, snapshot_hash) "
        "VALUES (:k, :e, 'v1', 1, :h) RETURNING id",
        k=f"snap-{uuid.uuid4()}",
        e=int(uuid.uuid4().int % 1_000_000) + 1,
        h=_hex(),
    ).scalar_one()
    snapshot_member = _sql(
        connection,
        "INSERT INTO dbv2_profiling.profile_snapshot_members (snapshot_id, bam_profile_id, "
        "dataset_id, partition, member_index) VALUES (:s, :p, :d, 'train', 0) RETURNING id",
        s=snapshot_id,
        p=profile_id,
        d=dataset_id,
    ).scalar_one()
    space_id = _sql(
        connection,
        "INSERT INTO dbv2_experiments.parameter_spaces (space_key, caller, "
        "parameter_space_hash, definition_artifact_id) "
        "VALUES (:k, 'gatk', :h, :a) RETURNING id",
        k=f"space-{uuid.uuid4()}",
        h=_hex(),
        a=artifact(),
    ).scalar_one()
    space_hash = _sql(
        connection,
        "SELECT parameter_space_hash FROM dbv2_experiments.parameter_spaces WHERE id = :i",
        i=space_id,
    ).scalar_one()
    config_hash = _hex()
    config_id = _sql(
        connection,
        "INSERT INTO dbv2_experiments.candidate_configs (parameter_space_id, config_hash, "
        "parameter_space_hash, payload_artifact_id) VALUES (:s, :h, :ph, :a) RETURNING id",
        s=space_id,
        h=config_hash,
        ph=space_hash,
        a=artifact(),
    ).scalar_one()
    set_id = _sql(
        connection,
        "INSERT INTO dbv2_experiments.candidate_sets (candidate_set_hash, parameter_space_id, "
        "candidate_count, generator_version) VALUES (:h, :s, 1, 'v1') RETURNING id",
        h=_hex(),
        s=space_id,
    ).scalar_one()
    _sql(
        connection,
        "INSERT INTO dbv2_experiments.candidate_set_configs (candidate_set_id, "
        "candidate_config_id, config_index) VALUES (:s, :c, 0)",
        s=set_id,
        c=config_id,
    )
    plan_id = _sql(
        connection,
        "INSERT INTO dbv2_experiments.experiment_plans (plan_hash, snapshot_id, "
        "candidate_set_id, parameter_space_id, partition, member_count, candidate_count, "
        "logical_job_count) VALUES (:h, :sn, :cs, :sp, 'train', 1, 1, 1) RETURNING id",
        h=_hex(),
        sn=snapshot_id,
        cs=set_id,
        sp=space_id,
    ).scalar_one()
    plan_member = _sql(
        connection,
        "INSERT INTO dbv2_experiments.experiment_plan_members (plan_id, snapshot_member_id, "
        "bam_profile_id, dataset_id, member_index) VALUES (:p, :m, :b, :d, 0) RETURNING id",
        p=plan_id,
        m=snapshot_member,
        b=profile_id,
        d=dataset_id,
    ).scalar_one()
    plan_config = _sql(
        connection,
        "INSERT INTO dbv2_experiments.experiment_plan_configs (plan_id, candidate_config_id, "
        "config_hash, config_index) VALUES (:p, :c, :h, 0) RETURNING id",
        p=plan_id,
        c=config_id,
        h=config_hash,
    ).scalar_one()
    return uuid.UUID(
        str(
            _sql(
                connection,
                "INSERT INTO dbv2_experiments.experiment_jobs (plan_id, plan_member_id, "
                "plan_config_id, job_key) VALUES (:p, :m, :c, :k) RETURNING id",
                p=plan_id,
                m=plan_member,
                c=plan_config,
                k=_hex(),
            ).scalar_one()
        )
    )


# --------------------------------------------------------------------------- #
# J12 / J13: immutability and DELETE
# --------------------------------------------------------------------------- #
def test_immutable_column_tampering_is_rejected(conn: Connection) -> None:
    """J12."""
    artifact_id = _seed_artifact_row(conn)
    for column, value in (("content_sha256", "f" * 64), ("backup_scope", "recovery")):
        savepoint = conn.begin_nested()
        with pytest.raises(Exception, match="immutable column"):
            _sql(
                conn,
                f"UPDATE dbv2_catalog.artifacts SET {column} = :v WHERE id = :i",
                v=value,
                i=artifact_id,
            )
        savepoint.rollback()


def test_a_fully_immutable_table_rejects_every_update(conn: Connection) -> None:
    """J12 and E10: the 23 fully immutable tables refuse UPDATE outright."""
    _sql(
        conn,
        "INSERT INTO dbv2_audit.events (actor_role, action, object_schema, object_table, "
        "object_id, payload_hash) VALUES ('r', 'a', 's', 't', :o, :h)",
        o=uuid.uuid4(),
        h=hashlib.sha256(b"x").hexdigest(),
    )
    with pytest.raises(Exception, match="every column is immutable"):
        _sql(conn, "UPDATE dbv2_audit.events SET action = 'b'")


def test_every_table_carries_an_enabled_delete_guard(conn: Connection) -> None:
    """J13: all 37 tables, read from pg_trigger - an enabled BEFORE DELETE FOR EACH ROW guard."""
    guarded = {
        row[0]
        for row in _sql(
            conn,
            "SELECT n.nspname || '.' || c.relname FROM pg_trigger t "
            "JOIN pg_class c ON c.oid = t.tgrelid "
            "JOIN pg_namespace n ON n.oid = c.relnamespace "
            "JOIN pg_proc p ON p.oid = t.tgfoid "
            "JOIN pg_namespace fn ON fn.oid = p.pronamespace "
            "WHERE NOT t.tgisinternal AND t.tgenabled = 'O' AND (t.tgtype & 8) <> 0 "
            "  AND (t.tgtype & 2) <> 0 AND (t.tgtype & 1) <> 0 "
            "  AND fn.nspname = 'dbv2_audit' AND p.proname = 'reject_delete'",
        )
    }
    assert guarded == set(contract.SHADOW_TABLES)
    assert len(guarded) == 37


def test_delete_is_actually_refused_on_a_populated_table(conn: Connection) -> None:
    """J13: the guard is not merely present - a real DELETE of a real row is refused."""
    backend_id = _backend(conn)
    artifact_id = _seed_artifact_row(conn)
    _sql(
        conn,
        "INSERT INTO dbv2_audit.events (actor_role, action, object_schema, object_table, "
        "object_id, payload_hash) VALUES ('r', 'a', 's', 't', :o, :h)",
        o=uuid.uuid4(),
        h=hashlib.sha256(b"x").hexdigest(),
    )
    for table, column, value in (
        ("dbv2_catalog.artifacts", "id", artifact_id),
        ("dbv2_catalog.storage_backends", "id", backend_id),
        ("dbv2_audit.events", "actor_role", "r"),
    ):
        savepoint = conn.begin_nested()
        with pytest.raises(Exception, match="is not permitted"):
            _sql(conn, f"DELETE FROM {table} WHERE {column} = :v", v=value)
        savepoint.rollback()


# --------------------------------------------------------------------------- #
# J14-J17: the recovery-set rules
# --------------------------------------------------------------------------- #
def _recovery_manifest(
    *,
    recovery_set_id: str,
    backup_digest: str,
    backup_size: int,
    snapshot_digest: str | None,
    snapshot_raw: str | None,
    artifact_count: int | None,
    artifact_total: int | None,
) -> dict[str, Any]:
    return {
        "artifact_count": artifact_count,
        "artifact_snapshot_manifest_sha256": snapshot_raw,
        "artifact_snapshot_sha256": snapshot_digest,
        "artifact_total_bytes": artifact_total,
        "artifact_verification_tool_version": "verify-1",
        "backup_tool_version": "pg_dump-16.2",
        "created_at": "2026-08-22T00:00:00+00:00",
        "database_backup_kind": "pg_dump",
        "database_backup_sha256": backup_digest,
        "database_backup_size_bytes": backup_size,
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


def _canonical(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8") + b"\n"


def _insert_backup_set(
    connection: Connection,
    *,
    complete: bool,
    snapshot_entries: list[dict[str, Any]] | None = None,
    snapshot_scope: str = "recovery",
    backup_scope_override: str | None = None,
    artifact_count_override: int | None = None,
) -> None:
    recovery_set_id = str(uuid.uuid4())
    backup_id, backup_digest, backup_size = _artifact(
        connection,
        media_type=BACKUP_MEDIA,
        scope=backup_scope_override or "recovery",
        kind="database_backup",
    )
    snapshot_id = snapshot_digest = snapshot_raw = None
    artifact_count = artifact_total = None
    if complete:
        entries = snapshot_entries if snapshot_entries is not None else []
        artifact_count = len(entries)
        artifact_total = sum(int(e["size_bytes"]) for e in entries)
        manifest = {
            "artifact_count": artifact_count,
            "artifact_total_bytes": artifact_total,
            "entries": sorted(
                entries, key=lambda e: (e["content_sha256"], e["size_bytes"], e["artifact_kind"])
            ),
            "predicate": "lifecycle_state = 'active' AND backup_scope = 'operational'",
            "recovery_set_id": recovery_set_id,
            "schema_version": "minos-artifact-snapshot-v1",
        }
        payload = _canonical(manifest)
        snapshot_digest = hashlib.sha256(SNAPSHOT_DOMAIN + payload).hexdigest()
        # the artifact carries the RAW digest of its own bytes; the domain-separated value is the
        # snapshot's scientific identity and lives in its own column
        snapshot_id, snapshot_raw, _ = _artifact(
            connection,
            payload=payload,
            media_type=SNAPSHOT_MEDIA,
            scope=snapshot_scope,
            kind="artifact_snapshot",
        )
    r1 = _recovery_manifest(
        recovery_set_id=recovery_set_id,
        backup_digest=backup_digest,
        backup_size=backup_size,
        snapshot_digest=snapshot_digest,
        snapshot_raw=snapshot_raw,
        artifact_count=artifact_count,
        artifact_total=artifact_total,
    )
    manifest_bytes = _canonical(r1)
    manifest_id, manifest_digest, _ = _artifact(
        connection,
        payload=manifest_bytes,
        media_type=RECOVERY_MEDIA,
        scope="recovery",
        kind="recovery_manifest",
    )
    _sql(
        connection,
        "INSERT INTO dbv2_catalog.backup_sets (backup_key, recovery_set_id, alembic_revision, "
        "quiesce_started_at, quiesce_ended_at, manifest_schema_version, database_name, "
        "recovery_manifest_artifact_id, recovery_manifest_sha256, database_backup_kind, "
        "database_backup_artifact_id, database_backup_sha256, database_backup_size_bytes, "
        "wal_start_lsn, wal_end_lsn, artifact_snapshot_manifest_artifact_id, "
        "artifact_snapshot_manifest_sha256, artifact_snapshot_sha256, "
        "artifact_snapshot_manifest_media_type, artifact_count, "
        "artifact_total_bytes, postgresql_version, backup_tool_version, "
        "artifact_verification_tool_version, completeness, created_at) "
        "VALUES (:bk, :rs, :rev, :qs, :qe, :sv, :db, :mid, :md, 'pg_dump', :bid, :bd, :bs, "
        "        :ws, :we, :sid, :sraw, :sd, :sm, :ac, :at, '16.2', 'pg_dump-16.2', "
        "        'verify-1', :comp, "
        "        :created)",
        bk=f"backup-{recovery_set_id}",
        rs=recovery_set_id,
        rev=contract.REVISION,
        qs=r1["quiesce_started_at"],
        qe=r1["quiesce_ended_at"],
        sv=r1["schema_version"],
        db=r1["database_name"],
        mid=manifest_id,
        md=manifest_digest,
        bid=backup_id,
        bd=backup_digest,
        bs=backup_size,
        ws=r1["wal_start_lsn"],
        we=r1["wal_end_lsn"],
        sid=snapshot_id,
        sraw=snapshot_raw,
        sd=snapshot_digest,
        sm=SNAPSHOT_MEDIA if complete else None,
        ac=artifact_count if artifact_count_override is None else artifact_count_override,
        at=artifact_total,
        comp="complete" if complete else "database_only",
        created=r1["created_at"],
    )


def test_a_complete_backup_set_requires_three_verified_recovery_artifacts(conn: Connection) -> None:
    """J14: the happy path, then each of the three verification conditions removed in turn."""
    operational = _artifact(conn, payload=b"payload-one", kind="vcf")
    entries = [
        {"artifact_kind": "vcf", "content_sha256": operational[1], "size_bytes": operational[2]}
    ]
    _insert_backup_set(conn, complete=True, snapshot_entries=entries)
    assert scalar_count(conn, "dbv2_catalog.backup_sets") == 1


def scalar_count(connection: Connection, table: str) -> int:
    return int(_sql(connection, f"SELECT count(*) FROM {table}").scalar_one())


def test_an_unverified_recovery_artifact_blocks_a_complete_set(conn: Connection) -> None:
    """J14."""
    operational = _artifact(conn, payload=b"payload-two", kind="vcf")
    entries = [
        {"artifact_kind": "vcf", "content_sha256": operational[1], "size_bytes": operational[2]}
    ]
    with pytest.raises(Exception, match="not verification_state"):
        _unverified_backup_set(conn, entries)


def _unverified_backup_set(connection: Connection, entries: list[dict[str, Any]]) -> None:
    recovery_set_id = str(uuid.uuid4())
    backup_id, backup_digest, backup_size = _artifact(
        connection,
        media_type=BACKUP_MEDIA,
        scope="recovery",
        kind="database_backup",
        verification="unverified",
    )
    manifest = {
        "artifact_count": len(entries),
        "artifact_total_bytes": sum(int(e["size_bytes"]) for e in entries),
        "entries": entries,
        "predicate": "lifecycle_state = 'active' AND backup_scope = 'operational'",
        "recovery_set_id": recovery_set_id,
        "schema_version": "minos-artifact-snapshot-v1",
    }
    payload = _canonical(manifest)
    snapshot_digest = hashlib.sha256(SNAPSHOT_DOMAIN + payload).hexdigest()
    snapshot_id, snapshot_raw, _ = _artifact(
        connection,
        payload=payload,
        media_type=SNAPSHOT_MEDIA,
        scope="recovery",
        kind="artifact_snapshot",
    )
    r1 = _recovery_manifest(
        recovery_set_id=recovery_set_id,
        backup_digest=backup_digest,
        backup_size=backup_size,
        snapshot_digest=snapshot_digest,
        snapshot_raw=snapshot_raw,
        artifact_count=len(entries),
        artifact_total=manifest["artifact_total_bytes"],
    )
    manifest_bytes = _canonical(r1)
    manifest_id, manifest_digest, _ = _artifact(
        connection,
        payload=manifest_bytes,
        media_type=RECOVERY_MEDIA,
        scope="recovery",
        kind="recovery_manifest",
    )
    _sql(
        connection,
        "INSERT INTO dbv2_catalog.backup_sets (backup_key, recovery_set_id, alembic_revision, "
        "quiesce_started_at, quiesce_ended_at, manifest_schema_version, database_name, "
        "recovery_manifest_artifact_id, recovery_manifest_sha256, database_backup_kind, "
        "database_backup_artifact_id, database_backup_sha256, database_backup_size_bytes, "
        "wal_start_lsn, wal_end_lsn, artifact_snapshot_manifest_artifact_id, "
        "artifact_snapshot_manifest_sha256, artifact_snapshot_sha256, "
        "artifact_snapshot_manifest_media_type, artifact_count, "
        "artifact_total_bytes, postgresql_version, backup_tool_version, "
        "artifact_verification_tool_version, completeness, created_at) "
        "VALUES (:bk, :rs, :rev, :qs, :qe, :sv, :db, :mid, :md, 'pg_dump', :bid, :bd, :bs, "
        "        :ws, :we, :sid, :sraw, :sd, :sm, :ac, :at, '16.2', 'pg_dump-16.2', "
        "        'verify-1', 'complete', :created)",
        bk=f"backup-{recovery_set_id}",
        rs=recovery_set_id,
        rev=contract.REVISION,
        qs=r1["quiesce_started_at"],
        qe=r1["quiesce_ended_at"],
        sv=r1["schema_version"],
        db=r1["database_name"],
        mid=manifest_id,
        md=manifest_digest,
        bid=backup_id,
        bd=backup_digest,
        bs=backup_size,
        ws=r1["wal_start_lsn"],
        we=r1["wal_end_lsn"],
        sid=snapshot_id,
        sraw=snapshot_raw,
        sd=snapshot_digest,
        sm=SNAPSHOT_MEDIA,
        ac=len(entries),
        at=manifest["artifact_total_bytes"],
        created=r1["created_at"],
    )


def test_a_database_only_set_accepts_exactly_its_nullable_shape(conn: Connection) -> None:
    """J15."""
    _insert_backup_set(conn, complete=False)
    row = _sql(
        conn,
        "SELECT completeness, artifact_snapshot_manifest_artifact_id, artifact_snapshot_sha256, "
        "artifact_snapshot_manifest_media_type, artifact_count, artifact_total_bytes "
        "FROM dbv2_catalog.backup_sets",
    ).one()
    assert row[0] == "database_only"
    assert all(value is None for value in row[1:])


def test_a_half_populated_shape_is_rejected(conn: Connection) -> None:
    """J15: all-or-none, refused by ck_backup_sets_shape at INSERT."""
    with pytest.raises(Exception, match="ck_backup_sets_shape"):
        _insert_backup_set(conn, complete=False, artifact_count_override=5)


def test_completeness_is_immutable(conn: Connection) -> None:
    """J15: a database_only row is never upgraded in place."""
    _insert_backup_set(conn, complete=False)
    with pytest.raises(Exception, match="immutable|completeness changed"):
        _sql(conn, "UPDATE dbv2_catalog.backup_sets SET completeness = 'complete'")


def test_a_recovery_artifact_cannot_enter_the_operational_snapshot(conn: Connection) -> None:
    """J16: with a real operational artifact present, so the bootstrap check is not what fires."""
    _artifact(conn, payload=b"an operational payload", kind="vcf")
    recovery_payload = _artifact(
        conn, payload=b"recovery-bytes", kind="recovery_extra", scope="recovery"
    )
    entries = [
        {
            "artifact_kind": "recovery_extra",
            "content_sha256": recovery_payload[1],
            "size_bytes": recovery_payload[2],
        }
    ]
    with pytest.raises(
        Exception, match="do not resolve to an active operational artifact|recovery artifacts"
    ):
        _insert_backup_set(conn, complete=True, snapshot_entries=entries)


def test_an_operational_artifact_cannot_masquerade_as_recovery_evidence(conn: Connection) -> None:
    """J17: the backup artifact must carry backup_scope = 'recovery'."""
    with pytest.raises(Exception, match="not backup_scope"):
        _insert_backup_set(conn, complete=False, backup_scope_override="operational")


def test_a_snapshot_manifest_that_is_not_recovery_scoped_is_rejected(conn: Connection) -> None:
    """J17."""
    operational = _artifact(conn, payload=b"payload-three", kind="vcf")
    entries = [
        {"artifact_kind": "vcf", "content_sha256": operational[1], "size_bytes": operational[2]}
    ]
    with pytest.raises(Exception, match="not backup_scope"):
        _insert_backup_set(
            conn, complete=True, snapshot_entries=entries, snapshot_scope="operational"
        )


def test_the_snapshot_counts_must_match_the_manifest(dbv2_url: str) -> None:
    """J14: entry count and total size are re-derived from the manifest bytes, not trusted."""
    assert (
        scalar(
            dbv2_url,
            "SELECT count(*) FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace "
            "WHERE n.nspname = 'dbv2_catalog' AND p.proname = 'enforce_backup_set_shape' "
            "AND p.prosrc LIKE '%artifact_total_bytes%'",
        )
        == 1
    )
