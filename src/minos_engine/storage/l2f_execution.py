"""L2-F F5 execution orchestration: claim -> run GATK -> terminal outcome.

``execute_next_accepted_job(*, worker_id)`` is the sole production entry point. It accepts no
caller-provided plan, hashes, database, paths, CONFIG, candidate set, partition, runner or trust
bundle: every accepted identity is constructed internally and every location comes from a
provisioned environment variable. It never scores, never reads truth/mutation/hap.py data, never
touches the legacy ``experiments.jobs``/``results``/``profiling.profiles``/``catalog.gatk_configs``
tables, and never retries automatically.

Exact-connection authorization
------------------------------
There is no preliminary "authorize once, use many connections" step. Every live connection the
production path opens — claim, preparation reads, start, release, success persistence, failure
persistence — runs :func:`verify_operational_database_identity` and requires the live revision to
be exactly ``0008_l2f_execution_results`` as its FIRST statements, before any other query,
publication or mutation. The explicit-trust scratch boundary passes
``require_operational_identity=False``; the accepted boundary passes ``True``. A successful check
on one connection therefore never authorizes another connection.
"""

from __future__ import annotations

import contextlib
import datetime as _dt
import hashlib
import os
import stat
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from sqlalchemy import Connection, Engine, text
from sqlalchemy.exc import DBAPIError

from minos_engine.experiments.execution_contract import (
    EXECUTION_RESULT_SCHEMA,
    ExecutionConfig,
    ExecutionFailure,
    ExecutionInput,
    ExecutionResultManifest,
    GatkExecutionError,
    GatkExecutionOutcome,
    GatkInvocationError,
    GatkOutputError,
    GatkTimeoutError,
    L2FExecutionError,
    LogicalGatkInvocation,
    build_result_manifest_bytes,
    compute_result_hash,
)
from minos_engine.storage.database import create_db_engine, verify_operational_database_identity
from minos_engine.storage.l2f_execution_config import load_accepted_execution_config
from minos_engine.storage.l2f_execution_contract import L2F_EXECUTION_REVISION
from minos_engine.storage.l2f_execution_inputs import (
    DatasetRoot,
    dataset_root_from_env,
    resolve_accepted_execution_input,
)
from minos_engine.storage.l2f_gatk_runner import (
    FakeGatkRunner,
    GatkRunner,
    SubprocessGatkRunner,
    build_logical_invocation,
    render_execution_argv,
    work_root_from_env,
)
from minos_engine.storage.l2f_job_claim import (
    AmbiguousClaimCommitError,
    InvalidJobTransitionError,
    JobPlanMissingError,
    _claim_next_job_with_trust,
    _release_job_with_trust,
    _start_job_with_trust,
    validate_worker_id,
)
from minos_engine.storage.l2f_plan_store import (
    ArtifactMetadataConflictError,
    PlanRevisionError,
    _build_accepted_plan,
)
from minos_engine.storage.l2f_result_publisher import (
    PublishedResultArtifact,
    ResultArtifactPublisher,
    result_artifact_root_from_env,
)
from minos_engine.storage.roles import SCHEMA_OWNER

if TYPE_CHECKING:
    from minos_engine.experiments.plan import ExperimentPlan

__all__ = [
    "F5_EXECUTION_REVISION",
    "ATTEMPT_DIR_MODE",
    "AttemptWorkspace",
    "reject_symlinked_components",
    "verify_produced_output",
    "acquire_produced_output",
    "AcquiredOutput",
    "OUTPUT_VCF_NAME",
    "ExecutionDispatchResult",
    "AmbiguousExecutionCommitError",
    "ExecutionResultConflictError",
    "ExecutionWorkspaceError",
    "AmbiguousStartCommitError",
    "AmbiguousRecoveryCommitError",
    "PreTerminalExecutionError",
    "ExecutionRecordedFailureError",
    "ExecutionRecoveryError",
    "PostCommitWrapperError",
    "find_nonterminal_jobs",
    "assert_no_stranded_jobs",
    "execute_next_accepted_job",
]

F5_EXECUTION_REVISION = L2F_EXECUTION_REVISION
#: every per-attempt work directory is created private to the executing user.
ATTEMPT_DIR_MODE = 0o700
#: the single produced-output name, resolved ONLY relative to the retained attempt descriptor.
OUTPUT_VCF_NAME = "output.vcf"
_OUTPUT_CHUNK = 1024 * 1024

_SQLSTATE_RESULT_CONFLICT = "MN022"
_SQLSTATE_DUAL_OUTCOME = "MN021"
_SQLSTATE_MISSING_RECORD = "MN020"
_SQLSTATE_NOT_OWNED = "MN003"


class AmbiguousExecutionCommitError(L2FExecutionError):
    """The COMMIT itself raised: the outcome is unknown. Artifacts are RETAINED; never retried."""


class ExecutionResultConflictError(L2FExecutionError):
    """A differing durable result/failure already exists for this job."""


class ExecutionWorkspaceError(L2FExecutionError):
    """The per-attempt work directory could not be created exclusively, or was substituted."""


class AmbiguousStartCommitError(AmbiguousExecutionCommitError):
    """The CLAIMED -> RUNNING commit was ambiguous. GATK is NEVER executed; never retried."""


class AmbiguousRecoveryCommitError(AmbiguousExecutionCommitError):
    """The recovery (release or failure-record) commit was ambiguous. Never retried."""


class PreTerminalExecutionError(L2FExecutionError):
    """A non-ambiguous failure BEFORE any terminal state; the job was recovered to PENDING.

    The original cause is chained, and :attr:`recovered_to` records the confirmed final state.
    """

    def __init__(self, message: str, *, recovered_to: str) -> None:
        super().__init__(message)
        self.recovered_to = recovered_to


class ExecutionRecordedFailureError(L2FExecutionError):
    """A non-ambiguous failure AFTER the job entered RUNNING; a durable bounded FAILED outcome
    was recorded. The original cause is chained and :attr:`failure_code` names the bounded code."""

    def __init__(self, message: str, *, failure_code: str) -> None:
        super().__init__(message)
        self.failure_code = failure_code


class ExecutionRecoveryError(L2FExecutionError):
    """The recovery itself failed: the job's final state is NOT the required one.

    Both failures are preserved — the original cause is chained, and :attr:`recovery_cause`
    holds the exception the recovery attempt raised. Nothing is ever retried automatically.
    """

    def __init__(self, message: str, *, recovery_cause: BaseException) -> None:
        super().__init__(message)
        self.recovery_cause = recovery_cause


