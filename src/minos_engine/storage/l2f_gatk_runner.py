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

The production launcher is a ``#!/usr/bin/env python`` script, so under the original policy GATK
started only if the worker's ambient ``PATH`` happened to contain a command named ``python``. It
did not on one worker, and five Phase-A jobs were recorded as candidate failures for configs GATK
never parsed. The corrected policy (``l2f-gatk-child-env-v2``) therefore invokes the launcher
through an EXPLICITLY provisioned, content-verified interpreter — ``[python, launcher, *argv]`` —
so the shebang and the ambient ``PATH`` are irrelevant to whether a scientific job can run, and
pins ``JAVA_HOME`` the same way.

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
    GatkNonzeroExitError,
    GatkOutputError,
    GatkRuntimeIdentityError,
    GatkTimeoutError,
    LogicalGatkInvocation,
)
from minos_engine.experiments.execution_environment import (
    CHILD_ENVIRONMENT_POLICY_VERSION,
    GatkExecutionEnvironment,
)

__all__ = [
    "CHILD_ENV_ALLOWLIST",
    "ENV_GATK_EXECUTABLE",
    "ENV_GATK_EXECUTABLE_SHA256",
    "ENV_GATK_PYTHON",
    "ENV_GATK_PYTHON_SHA256",
    "ENV_GATK_VERSION",
    "ENV_GATK_TIMEOUT_SECONDS",
    "ENV_JAVA_HOME",
    "ENV_WORK_ROOT",
    "GATK_JAR_OVERRIDE_VARIABLES",
    "MAX_CAPTURED_STREAM_BYTES",
    "GatkRunner",
    "FakeGatkRunner",
    "SubprocessGatkRunner",
    "build_logical_invocation",
    "render_execution_argv",
    "region_token",
    "validate_vcf_bytes",
    "validate_vcf_payload",
    "resolve_official_local_jar",
    "VCF_FIXED_COLUMNS",
    "VCF_SINGLE_SAMPLE_COLUMN_COUNT",
    "work_root_from_env",
]

ENV_GATK_EXECUTABLE = "MINOS_L2F_GATK_EXECUTABLE"
ENV_GATK_EXECUTABLE_SHA256 = "MINOS_L2F_GATK_EXECUTABLE_SHA256"
#: the interpreter the launcher is executed BY. Provisioned explicitly and verified by content,
#: never discovered through PATH, ``which``, ``/usr/bin/env`` or ``sys.executable``.
ENV_GATK_PYTHON = "MINOS_L2F_GATK_PYTHON"
ENV_GATK_PYTHON_SHA256 = "MINOS_L2F_GATK_PYTHON_SHA256"
ENV_GATK_VERSION = "MINOS_L2F_GATK_VERSION"
ENV_JAVA_HOME = "JAVA_HOME"
ENV_GATK_TIMEOUT_SECONDS = "MINOS_L2F_GATK_TIMEOUT_SECONDS"
ENV_WORK_ROOT = "MINOS_L2F_WORK_ROOT"

#: the official launcher selects its JAR from these variables BEFORE searching its own directory.
#: Neither is in the child allowlist, so a substituted payload can never reach the child; the
#: names are kept here so the exclusion is explicit and testable.
GATK_JAR_OVERRIDE_VARIABLES: tuple[str, ...] = ("GATK_LOCAL_JAR", "GATK_SPARK_JAR")
#: the launcher's own selection pattern for the scientific payload.
_LOCAL_JAR_RE = re.compile(r"^gatk.*local\.jar$")

#: hard cap on captured stdout/stderr bytes — streams are bounded files, never unbounded buffers.
MAX_CAPTURED_STREAM_BYTES = 1024 * 1024
_CHUNK = 1024 * 1024
_DEFAULT_TIMEOUT_SECONDS = 3600
#: bounded wait for each drain thread to observe EOF after the child has exited or been killed.
_DRAIN_JOIN_SECONDS = 30

