"""GATK HaplotypeCaller argv construction and runner port (no execution).

``build_gatk_argv`` renders a deterministic, tokenized argument vector (a list —
never a shell string). It is side-effect-free: it does not spawn a process, open
a container, or touch the filesystem. The runner *port* is defined so a later
stage can plug in a resource-capped executor; Stage 1 ships a disabled runner
(fails closed) and a deterministic fake runner used only in tests.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from pydantic import BaseModel, ConfigDict

from minos_engine.callers.gatk.command import render_flag_args
from minos_engine.common.errors import UnavailableError
from minos_engine.intake.contracts import Region
from minos_engine.twin.unavailable import ReasonCode

__all__ = [
    "build_gatk_argv",
    "GatkRunResult",
    "GatkRunner",
    "DisabledGatkRunner",
    "FakeGatkRunner",
]


def _region_to_gatk(region: Region) -> str:
    # GATK -L uses 1-based inclusive coordinates; Region is 0-based half-open.
    return f"{region.contig}:{region.start0 + 1}-{region.end0_exclusive}"


def build_gatk_argv(
    *,
    effective_config: dict[str, Any],
    region: Region,
    reference_path: str,
    bam_path: str,
    output_path: str,
) -> tuple[str, ...]:
    """Build the deterministic HaplotypeCaller argv (list form; no shell string).

    Paths are passed as individual argv tokens, so spaces / special characters
    are handled safely without shell quoting. Parameter flags follow the Stage 0
    registry ordering for determinism.
    """
    argv: list[str] = [
        "gatk",
        "HaplotypeCaller",
        "-R",
        reference_path,
        "-I",
        bam_path,
        "-L",
        _region_to_gatk(region),
        "-O",
        output_path,
    ]
    argv.extend(render_flag_args(effective_config))
    return tuple(argv)


class GatkRunResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    executed: bool
    vcf_sha256: str | None = None
    runtime_ms: int | None = None
    note: str | None = None


class GatkRunner(ABC):
    """Port for executing a GATK plan. Stage 1 does not run real GATK."""

    @abstractmethod
    def run(self, argv: tuple[str, ...]) -> GatkRunResult: ...


class DisabledGatkRunner(GatkRunner):
    """Default runner: real execution is not enabled in Stage 1 (fails closed)."""

    def run(self, argv: tuple[str, ...]) -> GatkRunResult:
        raise UnavailableError(
            f"{ReasonCode.TOOL_EXECUTION_NOT_ENABLED.value}: real GATK execution is not "
            "enabled in Stage 1 (Validator Twin operates in FIXTURE_REPLAY)"
        )


class FakeGatkRunner(GatkRunner):
    """Deterministic fake runner for tests. It never runs GATK; it labels itself."""

    def __init__(self, vcf_sha256: str) -> None:
        self._vcf_sha256 = vcf_sha256

    def run(self, argv: tuple[str, ...]) -> GatkRunResult:
        return GatkRunResult(
            executed=False,
            vcf_sha256=self._vcf_sha256,
            runtime_ms=0,
            note="fake runner: no real GATK executed",
        )
