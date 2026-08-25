"""The LEAST-PRIVILEGE L2-F2 baseline GATK execution boundary.

``0008`` deliberately grants ``minos_runner`` no direct privilege on any L2-F table, and the
historical F5 path both reads the plan graph with direct ``SELECT`` and persists artifacts under
``SET LOCAL ROLE minos_admin``. An external ``minos_runner_svc`` whose only membership is
``minos_runner`` therefore cannot use that path — and the wrong fix would be to hand the runner
service administrative authority. This module is the right fix: the same execution, driven
entirely through narrow ``SECURITY DEFINER`` interfaces.

What that costs the runner, precisely:

* it never reads the plan/member/config graph directly — one ``l2f2_resolve_claimed_execution``
  call returns the truth-free scientific identity for a job it already owns;
* it never inserts ``catalog.artifacts`` — ``l2f2_register_execution_artifact`` accepts only
  ``vcf`` and ``result_manifest`` and fixes media type and provenance itself;
* it never writes the result or failure ledgers — the existing ``0008`` completion and failure
  functions do, preserving the success/failure XOR;
* it never issues ``SET ROLE``.

What it keeps, unchanged from the audited F5 path: database metadata is not trusted as
sufficient. Every BAM, BAI, reference, FAI and dictionary is stream-hashed and matched against
the accepted identity, and the CONFIG artifact is re-read, re-hashed, re-canonicalised and
re-validated against the live GATK domain, all BEFORE any process starts. The recovery contract
is identical too: a failure while merely CLAIMED returns the job to PENDING, an ambiguous commit
is never retried, and every non-ambiguous exit after RUNNING is durably terminal.

This boundary is bound to the baseline store and to the frozen L2-F2-B protocol. It does not
weaken, replace or wrap :func:`~minos_engine.storage.l2f_execution.execute_next_accepted_job`,
which remains bound to the operational database at ``0008``.
"""

from __future__ import annotations

import hashlib
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy import Connection, Engine, text
from sqlalchemy.exc import DBAPIError

from minos_engine.baseline.phase_a import PhaseAAuthority, build_phase_a_authority
from minos_engine.common.errors import MinosEngineError
from minos_engine.experiments.execution_contract import (
    EXECUTION_RESULT_SCHEMA,
    ExecutionConfig,
    ExecutionInput,
    ExecutionResultManifest,
    GatkExecutionOutcome,
    GatkOutputError,
    LogicalGatkInvocation,
    build_result_manifest_bytes,
    compute_result_hash,
)
from minos_engine.storage.attempt_workspace import (
    AttemptWorkspace,
    create_attempt_workspace,
    remove_attempt_workspace,
)
from minos_engine.storage.l2f_execution import (
    OUTPUT_VCF_NAME,
    AmbiguousExecutionCommitError,
    ExecutionRecordedFailureError,
    ExecutionWorkspaceError,
    _now_utc,
    acquire_produced_output,
)
from minos_engine.storage.l2f_execution_config import validate_execution_config_artifact
from minos_engine.storage.l2f_execution_inputs import (
    DatasetRoot,
    dataset_root_from_env,
    verify_execution_input,
)
from minos_engine.storage.l2f_gatk_runner import (
    GatkRunner,
    build_logical_invocation,
    render_execution_argv,
)
from minos_engine.storage.l2f_result_publisher import (
    ResultArtifactPublisher,
    result_artifact_root_from_env,
)

__all__ = [
    "BASELINE_DATABASE_NAME",
    "BASELINE_REVISION",
    "BaselineRunnerAuthorityError",
    "L2F2DispatchResult",
    "L2F2ExecutionError",
    "execute_next_l2f2_phase_a_job",
]

#: the dedicated baseline store. The operational database is NEVER a valid target here.
BASELINE_DATABASE_NAME = "minos_l2f2_baseline"
#: the exact revision this boundary requires on EVERY connection it opens. It is EXACT, never a
#: floor and never ``head`` resolved at runtime: the runner fails closed on any other revision.
#:
#: It tracks the baseline store's revision rather than only the migrations the runner itself
#: needs, because the runner and the evaluator share ONE database. ``0011`` introduced this
#: boundary's own SECURITY DEFINER functions and grants; ``0012`` separated the plan-local member
#: ordinal from the source feature-matrix ordinal, without which the Phase-A plan is
#: unrepresentable; ``0013`` made the AdvancedScorer component columns nullable so evaluation can
#: store exactly what the pinned upstream scorer exposes. Neither grants the runner anything, and
#: ``0013`` touches no table the runner reads — but a database the evaluator has advanced is still
#: a database this boundary must recognise.
#:
#: ``0014`` is the one this boundary's OWN code depends on: it gives a failed execution an
#: authoritative elapsed runtime and drops the narrower six-argument failure writer, so a runner
#: at ``0014`` genuinely cannot record a failure against an older database.
BASELINE_REVISION = "0014_l2f2_exec_failure_runtime"

