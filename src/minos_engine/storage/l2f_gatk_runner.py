"""L2-F F5 GATK invocation building, runners and byte-level VCF validation.

Builds the deterministic TOKENIZED HaplotypeCaller argv (never a shell string), executes it with
``shell=False`` under a provisioned, pinned executable, and validates the produced VCF directly
from its bytes. A runner-supplied output hash is NEVER trusted.

Two runners exist:

* :class:`FakeGatkRunner` — deterministic, writes real VCF bytes; the ONLY runner ordinary tests
  and CI use. No GATK process is ever started.
* :class:`SubprocessGatkRunner` — production. It requires an exact absolute executable path plus
  its expected SHA-256 and expected version (no PATH discovery), runs with ``shell=False``,
  ``stdin=DEVNULL``, a controlled environment allowlist, bounded stdout/stderr files, an isolated
  per-job work directory and a wall-clock timeout that terminates the whole process group. It
  never retries.

**No memory limit is claimed**: this runner does not install an enforced RSS/cgroup cap, so it
deliberately does not pretend to have one.
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import re
import signal
import stat
import subprocess  # noqa: S404 - shell=False, fixed argv, pinned executable (see module docstring)
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from minos_engine.callers.gatk.command import render_flag_args
from minos_engine.experiments.execution_contract import (
    ARGV_BAM_PLACEHOLDER,
    ARGV_OUTPUT_PLACEHOLDER,
    ARGV_REFERENCE_PLACEHOLDER,
    ExecutionInput,
    GatkExecutionError,
    GatkExecutionOutcome,
    GatkInvocationError,
    GatkOutputError,
    GatkTimeoutError,
    LogicalGatkInvocation,
)

__all__ = [
    "ENV_GATK_EXECUTABLE",
    "ENV_GATK_EXECUTABLE_SHA256",
    "ENV_GATK_VERSION",
    "ENV_GATK_TIMEOUT_SECONDS",
    "ENV_WORK_ROOT",
    "MAX_CAPTURED_STREAM_BYTES",
    "GatkRunner",
    "FakeGatkRunner",
    "SubprocessGatkRunner",
    "build_logical_invocation",
    "render_execution_argv",
    "region_token",
    "validate_vcf_bytes",
    "validate_vcf_payload",
    "VCF_FIXED_COLUMNS",
    "VCF_SINGLE_SAMPLE_COLUMN_COUNT",
    "work_root_from_env",
]

ENV_GATK_EXECUTABLE = "MINOS_L2F_GATK_EXECUTABLE"
ENV_GATK_EXECUTABLE_SHA256 = "MINOS_L2F_GATK_EXECUTABLE_SHA256"
ENV_GATK_VERSION = "MINOS_L2F_GATK_VERSION"
ENV_GATK_TIMEOUT_SECONDS = "MINOS_L2F_GATK_TIMEOUT_SECONDS"
ENV_WORK_ROOT = "MINOS_L2F_WORK_ROOT"

#: hard cap on captured stdout/stderr bytes — streams are bounded files, never unbounded buffers.
MAX_CAPTURED_STREAM_BYTES = 1024 * 1024
_CHUNK = 1024 * 1024
_DEFAULT_TIMEOUT_SECONDS = 3600
#: bounded wait for each drain thread to observe EOF after the child has exited or been killed.
_DRAIN_JOIN_SECONDS = 30

#: the ONLY environment variables a GATK child process inherits.
_ENV_ALLOWLIST = ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "JAVA_HOME", "TZ")

_VCF_FILEFORMAT = re.compile(rb"^##fileformat=VCFv4\.\d+\s*$")


def region_token(inputs: ExecutionInput) -> str:
    """The GATK ``-L`` interval token (1-based inclusive) for the accepted region."""
    return f"{inputs.chromosome}:{inputs.region_start0 + 1}-{inputs.region_end0_exclusive}"


def render_execution_argv(
    *,
    effective_config: dict[str, Any],
    inputs: ExecutionInput,
    reference_path: str,
    bam_path: str,
    output_path: str,
) -> tuple[str, ...]:
    """The deterministic tokenized HaplotypeCaller argv (WITHOUT the executable token).

    Every value is a separate argv token, so spaces and shell metacharacters in any value remain
    inert data — no shell is ever involved.
    """
    argv: list[str] = [
        "HaplotypeCaller",
        "-R",
        reference_path,
        "-I",
        bam_path,
        "-L",
        region_token(inputs),
        "-O",
        output_path,
    ]
    argv.extend(render_flag_args(effective_config))
    return tuple(argv)


def build_logical_invocation(
    *,
    effective_config: dict[str, Any],
    inputs: ExecutionInput,
    gatk_executable_sha256: str,
    gatk_version: str,
) -> LogicalGatkInvocation:
    """The HOST-INDEPENDENT logical invocation: real paths replaced by stable placeholders."""
    argv = render_execution_argv(
        effective_config=effective_config,
        inputs=inputs,
        reference_path=ARGV_REFERENCE_PLACEHOLDER,
        bam_path=ARGV_BAM_PLACEHOLDER,
        output_path=ARGV_OUTPUT_PLACEHOLDER,
    )
    for placeholder in (ARGV_REFERENCE_PLACEHOLDER, ARGV_BAM_PLACEHOLDER, ARGV_OUTPUT_PLACEHOLDER):
        if placeholder not in argv:  # pragma: no cover - structural guard
            raise GatkInvocationError(f"logical argv is missing {placeholder}")
    return LogicalGatkInvocation(
        tool="HaplotypeCaller",
        region_token=region_token(inputs),
        logical_argv=argv,
        gatk_executable_sha256=gatk_executable_sha256,
        gatk_version=gatk_version,
    )


def _stream_sha256(path: Path) -> tuple[str, int]:
    """Stream-hash a produced file, rejecting symlinks and mutation during the read."""
    if path.is_symlink():
        raise GatkOutputError(f"produced output {path} is a symlink")
    # O_NONBLOCK so a FIFO/device planted as the output cannot block forever before the
    # regular-file check rejects it; O_NOFOLLOW refuses a symlinked output at the syscall level.
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
    except OSError as exc:
        raise GatkOutputError(f"produced output {path} is unreadable: {exc}") from exc
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode):
            raise GatkOutputError(f"produced output {path} is not a regular file")
        digest = hashlib.sha256()
        size = 0
        while True:
            chunk = os.read(fd, _CHUNK)
            if not chunk:
                break
            digest.update(chunk)
            size += len(chunk)
        after = os.fstat(fd)
    finally:
        os.close(fd)
    if (after.st_size, after.st_ino) != (before.st_size, before.st_ino) or size != after.st_size:
        raise GatkOutputError(f"produced output {path} changed while it was being hashed")
    return digest.hexdigest(), size


#: the exact single-sample VCF column layout: 8 fixed + FORMAT + exactly one sample column.
VCF_FIXED_COLUMNS: tuple[str, ...] = (
    "#CHROM",
    "POS",
    "ID",
    "REF",
    "ALT",
    "QUAL",
    "FILTER",
    "INFO",
    "FORMAT",
)
VCF_SINGLE_SAMPLE_COLUMN_COUNT = len(VCF_FIXED_COLUMNS) + 1


def validate_vcf_payload(payload: bytes, *, inputs: ExecutionInput) -> None:
    """Validate a produced VCF's structure and every record from EXACT bytes (no filesystem).

    This is the byte-level contract used by the descriptor-bound production acquisition, where
    the very bytes that are validated are also the bytes that are hashed and published.
    """
    _validate_vcf_lines(payload.split(b"\n"), label="produced VCF", inputs=inputs)


def _validate_vcf_structure(vcf_path: Path, inputs: ExecutionInput) -> None:
    """Validate the produced VCF's structure and every record, strictly from its bytes.

    Requires exactly one ``#CHROM`` header laying out a SINGLE-SAMPLE VCF, and requires every
    data record to carry that same column count, an integer ``POS``, the accepted chromosome and
    a position inside the accepted half-open interval ``[region_start0, region_end0_exclusive)``
    (1-based ``POS`` in ``[region_start0 + 1, region_end0_exclusive]``).
    """
    with vcf_path.open("rb") as fh:
        _validate_vcf_lines(list(fh), label=str(vcf_path), inputs=inputs)


def _validate_vcf_lines(lines: list[bytes], *, label: str, inputs: ExecutionInput) -> None:
    """The single structural contract, shared by the path-based and byte-based validators."""
    vcf_path = label
    chrom_headers = 0
    columns: int | None = None
    low = inputs.region_start0 + 1
    high = inputs.region_end0_exclusive
    for index, raw in enumerate(lines):
        line = raw.rstrip(b"\r\n")
        if not line and index > 0:
            continue  # a trailing newline is not a record
        if index == 0:
            if not _VCF_FILEFORMAT.match(line):
                raise GatkOutputError(
                    f"produced VCF {vcf_path} does not begin with a ##fileformat=VCFv4.x header"
                )
            continue
        if line.startswith(b"#CHROM"):
            chrom_headers += 1
            fields = line.split(b"\t")
            header = tuple(f.decode("utf-8", errors="replace") for f in fields)
            if header[: len(VCF_FIXED_COLUMNS)] != VCF_FIXED_COLUMNS:
                raise GatkOutputError(
                    f"produced VCF {vcf_path} has a malformed #CHROM header layout"
                )
            if len(header) != VCF_SINGLE_SAMPLE_COLUMN_COUNT:
                raise GatkOutputError(
                    f"produced VCF {vcf_path} is not single-sample: the #CHROM header declares "
                    f"{len(header)} columns, expected {VCF_SINGLE_SAMPLE_COLUMN_COUNT}"
                )
            columns = len(header)
            continue
        if line.startswith(b"#"):
            continue
        if columns is None:
            raise GatkOutputError(
                f"produced VCF {vcf_path} contains a record before its #CHROM header"
            )
        fields = line.split(b"\t")
        if len(fields) != columns:
            raise GatkOutputError(
                f"produced VCF {vcf_path} record has {len(fields)} columns, expected {columns}"
            )
        chrom = fields[0].decode("utf-8", errors="replace")
        if chrom != inputs.chromosome:
            raise GatkOutputError(
                f"produced VCF {vcf_path} contains record chromosome {chrom!r}, expected "
                f"{inputs.chromosome!r}"
            )
        raw_pos = fields[1].decode("utf-8", errors="replace")
        try:
            pos = int(raw_pos)
        except ValueError as exc:
            raise GatkOutputError(
                f"produced VCF {vcf_path} record POS {raw_pos!r} is not an integer"
            ) from exc
        if not (low <= pos <= high):
            raise GatkOutputError(
                f"produced VCF {vcf_path} record POS {pos} is outside the accepted interval "
                f"[{low}, {high}]"
            )
    if chrom_headers != 1:
        raise GatkOutputError(
            f"produced VCF {vcf_path} must contain exactly one #CHROM header, found {chrom_headers}"
        )


def validate_vcf_bytes(
    vcf_path: Path, *, work_dir: Path, inputs: ExecutionInput
) -> tuple[str, int]:
    """Validate the produced VCF directly from its BYTES and return ``(sha256, size)``.

    Never trusts a runner-supplied hash. Requires a regular, non-symlink file inside the job work
    directory, nonempty, a valid ``##fileformat=VCFv4.x`` first line, exactly one single-sample
    ``#CHROM`` header, and records that are well-formed, on the accepted chromosome and inside the
    accepted interval. A region with no variant records is legitimate.
    """
    if vcf_path.is_symlink():
        raise GatkOutputError(f"produced VCF {vcf_path} is a symlink")
    if not vcf_path.exists():
        raise GatkOutputError(f"produced VCF {vcf_path} does not exist")
    resolved = vcf_path.resolve()
    work = work_dir.resolve()
    if work not in resolved.parents:
        raise GatkOutputError(f"produced VCF {vcf_path} is outside the job work directory")

    sha, size = _stream_sha256(vcf_path)
    if size == 0:
        raise GatkOutputError(f"produced VCF {vcf_path} is empty")
    _validate_vcf_structure(vcf_path, inputs)
    return sha, size


class GatkRunner(Protocol):
    """Executes one prepared HaplotypeCaller invocation and returns its byte-verified outcome."""

    def run(
        self,
        *,
        argv: tuple[str, ...],
        work_dir: Path,
        vcf_path: Path,
        inputs: ExecutionInput,
    ) -> GatkExecutionOutcome: ...


@dataclass(frozen=True)
class FakeGatkRunner:
    """Deterministic test runner: writes REAL, deterministic VCF bytes. Never starts a process."""

    gatk_version: str = "fake-gatk-4.5.0.0"
    exit_code: int = 0
    runtime_ms: int = 1
    #: when set, these exact bytes are written instead of the deterministic valid VCF.
    override_bytes: bytes | None = None
    write_output: bool = True
    raise_timeout: bool = False

    def _deterministic_vcf(self, inputs: ExecutionInput) -> bytes:
        return (
            "##fileformat=VCFv4.2\n"
            f"##reference={inputs.reference_sha256}\n"
            f"##contig=<ID={inputs.chromosome}>\n"
            "#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\tFORMAT\tSAMPLE\n"
            f"{inputs.chromosome}\t{inputs.region_start0 + 1}\t.\tA\tG\t50.0\tPASS\t.\tGT\t0/1\n"
        ).encode()

    def run(
        self,
        *,
        argv: tuple[str, ...],
        work_dir: Path,
        vcf_path: Path,
        inputs: ExecutionInput,
    ) -> GatkExecutionOutcome:
        if self.raise_timeout:
            raise GatkTimeoutError("fake runner simulated a GATK timeout")
        if self.exit_code != 0:
            raise GatkExecutionError(f"fake runner simulated exit code {self.exit_code}")
        if self.write_output:
            payload = (
                self.override_bytes
                if self.override_bytes is not None
                else self._deterministic_vcf(inputs)
            )
            vcf_path.write_bytes(payload)
        sha, size = validate_vcf_bytes(vcf_path, work_dir=work_dir, inputs=inputs)
        return GatkExecutionOutcome(
            exit_code=0, runtime_ms=self.runtime_ms, vcf_sha256=sha, vcf_size_bytes=size
        )


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        while chunk := fh.read(_CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


class _BoundedDrain(threading.Thread):
    """Continuously drain one child pipe in CONSTANT memory, storing at most ``limit`` bytes.

    Reading never stops before EOF (so the child can never block on a full pipe), but everything
    past ``limit`` is discarded instead of buffered or written. ``digest`` is the SHA-256 of the
    bytes actually captured on disk, so it always matches the retained file exactly.
    """

    def __init__(self, pipe: Any, path: Path, limit: int) -> None:
        super().__init__(daemon=True)
        self._pipe = pipe
        self._path = path
        self._limit = limit
        self.captured_bytes = 0
        self.total_bytes = 0
        self.truncated = False
        self.digest: str | None = None

    def run(self) -> None:  # pragma: no cover - exercised through SubprocessGatkRunner.run
        hasher = hashlib.sha256()
        try:
            with self._path.open("wb") as sink:
                while True:
                    chunk = self._pipe.read(_CHUNK)
                    if not chunk:
                        break
                    self.total_bytes += len(chunk)
                    room = self._limit - self.captured_bytes
                    if room > 0:
                        keep = chunk[:room]
                        sink.write(keep)
                        hasher.update(keep)
                        self.captured_bytes += len(keep)
                    if len(chunk) > max(room, 0):
                        self.truncated = True
        except (OSError, ValueError):
            self.truncated = True
        finally:
            with contextlib.suppress(Exception):
                self._pipe.close()
            self.digest = hasher.hexdigest()


@dataclass(frozen=True)
class SubprocessGatkRunner:
    """Production runner: pinned absolute executable, ``shell=False``, bounded streams, timeout."""

    executable: Path
    expected_sha256: str
    expected_version: str
    timeout_seconds: int = _DEFAULT_TIMEOUT_SECONDS
    #: hard cap on bytes retained per captured stream (enforced DURING the run, not afterwards).
    max_captured_stream_bytes: int = MAX_CAPTURED_STREAM_BYTES

    @staticmethod
    def from_env() -> SubprocessGatkRunner:
        """Build from the PROVISIONED environment (no caller-provided executable or version)."""
        raw = os.environ.get(ENV_GATK_EXECUTABLE, "").strip()
        sha = os.environ.get(ENV_GATK_EXECUTABLE_SHA256, "").strip()
        version = os.environ.get(ENV_GATK_VERSION, "").strip()
        if not raw or not sha or not version:
            raise GatkExecutionError(
                f"{ENV_GATK_EXECUTABLE}, {ENV_GATK_EXECUTABLE_SHA256} and {ENV_GATK_VERSION} must "
                "all be provisioned (no PATH-based executable discovery is performed)"
            )
        timeout = int(os.environ.get(ENV_GATK_TIMEOUT_SECONDS, _DEFAULT_TIMEOUT_SECONDS))
        return SubprocessGatkRunner(
            executable=Path(raw),
            expected_sha256=sha,
            expected_version=version,
            timeout_seconds=timeout,
        )

    def _verify_executable(self) -> None:
        path = self.executable
        if not path.is_absolute():
            raise GatkExecutionError(f"GATK executable {path} must be an absolute path")
        if path.is_symlink():
            raise GatkExecutionError(f"GATK executable {path} is a symlink")
        if not path.is_file():
            raise GatkExecutionError(f"GATK executable {path} is not a regular file")
        actual = _sha256_file(path)
        if actual != self.expected_sha256:
            raise GatkExecutionError(
                f"GATK executable {path} sha256 {actual} != expected {self.expected_sha256}"
            )

    def run(
        self,
        *,
        argv: tuple[str, ...],
        work_dir: Path,
        vcf_path: Path,
        inputs: ExecutionInput,
    ) -> GatkExecutionOutcome:
        self._verify_executable()
        env = {k: os.environ[k] for k in _ENV_ALLOWLIST if k in os.environ}
        stdout_path = work_dir / "gatk.stdout"
        stderr_path = work_dir / "gatk.stderr"
        limit = self.max_captured_stream_bytes
        started = time.monotonic()
        with open(os.devnull, "rb") as devnull:
            proc = subprocess.Popen(  # noqa: S603 - fixed argv, shell=False, pinned executable
                [str(self.executable), *argv],
                cwd=str(work_dir),
                env=env,
                stdin=devnull,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                shell=False,
                start_new_session=True,  # its own process group, so a timeout kills all children
            )
            # Drain BOTH pipes continuously in constant memory while the process runs: at most
            # ``limit`` bytes ever reach disk and at most one chunk is ever held in memory, so a
            # process that emits gigabytes cannot exhaust memory OR disk, and cannot deadlock on
            # a full pipe either. Truncating after exit would bound neither.
            assert proc.stdout is not None and proc.stderr is not None  # noqa: S101 - PIPE above
            drains = [
                _BoundedDrain(proc.stdout, stdout_path, limit),
                _BoundedDrain(proc.stderr, stderr_path, limit),
            ]
            for drain in drains:
                drain.start()
            try:
                exit_code = proc.wait(timeout=self.timeout_seconds)
            except subprocess.TimeoutExpired as exc:
                self._terminate_group(proc)
                for drain in drains:
                    drain.join(_DRAIN_JOIN_SECONDS)
                raise GatkTimeoutError(
                    f"GATK exceeded {self.timeout_seconds}s and its process group was terminated"
                ) from exc
            finally:
                for drain in drains:
                    drain.join(_DRAIN_JOIN_SECONDS)
        runtime_ms = int((time.monotonic() - started) * 1000)
        stderr_sha = drains[1].digest
        if exit_code != 0:
            raise GatkExecutionError(f"GATK exited with code {exit_code}")
        if not vcf_path.exists():
            raise GatkOutputError("GATK produced no output VCF")
        sha, size = validate_vcf_bytes(vcf_path, work_dir=work_dir, inputs=inputs)
        return GatkExecutionOutcome(
            exit_code=exit_code,
            runtime_ms=runtime_ms,
            vcf_sha256=sha,
            vcf_size_bytes=size,
            stderr_sha256=stderr_sha,
        )

    @staticmethod
    def _terminate_group(proc: subprocess.Popen[bytes]) -> None:
        """Terminate the COMPLETE process group; never leave orphaned GATK children."""
        for sig in (signal.SIGTERM, signal.SIGKILL):
            try:
                os.killpg(os.getpgid(proc.pid), sig)
            except (ProcessLookupError, PermissionError):  # pragma: no cover - already gone
                return
            try:
                proc.wait(timeout=10)
                return
            except subprocess.TimeoutExpired:  # pragma: no cover - escalate to SIGKILL
                continue


def work_root_from_env() -> Path:
    raw = os.environ.get(ENV_WORK_ROOT)
    if raw is None or not raw.strip():
        raise GatkExecutionError(f"{ENV_WORK_ROOT} is not set; the work root must be provisioned")
    root = Path(raw.strip())
    if root.is_symlink() or not root.is_dir():
        raise GatkExecutionError(f"work root {root} must be an existing non-symlink directory")
    return root.resolve(strict=True)
