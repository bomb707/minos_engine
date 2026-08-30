"""Seed the two scratch databases the Phase-D preparation proof needs.

The preparation boundary spans two stores, so proving it needs two: a SOURCE that looks like the
closed TRAIN baseline at ``0020`` and holds the four frozen CONFIG payloads, and a TARGET that
looks like an empty validation store at the validation revision with the ten VALIDATION members
allocated upstream.

The four payloads are the REAL frozen bytes. They are copied from the repository-local campaign
fixture into the scratch source's own root — never referenced in place, so a test can tamper with
its copy without touching the committed bundle. Nothing here fabricates a configuration: a
synthetic four would hash to something else, and ``0024`` anchors the real four as SQL literals,
so a synthetic campaign could not reach the bootstrap at all. The proof runs on the real identities
and a scratch substrate, which is the only combination that proves anything.

The ten members are seeded from the frozen validation schedule — the same ``dataset_id`` /
``round_id`` / ``chromosome`` triples the schedule names — because the resolver matches on them.
Everything else about those rows (BAM digests, feature hashes) is deterministic scratch filler:
Phase-D preparation reads identity and partition, and reads no BAM.
"""

from __future__ import annotations

import hashlib
import shutil
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy import Connection, text

from tests.l2f2_phase_d_fixture import FIXTURE_CONFIG_ROOT

CONFIG_MEDIA_TYPE = "application/vnd.minos.l2f-config+json"
CONFIG_SCHEMA_VERSION = "l2f-config-payload-v1"

_NS = uuid.UUID("0000000d-2f2f-2f2f-2f2f-0000000000d0")


def U(label: str) -> str:
    return str(uuid.uuid5(_NS, label))


def H(label: str) -> str:
    return hashlib.sha256(label.encode("utf-8")).hexdigest()


_JSONB_COLUMNS = frozenset({"profile_document", "column_manifest"})


def _insert(conn: Connection, schema: str, table: str, row: dict[str, Any]) -> None:
    cols = ", ".join(row)
    vals = ", ".join(f"CAST(:{c} AS jsonb)" if c in _JSONB_COLUMNS else f":{c}" for c in row)
    conn.execute(text(f"INSERT INTO {schema}.{table} ({cols}) VALUES ({vals})"), row)  # noqa: S608


@dataclass(frozen=True)
class SeededSource:
    """What the scratch baseline holds: the four frozen payloads, addressable by hash."""

    config_root: Path
    config_hashes: tuple[str, ...]


def seed_source_configs(
    conn: Connection,
    ordered_config_hashes: tuple[str, ...],
    parameter_space_hash: str,
    *,
    config_root: Path,
    tamper: str | None = None,
) -> SeededSource:
    """Register the four frozen CONFIG payloads in a scratch baseline store.

    ``tamper`` names one config hash whose COPIED bytes are altered after registration, so the
    payload no longer hashes to the identity the row claims. The campaign's own file is untouched.
    """
    config_root.mkdir(parents=True, exist_ok=True)
    conn.execute(text("SET ROLE minos_admin"))
    for config_hash in ordered_config_hashes:
        source = FIXTURE_CONFIG_ROOT / f"{config_hash}.json"
        payload = source.read_bytes()
        if hashlib.sha256(payload).hexdigest() != config_hash:  # pragma: no cover - fail closed
            raise AssertionError(f"fixture artifact {source} does not hash to its own name")
        target = config_root / f"{config_hash}.json"
        shutil.copyfile(source, target)
        artifact_id = U(f"cfg:art:{config_hash}")
        _insert(
            conn,
            "catalog",
            "artifacts",
            {
                "id": artifact_id,
                "uri": f"file://{target.resolve()}",
                "sha256": config_hash,
                "media_type": CONFIG_MEDIA_TYPE,
                "size_bytes": len(payload),
                "provenance": "l2f2-phase-c-frozen",
            },
        )
        _insert(
            conn,
            "experiments",
            "l2f_config_payloads",
            {
                "id": U(f"cfg:payload:{config_hash}"),
                "config_hash": config_hash,
                "parameter_space_hash": parameter_space_hash,
                "schema_version": CONFIG_SCHEMA_VERSION,
                "media_type": CONFIG_MEDIA_TYPE,
                "artifact_id": artifact_id,
            },
        )
        if tamper == config_hash:
            target.write_bytes(payload + b"\n")
    conn.execute(text("RESET ROLE"))
    return SeededSource(config_root=config_root, config_hashes=tuple(ordered_config_hashes))


@dataclass(frozen=True)
class SeededTarget:
    """The upstream a validation plan is built against."""

    profile_snapshot_id: str
    snapshot_hash: str
    split_manifest_hash: str
    registry_snapshot_hash: str
    dataset_registry_ids: dict[str, str]