#: the ONLY MINOS group role the runner service may hold.
_REQUIRED_MEMBERSHIP = "minos_runner"
_FORBIDDEN_MEMBERSHIPS = ("minos_admin", "minos_evaluator", "minos_trainer", "minos_live")

_RESOLVE_SQL = "SELECT * FROM experiments.l2f2_resolve_claimed_execution(:h, :j, :w)"
_REGISTER_SQL = (
    "SELECT artifact_id, created FROM experiments.l2f2_register_execution_artifact("
    ":kind, :sha, :uri, :size)"
)


class L2F2ExecutionError(MinosEngineError):
    """The L2-F2 baseline execution boundary refused to proceed."""


class BaselineRunnerAuthorityError(L2F2ExecutionError):
    """The connection, database, revision or session principal is not the runner boundary."""


@dataclass(frozen=True)
class L2F2DispatchResult:
    """One durable L2-F2 execution outcome.

    ``execution_result_id`` is the evaluator's authoritative input, so it is returned directly
    rather than left to be rediscovered by a later table scan.
    """

    job_id: str
    job_key: str
    plan_hash: str
    worker_id: str
    status: str
    execution_result_id: str | None = None
    result_hash: str | None = None
    vcf_sha256: str | None = None
    result_manifest_sha256: str | None = None
    runtime_ms: int | None = None
    failure_code: str | None = None


@dataclass(frozen=True)
class _Prepared:
    """Everything resolved and byte-verified BEFORE the job is transitioned to RUNNING."""

    job_id: str
    job_key: str
    inputs: ExecutionInput
    config: ExecutionConfig
    invocation: LogicalGatkInvocation
    paths: Any


def authorize_baseline_runner_connection(conn: Connection) -> None:
    """Authorize THIS EXACT connection before any scientific access.

    Checks the database, the schema revision and the SESSION principal's authority. ``session_user``
    is used deliberately rather than ``current_user``: an already-issued ``SET ROLE`` must not be
    able to disguise which principal actually logged in.
    """
    database = str(conn.execute(text("SELECT current_database()")).scalar_one())
    if database != BASELINE_DATABASE_NAME:
        raise BaselineRunnerAuthorityError(
            f"the L2-F2 runner refuses database {database!r}; it executes only against "
            f"{BASELINE_DATABASE_NAME!r}"
        )
    revision = conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one_or_none()
    if revision != BASELINE_REVISION:
        raise BaselineRunnerAuthorityError(
            f"baseline database revision is {revision!r}, expected {BASELINE_REVISION!r}"
        )
    principal = conn.execute(
        text(
            "SELECT rolname, rolcanlogin, rolsuper, rolcreatedb, rolcreaterole, rolbypassrls "
            "  FROM pg_roles WHERE rolname = session_user"
        )
    ).one_or_none()
    if principal is None:
        raise BaselineRunnerAuthorityError("the session principal could not be resolved")
    name, can_login, is_super, createdb, createrole, bypassrls = principal
    if not can_login:
        raise BaselineRunnerAuthorityError(f"session principal {name!r} cannot log in")
    for label, elevated in (
        ("SUPERUSER", is_super),
        ("CREATEDB", createdb),
        ("CREATEROLE", createrole),
        ("BYPASSRLS", bypassrls),
    ):
        if elevated:
            raise BaselineRunnerAuthorityError(
                f"session principal {name!r} holds {label}; the runner must be least-privilege"
            )
    memberships = {
        row[0]
        for row in conn.execute(
            text(
                "SELECT r.rolname FROM pg_auth_members m "
                "  JOIN pg_roles r ON r.oid = m.roleid "
                "  JOIN pg_roles g ON g.oid = m.member "
                " WHERE g.rolname = session_user AND r.rolname LIKE 'minos%'"
            )
        )
    }
    if memberships != {_REQUIRED_MEMBERSHIP}:
        raise BaselineRunnerAuthorityError(
            f"session principal {name!r} holds MINOS memberships {sorted(memberships)}; the "
            f"runner requires exactly {{{_REQUIRED_MEMBERSHIP!r}}}"
        )
    forbidden = memberships.intersection(_FORBIDDEN_MEMBERSHIPS)
    if forbidden:  # pragma: no cover - unreachable given the equality check above
        raise BaselineRunnerAuthorityError(f"session principal holds {sorted(forbidden)}")


