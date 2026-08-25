"""Fixed, frozen L2-C v2 split policy — growth-stable, stratified, epoched.

The split is a pure function of ``{SALT, round_id, chromosome, epoch membership}``. It
never consults truth, mutation labels, scores, Layer 1 profiles, model outcomes,
filesystem order, wall-clock, locale, timezone, or Python hash randomization. Changing
:data:`SALT` or the ratio basis defines a *new* split identity and is an explicit owner
decision.

Algorithm (authoritative), applied independently per confirmed contig:
  * per-chromosome target counts come from the fixed ratio basis (10 train / 2 validation
    / 3 test per 15 = 66.67 / 13.33 / 20 %) scaled to the chromosome's sample count with
    **largest-remainder rounding**, so counts always sum exactly to the chromosome total;
  * within a chromosome, samples are ordered by ``digest = sha256(f"{SALT}:{round_id}")``
    (lowercase hex, bytewise order; ties broken by ``round_id`` for total determinism);
  * **grandfathering**: on a new epoch every already-assigned sample keeps its partition
    unchanged; only *new* samples are assigned, in hash order, each filling the partition
    with the largest remaining deficit ``target - current`` (ties broken train → validation
    → test). This preserves the test-set monotonic property (once ``test``, always ``test``)
    and never moves an existing sample as the corpus grows.

At epoch 1 with the accepted 75 samples (15 per chromosome) this yields exactly 10/2/3
per chromosome = 50 train / 10 validation / 15 test.
"""

from __future__ import annotations

import hashlib
from collections import Counter, defaultdict

from minos_engine.common.errors import MinosEngineError
from minos_engine.common.hashing import canonical_hash

__all__ = [
    "SALT",
    "SPLIT_ALGORITHM",
    "SPLIT_POLICY_VERSION",
    "ROUND_ID_CONVENTION",
    "ORDERING_RULE",
    "GRANDFATHER_RULE",
    "EPOCH1_RULE",
    "REGION_RULE",
    "SUPPORTED_CHROMOSOMES",
    "PARTITIONS",
    "RATIO_BASIS",
    "RATIO_BASIS_TOTAL",
    "allocation_digest",
    "partition_targets",
    "assign_epoch",
    "split_policy",
    "split_policy_hash",
    "SplitPolicyError",
]

#: Fixed v2 split salt. Changing this defines a NEW split identity (design decision only).
SALT: str = "minos-l2-split-v2"
SPLIT_ALGORITHM: str = "hash-ordered-ratio-filled-grandfathered"
SPLIT_POLICY_VERSION: str = "layer2-dataset-split-v2"

ROUND_ID_CONVENTION: str = "bare_hex_directory_suffix"
ORDERING_RULE: str = "bytewise_lowercase_hex_of_sha256(salt:round_id)"
GRANDFATHER_RULE: str = "existing_assignments_immutable_across_epochs; new_fill_largest_deficit"
REGION_RULE: str = "full_contig_zero_based_half_open"
#: Epoch 1 is a *pure inheritance* of the accepted v1 SPLIT-FROZEN per-sample partitions.
#: The v2 salt/ordering is NEVER applied to an accepted sample — it orders only genuinely
#: new samples from epoch 2 onward. This is the invariant that keeps the test set monotonic
#: (an accepted test/validation sample can never move) as the corpus grows.
EPOCH1_RULE: str = "inherit_v1_partitions_verbatim; v2_salt_orders_new_samples_only_from_epoch2"

SUPPORTED_CHROMOSOMES: tuple[str, ...] = ("chr18", "chr19", "chr20", "chr21", "chr22")
PARTITIONS: tuple[str, ...] = ("train", "validation", "test")

#: Exact integer ratio basis: per 15 samples → 10 train / 2 validation / 3 test.
#: (66.67 / 13.33 / 20 %). Represented as integers so the policy hash is exact/rational.
RATIO_BASIS: dict[str, int] = {"train": 10, "validation": 2, "test": 3}
RATIO_BASIS_TOTAL: int = 15


class SplitPolicyError(MinosEngineError):
    """A sample set violates the fixed v2 split policy (hard failure, fail closed)."""


def allocation_digest(round_id: str) -> str:
    """Return ``sha256(f"{SALT}:{round_id}")`` as lowercase hex (the ordering key)."""
    if not round_id:
        raise SplitPolicyError("round_id must be non-empty")
    return hashlib.sha256(f"{SALT}:{round_id}".encode()).hexdigest()


