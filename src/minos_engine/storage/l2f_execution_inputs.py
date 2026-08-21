"""L2-F F5 provisioned dataset resolver + streamed input identity verification.

Resolves the accepted BAM/BAI/FASTA/FAI/dict bytes for one accepted train member from a
PROVISIONED dataset root named by ``MINOS_L2F_DATASET_ROOT``. Production accepts no
caller-provided dataset path. The root must already exist and is NEVER created or repaired.

Every byte stream is hashed by STREAMING (BAM/FASTA are never loaded into memory), with a
size/inode re-check after hashing so a file mutated mid-read is rejected. Every resolved hash is
compared against the COMPLETE accepted ``catalog.dataset_registry`` + ``profiling.bam_profiles``
identity, and the member must be an exact accepted TRAIN plan member. No truth or mutation
directory is ever inspected.
"""

from __future__ import annotations

import hashlib
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy import Connection, text

from minos_engine.experiments.execution_contract import ExecutionInput, InputResolutionError

__all__ = [
    "ENV_DATASET_ROOT",
    "DatasetRoot",
    "ResolvedInputPaths",
    "dataset_root_from_env",
    "resolve_accepted_execution_input",
]

ENV_DATASET_ROOT = "MINOS_L2F_DATASET_ROOT"
_CHUNK = 1024 * 1024
_TRAIN = "train"


@dataclass(frozen=True)
class ResolvedInputPaths:
    """The five provisioned input paths for one member (all inside the resolved root)."""

    bam: Path
    bai: Path
    reference: Path
    fai: Path
    dictionary: Path


def _stream_sha256(path: Path) -> tuple[str, int]:
    """Stream-hash a regular file, rejecting symlinks and any change during the read."""
    if path.is_symlink():
        raise InputResolutionError(f"input {path} is a symlink")
    # O_NONBLOCK so a planted FIFO/device returns IMMEDIATELY instead of blocking this worker
    # forever before the regular-file check below can reject it; O_NOFOLLOW refuses a symlink at
    # the syscall level. On Linux both are no-ops for an ordinary regular file.
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
    except OSError as exc:
        raise InputResolutionError(f"input {path} is unreadable: {exc}") from exc
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode):
            raise InputResolutionError(f"input {path} is not a regular file")
        digest = hashlib.sha256()
        size = 0
        while True:
            chunk = os.read(fd, _CHUNK)
            if not chunk:
                break
            digest.update(chunk)
            size += len(chunk)
        after = os.fstat(fd)
    finally:
        os.close(fd)
    if (after.st_size, after.st_ino, after.st_dev) != (
        before.st_size,
        before.st_ino,
        before.st_dev,
    ):
        raise InputResolutionError(f"input {path} changed while it was being hashed")
    if size != after.st_size:
        raise InputResolutionError(f"input {path} size changed while it was being hashed")
    return digest.hexdigest(), size


@dataclass(frozen=True)
class DatasetRoot:
    """A provisioned, validated dataset root (never created or repaired here)."""

    root: Path

    @staticmethod
    def from_path(root: Path) -> DatasetRoot:
        if root.is_symlink():
            raise InputResolutionError(f"dataset root {root} is a symlink")
        if not root.is_dir():
            raise InputResolutionError(f"dataset root {root} is not an existing directory")
        return DatasetRoot(root=root.resolve(strict=True))

    def _inside(self, candidate: Path) -> Path:
        """Resolve ``candidate`` and require it to remain inside the root (no path escape)."""
        if candidate.is_symlink():
            raise InputResolutionError(f"input path {candidate} is a symlink")
        resolved = candidate.resolve()
        if resolved != self.root and self.root not in resolved.parents:
            raise InputResolutionError(f"input path {candidate} escapes the dataset root")
        return resolved

    def paths_for(self, *, round_id: str, chromosome: str) -> ResolvedInputPaths:
        """The established provisioned layout for one round + chromosome."""
        for token in (round_id, chromosome):
            if not token or "/" in token or "\\" in token or token in {".", ".."}:
                raise InputResolutionError(f"unsafe path token {token!r}")
        practice = self.root / "practice" / f"round_{round_id}"
        reference = self.root / "reference" / chromosome
        paths = ResolvedInputPaths(
            bam=practice / "input.bam",
            bai=practice / "input.bam.bai",
            reference=reference / f"{chromosome}.fa",
            fai=reference / f"{chromosome}.fa.fai",
            dictionary=reference / f"{chromosome}.dict",
        )
        for p in (paths.bam, paths.bai, paths.reference, paths.fai, paths.dictionary):
            if not p.exists():
                raise InputResolutionError(f"provisioned input {p} does not exist")
            self._inside(p)
        return paths


def dataset_root_from_env() -> DatasetRoot:
    raw = os.environ.get(ENV_DATASET_ROOT)
    if raw is None or not raw.strip():
        raise InputResolutionError(
            f"{ENV_DATASET_ROOT} is not set; the provisioned dataset root must be configured "
            "explicitly (production accepts no caller-provided dataset path)"
        )
    return DatasetRoot.from_path(Path(raw.strip()))