class PostCommitWrapperError(L2FExecutionError):
    """A wrapper failed AFTER a CONFIRMED commit. The committed terminal state and the published
    artifacts are preserved exactly; the original wrapper failure is chained."""


@dataclass(frozen=True)
class ExecutionDispatchResult:
    """The outcome of one dispatched execution."""

    job_id: str
    job_key: str
    plan_hash: str
    status: str
    worker_id: str
    result_hash: str | None = None
    vcf_sha256: str | None = None
    result_manifest_sha256: str | None = None
    runtime_ms: int | None = None
    failure_code: str | None = None
    replay: bool = False


def _require_f5_revision(conn: Connection) -> None:
    rev = conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one_or_none()
    if rev != F5_EXECUTION_REVISION:
        raise PlanRevisionError(
            f"live database revision is {rev!r}, not the required {F5_EXECUTION_REVISION!r}; "
            "refusing to execute (this boundary NEVER runs Alembic)"
        )


def _authorize_connection(conn: Connection, *, require_operational_identity: bool) -> None:
    """Authorize THIS EXACT connection before its first query, publication or mutation.

    Called at the top of every live connection the production path opens. A successful check on a
    different connection is never accepted as authorization for this one.
    """
    if not require_operational_identity:
        return
    verify_operational_database_identity(conn)
    _require_f5_revision(conn)


def _sqlstate(exc: DBAPIError) -> str | None:
    return getattr(getattr(exc, "orig", None), "sqlstate", None)


@contextlib.contextmanager
def _typed_execution_errors() -> Any:
    try:
        yield
    except DBAPIError as exc:
        state = _sqlstate(exc)
        if state in {_SQLSTATE_RESULT_CONFLICT, _SQLSTATE_DUAL_OUTCOME}:
            raise ExecutionResultConflictError(
                "a differing durable outcome already exists for this job"
            ) from exc
        if state in {_SQLSTATE_NOT_OWNED, _SQLSTATE_MISSING_RECORD}:
            raise InvalidJobTransitionError(
                "the requested terminal transition is not permitted for this job/worker"
            ) from exc
        raise


def _now_utc() -> str:
    return _dt.datetime.now(_dt.UTC).replace(microsecond=0).isoformat()


# --------------------------------------------------------------------------- #
# per-attempt workspace: fresh, exclusive, private, inode-verified, never reused
# --------------------------------------------------------------------------- #
@dataclass
class AttemptWorkspace:
    """One per-attempt directory bound to the EXACT inode this process created.

    Identity is pinned three ways, because each alone is defeatable:

    * ``(st_dev, st_ino)`` captured immediately after ``mkdir``;
    * a private ``O_EXCL`` sentinel with an unguessable name, because a filesystem may REUSE an
      inode number after ``rmdir`` — a replacement directory cannot reproduce the sentinel;
    * a RETAINED directory descriptor (``O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC``) opened at
      creation time. Every later child operation is **descriptor-relative**, so the pathname is
      never re-resolved and a replacement installed at :attr:`path` can never be traversed,
      read through or deleted — closing the check/use race a pathname-based ``rmtree`` leaves
      open.

    A parent descriptor is retained as well, so the final ``rmdir`` of the attempt entry itself
    is performed relative to the parent inode this attempt actually created its entry in.
    """

    path: Path
    st_dev: int
    st_ino: int
    sentinel: str
    dir_fd: int | None = None
    parent_fd: int | None = None

    # -- identity ---------------------------------------------------------------------------- #
    def same_inode(self) -> bool:
        """True when the PATH still resolves to a directory with the created ``(dev, ino)``.

        This is the check that detects a replacement installed at :attr:`path`; it is deliberately
        pathname-based, because that is exactly the substitution it exists to notice.
        """
        try:
            info = os.lstat(self.path)
        except OSError:
            return False
        return (
            stat.S_ISDIR(info.st_mode) and info.st_dev == self.st_dev and info.st_ino == self.st_ino
        )

    def descriptor_valid(self) -> bool:
        """True when the RETAINED descriptor still refers to the directory we created."""
        if self.dir_fd is None:
            return False
        try:
            info = os.fstat(self.dir_fd)
        except OSError:
            return False
        return (
            stat.S_ISDIR(info.st_mode) and info.st_dev == self.st_dev and info.st_ino == self.st_ino
        )

    def still_ours(self) -> bool:
        """True only when the path AND the retained descriptor are the directory we created."""
        if not self.same_inode() or not self.descriptor_valid():
            return False
        try:  # the sentinel is looked up RELATIVE to the retained descriptor
            marker = os.stat(self.sentinel, dir_fd=self.dir_fd, follow_symlinks=False)
        except OSError:
            return False  # an inode-number reuse cannot reproduce the private sentinel
        return stat.S_ISREG(marker.st_mode)

    # -- descriptor lifecycle ---------------------------------------------------------------- #
    def close(self) -> None:
        """Close both retained descriptors EXACTLY once, on every path (idempotent)."""
        for name in ("dir_fd", "parent_fd"):
            fd = getattr(self, name)
            setattr(self, name, None)  # cleared FIRST, so a second call can never double-close
            if fd is not None:
                with contextlib.suppress(OSError):
                    os.close(fd)


def reject_symlinked_components(path: Path) -> Path:
    """Reject a path any of whose components (including intermediates) is a symlink."""
    absolute = Path(os.path.abspath(path))
    walked = Path(absolute.anchor or os.sep)
    for part in absolute.relative_to(walked).parts:
        walked = walked / part
        if walked.is_symlink():
            raise ExecutionWorkspaceError(
                f"path component {walked} is a symlink; symlinked work paths are refused"
            )
    return absolute


def _remove_children_at(dir_fd: int) -> None:
    """Recursively empty a directory using ONLY descriptor-relative operations.

    Nothing here re-resolves a pathname and nothing follows a symlink, so a replacement installed
    at the attempt path cannot be traversed. Sub-directories are opened relative to their parent
    descriptor with ``O_NOFOLLOW``, so a symlink swapped in for a child is unlinked, never
    followed.
    """
    for name in os.listdir(dir_fd):
        try:
            info = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
        except OSError:
            continue
        if stat.S_ISDIR(info.st_mode):
            try:
                child = os.open(
                    name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=dir_fd
                )
            except OSError:
                continue
            try:
                _remove_children_at(child)
            finally:
                with contextlib.suppress(OSError):
                    os.close(child)
            with contextlib.suppress(OSError):
                os.rmdir(name, dir_fd=dir_fd)
        else:
            with contextlib.suppress(OSError):
                os.unlink(name, dir_fd=dir_fd)


