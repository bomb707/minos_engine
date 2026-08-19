"""Intake producer for the L2-D input-integrity attestation (``attest-input``).

Runs where the genomic inputs live and emits the content-addressed
:class:`~minos_engine.layer2.ingest.contracts.InputIntegrityAttestation` — the statement
Layer 2 consumes so it never opens BAM/BAI/FASTA/FAI itself. Producer and consumer are
separate: intake imports the contract *from* Layer 2 (the consumer owns it) and never
writes ingestion rows.

Per attestation this module:
  1. stream-computes exact-byte SHA-256 for BAM, BAI, FASTA, and FAI;
  2. canonicalizes + hashes the region definition;
  3. recomputes the complete identity-tuple hash;
  4. matches every component against the selected registry-snapshot record (fail-closed
     :class:`AttestationMismatchError` on any difference);
  5. reads the BAM header ``@SQ`` entry (stdlib BGZF — no pysam) including the ``M5`` tag;
  6. computes the SAM-compatible MD5 of the normalized reference contig sequence
     (uppercase, whitespace removed);
  7. assigns ``MATCH`` / ``ABSENT`` / ``MISMATCH``;
  8. emits the deterministic attestation (timestamps/paths/URIs excluded from the hash).
"""

from __future__ import annotations

import gzip
import hashlib
import re
import struct
from pathlib import Path
from typing import Any

from minos_engine.common.errors import AttestationMismatchError, IngestionError
from minos_engine.common.hashing import canonical_hash
from minos_engine.layer2.ingest.contracts import InputIntegrityAttestation, M5Status
from minos_engine.layer2.split.contracts import region_hash_for
from minos_engine.layer2.split.discovery import stream_sha256

__all__ = [
    "ATTEST_GENERATOR",
    "ATTEST_GENERATOR_VERSION",
    "read_bam_sq_entry",
    "compute_reference_contig_m5",
    "attest_input",
]

ATTEST_GENERATOR = "minos-engine intake attest-input"
ATTEST_GENERATOR_VERSION = "l2d-attest-v1"

_M5_RE = re.compile(r"\bM5:([0-9a-fA-F]{32})\b")
_SN_RE = re.compile(r"\bSN:(\S+)")


def _read_exact(fh: gzip.GzipFile, n: int, path: Path) -> bytes:
    data = fh.read(n)
    if len(data) != n:
        raise IngestionError(f"{path}: truncated BAM header")
    return data


def read_bam_sq_entry(bam_path: Path) -> tuple[str, int, str | None]:
    """Return the single ``@SQ`` entry ``(contig, length, m5_or_none)`` of a BAM.

    Reads only the header via stdlib BGZF (no pysam, no alignment records). The contig
    name/length come from the authoritative binary reference records; the optional ``M5``
    tag is parsed from the SAM header text's matching ``@SQ`` line. Multi-contig BAMs are
    rejected (the corpus contract is single-contig practice rounds).
    """
    with gzip.open(bam_path, "rb") as fh:
        if fh.read(4) != b"BAM\x01":
            raise IngestionError(f"{bam_path}: not a BAM file (bad magic)")
        (l_text,) = struct.unpack("<i", _read_exact(fh, 4, bam_path))
        header_text = (
            _read_exact(fh, l_text, bam_path).rstrip(b"\x00").decode("utf-8", errors="replace")
        )
        (n_ref,) = struct.unpack("<i", _read_exact(fh, 4, bam_path))
        if n_ref != 1:
            raise IngestionError(f"{bam_path}: expected exactly one @SQ contig, found {n_ref}")
        (l_name,) = struct.unpack("<i", _read_exact(fh, 4, bam_path))
        name = _read_exact(fh, l_name, bam_path).rstrip(b"\x00").decode("ascii")
        (l_ref,) = struct.unpack("<i", _read_exact(fh, 4, bam_path))

    m5: str | None = None
    for line in header_text.splitlines():
        if not line.startswith("@SQ"):
            continue
        sn = _SN_RE.search(line)
        if sn is None or sn.group(1) != name:
            continue
        tag = _M5_RE.search(line)
        if tag is not None:
            m5 = tag.group(1).lower()
        break
    return name, l_ref, m5