def _resolve_prepared(
    conn: Connection,
    *,
    plan_hash: str,
    job_id: str,
    job_key: str,
    worker_id: str,
    dataset_root: DatasetRoot,
    gatk_executable_sha256: str,
    gatk_runtime_bundle_sha256: str,
    gatk_version: str,
) -> _Prepared:
    """Resolve and BYTE-VERIFY the execution identity through the narrow interface only."""
    row = (
        conn.execute(text(_RESOLVE_SQL), {"h": plan_hash, "j": job_id, "w": worker_id})
        .mappings()
        .one_or_none()
    )
    if row is None:  # pragma: no cover - the function raises rather than returning empty
        raise L2F2ExecutionError(f"job {job_id} could not be resolved for worker {worker_id}")
    if str(row["job_key"]) != job_key:
        raise L2F2ExecutionError(
            f"resolved job_key {row['job_key']!r} does not match the claimed {job_key!r}"
        )

    # the SAME byte-verification core the historical operational path uses.
    inputs, paths = verify_execution_input(dict(row), root=dataset_root)
    config = validate_execution_config_artifact(
        {
            "config_index": row["config_index"],
            "config_hash": row["config_hash"],
            "parameter_space_hash": row["parameter_space_hash"],
            "media_type": row["config_media_type"],
            "uri": row["config_uri"],
            "sha256": row["config_sha256"],
            "size_bytes": row["config_size_bytes"],
        }
    )
    invocation = build_logical_invocation(
        effective_config=dict(config.effective_config),
        inputs=inputs,
        gatk_executable_sha256=gatk_executable_sha256,
        gatk_runtime_bundle_sha256=gatk_runtime_bundle_sha256,
        gatk_version=gatk_version,
    )
    return _Prepared(
        job_id=job_id,
        job_key=job_key,
        inputs=inputs,
        config=config,
        invocation=invocation,
        paths=paths,
    )


def _register(conn: Connection, *, kind: str, sha256: str, uri: str, size_bytes: int) -> str:
    row = conn.execute(
        text(_REGISTER_SQL), {"kind": kind, "sha": sha256, "uri": uri, "size": size_bytes}
    ).one()
    return str(row[0])


def _manifest(
    *,
    plan_hash: str,
    prepared: _Prepared,
    outcome: GatkExecutionOutcome,
    worker_id: str,
    result_hash: str,
) -> ExecutionResultManifest:
    inputs = prepared.inputs
    return ExecutionResultManifest(
        schema_version=EXECUTION_RESULT_SCHEMA,
        plan_hash=plan_hash,
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
        gatk_runtime_bundle_sha256=prepared.invocation.gatk_runtime_bundle_sha256,
        gatk_version=prepared.invocation.gatk_version,
        vcf_sha256=outcome.vcf_sha256,
        vcf_size_bytes=outcome.vcf_size_bytes,
        result_hash=result_hash,
        runtime_ms=outcome.runtime_ms,
        worker_id=worker_id,
        generated_at=_now_utc(),
    )