def _remove_created_inode(workspace: AttemptWorkspace, *, require_sentinel: bool = True) -> None:
    """Remove ONLY the directory inode this attempt created; never a replacement.

    Children are removed through the RETAINED descriptor, so the untrusted pathname is never
    traversed. The attempt's own directory entry is then removed relative to the retained PARENT
    descriptor, and only after re-confirming that the entry still names our exact inode; if that
    identity cannot be established the entry is LEFT ALONE rather than risking the deletion of a
    replacement. ``rmdir`` additionally refuses a non-empty directory, so a replacement that has
    had anything written into it survives even the final step.

    ``require_sentinel=False`` is used exclusively on the creation-failure path, where the
    sentinel may not have been written yet.
    """
    if require_sentinel:
        if not workspace.still_ours():
            workspace.close()
            return
    elif not workspace.descriptor_valid() and not workspace.same_inode():
        workspace.close()
        return

    try:
        if workspace.dir_fd is not None and workspace.descriptor_valid():
            _remove_children_at(workspace.dir_fd)
        _rmdir_entry(workspace)
    finally:
        workspace.close()


def _rmdir_entry(workspace: AttemptWorkspace) -> None:
    """Remove the attempt's own directory entry relative to the retained parent descriptor."""
    name = workspace.path.name
    parent_fd = workspace.parent_fd
    try:
        if parent_fd is not None:
            entry = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        else:  # pragma: no cover - the parent descriptor is always retained on the created path
            entry = os.lstat(workspace.path)
    except OSError:
        return
    if not stat.S_ISDIR(entry.st_mode):
        return
    if (entry.st_dev, entry.st_ino) != (workspace.st_dev, workspace.st_ino):
        return  # a replacement occupies the entry: leave it entirely alone
    with contextlib.suppress(OSError):
        if parent_fd is not None:
            os.rmdir(name, dir_fd=parent_fd)
        else:  # pragma: no cover - see above
            os.rmdir(workspace.path)


def _create_attempt_dir(work_root: Path, *, job_id: str, attempt_id: str) -> AttemptWorkspace:
    """Create a FRESH, EXCLUSIVE per-attempt directory (never ``exist_ok``, never reused).

    ``mkdir`` without ``exist_ok`` fails closed if anything already occupies the path, so a stale
    directory, a pre-planted output file or a substituted symlink cannot be adopted. The created
    inode's ``(st_dev, st_ino)`` is captured immediately, a descriptor onto that exact inode is
    retained for the attempt's whole lifetime, and every later check runs against that identity;
    if validation fails afterwards, ONLY that inode is removed.
    """
    root = reject_symlinked_components(work_root)
    if not root.is_dir():
        raise ExecutionWorkspaceError(f"work root {root} is not an existing directory")
    attempt = root / f"l2f-{job_id}-{attempt_id}"
    try:
        attempt.mkdir(mode=ATTEMPT_DIR_MODE)
    except FileExistsError as exc:
        raise ExecutionWorkspaceError(
            f"attempt work directory {attempt} already exists; it is never reused"
        ) from exc
    except OSError as exc:
        raise ExecutionWorkspaceError(
            f"attempt work directory {attempt} is unusable: {exc}"
        ) from exc

    # capture the created inode identity BEFORE any further check, so a substitution racing the
    # validation can only ever fail the validation - never be adopted, and never be deleted.
    try:
        info = os.lstat(attempt)
    except OSError as exc:  # pragma: no cover - the directory was just created
        raise ExecutionWorkspaceError(f"attempt work directory {attempt} vanished") from exc
    sentinel = f".minos-attempt-{uuid.uuid4().hex}"
    workspace = AttemptWorkspace(
        path=attempt, st_dev=info.st_dev, st_ino=info.st_ino, sentinel=sentinel
    )

    try:
        try:  # RETAIN descriptors: every later child operation is descriptor-relative
            workspace.parent_fd = os.open(
                root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
            )
            workspace.dir_fd = os.open(
                attempt, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
            )
        except OSError as exc:
            raise ExecutionWorkspaceError(
                f"attempt work directory {attempt} could not be opened: {exc}"
            ) from exc
        if not workspace.descriptor_valid():
            raise ExecutionWorkspaceError(
                f"attempt work directory {attempt} descriptor does not match the created inode"
            )
        try:  # a private O_EXCL marker pins the identity beyond inode-number reuse
            os.close(
                os.open(
                    sentinel,
                    os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW | os.O_CLOEXEC,
                    0o600,
                    dir_fd=workspace.dir_fd,
                )
            )
        except OSError as exc:
            raise ExecutionWorkspaceError(
                f"attempt work directory {attempt} could not be marked: {exc}"
            ) from exc
        if not stat.S_ISDIR(info.st_mode):
            raise ExecutionWorkspaceError(f"attempt work directory {attempt} is not a directory")
        if info.st_uid != os.geteuid():
            raise ExecutionWorkspaceError(
                f"attempt work directory {attempt} is not owned by this user"
            )
        if stat.S_IMODE(info.st_mode) != ATTEMPT_DIR_MODE:
            raise ExecutionWorkspaceError(
                f"attempt work directory {attempt} has mode {stat.S_IMODE(info.st_mode):#o}, "
                f"expected {ATTEMPT_DIR_MODE:#o}"
            )
        if attempt.parent != root:
            raise ExecutionWorkspaceError(
                f"attempt work directory {attempt} is not directly under the work root {root}"
            )
        if not workspace.still_ours():  # pragma: no cover - substitution racing this call
            raise ExecutionWorkspaceError(
                f"attempt work directory {attempt} was replaced during validation"
            )
    except BaseException:
        # remove ONLY the inode this call created (the sentinel may not exist yet).
        _remove_created_inode(workspace, require_sentinel=False)
        raise
    return workspace


def _require_absent_output(vcf_path: Path) -> None:
    """A produced-output path must not pre-exist: no stale or planted output is ever reused."""
    if vcf_path.is_symlink() or vcf_path.exists():
        raise ExecutionWorkspaceError(
            f"output path {vcf_path} already exists; a produced output is never reused"
        )


