"""Non-mutating verification of a frozen L2-C v2 epoch split manifest.

Independently recomputes every binding — schema validity, the canonical ``manifest_hash``,
the split-policy hash, the growth-capable ``registry_snapshot_hash``, the exact per-partition
and per-chromosome counts, one row per identity with a valid partition, and the epoch
lineage. For **epoch 1** it proves the partitions are inherited from the accepted v1
manifest *verbatim* (zero transitions, no accepted test/validation sample moved). For
**epoch ≥2** it proves the parent is grandfathered exactly: parent samples unchanged
(identity + partition + origin), only new samples added, nothing removed or replaced.
Nothing is trusted merely because it appears in the manifest.
"""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from typing import Any

from pydantic import BaseModel, ConfigDict

from minos_engine.common.hashing import canonical_hash
from minos_engine.schema_registry import validate_against

from .generator import MANIFEST_SCHEMA_VERSION, registry_snapshot_hash
from .policy import PARTITIONS, SUPPORTED_CHROMOSOMES, split_policy_hash

__all__ = [
    "EpochManifestVerification",
    "verify_epoch_manifest",
    "verify_epoch_against_parent",
    "MANIFEST_SCHEMA",
]

MANIFEST_SCHEMA = "layer2-dataset-split-v2"
_FORBIDDEN_TOKENS = ("truth", "mutation", "hap.py", "tp_", "fp_", "fn_", "score", "label")
_IDENTITY = ("dataset_id", "round_id", "chromosome", "identity_tuple_hash")