def seed_target_upstream(
    conn: Connection,
    members: tuple[Any, ...],
    *,
    split_manifest_hash: str,
    partitions: dict[str, str] | None = None,
    duplicate_dataset: str | None = None,
    second_snapshot_for: str | None = None,
    omit_member: str | None = None,
    wrong_round_for: str | None = None,
    wrong_chromosome_for: str | None = None,
) -> SeededTarget:
    """Seed the ten VALIDATION members (and, on request, one deliberate defect).

    ``partitions`` overrides the split allocation for named ``dataset_id``s — the TRAIN and TEST
    exclusion negatives. ``omit_member`` leaves one member unseeded. ``wrong_round_for`` and
    ``wrong_chromosome_for`` register one member under an identity the frozen schedule does not
    name, so a store holding a plausible near-miss cannot pass for the campaign.

    Two defects need a second profile snapshot, and therefore a second split snapshot, because
    ``uq_profile_snapshots_split_snapshot`` allows only one profile snapshot per split:

    * ``second_snapshot_for`` puts one member's ONLY snapshot membership in the second snapshot,
      so the ten members span two — a campaign measured against two different worlds;
    * ``duplicate_dataset`` gives one member a membership in BOTH, so it resolves to two
      authoritative rows and the resolver cannot choose.

    A second ``catalog.dataset_registry`` row for the same ``dataset_id`` is deliberately NOT how
    ambiguity is staged: ``uq_dataset_registry_dataset_id`` makes that unconstructable, so the
    resolver's count check is exercised through the membership graph, which is where a real
    duplicate could actually arise.
    """
    overrides = partitions or {}
    needs_second = duplicate_dataset is not None or second_snapshot_for is not None
    conn.execute(text("SET ROLE minos_admin"))

    generic_artifact = U("art:generic")
    _insert(
        conn,
        "catalog",
        "artifacts",
        {
            "id": generic_artifact,
            "uri": "mem://gen/validation",
            "sha256": H("art:generic"),
            "media_type": "application/octet-stream",
        },
    )
    snapshot_id = U("profile:snapshot")
    second_snapshot_id = U("profile:snapshot:2")
    snapshot_hash = H("profile:snapshot:hash")
    registry_snapshot_hash = H("registry_snapshot")
    for epoch in (1, 2):
        if epoch == 2 and not needs_second:
            continue
        split_id = U(f"split:snapshot:{epoch}")
        _insert(
            conn,
            "catalog",
            "split_snapshots",
            {
                "id": split_id,
                "epoch": epoch,
                "salt": "salt",
                "split_policy_version": "v1",
                "policy_hash": H(f"policy:{epoch}"),
                "manifest_hash": (split_manifest_hash if epoch == 1 else H(f"ssmanifest:{epoch}")),
                "registry_snapshot_hash": (
                    registry_snapshot_hash if epoch == 1 else H(f"ssregistry:{epoch}")
                ),
                "ancestor_v1_dataset_registry_hash": H("ancestor"),
                "parent_registry_snapshot_hash": None if epoch == 1 else registry_snapshot_hash,
                "parent_manifest_hash": None if epoch == 1 else split_manifest_hash,
                "parent_snapshot_id": None if epoch == 1 else U("split:snapshot:1"),
                "parent_epoch": None if epoch == 1 else 1,
                "transition_count": 0,
                "sample_count": len(members),
                "count_train": 0,
                "count_validation": len(members),
                "count_test": 0,
            },
        )
        _insert(
            conn,
            "profiling",
            "profile_snapshots",
            {
                "id": snapshot_id if epoch == 1 else second_snapshot_id,
                "epoch": epoch,
                "split_snapshot_id": split_id,
                "split_manifest_hash": (
                    split_manifest_hash if epoch == 1 else H(f"ssmanifest:{epoch}")
                ),
                "registry_snapshot_hash": (
                    registry_snapshot_hash if epoch == 1 else H(f"ssregistry:{epoch}")
                ),
                "member_count": len(members),
                "snapshot_hash": snapshot_hash if epoch == 1 else H("profile:snapshot:hash:2"),
            },
        )

    registry_ids: dict[str, str] = {}
    for member in members:
        label = member.dataset_id
        if omit_member == label:
            continue
        dsr_id = U(f"dsr:{label}")
        registry_ids[label] = dsr_id
        _insert(
            conn,
            "catalog",
            "dataset_registry",
            _dataset_row(
                label,
                dsr_id,
                round_id="0000000000000000" if wrong_round_for == label else None,
                chromosome="chr18" if wrong_chromosome_for == label else None,
            ),
        )
        _insert(
            conn,
            "catalog",
            "split_allocations",
            {
                "id": U(f"alloc:{dsr_id}"),
                "dataset_registry_id": dsr_id,
                "partition": overrides.get(label, "validation"),
                "sort_order": 0,
                "manifest_hash": split_manifest_hash,
            },
        )
        bam_id = U(f"bam:{label}")
        _insert(conn, "profiling", "bam_profiles", _bam_row(label, dsr_id, generic_artifact))
        homes = [snapshot_id]
        if second_snapshot_for == label:
            homes = [second_snapshot_id]
        elif duplicate_dataset == label:
            homes = [snapshot_id, second_snapshot_id]
        for home in homes:
            _insert(
                conn,
                "profiling",
                "profile_snapshot_members",
                {
                    "id": U(f"psm:{label}:{home}"),
                    "profile_snapshot_id": home,
                    "bam_profile_id": bam_id,
                    "dataset_registry_id": dsr_id,
                    "partition": overrides.get(label, "validation"),
                    "feature_values_hash": H(f"fvh:{label}"),
                },
            )
    conn.execute(text("RESET ROLE"))
    return SeededTarget(
        profile_snapshot_id=snapshot_id,
        snapshot_hash=snapshot_hash,
        split_manifest_hash=split_manifest_hash,
        registry_snapshot_hash=registry_snapshot_hash,
        dataset_registry_ids=registry_ids,
    )


