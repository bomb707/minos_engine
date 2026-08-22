"""Local qualification: isolation and PATH-independence.

Two failure modes are worth real proof. The first is a full local qualification that quietly runs
against a database the caller supplied — any database, not just the operational one. The second
is a qualification that silently uses whatever ``ruff``/``mypy``/``minos-engine`` happens to be on
``PATH`` instead of the environment the repository is installed into.

These tests prove the refusal happens before any subprocess starts, that every subprocess receives
a sanitized environment, and that every tool resolves through ``sys.executable``.
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

#: every variable a caller could set to redirect a database connection
ALL_ROUTING_VARS = (lq.ENV_DATABASE_URL, *lq.LIBPQ_ROUTING_VARS)


@pytest.fixture(autouse=True)
def _clean_database_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Start every test from an environment with no database configuration at all."""
    for name in lq.SANITIZED_ENV_VARS:
        monkeypatch.delenv(name, raising=False)


# --------------------------------------------------------------------------- #
# D1/D2 — every caller-supplied database variable is refused
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "url",
    [
        "postgresql://127.0.0.1:5433/minos_engine_db",  # the operational store
        "postgresql://127.0.0.1:5433/some_other_db",  # another db in that cluster
        "postgresql://127.0.0.1:5432/minos_engine_db",  # another port
        "postgresql://db.example.com:5432/scratch",  # another host
        "postgresql+psycopg://u:pw@10.0.0.5:6543/minos_scratch",  # a "scratch" database
        "postgresql://localhost/anything",
    ],
)
def test_any_supplied_database_url_is_refused(monkeypatch: pytest.MonkeyPatch, url: str) -> None:
    """D1: not just the operational coordinates — ANY caller-supplied DSN."""
    monkeypatch.setenv(lq.ENV_DATABASE_URL, url)
    with pytest.raises(lq.ExternalDatabaseRefused):
        lq.assert_isolated_database()


@pytest.mark.parametrize("name", lq.LIBPQ_ROUTING_VARS)
def test_each_libpq_routing_variable_is_refused(monkeypatch: pytest.MonkeyPatch, name: str) -> None:
    """D2: each listed PG variable is rejected on its own."""
    monkeypatch.setenv(name, "value")
    assert lq.find_external_database_vars() == [name]
    with pytest.raises(lq.ExternalDatabaseRefused, match=name):
        lq.assert_isolated_database()


def test_the_documented_variable_set_is_exactly_the_required_one() -> None:
    assert set(lq.LIBPQ_ROUTING_VARS) == {
        "PGHOST",
        "PGHOSTADDR",
        "PGPORT",
        "PGDATABASE",
        "PGUSER",
        "PGPASSWORD",
        "PGSERVICE",
        "PGSERVICEFILE",
        "PGPASSFILE",
        "PGOPTIONS",
    }


def test_several_offenders_are_all_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PGHOST", "h")
    monkeypatch.setenv("PGPORT", "5432")
    monkeypatch.setenv(lq.ENV_DATABASE_URL, "postgresql://x/y")
    assert lq.find_external_database_vars() == [lq.ENV_DATABASE_URL, "PGHOST", "PGPORT"]


def test_an_exported_but_empty_variable_is_tolerated(monkeypatch: pytest.MonkeyPatch) -> None:
    """An empty value cannot route a connection, so it is not treated as configuration."""
    monkeypatch.setenv(lq.ENV_DATABASE_URL, "   ")
    monkeypatch.setenv("PGHOST", "")
    assert lq.find_external_database_vars() == []
    lq.assert_isolated_database()