class EpochManifestVerification(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    ok: bool
    manifest_hash: str
    registry_snapshot_hash: str
    ancestor_v1_dataset_registry_hash: str
    checks: dict[str, bool]
    reasons: tuple[str, ...] = ()


def _content_without_hash(raw: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in raw.items() if k != "manifest_hash"}


def verify_epoch_against_parent(parent: dict[str, Any], child: dict[str, Any]) -> dict[str, bool]:
    """Prove ``child`` grandfathers ``parent`` exactly (epoch ≥2 immutability).

    Returns a checks dict. Every parent sample must reappear in the child with identical
    identity, partition, and origin_epoch; only new samples (assignment_source v2-policy,
    origin_epoch == child.epoch) may be added; nothing may be removed or replaced.
    """
    checks: dict[str, bool] = {}
    p_by_id = {str(s["dataset_id"]): s for s in parent["samples"]}
    c_by_id = {str(s["dataset_id"]): s for s in child["samples"]}

    # chain binding
    checks["parent_manifest_hash_bound"] = child.get("parent_manifest_hash") == parent.get(
        "manifest_hash"
    )
    checks["parent_registry_snapshot_hash_bound"] = child.get(
        "parent_registry_snapshot_hash"
    ) == parent.get("registry_snapshot_hash")
    p_epoch = parent.get("epoch")
    checks["parent_epoch_linear"] = (
        isinstance(p_epoch, int)
        and child.get("parent_epoch") == p_epoch
        and child.get("epoch") == p_epoch + 1
    )
    checks["ancestor_v1_preserved"] = child.get("ancestor_v1_dataset_registry_hash") == parent.get(
        "ancestor_v1_dataset_registry_hash"
    )

    # no removals: every parent id present in child
    checks["no_parent_removed"] = set(p_by_id).issubset(set(c_by_id))
    # parent subset immutable: identity + partition + origin unchanged
    immutable = True
    for did, ps in p_by_id.items():
        cs = c_by_id.get(did)
        if cs is None:
            immutable = False
            break
        if any(str(ps[f]) != str(cs[f]) for f in _IDENTITY):
            immutable = False
            break
        if ps["partition"] != cs["partition"] or ps["origin_epoch"] != cs["origin_epoch"]:
            immutable = False
            break
    checks["parent_samples_immutable"] = immutable

    # growth only: the child-minus-parent set is entirely new v2-policy samples
    new_ids = set(c_by_id) - set(p_by_id)
    checks["growth_new_samples_only"] = all(
        c_by_id[i].get("assignment_source") == "v2-policy"
        and c_by_id[i].get("origin_epoch") == child.get("epoch")
        for i in new_ids
    )
    # no round_id reused across the added set vs parent (replacement guard)
    parent_rounds = {str(s["round_id"]) for s in parent["samples"]}
    checks["no_round_id_replacement"] = all(
        str(c_by_id[i]["round_id"]) not in parent_rounds for i in new_ids
    )
    checks["child_transition_count_zero"] = child.get("transition_count") == 0
    return checks


def verify_epoch_manifest(
    raw: dict[str, Any],
    *,
    v1_manifest: dict[str, Any] | None = None,
    parent_manifest: dict[str, Any] | None = None,
) -> EpochManifestVerification:
    """Verify a v2 epoch manifest object (already parsed from JSON).

    ``v1_manifest`` (epoch 1) enables the exact-inheritance proof; ``parent_manifest``
    (epoch ≥2) enables the grandfathering/immutability proof.
    """
    reasons: list[str] = []
    checks: dict[str, bool] = {}

    try:
        validate_against(MANIFEST_SCHEMA, raw)
        checks["schema_valid"] = True
    except Exception as exc:  # noqa: BLE001
        checks["schema_valid"] = False
        reasons.append(f"schema: {exc}")

    stated_hash = str(raw.get("manifest_hash", ""))
    recomputed = canonical_hash(_content_without_hash(raw))
    checks["manifest_hash_matches"] = bool(stated_hash) and stated_hash == recomputed
    checks["schema_version_bound"] = raw.get("schema_version") == MANIFEST_SCHEMA_VERSION
    checks["split_policy_hash_bound"] = raw.get("split_policy_hash") == split_policy_hash()

    samples = raw.get("samples", [])
    counts = raw.get("counts", {})
    per_chrom_raw = raw.get("per_chromosome", {})

    ids = [s.get("dataset_id") for s in samples]
    checks["dataset_ids_unique"] = len(set(ids)) == len(ids) and all(ids)
    round_ids = [s.get("round_id") for s in samples]
    checks["round_ids_unique"] = len(set(round_ids)) == len(round_ids) and all(round_ids)
    checks["partitions_valid"] = all(s.get("partition") in PARTITIONS for s in samples)
    checks["assignment_sources_valid"] = all(
        s.get("assignment_source") in ("v1-inherited", "v2-policy") for s in samples
    )

    actual = Counter(s.get("partition") for s in samples)
    checks["counts_match_samples"] = all(
        int(counts.get(p, -1)) == actual.get(p, 0) for p in PARTITIONS
    )
    checks["counts_sum_to_samples"] = sum(int(counts.get(p, 0)) for p in PARTITIONS) == len(samples)

    per_actual: dict[str, Counter[str]] = defaultdict(Counter)
    for s in samples:
        per_actual[str(s.get("chromosome"))][s.get("partition")] += 1
    per_ok = set(per_chrom_raw) == set(per_actual) and all(
        all(int(per_chrom_raw[c].get(p, -1)) == per_actual[c].get(p, 0) for p in PARTITIONS)
        for c in per_chrom_raw
    )
    checks["per_chromosome_matches_samples"] = per_ok
    checks["chromosomes_supported"] = set(per_actual).issubset(set(SUPPORTED_CHROMOSOMES))

    # growth-capable registry snapshot, recomputed from the identities.
    checks["registry_snapshot_hash_bound"] = raw.get("registry_snapshot_hash") == (
        registry_snapshot_hash(samples)
    )

    epoch = raw.get("epoch")
    parent = raw.get("parent_epoch")
    checks["epoch_parent_linear"] = isinstance(epoch, int) and (
        (epoch == 1 and parent is None) or (isinstance(parent, int) and parent == epoch - 1)
    )

    # ---- epoch 1: EXACT inheritance from v1 (no accepted sample moves) ----
    if epoch == 1:
        checks["epoch1_parent_fields_null"] = (
            raw.get("parent_epoch") is None
            and raw.get("parent_manifest_hash") is None
            and raw.get("parent_registry_snapshot_hash") is None
        )
        checks["epoch1_all_v1_inherited"] = all(
            s.get("assignment_source") == "v1-inherited" and s.get("origin_epoch") == 1
            for s in samples
        )
        if v1_manifest is not None:
            v1p = {str(s["dataset_id"]): s["partition"] for s in v1_manifest["samples"]}
            cur = {str(s["dataset_id"]): s["partition"] for s in samples}
            same_ids = set(v1p) == set(cur)
            moved = sum(1 for d in v1p if cur.get(d) != v1p[d]) if same_ids else len(v1p)
            checks["epoch1_inherits_v1_partitions_exactly"] = same_ids and moved == 0
            checks["epoch1_zero_transitions"] = (raw.get("transition_count") == 0) and moved == 0
            # no accepted test/validation sample left its cohort
            for part in ("test", "validation"):
                v1set = {d for d, p in v1p.items() if p == part}
                curset = {d for d, p in cur.items() if p == part}
                checks[f"epoch1_{part}_cohort_preserved"] = v1set <= curset
            checks["ancestor_v1_dataset_registry_hash_bound"] = raw.get(
                "ancestor_v1_dataset_registry_hash"
            ) == v1_manifest.get("dataset_registry_hash")

    # ---- epoch ≥2: grandfathered parent immutability ----
    if isinstance(epoch, int) and epoch >= 2 and parent_manifest is not None:
        checks.update(verify_epoch_against_parent(parent_manifest, raw))

    # no truth/mutation/scoring leakage anywhere in the canonical content.
    blob = json.dumps(raw, sort_keys=True).lower()
    checks["no_truth_or_mutation_fields"] = not any(tok in blob for tok in _FORBIDDEN_TOKENS)

    for name, ok in checks.items():
        if not ok:
            reasons.append(f"{name} failed")

    return EpochManifestVerification(
        ok=all(checks.values()),
        manifest_hash=recomputed,
        registry_snapshot_hash=str(raw.get("registry_snapshot_hash", "")),
        ancestor_v1_dataset_registry_hash=str(raw.get("ancestor_v1_dataset_registry_hash", "")),
        checks=checks,
        reasons=tuple(dict.fromkeys(reasons)),
    )
