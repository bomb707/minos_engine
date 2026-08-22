"""Local full qualification — the replacement for the deleted GitHub full-CI workflow.

GitHub Actions runs the fast tier only. Full qualification is a MANUAL, local step, required at
major stage boundaries and before operational changes. It is never wired to a push, a commit
hook, a shell profile or application startup.

    make qualify-local

What it does, in order, stopping at the first failure:

1. ruff check, ruff format --check, mypy
2. the full pytest suite ONCE, with JUnit output and coverage >= 90%
3. the committed gate verifiers
4. the DB-V2 contract validation and the test-inventory drift check

Database isolation
------------------
Qualification refuses **every** externally supplied database configuration — not merely the
operational coordinates. ``MINOS_DATABASE_URL`` must be unset, and so must the libpq routing
variables (``PGHOST``, ``PGPORT``, ``PGDATABASE``, ``PGSERVICE``, ...). A caller-supplied DSN is
forbidden whatever it points at: another host, another port, a scratch database, or another
database inside the operational cluster. There is no bypass flag, and no DNS or connectivity
probe is used to argue that some supplied database is "safe enough" — the rule is structural.

PostgreSQL is therefore always provisioned by the repository's own isolated test fixtures
(bundled ``pgserver`` scratch clusters, created and dropped per test). As defence in depth, every
subprocess is launched with a sanitized environment from which all database-routing variables
have been removed, so a variable that somehow escaped the check still cannot reach a test.

Tool resolution
---------------
Tools are invoked as ``sys.executable -m <module>``, never by bare name, so qualification does
not depend on ``.venv/bin`` being on the caller's ``PATH``.

This script never pushes, commits, migrates or edits a repository file.
"""

from __future__ import annotations

import argparse
import os
import subprocess  # noqa: S404 - fixed argv lists, shell=False, interpreter-resolved modules
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

ENV_DATABASE_URL = "MINOS_DATABASE_URL"

#: libpq routing variables. Any of these can silently redirect a connection, so a caller-supplied
#: value is refused and none is ever passed to a subprocess.
LIBPQ_ROUTING_VARS: tuple[str, ...] = (
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
)

#: Everything stripped from the subprocess environment. Includes the libpq variables above plus
#: the application DSN and a few further libpq knobs that can influence connection routing.
SANITIZED_ENV_VARS: tuple[str, ...] = (
    ENV_DATABASE_URL,
    *LIBPQ_ROUTING_VARS,
    "DATABASE_URL",
    "PGSSLMODE",
    "PGSSLROOTCERT",
    "PGCONNECT_TIMEOUT",
    "PGREQUIRESSL",
    "PGCHANNELBINDING",
    "PGTARGETSESSIONATTRS",
)


class ExternalDatabaseRefused(RuntimeError):
    """A caller-supplied database configuration was found; qualification refuses to start."""


def find_external_database_vars(env: dict[str, str] | None = None) -> list[str]:
    """Names of caller-supplied database variables, in a stable order.

    A variable counts as supplied when it is present with a non-empty value. Emptiness is the
    only tolerance: an exported-but-empty variable cannot route a connection.
    """
    environ = os.environ if env is None else env
    found = [
        name for name in (ENV_DATABASE_URL, *LIBPQ_ROUTING_VARS) if environ.get(name, "").strip()
    ]
    return found


def assert_isolated_database(env: dict[str, str] | None = None) -> None:
    """Refuse if ANY database configuration was supplied by the caller.

    Runs before the plan is built and before any subprocess starts, so a refusal costs nothing
    and can never half-run a suite against the wrong database.
    """
    offenders = find_external_database_vars(env)
    if not offenders:
        return
    listed = ", ".join(offenders)
    raise ExternalDatabaseRefused(
        f"caller-supplied database configuration is not permitted: {listed}. "
        "Local qualification always provisions PostgreSQL through the repository's isolated "
        f"test fixtures. Unset {listed} and re-run. There is no override."
    )


