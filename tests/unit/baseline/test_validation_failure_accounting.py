"""Validation failure accounting: whose fault was it, ours or the candidate's?

The frozen protocol splits a decided-but-not-admitted observation into two very different things:

* a **candidate failure** — the configuration produced nothing usable (``GATK_NONZERO_EXIT``,
  ``GATK_TIMEOUT``, ``GATK_OUTPUT_INVALID``, ``GATK_OUTPUT_MISSING``), or the validator refused
  its result. That is evidence ABOUT the finalist, and the campaign continues past it;
* an **infrastructure incident** — WE failed (``PREPARATION_FAILED``, ``EXECUTION_ERROR``, or any
  bounded evaluation-side failure). That is evidence about the harness, it must never be charged
  to a finalist, and the campaign holds.

``not admitted`` collapses both into one bucket, which is precisely the mistake this module exists
to prevent. The classification authority is ``BaselineObservation.outcome`` /
``classify_failure_code``, surfaced as ``PlanObservationSnapshot.candidate_failure_count`` and
``.infrastructure_incident_count``; the validation control plane must read those and never
re-derive them.

These tests drive the DATABASE-BACKED wrappers — ``read_l2f2_validation_progress`` and
``rank_l2f2_validation_finalists`` — because that is where the accounting lives. The ledger reader
and the connection are stubbed so the real production arithmetic runs deterministically without a
database, without a validation workspace and without a byte of validation truth.
"""

from __future__ import annotations

from typing import Any

import pytest

from minos_engine.baseline.objective import BaselineObservation
from minos_engine.baseline.phase_d import build_l2f2_phase_d_authority
from minos_engine.baseline.plan_observations import PlanObservationSnapshot
from minos_engine.baseline.validation_members import build_validation_schedule
from minos_engine.storage import l2f2_validation_control as control
from tests.unit.baseline.test_validation_control_plane import _freeze

_ENV = "e" * 64


def _authority() -> Any:
    return build_l2f2_phase_d_authority(_freeze(), schedule=build_validation_schedule())


def _observation(
    config_hash: str,
    dataset_id: str,
    chromosome: str,
    *,
    score: float | None = 0.6,
    failure_code: str | None = None,
) -> BaselineObservation:
    admitted = failure_code is None and score is not None
    return BaselineObservation(
        config_hash=config_hash,
        dataset_id=dataset_id,
        chromosome=chromosome,
        minos_score=score if admitted else None,
        admitted=admitted,
        failure_code=failure_code,
        gatk_runtime_ms=60_000,
    )


def _forty(authority: Any, *, spoil: dict[str, Any] | None = None) -> list[BaselineObservation]:
    """Forty decided observations. ``spoil`` replaces the FIRST one with a failure of some kind."""
    observations = [
        _observation(p.config_hash, p.dataset_id, p.chromosome) for p in authority.pairs()
    ]
    if spoil is not None:
        first = authority.pairs()[0]
        observations[0] = _observation(
            first.config_hash, first.dataset_id, first.chromosome, **spoil
        )
    return observations


class _StubResult:
    def __init__(self, rows: list[str]) -> None:
        self._rows = rows

    def scalars(self) -> _StubResult:
        return self

    def all(self) -> list[str]:
        return self._rows


class _StubConnection:
    def __init__(self, rows: list[str]) -> None:
        self._rows = rows

    def execute(self, *_args: Any, **_kwargs: Any) -> _StubResult:
        return _StubResult(self._rows)

    def __enter__(self) -> _StubConnection:
        return self

    def __exit__(self, *_exc: Any) -> None:
        return None


class _StubEngine:
    """Just enough engine for the job-status query the progress function makes."""

    def __init__(self, rows: list[str]) -> None:
        self._rows = rows

    def connect(self) -> _StubConnection:
        return _StubConnection(self._rows)


def _snapshot(observations: list[BaselineObservation], *, evaluation_failures: int = 0) -> Any:
    """A real ``PlanObservationSnapshot``, so the authoritative counters are the real ones."""
    return PlanObservationSnapshot(
        observations=tuple(observations),
        execution_result_count=len(observations),
        execution_failure_count=sum(1 for o in observations if o.failure_code is not None),
        evaluation_result_count=len(observations) - evaluation_failures,
        evaluation_failure_count=evaluation_failures,
        execution_environment_hash=_ENV,
    )


