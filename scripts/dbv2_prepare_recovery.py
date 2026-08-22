"""Administrative CLI for the DB-V2 D3-A recovery sequence.

Four subcommands, one phase each, each requiring its own explicit invocation:

    build-r1              R1: dump, artifact snapshot, recovery manifest (source must be 0005)
    bootstrap-artifacts   B0: the artifact catalog and its locations only (must be 0009)
    register-r2           R2: register the complete recovery set (must be 0009, after B0)
    verify                the non-mutating D3-A verifier

There is deliberately no ``all`` and no ``--yes``. Combining the phases into one invocation would
turn an operational migration into a single keystroke, and each phase must verify the previous one
against the database rather than against a flag the caller passed. **No subcommand runs Alembic.**

Every database phase opens its own connection, verifies that connection's identity before any
other query, and verifies the exact Alembic revision it requires. Authorization is never carried
over from a previous connection.
"""

from __future__ import annotations

import argparse
import json
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import Connection, create_engine, text

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT / "src") not in sys.path:  # pragma: no cover - CLI convenience only
    sys.path.insert(0, str(REPO_ROOT / "src"))

from minos_engine.storage.database import (  # noqa: E402
    database_url,
    normalize_database_url,
    verify_operational_database_identity,
)
from minos_engine.storage.dbv2_artifact_bootstrap import bootstrap_artifacts  # noqa: E402
from minos_engine.storage.dbv2_d3a_verifier import verify_d3a  # noqa: E402
from minos_engine.storage.dbv2_recovery import (  # noqa: E402
    ArtifactRoots,
    R1Bundle,
    build_r1,
    load_r1_bundle_from_store,
    register_r2,
    scan_v1_artifacts,
)
from minos_engine.storage.dbv2_recovery_store import RecoveryRoot  # noqa: E402

R1_SOURCE_REVISION = "0005_l2e_feature_view"
SHADOW_REVISION = "0009_dbv2_shadow_schema"


def _require_revision(conn: Connection, expected: str) -> None:
    revision = str(conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one())
    if revision != expected:
        raise SystemExit(f"FAIL: this database is at {revision}, this phase requires {expected}")


def _connect() -> tuple[object, Connection]:
    """One connection per phase, identity-verified before any other query."""
    engine = create_engine(normalize_database_url(database_url()))
    conn = engine.connect()
    verify_operational_database_identity(conn)
    return engine, conn


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _bundle_from_store(root: RecoveryRoot, digest: str, conn: Connection) -> R1Bundle:
    """Rebuild the in-memory bundle from PUBLISHED bytes plus a live V1 scan.

    Nothing is taken from the caller except the manifest digest, which is then proved against the
    bytes it names.
    """
    loaded = load_r1_bundle_from_store(root, digest)
    manifest = loaded["manifest"]
    roots = ArtifactRoots.from_environment()
    artifacts = scan_v1_artifacts(conn, roots)
    return R1Bundle(
        recovery_set_id=str(manifest["recovery_set_id"]),
        recovery_manifest=manifest,
        recovery_manifest_bytes=bytes(loaded["manifest_bytes"]),
        recovery_manifest_sha256=digest,
        snapshot_manifest_bytes=bytes(loaded["snapshot_bytes"]),
        snapshot_manifest_sha256=str(manifest["artifact_snapshot_manifest_sha256"]),
        artifact_snapshot_sha256=str(manifest["artifact_snapshot_sha256"]),
        dump_sha256=str(manifest["database_backup_sha256"]),
        dump_size_bytes=int(manifest["database_backup_size_bytes"]),
        artifacts=artifacts,
        published=(),
    )


