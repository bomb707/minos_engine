"""Local full qualification — the replacement for the deleted GitHub full-CI workflow.

GitHub Actions now runs the fast tier only. Full qualification is a MANUAL, local step, required
at major stage boundaries and before operational changes. It is never wired to a push, a commit
hook, a shell profile or application startup.

    make qualify-local

What it does, in order, stopping at the first failure:

1. ruff check, ruff format --check, mypy
2. the full pytest suite ONCE, with JUnit output and coverage >= 90%
3. the committed gate verifiers
4. the DB-V2 contract validation and the test-inventory drift check

Operational-database safety is the first thing that happens, before any tool starts. The
operational store (127.0.0.1:5433/minos_engine_db) is refused by parsing the DSN — host, port and
database name — not by matching a string. Qualification uses the repository's existing isolated
test-PostgreSQL mechanism (bundled ``pgserver`` scratch clusters created and dropped by the test
fixtures) and never runs Alembic against a caller-supplied DSN.

This script never pushes, commits, migrates or edits a repository file.
"""

from __future__ import annotations

import argparse
import os
import subprocess  # noqa: S404 - fixed argv lists, shell=False, repository-local tools
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import unquote, urlsplit

REPO_ROOT = Path(__file__).resolve().parents[1]

#: The operational store. Qualification must never touch it.
OPERATIONAL_HOSTS = frozenset({"127.0.0.1", "localhost", "::1", "[::1]"})
OPERATIONAL_PORT = 5433
OPERATIONAL_DATABASE = "minos_engine_db"

ENV_DATABASE_URL = "MINOS_DATABASE_URL"


class OperationalDatabaseRefused(RuntimeError):
    """The configured DSN resolves to the operational store; qualification refuses to start."""


@dataclass(frozen=True)
class Dsn:
    """The parts of a database URL that decide whether it is the operational store."""

    host: str | None
    port: int | None
    database: str | None


def parse_dsn(url: str) -> Dsn:
    """Parse a SQLAlchemy/libpq URL into (host, port, database).

    Structural parsing, not substring matching: ``postgresql+psycopg://u@127.0.0.1:5433/
    minos_engine_db?x=1`` and ``postgresql://127.0.0.1:5433/minos_engine_db`` must resolve
    identically, and a database named ``minos_engine_db_scratch`` must NOT be mistaken for the
    operational one.
    """
    split = urlsplit(url)
    host = (split.hostname or "").strip().lower() or None
    try:
        port = split.port
    except ValueError:
        port = None
    database = unquote(split.path).lstrip("/").split("?", 1)[0].strip() or None
    return Dsn(host=host, port=port, database=database)


def is_operational(dsn: Dsn) -> bool:
    """True iff every operational coordinate matches: host AND port AND database name."""
    if dsn.database != OPERATIONAL_DATABASE:
        return False
    if dsn.port != OPERATIONAL_PORT:
        return False
    return (dsn.host or "") in OPERATIONAL_HOSTS


def assert_not_operational(env: dict[str, str] | None = None) -> Dsn | None:
    """Refuse immediately when the environment points at the operational store.

    Runs BEFORE any tool starts, so a refusal costs nothing and cannot half-run a suite.
    """
    environ = os.environ if env is None else env
    raw = (environ.get(ENV_DATABASE_URL) or "").strip()
    if not raw:
        return None
    dsn = parse_dsn(raw)
    if is_operational(dsn):
        raise OperationalDatabaseRefused(
            f"{ENV_DATABASE_URL} resolves to the operational store "
            f"({dsn.host}:{dsn.port}/{dsn.database}). Local qualification must use the isolated "
            f"test-PostgreSQL mechanism; unset {ENV_DATABASE_URL} and re-run."
        )
    return dsn


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


PYTEST_FULL_SUITE: tuple[str, ...] = (
    sys.executable,
    "-m",
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
        ("minos-engine", "gate", "require-pass", "--gate", "gates/protocol-ready.json"),
        "gates/protocol-ready.json",
    ),
    (
        "gate TWIN-READY",
        ("minos-engine", "twin", "gate", "require-pass", "--gate", "gates/twin-ready.json"),
        "gates/twin-ready.json",
    ),
    (
        "gate L1-READY",
        ("minos-engine", "layer1", "gate", "require-pass", "--gate", "gates/l1-ready.json"),
        "gates/l1-ready.json",
    ),
    (
        "gate FEATURE-VIEW-READY",
        (
            "minos-engine",
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
            "minos-engine",
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
    plan.steps.append(Step("ruff check", ("ruff", "check", ".")))
    plan.steps.append(Step("ruff format --check", ("ruff", "format", "--check", ".")))
    plan.steps.append(Step("mypy (strict)", ("mypy", "src")))
    plan.steps.append(Step("pytest full suite + coverage", PYTEST_FULL_SUITE))
    for name, argv, gate in _GATE_STEPS:
        plan.steps.append(Step(name, (*argv, "--base-dir", "."), optional_if_missing=root / gate))
    plan.steps.append(
        Step(
            "gates SPLIT-FROZEN-V2 / INGEST-READY / SNAPSHOT-FROZEN-1",
            (sys.executable, "scripts/local_qualification.py", "--verify-historical-gates"),
        )
    )
    plan.steps.append(
        Step("DB-V2 contract validation", (sys.executable, "scripts/dbv2_audit.py", "validate"))
    )
    plan.steps.append(
        Step("test inventory drift", (sys.executable, "scripts/test_inventory.py", "verify"))
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
    for name, run in checks:
        result = run()
        status = "ok" if result.ok else "FAIL"
        print(f"  {status:4s} {name}" + ("" if result.ok else f" {list(result.reasons)[:3]}"))
        failures += 0 if result.ok else 1
    return 1 if failures else 0


def run(plan: Plan, root: Path = REPO_ROOT) -> list[StepResult]:
    """Execute the plan, stopping at the first failure. shell=False throughout."""
    results: list[StepResult] = []
    for step in plan.steps:
        if step.optional_if_missing is not None and not step.optional_if_missing.exists():
            print(f"[skip] {step.name} (evidence not present)")
            results.append(StepResult(step.name, 0, 0.0, skipped=True))
            continue
        print(f"[run ] {step.name}")
        started = time.monotonic()
        proc = subprocess.run(step.argv, cwd=root, shell=False, check=False)  # noqa: S603
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

    # FIRST: refuse the operational store, before any tool runs.
    try:
        dsn = assert_not_operational()
    except OperationalDatabaseRefused as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 2

    if args.verify_historical_gates:
        return _verify_historical_gates(REPO_ROOT)

    if dsn is None:
        print(f"{ENV_DATABASE_URL} is unset — the isolated test-PostgreSQL mechanism will be used.")
    else:
        print(f"{ENV_DATABASE_URL} -> {dsn.host}:{dsn.port}/{dsn.database} (not operational)")

    plan = build_plan()
    if args.plan_only:
        for i, step in enumerate(plan.steps, 1):
            print(f"{i:2d}. {step.name}: {' '.join(step.argv)}")
        return 0
    return summarize(run(plan))


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    raise SystemExit(main())
