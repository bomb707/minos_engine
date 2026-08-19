"""Deterministic, pure v2 epoch manifest generator (no database dependency).

An epoch manifest labels a growing set of registered identities with an immutable
partition per epoch:

  * **Epoch 1 is a pure inheritance** of the accepted v1 SPLIT-FROZEN per-sample
    partitions — the v2 salt is never applied to an accepted sample, so *zero*
    assignments change versus v1 (the test/validation cohorts are preserved exactly).
  * **Epoch N+1** grandfathers every prior assignment unchanged and assigns only the
    genuinely-new samples, in v2 hash order, filling toward the per-chromosome ratio
    targets. No prior sample ever moves; the test set is monotonic.

Each epoch carries its own growth-capable ``registry_snapshot_hash`` (over the full
identity set) plus the parent lineage (``parent_manifest_hash`` /
``parent_registry_snapshot_hash``) and the pinned ``ancestor_v1_dataset_registry_hash``.
The canonical manifest contains no paths, timestamps, or machine state, so regeneration
is byte-identical.

Database persistence of an epoch manifest lives in the ``storage`` package
(``storage.dataset_split_v2``) so this Layer 2 module stays free of SQLAlchemy/DB imports
(enforced by the architecture-boundary leakage test).
"""

from __future__ import annotations

from collections import Counter, defaultdict
from typing import Any

from minos_engine.common.errors import ContractValidationError
from minos_engine.common.hashing import canonical_hash

from .policy import (
    PARTITIONS,
    RATIO_BASIS,
    RATIO_BASIS_TOTAL,
    SALT,
    SPLIT_ALGORITHM,
    SPLIT_POLICY_VERSION,
    SUPPORTED_CHROMOSOMES,
    assign_epoch,
    split_policy_hash,
)

__all__ = [
    "MANIFEST_SCHEMA_VERSION",
    "IDENTITY_FIELDS",
    "registry_snapshot_hash",
    "epoch1_from_v1_manifest",
    "build_next_epoch_manifest",
]

MANIFEST_SCHEMA_VERSION = "layer2-dataset-split-v2"

#: The identity fields that key a sample to ``catalog.dataset_registry`` and define the
#: registry snapshot. ``identity_tuple_hash`` already binds region + artifact + parameter +
#: feature identity, so these four are a complete, self-contained snapshot key.
IDENTITY_FIELDS = ("dataset_id", "round_id", "chromosome", "identity_tuple_hash")


def registry_snapshot_hash(samples: list[dict[str, Any]]) -> str:
    """Growth-capable hash over an epoch's full identity set (order-independent)."""
    rows = sorted(
        ({f: str(s[f]) for f in IDENTITY_FIELDS} for s in samples),
        key=lambda r: r["dataset_id"],
    )
    return canonical_hash(rows)


def _identity(s: dict[str, Any]) -> dict[str, str]:
    return {f: str(s[f]) for f in IDENTITY_FIELDS}


def _assemble(
    *,
    epoch: int,
    parent_epoch: int | None,
    records: list[dict[str, Any]],
    ancestor_v1_dataset_registry_hash: str,
    parent_registry_snapshot_hash: str | None,
    parent_manifest_hash: str | None,
    transition_count: int,
) -> dict[str, Any]:
    """Assemble + hash the canonical epoch manifest from resolved per-sample records.

    Each record already carries ``dataset_id``, ``round_id``, ``chromosome``,
    ``identity_tuple_hash``, ``partition``, ``origin_epoch``, ``assignment_source``.
    """
    if epoch < 1:
        raise ContractValidationError("epoch must be >= 1")
    if (epoch == 1) != (parent_epoch is None):
        raise ContractValidationError("epoch 1 has no parent; later epochs require parent_epoch")
    if parent_epoch is not None and parent_epoch != epoch - 1:
        raise ContractValidationError("parent_epoch must equal epoch - 1")

    for r in records:
        if r["partition"] not in PARTITIONS:
            raise ContractValidationError(f"invalid partition {r['partition']!r}")
        if r["assignment_source"] not in ("v1-inherited", "v2-policy"):
            raise ContractValidationError(f"invalid assignment_source {r['assignment_source']!r}")
        if r["chromosome"] not in SUPPORTED_CHROMOSOMES:
            raise ContractValidationError(f"unsupported chromosome {r['chromosome']!r}")

    records = sorted(records, key=lambda r: str(r["dataset_id"]))
    counts = Counter(r["partition"] for r in records)
    per_chrom: dict[str, Counter[str]] = defaultdict(Counter)
    for r in records:
        per_chrom[r["chromosome"]][r["partition"]] += 1
    inherited = sum(1 for r in records if r["assignment_source"] == "v1-inherited")

    content: dict[str, Any] = {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "epoch": epoch,
        "parent_epoch": parent_epoch,
        "salt": SALT,
        "split_algorithm": SPLIT_ALGORITHM,
        "split_policy_version": SPLIT_POLICY_VERSION,
        "split_policy_hash": split_policy_hash(),
        "ratio_basis": dict(sorted(RATIO_BASIS.items())),
        "ratio_basis_total": RATIO_BASIS_TOTAL,
        "ancestor_v1_dataset_registry_hash": ancestor_v1_dataset_registry_hash,
        "registry_snapshot_hash": registry_snapshot_hash(records),
        "parent_registry_snapshot_hash": parent_registry_snapshot_hash,
        "parent_manifest_hash": parent_manifest_hash,
        "transition_count": transition_count,
        "inherited_count": inherited,
        "new_count": len(records) - inherited,
        "counts": {p: counts.get(p, 0) for p in PARTITIONS},
        "per_chromosome": {
            c: {p: per_chrom[c].get(p, 0) for p in PARTITIONS} for c in sorted(per_chrom)
        },
        "samples": [
            {
                "dataset_id": r["dataset_id"],
                "round_id": r["round_id"],
                "chromosome": r["chromosome"],
                "identity_tuple_hash": r["identity_tuple_hash"],
                "partition": r["partition"],
                "origin_epoch": r["origin_epoch"],
                "assignment_source": r["assignment_source"],
            }
            for r in records
        ],
    }
    manifest = dict(content)
    manifest["manifest_hash"] = canonical_hash(content)
    return manifest