def sanitized_environment(env: dict[str, str] | None = None) -> dict[str, str]:
    """A copy of the environment with every database-routing variable removed.

    Defence in depth: even if a variable were somehow missed by the check above, it cannot reach
    a subprocess. Everything else the toolchain needs (PATH, HOME, VIRTUAL_ENV, ...) is kept.
    """
    environ = dict(os.environ if env is None else env)
    for name in SANITIZED_ENV_VARS:
        environ.pop(name, None)
    return environ


# --------------------------------------------------------------------------- #
# tool resolution: always through the interpreter running this script
# --------------------------------------------------------------------------- #
def python_module(module: str, *args: str) -> tuple[str, ...]:
    """``sys.executable -m module ...`` — never a bare tool name from PATH."""
    return (sys.executable, "-m", module, *args)


def cli(*args: str) -> tuple[str, ...]:
    """The MINOS CLI as a module, so no console-script shim needs to be on PATH."""
    return python_module("minos_engine.cli.main", *args)


def repo_script(relpath: str, *args: str) -> tuple[str, ...]:
    return (sys.executable, relpath, *args)


# --------------------------------------------------------------------------- #
# the planned command sequence
# --------------------------------------------------------------------------- #
@dataclass
class Step:
    """One command in the qualification sequence."""

    name: str
    argv: tuple[str, ...]
    optional_if_missing: Path | None = None


@dataclass
class StepResult:
    name: str
    returncode: int
    seconds: float
    skipped: bool = False


@dataclass
class Plan:
    steps: list[Step] = field(default_factory=list)


PYTEST_FULL_SUITE: tuple[str, ...] = python_module(
    "pytest",
    "--junitxml=reports/ci-junit.xml",
    "--cov=src/minos_engine",
    "--cov-fail-under=90",
    "--cov-report=term-missing",
    "--cov-report=xml:reports/ci-coverage.xml",
)

_GATE_STEPS: tuple[tuple[str, tuple[str, ...], str], ...] = (
    (
        "gate PROTOCOL-READY",
        ("gate", "require-pass", "--gate", "gates/protocol-ready.json"),
        "gates/protocol-ready.json",
    ),
    (
        "gate TWIN-READY",
        ("twin", "gate", "require-pass", "--gate", "gates/twin-ready.json"),
        "gates/twin-ready.json",
    ),
    (
        "gate L1-READY",
        ("layer1", "gate", "require-pass", "--gate", "gates/l1-ready.json"),
        "gates/l1-ready.json",
    ),
    (
        "gate FEATURE-VIEW-READY",
        (
            "layer2",
            "feature-view",
            "gate",
            "require-pass",
            "--gate",
            "gates/feature-view-ready.json",
        ),
        "gates/feature-view-ready.json",
    ),
    (
        "gate FEATURE-MATRIX-FROZEN-1",
        (
            "layer2",
            "feature-matrix",
            "gate",
            "require-pass",
            "--gate",
            "gates/feature-matrix-frozen-1.json",
        ),
        "gates/feature-matrix-frozen-1.json",
    ),
)


def build_plan(root: Path = REPO_ROOT) -> Plan:
    """The exact ordered command sequence. Pure: builds the plan, runs nothing."""
    plan = Plan()
    plan.steps.append(Step("ruff check", python_module("ruff", "check", ".")))
    plan.steps.append(Step("ruff format --check", python_module("ruff", "format", "--check", ".")))
    plan.steps.append(Step("mypy (strict)", python_module("mypy", "src")))
    plan.steps.append(Step("pytest full suite + coverage", PYTEST_FULL_SUITE))
    for name, args, gate in _GATE_STEPS:
        plan.steps.append(
            Step(name, cli(*args, "--base-dir", "."), optional_if_missing=root / gate)
        )
    plan.steps.append(
        Step(
            "gates SPLIT-FROZEN-V2 / INGEST-READY / SNAPSHOT-FROZEN-1",
            repo_script("scripts/local_qualification.py", "--verify-historical-gates"),
        )
    )
    plan.steps.append(
        Step("DB-V2 contract validation", repo_script("scripts/dbv2_audit.py", "validate"))
    )
    plan.steps.append(
        Step("test inventory drift", repo_script("scripts/test_inventory.py", "verify"))
    )
    return plan


