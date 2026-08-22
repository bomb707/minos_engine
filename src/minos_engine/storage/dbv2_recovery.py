"""DB-V2 D3-A phase R1: build a complete, deterministic external recovery set.

R1 is three files: a PostgreSQL custom-format dump, a canonical artifact-snapshot manifest and a
canonical recovery manifest. It is built from a database at ``0005_l2e_feature_view`` and, once
complete, is what authorizes the structural upgrade (S1).

Two things this module is careful never to claim:

* **It does not quiesce anything.** It fingerprints the database before and after the capture and
  refuses the whole attempt if anything moved. Equal fingerprints prove nothing changed *while it
  looked*; external write quiescence remains a deployment prerequisite, recorded as such.
* **It never touches an artifact payload.** Every read is descriptor-bound, no-follow, and
  re-stats the same descriptor afterwards. Nothing is written, renamed, chmod-ed or repaired.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import subprocess
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Final
from urllib.parse import unquote, urlsplit

from sqlalchemy import Connection, text

from .dbv2_recovery_store import PublishedFile, RecoveryRoot, RecoveryStoreError

__all__ = [
    "ARTIFACT_SNAPSHOT_DOMAIN",
    "ENV_ARTIFACT_ROOTS",
    "ENV_PG_DUMP",
    "R1Bundle",
    "R1Error",
    "RECOVERY_MANIFEST_SCHEMA_VERSION",
    "SNAPSHOT_PREDICATE",
    "SNAPSHOT_SCHEMA_VERSION",
    "ArtifactRoots",
    "build_r1",
    "canonical_json_bytes",
    "capture_v1_fingerprint",
]

ENV_ARTIFACT_ROOTS: Final = "MINOS_DBV2_ARTIFACT_ROOTS"
ENV_PG_DUMP: Final = "MINOS_DBV2_PG_DUMP"

ARTIFACT_SNAPSHOT_DOMAIN: Final = b"minos:db-v2-artifact-snapshot:v1\n"
SNAPSHOT_SCHEMA_VERSION: Final = "minos-artifact-snapshot-v1"
RECOVERY_MANIFEST_SCHEMA_VERSION: Final = "minos-db-recovery-manifest-v1"
SNAPSHOT_PREDICATE: Final = "lifecycle_state = 'active' AND backup_scope = 'operational'"
R1_SOURCE_REVISION: Final = "0005_l2e_feature_view"

#: the frozen artifact kind and retention class every bootstrapped V1 payload receives.
OPERATIONAL_RETENTION_CLASS: Final = "standard"

DUMP_TIMEOUT_SECONDS: Final = 900
MAX_DUMP_STDERR: Final = 64 * 1024
READ_CHUNK: Final = 1 << 20


class R1Error(RuntimeError):
    """R1 construction failed closed."""


# --------------------------------------------------------------------------- #
# canonical bytes
# --------------------------------------------------------------------------- #
def canonical_json_bytes(document: dict[str, Any]) -> bytes:
    """Sorted keys, tight separators, UTF-8, newline-terminated. The digests are over exactly this."""
    return json.dumps(document, sort_keys=True, separators=(",", ":")).encode("utf-8") + b"\n"


def _no_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    seen: dict[str, Any] = {}
    for key, value in pairs:
        if key in seen:
            raise R1Error(f"duplicate JSON key {key!r}")
        seen[key] = value
    return seen


def parse_strict(payload: bytes, *, required: frozenset[str]) -> dict[str, Any]:
    """Strict parse: duplicate keys rejected, and the field inventory must match exactly."""
    document = json.loads(payload.decode("utf-8"), object_pairs_hook=_no_duplicate_keys)
    if not isinstance(document, dict):
        raise R1Error("manifest is not a JSON object")
    present = set(document)
    if present != set(required):
        missing = sorted(set(required) - present)
        extra = sorted(present - set(required))
        raise R1Error(f"manifest field inventory: missing {missing}, unexpected {extra}")
    return document


# --------------------------------------------------------------------------- #
# provisioned artifact roots
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class ArtifactRoots:
    """The provisioned artifact roots, one storage backend per root.

    Declared as ``MINOS_DBV2_ARTIFACT_ROOTS=key=/abs/path[,key=/abs/path...]``. There is no
    default: an artifact whose payload does not resolve beneath exactly one declared root is a
    payload this deployment does not own, and R1 refuses it rather than guessing.
    """

    roots: tuple[tuple[str, Path], ...]

    @classmethod
    def from_environment(cls, environ: dict[str, str] | None = None) -> ArtifactRoots:
        source = os.environ if environ is None else environ
        raw = source.get(ENV_ARTIFACT_ROOTS)
        if not raw:
            raise R1Error(f"{ENV_ARTIFACT_ROOTS} is not set; it has no default")
        entries: list[tuple[str, Path]] = []
        for chunk in raw.split(","):
            key, separator, value = chunk.partition("=")
            if not separator or not key.strip() or not value.strip():
                raise R1Error(f"{ENV_ARTIFACT_ROOTS} entry {chunk!r} is not key=/absolute/path")
            path = Path(value.strip())
            if not path.is_absolute() or ".." in path.parts:
                raise R1Error(f"{ENV_ARTIFACT_ROOTS} root {path} must be absolute and clean")
            if not path.is_dir():
                raise R1Error(f"{ENV_ARTIFACT_ROOTS} root {path} does not exist")
            entries.append((key.strip(), path))
        keys = [key for key, _ in entries]
        if len(set(keys)) != len(keys):
            raise R1Error(f"{ENV_ARTIFACT_ROOTS} declares a duplicate backend key")
        return cls(tuple(sorted(entries)))

    def resolve(self, locator: str) -> tuple[str, str, Path]:
        """Map a V1 locator to (backend_key, clean relative object_key, absolute path).

        Accepts ``file://`` with an empty or ``localhost`` host, and a bare absolute POSIX path -
        the V1 catalog contains both. Everything else is refused.
        """
        if "://" in locator:
            parts = urlsplit(locator)
            if parts.scheme != "file":
                raise R1Error(f"unsupported artifact scheme {parts.scheme!r}: {locator}")
            if parts.netloc not in ("", "localhost"):
                raise R1Error(f"artifact URI names a remote host: {locator}")
            if parts.query or parts.fragment:
                raise R1Error(f"artifact URI carries a query or fragment: {locator}")
            raw_path = unquote(parts.path)
        else:
            raw_path = locator
        if not raw_path.startswith("/"):
            raise R1Error(f"artifact locator is not absolute: {locator}")
        pure = PurePosixPath(raw_path)
        if ".." in pure.parts or "" in pure.parts[1:] or raw_path.endswith("/"):
            raise R1Error(f"artifact locator is not a clean absolute path: {locator}")
        candidate = Path(raw_path)
        matches = [(key, root) for key, root in self.roots if candidate.is_relative_to(root)]
        if len(matches) != 1:
            raise R1Error(
                f"artifact {locator} resolves beneath {len(matches)} declared roots, expected 1"
            )
        key, root = matches[0]
        object_key = candidate.relative_to(root).as_posix()
        if not object_key or object_key.startswith("/") or ".." in object_key.split("/"):
            raise R1Error(f"artifact {locator} yields an unclean object_key {object_key!r}")
        return key, object_key, candidate


# --------------------------------------------------------------------------- #
# descriptor-bound payload reading
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class PayloadObservation:
    sha256: str
    size_bytes: int
    device: int
    inode: int


def hash_payload(path: Path) -> PayloadObservation:
    """Hash exactly the bytes behind one descriptor, and prove that descriptor did not move.

    ``O_NOFOLLOW`` refuses a symlink at the syscall level and ``O_NONBLOCK`` stops a planted FIFO
    from blocking forever before the regular-file check can reject it. The same descriptor is
    re-stat-ed afterwards, so a replacement between open and close is detected rather than hashed.
    """
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    except FileNotFoundError as error:
        raise R1Error(f"artifact payload is missing: {path}") from error
    except OSError as error:
        raise R1Error(f"artifact payload is not a regular file: {path}") from error
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode):
            raise R1Error(f"artifact payload is not a regular file: {path}")
        digest = hashlib.sha256()
        total = 0
        while chunk := os.read(fd, READ_CHUNK):
            digest.update(chunk)
            total += len(chunk)
        after = os.fstat(fd)
    finally:
        os.close(fd)
    if (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
        raise R1Error(f"artifact payload changed identity while being read: {path}")
    if before.st_size != after.st_size or after.st_size != total:
        raise R1Error(f"artifact payload changed size while being read: {path}")
    if before.st_mtime_ns != after.st_mtime_ns:
        raise R1Error(f"artifact payload was modified while being read: {path}")
    return PayloadObservation(digest.hexdigest(), total, after.st_dev, after.st_ino)


# --------------------------------------------------------------------------- #
# V1 fingerprint
# --------------------------------------------------------------------------- #
_FINGERPRINT_TABLES = (
    "catalog.artifacts",
    "catalog.datasets",
)


def capture_v1_fingerprint(conn: Connection) -> dict[str, Any]:
    """Revision, row counts and deterministic row hashes for the V1 schemas."""
    revision = str(conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one())
    relations = [
        (str(row[0]), str(row[1]))
        for row in conn.execute(
            text(
                "SELECT n.nspname, c.relname FROM pg_class c "
                "JOIN pg_namespace n ON n.oid = c.relnamespace "
                "WHERE c.relkind = 'r' AND n.nspname = ANY(:s) ORDER BY 1, 2"
            ),
            {
                "s": [
                    "catalog",
                    "profiling",
                    "experiments",
                    "evaluation",
                    "models",
                    "runtime",
                    "audit",
                ]
            },
        )
    ]
    counts: dict[str, int] = {}
    hashes: dict[str, str] = {}
    for schema, table in relations:
        ident = f'"{schema}"."{table}"'
        name = f"{schema}.{table}"
        counts[name] = int(conn.execute(text(f"SELECT count(*) FROM {ident}")).scalar_one())
        if counts[name]:
            hashes[name] = str(
                conn.execute(
                    text(
                        "SELECT md5(string_agg(t.row_text, E'\\n' ORDER BY t.row_text)) "
                        f"FROM (SELECT r::text AS row_text FROM {ident} AS r) AS t"
                    )
                ).scalar_one()
            )
    catalog_identity = str(
        conn.execute(
            text(
                "SELECT coalesce(md5(string_agg(a.sha256 || ':' || a.uri, E'\\n' ORDER BY "
                "a.sha256, a.uri)), '') FROM catalog.artifacts AS a"
            )
        ).scalar_one()
    )
    return {
        "artifact_catalog_identity": catalog_identity,
        "relations": [f"{s}.{t}" for s, t in relations],
        "revision": revision,
        "row_counts": counts,
        "row_hashes": hashes,
    }


# --------------------------------------------------------------------------- #
# database dump
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class DumpResult:
    payload: bytes
    sha256: str
    size_bytes: int
    tool_version: str
    tool_sha256: str


def _pg_dump_executable(environ: dict[str, str] | None = None) -> Path:
    source = os.environ if environ is None else environ
    raw = source.get(ENV_PG_DUMP)
    if not raw:
        raise R1Error(f"{ENV_PG_DUMP} is not set; the dump executable is provisioned, not searched")
    path = Path(raw)
    if not path.is_absolute():
        raise R1Error(f"{ENV_PG_DUMP} must be an absolute path, got {raw}")
    # a provisioned binary is routinely reached through an alternatives symlink; what pins it is
    # the digest of the bytes actually executed, which is recorded in the recovery manifest
    try:
        info = os.stat(path)
    except OSError as error:
        raise R1Error(f"{ENV_PG_DUMP} does not resolve to a file: {path}") from error
    if not stat.S_ISREG(info.st_mode) or not os.access(path, os.X_OK):
        raise R1Error(f"{ENV_PG_DUMP} is not an executable regular file: {path}")
    return path


def _libpq_env(dsn: str) -> dict[str, str]:
    """Decompose the DSN into discrete libpq variables.

    The connection reaches the child through the environment, never through argv: a DSN on argv is
    visible in ``ps`` to every user on the host, and a password in it would be too.
    """
    from sqlalchemy.engine import make_url

    url = make_url(dsn)
    env: dict[str, str] = {}
    if url.host:
        env["PGHOST"] = url.host
    if url.port:
        env["PGPORT"] = str(url.port)
    if url.username:
        env["PGUSER"] = url.username
    if url.password:
        env["PGPASSWORD"] = str(url.password)
    if url.database:
        env["PGDATABASE"] = url.database
    host_query = url.query.get("host")
    if host_query:
        env["PGHOST"] = host_query if isinstance(host_query, str) else host_query[0]
    return env


def _loader_path(executable: Path, source_env: Mapping[str, str]) -> str:
    """The shared-library path a relocatable PostgreSQL build needs, and nothing else."""
    candidates = [executable.parent.parent / "lib"]
    inherited = source_env.get("LD_LIBRARY_PATH", "")
    entries = [str(c) for c in candidates if c.is_dir()]
    if inherited:
        entries.append(inherited)
    return ":".join(entries)


def run_pg_dump(dsn: str, environ: dict[str, str] | None = None) -> DumpResult:
    """Run the provisioned ``pg_dump`` with a tokenized argv and a sanitized environment.

    The DSN reaches the child through ``PGSERVICE``-free libpq environment variables only; it is
    never placed on argv, so it cannot appear in ``ps`` output, a log line, a manifest or an audit
    row. A nonzero exit, a timeout, an empty dump or truncated output fails closed.
    """
    executable = _pg_dump_executable(environ)
    tool_digest = hashlib.sha256(executable.read_bytes()).hexdigest()
    # a sanitized environment: PATH, locale, the connection, and only the loader path the
    # provisioned binary needs. No PGPASSWORD, no PGSERVICE, nothing inherited by accident.
    source_env = os.environ if environ is None else environ
    child_env = {
        "PATH": "/usr/bin:/bin",
        "LC_ALL": "C",
        "PGCONNECT_TIMEOUT": "10",
        **_libpq_env(dsn),
    }
    library_path = _loader_path(executable, source_env)
    if library_path:
        child_env["LD_LIBRARY_PATH"] = library_path
    version_env = {k: v for k, v in child_env.items() if k not in ("PGDATABASE",)}
    version = subprocess.run(  # noqa: S603 - absolute executable, tokenized argv, shell=False
        [str(executable), "--version"],
        capture_output=True,
        check=False,
        env=version_env,
        shell=False,
        timeout=30,
    )
    if version.returncode != 0:
        detail = version.stderr[:MAX_DUMP_STDERR].decode("utf-8", "replace").strip()
        raise R1Error(f"pg_dump --version exited {version.returncode}: {detail[:300]}")
    tool_version = version.stdout.decode("utf-8", "replace").strip()

    with tempfile.TemporaryDirectory(prefix="minos_r1_dump_") as workspace:
        target = Path(workspace) / "dump.custom"
        argv = [
            str(executable),
            "--format=custom",
            "--no-owner",
            "--no-privileges",
            "--no-password",
            f"--file={target}",
        ]
        try:
            completed = subprocess.run(  # noqa: S603 - tokenized argv, shell=False
                argv,
                capture_output=True,
                check=False,
                cwd=workspace,
                env=child_env,
                shell=False,
                timeout=DUMP_TIMEOUT_SECONDS,
            )
        except subprocess.TimeoutExpired as error:
            raise R1Error(f"pg_dump timed out after {DUMP_TIMEOUT_SECONDS}s") from error
        stderr = completed.stderr[:MAX_DUMP_STDERR].decode("utf-8", "replace")
        if completed.returncode != 0:
            raise R1Error(f"pg_dump exited {completed.returncode}: {stderr.strip()[:400]}")
        if not target.is_file():
            raise R1Error("pg_dump produced no output file")
        payload = target.read_bytes()
    if not payload:
        raise R1Error("pg_dump produced an empty dump")
    return DumpResult(
        payload=payload,
        sha256=hashlib.sha256(payload).hexdigest(),
        size_bytes=len(payload),
        tool_version=tool_version,
        tool_sha256=tool_digest,
    )


# --------------------------------------------------------------------------- #
# artifact snapshot
# --------------------------------------------------------------------------- #
SNAPSHOT_REQUIRED_FIELDS: Final = frozenset(
    {
        "artifact_count",
        "artifact_total_bytes",
        "entries",
        "predicate",
        "recovery_set_id",
        "schema_version",
    }
)
RECOVERY_REQUIRED_FIELDS: Final = frozenset(
    {
        "artifact_count",
        "artifact_snapshot_manifest_sha256",
        "artifact_snapshot_sha256",
        "artifact_total_bytes",
        "artifact_verification_tool_version",
        "backup_tool_version",
        "created_at",
        "database_backup_kind",
        "database_backup_sha256",
        "database_backup_size_bytes",
        "database_name",
        "postgresql_version",
        "quiesce_ended_at",
        "quiesce_started_at",
        "recovery_set_id",
        "schema_version",
        "source_alembic_revision",
        "wal_end_lsn",
        "wal_start_lsn",
    }
)


@dataclass(frozen=True, slots=True)
class ScannedArtifact:
    v1_id: str
    locator: str
    backend_key: str
    object_key: str
    sha256: str
    size_bytes: int
    media_type: str
    artifact_kind: str


def scan_v1_artifacts(conn: Connection, roots: ArtifactRoots) -> tuple[ScannedArtifact, ...]:
    """Read every V1 artifact, resolve it beneath a declared root, and hash its exact bytes."""
    rows = conn.execute(
        text(
            "SELECT id, uri, sha256, size_bytes, media_type, provenance "
            "FROM catalog.artifacts ORDER BY sha256, uri"
        )
    ).all()
    scanned: list[ScannedArtifact] = []
    for row in rows:
        v1_id, locator, digest, size_bytes, media_type, provenance = row
        backend_key, object_key, path = roots.resolve(str(locator))
        observation = hash_payload(path)
        if observation.sha256 != str(digest).strip():
            raise R1Error(
                f"artifact {v1_id} hashes to {observation.sha256}, the V1 row says {digest}"
            )
        if size_bytes is not None and observation.size_bytes != int(size_bytes):
            raise R1Error(
                f"artifact {v1_id} is {observation.size_bytes} bytes, the V1 row says {size_bytes}"
            )
        scanned.append(
            ScannedArtifact(
                v1_id=str(v1_id),
                locator=str(locator),
                backend_key=backend_key,
                object_key=object_key,
                sha256=observation.sha256,
                size_bytes=observation.size_bytes,
                media_type=str(media_type),
                artifact_kind=str(provenance),
            )
        )
    return tuple(scanned)


def build_snapshot_manifest(
    artifacts: tuple[ScannedArtifact, ...], recovery_set_id: str
) -> tuple[bytes, str, str]:
    """The canonical snapshot manifest, its raw digest and its domain-separated identity."""
    entries: list[dict[str, Any]] = sorted(
        (
            {
                "artifact_kind": artifact.artifact_kind,
                "content_sha256": artifact.sha256,
                "size_bytes": artifact.size_bytes,
            }
            for artifact in artifacts
        ),
        key=lambda entry: (entry["content_sha256"], entry["size_bytes"], entry["artifact_kind"]),
    )
    seen = {(e["content_sha256"], e["size_bytes"], e["artifact_kind"]) for e in entries}
    if len(seen) != len(entries):
        raise R1Error("the V1 catalog yields a duplicate snapshot entry")
    manifest = {
        "artifact_count": len(entries),
        "artifact_total_bytes": sum(int(str(e["size_bytes"])) for e in entries),
        "entries": entries,
        "predicate": SNAPSHOT_PREDICATE,
        "recovery_set_id": recovery_set_id,
        "schema_version": SNAPSHOT_SCHEMA_VERSION,
    }
    payload = canonical_json_bytes(manifest)
    return (
        payload,
        hashlib.sha256(payload).hexdigest(),
        hashlib.sha256(ARTIFACT_SNAPSHOT_DOMAIN + payload).hexdigest(),
    )


# --------------------------------------------------------------------------- #
# the bundle
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class R1Bundle:
    """Everything R1 produced, plus the published evidence identities."""

    recovery_set_id: str
    recovery_manifest: dict[str, Any]
    recovery_manifest_bytes: bytes
    recovery_manifest_sha256: str
    snapshot_manifest_bytes: bytes
    snapshot_manifest_sha256: str
    artifact_snapshot_sha256: str
    dump_sha256: str
    dump_size_bytes: int
    artifacts: tuple[ScannedArtifact, ...]
    published: tuple[PublishedFile, ...]

    @property
    def artifact_count(self) -> int:
        return len(self.artifacts)

    @property
    def artifact_total_bytes(self) -> int:
        return sum(artifact.size_bytes for artifact in self.artifacts)


def build_r1(
    conn: Connection,
    *,
    dsn: str,
    root: RecoveryRoot,
    roots: ArtifactRoots,
    recovery_set_id: str,
    quiesce_started_at: str,
    quiesce_ended_at: str,
    created_at: str,
    environ: dict[str, str] | None = None,
) -> R1Bundle:
    """Build and publish a complete R1 recovery set from a database at 0005.

    The caller supplies the identity and the timestamps; nothing else is caller-controlled. No
    caller-supplied digest, count or verification result is accepted anywhere.
    """
    revision = str(conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one())
    if revision != R1_SOURCE_REVISION:
        raise R1Error(f"R1 must be built from {R1_SOURCE_REVISION}, this database is at {revision}")
    database_name = str(conn.execute(text("SELECT current_database()")).scalar_one())
    postgresql_version = str(conn.execute(text("SHOW server_version")).scalar_one())
    wal_start = _current_wal_lsn(conn)

    before = capture_v1_fingerprint(conn)
    dump = run_pg_dump(dsn, environ)
    artifacts = scan_v1_artifacts(conn, roots)
    after = capture_v1_fingerprint(conn)
    if before != after:
        raise R1Error(
            "the V1 database changed while R1 was being captured; the whole attempt is rejected. "
            "Equal fingerprints would not have proved quiescence either - external write "
            "quiescence is a deployment prerequisite, not something this code can establish."
        )
    wal_end = _current_wal_lsn(conn)

    snapshot_bytes, snapshot_raw, snapshot_scientific = build_snapshot_manifest(
        artifacts, recovery_set_id
    )
    manifest = {
        "artifact_count": len(artifacts),
        "artifact_snapshot_manifest_sha256": snapshot_raw,
        "artifact_snapshot_sha256": snapshot_scientific,
        "artifact_total_bytes": sum(a.size_bytes for a in artifacts),
        "artifact_verification_tool_version": _verification_tool_version(),
        "backup_tool_version": dump.tool_version,
        "created_at": created_at,
        "database_backup_kind": "pg_dump",
        "database_backup_sha256": dump.sha256,
        "database_backup_size_bytes": dump.size_bytes,
        "database_name": database_name,
        "postgresql_version": postgresql_version,
        "quiesce_ended_at": quiesce_ended_at,
        "quiesce_started_at": quiesce_started_at,
        "recovery_set_id": recovery_set_id,
        "schema_version": RECOVERY_MANIFEST_SCHEMA_VERSION,
        "source_alembic_revision": revision,
        "wal_end_lsn": wal_end,
        "wal_start_lsn": wal_start,
    }
    manifest_bytes = canonical_json_bytes(manifest)
    parse_strict(manifest_bytes, required=RECOVERY_REQUIRED_FIELDS)
    parse_strict(snapshot_bytes, required=SNAPSHOT_REQUIRED_FIELDS)

    try:
        published = (
            root.publish("backup", dump.payload),
            root.publish("snapshot", snapshot_bytes),
            root.publish("recovery", manifest_bytes),
        )
    except RecoveryStoreError as error:
        raise R1Error(f"publishing the R1 recovery set failed: {error}") from error

    return R1Bundle(
        recovery_set_id=recovery_set_id,
        recovery_manifest=manifest,
        recovery_manifest_bytes=manifest_bytes,
        recovery_manifest_sha256=hashlib.sha256(manifest_bytes).hexdigest(),
        snapshot_manifest_bytes=snapshot_bytes,
        snapshot_manifest_sha256=snapshot_raw,
        artifact_snapshot_sha256=snapshot_scientific,
        dump_sha256=dump.sha256,
        dump_size_bytes=dump.size_bytes,
        artifacts=artifacts,
        published=published,
    )


def _current_wal_lsn(conn: Connection) -> str:
    """The WAL position, where the server exposes one. A replica reports the replay position."""
    try:
        value = conn.execute(
            text(
                "SELECT CASE WHEN pg_is_in_recovery() THEN pg_last_wal_replay_lsn()::text "
                "ELSE pg_current_wal_lsn()::text END"
            )
        ).scalar_one()
    except Exception:  # noqa: BLE001 - a server without WAL access is recorded, not fatal
        return ""
    return "" if value is None else str(value)


def _verification_tool_version() -> str:
    from minos_engine import __version__ as engine_version

    return f"minos-dbv2-artifact-verify/{engine_version}"


def load_r1_bundle_from_store(root: RecoveryRoot, recovery_manifest_sha256: str) -> dict[str, Any]:
    """Re-read a published R1 recovery manifest and prove its bytes. No caller input is trusted."""
    payload = root.read("recovery", recovery_manifest_sha256)
    manifest = parse_strict(payload, required=RECOVERY_REQUIRED_FIELDS)
    if canonical_json_bytes(manifest) != payload:
        raise R1Error("the published recovery manifest is not in canonical form")
    snapshot_payload = root.read("snapshot", str(manifest["artifact_snapshot_manifest_sha256"]))
    scientific = hashlib.sha256(ARTIFACT_SNAPSHOT_DOMAIN + snapshot_payload).hexdigest()
    if scientific != manifest["artifact_snapshot_sha256"]:
        raise R1Error("the published snapshot manifest does not recompute to its identity")
    snapshot = parse_strict(snapshot_payload, required=SNAPSHOT_REQUIRED_FIELDS)
    if canonical_json_bytes(snapshot) != snapshot_payload:
        raise R1Error("the published snapshot manifest is not in canonical form")
    if not root.exists("backup", str(manifest["database_backup_sha256"])):
        raise R1Error("the published database dump is missing from the recovery root")
    return {
        "manifest": manifest,
        "manifest_bytes": payload,
        "snapshot": snapshot,
        "snapshot_bytes": snapshot_payload,
    }


# --------------------------------------------------------------------------- #
# R2: register the recovery set in the shadow schema
# --------------------------------------------------------------------------- #
#: the recovery root is itself a storage backend, so the external dump has a real location.
RECOVERY_BACKEND_KEY: Final = "minos-recovery-root"
SHADOW_REVISION: Final = "0009_dbv2_shadow_schema"
DATABASE_BACKUP_MEDIA_TYPE: Final = "application/vnd.postgresql.dump"
RECOVERY_MANIFEST_MEDIA_TYPE: Final = "application/vnd.minos.db-recovery-manifest+json"
SNAPSHOT_MANIFEST_MEDIA_TYPE: Final = "application/vnd.minos.artifact-snapshot+json"


class R2Error(RuntimeError):
    """R2 registration failed closed."""


@dataclass(frozen=True, slots=True)
class R2Result:
    backup_set_id: str
    recovery_manifest_artifact_id: str
    snapshot_manifest_artifact_id: str
    dump_artifact_id: str
    dump_location_id: str
    already_registered: bool


def register_r2(conn: Connection, *, bundle: R1Bundle, root: RecoveryRoot) -> R2Result:
    """Register the complete recovery set, inside the caller's transaction, after B0.

    The dump is re-read from the recovery root and re-hashed here; R1's word about it is not
    trusted. A conflicting replay rolls the whole R2 transaction back and leaves B0 untouched.
    """
    revision = str(conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one())
    if revision != SHADOW_REVISION:
        raise R2Error(f"R2 requires {SHADOW_REVISION}, this database is at {revision}")

    expected = {(a.sha256, a.size_bytes, a.artifact_kind) for a in bundle.artifacts}
    observed = {
        (str(row[0]), int(row[1]), str(row[2]))
        for row in conn.execute(
            text(
                "SELECT content_sha256, size_bytes, artifact_kind FROM dbv2_catalog.artifacts "
                "WHERE lifecycle_state = 'active' AND backup_scope = 'operational'"
            )
        )
    }
    if observed != expected:
        raise R2Error(
            f"the shadow artifact catalog is not the R1 set: {len(expected - observed)} missing, "
            f"{len(observed - expected)} extra. R2 requires a completed B0."
        )

    manifest_artifact = _publish_recovery_artifact(
        conn,
        payload=bundle.recovery_manifest_bytes,
        media_type=RECOVERY_MANIFEST_MEDIA_TYPE,
        kind="recovery_manifest",
        schema_version=RECOVERY_MANIFEST_SCHEMA_VERSION,
    )
    snapshot_artifact = _publish_recovery_artifact(
        conn,
        payload=bundle.snapshot_manifest_bytes,
        media_type=SNAPSHOT_MANIFEST_MEDIA_TYPE,
        kind="artifact_snapshot",
        schema_version=SNAPSHOT_SCHEMA_VERSION,
    )

    dump_bytes = root.read("backup", bundle.dump_sha256)
    observed_digest = hashlib.sha256(dump_bytes).hexdigest()
    if observed_digest != bundle.dump_sha256 or len(dump_bytes) != bundle.dump_size_bytes:
        raise R2Error("the published database dump does not match the R1 manifest")

    _ensure_recovery_backend(conn, root)
    dump_artifact = str(
        conn.execute(
            text(
                "SELECT dbv2_catalog.get_or_verify_external_artifact("
                ":d, :s, :m, 'database_backup', 'recovery', :rc, :sv, CAST(:p AS jsonb))"
            ),
            {
                "d": bundle.dump_sha256,
                "s": bundle.dump_size_bytes,
                "m": DATABASE_BACKUP_MEDIA_TYPE,
                "rc": "permanent",
                "sv": RECOVERY_MANIFEST_SCHEMA_VERSION,
                "p": json.dumps(
                    {"phase": "R2", "recovery_set_id": bundle.recovery_set_id},
                    sort_keys=True,
                    separators=(",", ":"),
                ),
            },
        ).scalar_one()
    )
    location_id = str(
        conn.execute(
            text("SELECT dbv2_catalog.get_or_verify_artifact_location(:a, :b, :k, true)"),
            {
                "a": dump_artifact,
                "b": RECOVERY_BACKEND_KEY,
                "k": root.relative_path_for("backup", bundle.dump_sha256),
            },
        ).scalar_one()
    )
    outcome = str(
        conn.execute(
            text("SELECT dbv2_catalog.record_artifact_verification(:a, :d, :s, :l)"),
            {
                "a": dump_artifact,
                "d": observed_digest,
                "s": len(dump_bytes),
                "l": location_id,
            },
        ).scalar_one()
    )
    if outcome != "verified":
        raise R2Error(f"the database dump verified as {outcome}")

    existing = conn.execute(
        text("SELECT id FROM dbv2_catalog.backup_sets WHERE recovery_set_id = :r"),
        {"r": bundle.recovery_set_id},
    ).one_or_none()
    call = dict(bundle.recovery_manifest)
    call.update(
        {
            "artifact_snapshot_manifest_artifact_id": snapshot_artifact,
            "backup_key": f"backup-{bundle.recovery_set_id}",
            "database_backup_artifact_id": dump_artifact,
            "recovery_manifest_artifact_id": manifest_artifact,
            "recovery_manifest_sha256": bundle.recovery_manifest_sha256,
        }
    )
    backup_set_id = str(
        conn.execute(
            text("SELECT dbv2_catalog.register_backup_set(CAST(:m AS jsonb), 'complete')"),
            {"m": json.dumps(call, sort_keys=True, separators=(",", ":"))},
        ).scalar_one()
    )
    _reread_and_require_equality(conn, bundle, backup_set_id)
    return R2Result(
        backup_set_id=backup_set_id,
        recovery_manifest_artifact_id=manifest_artifact,
        snapshot_manifest_artifact_id=snapshot_artifact,
        dump_artifact_id=dump_artifact,
        dump_location_id=location_id,
        already_registered=existing is not None,
    )


def _publish_recovery_artifact(
    conn: Connection, *, payload: bytes, media_type: str, kind: str, schema_version: str
) -> str:
    return str(
        conn.execute(
            text(
                "SELECT dbv2_catalog.get_or_verify_inline_artifact("
                ":p, :m, :k, 'recovery', 'permanent', :sv, CAST(:prov AS jsonb))"
            ),
            {
                "p": payload,
                "m": media_type,
                "k": kind,
                "sv": schema_version,
                "prov": json.dumps({"phase": "R2"}, sort_keys=True, separators=(",", ":")),
            },
        ).scalar_one()
    )


def _ensure_recovery_backend(conn: Connection, root: RecoveryRoot) -> None:
    existing = conn.execute(
        text(
            "SELECT logical_root FROM dbv2_catalog.storage_backends WHERE backend_key = :k "
            "FOR UPDATE"
        ),
        {"k": RECOVERY_BACKEND_KEY},
    ).one_or_none()
    if existing is not None:
        if str(existing[0]) != str(root.path):
            raise R2Error(
                f"backend {RECOVERY_BACKEND_KEY!r} is registered with a different logical root"
            )
        return
    conn.execute(
        text(
            "INSERT INTO dbv2_catalog.storage_backends (backend_key, backend_type, logical_root) "
            "VALUES (:k, 'local_fs', :r)"
        ),
        {"k": RECOVERY_BACKEND_KEY, "r": str(root.path)},
    )


def _reread_and_require_equality(conn: Connection, bundle: R1Bundle, backup_set_id: str) -> None:
    row = conn.execute(
        text(
            "SELECT completeness, recovery_manifest_sha256, artifact_snapshot_manifest_sha256, "
            "       artifact_snapshot_sha256, database_backup_sha256, artifact_count, "
            "       artifact_total_bytes, database_backup_size_bytes, alembic_revision "
            "FROM dbv2_catalog.backup_sets WHERE id = :i"
        ),
        {"i": backup_set_id},
    ).one()
    expected = (
        "complete",
        bundle.recovery_manifest_sha256,
        bundle.snapshot_manifest_sha256,
        bundle.artifact_snapshot_sha256,
        bundle.dump_sha256,
        bundle.artifact_count,
        bundle.artifact_total_bytes,
        bundle.dump_size_bytes,
        str(bundle.recovery_manifest["source_alembic_revision"]),
    )
    observed = (
        str(row[0]),
        str(row[1]),
        str(row[2]),
        str(row[3]),
        str(row[4]),
        int(row[5]),
        int(row[6]),
        int(row[7]),
        str(row[8]),
    )
    if observed != expected:
        raise R2Error("the registered backup set does not re-read equal to R1")
    admin = int(
        conn.execute(
            text("SELECT count(*) FROM dbv2_audit.admin_operations WHERE backup_set_id = :i"),
            {"i": backup_set_id},
        ).scalar_one()
    )
    if admin != 1:
        raise R2Error(f"the backup set has {admin} administrative audit rows, expected exactly 1")
