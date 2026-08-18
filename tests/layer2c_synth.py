"""Shared synthetic L2-C corpus/manifest builders for tests (no real BAMs).

Produces deterministic ``RawSample`` descriptors with fabricated but well-formed
SHA-256 identities so unit/integration/acceptance tests can exercise the split
policy, contracts, persistence, and gate without the external dataset.
"""

from __future__ import annotations

import hashlib

from minos_engine.layer2.split.contracts import region_hash_for
from minos_engine.layer2.split.discovery import RawSample
from minos_engine.layer2.split.generator import build_inventory, build_manifest
from minos_engine.layer2.split.policy import SAMPLES_PER_CHROMOSOME, SUPPORTED_CHROMOSOMES

_CONTIG_LEN = {
    "chr18": 80373285,
    "chr19": 58617616,
    "chr20": 64444167,
    "chr21": 46709983,
    "chr22": 50818468,
}


def _h(*parts: str) -> str:
    return hashlib.sha256(":".join(parts).encode()).hexdigest()


def synthetic_raw_samples(*, per_chrom: int = SAMPLES_PER_CHROMOSOME) -> list[RawSample]:
    """Deterministic synthetic raw samples: ``per_chrom`` unique rounds per chromosome."""
    out: list[RawSample] = []
    for ci, chrom in enumerate(SUPPORTED_CHROMOSOMES):
        length = _CONTIG_LEN[chrom]
        for i in range(per_chrom):
            rid = f"{ci:x}{i:015x}"  # 16 lowercase-hex chars, globally unique
            out.append(
                RawSample(
                    round_id=rid,
                    chromosome=chrom,
                    region_source=f"{chrom}:0-{length}",
                    region_start0=0,
                    region_end0_exclusive=length,
                    region_hash=region_hash_for(chrom, 0, length),
                    bam_sha256=_h("bam", rid),
                    bai_sha256=_h("bai", rid),
                    reference_sha256=_h("ref", chrom),
                    fai_sha256=_h("fai", chrom),
                    bam_size_bytes=1_000_000 + i,
                    bam_relpath=f"practice/round_{rid}/input.bam",
                    bai_relpath=f"practice/round_{rid}/input.bam.bai",
                    reference_relpath=f"reference/{chrom}/{chrom}.fa",
                    fai_relpath=f"reference/{chrom}/{chrom}.fa.fai",
                )
            )
    return out


def synthetic_manifest(*, per_chrom: int = SAMPLES_PER_CHROMOSOME):
    return build_manifest(synthetic_raw_samples(per_chrom=per_chrom))


def synthetic_inventory(*, per_chrom: int = SAMPLES_PER_CHROMOSOME):
    return build_inventory(synthetic_raw_samples(per_chrom=per_chrom))