def verify_produced_output(vcf_path: Path, workspace: AttemptWorkspace) -> None:
    """Standalone pathname predicate: is this path a private regular file in OUR attempt inode?

    The production path does **not** use this; it uses :func:`acquire_produced_output`, which
    binds validation, hashing and publication to one opened descriptor. This remains as a
    directly testable predicate over the same conditions.
    """
    try:
        info = os.lstat(vcf_path)
    except OSError as exc:
        raise GatkOutputError(f"produced output {vcf_path} is unreadable: {exc}") from exc
    if stat.S_ISLNK(info.st_mode):
        raise GatkOutputError(f"produced output {vcf_path} is a symlink")
    if not stat.S_ISREG(info.st_mode):
        raise GatkOutputError(f"produced output {vcf_path} is not a regular file")
    if info.st_nlink != 1:
        raise GatkOutputError(
            f"produced output {vcf_path} has {info.st_nlink} links; hard-linked outputs are refused"
        )
    if not workspace.still_ours():
        raise GatkOutputError(
            f"the attempt directory holding {vcf_path} was replaced during execution"
        )
    parent = os.lstat(vcf_path.parent)
    if (parent.st_dev, parent.st_ino) != (workspace.st_dev, workspace.st_ino):
        raise GatkOutputError(
            f"produced output {vcf_path} is not inside the attempt directory this run created"
        )


@dataclass(frozen=True)
class AcquiredOutput:
    """The produced VCF, read ONCE from a single opened inode, with its derived identity.

    ``sha256`` and ``size_bytes`` are computed from :attr:`payload` — the exact bytes that are
    validated, published and bound into the result. Nothing downstream re-opens the pathname.
    """

    payload: bytes
    sha256: str
    size_bytes: int


def _read_fd_bytes(fd: int) -> bytes:
    """Read a descriptor to EOF in constant-size chunks (no pathname involved)."""
    chunks: list[bytes] = []
    while True:
        chunk = os.read(fd, _OUTPUT_CHUNK)
        if not chunk:
            return b"".join(chunks)
        chunks.append(chunk)


def acquire_produced_output(workspace: AttemptWorkspace, inputs: ExecutionInput) -> AcquiredOutput:
    """THE production output boundary: one inode, opened once, read once, hashed once.

    Opens ``output.vcf`` **relative to the retained attempt descriptor** with ``O_NOFOLLOW |
    O_NONBLOCK``, so a symlink is refused by the kernel and a FIFO or device fails promptly
    instead of blocking. It then requires a regular file with ``st_nlink == 1`` whose descriptor
    and parent both belong to this attempt, reads the exact bytes once, re-``fstat``s to catch a
    mutation during the read, validates the VCF structure from **those** bytes, and derives the
    digest and size from **those same** bytes. No later step re-opens or re-reads the pathname.
    """
    from minos_engine.storage.l2f_gatk_runner import validate_vcf_payload

    if not workspace.descriptor_valid() or not workspace.same_inode():
        raise GatkOutputError("the attempt directory was replaced before the output was acquired")
    assert workspace.dir_fd is not None  # noqa: S101 - guaranteed by descriptor_valid()
    try:
        fd = os.open(
            OUTPUT_VCF_NAME,
            os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
            dir_fd=workspace.dir_fd,
        )
    except OSError as exc:
        raise GatkOutputError(f"produced VCF could not be opened: {exc}") from exc
    try:
        before = os.fstat(fd)
        if stat.S_ISFIFO(before.st_mode):
            raise GatkOutputError("produced VCF is a FIFO")
        if not stat.S_ISREG(before.st_mode):
            raise GatkOutputError("produced VCF is not a regular file")
        if before.st_nlink != 1:
            raise GatkOutputError(
                f"produced VCF has {before.st_nlink} links; hard-linked outputs are refused"
            )
        parent = os.fstat(workspace.dir_fd)
        if (parent.st_dev, parent.st_ino) != (workspace.st_dev, workspace.st_ino):
            raise GatkOutputError("produced VCF's parent is not this attempt's directory")
        payload = _read_fd_bytes(fd)
        after = os.fstat(fd)
    except OSError as exc:
        raise GatkOutputError(f"produced VCF could not be read: {exc}") from exc
    finally:
        with contextlib.suppress(OSError):
            os.close(fd)

    if (after.st_dev, after.st_ino) != (before.st_dev, before.st_ino):
        raise GatkOutputError("produced VCF was replaced while it was being read")
    if after.st_size != before.st_size or len(payload) != after.st_size:
        raise GatkOutputError("produced VCF changed size while it was being read")
    if not payload:
        raise GatkOutputError("produced VCF is empty")
    if not workspace.same_inode():
        raise GatkOutputError("the attempt directory was replaced while the output was read")

    validate_vcf_payload(payload, inputs=inputs)
    return AcquiredOutput(
        payload=payload, sha256=hashlib.sha256(payload).hexdigest(), size_bytes=len(payload)
    )


def _remove_attempt_dir(workspace: AttemptWorkspace | None) -> None:
    """Remove the per-attempt directory after EVERY terminal outcome — success, nonzero exit,
    timeout, subprocess-start failure, invalid output, persistence rollback, an ambiguous commit
    and a confirmed post-commit wrapper failure — through the RETAINED descriptor only."""
    if workspace is None:
        return
    _remove_created_inode(workspace)


def _register_artifact(conn: Connection, art: PublishedResultArtifact) -> str:
    """Get-or-verify a catalog.artifacts row for one published F5 artifact."""
    sel = (
        "SELECT id, uri, size_bytes, media_type, provenance FROM catalog.artifacts "
        "WHERE sha256 = :h"
    )
    row = conn.execute(text(sel), {"h": art.sha256}).mappings().first()
    if row is None:
        conn.execute(
            text(
                "INSERT INTO catalog.artifacts (uri, sha256, media_type, size_bytes, provenance) "
                "VALUES (:u, :h, :m, :s, :p) ON CONFLICT (sha256) DO NOTHING"
            ),
            {
                "u": art.uri,
                "h": art.sha256,
                "m": art.media_type,
                "s": art.size_bytes,
                "p": art.provenance,
            },
        )
        row = conn.execute(text(sel), {"h": art.sha256}).mappings().first()
    if row is None:  # pragma: no cover - a row must exist after get-or-insert
        raise ArtifactMetadataConflictError(f"artifact for sha256 {art.sha256} was not registered")
    size = row["size_bytes"]
    if (
        row["uri"] != art.uri
        or size is None
        or int(size) != art.size_bytes
        or row["media_type"] != art.media_type
        or row["provenance"] != art.provenance
    ):
        raise ArtifactMetadataConflictError(
            f"catalog.artifacts for sha256 {art.sha256} exists with differing metadata"
        )
    return str(row["id"])