def execute_next_l2f2_phase_a_job(*, worker_id: str) -> L2F2DispatchResult | None:
    """THE accepted L2-F2 baseline execution entry — no caller-provided trust, paths or runner.

    Resolves the frozen Phase-A authority, the provisioned environment and the REAL
    ``SubprocessGatkRunner`` internally, then claims, prepares, executes and records exactly one
    Phase-A job. Returns ``None`` when the queue is empty. Never retries.
    """
    from minos_engine.storage.database import create_db_engine
    from minos_engine.storage.l2f_gatk_runner import SubprocessGatkRunner, work_root_from_env
    from minos_engine.storage.l2f_job_claim import validate_worker_id

    validate_worker_id(worker_id)
    authority = build_phase_a_authority()
    engine = create_db_engine()
    try:
        runner = SubprocessGatkRunner.from_env()
        return _execute_l2f2_job(
            engine,
            authority,
            worker_id=worker_id,
            runner=runner,
            dataset_root=dataset_root_from_env(),
            publisher=ResultArtifactPublisher(result_artifact_root_from_env()),
            work_root=work_root_from_env(),
            gatk_executable_sha256=runner.expected_sha256,
            gatk_runtime_bundle_sha256=runner.runtime_bundle_sha256(),
            gatk_version=runner.expected_version,
        )
    finally:
        engine.dispose()


def _execute_l2f2_job(
    engine: Engine,
    authority: PhaseAAuthority,
    *,
    worker_id: str,
    runner: GatkRunner,
    dataset_root: DatasetRoot,
    publisher: ResultArtifactPublisher,
    work_root: Path,
    gatk_executable_sha256: str,
    gatk_runtime_bundle_sha256: str,
    gatk_version: str,
) -> L2F2DispatchResult | None:
    """PRIVATE least-privilege orchestration core. TEST-ONLY as a direct entry point.

    The accepted production boundary is :func:`execute_next_l2f2_phase_a_job`, which accepts no
    runner and constructs the real one itself. This helper exists so tests can drive the identical
    least-privilege sequence with a deterministic runner; it is never exported.
    """
    from minos_engine.storage.l2f_job_claim import validate_worker_id

    validate_worker_id(worker_id)
    plan_hash = authority.plan_hash

    with engine.connect() as conn, conn.begin():
        authorize_baseline_runner_connection(conn)
        claimed = (
            conn.execute(
                text("SELECT job_id, job_key FROM experiments.minos_l2f_claim_next_job(:h, :w)"),
                {"h": plan_hash, "w": worker_id},
            )
            .mappings()
            .one_or_none()
        )
    if claimed is None:
        return None
    job_id, job_key = str(claimed["job_id"]), str(claimed["job_key"])

    # ---- everything while merely CLAIMED recovers to PENDING ------------------------------
    try:
        with engine.connect() as conn, conn.begin():
            authorize_baseline_runner_connection(conn)
            prepared = _resolve_prepared(
                conn,
                plan_hash=plan_hash,
                job_id=job_id,
                job_key=job_key,
                worker_id=worker_id,
                dataset_root=dataset_root,
                gatk_executable_sha256=gatk_executable_sha256,
                gatk_runtime_bundle_sha256=gatk_runtime_bundle_sha256,
                gatk_version=gatk_version,
            )
    except BaseException:
        _release(engine, plan_hash=plan_hash, job_id=job_id, worker_id=worker_id)
        raise

    # ---- CLAIMED -> RUNNING ----------------------------------------------------------------
    try:
        with engine.connect() as conn, conn.begin():
            authorize_baseline_runner_connection(conn)
            conn.execute(
                text("SELECT * FROM experiments.minos_l2f_start_job(:h, :j, :w)"),
                {"h": plan_hash, "j": job_id, "w": worker_id},
            )
    except BaseException:
        _release(engine, plan_hash=plan_hash, job_id=job_id, worker_id=worker_id)
        raise

    return _run_and_finalize(
        engine,
        authority,
        prepared,
        worker_id=worker_id,
        runner=runner,
        publisher=publisher,
        work_root=work_root,
    )


def _release(engine: Engine, *, plan_hash: str, job_id: str, worker_id: str) -> None:
    """Return a merely-CLAIMED job to PENDING. Never called once the job is RUNNING."""
    with engine.connect() as conn, conn.begin():
        authorize_baseline_runner_connection(conn)
        conn.execute(
            text("SELECT * FROM experiments.minos_l2f_release_job(:h, :j, :w)"),
            {"h": plan_hash, "j": job_id, "w": worker_id},
        )


