"""The Phase-A expansion boundary's pure surface — no database.

What a caller may choose is the whole subject here: a contiguous slice of the frozen logical
order, bounded by the same ``MAX_ENQUEUE_BATCH`` the historical path uses, never reaching the
completed canary, and never naming a plan, member or candidate. Everything scientific is
recomputed from committed authority, so these controls check the SHAPE of the boundary rather
than any value a caller supplies.
"""

from __future__ import annotations

import inspect
from typing import Any

import pytest

from minos_engine.storage import l2f2_phase_a_control as PC
from minos_engine.storage.l2f2_phase_a_control import (
    CANARY_LOGICAL_INDEX,
    PHASE_A_LOGICAL_JOB_COUNT,
    PhaseAExpansionError,
    expand_l2f2_phase_a_jobs,
)
from minos_engine.storage.l2f_job_enqueue import MAX_ENQUEUE_BATCH


class _ExplodingEngine:
    """Any database access at all is a failure of the pre-argument contract."""

    def connect(self) -> Any:
        raise AssertionError("the range must be validated before any database access")


@pytest.mark.parametrize(
    ("start", "count"),
    [
        (0, 1),
        (-1, 1),
        (1, 0),
        (1, -1),
        (1, MAX_ENQUEUE_BATCH + 1),
        (194, 2),
        (195, 1),
        (True, 1),
        (1, True),
        ("1", 1),
        (1, 1.0),
    ],
)
def test_an_out_of_contract_range_never_reaches_the_database(start: Any, count: Any) -> None:
    with pytest.raises(PhaseAExpansionError):
        expand_l2f2_phase_a_jobs(_ExplodingEngine(), start=start, count=count)  # type: ignore[arg-type]


def test_the_screen_size_and_canary_index_come_from_committed_authority() -> None:
    from minos_engine.baseline.phase_a import build_phase_a_authority
    from minos_engine.experiments.plan import iter_logical_jobs

    authority = build_phase_a_authority()
    keys = [job.job_key for job in iter_logical_jobs(authority.plan)]

    assert PHASE_A_LOGICAL_JOB_COUNT == len(keys) == 195
    assert len(set(keys)) == 195, "the frozen order enumerates a duplicate job key"
    assert CANARY_LOGICAL_INDEX == 0
    assert keys[CANARY_LOGICAL_INDEX] == authority.canary.job_key
    assert PC._frozen_job_keys() == keys


def test_the_documented_slices_tile_the_remaining_jobs_exactly_once() -> None:
    """Four bounded operator acts cover 1..194 with no gap, no overlap and no job 0."""
    covered: list[int] = []
    for start, count in ((1, 64), (65, 64), (129, 64), (193, 2)):
        assert count <= MAX_ENQUEUE_BATCH
        covered.extend(range(start, start + count))

    assert covered == sorted(covered)
    assert covered == list(range(1, PHASE_A_LOGICAL_JOB_COUNT))
    assert CANARY_LOGICAL_INDEX not in covered


def test_the_boundary_offers_no_enqueue_all_and_no_scientific_choice() -> None:
    assert "expand_l2f2_phase_a_jobs" in PC.__all__
    for name in PC.__all__:
        assert not name.lower().startswith("enqueue_all")
        assert "remaining" not in name.lower()

    signature = inspect.signature(expand_l2f2_phase_a_jobs)
    assert list(signature.parameters) == ["engine", "start", "count"]
    for name in ("start", "count"):
        assert signature.parameters[name].kind is inspect.Parameter.KEYWORD_ONLY
        assert signature.parameters[name].default is inspect.Parameter.empty

    # structural, not textual: NO function in the module accepts a scientific selection.
    forbidden = {"plan", "plan_hash", "config", "config_hash", "member", "dataset_id", "all"}
    for name, obj in vars(PC).items():
        if not inspect.isfunction(obj):
            continue
        offending = forbidden & set(inspect.signature(obj).parameters)
        assert not offending, f"{name} lets a caller choose {offending}"


def test_progress_is_read_only_by_construction() -> None:
    """The progress reader takes an engine and nothing else, and returns counts only."""
    import dataclasses

    signature = inspect.signature(PC.read_l2f2_phase_a_progress)
    assert list(signature.parameters) == ["engine"]
    fields = {f.name for f in dataclasses.fields(PC.PhaseAProgress)}
    assert all(name.endswith("_count") for name in fields)
    assert "decided_observation_count" in fields