#: the ONLY environment variables a GATK child process inherits. Deliberately excludes every
#: JAR-override variable, so the launcher cannot be steered at a substituted scientific payload.
CHILD_ENV_ALLOWLIST: tuple[str, ...] = (
    "PATH",
    "HOME",
    "LANG",
    "LC_ALL",
    "TMPDIR",
    "JAVA_HOME",
    "TZ",
)

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
    gatk_runtime_bundle_sha256: str,
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
        gatk_runtime_bundle_sha256=gatk_runtime_bundle_sha256,
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
    """Executes one prepared HaplotypeCaller invocation and returns its byte-verified outcome.

    ``expected_runtime_bundle_sha256`` is the execution-bundle identity ALREADY frozen into the
    invocation, and therefore into ``result_hash``. It is passed explicitly rather than read from
    runner state or a global, so the value the result is identified by and the value the run
    boundary enforces are provably the same object.
    """

    def run(
        self,
        *,
        argv: tuple[str, ...],
        work_dir: Path,
        vcf_path: Path,
        inputs: ExecutionInput,
        expected_runtime_bundle_sha256: str,
        expected_execution_environment_hash: str = "",
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
    #: the stream digest a real nonzero exit would carry. None means "this fake produced no
    #: stderr", which is the truthful default: it starts no process.
    stderr_sha256: str | None = None

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
        #: accepted to satisfy the protocol ONLY. This runner starts no process and owns no GATK
        #: bundle, so it deliberately does not verify it — pretending to would be a false claim.
        #: It can never satisfy ``official_gatk_runner_used``; it is test-only.
        expected_runtime_bundle_sha256: str = "",
        #: likewise: this runner executes no interpreter and no JVM, so it establishes no runtime
        #: identity and does not pretend to check one.
        expected_execution_environment_hash: str = "",
    ) -> GatkExecutionOutcome:
        if self.raise_timeout:
            raise GatkTimeoutError("fake runner simulated a GATK timeout")
        if self.exit_code != 0:
            # the SAME structured exception the production runner raises, so the classification
            # and the evidence that reach persistence are the ones production would produce.
            raise GatkNonzeroExitError(
                f"fake runner simulated exit code {self.exit_code}",
                exit_code=self.exit_code,
                stderr_sha256=self.stderr_sha256,
                runtime_ms=self.runtime_ms,
            )
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


def resolve_official_local_jar(launcher: Path, *, gatk_version: str) -> Path:
    """Resolve the local JAR the OFFICIAL launcher would actually run. Fails closed.

    The launcher searches its own directory for ``^gatk.*local\\.jar$`` and runs the newest match,
    so this reproduces that selection and refuses anything ambiguous: a missing JAR, more than one
    candidate, a symlink, a non-regular file or a name that does not carry the pinned version.
    A caller cannot nominate an arbitrary file as "the GATK JAR".
    """
    parent = launcher.parent
    candidates = sorted(p for p in parent.iterdir() if _LOCAL_JAR_RE.match(p.name))
    if not candidates:
        raise GatkExecutionError(
            f"no official local GATK JAR (^gatk.*local.jar$) beside the launcher in {parent}"
        )
    if len(candidates) > 1:
        raise GatkExecutionError(
            f"ambiguous local GATK JARs beside the launcher: {[p.name for p in candidates]}; "
            "the launcher would pick the newest, so F7 refuses rather than guess"
        )
    jar = candidates[0]
    expected_name = f"gatk-package-{gatk_version}-local.jar"
    if jar.name != expected_name:
        raise GatkExecutionError(
            f"local GATK JAR {jar.name!r} does not match the pinned version layout "
            f"{expected_name!r}"
        )
    if jar.is_symlink():
        raise GatkExecutionError(f"local GATK JAR {jar} is a symlink")
    if not jar.is_file():
        raise GatkExecutionError(f"local GATK JAR {jar} is not a regular file")
    return jar


