"""Deterministic L2-C manifest + local-inventory generator.

Given a dataset root, discover the 75-sample corpus, apply the fixed split policy per
confirmed chromosome, and assemble the canonical dataset-split manifest (no paths, no
truth/mutation, no timestamps) plus the noncanonical local input inventory (relative
paths). Byte-identical for identical inputs regardless of enumeration order, CWD,
dataset-root absolute path, locale, timezone, or Python hash randomization.
"""

from __future__ import annotations

from collections import defaultdict
from pathlib import Path

from minos_engine.callers.gatk.parameter_registry import REGISTRY
from minos_engine.layer2.feature_registry import REGISTRY_HASH

from .contracts import (
    DatasetSplitManifest,
    LocalInputEntry,
    LocalInputInventory,
    SampleIdentity,
)
from .discovery import RawSample, discover_corpus
from .policy import (
    PARTITION_LAYOUT,
    PARTITION_TOTALS,
    SALT,
    SPLIT_POLICY_VERSION,
    SUPPORTED_CHROMOSOMES,
    assign_partitions,
    split_policy_hash,
)

__all__ = [
    "parameter_space_hash",
    "dataset_id_for",
    "build_manifest",
    "build_inventory",
    "generate",
]

#: Fixed sentinel; ``retrieved_at`` never enters the parameter-space hash, so this is
#: purely deterministic (only the caller + documented ranges are hashed).
_EPOCH = "1970-01-01T00:00:00+00:00"


def parameter_space_hash() -> str:
    """Deterministic bound GATK parameter-space (documented ranges) hash."""
    return REGISTRY.documented_parameter_space(retrieved_at=_EPOCH).parameter_space_hash


def dataset_id_for(chromosome: str, round_id: str) -> str:
    """Stable, path-independent dataset identifier."""
    return f"minos-{chromosome}-{round_id}"


def _assign(samples: list[RawSample]) -> dict[str, tuple[str, int, str]]:
    """round_id -> (partition, sort_order, allocation_digest) via the fixed policy."""
    by_chrom: dict[str, list[str]] = defaultdict(list)
    for s in samples:
        by_chrom[s.chromosome].append(s.round_id)
    assignment: dict[str, tuple[str, int, str]] = {}
    for contig in SUPPORTED_CHROMOSOMES:
        for rid, partition, order, digest in assign_partitions(sorted(by_chrom[contig])):
            assignment[rid] = (partition, order, digest)
    return assignment


def build_manifest(samples: list[RawSample]) -> DatasetSplitManifest:
    """Assemble the canonical manifest from validated raw samples."""
    assignment = _assign(samples)
    psh = parameter_space_hash()
    identities: list[SampleIdentity] = []
    for s in samples:
        partition, order, digest = assignment[s.round_id]
        identities.append(
            SampleIdentity(
                dataset_id=dataset_id_for(s.chromosome, s.round_id),
                round_id=s.round_id,
                chromosome=s.chromosome,
                region_source=s.region_source,
                region_contig=s.chromosome,
                region_start0=s.region_start0,
                region_end0_exclusive=s.region_end0_exclusive,
                region_length_bp=s.region_end0_exclusive - s.region_start0,
                region_hash=s.region_hash,
                bam_sha256=s.bam_sha256,
                bai_sha256=s.bai_sha256,
                reference_sha256=s.reference_sha256,
                fai_sha256=s.fai_sha256,
                bam_size_bytes=s.bam_size_bytes,
                parameter_space_hash=psh,
                feature_registry_hash=REGISTRY_HASH,
                split_algorithm_version=SPLIT_POLICY_VERSION,
                split_salt=SALT,
                allocation_digest=digest,
                partition=partition,
                sort_order=order,
            )
        )

    per_chromosome: dict[str, dict[str, int]] = {
        c: {p: 0 for p, _ in PARTITION_LAYOUT} for c in SUPPORTED_CHROMOSOMES
    }
    for ident in identities:
        per_chromosome[ident.chromosome][ident.partition] += 1

    return DatasetSplitManifest(
        split_policy_hash=split_policy_hash(),
        counts=dict(PARTITION_TOTALS),
        per_chromosome=per_chromosome,
        parameter_space_hash=psh,
        feature_registry_hash=REGISTRY_HASH,
        samples=tuple(identities),
    )


def build_inventory(samples: list[RawSample]) -> LocalInputInventory:
    """Assemble the noncanonical local input inventory (relative paths)."""
    entries = tuple(
        LocalInputEntry(
            dataset_id=dataset_id_for(s.chromosome, s.round_id),
            round_id=s.round_id,
            chromosome=s.chromosome,
            bam_relpath=s.bam_relpath,
            bai_relpath=s.bai_relpath,
            reference_relpath=s.reference_relpath,
            fai_relpath=s.fai_relpath,
        )
        for s in samples
    )
    return LocalInputInventory(entries=entries)


def generate(dataset_root: str | Path) -> tuple[DatasetSplitManifest, LocalInputInventory]:
    """Discover the corpus and build the manifest + inventory (fail-closed)."""
    samples = discover_corpus(dataset_root)
    return build_manifest(samples), build_inventory(samples)