def compute_reference_contig_m5(fasta_path: Path, contig: str) -> str:
    """SAM-compatible MD5 of the normalized reference contig sequence.

    Normalization per the SAM spec: the exact contig sequence with all whitespace
    removed, uppercased, hashed as bytes. Streams line-by-line; never loads the whole
    FASTA. Fails closed if the contig is absent or empty.
    """
    digest = hashlib.md5()  # noqa: S324 - SAM-spec M5 identity, not a security use
    in_target = False
    seen = False
    empty = True
    with fasta_path.open("rb") as fh:
        for raw in fh:
            line = raw.strip()
            if line.startswith(b">"):
                if in_target:
                    break  # end of target contig
                header_name = line[1:].split()[0].decode("ascii", errors="replace")
                in_target = header_name == contig
                seen = seen or in_target
                continue
            if in_target and line:
                digest.update(line.upper())
                empty = False
    if not seen:
        raise IngestionError(f"{fasta_path}: contig {contig!r} not present")
    if empty:
        raise IngestionError(f"{fasta_path}: contig {contig!r} has no sequence")
    return digest.hexdigest()


def attest_input(
    *,
    bam_path: Path,
    bai_path: Path,
    reference_path: Path,
    fai_path: Path,
    registry_record: dict[str, Any],
    registry_snapshot_hash: str,
) -> InputIntegrityAttestation:
    """Produce the attestation for one registered identity; fail closed on any mismatch.

    ``registry_record`` is the selected registry-snapshot record carrying the expected
    ``dataset_id``, ``round_id``, ``chromosome``, the four file hashes, the region bounds
    (``region_start0``/``region_end0_exclusive``), ``region_hash``, and
    ``identity_tuple_hash``. Every computed component must equal the record's value.
    """
    expected = registry_record
    computed = {
        "bam_sha256": stream_sha256(bam_path)[0],
        "bai_sha256": stream_sha256(bai_path)[0],
        "reference_sha256": stream_sha256(reference_path)[0],
        "fai_sha256": stream_sha256(fai_path)[0],
    }
    for key, value in computed.items():
        if value != str(expected[key]):
            raise AttestationMismatchError(f"{key} does not match the registered identity")

    contig, sq_length, bam_sq_m5 = read_bam_sq_entry(bam_path)
    if contig != str(expected["chromosome"]):
        raise AttestationMismatchError(
            f"BAM @SQ contig {contig!r} != registered {expected['chromosome']!r}"
        )

    region_hash = region_hash_for(
        contig, int(expected["region_start0"]), int(expected["region_end0_exclusive"])
    )
    if region_hash != str(expected["region_hash"]):
        raise AttestationMismatchError("region_hash does not match the registered region")
    if int(expected["region_end0_exclusive"]) - int(expected["region_start0"]) != sq_length:
        raise AttestationMismatchError("region length does not match the BAM @SQ length")

    identity_tuple_hash = canonical_hash(
        {
            "bam_sha256": computed["bam_sha256"],
            "bai_sha256": computed["bai_sha256"],
            "reference_sha256": computed["reference_sha256"],
            "fai_sha256": computed["fai_sha256"],
            "region_hash": region_hash,
        }
    )
    if identity_tuple_hash != str(expected["identity_tuple_hash"]):
        raise AttestationMismatchError("identity_tuple_hash does not match the registry")

    reference_m5 = compute_reference_contig_m5(reference_path, contig)
    status = (
        M5Status.ABSENT
        if bam_sq_m5 is None
        else (M5Status.MATCH if bam_sq_m5 == reference_m5 else M5Status.MISMATCH)
    )
    return InputIntegrityAttestation(
        generator=ATTEST_GENERATOR,
        generator_version=ATTEST_GENERATOR_VERSION,
        dataset_id=str(expected["dataset_id"]),
        round_id=str(expected["round_id"]),
        chromosome=contig,
        registry_snapshot_hash=registry_snapshot_hash,
        bam_sha256=computed["bam_sha256"],
        bai_sha256=computed["bai_sha256"],
        reference_sha256=computed["reference_sha256"],
        fai_sha256=computed["fai_sha256"],
        region_hash=region_hash,
        identity_tuple_hash=identity_tuple_hash,
        bam_sq_m5=bam_sq_m5,
        computed_reference_m5=reference_m5,
        m5_status=status,
    )
