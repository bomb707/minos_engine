"""TRAIN-only truth identity registration.

Truth is registered by **content hash**, never by path. Paths are runtime provisioning detail:
storing them in the scientific ledger is exactly what produced the F7 stale-absolute-path problem
when the workspace moved. Nothing here writes a path into PostgreSQL.

Partition safety is enforced twice on purpose: this module refuses anything that is not TRAIN
before it touches the database, and migration 0009's ``SECURITY DEFINER`` function re-derives the
partition from ``catalog.split_allocations`` and refuses again. Operator discipline is not a
control.
"""

from __future__ import annotations

import hashlib
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from minos_engine.common.errors import MinosEngineError

__all__ = [
    "TRUTH_FILENAMES",
    "ForbiddenPartitionError",
    "TruthBundle",
    "TruthRegistrationError",
    "TruthRegistrationResult",
    "hash_truth_bundle",
    "register_train_truth_identities",
    "resolve_truth_bundle",
]

#: the exact four files a practice round provides. Never globbed — each is derived from round_id.
TRUTH_FILENAMES: tuple[str, ...] = (
    "truth.vcf.gz",
    "truth.vcf.gz.tbi",
    "mutations.vcf.gz",
    "mutations.vcf.gz.tbi",
)

_CHUNK = 1024 * 1024


class TruthRegistrationError(MinosEngineError):
    """Truth material is absent, unsafe or inconsistent with what is already registered."""


class ForbiddenPartitionError(TruthRegistrationError):
    """Registration was attempted for a partition L2-F2-A must not touch.

    VALIDATION stays closed until finalists, objective and ranking are frozen; TEST stays closed
    until L2-I. This is a typed refusal so a caller cannot proceed by ignoring a log line.
    """


@dataclass(frozen=True)
class TruthBundle:
    """One round's four truth/mutation files, hashed by content."""

    dataset_registry_id: str
    dataset_id: str
    round_id: str
    truth_vcf_sha256: str
    truth_tbi_sha256: str
    mutations_vcf_sha256: str
    mutations_tbi_sha256: str


@dataclass(frozen=True)
class TruthRegistrationResult:
    """What a registration run actually did — never a bare success flag."""

    requested: int
    created: int
    already_registered: int
    bundles: tuple[TruthBundle, ...]


def _stable_sha256(path: Path) -> str:
    """Hash a truth file, refusing symlinks and non-regular files.

    ``O_NOFOLLOW`` means a symlink planted at the expected name cannot redirect the read, and the
    size/inode re-check refuses a file swapped underneath us mid-hash.
    """
    if path.is_symlink():
        raise TruthRegistrationError(f"truth file {path} is a symlink")
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError as exc:
        raise TruthRegistrationError(f"truth file {path} does not exist") from exc
    except OSError as exc:
        raise TruthRegistrationError(f"truth file {path} is unreadable: {exc}") from exc
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode):
            raise TruthRegistrationError(f"truth file {path} is not a regular file")
        digest = hashlib.sha256()
        size = 0
        while chunk := os.read(fd, _CHUNK):
            digest.update(chunk)
            size += len(chunk)
        after = os.fstat(fd)
    finally:
        os.close(fd)
    if (after.st_size, after.st_ino) != (before.st_size, before.st_ino) or size != after.st_size:
        raise TruthRegistrationError(f"truth file {path} changed while it was being hashed")
    return digest.hexdigest()