def _dataset_row(
    label: str,
    dsr_id: str,
    *,
    round_id: str | None = None,
    chromosome: str | None = None,
) -> dict[str, Any]:
    tag = label
    chromosome = chromosome or label.split("-")[1]
    round_id = round_id or label.split("-")[2]
    return {
        "id": dsr_id,
        "dataset_id": label,
        "round_id": round_id,
        "chromosome": chromosome,
        "region_source": "scratch",
        "region_start0": 0,
        "region_end0_exclusive": 1000,
        "region_length_bp": 1000,
        "region_coordinate_system": "zero-based-half-open",
        "region_hash": H(f"region:{tag}"),
        "bam_sha256": H(f"bam:{tag}"),
        "bai_sha256": H(f"bai:{tag}"),
        "reference_sha256": H(f"ref:{tag}"),
        "fai_sha256": H(f"fai:{tag}"),
        "bam_size_bytes": 1024,
        "parameter_space_hash": H(f"dsr_ps:{tag}"),
        "feature_registry_hash": H(f"dsr_fr:{tag}"),
        "identity_tuple_hash": H(f"idtuple:{tag}"),
        "manifest_hash": H(f"manifest:{tag}"),
        "split_algorithm_version": "v1",
        "split_salt": "salt",
        "allocation_digest": H(f"alloc:{tag}"),
    }


def _bam_row(label: str, dsr_id: str, artifact_id: str) -> dict[str, Any]:
    return {
        "id": U(f"bam:{label}"),
        "dataset_registry_id": dsr_id,
        "profile_id": f"profile-{label}",
        "bam_sha256": H(f"bam:{label}"),
        "bai_sha256": H(f"bai:{label}"),
        "reference_sha256": H(f"ref:{label}"),
        "fai_sha256": H(f"fai:{label}"),
        "region_hash": H(f"region:{label}"),
        "identity_tuple_hash": H(f"bam_idtuple:{label}"),
        "m5_status": "MATCH",
        "integrity_degraded": False,
        "attestation_hash": H(f"attest:{label}"),
        "registry_snapshot_hash": H(f"bam_reg:{label}"),
        # ck_bam_profiles_complete_only makes a non-COMPLETE bam_profile row unconstructable at
        # the database boundary, so the resolver's COMPLETE check is belt over a DB CHECK and has
        # no constructible negative.
        "profile_status": "COMPLETE",
        "profiler_version": "v1",
        "profiler_config_hash": H(f"profiler_cfg:{label}"),
        "windows_row_count": 10,
        "feature_values_hash": H(f"fvh:{label}"),
        "l1_feature_values_hash": H(f"l1_fvh:{label}"),
        "eligible_value_count": 10,
        "profile_document": "{}",
        "profile_sha256": H(f"profile_sha:{label}"),
        "profile_manifest_sha256": H(f"profile_manifest:{label}"),
        "windows_sha256": H(f"windows_sha:{label}"),
        "profile_artifact_id": artifact_id,
        "profile_manifest_artifact_id": artifact_id,
        "windows_artifact_id": artifact_id,
        "ingestion_key": H(f"ingestion:{label}"),
        "content_hash": H(f"content:{label}"),
    }
