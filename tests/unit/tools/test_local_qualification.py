"""Local qualification safety and plan — the operational store must be refused first.

The dangerous failure mode is a full local qualification that quietly runs Alembic or pytest
against the operational PostgreSQL. These tests prove the refusal happens *before* any tool is
launched, and that the refusal is decided by parsing the DSN rather than matching a substring.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]

_spec = importlib.util.spec_from_file_location(
    "local_qualification", REPO_ROOT / "scripts" / "local_qualification.py"
)
assert _spec is not None and _spec.loader is not None
lq = importlib.util.module_from_spec(_spec)
# dataclasses resolve their module from sys.modules, so register before executing.
sys.modules["local_qualification"] = lq
_spec.loader.exec_module(lq)


# --------------------------------------------------------------------------- #
# DSN parsing is structural, not textual
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "url",
    [
        "postgresql://127.0.0.1:5433/minos_engine_db",
        "postgresql+psycopg://postgres@127.0.0.1:5433/minos_engine_db",
        "postgresql+psycopg://postgres:secret@127.0.0.1:5433/minos_engine_db",
        "postgresql+psycopg://postgres@127.0.0.1:5433/minos_engine_db?sslmode=disable",
        "postgresql://localhost:5433/minos_engine_db",
        "postgresql://LOCALHOST:5433/minos_engine_db",
    ],
)
def test_operational_dsns_are_recognized(url: str) -> None:
    assert lq.is_operational(lq.parse_dsn(url)) is True


@pytest.mark.parametrize(
    ("url", "why"),
    [
        ("postgresql://127.0.0.1:5432/minos_engine_db", "different port"),
        ("postgresql://127.0.0.1:5433/minos_engine_db_scratch", "different database"),
        ("postgresql://127.0.0.1:5433/minos_scratch", "different database"),
        ("postgresql://db.example.com:5433/minos_engine_db", "different host"),
        ("postgresql://127.0.0.1:5433/", "no database"),
    ],
)
def test_non_operational_dsns_are_allowed(url: str, why: str) -> None:
    assert lq.is_operational(lq.parse_dsn(url)) is False, why


def test_a_scratch_database_whose_name_contains_the_operational_name_is_allowed() -> None:
    """Substring matching would wrongly refuse this; structural parsing must not."""
    dsn = lq.parse_dsn("postgresql://127.0.0.1:5433/minos_engine_db_test_scratch")
    assert dsn.database == "minos_engine_db_test_scratch"
    assert lq.is_operational(dsn) is False


def test_parse_dsn_extracts_host_port_database() -> None:
    dsn = lq.parse_dsn("postgresql+psycopg://user:pw@example.org:6543/somedb?x=1")
    assert (dsn.host, dsn.port, dsn.database) == ("example.org", 6543, "somedb")


# --------------------------------------------------------------------------- #
# the refusal happens before any tool starts
# --------------------------------------------------------------------------- #
def test_the_operational_dsn_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(lq.ENV_DATABASE_URL, "postgresql://127.0.0.1:5433/minos_engine_db")
    with pytest.raises(lq.OperationalDatabaseRefused):
        lq.assert_not_operational()


def test_main_refuses_before_launching_any_subprocess(monkeypatch: pytest.MonkeyPatch) -> None:
    """12: nothing is executed — no pytest, no Alembic, no gate verifier."""
    launched: list[Any] = []
    monkeypatch.setattr(lq.subprocess, "run", lambda *a, **k: launched.append(a))
    monkeypatch.setattr(lq, "build_plan", lambda *a, **k: pytest.fail("plan built before refusal"))
    monkeypatch.setenv(
        lq.ENV_DATABASE_URL, "postgresql+psycopg://postgres@127.0.0.1:5433/minos_engine_db"
    )
    assert lq.main([]) == 2
    assert launched == []


def test_an_unset_dsn_is_allowed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(lq.ENV_DATABASE_URL, raising=False)
    assert lq.assert_not_operational() is None


def test_a_safe_isolated_dsn_reaches_the_planned_sequence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """13: a non-operational configuration proceeds to the plan."""
    monkeypatch.setenv(lq.ENV_DATABASE_URL, "postgresql://127.0.0.1:59999/minos_scratch")
    planned: list[str] = []

    def _fake_run(plan: Any, root: Any = REPO_ROOT) -> list[Any]:
        planned.extend(step.name for step in plan.steps)
        return [lq.StepResult(step.name, 0, 0.0) for step in plan.steps]

    monkeypatch.setattr(lq, "run", _fake_run)
    assert lq.main([]) == 0
    assert planned, "the plan was never reached"
    assert any("pytest" in name for name in planned)


# --------------------------------------------------------------------------- #
# the plan itself
# --------------------------------------------------------------------------- #
def test_the_plan_schedules_the_full_suite_exactly_once() -> None:
    """14: only one step invokes the full pytest suite."""
    plan = lq.build_plan(REPO_ROOT)
    suite_steps = [s for s in plan.steps if "-m" in s.argv and "pytest" in s.argv]
    assert len(suite_steps) == 1, [s.name for s in suite_steps]
    argv = suite_steps[0].argv
    assert "--cov=src/minos_engine" in argv
    assert "--cov-fail-under=90" in argv
    assert any(a.startswith("--junitxml=") for a in argv)
    # no path selector: the whole suite, not a subset
    assert not any(a.startswith("tests/") for a in argv)


def test_the_plan_runs_static_checks_before_the_suite() -> None:
    names = [s.name for s in lq.build_plan(REPO_ROOT).steps]
    suite = next(i for i, n in enumerate(names) if "pytest" in n)
    for tool in ("ruff check", "ruff format --check", "mypy (strict)"):
        assert names.index(tool) < suite, tool


def test_the_plan_runs_gates_after_the_suite() -> None:
    """3: committed gate verifiers run after the test suite."""
    names = [s.name for s in lq.build_plan(REPO_ROOT).steps]
    suite = next(i for i, n in enumerate(names) if "pytest" in n)
    gate_indices = [i for i, n in enumerate(names) if n.startswith("gate")]
    assert gate_indices, names
    assert min(gate_indices) > suite


def test_the_plan_includes_dbv2_and_inventory_verification() -> None:
    names = [s.name for s in lq.build_plan(REPO_ROOT).steps]
    assert "DB-V2 contract validation" in names
    assert "test inventory drift" in names


def test_the_plan_never_pushes_commits_or_migrates() -> None:
    """7: no step may mutate the repository or a database."""
    forbidden = ("push", "commit", "alembic", "upgrade", "downgrade", "psql", "rm")
    for step in lq.build_plan(REPO_ROOT).steps:
        joined = " ".join(step.argv).lower()
        for token in forbidden:
            assert f" {token} " not in f" {joined} ", (step.name, token)


def test_every_planned_command_runs_without_a_shell() -> None:
    for step in lq.build_plan(REPO_ROOT).steps:
        assert isinstance(step.argv, tuple)
        assert all(isinstance(a, str) for a in step.argv)


def test_summarize_reports_failure() -> None:
    ok = [lq.StepResult("a", 0, 0.1), lq.StepResult("b", 0, 0.2)]
    bad = [lq.StepResult("a", 0, 0.1), lq.StepResult("b", 1, 0.2)]
    assert lq.summarize(ok) == 0
    assert lq.summarize(bad) == 1
