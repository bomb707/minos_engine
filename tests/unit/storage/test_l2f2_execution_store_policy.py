"""Which STORE each execution phase authorizes against, proved as a full matrix.

The shared execution core was hard-coded to the TRAIN baseline on every step of the lifecycle —
claim, prepare, start, release, fail, complete — while ``execute_next_l2f2_phase_d_job`` and the
``0021`` resolver were already validation-aware. Phase D could therefore be activated but never
executed: the public entry authorized the validation store, then the core it delegated to reached
for the baseline.

Store policy and PARTITION policy are separate layers on purpose. Phase D must be the validation
database *and* validation rows; a TRAIN phase must be the baseline database *and* train rows.
Collapsing them into one permissive check would let a single mistake open both doors, so both
matrices are asserted here side by side.
"""

from __future__ import annotations

from typing import Any

import pytest

from minos_engine.common.errors import MinosEngineError
from minos_engine.storage.l2f2_runner import (
    _EXECUTED_PARTITION_BY_PHASE,
    _RESOLVE_SQL_BY_PHASE,
    _STORE_AUTHORIZER_BY_PHASE,
    _authorize_runner_connection_for_phase,
    authorize_baseline_runner_connection,
    authorize_validation_runner_connection,
)

_BASELINE = ("minos_l2f2_baseline", "0020_l2f2_phase_c_execution")
_VALIDATION = ("minos_l2f2_validation", "0024_l2f2_phase_d_anchor")


class _Conn:
    """Answers only the two questions a store authorizer asks of a connection."""

    def __init__(self, database: str, revision: str) -> None:
        self._answers = {
            "SELECT current_database()": database,
            "SELECT version_num FROM alembic_version": revision,
        }

    def execute(self, statement: Any, *_: Any) -> Any:
        text = " ".join(str(statement).split())
        for probe, answer in self._answers.items():
            if probe in text:
                return _Scalar(answer)
        return _Scalar(None)


class _Scalar:
    def __init__(self, value: Any) -> None:
        self._value = value

    def scalar_one(self) -> Any:
        return self._value

    def scalar_one_or_none(self) -> Any:
        return self._value


def _authorizes(phase: str, store: tuple[str, str]) -> bool:
    """Did this phase accept this store, up to the point where a real connection is needed?"""
    try:
        _authorize_runner_connection_for_phase(_Conn(*store), phase=phase)
    except MinosEngineError as exc:
        message = str(exc)
        if "database" in message or "revision" in message or "store" in message:
            return False
        raise
    except Exception:  # noqa: BLE001 - a later principal/membership probe means the store passed
        return True
    return True


# --------------------------------------------------------------------------------------------
# the store matrix
# --------------------------------------------------------------------------------------------
@pytest.mark.parametrize("phase", ["PHASE_A", "PHASE_B", "PHASE_C"])
def test_train_phases_authorize_the_baseline_and_refuse_validation(phase: str) -> None:
    assert _authorizes(phase, _BASELINE) is True
    assert _authorizes(phase, _VALIDATION) is False


def test_phase_d_authorizes_validation_and_refuses_the_baseline() -> None:
    assert _authorizes("PHASE_D", _VALIDATION) is True
    assert _authorizes("PHASE_D", _BASELINE) is False


@pytest.mark.parametrize("phase", ["PHASE_E", "phase_d", "", "PHASE_TEST", "TEST"])
def test_an_unknown_phase_has_no_store_and_fails_closed(phase: str) -> None:
    with pytest.raises(MinosEngineError, match="no L2-F2 execution store is accepted"):
        _authorize_runner_connection_for_phase(_Conn(*_VALIDATION), phase=phase)


# --------------------------------------------------------------------------------------------
# the policy itself
# --------------------------------------------------------------------------------------------
def test_the_store_policy_is_exact_and_total() -> None:
    assert {
        "PHASE_A": authorize_baseline_runner_connection,
        "PHASE_B": authorize_baseline_runner_connection,
        "PHASE_C": authorize_baseline_runner_connection,
        "PHASE_D": authorize_validation_runner_connection,
    } == _STORE_AUTHORIZER_BY_PHASE


