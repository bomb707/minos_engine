"""Intake producer: stdlib BGZF @SQ+M5 reading, SAM M5 normalization, fail-closed attest."""

from __future__ import annotations

import gzip
import hashlib
import struct
from pathlib import Path

import pytest

from minos_engine.common.errors import AttestationMismatchError, IngestionError
from minos_engine.common.hashing import canonical_hash
from minos_engine.intake.attestation import (
    attest_input,
    compute_reference_contig_m5,
    read_bam_sq_entry,
)
from minos_engine.layer2.ingest.contracts import M5Status
from minos_engine.layer2.split.contracts import region_hash_for

_SEQ = "ACGT" * 25  # 100 bp
_M5 = hashlib.md5(_SEQ.encode()).hexdigest()


def _write_bam(
    path: Path, *, contig: str = "chr18", length: int = 100, m5: str | None = _M5, n_ref: int = 1
) -> None:
    tag = f"\tM5:{m5}" if m5 else ""
    text = f"@HD\tVN:1.6\n@SQ\tSN:{contig}\tLN:{length}{tag}\n".encode()
    body = b"BAM\x01" + struct.pack("<i", len(text)) + text + struct.pack("<i", n_ref)
    for _ in range(n_ref):
        name = contig.encode() + b"\x00"
        body += struct.pack("<i", len(name)) + name + struct.pack("<i", length)
    with gzip.open(path, "wb") as fh:
        fh.write(body)


def _write_fasta(path: Path, *, contig: str = "chr18", lowercase: bool = False) -> None:
    seq = _SEQ.lower() if lowercase else _SEQ
    path.write_text(f">{contig} desc\n{seq[:50]}\n{seq[50:]}\n", encoding="utf-8")


def _record(tmp: Path) -> dict:
    def sha(p: Path) -> str:
        return hashlib.sha256(p.read_bytes()).hexdigest()

    rh = region_hash_for("chr18", 0, 100)
    files = {
        k: sha(tmp / n)
        for k, n in (
            ("bam_sha256", "input.bam"),
            ("bai_sha256", "input.bam.bai"),
            ("reference_sha256", "chr18.fa"),
            ("fai_sha256", "chr18.fa.fai"),
        )
    }
    return {
        "dataset_id": "minos-chr18-00000000000000aa",
        "round_id": "00000000000000aa",
        "chromosome": "chr18",
        **files,
        "region_start0": 0,
        "region_end0_exclusive": 100,
        "region_hash": rh,
        "identity_tuple_hash": canonical_hash({**files, "region_hash": rh}),
    }


@pytest.fixture()
def corpus(tmp_path: Path) -> Path:
    _write_bam(tmp_path / "input.bam")
    (tmp_path / "input.bam.bai").write_bytes(b"BAI\x01fake")
    _write_fasta(tmp_path / "chr18.fa")
    (tmp_path / "chr18.fa.fai").write_text("chr18\t100\t7\t50\t51\n")
    return tmp_path


def _attest(tmp: Path):
    return attest_input(
        bam_path=tmp / "input.bam",
        bai_path=tmp / "input.bam.bai",
        reference_path=tmp / "chr18.fa",
        fai_path=tmp / "chr18.fa.fai",
        registry_record=_record(tmp),
        registry_snapshot_hash="a" * 64,
    )


def test_match_attestation(corpus: Path) -> None:
    a = _attest(corpus)
    assert a.m5_status is M5Status.MATCH and a.bam_sq_m5 == _M5


def test_absent_when_no_m5_tag(corpus: Path) -> None:
    _write_bam(corpus / "input.bam", m5=None)
    assert _attest(corpus).m5_status is M5Status.ABSENT


def test_mismatch_when_m5_differs(corpus: Path) -> None:
    _write_bam(corpus / "input.bam", m5="0" * 32)
    assert _attest(corpus).m5_status is M5Status.MISMATCH


def test_sam_m5_normalization_case_insensitive(corpus: Path) -> None:
    # lowercase FASTA sequence hashes identically (SAM spec uppercases).
    _write_fasta(corpus / "chr18.fa", lowercase=True)
    assert compute_reference_contig_m5(corpus / "chr18.fa", "chr18") == _M5


def test_multi_contig_bam_rejected(corpus: Path) -> None:
    _write_bam(corpus / "input.bam", n_ref=2)
    with pytest.raises(IngestionError):
        read_bam_sq_entry(corpus / "input.bam")


def test_hash_mismatch_fails_closed(corpus: Path) -> None:
    record = _record(corpus)
    record["bam_sha256"] = "0" * 64
    with pytest.raises(AttestationMismatchError):
        attest_input(
            bam_path=corpus / "input.bam",
            bai_path=corpus / "input.bam.bai",
            reference_path=corpus / "chr18.fa",
            fai_path=corpus / "chr18.fa.fai",
            registry_record=record,
            registry_snapshot_hash="a" * 64,
        )


def test_missing_contig_fails_closed(corpus: Path) -> None:
    with pytest.raises(IngestionError):
        compute_reference_contig_m5(corpus / "chr18.fa", "chr19")
