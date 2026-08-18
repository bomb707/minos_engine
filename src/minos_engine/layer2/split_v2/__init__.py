"""L2-C SPLIT-FROZEN v2 — growth-stable, stratified, epoched dataset split.

Supersedes the v1 fixed-count split (``layer2/split``, kept frozen as historical epoch
1 / policy v1). v2 assigns each sample a partition by a **hash-ordered, ratio-filled,
grandfathered** rule that keeps exact per-chromosome ratios (10/2/3 at 15 samples) while
guaranteeing that, as the corpus grows in periodic batches, **no already-assigned sample
ever changes partition** and the test set is monotonic (once test, always test).

Growth is modelled as immutable **epochs**: each epoch is a frozen snapshot that is a
superset of the previous one; new samples are assigned to fill toward the per-chromosome
target while every prior assignment is grandfathered unchanged.
"""

from __future__ import annotations

from .policy import (
    PARTITIONS,
    RATIO_BASIS,
    RATIO_BASIS_TOTAL,
    SALT,
    SPLIT_ALGORITHM,
    SPLIT_POLICY_VERSION,
    SUPPORTED_CHROMOSOMES,
    SplitPolicyError,
    allocation_digest,
    assign_epoch,
    partition_targets,
    split_policy,
    split_policy_hash,
)

__all__ = [
    "SALT",
    "SPLIT_ALGORITHM",
    "SPLIT_POLICY_VERSION",
    "SUPPORTED_CHROMOSOMES",
    "PARTITIONS",
    "RATIO_BASIS",
    "RATIO_BASIS_TOTAL",
    "SplitPolicyError",
    "allocation_digest",
    "partition_targets",
    "assign_epoch",
    "split_policy",
    "split_policy_hash",
]