def _progress(
    monkeypatch: pytest.MonkeyPatch,
    observations: list[BaselineObservation],
    *,
    evaluation_failures: int = 0,
) -> Any:
    snapshot = _snapshot(observations, evaluation_failures=evaluation_failures)
    monkeypatch.setattr(
        "minos_engine.baseline.plan_observations.load_plan_observations",
        lambda *_a, **_k: snapshot,
    )
    authority = _authority()
    engine = _StubEngine(["SUCCEEDED"] * len(observations))
    return control.read_l2f2_validation_progress(engine, authority=authority, plan=object())


def _rank(
    monkeypatch: pytest.MonkeyPatch,
    observations: list[BaselineObservation],
    *,
    evaluation_failures: int = 0,
) -> Any:
    snapshot = _snapshot(observations, evaluation_failures=evaluation_failures)
    monkeypatch.setattr(
        "minos_engine.baseline.plan_observations.load_plan_observations",
        lambda *_a, **_k: snapshot,
    )
    return control.rank_l2f2_validation_finalists(
        _StubEngine([]), authority=_authority(), plan=object()
    )


# --------------------------------------------------------------------------------------------
# the classification authority itself — the source of truth these tests hold the control plane to
# --------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("failure_code", "expected"),
    [
        (None, "CANDIDATE_FAILURE"),  # validator non-admission
        ("GATK_NONZERO_EXIT", "CANDIDATE_FAILURE"),
        ("GATK_TIMEOUT", "CANDIDATE_FAILURE"),
        ("GATK_OUTPUT_INVALID", "CANDIDATE_FAILURE"),
        ("GATK_OUTPUT_MISSING", "CANDIDATE_FAILURE"),
        ("PREPARATION_FAILED", "INFRASTRUCTURE_INCIDENT"),
        ("EXECUTION_ERROR", "INFRASTRUCTURE_INCIDENT"),
    ],
)
def test_the_committed_outcome_property_splits_blame(
    failure_code: str | None, expected: str
) -> None:
    """``not admitted`` is not one thing. Two of these seven are OUR failures, not a finalist's."""
    observation = _observation("a" * 64, "d", "chr18", score=None, failure_code=failure_code)
    assert observation.admitted is False
    assert observation.outcome == expected


# --------------------------------------------------------------------------------------------
# A-C: what the campaign may continue past
# --------------------------------------------------------------------------------------------


def test_a_fully_admitted_validation_charges_nothing_to_anyone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    progress = _progress(monkeypatch, _forty(_authority()))
    assert progress.candidate_failure_count == 0
    assert progress.infrastructure_incident_count == 0
    assert progress.decided_observation_count == 40
    assert progress.complete is True