@dataclass(frozen=True)
class _Prepared:
    """Everything resolved BEFORE the job is transitioned to RUNNING."""

    job_id: str
    job_key: str
    plan_member_id: str
    plan_config_id: str
    inputs: ExecutionInput
    config: ExecutionConfig
    invocation: LogicalGatkInvocation
    paths: Any


def _resolve_plan_id(conn: Connection, plan: ExperimentPlan) -> str:
    plan_id = conn.execute(
        text("SELECT id FROM experiments.l2f_experiment_plans WHERE plan_hash = :h"),
        {"h": plan.plan_hash},
    ).scalar_one_or_none()
    if plan_id is None:
        raise JobPlanMissingError("the accepted F3-C1 plan graph is not persisted")
    return str(plan_id)


def _prepare(
    engine: Engine,
    plan: ExperimentPlan,
    *,
    job_id: str,
    job_key: str,
    dataset_root: DatasetRoot,
    gatk_executable_sha256: str,
    gatk_version: str,
    require_operational_identity: bool,
) -> _Prepared:
    """Resolve the complete job/member/config/upstream identity and all input bytes."""
    with engine.connect() as conn:
        _authorize_connection(conn, require_operational_identity=require_operational_identity)
        plan_id = _resolve_plan_id(conn, plan)
        job = (
            conn.execute(
                text(
                    "SELECT plan_member_id, plan_config_id FROM experiments.l2f_experiment_jobs "
                    "WHERE id = :i AND plan_id = :p"
                ),
                {"i": job_id, "p": plan_id},
            )
            .mappings()
            .one_or_none()
        )
        if job is None:
            raise JobPlanMissingError(f"job {job_id} does not belong to the accepted plan")
        inputs, paths = resolve_accepted_execution_input(
            conn,
            plan_id=plan_id,
            plan_member_id=str(job["plan_member_id"]),
            root=dataset_root,
        )
        config = load_accepted_execution_config(
            conn, plan_id=plan_id, plan_config_id=str(job["plan_config_id"])
        )
    invocation = build_logical_invocation(
        effective_config=config.effective_config,
        inputs=inputs,
        gatk_executable_sha256=gatk_executable_sha256,
        gatk_version=gatk_version,
    )
    return _Prepared(
        job_id=job_id,
        job_key=job_key,
        plan_member_id=str(job["plan_member_id"]),
        plan_config_id=str(job["plan_config_id"]),
        inputs=inputs,
        config=config,
        invocation=invocation,
        paths=paths,
    )


def _commit_or_ambiguous(trans: Any) -> None:
    try:
        trans.commit()
    except BaseException as exc:  # a raising COMMIT -> unknown outcome; artifacts are RETAINED
        raise AmbiguousExecutionCommitError(
            "COMMIT raised; the execution outcome is ambiguous and is NOT retried"
        ) from exc


def _post_commit_hook() -> None:
    """No-op seam invoked AFTER a successful commit. A failure here must NOT roll back or remove
    the committed rows or the published immutable artifacts."""
    return None


def _confirmed_post_commit() -> None:
    """Run the post-commit seam and label any failure as POST-COMMIT.

    The commit is already confirmed durable, so this never rolls back, never removes artifacts
    and never attempts a second terminal transition; it only re-types the wrapper failure so a
    caller can tell it apart from a pre-terminal failure or an ambiguous commit.
    """
    try:
        _post_commit_hook()
    except BaseException as exc:
        raise PostCommitWrapperError(
            "a wrapper failed AFTER a confirmed commit; the terminal state and artifacts stand"
        ) from exc


def _record_failure(
    engine: Engine,
    plan: ExperimentPlan,
    *,
    job_id: str,
    job_key: str,
    worker_id: str,
    failure: ExecutionFailure,
    require_operational_identity: bool,
) -> ExecutionDispatchResult:
    """Insert/verify the bounded failure record and transition RUNNING -> FAILED.

    ``job_key`` is the value already resolved by the claim, so no post-commit database lookup is
    performed. Commit-state semantics match the success path exactly: a pre-commit exception rolls
    back, a raising COMMIT is an :class:`AmbiguousExecutionCommitError` that is never retried, and
    a wrapper failure AFTER a successful commit leaves the durable FAILED row in place.
    """
    conn = engine.connect()
    trans = conn.begin()
    committed = False
    try:
        _authorize_connection(conn, require_operational_identity=require_operational_identity)
        conn.execute(text(f"SET LOCAL ROLE {SCHEMA_OWNER}"))
        with _typed_execution_errors():
            conn.execute(
                text("SELECT * FROM experiments.minos_l2f_fail_job(:h, :j, :w, :c, :e, :s)"),
                {
                    "h": plan.plan_hash,
                    "j": job_id,
                    "w": worker_id,
                    "c": failure.failure_code,
                    "e": failure.exit_code,
                    "s": failure.stderr_sha256,
                },
            ).mappings().one()
        _commit_or_ambiguous(trans)
        committed = True
        _confirmed_post_commit()  # the FAILED row must survive any wrapper failure below
        return ExecutionDispatchResult(
            job_id=job_id,
            job_key=job_key,
            plan_hash=plan.plan_hash,
            status="FAILED",
            worker_id=worker_id,
            failure_code=failure.failure_code,
        )
    except (AmbiguousExecutionCommitError, PostCommitWrapperError):
        raise  # unknown OR already confirmed: do NOT roll back and do NOT retry
    except BaseException:
        if not committed:
            with contextlib.suppress(Exception):
                trans.rollback()
        raise
    finally:
        conn.close()


