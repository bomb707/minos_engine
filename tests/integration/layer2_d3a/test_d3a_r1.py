"""DB-V2 D3-A: R1 construction against a scratch V1 database and a temporary corpus."""

from __future__ import annotations

import hashlib
import os
import stat
import uuid
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import text

from minos_engine.storage.dbv2_recovery import (
    ARTIFACT_SNAPSHOT_DOMAIN,
    RECOVERY_MANIFEST_SCHEMA_VERSION,
    SNAPSHOT_SCHEMA_VERSION,
    R1Error,
    build_r1,
    canonical_json_bytes,
    capture_v1_fingerprint,
    hash_payload,
    run_pg_dump,
)
from minos_engine.storage.dbv2_recovery_store import (
    FILE_MODE,
    RecoveryRoot,
    RecoveryRootError,
)

from .conftest import build_corpus, connect, seed_v1_artifacts

pytestmark = [pytest.mark.integration, pytest.mark.postgres]

#: two corpora with DIFFERENT artifact counts, neither of them the live 227.
CORPUS_A = {"counts": {"corpus": 11, "features": 3}, "bare_path_roots": ("features",)}
CORPUS_B = {"counts": {"primary": 6}, "bare_path_roots": ()}


def _build(url: str, corpus: Any, root: RecoveryRoot, pg_dump: str, **overrides: Any) -> Any:
    engine, conn = connect(url)
    try:
        environ = dict(os.environ)
        environ["MINOS_DBV2_PG_DUMP"] = pg_dump
        parameters: dict[str, Any] = {
            "created_at": "2026-08-22T00:10:00+00:00",
            "quiesce_ended_at": "2026-08-22T00:05:00+00:00",
            "quiesce_started_at": "2026-08-22T00:00:00+00:00",
            "recovery_set_id": str(uuid.uuid4()),
        }
        parameters.update(overrides)
        return build_r1(
            conn,
            dsn=url,
            root=root,
            roots=corpus.artifact_roots(),
            environ=environ,
            **parameters,
        )
    finally:
        conn.rollback()
        conn.close()
        engine.dispose()


# --------------------------------------------------------------------------- #
# K1-K3
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("spec", [CORPUS_A, CORPUS_B], ids=["uneven-two-roots", "single-root"])
def test_r1_is_built_from_an_uneven_synthetic_corpus(
    v1_url: str, recovery_root: RecoveryRoot, pg_dump_executable: str, tmp_path: Path, spec: Any
) -> None:
    """K1: two corpora, different counts, both locator forms, several kinds and sizes."""
    corpus = build_corpus(tmp_path, name="a", **spec)
    seed_v1_artifacts(v1_url, corpus)
    bundle = _build(v1_url, corpus, recovery_root, pg_dump_executable)

    assert bundle.artifact_count == corpus.artifact_count
    assert bundle.artifact_total_bytes == corpus.total_bytes
    assert bundle.recovery_manifest["schema_version"] == RECOVERY_MANIFEST_SCHEMA_VERSION
    assert bundle.recovery_manifest["source_alembic_revision"] == "0005_l2e_feature_view"
    assert bundle.recovery_manifest["database_name"] == "minos_engine_db"

    # the three files are published, mode 0640, and hash to their own names
    assert {p.kind for p in bundle.published} == {"backup", "snapshot", "recovery"}
    for published in bundle.published:
        assert recovery_root.stat_mode(published.kind, published.sha256) == FILE_MODE
        assert (
            hashlib.sha256(recovery_root.read(published.kind, published.sha256)).hexdigest()
            == published.sha256
        )

    snapshot = bundle.snapshot_manifest_bytes
    assert hashlib.sha256(snapshot).hexdigest() == bundle.snapshot_manifest_sha256
    assert (
        hashlib.sha256(ARTIFACT_SNAPSHOT_DOMAIN + snapshot).hexdigest()
        == bundle.artifact_snapshot_sha256
    )
    assert bundle.snapshot_manifest_sha256 != bundle.artifact_snapshot_sha256


