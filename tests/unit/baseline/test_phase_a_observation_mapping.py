"""The per-row observation mapping, in isolation — no database.

The mapper now lives in the shared plan-scoped reader, because Phase A and Phase B must interpret
a row identically; these controls therefore pin the shared implementation Phase A delegates to.

The integration suite proves these mappings against real ledger rows. This one exercises the same
mapper directly, because the distinctions it makes are the ones the frozen objective is most
sensitive to: a refused admission is not a zero, an evaluation failure keeps the runtime of the
GATK run that DID succeed, and a row that cannot be interpreted faithfully is refused outright
rather than approximated.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

from minos_engine.baseline.objective import BaselineObjectiveError
from minos_engine.baseline.phase_a_observations import (
    PHASE_A_SCORING_CONTRACT,
    PhaseAObservationError,
)
from minos_engine.baseline.plan_observations import _observation_for_success

_CONFIG = "a" * 64
_DATASET = "minos-chr18-028662fb934529d7"


@dataclass(frozen=True)
class _Job:
    config_hash: str = _CONFIG
    dataset_id: str = _DATASET
    member_index: int = 0


def _row(**overrides: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "job_key": "b" * 64,
        "chromosome": "chr18",
        "success_runtime_ms": 71962,
        "evaluation_failure_code": None,
        "evaluation_id": "0f1e2d3c-4b5a-6978-8796-a5b4c3d2e1f0",
        "minos_score": 0.618836338270872,
        "admitted": True,
        "admission_code": "ADMITTED",
        "scoring_contract_hash": PHASE_A_SCORING_CONTRACT,
    }
    row.update(overrides)
    return row


def test_an_admitted_row_carries_the_exact_score_and_its_own_runtime() -> None:
    observation = _observation_for_success(_row(), _Job())

    assert observation is not None
    assert observation.minos_score == 0.618836338270872
    assert observation.admitted is True
    assert observation.failure_code is None
    assert observation.gatk_runtime_ms == 71962
    assert observation.outcome == "ADMITTED"


def test_a_row_that_has_not_been_scored_yet_is_no_observation() -> None:
    """Executed but unevaluated is UNDECIDED, and undecided is represented by absence."""
    assert _observation_for_success(_row(evaluation_id=None, minos_score=None), _Job()) is None


def test_a_refused_admission_carries_no_score_and_no_failure_code() -> None:
    observation = _observation_for_success(
        _row(admitted=False, admission_code="NONPOSITIVE_SCORE", minos_score=0.0), _Job()
    )

    assert observation is not None
    assert observation.minos_score is None, "a refusal must not be recorded as a score of zero"
    assert observation.admitted is False
    assert observation.failure_code is None
    assert observation.outcome == "CANDIDATE_FAILURE"
    assert observation.gatk_runtime_ms == 71962


def test_an_evaluation_failure_keeps_the_successful_gatk_runtime() -> None:
    observation = _observation_for_success(
        _row(evaluation_failure_code="HAPPY_TIMEOUT", evaluation_id=None, minos_score=None),
        _Job(),
    )

    assert observation is not None
    assert observation.failure_code == "HAPPY_TIMEOUT"
    assert observation.minos_score is None
    assert observation.gatk_runtime_ms == 71962, "GATK succeeded; its own duration is the measure"
    assert observation.outcome == "INFRASTRUCTURE_INCIDENT"


def test_an_unbounded_evaluation_failure_code_is_refused() -> None:
    with pytest.raises(BaselineObjectiveError, match="unknown bounded failure code"):
        _observation_for_success(
            _row(evaluation_failure_code="NOT_A_CODE", evaluation_id=None, minos_score=None),
            _Job(),
        )


def test_a_success_without_a_recorded_runtime_is_refused() -> None:
    """The tie-break statistic is a measurement; there is no default to fall back on."""
    with pytest.raises(PhaseAObservationError, match="without a recorded runtime"):
        _observation_for_success(_row(success_runtime_ms=None), _Job())


def test_an_evaluation_under_another_scoring_contract_is_refused() -> None:
    with pytest.raises(PhaseAObservationError, match="not the production contract"):
        _observation_for_success(_row(scoring_contract_hash="c" * 64), _Job())


def test_an_admitted_row_without_a_score_is_refused() -> None:
    with pytest.raises(PhaseAObservationError, match="ADMITTED with no persisted minos_score"):
        _observation_for_success(_row(minos_score=None), _Job())
