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
import shutil
import stat
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from sqlalchemy import Connection, Engine, text
from sqlalchemy.exc import DBAPIError

from minos_engine.experiments.execution_contract import (
    EXECUTION_RESULT_SCHEMA,
    ConfigArtifactError,
    ExecutionConfig,
    ExecutionFailure,
    ExecutionInput,
    ExecutionResultManifest,
    GatkExecutionError,
    GatkExecutionOutcome,
    GatkOutputError,
    GatkTimeoutError,
    InputResolutionError,
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
    "ExecutionDispatchResult",
    "AmbiguousExecutionCommitError",
    "ExecutionResultConflictError",
    "ExecutionWorkspaceError",
    "execute_next_accepted_job",
]

F5_EXECUTION_REVISION = L2F_EXECUTION_REVISION
#: every per-attempt work directory is created private to the executing user.
ATTEMPT_DIR_MODE = 0o700

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
# per-attempt workspace: fresh, exclusive, private, never reused
# --------------------------------------------------------------------------- #
def _create_attempt_dir(work_root: Path, *, job_id: str, attempt_id: str) -> Path:
    """Create a FRESH, EXCLUSIVE per-attempt directory (never ``exist_ok``, never reused).

    ``mkdir`` without ``exist_ok`` fails closed if anything already occupies the path, so a stale
    directory, a pre-planted output file or a substituted symlink cannot be adopted. The created
    inode is then re-verified through its own file descriptor to be a real directory, owned by
    this user, at mode ``0o700`` and located directly under the resolved provisioned root.
    """
    root = work_root.resolve(strict=True)
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

    if attempt.is_symlink():
        raise ExecutionWorkspaceError(f"attempt work directory {attempt} is a symlink")
    info = os.lstat(attempt)
    if not stat.S_ISDIR(info.st_mode):
        raise ExecutionWorkspaceError(f"attempt work directory {attempt} is not a directory")
    if info.st_uid != os.geteuid():
        raise ExecutionWorkspaceError(f"attempt work directory {attempt} is not owned by this user")
    if stat.S_IMODE(info.st_mode) != ATTEMPT_DIR_MODE:
        raise ExecutionWorkspaceError(
            f"attempt work directory {attempt} has mode {stat.S_IMODE(info.st_mode):#o}, "
            f"expected {ATTEMPT_DIR_MODE:#o}"
        )
    if attempt.resolve(strict=True).parent != root:
        raise ExecutionWorkspaceError(
            f"attempt work directory {attempt} resolves outside the provisioned work root {root}"
        )
    return attempt


def _require_absent_output(vcf_path: Path) -> None:
    """A produced-output path must not pre-exist: no stale or planted output is ever reused."""
    if vcf_path.is_symlink() or vcf_path.exists():
        raise ExecutionWorkspaceError(
            f"output path {vcf_path} already exists; a produced output is never reused"
        )


def _remove_attempt_dir(attempt_dir: Path | None) -> None:
    """Remove the per-attempt directory. Called after EVERY terminal outcome, including a
    successful commit, an ambiguous commit, a recorded failure and any pre-commit exception."""
    if attempt_dir is None:
        return
    with contextlib.suppress(Exception):
        shutil.rmtree(attempt_dir, ignore_errors=True)


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
        _post_commit_hook()  # post-durable-commit: the FAILED row must survive a wrapper failure
        return ExecutionDispatchResult(
            job_id=job_id,
            job_key=job_key,
            plan_hash=plan.plan_hash,
            status="FAILED",
            worker_id=worker_id,
            failure_code=failure.failure_code,
        )
    except AmbiguousExecutionCommitError:
        raise  # outcome unknown: do NOT roll back and do NOT retry
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
        _post_commit_hook()  # a failure here is post-durable-commit: keep rows + artifacts
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
    except AmbiguousExecutionCommitError:
        raise  # outcome unknown: DO NOT roll back and DO NOT remove immutable artifacts
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
)


def _failure_code_for(exc: BaseException) -> str:
    for kind, code in _FAILURE_CODES:
        if isinstance(exc, kind):
            return code
    return "EXECUTION_ERROR"


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
    """Claim one accepted job, prepare it, run GATK, and record its terminal outcome."""
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

    # ---- preparation while merely CLAIMED: a failure here RELEASES back to PENDING ----
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
    except (InputResolutionError, ConfigArtifactError, JobPlanMissingError):
        with contextlib.suppress(Exception):
            _release_job_with_trust(
                engine,
                plan,
                job_id=uuid.UUID(job_id),
                worker_id=worker_id,
                require_operational_identity=require_operational_identity,
                revision_check=_require_f5_revision,
            )
        raise

    _start_job_with_trust(
        engine,
        plan,
        job_id=uuid.UUID(job_id),
        worker_id=worker_id,
        require_operational_identity=require_operational_identity,
        revision_check=_require_f5_revision,
    )

    # ---- execution happens OUTSIDE any long-lived database transaction ----
    # The per-attempt directory is created fresh and exclusively, and is removed on EVERY exit
    # path below: success, nonzero exit, timeout, invalid/missing VCF, failure-record persistence,
    # a pre-commit exception, and a successful OR ambiguous terminal commit.
    attempt_dir: Path | None = None
    try:
        attempt_dir = _create_attempt_dir(work_root, job_id=job_id, attempt_id=uuid.uuid4().hex)
        vcf_path = attempt_dir / "output.vcf"
        _require_absent_output(vcf_path)
        try:
            argv = render_execution_argv(
                effective_config=prepared.config.effective_config,
                inputs=prepared.inputs,
                reference_path=str(prepared.paths.reference),
                bam_path=str(prepared.paths.bam),
                output_path=str(vcf_path),
            )
            outcome = runner.run(
                argv=argv, work_dir=attempt_dir, vcf_path=vcf_path, inputs=prepared.inputs
            )
            vcf_bytes = vcf_path.read_bytes()
        except (GatkTimeoutError, GatkExecutionError, GatkOutputError, OSError) as exc:
            return _record_failure(
                engine,
                plan,
                job_id=job_id,
                job_key=job_key,
                worker_id=worker_id,
                failure=ExecutionFailure(failure_code=_failure_code_for(exc)),
                require_operational_identity=require_operational_identity,
            )
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
    finally:
        _remove_attempt_dir(attempt_dir)


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