def _build_manifest(
    plan: ExperimentPlan,
    prepared: _Prepared,
    outcome: GatkExecutionOutcome,
    *,
    worker_id: str,
    result_hash: str,
) -> ExecutionResultManifest:
    inputs = prepared.inputs
    return ExecutionResultManifest(
        schema_version=EXECUTION_RESULT_SCHEMA,
        plan_hash=plan.plan_hash,
        job_id=prepared.job_id,
        job_key=prepared.job_key,
        dataset_id=inputs.dataset_id,
        round_id=inputs.round_id,
        profile_id=inputs.profile_id,
        content_hash=inputs.content_hash,
        feature_values_hash=inputs.feature_values_hash,
        config_hash=prepared.config.config_hash,
        parameter_space_hash=prepared.config.parameter_space_hash,
        input_identity_hash=inputs.identity_hash(),
        bam_sha256=inputs.bam_sha256,
        bai_sha256=inputs.bai_sha256,
        reference_sha256=inputs.reference_sha256,
        fai_sha256=inputs.fai_sha256,
        dictionary_sha256=inputs.dictionary_sha256,
        bam_size_bytes=inputs.bam_size_bytes,
        region_hash=inputs.region_hash,
        region_start0=inputs.region_start0,
        region_end0_exclusive=inputs.region_end0_exclusive,
        chromosome=inputs.chromosome,
        logical_argv_hash=prepared.invocation.argv_hash(),
        gatk_executable_sha256=prepared.invocation.gatk_executable_sha256,
        gatk_version=prepared.invocation.gatk_version,
        vcf_sha256=outcome.vcf_sha256,
        vcf_size_bytes=outcome.vcf_size_bytes,
        result_hash=result_hash,
        runtime_ms=outcome.runtime_ms,
        worker_id=worker_id,
        generated_at=_now_utc(),
    )


def _complete_success(
    engine: Engine,
    plan: ExperimentPlan,
    prepared: _Prepared,
    outcome: GatkExecutionOutcome,
    *,
    worker_id: str,
    publisher: ResultArtifactPublisher,
    vcf_bytes: bytes,
    require_operational_identity: bool,
) -> ExecutionDispatchResult:
    """Publish both artifacts, then transactionally register + insert/verify + transition."""
    result_hash = compute_result_hash(
        plan_hash=plan.plan_hash,
        job_key=prepared.job_key,
        inputs=prepared.inputs,
        config=prepared.config,
        invocation=prepared.invocation,
        outcome=outcome,
    )
    manifest = _build_manifest(
        plan, prepared, outcome, worker_id=worker_id, result_hash=result_hash
    )
    manifest_bytes = build_result_manifest_bytes(manifest)
    manifest_sha = hashlib.sha256(manifest_bytes).hexdigest()

    created: list[PublishedResultArtifact] = []
    conn = engine.connect()
    trans = conn.begin()
    committed = False
    try:
        _authorize_connection(conn, require_operational_identity=require_operational_identity)
        vcf_art = publisher.publish(vcf_bytes, kind="vcf", sha256=outcome.vcf_sha256)
        created.append(vcf_art)
        man_art = publisher.publish(manifest_bytes, kind="result_manifest", sha256=manifest_sha)
        created.append(man_art)

        conn.execute(text(f"SET LOCAL ROLE {SCHEMA_OWNER}"))
        vcf_artifact_id = _register_artifact(conn, vcf_art)
        manifest_artifact_id = _register_artifact(conn, man_art)
        with _typed_execution_errors():
            row = (
                conn.execute(
                    text(
                        "SELECT * FROM experiments.minos_l2f_complete_job_success("
                        ":h, :j, :w, :k, :ch, :ps, :ii, :la, :ex, :gv, :va, :vs, :ma, :ms, :rh, :rt)"
                    ),
                    {
                        "h": plan.plan_hash,
                        "j": prepared.job_id,
                        "w": worker_id,
                        "k": prepared.job_key,
                        "ch": prepared.config.config_hash,
                        "ps": prepared.config.parameter_space_hash,
                        "ii": prepared.inputs.identity_hash(),
                        "la": prepared.invocation.argv_hash(),
                        "ex": prepared.invocation.gatk_executable_sha256,
                        "gv": prepared.invocation.gatk_version,
                        "va": vcf_artifact_id,
                        "vs": outcome.vcf_sha256,
                        "ma": manifest_artifact_id,
                        "ms": manifest_sha,
                        "rh": result_hash,
                        "rt": outcome.runtime_ms,
                    },
                )
                .mappings()
                .one()
            )
        _commit_or_ambiguous(trans)
        committed = True
        _confirmed_post_commit()  # post-durable-commit: keep the rows AND the artifacts
        return ExecutionDispatchResult(
            job_id=prepared.job_id,
            job_key=prepared.job_key,
            plan_hash=plan.plan_hash,
            status="SUCCEEDED",
            worker_id=worker_id,
            result_hash=result_hash,
            vcf_sha256=outcome.vcf_sha256,
            result_manifest_sha256=manifest_sha,
            runtime_ms=outcome.runtime_ms,
            replay=not bool(row["created"]),
        )
    except (AmbiguousExecutionCommitError, PostCommitWrapperError):
        raise  # unknown OR already confirmed: never roll back, never remove immutable artifacts
    except BaseException:
        if not committed:
            with contextlib.suppress(Exception):
                trans.rollback()
            for art in created:
                publisher.unpublish_if_created(art)
        raise
    finally:
        conn.close()


_FAILURE_CODES: tuple[tuple[type[BaseException], str], ...] = (
    (GatkTimeoutError, "GATK_TIMEOUT"),
    (GatkOutputError, "GATK_OUTPUT_INVALID"),
    (GatkExecutionError, "GATK_NONZERO_EXIT"),
    (ExecutionWorkspaceError, "EXECUTION_ERROR"),
    (GatkInvocationError, "EXECUTION_ERROR"),
)

#: the recognized GATK execution failures whose bounded FAILED outcome IS the dispatch result.
_RUNNER_FAILURES: tuple[type[BaseException], ...] = (
    GatkTimeoutError,
    GatkExecutionError,
    GatkOutputError,
    OSError,
)


def _failure_code_for(exc: BaseException) -> str:
    for kind, code in _FAILURE_CODES:
        if isinstance(exc, kind):
            return code
    return "EXECUTION_ERROR"


# --------------------------------------------------------------------------- #
# final-state assertions (used by tests to prove no job is ever stranded)
# --------------------------------------------------------------------------- #
def find_nonterminal_jobs(engine: Engine, plan_hash: str) -> tuple[tuple[str, str], ...]:
    """Every ``(job_id, status)`` of this plan still sitting in CLAIMED or RUNNING."""
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT j.id, j.status FROM experiments.l2f_experiment_jobs j "
                "JOIN experiments.l2f_experiment_plans p ON p.id = j.plan_id "
                "WHERE p.plan_hash = :h AND j.status IN ('CLAIMED', 'RUNNING') "
                "ORDER BY j.created_at, j.id"
            ),
            {"h": plan_hash},
        ).all()
    return tuple((str(r[0]), str(r[1])) for r in rows)