def epoch1_from_v1_manifest(v1_manifest: dict[str, Any]) -> dict[str, Any]:
    """Build the v2 epoch-1 manifest by INHERITING the v1 partitions verbatim.

    Every accepted sample keeps the exact partition the accepted v1 SPLIT-FROZEN manifest
    assigned it (``origin_epoch = 1``, ``assignment_source = "v1-inherited"``). The v2 salt
    is not consulted for any epoch-1 sample, so ``transition_count`` is 0 by construction.
    """
    v1_samples = v1_manifest["samples"]
    seen_ids: set[str] = set()
    seen_rounds: set[str] = set()
    records: list[dict[str, Any]] = []
    for s in v1_samples:
        did = str(s["dataset_id"])
        rid = str(s["round_id"])
        if did in seen_ids or rid in seen_rounds:
            raise ContractValidationError(f"duplicate identity in v1 manifest: {did}")
        seen_ids.add(did)
        seen_rounds.add(rid)
        records.append(
            {
                **_identity(s),
                "partition": s["partition"],
                "origin_epoch": 1,
                "assignment_source": "v1-inherited",
            }
        )
    return _assemble(
        epoch=1,
        parent_epoch=None,
        records=records,
        ancestor_v1_dataset_registry_hash=str(v1_manifest["dataset_registry_hash"]),
        parent_registry_snapshot_hash=None,
        parent_manifest_hash=None,
        transition_count=0,
    )


def build_next_epoch_manifest(
    parent_manifest: dict[str, Any],
    new_samples: list[dict[str, Any]],
) -> dict[str, Any]:
    """Build epoch ``parent+1`` by grandfathering the parent and assigning only new samples.

    ``new_samples`` are identity rows (``IDENTITY_FIELDS``) that are NOT already present in
    the parent. Every parent sample is carried over with its partition/origin unchanged;
    the new samples are ordered by the v2 salt and filled toward the per-chromosome ratio
    targets. Fails closed on any parent sample missing, any new sample colliding with a
    parent identity, or any prior partition changing (``transition_count`` must be 0).
    """
    parent_samples = parent_manifest["samples"]
    parent_by_round = {str(s["round_id"]): s for s in parent_samples}
    parent_ids = {str(s["dataset_id"]) for s in parent_samples}

    # new samples must be genuinely new (no round_id or dataset_id collision with parent).
    for s in new_samples:
        rid, did = str(s["round_id"]), str(s["dataset_id"])
        if rid in parent_by_round or did in parent_ids:
            raise ContractValidationError(f"new sample {did} collides with a parent identity")

    prior_assignments = {rid: s["partition"] for rid, s in parent_by_round.items()}
    all_pairs = [(str(s["round_id"]), str(s["chromosome"])) for s in parent_samples] + [
        (str(s["round_id"]), str(s["chromosome"])) for s in new_samples
    ]
    assign = assign_epoch(prior_assignments, all_pairs)

    # prior partitions MUST be unchanged (grandfathering invariant).
    transitions = sum(1 for rid, part in prior_assignments.items() if assign.get(rid) != part)
    if transitions != 0:
        raise ContractValidationError("grandfathering violated: a prior partition changed")

    epoch = int(parent_manifest["epoch"]) + 1
    records: list[dict[str, Any]] = []
    for s in parent_samples:  # grandfathered, unchanged
        records.append(
            {
                **_identity(s),
                "partition": s["partition"],
                "origin_epoch": s["origin_epoch"],
                "assignment_source": s["assignment_source"],
            }
        )
    for s in new_samples:  # newly assigned by the v2 policy
        records.append(
            {
                **_identity(s),
                "partition": assign[str(s["round_id"])],
                "origin_epoch": epoch,
                "assignment_source": "v2-policy",
            }
        )
    return _assemble(
        epoch=epoch,
        parent_epoch=int(parent_manifest["epoch"]),
        records=records,
        ancestor_v1_dataset_registry_hash=str(parent_manifest["ancestor_v1_dataset_registry_hash"]),
        parent_registry_snapshot_hash=str(parent_manifest["registry_snapshot_hash"]),
        parent_manifest_hash=str(parent_manifest["manifest_hash"]),
        transition_count=0,
    )