def test_store_and_partition_policies_cover_the_same_phases() -> None:
    """A phase with a resolver but no store — or no partition — would fail closed. None exists."""
    assert set(_STORE_AUTHORIZER_BY_PHASE) == set(_EXECUTED_PARTITION_BY_PHASE)
    assert set(_STORE_AUTHORIZER_BY_PHASE) == set(_RESOLVE_SQL_BY_PHASE)


def test_store_and_partition_policies_agree_phase_by_phase() -> None:
    """Two independent layers, and they must not disagree about what a phase is."""
    expected = {
        "PHASE_A": (authorize_baseline_runner_connection, "train"),
        "PHASE_B": (authorize_baseline_runner_connection, "train"),
        "PHASE_C": (authorize_baseline_runner_connection, "train"),
        "PHASE_D": (authorize_validation_runner_connection, "validation"),
    }
    for phase, (store, partition) in expected.items():
        assert _STORE_AUTHORIZER_BY_PHASE[phase] is store, phase
        assert _EXECUTED_PARTITION_BY_PHASE[phase] == partition, phase
    assert "test" not in set(_EXECUTED_PARTITION_BY_PHASE.values())


# --------------------------------------------------------------------------------------------
# the whole lifecycle is routed, not just the claim
# --------------------------------------------------------------------------------------------
def test_no_shared_lifecycle_step_is_hard_bound_to_a_store() -> None:
    """The original defect was six baseline-only sites, not one. Prove none is left.

    Only the four phase-specific PUBLIC entries may name a store directly; every function in the
    shared lifecycle must route through the phase dispatcher, or a Phase-D job could claim on the
    validation store and then try to fail on the baseline.
    """
    import ast
    from pathlib import Path

    source = Path("src/minos_engine/storage/l2f2_runner.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    functions = [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)]

    direct: dict[str, list[int]] = {}
    for line_number, line in enumerate(source.splitlines(), 1):
        if (
            "authorize_baseline_runner_connection(conn)" in line
            or "authorize_validation_runner_connection(conn)" in line
        ):
            owner = max(
                (f for f in functions if f.lineno <= line_number <= (f.end_lineno or f.lineno)),
                key=lambda f: f.lineno,
            )
            direct.setdefault(owner.name, []).append(line_number)

    assert set(direct) == {
        "execute_next_l2f2_phase_b_job",
        "execute_next_l2f2_phase_c_job",
        "execute_next_l2f2_phase_d_job",
    }, direct

    routed = {
        max(
            (f for f in functions if f.lineno <= n <= (f.end_lineno or f.lineno)),
            key=lambda f: f.lineno,
        ).name
        for n, line in enumerate(source.splitlines(), 1)
        if "_authorize_runner_connection_for_phase(conn" in line
    }
    assert {"_execute_l2f2_job", "_release", "_fail", "_complete_success"} <= routed, routed


def test_no_public_execution_entry_accepts_a_store_or_phase() -> None:
    """Store is phase authority, not user input — the public trust surface is unchanged."""
    import inspect

    from minos_engine.storage import l2f2_runner

    for name in (
        "execute_next_l2f2_phase_a_job",
        "execute_next_l2f2_phase_b_job",
        "execute_next_l2f2_phase_c_job",
        "execute_next_l2f2_phase_d_job",
    ):
        parameters = inspect.signature(getattr(l2f2_runner, name)).parameters
        assert list(parameters) == ["worker_id"], (name, list(parameters))
        for forbidden in ("phase", "partition", "database", "store", "revision", "validation"):
            assert forbidden not in parameters, (name, forbidden)


def test_the_dispatcher_is_private() -> None:
    from minos_engine.storage import l2f2_runner

    assert "_authorize_runner_connection_for_phase" not in l2f2_runner.__all__
    assert "_STORE_AUTHORIZER_BY_PHASE" not in l2f2_runner.__all__