def test_a_validator_non_admission_is_a_candidate_failure_not_an_incident(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    progress = _progress(
        monkeypatch, _forty(_authority(), spoil={"failure_code": None, "score": None})
    )
    assert progress.candidate_failure_count == 1
    assert progress.infrastructure_incident_count == 0
    assert progress.complete is True  # the campaign continues past a candidate's own failure


@pytest.mark.parametrize(
    "failure_code",
    ["GATK_NONZERO_EXIT", "GATK_TIMEOUT", "GATK_OUTPUT_INVALID", "GATK_OUTPUT_MISSING"],
)
def test_a_candidate_execution_failure_is_charged_to_the_candidate(
    monkeypatch: pytest.MonkeyPatch, failure_code: str
) -> None:
    progress = _progress(monkeypatch, _forty(_authority(), spoil={"failure_code": failure_code}))
    assert progress.candidate_failure_count == 1
    assert progress.infrastructure_incident_count == 0
    assert progress.complete is True


def test_ranking_permits_legitimate_candidate_failures(monkeypatch: pytest.MonkeyPatch) -> None:
    ranking = _rank(monkeypatch, _forty(_authority(), spoil={"failure_code": "GATK_TIMEOUT"}))
    assert ranking.observation_count == 40
    assert sum(e.candidate_failure_count for e in ranking.entries) == 1


# --------------------------------------------------------------------------------------------
# D-F: what must stop the campaign, and must never be charged to a finalist
# --------------------------------------------------------------------------------------------


@pytest.mark.parametrize("failure_code", ["PREPARATION_FAILED", "EXECUTION_ERROR"])
def test_an_execution_infrastructure_failure_is_never_charged_to_a_finalist(
    monkeypatch: pytest.MonkeyPatch, failure_code: str
) -> None:
    """THE regression. ``not admitted`` would have billed our own failure to the candidate."""
    progress = _progress(monkeypatch, _forty(_authority(), spoil={"failure_code": failure_code}))
    assert progress.candidate_failure_count == 0, "our failure was charged to a finalist"
    assert progress.infrastructure_incident_count == 1


@pytest.mark.parametrize("failure_code", ["PREPARATION_FAILED", "EXECUTION_ERROR"])
def test_forty_decided_observations_are_not_complete_with_an_execution_incident(
    monkeypatch: pytest.MonkeyPatch, failure_code: str
) -> None:
    """Forty decided rows and zero evaluation failures — and still not a finished confirmation."""
    progress = _progress(monkeypatch, _forty(_authority(), spoil={"failure_code": failure_code}))
    assert progress.decided_observation_count == 40
    assert progress.evaluation_failure_count == 0
    assert progress.complete is False


@pytest.mark.parametrize("failure_code", ["PREPARATION_FAILED", "EXECUTION_ERROR"])
def test_ranking_refuses_an_execution_incident_even_with_no_evaluation_failures(
    monkeypatch: pytest.MonkeyPatch, failure_code: str
) -> None:
    """The gate the old wrapper could not see: infra on the EXECUTION side, evaluation clean."""
    with pytest.raises(control.ValidationControlError, match="infrastructure incident"):
        _rank(monkeypatch, _forty(_authority(), spoil={"failure_code": failure_code}))


@pytest.mark.parametrize(
    "code", ["EVALUATION_ERROR", "HAPPY_TIMEOUT", "TRUTH_BYTES_MISMATCH", "ARTIFACT_PUBLISH_FAILED"]
)
def test_an_evaluation_side_bounded_failure_is_infrastructure(
    monkeypatch: pytest.MonkeyPatch, code: str
) -> None:
    """Evaluation-side failures were already caught; they must stay caught, and stay OURS.

    An evaluation failure becomes a DECIDED observation carrying its bounded code — GATK produced
    output, our scoring of it failed — so it is visible to the same authoritative counter that
    sees an execution-side incident, and needs no second mechanism.
    """
    progress = _progress(monkeypatch, _forty(_authority(), spoil={"failure_code": code}))
    assert progress.candidate_failure_count == 0, "our scoring failure was charged to a finalist"
    assert progress.infrastructure_incident_count == 1
    assert progress.complete is False
    with pytest.raises(control.ValidationControlError, match="infrastructure incident"):
        _rank(monkeypatch, _forty(_authority(), spoil={"failure_code": code}))


def test_an_evaluation_failure_row_alone_still_withholds_completion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Belt and braces: a bare evaluation-failure row leaves the confirmation short of forty."""
    observations = _forty(_authority())[:39]
    progress = _progress(monkeypatch, observations, evaluation_failures=1)
    assert progress.decided_observation_count == 39
    assert progress.evaluation_failure_count == 1
    assert progress.complete is False


def _executable_source(module: Any) -> str:
    """The module's source with comments and docstrings removed.

    Naming a failure code in prose is documentation; branching on one is a second classifier. This
    test is about the latter, so it must not be satisfied — or tripped — by a comment.
    """
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(module))
    for node in ast.walk(tree):
        if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            body = node.body
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)
            ):
                body.pop(0)
    return ast.unparse(tree)


def test_the_control_plane_does_not_re_enumerate_failure_codes() -> None:
    """The classification authority stays in one place; the control plane only reads its verdict."""
    executable = _executable_source(control)
    for code in (
        "GATK_NONZERO_EXIT",
        "GATK_TIMEOUT",
        "GATK_OUTPUT_INVALID",
        "GATK_OUTPUT_MISSING",
        "PREPARATION_FAILED",
        "EXECUTION_ERROR",
        "CANDIDATE_EXECUTION_FAILURE_CODES",
        "INFRASTRUCTURE_EXECUTION_FAILURE_CODES",
        "INFRASTRUCTURE_EVALUATION_FAILURE_CODES",
        "classify_failure_code",
    ):
        assert code not in executable, f"the validation control plane re-enumerates {code}"
    # and it does not re-derive the split from ``admitted`` either
    assert "admitted" not in executable
    assert "snapshot.candidate_failure_count" in executable
    assert "snapshot.infrastructure_incident_count" in executable