def _verify_historical_gates(root: Path) -> int:
    """The three gates whose evidence is read from frozen commits, not from HEAD."""
    from minos_engine.qualification.layer2_ingest_runner import verify_ingest_ready_gate
    from minos_engine.qualification.layer2_snapshot_runner import verify_snapshot_offline
    from minos_engine.qualification.layer2_split_v2_runner import verify_split_frozen_v2_gate

    failures = 0
    checks = (
        (
            "SPLIT-FROZEN-V2",
            lambda: verify_split_frozen_v2_gate(root, root / "gates" / "split-frozen-v2.json"),
        ),
        (
            "INGEST-READY",
            lambda: verify_ingest_ready_gate(
                root, root / "gates" / "ingest-ready.json", require_descends=True
            ),
        ),
        ("PROFILE-SNAPSHOT-FROZEN-1", lambda: verify_snapshot_offline(root, 1)),
    )
    for name, run_check in checks:
        result = run_check()
        status = "ok" if result.ok else "FAIL"
        print(f"  {status:4s} {name}" + ("" if result.ok else f" {list(result.reasons)[:3]}"))
        failures += 0 if result.ok else 1
    return 1 if failures else 0


def run(plan: Plan, root: Path = REPO_ROOT) -> list[StepResult]:
    """Execute the plan, stopping at the first failure.

    Every subprocess gets the SANITIZED environment explicitly and runs with ``shell=False``.
    """
    env = sanitized_environment()
    results: list[StepResult] = []
    for step in plan.steps:
        if step.optional_if_missing is not None and not step.optional_if_missing.exists():
            print(f"[skip] {step.name} (evidence not present)")
            results.append(StepResult(step.name, 0, 0.0, skipped=True))
            continue
        print(f"[run ] {step.name}")
        started = time.monotonic()
        proc = subprocess.run(  # noqa: S603 - fixed argv, shell=False, sanitized env
            step.argv, cwd=root, shell=False, check=False, env=env
        )
        elapsed = time.monotonic() - started
        results.append(StepResult(step.name, proc.returncode, elapsed))
        if proc.returncode != 0:
            break
    return results


def summarize(results: list[StepResult]) -> int:
    print("\n" + "=" * 62)
    print("LOCAL QUALIFICATION SUMMARY")
    print("=" * 62)
    failed = 0
    for r in results:
        if r.skipped:
            print(f"  skip  {r.name}")
            continue
        mark = "ok  " if r.returncode == 0 else "FAIL"
        print(f"  {mark}  {r.name}  ({r.seconds:.1f}s)")
        failed += 0 if r.returncode == 0 else 1
    total = sum(r.seconds for r in results)
    print("-" * 62)
    verdict = "QUALIFIED" if failed == 0 else "NOT QUALIFIED"
    print(f"  {verdict} — {len(results)} step(s), {total:.1f}s total")
    return 1 if failed else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="MINOS local full qualification (manual)")
    parser.add_argument(
        "--plan-only", action="store_true", help="print the planned sequence and exit"
    )
    parser.add_argument("--verify-historical-gates", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)

    # FIRST, for every mode including --plan-only: refuse any caller-supplied database
    # configuration, before the plan is built and before any tool runs.
    try:
        assert_isolated_database()
    except ExternalDatabaseRefused as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 2

    if args.verify_historical_gates:
        return _verify_historical_gates(REPO_ROOT)

    print(
        "database environment is clean — PostgreSQL will be provisioned by the isolated "
        "test fixtures"
    )
    plan = build_plan()
    if args.plan_only:
        for i, step in enumerate(plan.steps, 1):
            print(f"{i:2d}. {step.name}: {' '.join(step.argv)}")
        return 0
    return summarize(run(plan))


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    raise SystemExit(main())