def test_r1_bytes_are_deterministic(
    v1_url: str, recovery_root: RecoveryRoot, pg_dump_executable: str, tmp_path: Path
) -> None:
    """K2: identical inputs give identical manifest bytes and identities.

    The dump itself carries a timestamp, so only the manifest digests that do not depend on it are
    compared - the snapshot manifest and its two identities.
    """
    corpus = build_corpus(tmp_path, name="det", **CORPUS_A)
    seed_v1_artifacts(v1_url, corpus)
    identity = str(uuid.uuid4())
    first = _build(v1_url, corpus, recovery_root, pg_dump_executable, recovery_set_id=identity)
    second = _build(v1_url, corpus, recovery_root, pg_dump_executable, recovery_set_id=identity)
    assert first.snapshot_manifest_bytes == second.snapshot_manifest_bytes
    assert first.snapshot_manifest_sha256 == second.snapshot_manifest_sha256
    assert first.artifact_snapshot_sha256 == second.artifact_snapshot_sha256
    assert canonical_json_bytes(
        {k: v for k, v in first.recovery_manifest.items() if not k.startswith("database_backup")}
    ) == canonical_json_bytes(
        {k: v for k, v in second.recovery_manifest.items() if not k.startswith("database_backup")}
    )


def test_r1_replay_does_not_rewrite_published_files(
    v1_url: str, recovery_root: RecoveryRoot, pg_dump_executable: str, tmp_path: Path
) -> None:
    """K3: the second publication of the same bytes is a verified no-op, inode included."""
    corpus = build_corpus(tmp_path, name="replay", **CORPUS_B)
    seed_v1_artifacts(v1_url, corpus)
    identity = str(uuid.uuid4())
    first = _build(v1_url, corpus, recovery_root, pg_dump_executable, recovery_set_id=identity)
    snapshot_path = recovery_root.path / recovery_root.relative_path_for(
        "snapshot", first.snapshot_manifest_sha256
    )
    before = os.stat(snapshot_path)
    second = _build(v1_url, corpus, recovery_root, pg_dump_executable, recovery_set_id=identity)
    after = os.stat(snapshot_path)
    assert second.snapshot_manifest_sha256 == first.snapshot_manifest_sha256
    assert (after.st_ino, after.st_mtime_ns) == (before.st_ino, before.st_mtime_ns)
    republished = next(p for p in second.published if p.kind == "snapshot")
    assert republished.already_present is True


