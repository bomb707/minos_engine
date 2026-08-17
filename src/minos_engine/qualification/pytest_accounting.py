"""Machine-readable pytest accounting via JUnit XML.

We never parse terminal presentation (``-q`` can suppress the ``N passed``
summary — the original defect). Instead we run pytest with ``--junitxml`` and
read the structured ``tests/failures/errors/skipped`` attributes plus the
process exit code. ``parse_junit_xml`` is separated from the subprocess so it is
unit-testable with synthetic XML.
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path
from xml.etree import ElementTree as ET

from pydantic import BaseModel, ConfigDict

from minos_engine.common.errors import MinosEngineError

__all__ = ["PytestAccounting", "parse_junit_xml", "suite_passes", "run_pytest"]


class PytestAccounting(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    collected: int
    passed: int
    failed: int
    errors: int
    skipped: int
    duration_seconds: float
    exit_code: int

    def as_report_row(self) -> dict[str, object]:
        return {
            "collected": self.collected,
            "passed": self.passed,
            "failed": self.failed,
            "errors": self.errors,
            "skipped": self.skipped,
            "duration_seconds": round(self.duration_seconds, 3),
            "exit_code": self.exit_code,
        }


def parse_junit_xml(xml_text: str, *, exit_code: int) -> PytestAccounting:
    """Parse a JUnit XML string into structured accounting.

    Handles both a ``<testsuites>`` root and a bare ``<testsuite>`` root, summing
    across every ``testsuite`` element.
    """
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        raise MinosEngineError(f"could not parse JUnit XML: {exc}") from exc

    suites = list(root.iter("testsuite"))
    if not suites:
        raise MinosEngineError("JUnit XML contains no <testsuite> element")

    collected = failed = errors = skipped = 0
    duration = 0.0
    for s in suites:
        collected += int(s.get("tests", "0"))
        failed += int(s.get("failures", "0"))
        errors += int(s.get("errors", "0"))
        skipped += int(s.get("skipped", "0"))
        duration += float(s.get("time", "0") or 0.0)

    passed = collected - failed - errors - skipped
    return PytestAccounting(
        collected=collected,
        passed=max(passed, 0),
        failed=failed,
        errors=errors,
        skipped=skipped,
        duration_seconds=duration,
        exit_code=exit_code,
    )


def suite_passes(acc: PytestAccounting) -> bool:
    """The test suite qualifies only when it collected tests and all passed."""
    return acc.exit_code == 0 and acc.collected > 0 and acc.failed == 0 and acc.errors == 0


def run_pytest(  # pragma: no cover - subprocess glue (recurses if run in-suite)
    root: Path, *, extra_args: list[str] | None = None
) -> PytestAccounting:
    """Run pytest with a JUnit report and return structured accounting."""
    with tempfile.TemporaryDirectory() as tmp:
        junit = Path(tmp) / "junit.xml"
        cmd = [sys.executable, "-m", "pytest", f"--junitxml={junit}"]
        if extra_args:
            cmd.extend(extra_args)
        proc = subprocess.run(cmd, cwd=str(root), capture_output=True, text=True, check=False)
        if not junit.exists():
            raise MinosEngineError(
                f"pytest did not produce a JUnit report (exit={proc.returncode}); "
                f"stderr tail: {proc.stderr[-500:]}"
            )
        return parse_junit_xml(junit.read_text(encoding="utf-8"), exit_code=proc.returncode)
