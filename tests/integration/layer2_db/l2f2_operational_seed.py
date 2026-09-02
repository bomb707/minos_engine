"""Seed a scratch analogue of the verified operational lineage store.

The provisioner reads ``minos_engine_db`` at ``0005_l2e_feature_view``: the store whose content
was verified against ``PROFILE-SNAPSHOT-FROZEN-1``. This builds a scratch stand-in holding the
frozen snapshot identity and the exact ten VALIDATION lineages, so provisioning can be proven
end to end without touching the operational database.

Every identity that the provisioner compares comes from the FROZEN authorities, not from this
module's imagination: the ten members' ``dataset_id`` / ``round_id`` / ``chromosome`` /
``identity_tuple_hash`` are read from ``build_validation_schedule()``, and the snapshot and
registry-snapshot hashes are the accepted ``cf717ebb…`` / ``3e60aa65…``. A seeder that invented
those would make the checks it exists to exercise unfalsifiable.

The remaining columns — region geometry, BAM digests, profile documents — are deterministic
scratch filler. Provisioning transfers them verbatim and never interprets them, so their VALUES
are immaterial; what matters is that they are carried across byte for byte, which the proof
asserts by comparing source and target rows directly.

TRAIN and TEST members are seeded too, and deliberately: a proof that TEST is not transferred is
worth nothing unless TEST was there to be transferred.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from pathlib import Path
from typing import Any

from sqlalchemy import Connection, text

from minos_engine.baseline.validation_members import build_validation_schedule

__all__ = [
    "ACCEPTED_REGISTRY_SNAPSHOT_HASH",
    "provision_scratch_dataset_root",
    "ACCEPTED_SNAPSHOT_HASH",
    "OPERATIONAL_REVISION",
    "seed_operational_store",
]

#: mirrors the verified operational store.
OPERATIONAL_REVISION = "0005_l2e_feature_view"
ACCEPTED_SNAPSHOT_HASH = "cf717ebb44e76a3408e975e027b51139df28d643dd1616c5edbce3643182c4c7"
ACCEPTED_REGISTRY_SNAPSHOT_HASH = "3e60aa65aeed8969e29ebeef83024f6fa2285a13c155d7d6dc0c601d1e94f675"
#: the DB-side split identity the frozen snapshot cites — NOT the manifest file's sha256.
ACCEPTED_SPLIT_MANIFEST_HASH = "b23cd5716ab46033f7ea0bf123cc9b2a5f401fa37dbffddba8d4201f5ea76145"

SNAPSHOT_MEMBER_COUNT = 75
_NS = uuid.UUID("0000000e-2f2f-2f2f-2f2f-00000000ee11")

_CHROMOSOMES = ("chr18", "chr19", "chr20", "chr21", "chr22")


def U(label: str) -> str:
    return str(uuid.uuid5(_NS, label))


def H(label: str) -> str:
    return hashlib.sha256(label.encode("utf-8")).hexdigest()


def _insert(conn: Connection, schema: str, table: str, row: dict[str, Any]) -> None:
    cols = ", ".join(row)
    vals = ", ".join(f"CAST(:{c} AS jsonb)" if c == "profile_document" else f":{c}" for c in row)
    conn.execute(text(f"INSERT INTO {schema}.{table} ({cols}) VALUES ({vals})"), row)  # noqa: S608


def seed_operational_store(
    conn: Connection,
    *,
    snapshot_hash: str = ACCEPTED_SNAPSHOT_HASH,
    registry_snapshot_hash: str = ACCEPTED_REGISTRY_SNAPSHOT_HASH,
    member_count: int = SNAPSHOT_MEMBER_COUNT,
    partitions: dict[str, str] | None = None,
    omit_member: str | None = None,
    duplicate_member: str | None = None,
    field_overrides: dict[str, dict[str, Any]] | None = None,
    profile_overrides: dict[str, dict[str, Any]] | None = None,
    mismatch_feature_values_for: str | None = None,
) -> dict[str, Any]:
    """Build the 75-member frozen snapshot: the ten frozen VALIDATION members plus filler.

    The keyword defects exist so the provisioner's refusals can be exercised against a store that
    is otherwise entirely valid.
    """
    overrides = field_overrides or {}
    profile_over = profile_overrides or {}
    partition_over = partitions or {}
    conn.execute(text("SET ROLE minos_admin"))

    generic_artifact = U("art:generic")
    _insert(
        conn,
        "catalog",
        "artifacts",
        {
            "id": generic_artifact,
            "uri": "mem://gen/operational",
            "sha256": H("art:generic"),
            "media_type": "application/octet-stream",
        },
    )

    split_snapshot_id = U("split:snapshot:1")
    _insert(
        conn,
        "catalog",
        "split_snapshots",
        {
            "id": split_snapshot_id,
            "epoch": 1,
            "salt": "salt",
            "split_policy_version": "v2",
            "policy_hash": H("policy"),
            "manifest_hash": ACCEPTED_SPLIT_MANIFEST_HASH,
            "registry_snapshot_hash": registry_snapshot_hash,
            "ancestor_v1_dataset_registry_hash": H("ancestor"),
            "parent_registry_snapshot_hash": None,
            "parent_manifest_hash": None,
            "parent_snapshot_id": None,
            "parent_epoch": None,
            "transition_count": 0,
            "sample_count": 75,
            "count_train": 50,
            "count_validation": 10,
            "count_test": 15,
        },
    )

    snapshot_id = U("profile:snapshot:1")
    _insert(
        conn,
        "profiling",
        "profile_snapshots",
        {
            "id": snapshot_id,
            "epoch": 1,
            "split_snapshot_id": split_snapshot_id,
            "split_manifest_hash": ACCEPTED_SPLIT_MANIFEST_HASH,
            "registry_snapshot_hash": registry_snapshot_hash,
            "member_count": member_count,
            "snapshot_hash": snapshot_hash,
        },
    )

    schedule = build_validation_schedule()
    validation = [
        {
            "dataset_id": m.dataset_id,
            "round_id": m.round_id,
            "chromosome": m.chromosome,
            "identity_tuple_hash": m.identity_tuple_hash,
            "partition": "validation",
        }
        for m in schedule.members
    ]
    # the fifty TRAIN and fifteen TEST members: present, so their exclusion means something.
    filler = [
        {
            "dataset_id": f"minos-{chrom}-filler{index:016x}",
            "round_id": f"filler{index:010x}",
            "chromosome": chrom,
            "identity_tuple_hash": H(f"filler:{index}"),
            "partition": "train" if index % 65 < 50 else "test",
        }
        for index, chrom in enumerate((_CHROMOSOMES * 13)[:65], start=1)
    ]

    resolved: dict[str, Any] = {"snapshot_id": snapshot_id, "validation": []}
    for entry in validation + filler:
        label = entry["dataset_id"]
        if omit_member == label:
            continue
        for suffix in ("", ":dup") if duplicate_member == label else ("",):
            registry_id = U(f"dsr:{label}{suffix}")
            over = overrides.get(label, {}) if suffix == "" else {}
            row = {
                "id": registry_id,
                "dataset_id": label if suffix == "" else f"{label}-dup",
                "round_id": entry["round_id"],
                "chromosome": entry["chromosome"],
                "region_source": "operational",
                "region_start0": 0,
                "region_end0_exclusive": 1000,
                "region_length_bp": 1000,
                "region_coordinate_system": "zero-based-half-open",
                "region_hash": H(f"region:{label}"),
                "bam_sha256": H(f"bam:{label}"),
                "bai_sha256": H(f"bai:{label}"),
                "reference_sha256": H(f"ref:{label}"),
                "fai_sha256": H(f"fai:{label}"),
                "bam_size_bytes": 1024,
                "parameter_space_hash": H(f"ps:{label}"),
                "feature_registry_hash": H(f"fr:{label}"),
                "identity_tuple_hash": entry["identity_tuple_hash"],
                "manifest_hash": ACCEPTED_SPLIT_MANIFEST_HASH,
                "split_algorithm_version": "v2",
                "split_salt": "salt",
                "allocation_digest": H(f"alloc:{label}"),
            }
            row.update(over)
            if suffix == ":dup":
                row["dataset_id"] = label  # a second row claiming the SAME dataset identity
                row["id"] = U(f"dsr:dup:{label}")
            _insert(conn, "catalog", "dataset_registry", row)
            _insert(
                conn,
                "catalog",
                "split_allocations",
                {
                    "id": U(f"alloc:{row['id']}"),
                    "dataset_registry_id": row["id"],
                    "partition": partition_over.get(label, entry["partition"]),
                    "sort_order": 0,
                    "manifest_hash": ACCEPTED_SPLIT_MANIFEST_HASH,
                },
            )
            # three distinct artifacts per profile, as the real operational store holds:
            # profile document, profile manifest, windows. Sharing one would hide the
            # provisioner's artifact closure and its de-duplication.
            artifacts = {}
            for kind in ("profile", "manifest", "windows"):
                artifacts[kind] = U(f"art:{kind}:{label}")
                _insert(
                    conn,
                    "catalog",
                    "artifacts",
                    {
                        "id": artifacts[kind],
                        "uri": f"file:///operational/{label}/{kind}",
                        "sha256": H(f"art:{kind}:{label}"),
                        "media_type": "application/octet-stream",
                        "size_bytes": 1024,
                        "provenance": "layer1-profiler-v1",
                    },
                )
            profile_row = _bam_row(label, row["id"], artifacts, entry)
            profile_row.update(profile_over.get(label, {}) if suffix == "" else {})
            _insert(conn, "profiling", "bam_profiles", profile_row)
            _insert(
                conn,
                "profiling",
                "profile_snapshot_members",
                {
                    "id": U(f"psm:{row['id']}"),
                    "profile_snapshot_id": snapshot_id,
                    "bam_profile_id": profile_row["id"],
                    "dataset_registry_id": row["id"],
                    "partition": partition_over.get(label, entry["partition"]),
                    "feature_values_hash": (
                        H(f"divergent:{label}")
                        if mismatch_feature_values_for == label
                        else profile_row["feature_values_hash"]
                    ),
                },
            )
            if entry["partition"] == "validation" and suffix == "":
                resolved["validation"].append(
                    {
                        "dataset_id": label,
                        "dataset_registry_id": row["id"],
                        "bam_profile_id": profile_row["id"],
                    }
                )
    conn.execute(text("RESET ROLE"))
    return resolved


def _bam_row(
    label: str, registry_id: str, artifacts: dict[str, str], entry: dict[str, Any]
) -> dict[str, Any]:
    return {
        "id": U(f"bam:{label}"),
        "dataset_registry_id": registry_id,
        "profile_id": f"profile-{label}",
        "bam_sha256": H(f"bam:{label}"),
        "bai_sha256": H(f"bai:{label}"),
        "reference_sha256": H(f"ref:{label}"),
        "fai_sha256": H(f"fai:{label}"),
        "region_hash": H(f"region:{label}"),
        "identity_tuple_hash": entry["identity_tuple_hash"],
        "m5_status": "ABSENT",
        "integrity_degraded": True,
        "attestation_hash": H(f"attest:{label}"),
        "registry_snapshot_hash": ACCEPTED_REGISTRY_SNAPSHOT_HASH,
        "profile_status": "COMPLETE",
        "profiler_version": "layer1-profiler-v1",
        "profiler_config_hash": H("profiler-config"),
        "windows_row_count": 10,
        "feature_values_hash": H(f"fvh:{label}"),
        "l1_feature_values_hash": H(f"l1:{label}"),
        "eligible_value_count": 10,
        "profile_document": json.dumps({"dataset_id": label}, sort_keys=True),
        "profile_sha256": H(f"profile:{label}"),
        "profile_manifest_sha256": H(f"manifest:{label}"),
        "windows_sha256": H(f"windows:{label}"),
        "profile_artifact_id": artifacts["profile"],
        "profile_manifest_artifact_id": artifacts["manifest"],
        "windows_artifact_id": artifacts["windows"],
        "ingestion_key": H(f"ingest:{label}"),
        "content_hash": H(f"content:{label}"),
    }


def scratch_root_under_minos(prefix: str, *, fallback: Path) -> tuple[Path, Path]:
    """A scratch root under the MINOS physical root when one exists, else under ``fallback``.

    Delegates to :mod:`tests.minos_scratch`, which discovers the root rather than assuming the
    operator's. Returns ``(scratch, effective_root)`` so containment can be asserted against the
    root actually in force.
    """
    from tests.minos_scratch import minos_scratch_root

    return minos_scratch_root(prefix, fallback=fallback)


def provision_scratch_dataset_root(root: Path, members: tuple[Any, ...]) -> dict[str, Any]:
    """Write a complete input set per member and return the REAL digests of what was written.

    The operational seeder can then record those digests, so the scratch campaign's metadata and
    its provisioned bytes agree — which is what makes the byte verifier's PASS mean something
    rather than being a comparison of one invented constant against another.

    Only BAM/BAI/reference/FAI/dictionary. No truth file is created, so the pre-GATK proof has no
    truth bytes available to open even by accident.
    """
    digests: dict[str, Any] = {}
    for member in members:
        practice = root / "practice" / f"round_{member.round_id}"
        reference = root / "reference" / member.chromosome
        practice.mkdir(parents=True, exist_ok=True)
        reference.mkdir(parents=True, exist_ok=True)

        payloads = {
            # round-distinct bytes, as real BAMs are: identical filler would let a substituted
            # round hash the same as the real one and quietly pass.
            practice / "input.bam": f"bam:{member.dataset_id}\n".encode(),
            practice / "input.bam.bai": f"bai:{member.dataset_id}\n".encode(),
            reference / f"{member.chromosome}.fa": b">seq\nACGTACGTAC\n",
            reference / f"{member.chromosome}.fa.fai": f"fai:{member.chromosome}\n".encode(),
        }
        for path, payload in payloads.items():
            if not path.exists():
                path.write_bytes(payload)
        dictionary = reference / f"{member.chromosome}.dict"
        if not dictionary.exists():
            dictionary.write_bytes(f"@HD\tVN:1.6\n@SQ\tSN:{member.chromosome}\tLN:10\n".encode())

        def sha(path: Path) -> str:
            return hashlib.sha256(path.read_bytes()).hexdigest()

        bam = practice / "input.bam"
        digests[member.dataset_id] = {
            "bam_sha256": sha(bam),
            "bai_sha256": sha(practice / "input.bam.bai"),
            "reference_sha256": sha(reference / f"{member.chromosome}.fa"),
            "fai_sha256": sha(reference / f"{member.chromosome}.fa.fai"),
            "bam_size_bytes": bam.stat().st_size,
            "region_start0": 0,
            "region_end0_exclusive": 10,
            # ck_dataset_registry_region_length ties these together; the override must carry all
            # three or the row is internally inconsistent.
            "region_length_bp": 10,
        }
    return digests
