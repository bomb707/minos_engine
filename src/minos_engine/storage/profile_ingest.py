"""L2-D profile ingestion + per-epoch snapshot persistence (schema-owner writes).

Storage-side counterpart of the pure ``layer2.ingest`` validation: this module owns every
database write for L2-D. The intake producer has no write path; no application role holds
INSERT on any L2-D table; writes run as the schema owner (``minos_admin``).

Trusted-boundary rules (owner corrections):
  * **Exact-byte hashing inside the boundary**: the profile JSON, manifest JSON, and
    windows parquet are read and SHA-256-hashed HERE, and the validated documents are
    decoded from those exact bytes. A caller can never substitute a hash for content.
  * **Three-artifact contract**: all three artifacts are registered in
    ``catalog.artifacts`` with uri + sha256 + size_bytes + media_type + kind
    (``provenance``); reuse of an existing sha row requires exact metadata equality
    (:class:`ArtifactMetadataConflictError` otherwise).
  * **Epoch membership**: the ingested dataset must be a member of the requested split
    epoch's allocation set (joined through ``split_snapshots``/``split_epoch_allocations``
    — never an arbitrary registry row), and its dataset/round/chromosome/identity-tuple
    must match the attestation.
  * **Idempotency/conflicts** via the canonical ``ingestion_key``
    (= canonical_hash({identity_tuple_hash, profile_id})): same key + same content →
    idempotent success returning the existing row; same key + different content →
    :class:`ContentConflictError`; same profile_id under a different identity/content →
    :class:`ProfileIdConflictError`; same identity with a genuinely new profile version →
    append-only new row.
  * **Atomic audit**: the ADMITTED attempt row commits in the SAME transaction as the
    accepted row — an accepted scientific row can never exist without its audit record.
    REJECTED attempts are recorded in their own transaction after rollback.
  * **Windows parquet**: stream-hashed, exact ``WINDOW_ARROW_SCHEMA`` (no extra/missing
    columns), row count == manifest, every row's profile_id == the profile, contig ==
    the registered chromosome, and window coordinates within the registered region.

``freeze_profile_snapshot`` requires an EXPLICIT owner version selection
(``{dataset_id: content_hash}``) covering exactly the epoch's members; each selection is
verified to resolve to an accepted row for that identity. Member count derives from the
epoch's ``sample_count`` — never a hardcoded corpus size.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.exc import DBAPIError

from minos_engine.common.errors import (
    AdmissionRejectedError,
    ArtifactMetadataConflictError,
    ContentConflictError,
    ContractValidationError,
    EpochMembershipError,
    FeatureHashConflictError,
    ProfileIdConflictError,
)
from minos_engine.common.hashing import canonical_hash
from minos_engine.layer2.ingest.contracts import (
    InputIntegrityAttestation,
    canonical_feature_values_hash,
    extract_eligible_feature_values,
)
from minos_engine.layer2.ingest.validation import validate_admission

__all__ = [
    "ARTIFACT_KIND_PROFILE",
    "ARTIFACT_KIND_MANIFEST",
    "ARTIFACT_KIND_WINDOWS",
    "IngestOutcome",
    "ingestion_key_for",
    "ingest_profile",
    "freeze_profile_snapshot",
    "verify_windows_parquet",
]

ARTIFACT_KIND_PROFILE = "l2d:profile-json"
ARTIFACT_KIND_MANIFEST = "l2d:profile-manifest-json"
ARTIFACT_KIND_WINDOWS = "l2d:window-parquet"
_MEDIA_JSON = "application/json"
_MEDIA_PARQUET = "application/vnd.apache.parquet"
_CHUNK = 4 * 1024 * 1024


class IngestOutcome:
    """Result of an ingestion: the accepted row id + whether it was idempotent."""

    __slots__ = ("row_id", "idempotent")

    def __init__(self, row_id: str, *, idempotent: bool) -> None:
        self.row_id = row_id
        self.idempotent = idempotent


def ingestion_key_for(identity_tuple_hash: str, profile_id: str) -> str:
    """Canonical ingestion identity: one logical ingestion per (identity, profile)."""
    return canonical_hash({"identity_tuple_hash": identity_tuple_hash, "profile_id": profile_id})


def _stream_sha256(path: Path) -> tuple[str, int]:
    h = hashlib.sha256()
    size = 0
    with path.open("rb") as fh:
        while True:
            chunk = fh.read(_CHUNK)
            if not chunk:
                break
            size += len(chunk)
            h.update(chunk)
    return h.hexdigest(), size


def verify_windows_parquet(
    parquet_path: Path,
    *,
    expected_row_count: int,
    profile_id: str,
    chromosome: str,
    region_start0: int,
    region_end0_exclusive: int,
) -> tuple[str, int]:
    """Verify the windows artifact: exact schema, row identity, coordinates, invariants.

    Returns ``(sha256, size_bytes)`` of the exact bytes. Storage may import the Layer 1
    serializer directly (the boundary restriction applies to the ``layer2`` package only).
    """
    import pyarrow.parquet as pq

    from minos_engine.layer1.serializer import WINDOW_ARROW_SCHEMA

    sha, size = _stream_sha256(parquet_path)
    try:
        table = pq.read_table(parquet_path)
    except Exception as exc:  # noqa: BLE001 - corrupt/unreadable parquet
        raise AdmissionRejectedError(f"windows parquet unreadable/corrupt: {exc}") from exc
    if not table.schema.equals(WINDOW_ARROW_SCHEMA):
        raise AdmissionRejectedError(
            "windows parquet schema != frozen Layer 1 window schema (missing/extra/"
            "retyped columns are rejected)"
        )
    if table.num_rows != expected_row_count:
        raise AdmissionRejectedError(
            f"windows parquet rows {table.num_rows} != manifest windows_row_count "
            f"{expected_row_count}"
        )
    cols = {
        name: table.column(name).to_pylist()
        for name in ("profile_id", "contig", "window_id", "start0", "end0", "length_bp")
    }
    # Row-sequence contract (frozen Layer 1 serializer): window_id strictly increasing
    # (unique, ordered), coordinates ascending and non-overlapping. Windows may be a
    # SAMPLE of the region (Layer 1 sampling policy), so gap-free coverage is NOT
    # required — only ordering, uniqueness, and disjointness.
    prev_id = None
    prev_end = None
    for i in range(table.num_rows):
        if cols["profile_id"][i] != profile_id:
            raise AdmissionRejectedError(f"windows row {i}: profile_id mismatch")
        if cols["contig"][i] != chromosome:
            raise AdmissionRejectedError(f"windows row {i}: contig != {chromosome}")
        wid = cols["window_id"][i]
        if prev_id is not None and wid <= prev_id:
            raise AdmissionRejectedError(
                f"windows row {i}: window_id not strictly increasing "
                "(duplicate/shuffled/reversed rows are rejected)"
            )
        prev_id = wid
        s, e, ln = cols["start0"][i], cols["end0"][i], cols["length_bp"][i]
        if not (region_start0 <= s < e <= region_end0_exclusive):
            raise AdmissionRejectedError(f"windows row {i}: window outside region bounds")
        if ln != e - s:
            raise AdmissionRejectedError(f"windows row {i}: length_bp != end0 - start0")
        if prev_end is not None and s < prev_end:
            raise AdmissionRejectedError(f"windows row {i}: windows overlap or are unsorted")
        prev_end = e
    return sha, size


def _epoch_member_identity(conn: Connection, dataset_id: str, epoch: int) -> dict[str, Any]:
    """Resolve the identity THROUGH the epoch's allocation membership (never an arbitrary
    registry row). Fails closed if the dataset is not allocated in this epoch."""
    row = (
        conn.execute(
            text(
                "SELECT dr.id AS registry_id, dr.dataset_id, dr.round_id, dr.chromosome, "
                " dr.region_start0, dr.region_end0_exclusive, "
                " dr.bam_sha256, dr.bai_sha256, dr.reference_sha256, dr.fai_sha256, "
                " dr.region_hash, dr.identity_tuple_hash, "
                " ss.registry_snapshot_hash "
                "FROM catalog.split_epoch_allocations ea "
                "JOIN catalog.split_snapshots ss "
                "  ON ss.id = ea.snapshot_id AND ss.epoch = :e "
                "JOIN catalog.dataset_registry dr ON dr.id = ea.dataset_registry_id "
                "WHERE dr.dataset_id = :d"
            ),
            {"d": dataset_id, "e": epoch},
        )
        .mappings()
        .first()
    )
    if row is None:
        raise EpochMembershipError(f"dataset {dataset_id!r} is not a member of split epoch {epoch}")
    return dict(row)


def _register_artifact(
    conn: Connection, *, uri: str, sha256: str, size_bytes: int, media_type: str, kind: str
) -> str:
    """Register an artifact, or reuse an existing sha row only on EXACT metadata match."""
    existing = (
        conn.execute(
            text(
                "SELECT id, size_bytes, media_type, provenance "
                "FROM catalog.artifacts WHERE sha256 = :h"
            ),
            {"h": sha256},
        )
        .mappings()
        .first()
    )
    if existing is None:
        # race-free: a concurrent insert of the same sha resolves via ON CONFLICT
        # DO NOTHING + re-read — never an untyped uniqueness failure.
        conn.execute(
            text(
                "INSERT INTO catalog.artifacts (uri, sha256, size_bytes, media_type, "
                " provenance) VALUES (:u, :h, :s, :m, :k) "
                "ON CONFLICT (sha256) DO NOTHING"
            ),
            {"u": uri, "h": sha256, "s": size_bytes, "m": media_type, "k": kind},
        )
        existing = (
            conn.execute(
                text(
                    "SELECT id, size_bytes, media_type, provenance "
                    "FROM catalog.artifacts WHERE sha256 = :h"
                ),
                {"h": sha256},
            )
            .mappings()
            .one()
        )
    if (
        existing["size_bytes"] != size_bytes
        or existing["media_type"] != media_type
        or existing["provenance"] != kind
    ):
        raise ArtifactMetadataConflictError(
            f"artifact {sha256[:12]}… exists with conflicting metadata "
            f"(size={existing['size_bytes']}, media={existing['media_type']}, "
            f"kind={existing['provenance']})"
        )
    return str(existing["id"])


def _record_attempt(
    conn: Connection,
    *,
    registry_id: str | None,
    profile_id: str,
    outcome: str,
    reasons: tuple[str, ...],
    attestation_hash: str | None,
    content_hash: str | None,
) -> None:
    conn.execute(
        text(
            "INSERT INTO profiling.profile_ingest_attempts "
            "(dataset_registry_id, profile_id, outcome, reasons, attestation_hash, "
            " content_hash) VALUES (:r, :p, :o, CAST(:re AS jsonb), :a, :c)"
        ),
        {
            "r": registry_id,
            "p": profile_id,
            "o": outcome,
            "re": json.dumps(list(reasons)),
            "a": attestation_hash,
            "c": content_hash,
        },
    )


def _existing_by_key(conn: Connection, ingestion_key: str) -> dict[str, Any] | None:
    row = (
        conn.execute(
            text("SELECT id, content_hash FROM profiling.bam_profiles WHERE ingestion_key = :k"),
            {"k": ingestion_key},
        )
        .mappings()
        .first()
    )
    return dict(row) if row else None


def ingest_profile(
    engine: Engine,
    *,
    epoch: int,
    profile_json_path: Path,
    manifest_json_path: Path,
    windows_parquet_path: Path,
    attestation: InputIntegrityAttestation | dict[str, Any],
    profile_artifact_uri: str,
    manifest_artifact_uri: str,
    windows_artifact_uri: str,
) -> IngestOutcome:
    """Admit one COMPLETE profile; exact bytes are hashed and decoded HERE.

    Returns :class:`IngestOutcome`. Rejection raises the typed error AND records a
    REJECTED attempt in its own transaction; admission commits the accepted row and its
    ADMITTED audit record atomically.
    """
    att = (
        attestation
        if isinstance(attestation, InputIntegrityAttestation)
        else InputIntegrityAttestation.model_validate(attestation)
    )
    # ---- trusted boundary: hash exact bytes, decode documents FROM those bytes ------
    profile_bytes = profile_json_path.read_bytes()
    manifest_bytes = manifest_json_path.read_bytes()
    profile_sha256 = hashlib.sha256(profile_bytes).hexdigest()
    manifest_sha256 = hashlib.sha256(manifest_bytes).hexdigest()
    try:
        profile_document = json.loads(profile_bytes.decode("utf-8"))
        manifest_document = json.loads(manifest_bytes.decode("utf-8"))
    except Exception as exc:  # noqa: BLE001
        raise AdmissionRejectedError(f"artifact bytes are not valid JSON: {exc}") from exc

    profile_id = str(profile_document.get("profile_id", "unknown"))
    registry_id: str | None = None
    content_hash: str | None = None
    try:
        with engine.begin() as conn:
            identity = _epoch_member_identity(conn, att.dataset_id, epoch)
            registry_id = str(identity.pop("registry_id"))
            region_start0 = int(identity.pop("region_start0"))
            region_end0 = int(identity.pop("region_end0_exclusive"))

            windows_sha256, windows_size = verify_windows_parquet(
                windows_parquet_path,
                expected_row_count=int(manifest_document.get("windows_row_count", -1)),
                profile_id=profile_id,
                chromosome=str(identity["chromosome"]),
                region_start0=region_start0,
                region_end0_exclusive=region_end0,
            )
            decision = validate_admission(
                profile_document=profile_document,
                manifest_document=manifest_document,
                attestation=att,
                registry_identity=identity,
                profile_artifact_sha256=profile_sha256,
                windows_artifact_sha256=windows_sha256,
            )
            if not decision.admissible:
                raise AdmissionRejectedError(f"profile {profile_id} rejected: {decision.reasons}")
            recomputed = canonical_feature_values_hash(
                extract_eligible_feature_values(profile_document)
            )
            if recomputed != decision.feature_values_hash:
                raise FeatureHashConflictError(
                    "canonical feature_values_hash diverged between validation and write"
                )

            content_hash = canonical_hash(
                {
                    "identity_tuple_hash": identity["identity_tuple_hash"],
                    "feature_values_hash": decision.feature_values_hash,
                    "l1_feature_values_hash": decision.l1_feature_values_hash,
                    "profile_sha256": profile_sha256,
                    "profile_manifest_sha256": manifest_sha256,
                    "windows_sha256": windows_sha256,
                    "profiler_version": str(manifest_document["profiler_version"]),
                    "profiler_config_hash": str(manifest_document["profiler_config_hash"]),
                    "attestation_hash": att.attestation_hash,
                }
            )
            ingestion_key = ingestion_key_for(str(identity["identity_tuple_hash"]), profile_id)

            # ---- idempotency / conflict resolution --------------------------------
            existing = _existing_by_key(conn, ingestion_key)
            if existing is not None:
                if existing["content_hash"] == content_hash:
                    _record_attempt(
                        conn,
                        registry_id=registry_id,
                        profile_id=profile_id,
                        outcome="ADMITTED",
                        reasons=("idempotent-duplicate",),
                        attestation_hash=att.attestation_hash,
                        content_hash=content_hash,
                    )
                    return IngestOutcome(str(existing["id"]), idempotent=True)
                raise ContentConflictError(
                    f"ingestion_key {ingestion_key[:12]}… resubmitted with different content"
                )
            pid_row = (
                conn.execute(
                    text(
                        "SELECT identity_tuple_hash, content_hash FROM profiling.bam_profiles "
                        "WHERE profile_id = :p"
                    ),
                    {"p": profile_id},
                )
                .mappings()
                .first()
            )
            if pid_row is not None:
                raise ProfileIdConflictError(
                    f"profile_id {profile_id} already accepted with different identity/content"
                )

            profile_artifact_id = _register_artifact(
                conn,
                uri=profile_artifact_uri,
                sha256=profile_sha256,
                size_bytes=len(profile_bytes),
                media_type=_MEDIA_JSON,
                kind=ARTIFACT_KIND_PROFILE,
            )
            manifest_artifact_id = _register_artifact(
                conn,
                uri=manifest_artifact_uri,
                sha256=manifest_sha256,
                size_bytes=len(manifest_bytes),
                media_type=_MEDIA_JSON,
                kind=ARTIFACT_KIND_MANIFEST,
            )
            windows_artifact_id = _register_artifact(
                conn,
                uri=windows_artifact_uri,
                sha256=windows_sha256,
                size_bytes=windows_size,
                media_type=_MEDIA_PARQUET,
                kind=ARTIFACT_KIND_WINDOWS,
            )

            row_id = conn.execute(
                text(
                    "INSERT INTO profiling.bam_profiles "
                    "(dataset_registry_id, profile_id, bam_sha256, bai_sha256, "
                    " reference_sha256, fai_sha256, region_hash, identity_tuple_hash, "
                    " m5_status, integrity_degraded, attestation_hash, "
                    " registry_snapshot_hash, profile_status, profiler_version, "
                    " profiler_config_hash, windows_row_count, feature_values_hash, "
                    " l1_feature_values_hash, eligible_value_count, profile_document, "
                    " profile_sha256, profile_manifest_sha256, windows_sha256, "
                    " profile_artifact_id, profile_manifest_artifact_id, "
                    " windows_artifact_id, ingestion_key, content_hash) "
                    "VALUES (:reg, :pid, :bam, :bai, :ref, :fai, :rh, :ith, :m5, :deg, "
                    " :ath, :rsh, 'COMPLETE', :pv, :pch, :wrc, :fvh, :l1h, :evc, "
                    " CAST(:doc AS jsonb), :psha, :msha, :wsha, :pa, :ma, :wa, :ik, :ch) "
                    "RETURNING id"
                ),
                {
                    "reg": registry_id,
                    "pid": profile_id,
                    "bam": identity["bam_sha256"],
                    "bai": identity["bai_sha256"],
                    "ref": identity["reference_sha256"],
                    "fai": identity["fai_sha256"],
                    "rh": identity["region_hash"],
                    "ith": identity["identity_tuple_hash"],
                    "m5": att.m5_status.value,
                    "deg": decision.integrity_degraded,
                    "ath": att.attestation_hash,
                    "rsh": identity["registry_snapshot_hash"],
                    "pv": str(manifest_document["profiler_version"]),
                    "pch": str(manifest_document["profiler_config_hash"]),
                    "wrc": int(manifest_document["windows_row_count"]),
                    "fvh": decision.feature_values_hash,
                    "l1h": decision.l1_feature_values_hash,
                    "evc": decision.eligible_value_count,
                    "doc": json.dumps(profile_document),
                    "psha": profile_sha256,
                    "msha": manifest_sha256,
                    "wsha": windows_sha256,
                    "pa": profile_artifact_id,
                    "ma": manifest_artifact_id,
                    "wa": windows_artifact_id,
                    "ik": ingestion_key,
                    "ch": content_hash,
                },
            ).scalar_one()
            # atomic audit: the ADMITTED record commits WITH the accepted row.
            _record_attempt(
                conn,
                registry_id=registry_id,
                profile_id=profile_id,
                outcome="ADMITTED",
                reasons=(),
                attestation_hash=att.attestation_hash,
                content_hash=content_hash,
            )
            return IngestOutcome(str(row_id), idempotent=False)
    except DBAPIError as exc:
        # classify the constraint that fired (application pre-checks are raceable; the
        # DB constraints are the concurrency-safe authority).
        message = str(getattr(exc, "orig", exc))
        if "uq_bam_profiles_profile_id" in message:
            pid_conflict = ProfileIdConflictError(
                f"profile_id {profile_id} already accepted (concurrent)"
            )
            _record_rejapi(engine, registry_id, profile_id, pid_conflict, att)
            raise pid_conflict from exc
        # concurrent duplicate: the UNIQUE(ingestion_key) backstop fired — re-check and
        # resolve as idempotent success or content conflict.
        with engine.connect() as conn:
            ik = ingestion_key_for(str(att.identity_tuple_hash), profile_id)
            existing = _existing_by_key(conn, ik)
        if existing is not None and content_hash is not None:
            if existing["content_hash"] == content_hash:
                with engine.begin() as conn:
                    _record_attempt(
                        conn,
                        registry_id=registry_id,
                        profile_id=profile_id,
                        outcome="ADMITTED",
                        reasons=("idempotent-duplicate-concurrent",),
                        attestation_hash=att.attestation_hash,
                        content_hash=content_hash,
                    )
                return IngestOutcome(str(existing["id"]), idempotent=True)
            conflict = ContentConflictError("concurrent resubmission with different content")
            _record_rejapi(engine, registry_id, profile_id, conflict, att)
            raise conflict from exc
        _record_rejapi(engine, registry_id, profile_id, exc, att)
        raise
    except Exception as exc:
        _record_rejapi(engine, registry_id, profile_id, exc, att)
        raise


def _record_rejapi(
    engine: Engine,
    registry_id: str | None,
    profile_id: str,
    exc: Exception,
    att: InputIntegrityAttestation,
) -> None:
    """Record a REJECTED attempt in its own transaction (survives the rollback)."""
    try:
        with engine.begin() as conn:
            _record_attempt(
                conn,
                registry_id=registry_id,
                profile_id=profile_id,
                outcome="REJECTED",
                reasons=(str(exc)[:500],),
                attestation_hash=att.attestation_hash,
                content_hash=None,
            )
    except Exception:  # noqa: BLE001, S110 - rejection logging must not mask the cause
        pass


def freeze_profile_snapshot(conn: Connection, epoch: int, selections: dict[str, str]) -> str:
    """Freeze the epoch's profile snapshot from an EXPLICIT owner version selection.

    ``selections`` maps every member ``dataset_id`` to the exact accepted
    ``content_hash`` chosen for this snapshot. Freezing verifies: the selection covers
    exactly the epoch's allocated identities (no extras, no omissions), and each selected
    content hash resolves to an accepted ``bam_profiles`` row for that identity. Member
    count must equal the split epoch's ``sample_count``.
    """
    snap = (
        conn.execute(
            text(
                "SELECT id, manifest_hash, registry_snapshot_hash, sample_count "
                "FROM catalog.split_snapshots WHERE epoch = :e"
            ),
            {"e": epoch},
        )
        .mappings()
        .first()
    )
    if snap is None:
        raise ContractValidationError(f"split epoch {epoch} is not persisted")

    alloc = conn.execute(
        text(
            "SELECT ea.dataset_registry_id, ea.partition, dr.dataset_id "
            "FROM catalog.split_epoch_allocations ea "
            "JOIN catalog.split_snapshots ss ON ss.id = ea.snapshot_id "
            "JOIN catalog.dataset_registry dr ON dr.id = ea.dataset_registry_id "
            "WHERE ss.epoch = :e ORDER BY dr.dataset_id"
        ),
        {"e": epoch},
    ).mappings()
    members_in = {r["dataset_id"]: dict(r) for r in alloc}
    if set(selections) != set(members_in):
        missing = sorted(set(members_in) - set(selections))
        extra = sorted(set(selections) - set(members_in))
        raise ContractValidationError(
            f"version selection must cover exactly the epoch members "
            f"(missing={missing}, extra={extra})"
        )

    members: list[dict[str, Any]] = []
    for dataset_id, m in sorted(members_in.items()):
        chosen = (
            conn.execute(
                text(
                    "SELECT id, feature_values_hash, content_hash "
                    "FROM profiling.bam_profiles "
                    "WHERE dataset_registry_id = :d AND content_hash = :c"
                ),
                {"d": m["dataset_registry_id"], "c": selections[dataset_id]},
            )
            .mappings()
            .first()
        )
        if chosen is None:
            raise ContractValidationError(
                f"selection for {dataset_id} does not resolve to an accepted row "
                f"(content_hash {selections[dataset_id][:12]}…)"
            )
        members.append({**m, **dict(chosen), "bam_profile_id": chosen["id"]})
    if len(members) != int(snap["sample_count"]):
        raise ContractValidationError(
            f"member count {len(members)} != epoch sample_count {snap['sample_count']}"
        )

    snapshot_hash = canonical_hash(
        {
            "epoch": epoch,
            "split_manifest_hash": snap["manifest_hash"],
            "registry_snapshot_hash": snap["registry_snapshot_hash"],
            "members": [
                {
                    "dataset_id": m["dataset_id"],
                    "partition": m["partition"],
                    "content_hash": m["content_hash"],
                    "feature_values_hash": m["feature_values_hash"],
                }
                for m in members
            ],
        }
    )
    snapshot_id = conn.execute(
        text(
            "INSERT INTO profiling.profile_snapshots "
            "(epoch, split_snapshot_id, split_manifest_hash, registry_snapshot_hash, "
            " member_count, snapshot_hash) VALUES (:e, :sid, :smh, :rsh, :mc, :sh) "
            "RETURNING id"
        ),
        {
            "e": epoch,
            "sid": snap["id"],
            "smh": snap["manifest_hash"],
            "rsh": snap["registry_snapshot_hash"],
            "mc": len(members),
            "sh": snapshot_hash,
        },
    ).scalar_one()
    for m in members:
        conn.execute(
            text(
                "INSERT INTO profiling.profile_snapshot_members "
                "(profile_snapshot_id, bam_profile_id, dataset_registry_id, partition, "
                " feature_values_hash) VALUES (:s, :b, :d, :p, :f)"
            ),
            {
                "s": snapshot_id,
                "b": m["bam_profile_id"],
                "d": m["dataset_registry_id"],
                "p": m["partition"],
                "f": m["feature_values_hash"],
            },
        )
    return str(snapshot_id)
