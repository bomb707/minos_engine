"""Deterministic, fail-closed discovery of the L2-C practice corpus.

Scans a dataset root for ``practice/round_<round_id>`` samples, confirms each BAM's
single contig via its ``@SQ`` header, cross-checks the ``@SQ`` length against the
reference FAI, and streams SHA-256 over BAM/BAI/reference/FAI (never loading a BAM
into memory). Truth and mutation files are **never** read, hashed, or referenced here.

Every failure is hard: missing sidecars/indexes, empty files, unsupported contigs,
multi-contig BAMs, ``@SQ``/FAI length mismatch, symlink escape, path traversal, files
changing size during hashing, wrong per-chromosome counts, or unexpected extra
samples. The result is exactly ``TOTAL_SAMPLES`` validated descriptors or an error.
"""

from __future__ import annotations

import gzip
import hashlib
import os
import struct
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

from .contracts import region_hash_for
from .policy import SAMPLES_PER_CHROMOSOME, SUPPORTED_CHROMOSOMES, TOTAL_SAMPLES

__all__ = ["RawSample", "DiscoveryError", "discover_corpus", "stream_sha256"]

_CHUNK = 1 << 20  # 1 MiB streaming reads
_PRACTICE = "practice"
_REFERENCE = "reference"
_ROUND_PREFIX = "round_"


class DiscoveryError(RuntimeError):
    """The corpus violates a fail-closed discovery invariant (hard failure)."""


@dataclass(frozen=True)
class RawSample:
    """A validated raw sample descriptor (identity + relative paths, no partition)."""

    round_id: str
    chromosome: str
    region_source: str
    region_start0: int
    region_end0_exclusive: int
    region_hash: str
    bam_sha256: str
    bai_sha256: str
    reference_sha256: str
    fai_sha256: str
    bam_size_bytes: int
    bam_relpath: str
    bai_relpath: str
    reference_relpath: str
    fai_relpath: str


def stream_sha256(path: Path) -> tuple[str, int]:
    """Return ``(sha256_hex, size_bytes)`` streamed in chunks; reject empty files.

    Re-stats the file size after hashing and fails if it changed during the read
    (a file mutated between discovery and hashing).
    """
    size_before = path.stat().st_size
    if size_before == 0:
        raise DiscoveryError(f"empty file: {path}")
    h = hashlib.sha256()
    read = 0
    with path.open("rb") as fh:
        while True:
            chunk = fh.read(_CHUNK)
            if not chunk:
                break
            read += len(chunk)
            h.update(chunk)
    size_after = path.stat().st_size
    if size_after != size_before or read != size_before:
        raise DiscoveryError(f"file changed during hashing: {path}")
    return h.hexdigest(), read


def _require_within(root: Path, target: Path) -> Path:
    """Resolve ``target`` and require it to stay within ``root`` (no symlink escape)."""
    resolved = target.resolve()
    root_resolved = root.resolve()
    if root_resolved != resolved and root_resolved not in resolved.parents:
        raise DiscoveryError(f"path escapes dataset root: {target}")
    return resolved


def _read_bam_references(bam_path: Path) -> list[tuple[str, int]]:
    """Return the BAM binary ``@SQ`` reference list ``[(name, length), ...]``.

    Reads only the BAM header (magic, header text, then the binary reference records)
    using the stdlib — a BAM is BGZF, which ``gzip`` decompresses transparently. No
    alignment records are read and no BAM-reader dependency (pysam) is imported, so
    Layer 2 stays within its architecture boundary. Streaming: decompression stops
    after the reference list, never loading alignments into memory.
    """
    with gzip.open(bam_path, "rb") as fh:
        magic = fh.read(4)
        if magic != b"BAM\x01":
            raise DiscoveryError(f"{bam_path}: not a BAM file (bad magic)")
        (l_text,) = struct.unpack("<i", _read_exact(fh, 4, bam_path))
        _read_exact(fh, l_text, bam_path)  # SAM header text (skipped; @SQ authoritative below)
        (n_ref,) = struct.unpack("<i", _read_exact(fh, 4, bam_path))
        if n_ref < 0:
            raise DiscoveryError(f"{bam_path}: invalid reference count")
        refs: list[tuple[str, int]] = []
        for _ in range(n_ref):
            (l_name,) = struct.unpack("<i", _read_exact(fh, 4, bam_path))
            name = _read_exact(fh, l_name, bam_path).rstrip(b"\x00").decode("ascii")
            (l_ref,) = struct.unpack("<i", _read_exact(fh, 4, bam_path))
            refs.append((name, l_ref))
    return refs


def _read_exact(fh: gzip.GzipFile, n: int, bam_path: Path) -> bytes:
    data = fh.read(n)
    if len(data) != n:
        raise DiscoveryError(f"{bam_path}: truncated BAM header")
    return data


def _confirm_contig(bam_path: Path) -> tuple[str, int]:
    """Return the BAM's single ``@SQ`` (contig, length); reject multi-contig BAMs.

    A BAM that cannot be opened/parsed (empty, truncated, corrupt) is a hard failure.
    """
    try:
        refs = _read_bam_references(bam_path)
    except (OSError, EOFError, struct.error, UnicodeDecodeError) as exc:
        raise DiscoveryError(f"{bam_path}: unreadable BAM header ({exc})") from exc
    if len(refs) != 1:
        raise DiscoveryError(f"{bam_path}: expected exactly one @SQ contig, found {len(refs)}")
    return refs[0][0], int(refs[0][1])