def _require_dictionary(path: Path, *, chromosome: str, reference_length: int | None) -> str:
    """Verify the ``.dict`` names the accepted chromosome (and length when known)."""
    sha, _size = _stream_sha256(path)
    try:
        text_body = path.read_text(encoding="utf-8", errors="strict")
    except (OSError, UnicodeDecodeError) as exc:
        raise InputResolutionError(f"sequence dictionary {path} is unreadable: {exc}") from exc
    seen = False
    for line in text_body.splitlines():
        if not line.startswith("@SQ"):
            continue
        fields = dict(part.split(":", 1) for part in line.split("\t")[1:] if ":" in part)
        if fields.get("SN") != chromosome:
            continue
        seen = True
        if reference_length is not None and int(fields.get("LN", -1)) != reference_length:
            raise InputResolutionError(
                f"sequence dictionary {path} declares LN {fields.get('LN')!r} for {chromosome}, "
                f"expected {reference_length}"
            )
    if not seen:
        raise InputResolutionError(
            f"sequence dictionary {path} does not declare the accepted chromosome {chromosome!r}"
        )
    return sha


def _resolve_one(conn: Connection, sql: str, params: dict[str, Any], what: str) -> dict[str, Any]:
    rows = conn.execute(text(sql), params).mappings().all()
    if len(rows) != 1:
        raise InputResolutionError(f"{what}: expected exactly 1 accepted row, found {len(rows)}")
    return dict(rows[0])


def resolve_accepted_execution_input(
    conn: Connection,
    *,
    plan_id: str,
    plan_member_id: str,
    root: DatasetRoot,
    reference_length: int | None = None,
) -> tuple[ExecutionInput, ResolvedInputPaths]:
    """Resolve + byte-verify the complete accepted input identity for one TRAIN plan member.

    Fails closed BEFORE any GATK process starts if anything is missing, ambiguous, substituted or
    changed. Never inspects a truth or mutation directory.
    """
    member = _resolve_one(
        conn,
        "SELECT pm.member_index, pm.partition, pm.feature_values_hash, "
        "       dr.dataset_id, dr.round_id, dr.chromosome, dr.region_hash, "
        "       dr.region_start0, dr.region_end0_exclusive, dr.bam_size_bytes, "
        "       dr.bam_sha256, dr.bai_sha256, dr.reference_sha256, dr.fai_sha256, "
        "       bp.profile_id, bp.content_hash "
        "  FROM experiments.l2f_experiment_plan_members pm "
        "  JOIN catalog.dataset_registry dr ON dr.id = pm.dataset_registry_id "
        "  JOIN profiling.bam_profiles bp ON bp.id = pm.bam_profile_id "
        " WHERE pm.plan_id = :p AND pm.id = :m",
        {"p": plan_id, "m": plan_member_id},
        "accepted plan member",
    )
    if member["partition"] != _TRAIN:
        raise InputResolutionError(
            f"plan member {plan_member_id} has partition {member['partition']!r}; only accepted "
            "TRAIN members may be executed"
        )

    paths = root.paths_for(round_id=str(member["round_id"]), chromosome=str(member["chromosome"]))
    bam_sha, bam_size = _stream_sha256(paths.bam)
    bai_sha, _ = _stream_sha256(paths.bai)
    ref_sha, _ = _stream_sha256(paths.reference)
    fai_sha, _ = _stream_sha256(paths.fai)
    dict_sha = _require_dictionary(
        paths.dictionary,
        chromosome=str(member["chromosome"]),
        reference_length=reference_length,
    )

    for label, actual, expected in (
        ("bam_sha256", bam_sha, member["bam_sha256"]),
        ("bai_sha256", bai_sha, member["bai_sha256"]),
        ("reference_sha256", ref_sha, member["reference_sha256"]),
        ("fai_sha256", fai_sha, member["fai_sha256"]),
    ):
        if actual != expected:
            raise InputResolutionError(
                f"{label} of the provisioned input does not match the accepted dataset identity "
                f"(got {actual}, accepted {expected})"
            )
    if bam_size != int(member["bam_size_bytes"]):
        raise InputResolutionError(
            f"provisioned BAM size {bam_size} != accepted {int(member['bam_size_bytes'])}"
        )

    inputs = ExecutionInput(
        dataset_id=str(member["dataset_id"]),
        round_id=str(member["round_id"]),
        chromosome=str(member["chromosome"]),
        profile_id=str(member["profile_id"]),
        content_hash=str(member["content_hash"]),
        feature_values_hash=str(member["feature_values_hash"]),
        bam_sha256=bam_sha,
        bai_sha256=bai_sha,
        reference_sha256=ref_sha,
        fai_sha256=fai_sha,
        dictionary_sha256=dict_sha,
        bam_size_bytes=bam_size,
        region_hash=str(member["region_hash"]),
        region_start0=int(member["region_start0"]),
        region_end0_exclusive=int(member["region_end0_exclusive"]),
    )
    return inputs, paths
