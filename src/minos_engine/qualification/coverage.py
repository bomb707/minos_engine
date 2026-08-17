"""Coverage measurement and threshold enforcement (Cobertura XML).

We read the machine-readable coverage XML (``lines-covered`` / ``lines-valid``)
rather than terminal output. A coverage-execution failure or unreadable report
must fail the gate — callers treat a raised error as a failed check.
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path
from xml.etree import ElementTree as ET

from pydantic import BaseModel, ConfigDict

from minos_engine.common.errors import MinosEngineError

__all__ = ["CoverageResult", "STAGE0_COVERAGE_THRESHOLD", "parse_coverage_xml", "run_coverage"]

STAGE0_COVERAGE_THRESHOLD = 90.0


class CoverageResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    line_coverage_percent: float
    covered_lines: int
    valid_lines: int
    missing_lines: int
    tool: str

    def meets(self, threshold: float = STAGE0_COVERAGE_THRESHOLD) -> bool:
        return self.line_coverage_percent >= threshold


def parse_coverage_xml(xml_text: str, *, tool: str = "coverage.py") -> CoverageResult:
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        raise MinosEngineError(f"could not parse coverage XML: {exc}") from exc

    covered = root.get("lines-covered")
    valid = root.get("lines-valid")
    if covered is None or valid is None:
        # Fall back to line-rate when counts are absent.
        rate = root.get("line-rate")
        if rate is None:
            raise MinosEngineError("coverage XML missing lines-covered/lines-valid/line-rate")
        percent = float(rate) * 100.0
        return CoverageResult(
            line_coverage_percent=percent,
            covered_lines=0,
            valid_lines=0,
            missing_lines=0,
            tool=tool,
        )

    covered_i = int(covered)
    valid_i = int(valid)
    percent = (covered_i / valid_i * 100.0) if valid_i else 0.0
    return CoverageResult(
        line_coverage_percent=round(percent, 2),
        covered_lines=covered_i,
        valid_lines=valid_i,
        missing_lines=max(valid_i - covered_i, 0),
        tool=tool,
    )


def run_coverage(root: Path) -> tuple[CoverageResult, str]:  # pragma: no cover - subprocess glue
    """Run pytest under coverage and return ``(result, coverage_xml_text)``."""
    with tempfile.TemporaryDirectory() as tmp:
        xml_path = Path(tmp) / "coverage.xml"
        cmd = [
            sys.executable,
            "-m",
            "pytest",
            "--cov=src/minos_engine",
            f"--cov-report=xml:{xml_path}",
            "-p",
            "no:cacheprovider",
        ]
        proc = subprocess.run(cmd, cwd=str(root), capture_output=True, text=True, check=False)
        if not xml_path.exists():
            raise MinosEngineError(
                f"coverage run produced no XML (exit={proc.returncode}); "
                f"stderr tail: {proc.stderr[-500:]}"
            )
        xml_text = xml_path.read_text(encoding="utf-8")
        return parse_coverage_xml(xml_text), xml_text