def _fail(
    engine: Engine,
    *,
    plan_hash: str,
    job_id: str,
    job_key: str,
    worker_id: str,
    failure_code: str,
    exit_code: int | None,
    stderr_sha256: str | None,
    runtime_ms: int,
) -> L2F2DispatchResult:
    """Record ONE durable failure, including how long the attempt actually took.

    ``runtime_ms`` is a measurement, never a placeholder: the frozen objective uses mean GATK
    runtime as a tie-break, so a fabricated duration would flow straight into candidate ranking.
    It is elapsed monotonic time for the attempt, and 0 means exactly "no GATK attempt elapsed",
    never "unknown". The runner still holds no direct DML on the failure ledger; the narrow
    SECURITY DEFINER writer is the only path.
    """
    if runtime_ms < 0:
        raise L2F2ExecutionError(f"elapsed runtime {runtime_ms} is not a measurement")
    with engine.connect() as conn, conn.begin():
        authorize_baseline_runner_connection(conn)
        conn.execute(
            text("SELECT * FROM experiments.minos_l2f_fail_job(:h, :j, :w, :c, :e, :s, :rt)"),
            {
                "h": plan_hash,
                "j": job_id,
                "w": worker_id,
                "c": failure_code,
                "e": exit_code,
                "s": stderr_sha256,
                "rt": runtime_ms,
            },
        )
    return L2F2DispatchResult(
        job_id=job_id,
        job_key=job_key,
        plan_hash=plan_hash,
        worker_id=worker_id,
        status="FAILED",
        failure_code=failure_code,
    )


def _failure_code(exc: BaseException) -> tuple[str, int | None, str | None]:
    from minos_engine.experiments.execution_contract import (
        GatkExecutionError,
        GatkTimeoutError,
    )
    from minos_engine.experiments.execution_contract import (
        GatkOutputError as _OutputError,
    )

    if isinstance(exc, GatkTimeoutError):
        return "GATK_TIMEOUT", None, None
    if isinstance(exc, GatkExecutionError):
        return "GATK_NONZERO_EXIT", getattr(exc, "exit_code", None), None
    if isinstance(exc, _OutputError):
        return "GATK_OUTPUT_INVALID", None, None
    return "EXECUTION_ERROR", None, None


