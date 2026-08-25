"""The FROZEN chromosome-balanced TRAIN schedule.

Every phase draws its members from ONE deterministic schedule derived solely from the committed
split manifest. No database, no truth filesystem and no round directory is consulted: the
schedule is a property of committed bytes, so it is reproducible on any host and cannot drift
with the contents of a workspace.

Ten balanced batches are built by taking the j-th TRAIN member of each chromosome in the
manifest's own committed order. Every batch therefore holds exactly one member per chromosome,
which is what makes a partial phase still chromosome-balanced — and that is precisely the
property the worst-chromosome floor in :mod:`~minos_engine.baseline.objective` depends on. Racing
on an unbalanced prefix would bias the floor toward whichever chromosome happened to run first.

VALIDATION and TEST members are structurally excluded: they are filtered out before anything
else happens, and the schedule refuses to contain them.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from minos_engine.common.errors import MinosEngineError

__all__ = [
    "BATCH_COUNT",
    "CHROMOSOMES",
    "SPLIT_MANIFEST_PATH",
    "TRAIN_COUNT",
    "TRAIN_PER_CHROMOSOME",
    "ScheduleError",
    "TrainMember",
    "TrainSchedule",
    "build_train_schedule",
    "split_manifest_sha256",
]

#: the canonical chromosome order used inside every batch.
CHROMOSOMES: tuple[str, ...] = ("chr18", "chr19", "chr20", "chr21", "chr22")
TRAIN_PER_CHROMOSOME = 10
TRAIN_COUNT = TRAIN_PER_CHROMOSOME * len(CHROMOSOMES)
BATCH_COUNT = TRAIN_PER_CHROMOSOME

SPLIT_MANIFEST_PATH = "manifests/layer2_dataset_split_v2_epoch1.json"

_STRICT = ConfigDict(extra="forbid", frozen=True, strict=True)


class ScheduleError(MinosEngineError):
    """The committed split manifest does not yield the frozen TRAIN schedule."""


class TrainMember(BaseModel):
    """One TRAIN member. Identity only — no path, no truth digest, no score."""

    model_config = _STRICT

    dataset_id: str = Field(min_length=1)
    round_id: str = Field(min_length=1)
    chromosome: str = Field(min_length=1)


class TrainSchedule(BaseModel):
    """Ten chromosome-balanced batches of five members each."""

    model_config = _STRICT

    split_manifest_sha256: str = Field(min_length=64, max_length=64)
    batches: tuple[tuple[TrainMember, ...], ...]

    @property
    def members(self) -> tuple[TrainMember, ...]:
        return tuple(member for batch in self.batches for member in batch)

    def phase_members(self, batch_count: int) -> tuple[TrainMember, ...]:
        """The members of the first ``batch_count`` batches, in batch then chromosome order."""
        if not 1 <= batch_count <= BATCH_COUNT:
            raise ScheduleError(f"batch_count {batch_count} outside 1..{BATCH_COUNT}")
        return tuple(m for batch in self.batches[:batch_count] for m in batch)

    def required_pairs(self, batch_count: int) -> tuple[tuple[str, str], ...]:
        """``(dataset_id, chromosome)`` pairs, the required member set an aggregate is scored on."""
        return tuple((m.dataset_id, m.chromosome) for m in self.phase_members(batch_count))

    def content(self) -> dict[str, Any]:
        """Canonical, score-free schedule content for the committed manifest."""
        return {
            "schema_version": "l2f2-train-schedule-v1",
            "split_manifest": SPLIT_MANIFEST_PATH,
            "split_manifest_sha256": self.split_manifest_sha256,
            "chromosomes": list(CHROMOSOMES),
            "train_count": TRAIN_COUNT,
            "train_per_chromosome": TRAIN_PER_CHROMOSOME,
            "batch_count": BATCH_COUNT,
            "batch_size": len(CHROMOSOMES),
            "batches": [
                [
                    {
                        "chromosome": m.chromosome,
                        "dataset_id": m.dataset_id,
                        "round_id": m.round_id,
                    }
                    for m in batch
                ]
                for batch in self.batches
            ],
        }


def _repository_root() -> Path:
    from minos_engine.qualification.l2f_accepted_identities import repository_root

    return repository_root()


def split_manifest_sha256(root: Path | None = None) -> str:
    """The committed split manifest's byte identity, bound into the protocol."""
    path = (root or _repository_root()) / SPLIT_MANIFEST_PATH
    if not path.is_file():
        raise ScheduleError(f"committed split manifest is missing: {path}")
    return hashlib.sha256(path.read_bytes()).hexdigest()


def build_train_schedule(root: Path | None = None) -> TrainSchedule:
    """Build the frozen schedule from the committed split manifest. Fails closed on any drift."""
    base = root or _repository_root()
    path = base / SPLIT_MANIFEST_PATH
    if not path.is_file():
        raise ScheduleError(f"committed split manifest is missing: {path}")
    raw = path.read_bytes()
    try:
        document = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ScheduleError(f"split manifest is not valid JSON: {exc}") from exc

    samples = document.get("samples")
    if not isinstance(samples, list):
        raise ScheduleError("split manifest has no samples array")

    # VALIDATION and TEST are filtered out here, before anything else touches the data.
    train = [s for s in samples if s.get("partition") == "train"]
    if len(train) != TRAIN_COUNT:
        raise ScheduleError(f"expected {TRAIN_COUNT} TRAIN samples, found {len(train)}")

    per_chromosome: dict[str, list[TrainMember]] = {c: [] for c in CHROMOSOMES}
    for sample in train:  # the manifest's committed order IS the canonical ordering
        chromosome = sample.get("chromosome")
        if chromosome not in per_chromosome:
            raise ScheduleError(f"TRAIN sample on unexpected chromosome {chromosome!r}")
        per_chromosome[chromosome].append(
            TrainMember(
                dataset_id=str(sample["dataset_id"]),
                round_id=str(sample["round_id"]),
                chromosome=str(chromosome),
            )
        )

    for chromosome, members in per_chromosome.items():
        if len(members) != TRAIN_PER_CHROMOSOME:
            raise ScheduleError(
                f"{chromosome} has {len(members)} TRAIN members, expected {TRAIN_PER_CHROMOSOME}"
            )

    batches = tuple(
        tuple(per_chromosome[c][index] for c in CHROMOSOMES) for index in range(BATCH_COUNT)
    )
    schedule = TrainSchedule(split_manifest_sha256=hashlib.sha256(raw).hexdigest(), batches=batches)

    identifiers = [m.dataset_id for m in schedule.members]
    if len(set(identifiers)) != TRAIN_COUNT:
        raise ScheduleError("the TRAIN schedule contains a duplicate dataset_id")
    closed = {s["dataset_id"] for s in samples if s.get("partition") != "train"}
    if closed & set(identifiers):
        raise ScheduleError("a non-TRAIN dataset reached the TRAIN schedule")
    return schedule
