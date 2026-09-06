"""The frozen v2 scientific reference, rebuilt from bundle bytes with no database.

An offline verifier that only recomputes hashes of the bytes in front of it proves internal
consistency and nothing else. A campaign could publish a beautifully self-consistent tree in which
every ``actual_delta`` was shifted, every utility rewritten, and every decision authored by hand;
recomputed hashes over those bytes agree perfectly, because they were recomputed over the same
lie.

So the verifier needs an independent source of scientific truth. This module is it: the frozen
TRAIN bundle is re-read from disk, the ``TrainingDataset`` is rebuilt and required to hash to the
accepted identity, the ``RelativeFinalistDataset`` is derived from it and required to hash to the
accepted relative identity, and the four-finalist utility table is derived from the source rows.
Nothing here consults the TRAIN database, VALIDATION, or TEST — the whole point is that an
independent reviewer can run it from a checkout and a bundle.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any, Final

from minos_engine.models.relative_finalist_contract import (
    ALTERNATIVE_FINALISTS,
    FINALIST_DOMAIN,
    SAFE_BASELINE_CONFIG_HASH,
    RelativeFinalistError,
)

__all__ = [
    "FrozenScientificReference",
    "build_frozen_scientific_reference",
    "four_finalist_utility_table",
]

RELATIVE_ROWS: Final = 150
SOURCE_CELLS: Final = 200


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise RelativeFinalistError(message)


def four_finalist_utility_table(source: Any) -> dict[tuple[str, str], float]:
    """U(BAM, config) over the four finalists, from the frozen TRAIN rows.

    A cell that was not ADMITTED scores zero: the utility of a configuration that does not produce
    an admissible callset is not "missing", it is nothing. This is THE definition used by the
    campaign and by the verifier alike, so the two can never drift apart.
    """
    utility: dict[tuple[str, str], float] = {}
    for row in source.rows:
        if row.config_hash not in FINALIST_DOMAIN:
            continue
        score = row.admitted_score
        utility[(row.dataset_id, row.config_hash)] = (
            float(score) if row.outcome == "ADMITTED" and score is not None else 0.0
        )
    _require(
        len(utility) == SOURCE_CELLS,
        f"the four-finalist utility table has {len(utility)} cells, expected {SOURCE_CELLS}",
    )
    return utility


class FrozenScientificReference:
    """Every scientific label a published v2 tree may legitimately contain.

    Held by value and handed out as copies, so a verifier cannot be tricked into comparing the
    evidence against something the evidence itself supplied.
    """

    __slots__ = (
        "_alternative_utility",
        "_chromosome_of",
        "_delta",
        "_safe_utility",
        "_utility",
        "dataset_identity",
        "source_dataset_identity",
    )

    def __init__(
        self,
        *,
        chromosome_of: dict[str, str],
        utility: dict[tuple[str, str], float],
        delta: dict[tuple[str, str], float],
        safe_utility: dict[str, float],
        alternative_utility: dict[tuple[str, str], float],
        dataset_identity: str,
        source_dataset_identity: str,
    ) -> None:
        self._chromosome_of = dict(chromosome_of)
        self._utility = dict(utility)
        self._delta = dict(delta)
        self._safe_utility = dict(safe_utility)
        self._alternative_utility = dict(alternative_utility)
        self.dataset_identity = dataset_identity
        self.source_dataset_identity = source_dataset_identity

    @property
    def chromosome_of(self) -> dict[str, str]:
        return dict(self._chromosome_of)

    @property
    def utility(self) -> dict[tuple[str, str], float]:
        return dict(self._utility)

    @property
    def safe_utility(self) -> dict[str, float]:
        return dict(self._safe_utility)

    @property
    def delta(self) -> dict[tuple[str, str], float]:
        return dict(self._delta)

    @property
    def alternative_utility(self) -> dict[tuple[str, str], float]:
        return dict(self._alternative_utility)

    def cells(self) -> list[tuple[str, str]]:
        return sorted(self._delta)

    def bams(self) -> list[str]:
        return sorted(self._chromosome_of)


@lru_cache(maxsize=4)
def _build(root: str | None, workspace: str | None) -> FrozenScientificReference:
    from minos_engine.models.prefit_loader import load_verified_training_dataset
    from minos_engine.models.relative_finalist_authority import (
        ACCEPTED_RELATIVE_DATASET_IDENTITY,
        ACCEPTED_SOURCE_TRAINING_DATASET,
    )
    from minos_engine.models.relative_finalist_dataset import build_relative_finalist_dataset

    source = load_verified_training_dataset(
        workspace=Path(workspace) if workspace else None,
        root=Path(root) if root else None,
    )
    _require(
        source.identity() == ACCEPTED_SOURCE_TRAINING_DATASET,
        f"the rebuilt TRAIN dataset hashes to {source.identity()}, not the accepted identity",
    )
    dataset = build_relative_finalist_dataset(source)
    _require(
        dataset.identity() == ACCEPTED_RELATIVE_DATASET_IDENTITY,
        f"the rebuilt relative dataset hashes to {dataset.identity()}, not the accepted identity",
    )
    _require(len(dataset.rows) == RELATIVE_ROWS, f"{len(dataset.rows)} rows, expected 150")

    utility = four_finalist_utility_table(source)
    delta: dict[tuple[str, str], float] = {}
    alternative_utility: dict[tuple[str, str], float] = {}
    for row in dataset.rows:
        key = (row.dataset_id, row.config_hash)
        _require(key not in delta, f"the frozen dataset repeats the cell {key}")
        delta[key] = float(row.delta)
        alternative_utility[key] = float(row.alternative_utility)
        # the row's own utilities must be the table's, or the two sources of truth disagree and
        # neither can be used to judge evidence
        _require(
            float(row.alternative_utility) == utility[key]
            and float(row.safe_utility) == utility[(row.dataset_id, SAFE_BASELINE_CONFIG_HASH)],
            f"the frozen advantage row for {key} disagrees with the frozen utility table",
        )
        _require(
            row.chromosome == dataset.bam_chromosome[row.dataset_id],
            f"the frozen row for {key} disagrees with the frozen chromosome assignment",
        )
    _require(
        sorted({c for _, c in delta}) == sorted(ALTERNATIVE_FINALISTS),
        "the frozen cells do not span exactly the three alternative finalists",
    )
    return FrozenScientificReference(
        chromosome_of=dict(dataset.bam_chromosome),
        utility=utility,
        delta=delta,
        safe_utility={b: float(u) for b, u in dataset.safe_utility.items()},
        alternative_utility=alternative_utility,
        dataset_identity=dataset.identity(),
        source_dataset_identity=source.identity(),
    )


def build_frozen_scientific_reference(
    *, root: Any = None, workspace: Any = None
) -> FrozenScientificReference:
    """Rebuild the frozen scientific reference. Cached: the bundle bytes cannot change mid-run."""
    return _build(str(root) if root is not None else None, str(workspace) if workspace else None)