def assert_no_stranded_jobs(engine: Engine, plan_hash: str) -> None:
    """Raise unless EVERY job of this plan is PENDING or terminal.

    After any handled non-ambiguous failure the recovery contract requires exactly one of
    PENDING / SUCCEEDED / FAILED, so a CLAIMED or RUNNING row means a job was stranded.
    """
    stranded = find_nonterminal_jobs(engine, plan_hash)
    if stranded:
        raise ExecutionRecoveryError(
            f"{len(stranded)} job(s) are stranded in CLAIMED/RUNNING: {stranded}",
            recovery_cause=AssertionError("stranded jobs"),
        )


# --------------------------------------------------------------------------- #
# recovery primitives
# --------------------------------------------------------------------------- #
def _recover_to_pending(
    engine: Engine,
    plan: ExperimentPlan,
    *,
    job_id: str,
    worker_id: str,
    cause: BaseException,
    require_operational_identity: bool,
) -> None:
    """Release a merely-CLAIMED job back to PENDING, preserving BOTH failure statuses.

    Never suppresses the release failure: an ambiguous release commit raises
    :class:`AmbiguousRecoveryCommitError` and any other release failure raises
    :class:`ExecutionRecoveryError`, in both cases chained from the ORIGINAL cause.
    """
    try:
        _release_job_with_trust(
            engine,
            plan,
            job_id=uuid.UUID(job_id),
            worker_id=worker_id,
            require_operational_identity=require_operational_identity,
            revision_check=_require_f5_revision,
        )
    except AmbiguousClaimCommitError:
        raise AmbiguousRecoveryCommitError(
            f"the release of job {job_id} committed ambiguously; it is NOT retried"
        ) from cause
    except BaseException as rel:
        raise ExecutionRecoveryError(
            f"job {job_id} could not be released back to PENDING after a pre-terminal failure",
            recovery_cause=rel,
        ) from cause
    raise PreTerminalExecutionError(
        f"job {job_id} failed before any terminal state and was released to PENDING",
        recovered_to="PENDING",
    ) from cause


def _recover_to_failed(
    engine: Engine,
    plan: ExperimentPlan,
    *,
    job_id: str,
    job_key: str,
    worker_id: str,
    cause: BaseException,
    require_operational_identity: bool,
) -> ExecutionDispatchResult:
    """Record the durable bounded FAILED outcome for a RUNNING job, preserving both statuses."""
    code = _failure_code_for(cause)
    try:
        return _record_failure(
            engine,
            plan,
            job_id=job_id,
            job_key=job_key,
            worker_id=worker_id,
            failure=ExecutionFailure(failure_code=code),
            require_operational_identity=require_operational_identity,
        )
    except PostCommitWrapperError:
        # the FAILED row is already durable; a wrapper failure after that CONFIRMED commit is
        # not a failed recovery and must keep its own type.
        raise
    except AmbiguousExecutionCommitError:
        raise AmbiguousRecoveryCommitError(
            f"the FAILED record for job {job_id} committed ambiguously; it is NOT retried"
        ) from cause
    except BaseException as rec:
        raise ExecutionRecoveryError(
            f"job {job_id} could not be transitioned to a durable FAILED outcome",
            recovery_cause=rec,
        ) from cause


def _execute(
    engine: Engine,
    plan: ExperimentPlan,
    *,
    worker_id: str,
    runner: GatkRunner,
    dataset_root: DatasetRoot,
    publisher: ResultArtifactPublisher,
    work_root: Path,
    gatk_executable_sha256: str,
    gatk_version: str,
    require_operational_identity: bool,
) -> ExecutionDispatchResult | None:
    """Claim one accepted job, prepare it, run GATK, and record its terminal outcome.

    Recovery contract — after the claim, EVERY exit produces exactly one of:

    ==========================================  ==========================================
    situation                                   final state
    ==========================================  ==========================================
    preparation fails while CLAIMED             PENDING (PreTerminalExecutionError)
    release commit ambiguous                    unknown (AmbiguousRecoveryCommitError)
    start commit ambiguous                      unknown (AmbiguousStartCommitError); no GATK
    non-ambiguous error after RUNNING           FAILED (ExecutionRecordedFailureError)
    successful result commit                    SUCCEEDED
    failure-result commit                       FAILED
    terminal commit ambiguous                   unknown (AmbiguousExecutionCommitError)
    wrapper failure after confirmed commit      the committed terminal state (PostCommitWrapperError)
    ==========================================  ==========================================
    """
    claimed = _claim_next_job_with_trust(
        engine,
        plan,
        worker_id=worker_id,
        require_operational_identity=require_operational_identity,
        revision_check=_require_f5_revision,
    )
    if claimed is None:
        return None
    job_id, job_key = claimed.job_id, claimed.job_key

    # ---- everything while merely CLAIMED recovers to PENDING -----------------------------
    try:
        prepared = _prepare(
            engine,
            plan,
            job_id=job_id,
            job_key=job_key,
            dataset_root=dataset_root,
            gatk_executable_sha256=gatk_executable_sha256,
            gatk_version=gatk_version,
            require_operational_identity=require_operational_identity,
        )
    except AmbiguousExecutionCommitError:
        raise  # outcome unknown: never a second attempt, never a release
    except BaseException as exc:
        _recover_to_pending(
            engine,
            plan,
            job_id=job_id,
            worker_id=worker_id,
            cause=exc,
            require_operational_identity=require_operational_identity,
        )
        raise  # pragma: no cover - _recover_to_pending always raises

    # ---- CLAIMED -> RUNNING ---------------------------------------------------------------
    try:
        _start_job_with_trust(
            engine,
            plan,
            job_id=uuid.UUID(job_id),
            worker_id=worker_id,
            require_operational_identity=require_operational_identity,
            revision_check=_require_f5_revision,
        )
    except AmbiguousClaimCommitError as exc:
        # the CLAIMED -> RUNNING outcome is UNKNOWN: GATK is never executed and nothing retried.
        raise AmbiguousStartCommitError(
            f"the CLAIMED -> RUNNING commit for job {job_id} is ambiguous; GATK was NOT executed"
        ) from exc
    except BaseException as exc:
        # the transition did not happen, so the job is still merely CLAIMED: release it.
        _recover_to_pending(
            engine,
            plan,
            job_id=job_id,
            worker_id=worker_id,
            cause=exc,
            require_operational_identity=require_operational_identity,
        )
        raise  # pragma: no cover - _recover_to_pending always raises

    # ---- from here the job is RUNNING: every non-ambiguous exit must be durably terminal ----
    return _run_and_finalize(
        engine,
        plan,
        prepared,
        worker_id=worker_id,
        runner=runner,
        publisher=publisher,
        work_root=work_root,
        require_operational_identity=require_operational_identity,
    )


