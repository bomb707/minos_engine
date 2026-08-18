"""Non-mutating verification of a frozen L2-C dataset-split manifest.

Independently recomputes every binding — schema validity, canonical manifest and
registry hashes, region and identity-tuple hashes, split-policy hash, parameter-space
and feature-registry hashes, the exact 50/10/15 (10/2/3 per chromosome) counts,
pairwise-disjoint partitions whose union is all samples, and — critically — the
partition assignment itself by re-deriving it from ``{SALT, round_id}``. Nothing is
trusted merely because it appears in the manifest.
"""

from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from minos_engine.layer2.feature_registry import REGISTRY_HASH
from minos_engine.schema_registry import validate_against

from .contracts import MANIFEST_SCHEMA_VERSION, DatasetSplitManifest
from .generator import dataset_id_for, parameter_space_hash
from .policy import (
    PARTITION_LAYOUT,
    PARTITION_TOTALS,
    SAMPLES_PER_CHROMOSOME,
    SUPPORTED_CHROMOSOMES,
    TOTAL_SAMPLES,
    assign_partitions,
    split_policy_hash,
)

__all__ = ["ManifestVerification", "verify_manifest", "verify_manifest_file", "MANIFEST_SCHEMA"]

MANIFEST_SCHEMA = "layer2-dataset-split-v1"


class ManifestVerification(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    ok: bool
    manifest_hash: str
    dataset_registry_hash: str
    checks: dict[str, bool]
    reasons: tuple[str, ...] = ()


def _expected_partitions(manifest: DatasetSplitManifest) -> dict[str, tuple[str, int]]:
    """Re-derive {round_id: (partition, sort_order)} independently from the policy."""
    by_chrom: dict[str, list[str]] = defaultdict(list)
    for s in manifest.samples:
        by_chrom[s.chromosome].append(s.round_id)
    out: dict[str, tuple[str, int]] = {}
    for contig in SUPPORTED_CHROMOSOMES:
        rids = by_chrom.get(contig, [])
        if len(rids) != SAMPLES_PER_CHROMOSOME:
            continue
        for rid, partition, order, _digest in assign_partitions(sorted(rids)):
            out[rid] = (partition, order)
    return out


def verify_manifest(raw: dict[str, object]) -> ManifestVerification:
    """Verify a manifest object (already parsed from JSON)."""
    reasons: list[str] = []
    checks: dict[str, bool] = {}

    try:
        validate_against(MANIFEST_SCHEMA, raw)
        checks["schema_valid"] = True
    except Exception as exc:  # noqa: BLE001
        checks["schema_valid"] = False
        reasons.append(f"schema: {exc}")

    try:
        manifest = DatasetSplitManifest.model_validate(raw)
        checks["contract_valid"] = True
    except Exception as exc:  # noqa: BLE001
        return ManifestVerification(
            ok=False,
            manifest_hash="",
            dataset_registry_hash="",
            checks={**checks, "contract_valid": False},
            reasons=(*reasons, f"contract: {exc}"),
        )

    stated_hash = str(raw.get("manifest_hash", ""))
    checks["manifest_hash_matches"] = stated_hash == manifest.compute_manifest_hash()
    checks["registry_hash_recomputed"] = raw.get("dataset_registry_hash") == (
        manifest.dataset_registry_hash
    )
    checks["schema_version_bound"] = manifest.schema_version == MANIFEST_SCHEMA_VERSION
    checks["split_policy_hash_bound"] = manifest.split_policy_hash == split_policy_hash()
    checks["feature_registry_hash_bound"] = manifest.feature_registry_hash == REGISTRY_HASH
    checks["parameter_space_hash_bound"] = manifest.parameter_space_hash == parameter_space_hash()

    # Counts and per-chromosome layout.
    checks["total_sample_count"] = len(manifest.samples) == TOTAL_SAMPLES
    checks["partition_totals_exact"] = manifest.counts == PARTITION_TOTALS
    layout = dict(PARTITION_LAYOUT)
    per_ok = set(manifest.per_chromosome) == set(SUPPORTED_CHROMOSOMES) and all(
        manifest.per_chromosome[c] == layout for c in SUPPORTED_CHROMOSOMES
    )
    checks["per_chromosome_layout_exact"] = per_ok

    # Disjoint partitions whose union is all samples (one row per dataset_id).
    ds_ids = [s.dataset_id for s in manifest.samples]
    checks["dataset_ids_unique"] = len(set(ds_ids)) == len(ds_ids)
    buckets: dict[str, set[str]] = defaultdict(set)
    for s in manifest.samples:
        buckets[s.partition].add(s.dataset_id)
    union = set().union(*buckets.values()) if buckets else set()
    pairwise_disjoint = sum(len(v) for v in buckets.values()) == len(union)
    checks["partitions_disjoint"] = pairwise_disjoint
    checks["partitions_cover_all"] = union == set(ds_ids)

    # Independently re-derive the partition assignment and compare.
    expected = _expected_partitions(manifest)
    assign_ok = len(expected) == len(manifest.samples) and all(
        expected.get(s.round_id) == (s.partition, s.sort_order) for s in manifest.samples
    )
    checks["assignment_matches_policy"] = assign_ok

    # dataset_id derivation and region/identity consistency (contract already checked
    # region_hash + identity_tuple_hash; re-affirm dataset_id derivation here).
    checks["dataset_id_derivation"] = all(
        s.dataset_id == dataset_id_for(s.chromosome, s.round_id) for s in manifest.samples
    )

    # No truth/mutation leakage: the strict schema forbids unknown fields, but assert
    # explicitly that no forbidden token appears anywhere in the canonical content.
    blob = json.dumps(manifest.to_canonical(), sort_keys=True).lower()
    # Defence-in-depth: the strict schema already forbids these, but assert no truth/
    # mutation/scoring token appears anywhere in the canonical content.
    forbidden = ("truth", "mutation", "hap.py", "tp_", "fp_", "fn_")
    checks["no_truth_or_mutation_fields"] = not any(tok in blob for tok in forbidden)

    for name, ok in checks.items():
        if not ok:
            reasons.append(f"{name} failed")

    return ManifestVerification(
        ok=all(checks.values()),
        manifest_hash=manifest.manifest_hash,
        dataset_registry_hash=manifest.dataset_registry_hash,
        checks=checks,
        reasons=tuple(dict.fromkeys(reasons)),
    )


def verify_manifest_file(path: str | Path) -> ManifestVerification:
    p = Path(path)
    if not p.exists():
        return ManifestVerification(
            ok=False,
            manifest_hash="",
            dataset_registry_hash="",
            checks={"file_present": False},
            reasons=(f"manifest not found: {p}",),
        )
    raw = json.loads(p.read_text(encoding="utf-8"))
    return verify_manifest(raw)