# --------------------------------------------------------------------------- #
# K4-K8: failure injection
# --------------------------------------------------------------------------- #
def test_a_changed_database_during_capture_rejects_the_attempt(
    v1_url: str,
    recovery_root: RecoveryRoot,
    pg_dump_executable: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """K4: a real committed change between the two fingerprints refuses the whole R1."""
    corpus = build_corpus(tmp_path, name="moving", **CORPUS_B)
    seed_v1_artifacts(v1_url, corpus)
    engine, conn = connect(v1_url)
    try:
        before = capture_v1_fingerprint(conn)
        conn.rollback()
    finally:
        conn.close()
        engine.dispose()

    from minos_engine.storage import dbv2_recovery

    original = dbv2_recovery.scan_v1_artifacts
    inserted: dict[str, Any] = {}

    def scanning_then_changing(scan_conn: Any, roots: Any) -> Any:
        """Commit a real V1 change on a SEPARATE connection, mid-capture."""
        scanned = original(scan_conn, roots)
        if not inserted:
            other, side = connect(v1_url)
            try:
                with side.begin():
                    side.execute(
                        text(
                            "INSERT INTO catalog.artifacts (uri, sha256, media_type, size_bytes, "
                            "provenance) VALUES ('file:///tmp/injected', :h, 'application/json', "
                            "1, 'synthetic:injected')"
                        ),
                        {"h": "c" * 64},
                    )
                inserted["done"] = True
            finally:
                side.close()
                other.dispose()
        return scanned

    monkeypatch.setattr(dbv2_recovery, "scan_v1_artifacts", scanning_then_changing)
    with pytest.raises(R1Error, match="changed while R1 was being captured"):
        _build(v1_url, corpus, recovery_root, pg_dump_executable)

    engine, conn = connect(v1_url)
    try:
        after = capture_v1_fingerprint(conn)
        conn.rollback()
    finally:
        conn.close()
        engine.dispose()
    assert before != after, "the injected change must be visible in the fingerprint"


def test_a_missing_artifact_payload_fails(
    v1_url: str, recovery_root: RecoveryRoot, pg_dump_executable: str, tmp_path: Path
) -> None:
    """K5."""
    corpus = build_corpus(tmp_path, name="missing", **CORPUS_B)
    seed_v1_artifacts(v1_url, corpus)
    corpus.path_of(corpus.rows[0]).unlink()
    with pytest.raises(R1Error, match="payload is missing"):
        _build(v1_url, corpus, recovery_root, pg_dump_executable)


def test_changed_artifact_bytes_fail(
    v1_url: str, recovery_root: RecoveryRoot, pg_dump_executable: str, tmp_path: Path
) -> None:
    """K6: the V1 row's digest is what the bytes must equal."""
    corpus = build_corpus(tmp_path, name="changed", **CORPUS_B)
    seed_v1_artifacts(v1_url, corpus)
    target = corpus.path_of(corpus.rows[1])
    target.write_bytes(target.read_bytes() + b"tampered")
    with pytest.raises(R1Error, match="hashes to .*the V1 row says"):
        _build(v1_url, corpus, recovery_root, pg_dump_executable)


@pytest.mark.parametrize("attack", ["symlink", "fifo", "directory"])
def test_a_replaced_payload_inode_fails(
    v1_url: str,
    recovery_root: RecoveryRoot,
    pg_dump_executable: str,
    tmp_path: Path,
    attack: str,
) -> None:
    """K7: symlink, FIFO and directory are each refused at the syscall level."""
    corpus = build_corpus(tmp_path, name=f"attack-{attack}", **CORPUS_B)
    seed_v1_artifacts(v1_url, corpus)
    target = corpus.path_of(corpus.rows[0])
    payload = target.read_bytes()
    target.unlink()
    if attack == "symlink":
        decoy = tmp_path / "decoy.bin"
        decoy.write_bytes(payload)
        target.symlink_to(decoy)
    elif attack == "fifo":
        os.mkfifo(target)
    else:
        target.mkdir()
    with pytest.raises(R1Error, match="not a regular file"):
        _build(v1_url, corpus, recovery_root, pg_dump_executable)


#: an empty URI is already impossible in V1 (``ck_artifacts_uri_nonempty``), so it is not listed.
@pytest.mark.parametrize(
    "locator", ["s3://bucket/key", "file://remotehost/tmp/x", "relative/key", "/etc/passwd"]
)
def test_a_root_escaping_locator_fails(
    v1_url: str,
    recovery_root: RecoveryRoot,
    pg_dump_executable: str,
    tmp_path: Path,
    locator: str,
) -> None:
    """K7: a different scheme, a remote host, a relative key and an empty key are all refused."""
    corpus = build_corpus(tmp_path, name="escape", **CORPUS_B)
    seed_v1_artifacts(v1_url, corpus)
    engine, conn = connect(v1_url)
    try:
        with conn.begin():
            conn.execute(
                text("UPDATE catalog.artifacts SET uri = :u WHERE sha256 = :h"),
                {"u": locator, "h": corpus.rows[0]["sha256"]},
            )
    finally:
        conn.close()
        engine.dispose()
    with pytest.raises(R1Error):
        _build(v1_url, corpus, recovery_root, pg_dump_executable)


def test_a_payload_replaced_between_open_and_close_fails(tmp_path: Path) -> None:
    """K7: the same descriptor is re-stat-ed, so a swap during the read is detected."""
    target = tmp_path / "payload.bin"
    target.write_bytes(b"original bytes")
    observation = hash_payload(target)
    assert observation.sha256 == hashlib.sha256(b"original bytes").hexdigest()
    replacement = tmp_path / "replacement.bin"
    replacement.write_bytes(b"different bytes entirely")
    os.replace(replacement, target)
    again = hash_payload(target)
    assert again.inode != observation.inode
    assert again.sha256 != observation.sha256


def test_a_missing_dump_executable_fails(tmp_path: Path) -> None:
    """K8."""
    with pytest.raises(R1Error, match="is not set"):
        run_pg_dump("postgresql:///x", {})
    with pytest.raises(R1Error, match="must be an absolute path"):
        run_pg_dump("postgresql:///x", {"MINOS_DBV2_PG_DUMP": "pg_dump"})
    fake = tmp_path / "not-executable"
    fake.write_text("#!/bin/sh\n")
    with pytest.raises(R1Error, match="not an executable regular file"):
        run_pg_dump("postgresql:///x", {"MINOS_DBV2_PG_DUMP": str(fake)})


def test_a_failing_dump_fails_closed(tmp_path: Path) -> None:
    """K8: a nonzero exit is refused, and no partial evidence is produced."""
    failing = tmp_path / "failing-dump"
    failing.write_text(
        "#!/bin/sh\n"
        'case "$1" in --version) echo "pg_dump (PostgreSQL) 16.2"; exit 0;; esac\n'
        "echo 'connection refused' >&2\nexit 2\n"
    )
    failing.chmod(0o750)
    with pytest.raises(R1Error, match="pg_dump exited 2"):
        run_pg_dump("postgresql:///x", {"MINOS_DBV2_PG_DUMP": str(failing)})


def test_an_empty_dump_fails_closed(tmp_path: Path) -> None:
    """K8."""
    empty = tmp_path / "empty-dump"
    empty.write_text(
        "#!/bin/sh\n"
        'case "$1" in --version) echo "pg_dump (PostgreSQL) 16.2"; exit 0;; esac\n'
        'for a in "$@"; do case "$a" in --file=*) : > "${a#--file=}";; esac; done\nexit 0\n'
    )
    empty.chmod(0o750)
    with pytest.raises(R1Error, match="empty dump"):
        run_pg_dump("postgresql:///x", {"MINOS_DBV2_PG_DUMP": str(empty)})


def test_a_changed_published_dump_is_detected(
    v1_url: str, recovery_root: RecoveryRoot, pg_dump_executable: str, tmp_path: Path
) -> None:
    """K8: the recovery store re-hashes on read, so a tampered published file is caught."""
    corpus = build_corpus(tmp_path, name="tamper", **CORPUS_B)
    seed_v1_artifacts(v1_url, corpus)
    bundle = _build(v1_url, corpus, recovery_root, pg_dump_executable)
    dump_path = recovery_root.path / recovery_root.relative_path_for("backup", bundle.dump_sha256)
    os.chmod(dump_path, 0o640)
    dump_path.write_bytes(b"not the dump")
    with pytest.raises(Exception, match="hashes to"):
        recovery_root.read("backup", bundle.dump_sha256)


# --------------------------------------------------------------------------- #
# recovery-root contract
# --------------------------------------------------------------------------- #
def test_the_recovery_root_contract_is_enforced(tmp_path: Path) -> None:
    """E: no default, absolute only, must exist, exact mode, no symlink component."""
    with pytest.raises(RecoveryRootError, match="has no default"):
        RecoveryRoot.from_environment({})
    with pytest.raises(RecoveryRootError, match="must be absolute"):
        RecoveryRoot(Path("relative/root"))
    with pytest.raises(RecoveryRootError, match="must already exist"):
        RecoveryRoot(tmp_path / "absent")
    wrong_mode = tmp_path / "wrong-mode"
    wrong_mode.mkdir(mode=0o755)
    os.chmod(wrong_mode, 0o755)
    with pytest.raises(RecoveryRootError, match="must be mode 2750"):
        RecoveryRoot(wrong_mode)
    real = tmp_path / "real"
    real.mkdir(mode=0o2750)
    os.chmod(real, 0o2750)
    link = tmp_path / "linked"
    link.symlink_to(real)
    with pytest.raises(RecoveryRootError, match="symlink component"):
        RecoveryRoot(link / ".")


def test_the_recovery_manifest_carries_no_secret_material(
    v1_url: str, recovery_root: RecoveryRoot, pg_dump_executable: str, tmp_path: Path
) -> None:
    """G: no credential, DSN, repository path or host temporary path reaches the evidence."""
    corpus = build_corpus(tmp_path, name="secrets", **CORPUS_A)
    seed_v1_artifacts(v1_url, corpus)
    bundle = _build(v1_url, corpus, recovery_root, pg_dump_executable)
    for payload in (bundle.recovery_manifest_bytes, bundle.snapshot_manifest_bytes):
        text_payload = payload.decode("utf-8")
        for marker in ("postgresql://", "password", "PGPASSWORD", "/home/", str(tmp_path)):
            assert marker not in text_payload, marker
    assert bundle.recovery_manifest["schema_version"] == RECOVERY_MANIFEST_SCHEMA_VERSION
    snapshot = bundle.snapshot_manifest_bytes.decode("utf-8")
    assert SNAPSHOT_SCHEMA_VERSION in snapshot
    assert stat.S_IMODE(os.stat(recovery_root.path).st_mode) == 0o2750