def _run_and_finalize(
    engine: Engine,
    plan: ExperimentPlan,
    prepared: _Prepared,
    *,
    worker_id: str,
    runner: GatkRunner,
    publisher: ResultArtifactPublisher,
    work_root: Path,
    require_operational_identity: bool,
) -> ExecutionDispatchResult:
    """Run GATK for a RUNNING job and drive it to exactly one durable terminal outcome.

    Workspace creation/validation, invocation rendering, runner setup, subprocess startup, output
    reading and success persistence are ALL inside the recovery scope, so none of them can leave
    the job stranded in RUNNING.
    """
    job_id, job_key = prepared.job_id, prepared.job_key
    workspace: AttemptWorkspace | None = None
    try:
        try:
            workspace = _create_attempt_dir(work_root, job_id=job_id, attempt_id=uuid.uuid4().hex)
            vcf_path = workspace.path / OUTPUT_VCF_NAME
            _require_absent_output(vcf_path)
            argv = render_execution_argv(
                effective_config=prepared.config.effective_config,
                inputs=prepared.inputs,
                reference_path=str(prepared.paths.reference),
                bam_path=str(prepared.paths.bam),
                output_path=str(vcf_path),
            )
            outcome = runner.run(
                argv=argv, work_dir=workspace.path, vcf_path=vcf_path, inputs=prepared.inputs
            )
            # THE output boundary: one inode, opened relative to the retained attempt descriptor,
            # read once, validated and hashed from those exact bytes. Nothing below re-opens the
            # pathname, so no object other than this one can be published or bound into a result.
            acquired = acquire_produced_output(workspace, prepared.inputs)
            # the runner's reported digest is never trusted on its own: it must agree with the
            # digest of the bytes actually acquired, or the output changed underneath us.
            if (
                acquired.sha256 != outcome.vcf_sha256
                or acquired.size_bytes != outcome.vcf_size_bytes
            ):
                raise GatkOutputError(
                    f"produced VCF for job {job_id} changed between execution and acquisition"
                )
            vcf_bytes = acquired.payload
            # every downstream identity is derived from the ACQUIRED bytes.
            outcome = outcome.model_copy(
                update={"vcf_sha256": acquired.sha256, "vcf_size_bytes": acquired.size_bytes}
            )
        except _RUNNER_FAILURES as exc:
            # a recognized GATK execution failure: the bounded FAILED outcome IS the result.
            return _recover_to_failed(
                engine,
                plan,
                job_id=job_id,
                job_key=job_key,
                worker_id=worker_id,
                cause=exc,
                require_operational_identity=require_operational_identity,
            )
        except AmbiguousExecutionCommitError:
            raise
        except BaseException as exc:
            # workspace, invocation or any other non-ambiguous pre-GATK failure: still durable.
            recorded = _recover_to_failed(
                engine,
                plan,
                job_id=job_id,
                job_key=job_key,
                worker_id=worker_id,
                cause=exc,
                require_operational_identity=require_operational_identity,
            )
            raise ExecutionRecordedFailureError(
                f"job {job_id} failed after entering RUNNING and was durably recorded as FAILED",
                failure_code=recorded.failure_code or "EXECUTION_ERROR",
            ) from exc

        try:
            return _complete_success(
                engine,
                plan,
                prepared,
                outcome,
                worker_id=worker_id,
                publisher=publisher,
                vcf_bytes=vcf_bytes,
                require_operational_identity=require_operational_identity,
            )
        except (AmbiguousExecutionCommitError, PostCommitWrapperError):
            raise  # outcome unknown or already confirmed: never a second terminal attempt
        except ExecutionResultConflictError:
            raise  # a differing durable outcome already exists: the job is already terminal
        except BaseException as exc:
            # a NON-ambiguous success-persistence failure after GATK completed: the transaction
            # rolled back and the job is still RUNNING, so drive it to a durable FAILED outcome.
            recorded = _recover_to_failed(
                engine,
                plan,
                job_id=job_id,
                job_key=job_key,
                worker_id=worker_id,
                cause=exc,
                require_operational_identity=require_operational_identity,
            )
            raise ExecutionRecordedFailureError(
                f"job {job_id} could not persist its success and was durably recorded as FAILED",
                failure_code=recorded.failure_code or "EXECUTION_ERROR",
            ) from exc
    finally:
        _remove_attempt_dir(workspace)


def execute_next_accepted_job(*, worker_id: str) -> ExecutionDispatchResult | None:
    """THE accepted F5 execution entry point — no caller-provided trust, paths or runner.

    Validates ``worker_id`` before any database or filesystem access, then claims, prepares,
    executes and records exactly one accepted job. Every live connection it opens verifies the
    canonical operational identity and the exact ``0008`` revision as its first statements.
    Returns ``None`` when the queue is empty. Never retries.
    """
    validate_worker_id(worker_id)
    engine = create_db_engine()
    try:
        runner = SubprocessGatkRunner.from_env()
        return _execute(
            engine,
            _build_accepted_plan(),
            worker_id=worker_id,
            runner=runner,
            dataset_root=dataset_root_from_env(),
            publisher=ResultArtifactPublisher(result_artifact_root_from_env()),
            work_root=work_root_from_env(),
            gatk_executable_sha256=runner.expected_sha256,
            gatk_version=runner.expected_version,
            require_operational_identity=True,
        )
    finally:
        engine.dispose()


def _execute_next_job_with_trust(
    engine: Engine,
    plan: ExperimentPlan,
    *,
    worker_id: str,
    runner: FakeGatkRunner,
    dataset_root: DatasetRoot,
    publisher: ResultArtifactPublisher,
    work_root: Path,
    gatk_executable_sha256: str = "0" * 64,
    gatk_version: str = "fake-gatk-4.5.0.0",
    require_operational_identity: bool = False,
) -> ExecutionDispatchResult | None:
    """PRIVATE explicit-trust execution for scratch / non-75 tests ONLY (FakeGatkRunner). Never
    exported; the accepted path is :func:`execute_next_accepted_job`."""
    validate_worker_id(worker_id)
    return _execute(
        engine,
        plan,
        worker_id=worker_id,
        runner=runner,
        dataset_root=dataset_root,
        publisher=publisher,
        work_root=work_root,
        gatk_executable_sha256=gatk_executable_sha256,
        gatk_version=gatk_version,
        require_operational_identity=require_operational_identity,
    )