def _run_and_finalize(
    engine: Engine,
    authority: PhaseAAuthority,
    prepared: _Prepared,
    *,
    worker_id: str,
    runner: GatkRunner,
    publisher: ResultArtifactPublisher,
    work_root: Path,
) -> L2F2DispatchResult:
    """Run GATK for a RUNNING job and drive it to exactly one durable terminal outcome."""
    plan_hash = authority.plan_hash
    job_id, job_key = prepared.job_id, prepared.job_key
    workspace: AttemptWorkspace | None = None
    # a MONOTONIC clock, never wall time: the elapsed attempt duration is a measurement the
    # frozen objective uses as a tie-break, and a clock step must not be able to move it.
    attempt_started = time.monotonic_ns()

    def _elapsed_ms() -> int:
        return max(0, (time.monotonic_ns() - attempt_started) // 1_000_000)

    try:
        try:
            workspace = create_attempt_workspace(
                work_root,
                name=f"l2f2-{job_id}-{uuid.uuid4().hex}",
                error=ExecutionWorkspaceError,
            )
            vcf_path = workspace.path / OUTPUT_VCF_NAME
            if vcf_path.is_symlink() or vcf_path.exists():
                raise ExecutionWorkspaceError(f"output path {vcf_path} already exists")
            argv = render_execution_argv(
                effective_config=dict(prepared.config.effective_config),
                inputs=prepared.inputs,
                reference_path=str(prepared.paths.reference),
                bam_path=str(prepared.paths.bam),
                output_path=str(vcf_path),
            )
            outcome = runner.run(
                argv=argv,
                work_dir=workspace.path,
                vcf_path=vcf_path,
                inputs=prepared.inputs,
                expected_runtime_bundle_sha256=prepared.invocation.gatk_runtime_bundle_sha256,
            )
            acquired = acquire_produced_output(workspace, prepared.inputs)
            if (
                acquired.sha256 != outcome.vcf_sha256
                or acquired.size_bytes != outcome.vcf_size_bytes
            ):
                raise GatkOutputError(
                    f"produced VCF for job {job_id} changed between execution and acquisition"
                )
            vcf_bytes = acquired.payload
            outcome = outcome.model_copy(
                update={"vcf_sha256": acquired.sha256, "vcf_size_bytes": acquired.size_bytes}
            )
        except AmbiguousExecutionCommitError:
            raise
        except BaseException as exc:
            code, exit_code, stderr = _failure_code(exc)
            recorded = _fail(
                engine,
                plan_hash=plan_hash,
                job_id=job_id,
                job_key=job_key,
                worker_id=worker_id,
                failure_code=code,
                exit_code=exit_code,
                stderr_sha256=stderr,
                # the attempt genuinely elapsed, whether GATK exited non-zero, timed out or
                # produced unusable output; that duration is what is recorded.
                runtime_ms=_elapsed_ms(),
            )
            if code in ("GATK_TIMEOUT", "GATK_NONZERO_EXIT", "GATK_OUTPUT_INVALID"):
                return recorded
            raise ExecutionRecordedFailureError(
                f"job {job_id} failed after entering RUNNING and was durably recorded as FAILED",
                failure_code=code,
            ) from exc

        try:
            return _complete_success(
                engine,
                authority,
                prepared,
                outcome,
                worker_id=worker_id,
                publisher=publisher,
                vcf_bytes=vcf_bytes,
            )
        except AmbiguousExecutionCommitError:
            raise
        except BaseException as exc:
            recorded = _fail(
                engine,
                plan_hash=plan_hash,
                job_id=job_id,
                job_key=job_key,
                worker_id=worker_id,
                failure_code="EXECUTION_ERROR",
                exit_code=None,
                stderr_sha256=None,
                # GATK itself finished; the runtime that elapsed is the one it actually took,
                # and persistence failing afterwards does not change that measurement.
                runtime_ms=outcome.runtime_ms,
            )
            raise ExecutionRecordedFailureError(
                f"job {job_id} could not persist its success and was durably recorded as FAILED",
                failure_code=recorded.failure_code or "EXECUTION_ERROR",
            ) from exc
    finally:
        remove_attempt_workspace(workspace)


def _complete_success(
    engine: Engine,
    authority: PhaseAAuthority,
    prepared: _Prepared,
    outcome: GatkExecutionOutcome,
    *,
    worker_id: str,
    publisher: ResultArtifactPublisher,
    vcf_bytes: bytes,
) -> L2F2DispatchResult:
    """Publish both artifacts, register them narrowly, then transition — no admin role."""
    plan_hash = authority.plan_hash
    result_hash = compute_result_hash(
        plan_hash=plan_hash,
        job_key=prepared.job_key,
        inputs=prepared.inputs,
        config=prepared.config,
        invocation=prepared.invocation,
        outcome=outcome,
    )
    manifest = _manifest(
        plan_hash=plan_hash,
        prepared=prepared,
        outcome=outcome,
        worker_id=worker_id,
        result_hash=result_hash,
    )
    manifest_bytes = build_result_manifest_bytes(manifest)
    manifest_sha = hashlib.sha256(manifest_bytes).hexdigest()

    with engine.connect() as conn, conn.begin():
        authorize_baseline_runner_connection(conn)
        vcf_art = publisher.publish(vcf_bytes, kind="vcf", sha256=outcome.vcf_sha256)
        man_art = publisher.publish(manifest_bytes, kind="result_manifest", sha256=manifest_sha)
        vcf_artifact_id = _register(
            conn,
            kind="vcf",
            sha256=vcf_art.sha256,
            uri=vcf_art.uri,
            size_bytes=vcf_art.size_bytes,
        )
        manifest_artifact_id = _register(
            conn,
            kind="result_manifest",
            sha256=man_art.sha256,
            uri=man_art.uri,
            size_bytes=man_art.size_bytes,
        )
        try:
            row = conn.execute(
                text(
                    "SELECT result_id, created FROM experiments.minos_l2f_complete_job_success("
                    ":h, :j, :w, :k, :ch, :ps, :ii, :la, :ex, :gv, :va, :vs, :ma, :ms, :rh, :rt)"
                ),
                {
                    "h": plan_hash,
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
            ).one()
        except DBAPIError as exc:
            raise L2F2ExecutionError(f"success persistence refused: {exc}") from exc

    return L2F2DispatchResult(
        job_id=prepared.job_id,
        job_key=prepared.job_key,
        plan_hash=plan_hash,
        worker_id=worker_id,
        status="SUCCEEDED",
        execution_result_id=str(row[0]),
        result_hash=result_hash,
        vcf_sha256=outcome.vcf_sha256,
        result_manifest_sha256=manifest_sha,
        runtime_ms=outcome.runtime_ms,
    )
