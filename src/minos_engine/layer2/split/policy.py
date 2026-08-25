"""Fixed, frozen L2-C split policy — deterministic 50/10/15 hash-stratified split.

The split is a pure function of ``{SALT, round_id, chromosome}``. It never consults
truth, mutation labels, scores, Layer 1 profiles, model outcomes, filesystem order,
wall-clock, locale, timezone, or Python hash randomization. Changing :data:`SALT`
defines a *new* split identity and is an explicit design decision.

Algorithm (authoritative):
  * group complete samples by confirmed contig (each chromosome has exactly 15);
  * for each sample compute ``digest = sha256(f"{SALT}:{round_id}").hexdigest()``
    (lowercase hex);
  * order each chromosome's samples by ``digest`` using bytewise lowercase-hex
    ordering (ties broken by ``round_id`` for total determinism — digests are
    64-hex and collisions are cryptographically negligible);
  * first 10 → ``train``; next 2 → ``validation``; last 3 → ``test``.

Bytewise lowercase-hex ordering is identical to numeric ordering of the digest
value: every digest is exactly 64 lowercase-hex characters, and the ASCII order of
``0-9`` then ``a-f`` matches the numeric value of each nibble, so ``sorted`` on the
hex string reproduces ``sorted`` on ``int(digest, 16)`` (the form used in the design
policy, ``reports/LAYER2_DATASET_SPLIT_POLICY.md``).
"""

from __future__ import annotations

import hashlib

from minos_engine.common.hashing import canonical_hash

__all__ = [
    "SALT",
    "SPLIT_ALGORITHM",
    "SPLIT_POLICY_VERSION",
    "ROUND_ID_CONVENTION",
    "ORDERING_RULE",
    "REGION_RULE",
    "SUPPORTED_CHROMOSOMES",
    "SAMPLES_PER_CHROMOSOME",
    "PARTITIONS",
    "PARTITION_LAYOUT",
    "TOTAL_SAMPLES",
    "PARTITION_TOTALS",
    "allocation_digest",
    "assign_partitions",
    "split_policy",
    "split_policy_hash",
    "SplitPolicyError",
]

#: Fixed split salt. Changing this defines a NEW split identity (design decision only).
SALT: str = "minos-l2-split-v1"
SPLIT_ALGORITHM: str = "hash-stratified"
SPLIT_POLICY_VERSION: str = "layer2-dataset-split-v1"

#: ``round_id`` is the bare lowercase-hex directory suffix (``round_<round_id>``),
#: matching the Layer 1 ``ProfileRequest.round_id`` convention across the engine.
ROUND_ID_CONVENTION: str = "bare_hex_directory_suffix"
ORDERING_RULE: str = "bytewise_lowercase_hex_of_sha256(salt:round_id)"
#: The normalized region is the full confirmed contig ``[0, contig_length)`` in
#: zero-based half-open coordinates (derived from the BAM @SQ length, cross-checked
#: to the reference FAI). It is independent of BAM read content.
REGION_RULE: str = "full_contig_zero_based_half_open"

SUPPORTED_CHROMOSOMES: tuple[str, ...] = ("chr18", "chr19", "chr20", "chr21", "chr22")
SAMPLES_PER_CHROMOSOME: int = 15
PARTITIONS: tuple[str, ...] = ("train", "validation", "test")

#: (partition, count) per chromosome, applied in this exact order to the sorted list.
PARTITION_LAYOUT: tuple[tuple[str, int], ...] = (
    ("train", 10),
    ("validation", 2),
    ("test", 3),
)

TOTAL_SAMPLES: int = len(SUPPORTED_CHROMOSOMES) * SAMPLES_PER_CHROMOSOME  # 75
PARTITION_TOTALS: dict[str, int] = {
    name: count * len(SUPPORTED_CHROMOSOMES) for name, count in PARTITION_LAYOUT
}  # train:50, validation:10, test:15


class SplitPolicyError(ValueError):
    """A chromosome group violates the fixed split policy (hard failure)."""


def allocation_digest(round_id: str) -> str:
    """Return ``sha256(f"{SALT}:{round_id}")`` as lowercase hex (the ordering key)."""
    if not round_id:
        raise SplitPolicyError("round_id must be non-empty")
    return hashlib.sha256(f"{SALT}:{round_id}".encode()).hexdigest()


def assign_partitions(round_ids: list[str]) -> list[tuple[str, str, int, str]]:
    """Assign partitions to one chromosome's samples.

    ``round_ids`` must be exactly :data:`SAMPLES_PER_CHROMOSOME` unique ids. Returns a
    list of ``(round_id, partition, sort_order, allocation_digest)`` ordered by
    ``sort_order`` (0-based position within the chromosome after hash ordering). Fails
    closed on the wrong count or duplicates — never searches for a favorable split.
    """
    if len(round_ids) != SAMPLES_PER_CHROMOSOME:
        raise SplitPolicyError(
            f"expected exactly {SAMPLES_PER_CHROMOSOME} samples, got {len(round_ids)}"
        )
    if len(set(round_ids)) != len(round_ids):
        raise SplitPolicyError("duplicate round_id within a chromosome group")

    digests = {rid: allocation_digest(rid) for rid in round_ids}
    # bytewise lowercase-hex ordering; secondary key round_id for total determinism.
    ordered = sorted(round_ids, key=lambda r: (digests[r], r))

    out: list[tuple[str, str, int, str]] = []
    index = 0
    for partition, count in PARTITION_LAYOUT:
        for _ in range(count):
            rid = ordered[index]
            out.append((rid, partition, index, digests[rid]))
            index += 1
    return out


def split_policy() -> dict[str, object]:
    """Canonical, deterministic description of the fixed split policy (hash source)."""
    return {
        "schema_version": SPLIT_POLICY_VERSION,
        "split_algorithm": SPLIT_ALGORITHM,
        "salt": SALT,
        "round_id_convention": ROUND_ID_CONVENTION,
        "ordering_rule": ORDERING_RULE,
        "region_rule": REGION_RULE,
        "supported_chromosomes": list(SUPPORTED_CHROMOSOMES),
        "samples_per_chromosome": SAMPLES_PER_CHROMOSOME,
        "partitions": list(PARTITIONS),
        "partition_layout": [{"partition": p, "count": c} for p, c in PARTITION_LAYOUT],
        "total_samples": TOTAL_SAMPLES,
        "partition_totals": dict(sorted(PARTITION_TOTALS.items())),
        "leakage_controls": {
            "assigned_only_after_contig_confirmed": True,
            "independent_of_truth_mutation_scores": True,
            "independent_of_filesystem_order": True,
            "split_by_complete_sample_only": True,
        },
    }


def split_policy_hash() -> str:
    """SHA-256 over the canonical :func:`split_policy` description."""
    return canonical_hash(split_policy())