def resolve_truth_bundle(dataset_root: Path, round_id: str) -> dict[str, Path]:
    """Derive the four exact paths for one round. Never globs, never discovers.

    The directory name comes from the REGISTERED ``round_id``, so an unregistered directory
    sitting in the corpus can never be picked up.
    """
    if not dataset_root.is_absolute():
        raise TruthRegistrationError(f"practice dataset root {dataset_root} must be absolute")
    if dataset_root.is_symlink() or not dataset_root.is_dir():
        raise TruthRegistrationError(
            f"practice dataset root {dataset_root} must be an existing non-symlink directory"
        )
    if not round_id or "/" in round_id or round_id.startswith("."):
        raise TruthRegistrationError(f"unsafe round_id {round_id!r}")
    directory = dataset_root / f"round_{round_id}"
    if directory.is_symlink() or not directory.is_dir():
        raise TruthRegistrationError(
            f"round directory {directory} must be an existing non-symlink directory"
        )
    return {name: directory / name for name in TRUTH_FILENAMES}


def hash_truth_bundle(
    *, dataset_registry_id: str, dataset_id: str, round_id: str, dataset_root: Path
) -> TruthBundle:
    """Resolve and content-hash one round's truth bundle."""
    paths = resolve_truth_bundle(dataset_root, round_id)
    return TruthBundle(
        dataset_registry_id=dataset_registry_id,
        dataset_id=dataset_id,
        round_id=round_id,
        truth_vcf_sha256=_stable_sha256(paths["truth.vcf.gz"]),
        truth_tbi_sha256=_stable_sha256(paths["truth.vcf.gz.tbi"]),
        mutations_vcf_sha256=_stable_sha256(paths["mutations.vcf.gz"]),
        mutations_tbi_sha256=_stable_sha256(paths["mutations.vcf.gz.tbi"]),
    )


def register_train_truth_identities(
    engine: Any, *, dataset_root: Path, expected_count: int | None = None
) -> TruthRegistrationResult:
    """Register every TRAIN round's truth identity. Idempotent; conflicting bytes fail closed.

    Only ``evaluation.l2f_train_truth_registration_targets`` is queried — a TRAIN-only projection
    in which validation and test are structurally absent, so this interface cannot enumerate them
    even if asked. Persistence goes through the ``SECURITY DEFINER`` function, which re-derives
    the partition itself.
    """
    from sqlalchemy import text

    with engine.connect() as conn:
        targets = (
            conn.execute(
                text(
                    "SELECT dataset_registry_id, dataset_id, round_id "
                    "FROM evaluation.l2f_train_truth_registration_targets ORDER BY dataset_id"
                )
            )
            .mappings()
            .all()
        )

    if expected_count is not None and len(targets) != expected_count:
        raise TruthRegistrationError(
            f"expected {expected_count} TRAIN registration targets, found {len(targets)}"
        )

    bundles = tuple(
        hash_truth_bundle(
            dataset_registry_id=str(row["dataset_registry_id"]),
            dataset_id=str(row["dataset_id"]),
            round_id=str(row["round_id"]),
            dataset_root=dataset_root,
        )
        for row in targets
    )

    created = 0
    with engine.connect() as conn, conn.begin():
        for bundle in bundles:
            row = conn.execute(
                text(
                    "SELECT created FROM evaluation.l2f_register_train_truth_identity("
                    ":d, :tv, :tt, :mv, :mt)"
                ),
                {
                    "d": bundle.dataset_registry_id,
                    "tv": bundle.truth_vcf_sha256,
                    "tt": bundle.truth_tbi_sha256,
                    "mv": bundle.mutations_vcf_sha256,
                    "mt": bundle.mutations_tbi_sha256,
                },
            ).one()
            created += 1 if row[0] else 0

    return TruthRegistrationResult(
        requested=len(bundles),
        created=created,
        already_registered=len(bundles) - created,
        bundles=bundles,
    )


def refuse_non_train_partition(partition: str) -> None:
    """The explicit source-side partition gate.

    L2-F2-A registers TRAIN only. VALIDATION and TEST are refused with a typed error rather than
    a warning, so a caller cannot proceed by ignoring output.
    """
    if partition != "train":
        raise ForbiddenPartitionError(
            f"L2-F2-A registers TRAIN truth only; partition {partition!r} is refused "
            "(validation opens after the objective is frozen; test stays locked until L2-I)"
        )
