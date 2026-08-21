"""F3-C2 bounded enqueue — pure unit tests (no database).

Covers the range-bound validation that must fail before any database/filesystem access, the
maximum batch bound, the exported surface (no enqueue-all; no caller-supplied trust), and that
the enqueue selection order and job_key are exactly the frozen F3-B logical-job order/formula.
"""

from __future__ import annotations

import inspect

import pytest

from minos_engine.experiments.accepted_plan import build_accepted_experiment_plan
from minos_engine.experiments.plan import compute_job_key, iter_logical_jobs
from minos_engine.storage import l2f_job_enqueue as EN
from minos_engine.storage.l2f_job_enqueue import (
    MAX_ENQUEUE_BATCH,
    JobEnqueueRangeError,
    enqueue_accepted_experiment_jobs,
)

_PLAN = build_accepted_experiment_plan()


def test_max_batch_is_bounded() -> None:
    assert MAX_ENQUEUE_BATCH == 64


@pytest.mark.parametrize(
    ("start", "count"),
    [(-1, 1), (0, 0), (0, 65), (5, 0), (0, MAX_ENQUEUE_BATCH + 1), (-100, 10)],
)
def test_invalid_range_fails_before_any_db_access(
    monkeypatch: pytest.MonkeyPatch, start: int, count: int
) -> None:
    # No MINOS_DATABASE_URL: an invalid range must raise the typed range error BEFORE the code
    # ever tries to reach the database (which would raise a different, DB-not-configured error).
    monkeypatch.delenv("MINOS_DATABASE_URL", raising=False)
    with pytest.raises(JobEnqueueRangeError):
        enqueue_accepted_experiment_jobs(start=start, count=count)


def test_boolean_start_or_count_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MINOS_DATABASE_URL", raising=False)
    with pytest.raises(JobEnqueueRangeError):
        enqueue_accepted_experiment_jobs(start=True, count=1)  # type: ignore[arg-type]
    with pytest.raises(JobEnqueueRangeError):
        enqueue_accepted_experiment_jobs(start=0, count=True)  # type: ignore[arg-type]


def test_public_api_requires_keyword_start_and_count_no_defaults() -> None:
    sig = inspect.signature(enqueue_accepted_experiment_jobs)
    params = sig.parameters
    assert set(params) == {"start", "count"}
    for name in ("start", "count"):
        assert params[name].kind is inspect.Parameter.KEYWORD_ONLY
        assert params[name].default is inspect.Parameter.empty  # no defaults


def test_no_enqueue_all_or_trust_surface() -> None:
    # exactly one production entry point; no enqueue-all; the trust boundary is unexported.
    assert "enqueue_accepted_experiment_jobs" in EN.__all__
    assert not any("all" in name.lower() for name in EN.__all__)
    assert "_enqueue_experiment_jobs_with_trust" not in EN.__all__
    assert not hasattr(EN, "enqueue_all_experiment_jobs")


def test_selection_order_and_job_key_match_frozen_f3b() -> None:
    jobs = list(iter_logical_jobs(_PLAN))
    cc = _PLAN.candidate_count
    assert len(jobs) == _PLAN.logical_job_count == _PLAN.train_member_count * cc
    for k, lj in enumerate(jobs):
        # member-major then config-index order.
        assert lj.member_index == k // cc
        assert lj.config_index == k % cc
        member = _PLAN.members[k // cc]
        config = _PLAN.configs[k % cc]
        assert lj.dataset_id == member.dataset_id
        assert lj.config_hash == config.config_hash
        # independently recomputed key equals the frozen logical job key.
        assert lj.job_key == compute_job_key(
            plan_hash=_PLAN.plan_hash,
            member_index=member.member_index,
            dataset_id=member.dataset_id,
            profile_id=member.profile_id,
            content_hash=member.content_hash,
            feature_values_hash=member.feature_values_hash,
            config_index=config.config_index,
            config_hash=config.config_hash,
        )
    # keys are globally unique across the corpus.
    assert len({lj.job_key for lj in jobs}) == len(jobs)
