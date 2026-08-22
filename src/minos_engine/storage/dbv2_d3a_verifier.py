"""DB-V2 D3-A: the non-mutating verifier.

Every check reads. The transaction is always rolled back, so running the verifier three times in
a row leaves the database byte-identical - which is itself one of the checks a caller can make.
It accepts no caller-supplied digest, count or verification result: everything it compares, it
re-derives from the published R1 bytes, the V1 catalog and the shadow graph.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any, Final

from sqlalchemy import Connection, text

from .dbv2_artifact_bootstrap import NON_ARTIFACT_SHADOW_TABLES
from .dbv2_recovery import (
    ARTIFACT_SNAPSHOT_DOMAIN,
    RECOVERY_MANIFEST_SCHEMA_VERSION,
    RECOVERY_REQUIRED_FIELDS,
    SNAPSHOT_PREDICATE,
    SNAPSHOT_REQUIRED_FIELDS,
    SNAPSHOT_SCHEMA_VERSION,
    ArtifactRoots,
    canonical_json_bytes,
    hash_payload,
    parse_strict,
)
from .dbv2_recovery_store import FILE_MODE, RecoveryRoot

__all__ = ["CheckResult", "D3AVerification", "verify_d3a"]

FILE_KINDS: Final = ("recovery", "snapshot", "backup")


@dataclass(frozen=True, slots=True)
class CheckResult:
    name: str
    passed: bool
    detail: str

    def __str__(self) -> str:  # pragma: no cover - human output only
        return f"{'PASS' if self.passed else 'FAIL'}  {self.name}: {self.detail}"


@dataclass(slots=True)
class D3AVerification:
    checks: list[CheckResult] = field(default_factory=list)

    def record(self, name: str, passed: bool, detail: str = "") -> None:
        self.checks.append(CheckResult(name, passed, detail))

    @property
    def passed(self) -> bool:
        return all(check.passed for check in self.checks)

    @property
    def failures(self) -> list[CheckResult]:
        return [check for check in self.checks if not check.passed]

    def as_dict(self) -> dict[str, Any]:
        return {
            "checks": [
                {"detail": c.detail, "name": c.name, "passed": c.passed} for c in self.checks
            ],
            "failed": len(self.failures),
            "passed": self.passed,
            "total": len(self.checks),
        }


def verify_d3a(
    v1_conn: Connection,
    shadow_conn: Connection,
    *,
    root: RecoveryRoot,
    roots: ArtifactRoots,
    recovery_manifest_sha256: str,
    expect_r2: bool = True,
) -> D3AVerification:
    """Verify the whole D3-A result. Reads only; both connections must be rolled back by the caller."""
    result = D3AVerification()

    # ---- R1 files ----------------------------------------------------------------------
    try:
        manifest_bytes = root.read("recovery", recovery_manifest_sha256)
        manifest = parse_strict(manifest_bytes, required=RECOVERY_REQUIRED_FIELDS)
        result.record("r1.recovery_manifest_bytes", True, recovery_manifest_sha256)
    except Exception as error:  # noqa: BLE001 - the failure is the result
        result.record("r1.recovery_manifest_bytes", False, str(error))
        return result
    result.record(
        "r1.recovery_manifest_canonical",
        canonical_json_bytes(manifest) == manifest_bytes,
        "canonical JSON bytes",
    )
    result.record(
        "r1.recovery_manifest_schema_version",
        manifest["schema_version"] == RECOVERY_MANIFEST_SCHEMA_VERSION,
        str(manifest["schema_version"]),
    )

    snapshot_raw = str(manifest["artifact_snapshot_manifest_sha256"])
    try:
        snapshot_bytes = root.read("snapshot", snapshot_raw)
        snapshot = parse_strict(snapshot_bytes, required=SNAPSHOT_REQUIRED_FIELDS)
        result.record("r1.snapshot_manifest_bytes", True, snapshot_raw)
    except Exception as error:  # noqa: BLE001
        result.record("r1.snapshot_manifest_bytes", False, str(error))
        return result
    result.record(
        "r1.snapshot_manifest_canonical",
        canonical_json_bytes(snapshot) == snapshot_bytes,
        "canonical JSON bytes",
    )
    result.record(
        "r1.snapshot_schema_version",
        snapshot["schema_version"] == SNAPSHOT_SCHEMA_VERSION,
        str(snapshot["schema_version"]),
    )
    result.record(
        "r1.snapshot_predicate",
        snapshot["predicate"] == SNAPSHOT_PREDICATE,
        str(snapshot["predicate"]),
    )
    scientific = hashlib.sha256(ARTIFACT_SNAPSHOT_DOMAIN + snapshot_bytes).hexdigest()
    result.record(
        "r1.snapshot_scientific_identity",
        scientific == manifest["artifact_snapshot_sha256"],
        scientific,
    )

    dump_digest = str(manifest["database_backup_sha256"])
    try:
        dump_bytes = root.read("backup", dump_digest)
        result.record("r1.dump_digest", True, dump_digest)
        result.record(
            "r1.dump_size",
            len(dump_bytes) == int(manifest["database_backup_size_bytes"]),
            f"{len(dump_bytes)} bytes",
        )
    except Exception as error:  # noqa: BLE001
        result.record("r1.dump_digest", False, str(error))
        result.record("r1.dump_size", False, "unavailable")

    modes = {
        kind: root.stat_mode(kind, digest)
        for kind, digest in (
            ("recovery", recovery_manifest_sha256),
            ("snapshot", snapshot_raw),
            ("backup", dump_digest),
        )
        if root.exists(kind, digest)
    }
    result.record(
        "r1.file_permissions",
        bool(modes) and all(mode == FILE_MODE for mode in modes.values()),
        ", ".join(f"{k}={v:04o}" for k, v in sorted(modes.items())),
    )
    result.record(
        "r1.no_secret_material",
        not any(
            marker in manifest_bytes.decode("utf-8")
            for marker in ("postgresql://", "password", "PGPASSWORD", "/home/", "/tmp/")
        ),
        "no DSN, credential or host path in the recovery manifest",
    )

    # ---- source identity ----------------------------------------------------------------
    v1_revision = str(v1_conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one())
    v1_database = str(v1_conn.execute(text("SELECT current_database()")).scalar_one())
    result.record(
        "v1.source_revision", v1_revision == manifest["source_alembic_revision"], v1_revision
    )
    result.record("v1.database_name", v1_database == manifest["database_name"], v1_database)

    # ---- exact V1 <-> R1 artifact set ----------------------------------------------------
    v1_rows = v1_conn.execute(
        text(
            "SELECT sha256, size_bytes, provenance, uri FROM catalog.artifacts ORDER BY sha256, uri"
        )
    ).all()
    v1_set = {(str(r[0]).strip(), int(r[1]), str(r[2])) for r in v1_rows}
    snapshot_set = {
        (str(e["content_sha256"]), int(e["size_bytes"]), str(e["artifact_kind"]))
        for e in snapshot["entries"]
    }
    result.record(
        "r1.exact_v1_artifact_set",
        v1_set == snapshot_set,
        f"{len(v1_set)} V1 rows, {len(snapshot_set)} snapshot entries",
    )
    result.record(
        "r1.snapshot_counts",
        int(snapshot["artifact_count"]) == len(snapshot_set)
        and int(snapshot["artifact_total_bytes"])
        == sum(int(e["size_bytes"]) for e in snapshot["entries"]),
        f"count={snapshot['artifact_count']} bytes={snapshot['artifact_total_bytes']}",
    )

    # ---- exact V1 <-> B0 graph ------------------------------------------------------------
    shadow_rows = shadow_conn.execute(
        text(
            "SELECT a.content_sha256, a.size_bytes, a.artifact_kind, a.verification_state, "
            "       a.storage_mode, b.backend_key, l.object_key, l.location_state, l.is_primary "
            "FROM dbv2_catalog.artifacts AS a "
            "LEFT JOIN dbv2_catalog.artifact_locations AS l ON l.artifact_id = a.id "
            "LEFT JOIN dbv2_catalog.storage_backends AS b ON b.id = l.backend_id "
            "WHERE a.lifecycle_state = 'active' AND a.backup_scope = 'operational' "
            "ORDER BY a.content_sha256"
        )
    ).all()
    shadow_set = {(str(r[0]), int(r[1]), str(r[2])) for r in shadow_rows}
    result.record(
        "b0.exact_v1_artifact_set", v1_set == shadow_set, f"{len(shadow_set)} shadow artifacts"
    )
    result.record(
        "b0.all_verified",
        bool(shadow_rows) and all(str(r[3]) == "verified" for r in shadow_rows),
        f"{sum(1 for r in shadow_rows if str(r[3]) == 'verified')}/{len(shadow_rows)} verified",
    )
    result.record(
        "b0.no_unhealthy_artifacts",
        not any(str(r[3]) in {"missing", "corrupt", "unverified"} for r in shadow_rows),
        "no missing, corrupt or unverified operational artifact",
    )
    primary_counts = shadow_conn.execute(
        text(
            "SELECT a.content_sha256, count(*) FILTER (WHERE l.is_primary "
            "  AND l.location_state = 'present') "
            "FROM dbv2_catalog.artifacts AS a "
            "LEFT JOIN dbv2_catalog.artifact_locations AS l ON l.artifact_id = a.id "
            "WHERE a.lifecycle_state = 'active' AND a.backup_scope = 'operational' "
            "GROUP BY 1"
        )
    ).all()
    result.record(
        "b0.exactly_one_present_primary",
        bool(primary_counts) and all(int(row[1]) == 1 for row in primary_counts),
        f"{len(primary_counts)} artifacts checked",
    )

    # ---- exact URI reconstruction ---------------------------------------------------------
    by_root = dict(roots.roots)
    reconstructed: set[str] = set()
    for row in shadow_rows:
        backend_key, object_key = row[5], row[6]
        if backend_key is None or object_key is None:
            continue
        reconstructed.add(str(by_root[str(backend_key)] / str(object_key)))
    v1_paths = {roots.resolve(str(r[3]))[2].as_posix() for r in v1_rows}
    result.record(
        "b0.exact_uri_reconstruction",
        reconstructed == v1_paths,
        f"{len(reconstructed)} reconstructed, {len(v1_paths)} V1 locators",
    )
    payload_ok = True
    for row in shadow_rows:
        if row[5] is None:
            payload_ok = False
            break
        try:
            observation = hash_payload(by_root[str(row[5])] / str(row[6]))
        except Exception:  # noqa: BLE001
            payload_ok = False
            break
        if observation.sha256 != str(row[0]) or observation.size_bytes != int(row[1]):
            payload_ok = False
            break
    result.record("b0.payloads_rehash", payload_ok, "every payload re-read and re-hashed")

    # ---- R2 ---------------------------------------------------------------------------------
    backup = shadow_conn.execute(
        text(
            "SELECT id, completeness, recovery_manifest_sha256, artifact_snapshot_manifest_sha256, "
            "       artifact_snapshot_sha256, database_backup_sha256, artifact_count, "
            "       artifact_total_bytes, recovery_set_id "
            "FROM dbv2_catalog.backup_sets"
        )
    ).all()
    if expect_r2:
        result.record("r2.single_row", len(backup) == 1, f"{len(backup)} backup_sets rows")
        if len(backup) == 1:
            row = backup[0]
            result.record("r2.completeness", str(row[1]) == "complete", str(row[1]))
            result.record(
                "r2.binds_r1",
                str(row[2]) == recovery_manifest_sha256
                and str(row[3]) == snapshot_raw
                and str(row[4]) == str(manifest["artifact_snapshot_sha256"])
                and str(row[5]) == dump_digest
                and int(row[6]) == int(manifest["artifact_count"])
                and int(row[7]) == int(manifest["artifact_total_bytes"]),
                "every recovery identity equals R1",
            )
            artifacts = shadow_conn.execute(
                text(
                    "SELECT content_sha256, storage_mode, verification_state, schema_version "
                    "FROM dbv2_catalog.artifacts WHERE backup_scope = 'recovery' "
                    "ORDER BY content_sha256"
                )
            ).all()
            result.record(
                "r2.three_recovery_artifacts",
                len(artifacts) == 3 and all(str(a[2]) == "verified" for a in artifacts),
                f"{len(artifacts)} recovery artifacts",
            )
            admin = shadow_conn.execute(
                text(
                    "SELECT operation_kind, outcome, backup_set_id, evidence_hash "
                    "FROM dbv2_audit.admin_operations"
                )
            ).all()
            result.record(
                "r2.administrative_audit_row",
                len(admin) == 1
                and str(admin[0][0]) == "migration"
                and str(admin[0][1]) == "succeeded"
                and str(admin[0][2]) == str(row[0]),
                f"{len(admin)} admin_operations rows bound to the backup set",
            )
            dump_locations = shadow_conn.execute(
                text(
                    "SELECT count(*) FROM dbv2_catalog.artifacts AS a "
                    "JOIN dbv2_catalog.artifact_locations AS l ON l.artifact_id = a.id "
                    "WHERE a.content_sha256 = :d AND l.location_state = 'present' AND l.is_primary"
                ),
                {"d": dump_digest},
            ).scalar_one()
            result.record(
                "r2.dump_location_present", int(dump_locations) == 1, f"{dump_locations} locations"
            )
    else:
        result.record("r2.absent", not backup, f"{len(backup)} backup_sets rows")

    # ---- B1 has not started ------------------------------------------------------------------
    populated = [
        table
        for table in NON_ARTIFACT_SHADOW_TABLES
        if table != "dbv2_catalog.backup_sets"
        and int(shadow_conn.execute(text(f"SELECT count(*) FROM {table}")).scalar_one())
    ]
    result.record("b1.absent", not populated, f"populated business tables: {populated}")
    return result
