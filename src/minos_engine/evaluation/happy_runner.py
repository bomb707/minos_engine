"""The hap.py execution boundary. No real hap.py runs at L2-F2-A — only the contract exists.

The production runner mirrors the safety properties the GATK runner already proved out in L2-F1:
digest-pinned image, ``shell=False``, fixed argv, bounded timeout, explicit read-only input
mounts, no network in the container, and typed failures. ``FakeHappyRunner`` exists for Tier-2
tests and can never masquerade as the real one.
"""

from __future__ import annotations

import os
import subprocess  # noqa: S404 - fixed argv, shell=False, digest-pinned image
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from minos_engine.common.errors import MinosEngineError

__all__ = [
    "HAPPY_CHILD_ENV_ALLOWLIST",
    "FakeHappyRunner",
    "HappyExecutionError",
    "HappyOutcome",
    "HappyOutputError",
    "HappyRunner",
    "HappyTimeoutError",
    "SubprocessDockerHappyRunner",
    "build_happy_argv",
]

#: the only variables a hap.py container invocation inherits.
HAPPY_CHILD_ENV_ALLOWLIST: tuple[str, ...] = ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "TZ")

_DEFAULT_TIMEOUT_SECONDS = 3600
_MAX_STDERR_BYTES = 1024 * 1024


class HappyExecutionError(MinosEngineError):
    """hap.py exited non-zero, or could not be started."""


class HappyTimeoutError(MinosEngineError):
    """hap.py exceeded its bounded timeout and its container was terminated."""


class HappyOutputError(MinosEngineError):
    """hap.py produced no usable output."""


@dataclass(frozen=True)
class HappyOutcome:
    """What one hap.py invocation produced."""

    exit_code: int
    runtime_ms: int
    output_prefix: Path
    stderr_sha256: str | None = None


class HappyRunner(Protocol):
    """Executes one prepared hap.py comparison."""

    def run(
        self,
        *,
        truth_vcf: Path,
        query_vcf: Path,
        reference: Path,
        region_bed: Path,
        output_prefix: Path,
        work_dir: Path,
    ) -> HappyOutcome: ...


def build_happy_argv(
    *,
    image: str,
    truth_vcf: Path,
    query_vcf: Path,
    reference: Path,
    region_bed: Path,
    output_prefix: Path,
    work_dir: Path,
    threads: int = 1,
) -> tuple[str, ...]:
    """The deterministic container argv.

    Inputs are mounted read-only and individually; only the work directory is writable. The
    container is started with ``--network none`` so an evaluation can never reach the network,
    and the image must be digest-pinned so the comparison is reproducible.
    """
    if "@sha256:" not in image:
        raise HappyExecutionError(
            f"hap.py image {image!r} must be digest-pinned; a tag can be moved underneath us"
        )
    for path in (truth_vcf, query_vcf, reference, region_bed, work_dir):
        if not path.is_absolute():
            raise HappyExecutionError(f"hap.py input {path} must be an absolute path")

    return (
        "docker",
        "run",
        "--rm",
        "--network",
        "none",
        "--read-only",
        "-v",
        f"{truth_vcf.parent}:/truth:ro",
        "-v",
        f"{query_vcf.parent}:/query:ro",
        "-v",
        f"{reference.parent}:/reference:ro",
        "-v",
        f"{region_bed.parent}:/regions:ro",
        "-v",
        f"{work_dir}:/work",
        image,
        f"/truth/{truth_vcf.name}",
        f"/query/{query_vcf.name}",
        "-r",
        f"/reference/{reference.name}",
        "-T",
        f"/regions/{region_bed.name}",
        "-o",
        f"/work/{output_prefix.name}",
        "--threads",
        str(threads),
    )


@dataclass(frozen=True)
class SubprocessDockerHappyRunner:
    """The production runner. Digest-pinned, shell-free, bounded, network-isolated."""

    image: str
    timeout_seconds: int = _DEFAULT_TIMEOUT_SECONDS
    threads: int = 1

    def run(
        self,
        *,
        truth_vcf: Path,
        query_vcf: Path,
        reference: Path,
        region_bed: Path,
        output_prefix: Path,
        work_dir: Path,
    ) -> HappyOutcome:
        import hashlib

        argv = build_happy_argv(
            image=self.image,
            truth_vcf=truth_vcf,
            query_vcf=query_vcf,
            reference=reference,
            region_bed=region_bed,
            output_prefix=output_prefix,
            work_dir=work_dir,
            threads=self.threads,
        )
        env = {k: os.environ[k] for k in HAPPY_CHILD_ENV_ALLOWLIST if k in os.environ}
        started = time.monotonic()
        try:
            proc = subprocess.run(  # noqa: S603 - fixed argv, shell=False, pinned image
                argv,
                capture_output=True,
                check=False,
                env=env,
                cwd=str(work_dir),
                timeout=self.timeout_seconds,
            )
        except subprocess.TimeoutExpired as exc:
            raise HappyTimeoutError(
                f"hap.py exceeded {self.timeout_seconds}s and was terminated"
            ) from exc
        except OSError as exc:
            raise HappyExecutionError(f"hap.py could not be started: {exc}") from exc

        runtime_ms = int((time.monotonic() - started) * 1000)
        stderr = proc.stderr[:_MAX_STDERR_BYTES]
        stderr_sha = hashlib.sha256(stderr).hexdigest() if stderr else None
        if proc.returncode != 0:
            raise HappyExecutionError(f"hap.py exited with code {proc.returncode}")
        return HappyOutcome(
            exit_code=proc.returncode,
            runtime_ms=runtime_ms,
            output_prefix=output_prefix,
            stderr_sha256=stderr_sha,
        )


@dataclass(frozen=True)
class FakeHappyRunner:
    """Deterministic test runner. Starts no container and is never the production boundary."""

    exit_code: int = 0
    runtime_ms: int = 1
    raise_timeout: bool = False
    written_files: dict[str, str] = field(default_factory=dict)

    def run(
        self,
        *,
        truth_vcf: Path,
        query_vcf: Path,
        reference: Path,
        region_bed: Path,
        output_prefix: Path,
        work_dir: Path,
    ) -> HappyOutcome:
        if self.raise_timeout:
            raise HappyTimeoutError("fake hap.py runner simulated a timeout")
        if self.exit_code != 0:
            raise HappyExecutionError(f"fake hap.py runner simulated exit code {self.exit_code}")
        for name, content in self.written_files.items():
            (work_dir / name).write_text(content, encoding="utf-8")
        return HappyOutcome(exit_code=0, runtime_ms=self.runtime_ms, output_prefix=output_prefix)