def _order_key(round_id: str) -> tuple[str, str]:
    return (allocation_digest(round_id), round_id)


def partition_targets(n: int) -> dict[str, int]:
    """Per-chromosome target counts for ``n`` samples via largest-remainder rounding.

    Counts always sum exactly to ``n``. For ``n = 15`` this is exactly ``10/2/3``.
    """
    if n < 0:
        raise SplitPolicyError("sample count must be non-negative")
    raw = {p: n * RATIO_BASIS[p] / RATIO_BASIS_TOTAL for p in PARTITIONS}
    floors = {p: int(raw[p]) for p in PARTITIONS}
    remainder = n - sum(floors.values())
    # distribute the remainder to the largest fractional parts; ties by PARTITIONS order.
    order = sorted(PARTITIONS, key=lambda p: (-(raw[p] - int(raw[p])), PARTITIONS.index(p)))
    for p in order[:remainder]:
        floors[p] += 1
    return floors


def assign_epoch(prior: dict[str, str], samples: list[tuple[str, str]]) -> dict[str, str]:
    """Assign partitions for one epoch, grandfathering ``prior`` unchanged.

    ``samples`` is the epoch's complete ``(round_id, chromosome)`` set and must be a
    superset of ``prior`` (growth is additive; nothing is ever removed or re-labelled).
    Returns ``{round_id: partition}`` for the whole epoch. Fails closed on unsupported
    chromosomes, duplicate round ids, an invalid prior partition, or a prior sample that
    is missing from ``samples`` (which would be a non-additive change).
    """
    round_ids = [rid for rid, _ in samples]
    if len(set(round_ids)) != len(round_ids):
        raise SplitPolicyError("duplicate round_id in epoch sample set")
    sample_ids = set(round_ids)
    for rid, part in prior.items():
        if part not in PARTITIONS:
            raise SplitPolicyError(f"invalid prior partition {part!r} for {rid}")
        if rid not in sample_ids:
            raise SplitPolicyError(f"prior sample {rid} absent from epoch (non-additive)")

    by_chrom: dict[str, list[str]] = defaultdict(list)
    for rid, chrom in samples:
        if chrom not in SUPPORTED_CHROMOSOMES:
            raise SplitPolicyError(f"unsupported chromosome {chrom!r}")
        by_chrom[chrom].append(rid)

    result: dict[str, str] = dict(prior)
    for chrom in SUPPORTED_CHROMOSOMES:
        rids = by_chrom.get(chrom, [])
        if not rids:
            continue
        targets = partition_targets(len(rids))
        counts: Counter[str] = Counter(result[r] for r in rids if r in result)
        new_ids = sorted((r for r in rids if r not in result), key=_order_key)
        for rid in new_ids:
            part = max(
                PARTITIONS,
                key=lambda p: (targets[p] - counts[p], -PARTITIONS.index(p)),
            )
            result[rid] = part
            counts[part] += 1
    return result


def split_policy() -> dict[str, object]:
    """Canonical, deterministic description of the fixed v2 split policy (hash source)."""
    return {
        "schema_version": SPLIT_POLICY_VERSION,
        "split_algorithm": SPLIT_ALGORITHM,
        "salt": SALT,
        "round_id_convention": ROUND_ID_CONVENTION,
        "ordering_rule": ORDERING_RULE,
        "grandfather_rule": GRANDFATHER_RULE,
        "epoch1_rule": EPOCH1_RULE,
        "region_rule": REGION_RULE,
        "supported_chromosomes": list(SUPPORTED_CHROMOSOMES),
        "partitions": list(PARTITIONS),
        "ratio_basis": dict(sorted(RATIO_BASIS.items())),
        "ratio_basis_total": RATIO_BASIS_TOTAL,
        "rounding_rule": "largest_remainder",
        "leakage_controls": {
            "assigned_only_after_contig_confirmed": True,
            "independent_of_truth_mutation_scores": True,
            "independent_of_filesystem_order": True,
            "epoch1_inherits_v1_partitions_verbatim": True,
            "existing_assignments_immutable_across_epochs": True,
            "v2_salt_orders_new_samples_only": True,
            "test_set_monotonic": True,
        },
    }


def split_policy_hash() -> str:
    """SHA-256 over the canonical :func:`split_policy` description."""
    return canonical_hash(split_policy())