def command_build_r1(args: argparse.Namespace) -> int:
    root = RecoveryRoot.from_environment()
    roots = ArtifactRoots.from_environment()
    engine, conn = _connect()
    try:
        _require_revision(conn, R1_SOURCE_REVISION)
        started = _now()
        bundle = build_r1(
            conn,
            dsn=database_url(),
            root=root,
            roots=roots,
            recovery_set_id=str(uuid.uuid4()),
            quiesce_started_at=args.quiesce_started_at or started,
            quiesce_ended_at=args.quiesce_ended_at or _now(),
            created_at=_now(),
        )
        conn.rollback()
    finally:
        conn.close()
        engine.dispose()  # type: ignore[attr-defined]
    print(
        json.dumps(
            {
                "artifact_count": bundle.artifact_count,
                "artifact_snapshot_sha256": bundle.artifact_snapshot_sha256,
                "artifact_total_bytes": bundle.artifact_total_bytes,
                "database_backup_sha256": bundle.dump_sha256,
                "published": [p.relative_path for p in bundle.published],
                "recovery_manifest_sha256": bundle.recovery_manifest_sha256,
                "recovery_set_id": bundle.recovery_set_id,
                "snapshot_manifest_sha256": bundle.snapshot_manifest_sha256,
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


def command_bootstrap_artifacts(args: argparse.Namespace) -> int:
    root = RecoveryRoot.from_environment()
    roots = ArtifactRoots.from_environment()
    engine, conn = _connect()
    try:
        _require_revision(conn, SHADOW_REVISION)
        bundle = _bundle_from_store(root, args.recovery_manifest_sha256, conn)
        transaction = conn.begin()
        result = bootstrap_artifacts(conn, bundle=bundle, roots=roots)
        transaction.commit()
    finally:
        conn.close()
        engine.dispose()  # type: ignore[attr-defined]
    print(
        json.dumps(
            {
                "already_present": result.already_present,
                "artifacts_registered": result.artifacts_registered,
                "artifacts_verified": result.artifacts_verified,
                "backends": result.backends,
                "locations_registered": result.locations_registered,
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


def command_register_r2(args: argparse.Namespace) -> int:
    root = RecoveryRoot.from_environment()
    engine, conn = _connect()
    try:
        _require_revision(conn, SHADOW_REVISION)
        bundle = _bundle_from_store(root, args.recovery_manifest_sha256, conn)
        transaction = conn.begin()
        result = register_r2(conn, bundle=bundle, root=root)
        transaction.commit()
    finally:
        conn.close()
        engine.dispose()  # type: ignore[attr-defined]
    print(
        json.dumps(
            {
                "already_registered": result.already_registered,
                "backup_set_id": result.backup_set_id,
                "dump_artifact_id": result.dump_artifact_id,
                "dump_location_id": result.dump_location_id,
                "recovery_manifest_artifact_id": result.recovery_manifest_artifact_id,
                "snapshot_manifest_artifact_id": result.snapshot_manifest_artifact_id,
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


def command_verify(args: argparse.Namespace) -> int:
    root = RecoveryRoot.from_environment()
    roots = ArtifactRoots.from_environment()
    engine, conn = _connect()
    try:
        _require_revision(conn, args.expect_revision)
        result = verify_d3a(
            conn,
            conn,
            root=root,
            roots=roots,
            recovery_manifest_sha256=args.recovery_manifest_sha256,
            expect_r2=not args.no_r2,
        )
        conn.rollback()
    finally:
        conn.close()
        engine.dispose()  # type: ignore[attr-defined]
    print(json.dumps(result.as_dict(), indent=2, sort_keys=True))
    for check in result.failures:
        print(f"FAIL: {check.name}: {check.detail}", file=sys.stderr)
    return 0 if result.passed else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="DB-V2 D3-A recovery preparation")
    sub = parser.add_subparsers(dest="command", required=True)

    build = sub.add_parser("build-r1", help=f"R1, from a database at {R1_SOURCE_REVISION}")
    build.add_argument("--quiesce-started-at")
    build.add_argument("--quiesce-ended-at")
    build.set_defaults(handler=command_build_r1)

    bootstrap = sub.add_parser(
        "bootstrap-artifacts", help=f"B0, on a database at {SHADOW_REVISION}"
    )
    bootstrap.add_argument("--recovery-manifest-sha256", required=True)
    bootstrap.set_defaults(handler=command_bootstrap_artifacts)

    register = sub.add_parser("register-r2", help=f"R2, on a database at {SHADOW_REVISION}")
    register.add_argument("--recovery-manifest-sha256", required=True)
    register.set_defaults(handler=command_register_r2)

    verify = sub.add_parser("verify", help="the non-mutating D3-A verifier")
    verify.add_argument("--recovery-manifest-sha256", required=True)
    verify.add_argument("--expect-revision", default=SHADOW_REVISION)
    verify.add_argument("--no-r2", action="store_true", help="verify B0 only, before R2 has run")
    verify.set_defaults(handler=command_verify)

    args = parser.parse_args(argv)
    handler = args.handler
    return int(handler(args))


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    raise SystemExit(main())
