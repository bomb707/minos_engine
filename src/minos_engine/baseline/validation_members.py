"""THE frozen ten-member VALIDATION schedule.

The TRAIN schedule (``schedule.py``) filters the split manifest down to its fifty TRAIN samples and
refuses anything else. This is its VALIDATION counterpart, built the same way from the same frozen
manifest, and it is deliberately a separate module rather than a parameter on the TRAIN one: a
function that returns TRAIN or VALIDATION depending on an argument is a function that can be handed
the wrong argument.

Two properties matter more than convenience:

* **Truth-free.** The split manifest records identity only — ``dataset_id``, ``chromosome``,
  ``round_id``, ``identity_tuple_hash``. There is no truth path and no truth digest anywhere in
  it, so constructing this authority cannot read, resolve or hash validation truth. That is not a
  discipline the caller has to observe; it is what the manifest contains.
* **TEST cannot arrive here.** The fifteen TEST samples are filtered out before anything else
  touches the data, and a closing assertion re-checks that no non-VALIDATION dataset id survived.
  TEST stays sealed until L2-I.

The ten members are chromosome-balanced two apiece, and their order is the manifest's committed
order — the same canonical ordering the TRAIN schedule uses, so neither depends on dictionary
iteration or on when a file was read.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from minos_engine.baseline.schedule import (
    CHROMOSOMES,
    SPLIT_MANIFEST_PATH,
    ScheduleError,
    split_manifest_sha256,
)

__all__ = [
    "VALIDATION_COUNT",
    "VALIDATION_PARTITION",
    "VALIDATION_PER_CHROMOSOME",
    "ValidationMember",
    "ValidationSchedule",
    "build_validation_schedule",
]

VALIDATION_PARTITION = "validation"
VALIDATION_PER_CHROMOSOME = 2
VALIDATION_COUNT = VALIDATION_PER_CHROMOSOME * len(CHROMOSOMES)  # 10

_TRAIN_PARTITION = "train"
_TEST_PARTITION = "test"


@dataclass(frozen=True, slots=True)
class ValidationMember:
    """One VALIDATION member. Identity only — no path, no truth digest, no score."""

    dataset_id: str
    chromosome: str
    round_id: str
    identity_tuple_hash: str
    member_index: int


@dataclass(frozen=True, slots=True)
class ValidationSchedule:
    """The ten VALIDATION members in the manifest's committed order."""

    members: tuple[ValidationMember, ...]
    split_manifest_sha256: str

    def __post_init__(self) -> None:
        if len(self.members) != VALIDATION_COUNT:  # pragma: no cover - built verified
            raise ScheduleError("a verified VALIDATION schedule always holds ten members")

    @property
    def dataset_ids(self) -> tuple[str, ...]:
        return tuple(m.dataset_id for m in self.members)

    @property
    def member_chromosomes(self) -> tuple[str, ...]:
        return tuple(m.chromosome for m in self.members)

    def required_pairs(self) -> tuple[tuple[str, str], ...]:
        """``(dataset_id, chromosome)`` for all ten members, in plan order."""
        return tuple((m.dataset_id, m.chromosome) for m in self.members)

    def per_chromosome(self) -> dict[str, int]:
        counts: dict[str, int] = dict.fromkeys(CHROMOSOMES, 0)
        for member in self.members:
            counts[member.chromosome] += 1
        return counts


def _repository_root() -> Path:
    return Path(__file__).resolve().parents[3]


def build_validation_schedule(root: Path | None = None) -> ValidationSchedule:
    """Derive the frozen ten-member VALIDATION schedule from the committed split manifest.

    Raises ``ScheduleError`` rather than returning a partial schedule: a validation campaign that
    silently ran nine members would produce an aggregate that looks complete and is not.
    """
    path = (root or _repository_root()) / SPLIT_MANIFEST_PATH
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ScheduleError(f"the split manifest at {path} is unreadable: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise ScheduleError(f"the split manifest at {path} is not JSON: {exc}") from exc

    samples = document.get("samples")
    if not isinstance(samples, list):
        raise ScheduleError("the split manifest carries no sample list")

    # VALIDATION is selected here, before anything else touches the data. TRAIN and TEST never
    # reach the loop below.
    validation = [s for s in samples if s.get("partition") == VALIDATION_PARTITION]
    if len(validation) != VALIDATION_COUNT:
        raise ScheduleError(
            f"expected {VALIDATION_COUNT} VALIDATION samples, found {len(validation)}"
        )

    members: list[ValidationMember] = []
    per_chromosome: dict[str, int] = dict.fromkeys(CHROMOSOMES, 0)
    for index, sample in enumerate(validation):  # the manifest's committed order IS canonical
        chromosome = sample.get("chromosome")
        if chromosome not in per_chromosome:
            raise ScheduleError(f"VALIDATION sample on unexpected chromosome {chromosome!r}")
        per_chromosome[chromosome] += 1
        members.append(
            ValidationMember(
                dataset_id=_text(sample, "dataset_id"),
                chromosome=str(chromosome),
                round_id=_text(sample, "round_id"),
                identity_tuple_hash=_text(sample, "identity_tuple_hash"),
                member_index=index,
            )
        )

    for chromosome, count in per_chromosome.items():
        if count != VALIDATION_PER_CHROMOSOME:
            raise ScheduleError(
                f"{chromosome} has {count} VALIDATION members, expected {VALIDATION_PER_CHROMOSOME}"
            )

    identifiers = [m.dataset_id for m in members]
    if len(set(identifiers)) != VALIDATION_COUNT:
        raise ScheduleError("the VALIDATION schedule contains a duplicate dataset_id")

    # a closing check on the OTHER partitions rather than on this one: if a TRAIN or TEST dataset
    # id appears among the ten, the filter above was not the only thing selecting them.
    closed = {
        s["dataset_id"]
        for s in samples
        if s.get("partition") in (_TRAIN_PARTITION, _TEST_PARTITION)
    }
    if closed & set(identifiers):
        raise ScheduleError("a TRAIN or TEST dataset reached the VALIDATION schedule")

    return ValidationSchedule(
        members=tuple(members),
        split_manifest_sha256=split_manifest_sha256(root),
    )


def _text(sample: dict[str, Any], key: str) -> str:
    value = sample.get(key)
    if not isinstance(value, str) or not value:
        raise ScheduleError(f"a VALIDATION sample is missing {key!r}")
    return value