def test_there_is_no_bypass_flag() -> None:
    """B7: no --allow-external-db or equivalent escape hatch exists.

    Asks the real CLI for its own help rather than instrumenting argparse, so the assertion is
    about the interface a caller actually sees.
    """
    import subprocess

    proc = subprocess.run(
        [sys.executable, "scripts/local_qualification.py", "--help"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    help_text = proc.stdout.lower()
    for banned in ("--allow", "--external", "--force", "--skip-", "--no-check"):
        assert banned not in help_text, banned
    # the only flags are the plan preview and the hidden historical-gate entry point
    assert "--plan-only" in help_text


def test_no_dns_or_connectivity_probe_is_used() -> None:
    """B8: classification is structural; the module must not resolve or connect to anything."""
    source = (REPO_ROOT / "scripts" / "local_qualification.py").read_text(encoding="utf-8")
    for banned in ("socket", "gethostbyname", "getaddrinfo", "psycopg", "create_engine"):
        assert banned not in source, banned


# --------------------------------------------------------------------------- #
# D1/D8 — the refusal precedes plan construction and every subprocess
# --------------------------------------------------------------------------- #
def test_main_refuses_before_building_the_plan_or_launching_anything(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """D1: nothing is planned and nothing is executed."""
    launched: list[Any] = []
    monkeypatch.setattr(lq.subprocess, "run", lambda *a, **k: launched.append(a))
    monkeypatch.setattr(
        lq, "build_plan", lambda *a, **k: pytest.fail("plan built before the refusal")
    )
    monkeypatch.setenv(lq.ENV_DATABASE_URL, "postgresql://db.example.com:5432/scratch")
    assert lq.main([]) == 2
    assert launched == []


@pytest.mark.parametrize("name", ALL_ROUTING_VARS)
def test_plan_only_also_performs_the_safety_check_first(
    monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    """D8: --plan-only is not a way around the check."""
    monkeypatch.setattr(
        lq, "build_plan", lambda *a, **k: pytest.fail("plan built before the refusal")
    )
    monkeypatch.setenv(name, "postgresql://x/y" if name == lq.ENV_DATABASE_URL else "value")
    assert lq.main(["--plan-only"]) == 2


def test_a_clean_environment_reaches_the_plan(monkeypatch: pytest.MonkeyPatch) -> None:
    """D3: with nothing supplied, qualification proceeds."""
    planned: list[str] = []

    def _fake_run(plan: Any, root: Any = REPO_ROOT) -> list[Any]:
        planned.extend(step.name for step in plan.steps)
        return [lq.StepResult(step.name, 0, 0.0) for step in plan.steps]

    monkeypatch.setattr(lq, "run", _fake_run)
    assert lq.main([]) == 0
    assert planned, "the plan was never reached"
    assert any("pytest" in name for name in planned)


# --------------------------------------------------------------------------- #
# D4/D5 — every subprocess gets the sanitized environment
# --------------------------------------------------------------------------- #
def test_sanitized_environment_removes_every_routing_variable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in lq.SANITIZED_ENV_VARS:
        monkeypatch.setenv(name, "leaked")
    monkeypatch.setenv("PATH", "/usr/bin")
    env = lq.sanitized_environment()
    for name in lq.SANITIZED_ENV_VARS:
        assert name not in env, name
    assert env["PATH"] == "/usr/bin", "unrelated variables must survive"


def test_every_subprocess_receives_the_sanitized_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """D4/D5: the env= kwarg is passed on every call, and carries no routing variable."""
    calls: list[dict[str, Any]] = []

    class _Completed:
        returncode = 0

    def _fake_run(argv: Any, **kwargs: Any) -> Any:
        calls.append({"argv": argv, **kwargs})
        return _Completed()

    # a routing variable that somehow survived the check must still not reach a subprocess
    monkeypatch.setattr(lq, "find_external_database_vars", lambda env=None: [])
    monkeypatch.setenv(lq.ENV_DATABASE_URL, "postgresql://sneaky/db")
    monkeypatch.setenv("PGHOST", "sneaky-host")
    monkeypatch.setattr(lq.subprocess, "run", _fake_run)

    results = lq.run(lq.build_plan(REPO_ROOT))
    assert results
    assert calls, "no subprocess was launched"
    for call in calls:
        assert "env" in call, "a subprocess was launched without an explicit environment"
        assert call["shell"] is False
        for name in lq.SANITIZED_ENV_VARS:
            assert name not in call["env"], (call["argv"], name)


def test_the_run_helper_never_inherits_the_ambient_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """env=None would mean 'inherit', which is exactly what must not happen."""
    seen: list[Any] = []

    class _Completed:
        returncode = 0

    monkeypatch.setattr(
        lq.subprocess, "run", lambda argv, **kw: (seen.append(kw.get("env")), _Completed())[1]
    )
    lq.run(lq.build_plan(REPO_ROOT))
    assert seen and all(env is not None for env in seen)


# --------------------------------------------------------------------------- #
# D6/D7 — tools resolve through sys.executable, not PATH
# --------------------------------------------------------------------------- #
def test_every_planned_command_starts_with_this_interpreter() -> None:
    """D6: no step invokes a bare tool name that PATH would have to resolve."""
    for step in lq.build_plan(REPO_ROOT).steps:
        assert step.argv[0] == sys.executable, (step.name, step.argv[0])


def test_python_tools_are_invoked_as_modules() -> None:
    plan = {s.name: s.argv for s in lq.build_plan(REPO_ROOT).steps}
    assert plan["ruff check"][:4] == (sys.executable, "-m", "ruff", "check")
    assert plan["ruff format --check"][:3] == (sys.executable, "-m", "ruff")
    assert plan["mypy (strict)"][:4] == (sys.executable, "-m", "mypy", "src")
    assert plan["pytest full suite + coverage"][:3] == (sys.executable, "-m", "pytest")


def test_gate_commands_do_not_depend_on_the_console_script() -> None:
    """D7: the CLI runs as a module; no `minos-engine` shim needs to be on PATH."""
    gate_steps = [s for s in lq.build_plan(REPO_ROOT).steps if s.name.startswith("gate ")]
    assert gate_steps
    for step in gate_steps:
        assert step.argv[:3] == (sys.executable, "-m", "minos_engine.cli.main"), step.name
        assert "minos-engine" not in step.argv


def test_repository_scripts_run_through_this_interpreter() -> None:
    script_steps = [
        s for s in lq.build_plan(REPO_ROOT).steps if any("scripts/" in a for a in s.argv)
    ]
    assert script_steps
    for step in script_steps:
        assert step.argv[0] == sys.executable
        assert step.argv[1].startswith("scripts/")


# --------------------------------------------------------------------------- #
# the plan itself
# --------------------------------------------------------------------------- #
def test_the_plan_schedules_the_full_suite_exactly_once() -> None:
    plan = lq.build_plan(REPO_ROOT)
    suite_steps = [s for s in plan.steps if "-m" in s.argv and "pytest" in s.argv]
    assert len(suite_steps) == 1, [s.name for s in suite_steps]
    argv = suite_steps[0].argv
    assert "--cov=src/minos_engine" in argv
    assert "--cov-fail-under=90" in argv
    assert any(a.startswith("--junitxml=") for a in argv)
    assert not any(a.startswith("tests/") for a in argv)


def test_the_plan_runs_static_checks_before_the_suite() -> None:
    names = [s.name for s in lq.build_plan(REPO_ROOT).steps]
    suite = next(i for i, n in enumerate(names) if "pytest" in n)
    for tool in ("ruff check", "ruff format --check", "mypy (strict)"):
        assert names.index(tool) < suite, tool


def test_the_plan_runs_gates_after_the_suite() -> None:
    names = [s.name for s in lq.build_plan(REPO_ROOT).steps]
    suite = next(i for i, n in enumerate(names) if "pytest" in n)
    gate_indices = [i for i, n in enumerate(names) if n.startswith("gate ")]
    assert gate_indices and min(gate_indices) > suite


def test_the_plan_includes_dbv2_and_inventory_verification() -> None:
    names = [s.name for s in lq.build_plan(REPO_ROOT).steps]
    assert "DB-V2 contract validation" in names
    assert "test inventory drift" in names


def test_the_plan_never_pushes_commits_or_migrates() -> None:
    forbidden = ("push", "commit", "alembic", "upgrade", "downgrade", "psql", "rm")
    for step in lq.build_plan(REPO_ROOT).steps:
        joined = " ".join(step.argv).lower()
        for token in forbidden:
            assert f" {token} " not in f" {joined} ", (step.name, token)


def test_summarize_reports_failure() -> None:
    ok = [lq.StepResult("a", 0, 0.1), lq.StepResult("b", 0, 0.2)]
    bad = [lq.StepResult("a", 0, 0.1), lq.StepResult("b", 1, 0.2)]
    assert lq.summarize(ok) == 0
    assert lq.summarize(bad) == 1


# --------------------------------------------------------------------------- #
# D9/D10 — nothing else moved
# --------------------------------------------------------------------------- #
def test_historical_gate_verification_is_unchanged() -> None:
    """D9: the three frozen-commit gates still verify, with ci.yml absent from HEAD."""
    assert not (REPO_ROOT / ".github" / "workflows" / "ci.yml").exists()
    assert lq._verify_historical_gates(REPO_ROOT) == 0


def test_fast_ci_remains_the_only_workflow() -> None:
    """D10: ci.yml stays absent and no full workflow reappeared."""
    workflows = sorted((REPO_ROOT / ".github" / "workflows").glob("*.y*ml"))
    assert [p.name for p in workflows] == ["ci-fast.yml"]
