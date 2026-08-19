"""L2-D profile ingestion + per-epoch snapshot persistence (schema-owner writes).

Storage-side counterpart of the pure ``layer2.ingest`` validation: this module owns every
database write for L2-D. The intake producer has no write path; no application role holds
INSERT on any L2-D table; writes run as the schema owner (``minos_admin``), exactly like
the split persistence modules.

``ingest_profile`` is fail-closed and two-phase:
  1. **Admission** (own transaction): resolves the registered identity + the epoch's
     ``registry_snapshot_hash`` from the database, runs the complete pure validator, then
     independently re-extracts the canonical feature values from the exact document being
     stored and requires equality with the validator's hash
     (:class:`FeatureHashConflictError` otherwise — the 4-way equality rule: recomputed ==
     validated == typed column == re-derivable from the stored JSONB). Parquet windows are
     verified against the frozen Layer 1 Arrow schema and the manifest row count. Any
     failure rolls the transaction back — nothing enters the accepted corpus.
  2. **Attempt record** (separate transaction): every attempt, admitted or rejected, is
     appended to ``profiling.profile_ingest_attempts`` with its reasons, so rejected or
     partial operational attempts are preserved WITHOUT weakening the accepted-row
     constraints.

``freeze_profile_snapshot`` builds the ``PROFILE-SNAPSHOT-FROZEN-<epoch>`` membership:
one accepted ``bam_profiles`` version per identity in the epoch's split allocation set,
member count equal to the epoch's ``sample_count`` (never a hardcoded corpus size), bound
to the split snapshot id + split manifest hash + registry snapshot hash.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Connection, Engine

from minos_engine.common.errors import (
    AdmissionRejectedError,
    ContractValidationError,
    FeatureHashConflictError,
)
from minos_engine.common.hashing import canonical_hash, sha256_hex
from minos_engine.layer2.ingest.contracts import (
    InputIntegrityAttestation,
    canonical_feature_values_hash,
    extract_eligible_feature_values,
)
from minos_engine.layer2.ingest.validation import validate_admission

__all__ = ["ingest_profile", "freeze_profile_snapshot", "verify_windows_parquet"]


def verify_windows_parquet(parquet_path: Path, expected_row_count: int) -> str:
    """Verify the windows artifact against the frozen Layer 1 Arrow schema + row count.

    Returns the artifact's byte sha256. Storage may import the Layer 1 serializer
    directly (the boundary restriction applies to the ``layer2`` package only).
    """
    import pyarrow.parquet as pq

    from minos_engine.layer1.serializer import WINDOW_ARROW_SCHEMA

    table = pq.read_table(parquet_path)
    if not table.schema.equals(WINDOW_ARROW_SCHEMA):
        raise AdmissionRejectedError("windows parquet schema != frozen Layer 1 window schema")
    if table.num_rows != expected_row_count:
        raise AdmissionRejectedError(
            f"windows parquet rows {table.num_rows} != manifest windows_row_count "
            f"{expected_row_count}"
        )
    return sha256_hex(parquet_path.read_bytes())


def _registry_identity(conn: Connection, dataset_id: str, epoch: int) -> dict[str, Any]:
    row = (
        conn.execute(
            text(
                "SELECT dr.id AS registry_id, dr.dataset_id, dr.round_id, dr.chromosome, "
                " dr.bam_sha256, dr.bai_sha256, dr.reference_sha256, dr.fai_sha256, "
                " dr.region_hash, dr.identity_tuple_hash "
                "FROM catalog.dataset_registry dr WHERE dr.dataset_id = :d"
            ),
            {"d": dataset_id},
        )
        .mappings()
        .first()
    )
    if row is None:
        raise ContractValidationError(f"dataset {dataset_id!r} is not registered")
    snap = (
        conn.execute(
            text("SELECT registry_snapshot_hash FROM catalog.split_snapshots WHERE epoch = :e"),
            {"e": epoch},
        )
        .mappings()
        .first()
    )
    if snap is None:
        raise ContractValidationError(f"split epoch {epoch} is not persisted")
    identity = dict(row)
    identity["registry_snapshot_hash"] = snap["registry_snapshot_hash"]
    return identity


def _insert_artifact(conn: Connection, uri: str, sha256: str) -> str:
    existing = conn.execute(
        text("SELECT id FROM catalog.artifacts WHERE sha256 = :h"), {"h": sha256}
    ).scalar()
    if existing is not None:
        return str(existing)
    return str(
        conn.execute(
            text("INSERT INTO catalog.artifacts (uri, sha256) VALUES (:u, :h) RETURNING id"),
            {"u": uri, "h": sha256},
        ).scalar_one()
    )


def _record_attempt(
    engine: Engine,
    *,
    registry_id: str | None,
    profile_id: str,
    outcome: str,
    reasons: tuple[str, ...],
    attestation_hash: str | None,
    content_hash: str | None,
) -> None:
    with engine.begin() as conn:
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


def ingest_profile(
    engine: Engine,
    *,
    epoch: int,
    profile_document: dict[str, Any],
    manifest_document: dict[str, Any],
    attestation: InputIntegrityAttestation | dict[str, Any],
    profile_artifact_uri: str,
    profile_artifact_sha256: str,
    windows_artifact_uri: str,
    windows_parquet_path: Path,
) -> str:
    """Admit one COMPLETE profile into the accepted corpus; returns the new row id.

    Rejection raises the typed error AND records a REJECTED attempt; admission records an
    ADMITTED attempt. The accepted insert and the attempt record are separate
    transactions, so a rejected attempt survives the admission rollback.
    """
    att = (
        attestation
        if isinstance(attestation, InputIntegrityAttestation)
        else InputIntegrityAttestation.model_validate(attestation)
    )
    profile_id = str(profile_document.get("profile_id", "unknown"))
    registry_id: str | None = None
    try:
        with engine.begin() as conn:
            identity = _registry_identity(conn, att.dataset_id, epoch)
            registry_id = str(identity.pop("registry_id"))

            windows_sha256 = verify_windows_parquet(
                windows_parquet_path, int(manifest_document.get("windows_row_count", -1))
            )
            decision = validate_admission(
                profile_document=profile_document,
                manifest_document=manifest_document,
                attestation=att,
                registry_identity=identity,
                profile_artifact_sha256=profile_artifact_sha256,
                windows_artifact_sha256=windows_sha256,
            )
            if not decision.admissible:
                raise AdmissionRejectedError(f"profile {profile_id} rejected: {decision.reasons}")
            # 4-way equality guard: re-extract from the EXACT document being stored and
            # require equality with the validator's canonical hash before it becomes the
            # typed column value + content_hash component.
            recomputed = canonical_feature_values_hash(
                extract_eligible_feature_values(profile_document)
            )
            if recomputed != decision.feature_values_hash:
                raise FeatureHashConflictError(
                    "canonical feature_values_hash diverged between validation and write"
                )

            profile_artifact_id = _insert_artifact(
                conn, profile_artifact_uri, profile_artifact_sha256
            )
            windows_artifact_id = _insert_artifact(conn, windows_artifact_uri, windows_sha256)

            content_hash = canonical_hash(
                {
                    "identity_tuple_hash": identity["identity_tuple_hash"],
                    "feature_values_hash": decision.feature_values_hash,
                    "l1_feature_values_hash": decision.l1_feature_values_hash,
                    "profile_sha256": profile_artifact_sha256,
                    "windows_sha256": windows_sha256,
                    "profiler_version": str(manifest_document["profiler_version"]),
                    "profiler_config_hash": str(manifest_document["profiler_config_hash"]),
                    "attestation_hash": att.attestation_hash,
                }
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
                    " profile_sha256, windows_sha256, profile_artifact_id, "
                    " windows_artifact_id, content_hash) "
                    "VALUES (:reg, :pid, :bam, :bai, :ref, :fai, :rh, :ith, :m5, :deg, "
                    " :ath, :rsh, 'COMPLETE', :pv, :pch, :wrc, :fvh, :l1h, :evc, "
                    " CAST(:doc AS jsonb), :psha, :wsha, :pa, :wa, :ch) RETURNING id"
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
                    "psha": profile_artifact_sha256,
                    "wsha": windows_sha256,
                    "pa": profile_artifact_id,
                    "wa": windows_artifact_id,
                    "ch": content_hash,
                },
            ).scalar_one()
    except Exception as exc:
        _record_attempt(
            engine,
            registry_id=registry_id,
            profile_id=profile_id,
            outcome="REJECTED",
            reasons=(str(exc)[:500],),
            attestation_hash=att.attestation_hash,
            content_hash=None,
        )
        raise
    _record_attempt(
        engine,
        registry_id=registry_id,
        profile_id=profile_id,
        outcome="ADMITTED",
        reasons=(),
        attestation_hash=att.attestation_hash,
        content_hash=content_hash,
    )
    return str(row_id)


def freeze_profile_snapshot(conn: Connection, epoch: int) -> str:
    """Freeze the epoch's profile snapshot; returns the new snapshot id.

    Membership is derived from the epoch's split allocations: every allocated identity
    must have EXACTLY ONE accepted ``bam_profiles`` row (zero → incomplete corpus;
    more than one → ambiguous version, requiring an explicit owner selection — both fail
    closed). ``member_count`` must equal the split epoch's ``sample_count``.
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

    rows = conn.execute(
        text(
            "SELECT ea.dataset_registry_id, ea.partition, dr.dataset_id, "
            " bp.id AS bam_profile_id, bp.feature_values_hash, bp.content_hash, "
            " count(bp.id) OVER (PARTITION BY ea.dataset_registry_id) AS n_versions "
            "FROM catalog.split_epoch_allocations ea "
            "JOIN catalog.split_snapshots ss ON ss.id = ea.snapshot_id "
            "JOIN catalog.dataset_registry dr ON dr.id = ea.dataset_registry_id "
            "LEFT JOIN profiling.bam_profiles bp "
            "  ON bp.dataset_registry_id = ea.dataset_registry_id "
            "WHERE ss.epoch = :e "
            "ORDER BY dr.dataset_id"
        ),
        {"e": epoch},
    ).mappings()
    members: list[dict[str, Any]] = []
    for r in rows:
        if r["bam_profile_id"] is None:
            raise ContractValidationError(
                f"identity {r['dataset_id']} has no accepted profile — corpus incomplete"
            )
        if int(r["n_versions"]) != 1:
            raise ContractValidationError(
                f"identity {r['dataset_id']} has {r['n_versions']} accepted versions — "
                "ambiguous; explicit version selection required"
            )
        members.append(dict(r))
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