def _fai_length(fai_path: Path, contig: str) -> int:
    """Return the FAI-declared length of ``contig`` (first column match)."""
    with fai_path.open("r", encoding="utf-8") as fh:
        for line in fh:
            parts = line.split("\t")
            if len(parts) >= 2 and parts[0] == contig:
                return int(parts[1])
    raise DiscoveryError(f"{fai_path}: contig {contig} absent from FAI")


def discover_corpus(dataset_root: str | Path) -> list[RawSample]:
    """Discover and validate exactly :data:`TOTAL_SAMPLES` complete samples.

    Deterministic and independent of filesystem enumeration order (results are sorted
    by ``(chromosome, round_id)``). Fails closed on any violation.
    """
    root = Path(dataset_root).resolve()
    practice = root / _PRACTICE
    reference = root / _REFERENCE
    if not practice.is_dir():
        raise DiscoveryError(f"practice directory not found: {practice}")
    if not reference.is_dir():
        raise DiscoveryError(f"reference directory not found: {reference}")

    round_dirs = sorted(
        p for p in practice.iterdir() if p.is_dir() and p.name.startswith(_ROUND_PREFIX)
    )
    samples: list[RawSample] = []
    by_chrom: dict[str, list[str]] = defaultdict(list)

    for rd in round_dirs:
        round_id = rd.name[len(_ROUND_PREFIX) :]
        if not round_id:
            raise DiscoveryError(f"malformed round directory: {rd.name}")
        bam = rd / "input.bam"
        bai = rd / "input.bam.bai"
        for required in (bam, bai):
            if not required.is_file():
                raise DiscoveryError(f"{round_id}: missing required input {required.name}")
            _require_within(root, required)

        contig, sq_len = _confirm_contig(bam)
        if contig not in SUPPORTED_CHROMOSOMES:
            raise DiscoveryError(f"{round_id}: unsupported chromosome {contig!r}")

        ref = reference / contig / f"{contig}.fa"
        fai = reference / contig / f"{contig}.fa.fai"
        for required in (ref, fai):
            if not required.is_file():
                raise DiscoveryError(f"{round_id}: missing reference input {required}")
            _require_within(root, required)

        fai_len = _fai_length(fai, contig)
        if fai_len != sq_len:
            raise DiscoveryError(
                f"{round_id}: @SQ length {sq_len} != FAI length {fai_len} for {contig}"
            )
        start0, end0 = 0, sq_len
        if not start0 < end0:
            raise DiscoveryError(f"{round_id}: malformed region [{start0},{end0})")

        bam_sha, bam_size = stream_sha256(bam)
        bai_sha, _ = stream_sha256(bai)
        ref_sha, _ = stream_sha256(ref)
        fai_sha, _ = stream_sha256(fai)

        samples.append(
            RawSample(
                round_id=round_id,
                chromosome=contig,
                region_source=f"{contig}:{start0}-{end0}",
                region_start0=start0,
                region_end0_exclusive=end0,
                region_hash=region_hash_for(contig, start0, end0),
                bam_sha256=bam_sha,
                bai_sha256=bai_sha,
                reference_sha256=ref_sha,
                fai_sha256=fai_sha,
                bam_size_bytes=bam_size,
                bam_relpath=os.path.relpath(bam, root).replace(os.sep, "/"),
                bai_relpath=os.path.relpath(bai, root).replace(os.sep, "/"),
                reference_relpath=os.path.relpath(ref, root).replace(os.sep, "/"),
                fai_relpath=os.path.relpath(fai, root).replace(os.sep, "/"),
            )
        )
        by_chrom[contig].append(round_id)

    _validate_corpus_shape(by_chrom, samples)
    samples.sort(key=lambda s: (s.chromosome, s.round_id))
    return samples


def _validate_corpus_shape(by_chrom: dict[str, list[str]], samples: list[RawSample]) -> None:
    extra = sorted(set(by_chrom) - set(SUPPORTED_CHROMOSOMES))
    if extra:
        raise DiscoveryError(f"unexpected extra chromosomes present: {extra}")
    for contig in SUPPORTED_CHROMOSOMES:
        rids = by_chrom.get(contig, [])
        if len(rids) != SAMPLES_PER_CHROMOSOME:
            raise DiscoveryError(
                f"chromosome {contig} has {len(rids)} samples, expected {SAMPLES_PER_CHROMOSOME}"
            )
        if len(set(rids)) != len(rids):
            raise DiscoveryError(f"duplicate round_id within {contig}")
    if len(samples) != TOTAL_SAMPLES:
        raise DiscoveryError(f"expected {TOTAL_SAMPLES} samples, found {len(samples)}")
    # Duplicate complete identity tuples (bam,bai,reference,fai,region) are impossible
    # across distinct BAMs, but enforce it explicitly (fail closed).
    tuples = {
        (s.bam_sha256, s.bai_sha256, s.reference_sha256, s.fai_sha256, s.region_hash)
        for s in samples
    }
    if len(tuples) != len(samples):
        raise DiscoveryError("duplicate complete input identity tuple detected")