def _stable_sha256(path: Path) -> str:
    """Stream-hash a file and reject mutation during the read (size/inode re-checked)."""
    if path.is_symlink():
        raise GatkExecutionError(f"{path} is a symlink")
    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode):
            raise GatkExecutionError(f"{path} is not a regular file")
        digest = hashlib.sha256()
        size = 0
        while chunk := os.read(fd, _CHUNK):
            digest.update(chunk)
            size += len(chunk)
        after = os.fstat(fd)
    finally:
        os.close(fd)
    if (after.st_size, after.st_ino) != (before.st_size, before.st_ino) or size != after.st_size:
        raise GatkExecutionError(f"{path} changed while it was being hashed")
    return digest.hexdigest()


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
    #: the interpreter the launcher is executed BY. The launcher is a ``#!/usr/bin/env python``
    #: script, so under the old policy it started only if the ambient PATH happened to provide a
    #: ``python``; here it is an explicit, content-verified absolute executable and the shebang is
    #: never consulted.
    launcher_python: Path
    expected_python_sha256: str
    #: the provisioned JDK. ``java_home/bin/java`` is resolved without any PATH lookup.
    java_home: Path
    timeout_seconds: int = _DEFAULT_TIMEOUT_SECONDS
    #: the scientific payload the official launcher will run. Resolved fail-closed from the
    #: launcher's own directory; never nominated by a caller.
    local_jar: Path | None = None
    #: hard cap on bytes retained per captured stream (enforced DURING the run, not afterwards).
    max_captured_stream_bytes: int = MAX_CAPTURED_STREAM_BYTES

    @staticmethod
    def from_env() -> SubprocessGatkRunner:
        """Build from the PROVISIONED environment (no discovery of ANY executable).

        Every executable this runner will start — the launcher, the interpreter that runs it and
        the JVM the launcher starts — must be named explicitly and pinned by content. Nothing is
        located through ``PATH``, ``shutil.which``, ``/usr/bin/env`` or ``sys.executable``: an
        interpreter chosen by the worker's shell is exactly how a runtime defect became five
        candidate-failure observations.
        """
        raw = os.environ.get(ENV_GATK_EXECUTABLE, "").strip()
        sha = os.environ.get(ENV_GATK_EXECUTABLE_SHA256, "").strip()
        version = os.environ.get(ENV_GATK_VERSION, "").strip()
        if not raw or not sha or not version:
            raise GatkExecutionError(
                f"{ENV_GATK_EXECUTABLE}, {ENV_GATK_EXECUTABLE_SHA256} and {ENV_GATK_VERSION} must "
                "all be provisioned (no PATH-based executable discovery is performed)"
            )
        python_raw = os.environ.get(ENV_GATK_PYTHON, "").strip()
        python_sha = os.environ.get(ENV_GATK_PYTHON_SHA256, "").strip()
        if not python_raw or not python_sha:
            raise GatkExecutionError(
                f"{ENV_GATK_PYTHON} and {ENV_GATK_PYTHON_SHA256} must be provisioned: the GATK "
                "launcher is a '#!/usr/bin/env python' script and production must never let the "
                "ambient PATH decide which interpreter runs it"
            )
        java_home_raw = os.environ.get(ENV_JAVA_HOME, "").strip()
        if not java_home_raw:
            raise GatkExecutionError(
                f"{ENV_JAVA_HOME} must be provisioned; the JVM is never located through PATH"
            )
        timeout = int(os.environ.get(ENV_GATK_TIMEOUT_SECONDS, _DEFAULT_TIMEOUT_SECONDS))
        launcher = Path(raw)
        return SubprocessGatkRunner(
            executable=launcher,
            expected_sha256=sha,
            expected_version=version,
            launcher_python=Path(python_raw),
            expected_python_sha256=python_sha,
            java_home=Path(java_home_raw),
            timeout_seconds=timeout,
            local_jar=resolve_official_local_jar(launcher, gatk_version=version)
            if launcher.is_absolute() and launcher.parent.is_dir()
            else None,
        )

    def runtime_bundle_sha256(self) -> str:
        """The frozen execution-bundle identity: launcher bytes + local JAR bytes + version.

        Streams both files and re-checks size/inode, so a payload swapped during hashing is
        rejected rather than silently averaged over.
        """
        from minos_engine.experiments.execution_contract import (
            compute_gatk_runtime_bundle_sha256,
        )

        if self.local_jar is None:
            raise GatkExecutionError(
                "the official local GATK JAR was not resolved; the launcher alone is a dispatcher "
                "and cannot stand for the scientific payload"
            )
        return compute_gatk_runtime_bundle_sha256(
            launcher_sha256=_stable_sha256(self.executable),
            local_jar_sha256=_stable_sha256(self.local_jar),
            gatk_version=self.expected_version,
        )

    def _child_env(self) -> dict[str, str]:
        """The allowlisted child environment. Nothing outside the allowlist ever reaches GATK."""
        return {k: os.environ[k] for k in CHILD_ENV_ALLOWLIST if k in os.environ}

    def _launch_argv(self, *tokens: str) -> list[str]:
        """``[interpreter, launcher, *tokens]`` — the launcher's shebang is never consulted."""
        return [str(self.launcher_python), str(self.executable), *tokens]

    @property
    def java_binary(self) -> Path:
        """``JAVA_HOME/bin/java``, resolved WITHOUT any PATH lookup."""
        return self.java_home / "bin" / "java"

    def observe_python_version(self) -> str:
        """Bounded probe of the EXPLICIT interpreter's own version. Starts no GATK."""
        self._verify_python()
        proc = subprocess.run(  # noqa: S603 - pinned interpreter, fixed argv, shell=False
            [str(self.launcher_python), "--version"],
            capture_output=True,
            text=True,
            check=False,
            env=self._child_env(),
            timeout=min(self.timeout_seconds, 300),
        )
        blob = f"{proc.stdout}\n{proc.stderr}".strip()
        match = re.search(r"Python\s+([0-9][0-9A-Za-z.\-]*)", blob)
        if not match:
            raise GatkRuntimeIdentityError(
                f"the provisioned launcher interpreter did not report a version: {blob[:200]!r}"
            )
        return match.group(1)

    def observe_java_version(self) -> str:
        """Bounded probe of the provisioned JVM. Starts no GATK."""
        self._verify_java()
        proc = subprocess.run(  # noqa: S603 - pinned java binary, fixed argv, shell=False
            [str(self.java_binary), "-version"],
            capture_output=True,
            text=True,
            check=False,
            env=self._child_env(),
            timeout=min(self.timeout_seconds, 300),
        )
        # every JDK prints its version banner on stderr.
        blob = f"{proc.stdout}\n{proc.stderr}".strip()
        match = re.search(r'version\s+"?([0-9][0-9A-Za-z._\-+]*)"?', blob)
        if not match:
            raise GatkRuntimeIdentityError(
                f"the provisioned JVM did not report a version: {blob[:200]!r}"
            )
        return match.group(1)

    def observe_version(self) -> str:
        """Bounded, offline ``gatk --version`` probe under the SAME restricted child environment.

        HaplotypeCaller is never executed here, and the launcher is started through the EXPLICIT
        interpreter, so this probe answers "can this worker run GATK at all" without depending on
        whether some ``python`` happens to be on PATH.
        """
        self._verify_executable()
        self._verify_python()
        env = self._child_env()
        proc = subprocess.run(  # noqa: S603 - pinned executable, fixed argv, shell=False
            self._launch_argv("--version"),
            capture_output=True,
            text=True,
            check=False,
            env=env,
            timeout=min(self.timeout_seconds, 600),
        )
        blob = f"{proc.stdout}\n{proc.stderr}"
        if proc.returncode != 0:
            # a process that FAILED reported nothing. Without this the version could be scraped
            # out of an error message that merely quotes it — an interpreter's SyntaxError echoing
            # the banner line would "confirm" a launcher that cannot run at all.
            raise GatkRuntimeIdentityError(
                f"the GATK bundle exited {proc.returncode} instead of reporting its version: "
                f"{blob.strip()[:200]!r}"
            )
        match = re.search(r"\(GATK\)\s+v([0-9][0-9A-Za-z.\-]*)", blob)
        if not match:
            raise GatkExecutionError(
                f"the GATK bundle did not report a recognizable version: {blob.strip()[:200]!r}"
            )
        return match.group(1)

    def _require_runtime_bundle(self, expected: str, *, when: str) -> None:
        """Fail closed unless the CURRENT execution bundle equals the frozen expected identity.

        Bounded honesty: this proves the launcher and JAR bytes were the pinned ones immediately
        before the process started and immediately after it exited, and that the child cannot be
        redirected through the known JAR-override variables. It does not — and does not claim to —
        defeat a privileged attacker able to swap bytes transiently and restore them.
        """
        if not expected:
            raise GatkExecutionError(
                "no expected GATK runtime bundle was supplied; the execution identity must be "
                "frozen before execution, never derived from whatever happens to be on disk"
            )
        actual = self.runtime_bundle_sha256()
        if actual != expected:
            raise GatkExecutionError(
                f"GATK runtime bundle {when} is {actual}, but the frozen execution identity is "
                f"{expected}; the scientific payload changed and this job cannot be identified "
                "by that bundle"
            )

    def _verify_python(self) -> None:
        """The launcher interpreter must be the EXACT provisioned bytes, checked every time."""
        path = self.launcher_python
        if not path.is_absolute():
            raise GatkRuntimeIdentityError(f"launcher interpreter {path} must be an absolute path")
        if path.is_symlink():
            raise GatkRuntimeIdentityError(
                f"launcher interpreter {path} is a symlink; a symlink can be re-pointed between "
                "the check and the run, so the interpreter must be named directly"
            )
        if not path.is_file():
            raise GatkRuntimeIdentityError(f"launcher interpreter {path} is not a regular file")
        if not os.access(path, os.X_OK):
            raise GatkRuntimeIdentityError(f"launcher interpreter {path} is not executable")
        actual = _sha256_file(path)
        if actual != self.expected_python_sha256:
            raise GatkRuntimeIdentityError(
                f"launcher interpreter {path} sha256 {actual} != expected "
                f"{self.expected_python_sha256}"
            )

    def _verify_java(self) -> None:
        """The JVM must be the provisioned one, resolved from JAVA_HOME and never from PATH."""
        home = self.java_home
        if not home.is_absolute():
            raise GatkRuntimeIdentityError(f"JAVA_HOME {home} must be an absolute path")
        if not home.is_dir():
            raise GatkRuntimeIdentityError(f"JAVA_HOME {home} is not an existing directory")
        java = self.java_binary
        if not java.is_file():
            raise GatkRuntimeIdentityError(f"{java} does not exist")
        if not os.access(java, os.X_OK):
            raise GatkRuntimeIdentityError(f"{java} is not executable")

    def java_sha256(self) -> str:
        """The content identity of the provisioned ``java`` binary."""
        self._verify_java()
        return _stable_sha256(self.java_binary)

    def launcher_python_sha256(self) -> str:
        """The content identity of the provisioned launcher interpreter."""
        self._verify_python()
        return _stable_sha256(self.launcher_python)

    def execution_environment(self) -> GatkExecutionEnvironment:
        """Derive the CURRENT runtime identity, verifying every component's bytes as it goes.

        This is a measurement, never a declaration: each field is re-derived from the files as
        they are right now, so a runtime that moved between two calls produces two different
        hashes rather than one comfortable constant.
        """
        return GatkExecutionEnvironment(
            gatk_launcher_sha256=_stable_sha256(self.executable),
            gatk_runtime_bundle_sha256=self.runtime_bundle_sha256(),
            gatk_version=self.expected_version,
            launcher_python_sha256=self.launcher_python_sha256(),
            launcher_python_version=self.observe_python_version(),
            java_sha256=self.java_sha256(),
            java_version=self.observe_java_version(),
            child_environment_policy_version=CHILD_ENVIRONMENT_POLICY_VERSION,
        )

    def preflight(self) -> GatkExecutionEnvironment:
        """Prove this worker can run GATK AT ALL, before any scientific job is at stake.

        Verifies the launcher, the scientific payload bundle, the explicit interpreter and the
        provisioned JVM by content, then runs the real ``gatk --version`` through that exact
        interpreter and requires the pinned version. A worker that cannot pass this must never
        consume a candidate observation, so the caller runs it BEFORE claiming a job.
        """
        self._verify_executable()
        self._verify_python()
        self._verify_java()
        environment = self.execution_environment()
        observed = self.observe_version()
        if observed != self.expected_version:
            raise GatkRuntimeIdentityError(
                f"the GATK bundle reports version {observed!r}, but this worker is provisioned "
                f"for {self.expected_version!r}"
            )
        return environment

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

    def _require_execution_environment(self, expected: str, *, when: str) -> None:
        """Fail closed unless the CURRENT runtime identity is the one this run is bound to.

        A runtime that changes across a job — a re-provisioned interpreter, a swapped JDK, a
        different bundle — invalidates the result's provenance. That is OUR failure, so it raises
        a runtime-identity error and never a GATK execution error, which is what keeps it from
        being charged to the candidate.
        """
        if not expected:
            return  # the caller did not bind an environment identity to this run
        actual = self.execution_environment().environment_hash()
        if actual != expected:
            raise GatkRuntimeIdentityError(
                f"the execution environment {when} is {actual}, but this execution is identified "
                f"by {expected}; the runtime moved and its output cannot be attributed"
            )

    def run(
        self,
        *,
        argv: tuple[str, ...],
        work_dir: Path,
        vcf_path: Path,
        inputs: ExecutionInput,
        expected_runtime_bundle_sha256: str,
        expected_execution_environment_hash: str = "",
    ) -> GatkExecutionOutcome:
        self._verify_executable()
        self._verify_python()
        self._verify_java()
        # THE check/use boundary. The bundle observed at qualification time is minutes old by now;
        # re-derive it from the launcher and JAR bytes as they are RIGHT NOW and refuse to start
        # HaplotypeCaller unless it still equals the identity already frozen into result_hash.
        self._require_runtime_bundle(expected_runtime_bundle_sha256, when="before execution")
        self._require_execution_environment(
            expected_execution_environment_hash, when="before execution"
        )
        env = self._child_env()
        stdout_path = work_dir / "gatk.stdout"
        stderr_path = work_dir / "gatk.stderr"
        limit = self.max_captured_stream_bytes
        started = time.monotonic()
        with open(os.devnull, "rb") as devnull:
            proc = subprocess.Popen(  # noqa: S603 - fixed argv, shell=False, pinned executable
                # the EXPLICIT interpreter, then the launcher: the launcher's own
                # '#!/usr/bin/env python' shebang is never consulted, so a worker whose PATH has
                # no 'python' still runs the identical scientific process.
                self._launch_argv(*argv),
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
        stdout_sha = drains[0].digest
        if exit_code != 0:
            # the evidence travels WITH the exception. A caller that has to re-run the job to
            # learn the exit code cannot diagnose anything from the durable ledger.
            raise GatkNonzeroExitError(
                f"GATK exited with code {exit_code}",
                exit_code=exit_code,
                stderr_sha256=stderr_sha,
                stdout_sha256=stdout_sha,
                runtime_ms=runtime_ms,
            )
        # the bundle must ALSO be unchanged now, before any produced VCF is accepted: a run whose
        # payload moved underneath it is not scientifically identified by the frozen bundle.
        self._require_runtime_bundle(expected_runtime_bundle_sha256, when="after execution")
        self._require_execution_environment(
            expected_execution_environment_hash, when="after execution"
        )
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
