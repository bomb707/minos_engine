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
from typing import Any, Protocol

from sqlalchemy import Connection, Engine, text
from sqlalchemy.exc import DBAPIError

from minos_engine.baseline.phase_a import build_phase_a_authority
from minos_engine.common.errors import MinosEngineError
from minos_engine.experiments.execution_contract import (
    EXECUTION_RESULT_SCHEMA_V2,
    ExecutionConfig,
    ExecutionInput,
    ExecutionResultManifestV2,
    GatkExecutionOutcome,
    GatkOutputError,
    LogicalGatkInvocation,
    build_result_manifest_bytes,
    compute_result_hash_v2,
)
from minos_engine.experiments.execution_environment import GatkExecutionEnvironment
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
    "VALIDATION_DATABASE_NAME",
    "VALIDATION_REVISION",
    "BaselineRunnerAuthorityError",
    "L2F2DispatchResult",
    "L2F2ExecutionError",
    "execute_next_l2f2_phase_a_job",
    "execute_next_l2f2_phase_b_job",
    "execute_next_l2f2_phase_c_job",
    "execute_next_l2f2_phase_d_job",
    "authorize_validation_runner_connection",
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
#: ``0014`` gave a failed execution an authoritative elapsed runtime and dropped the narrower
#: six-argument failure writer. ``0015`` binds the EXECUTION ENVIRONMENT identity into every
#: durable outcome and drops both narrower writers, so a runner at ``0015`` genuinely cannot
#: record an outcome against an older database — which is deliberate: an outcome that cannot say
#: which runtime produced it is what made the first Phase-A campaign unusable. ``0016`` then adds
#: the Phase-B execution authority and its resolver, without which no Phase-B job can be run at
#: all: a runner that accepted ``0015`` would claim a Phase-B job and then fail to resolve it.
#: ``0017`` takes SUPERUSER authority away from the two ``0011`` definers this boundary calls, so
#: a runner at ``0016``/``0017`` is one whose privileged calls still execute with more authority than the
#: control plane has, and ``0018`` does the same for the evaluator's four. The runner and the
#: evaluator share this store, so the revision this boundary accepts tracks both of them.
BASELINE_REVISION = "0020_l2f2_phase_c_execution"

#: L2-F2-F runs in a SEPARATE store, so from ``0021`` the repository head and the TRAIN baseline's
#: required revision are no longer the same string — and must not be. The TRAIN baseline is
#: scientifically closed at ``0020``; migrating it to ``0021`` to keep one number tidy would alter
#: a completed 500-observation ledger's database for the convenience of a constant. Each store
#: therefore pins its own revision, and the runner checks the pin belonging to the store it is
#: actually connected to.
VALIDATION_DATABASE_NAME = "minos_l2f2_validation"
VALIDATION_REVISION = "0022_l2f2_validation_store"

#: the ONLY MINOS group role the runner service may hold.
_REQUIRED_MEMBERSHIP = "minos_runner"
_FORBIDDEN_MEMBERSHIPS = ("minos_admin", "minos_evaluator", "minos_trainer", "minos_live")

#: one narrow resolver per phase, each with its OWN fixed phase predicate inside the database.
#: Nothing here is selected by a caller: the mapping is keyed by the authority's own phase, so a
#: Phase-A authority cannot reach the Phase-B resolver even by mistake, and neither function falls
#: back to the other phase's authorities.
_RESOLVE_SQL_BY_PHASE: dict[str, str] = {
    "PHASE_A": "SELECT * FROM experiments.l2f2_resolve_claimed_execution(:h, :j, :w)",
    "PHASE_B": "SELECT * FROM experiments.l2f2_resolve_claimed_phase_b_execution(:h, :j, :w)",
    "PHASE_C": "SELECT * FROM experiments.l2f2_resolve_claimed_phase_c_execution(:h, :j, :w)",
    #: Phase D resolves VALIDATION members and refuses TRAIN. Its predicate is the mutually
    #: exclusive complement of the three above, so no worker can cross the partition boundary by
    #: reaching for the wrong resolver: the row it wants is not visible through the other one.
    "PHASE_D": "SELECT * FROM experiments.l2f2_resolve_claimed_phase_d_execution(:h, :j, :w)",
}
#: the truth-free Phase-B bootstrap (0019). No arguments: the database decides what is
#: authorized, so a worker cannot nominate a plan or a runtime.
_PHASE_B_BOOTSTRAP_SQL = (
    "SELECT plan_hash, execution_environment_hash FROM "
    "experiments.l2f2_resolve_phase_b_runner_bootstrap()"
)
#: the truth-free Phase-C bootstrap (0020). Same contract, same absence of arguments.
_PHASE_C_BOOTSTRAP_SQL = (
    "SELECT plan_hash, execution_environment_hash FROM "
    "experiments.l2f2_resolve_phase_c_runner_bootstrap()"
)
#: the truth-free Phase-D bootstrap (0021). Same contract, same absence of arguments.
_PHASE_D_BOOTSTRAP_SQL = (
    "SELECT plan_hash, execution_environment_hash FROM "
    "experiments.l2f2_resolve_phase_d_runner_bootstrap()"
)
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
    _authorize_runner_connection(
        conn, database_name=BASELINE_DATABASE_NAME, revision=BASELINE_REVISION
    )


def authorize_validation_runner_connection(conn: Connection) -> None:
    """The same boundary, pinned to the SEPARATE validation store.

    Identical in every respect except which database and which revision it accepts. A validation
    worker pointed at the closed TRAIN baseline is refused by name, and a TRAIN worker pointed at
    the validation store is refused by name — neither can reach the other's jobs even before the
    per-phase resolvers get a say.
    """
    _authorize_runner_connection(
        conn, database_name=VALIDATION_DATABASE_NAME, revision=VALIDATION_REVISION
    )


def _authorize_runner_connection(conn: Connection, *, database_name: str, revision: str) -> None:
    """The single boundary body. Both stores get exactly the same checks, on their own pins."""
    database = str(conn.execute(text("SELECT current_database()")).scalar_one())
    if database != database_name:
        raise BaselineRunnerAuthorityError(
            f"the L2-F2 runner refuses database {database!r}; it executes only against "
            f"{database_name!r}"
        )
    live = conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one_or_none()
    if live != revision:
        raise BaselineRunnerAuthorityError(
            f"baseline database revision is {live!r}, expected {revision!r}"
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
    phase: str,
    plan_hash: str,
    job_id: str,
    job_key: str,
    worker_id: str,
    dataset_root: DatasetRoot,
    gatk_executable_sha256: str,
    gatk_runtime_bundle_sha256: str,
    gatk_version: str,
) -> _Prepared:
    """Resolve and BYTE-VERIFY the execution identity through the narrow interface only.

    ``phase`` selects WHICH narrow resolver, and comes from the authority itself. Each database
    function then requires an authority of its own phase, so the phase is enforced twice: once by
    which function is called and once inside it.
    """
    resolve_sql = _RESOLVE_SQL_BY_PHASE.get(phase)
    if resolve_sql is None:
        raise L2F2ExecutionError(f"no L2-F2 execution resolver is accepted for phase {phase!r}")
    row = (
        conn.execute(text(resolve_sql), {"h": plan_hash, "j": job_id, "w": worker_id})
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
    execution_environment_hash: str,
) -> ExecutionResultManifestV2:
    inputs = prepared.inputs
    return ExecutionResultManifestV2(
        schema_version=EXECUTION_RESULT_SCHEMA_V2,
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
        execution_environment_hash=execution_environment_hash,
        vcf_sha256=outcome.vcf_sha256,
        vcf_size_bytes=outcome.vcf_size_bytes,
        result_hash=result_hash,
        runtime_ms=outcome.runtime_ms,
        worker_id=worker_id,
        generated_at=_now_utc(),
    )


class _PlanAuthority(Protocol):
    """What the execution core actually needs from an authority: which plan, and which phase.

    Phase A and Phase B are different scientific questions but the SAME least-privilege execution
    sequence, so the core is typed by the two properties it uses rather than duplicated per phase.

    ``phase`` is read from the authority rather than passed in beside it. That is the whole point:
    an authority is a phase, so pairing the wrong resolver with a plan is not a mistake a caller
    can make — there is no argument in which to make it.
    """

    @property
    def plan_hash(self) -> str: ...

    @property
    def phase(self) -> str: ...


@dataclass(frozen=True)
class _PhaseBRunnerAuthority:
    """The ONLY thing the execution core needs about Phase B: which plan, and which phase.

    Deliberately not a :class:`~minos_engine.baseline.phase_b.PhaseBAuthority`. That object is a
    scientific derivation — 48 configurations, ten members, six dimensions, six anchors, the
    Phase-A analysis they came from — and building it requires reading the completed Phase-A
    SCIENTIFIC ledger, which the runner is denied on purpose. A runner that could construct it
    could read the answer key.
    """

    plan_hash: str
    phase: str = "PHASE_B"


@dataclass(frozen=True)
class _PhaseBRunnerBootstrap:
    """What the database is willing to tell a truth-free worker about Phase B."""

    authority: _PhaseBRunnerAuthority
    execution_environment_hash: str


def _resolve_phase_c_runner_bootstrap(conn: Connection) -> _PhaseBRunnerBootstrap:
    """The Phase-C ticket: which plan this worker may claim within, and under what runtime.

    Identical in shape and in spirit to the Phase-B bootstrap — no arguments, no science, and no
    fallback — because the reason is identical: deriving the Phase-C authority means reading the
    completed Phase-B SCIENTIFIC ledger, which the runner is denied.
    """
    row = conn.execute(text(_PHASE_C_BOOTSTRAP_SQL)).mappings().one_or_none()
    if row is None:  # pragma: no cover - the function raises rather than returning empty
        raise L2F2ExecutionError("the Phase-C runner bootstrap returned no row")
    plan_hash = str(row["plan_hash"])
    environment = str(row["execution_environment_hash"])
    if not plan_hash or not environment:
        raise L2F2ExecutionError("the Phase-C runner bootstrap returned an incomplete ticket")
    return _PhaseBRunnerBootstrap(
        authority=_PhaseBRunnerAuthority(plan_hash=plan_hash, phase="PHASE_C"),
        execution_environment_hash=environment,
    )


def _resolve_phase_d_runner_bootstrap(conn: Connection) -> _PhaseBRunnerBootstrap:
    """The Phase-D ticket: which validation plan this worker may claim within, and under what
    runtime.

    Same contract as Phase B's and Phase C's — no arguments, no science, no fallback. The reason
    differs slightly: deriving the Phase-D authority means verifying a frozen finalist artifact
    against the completed TRAIN ledger, and a validation worker has neither the artifact nor the
    ledger. It is told two strings and nothing else.
    """
    row = conn.execute(text(_PHASE_D_BOOTSTRAP_SQL)).mappings().one_or_none()
    if row is None:  # pragma: no cover - the function raises rather than returning empty
        raise L2F2ExecutionError("the Phase-D runner bootstrap returned no row")
    plan_hash = str(row["plan_hash"])
    environment = str(row["execution_environment_hash"])
    if not plan_hash or not environment:
        raise L2F2ExecutionError("the Phase-D runner bootstrap returned an incomplete ticket")
    return _PhaseBRunnerBootstrap(
        authority=_PhaseBRunnerAuthority(plan_hash=plan_hash, phase="PHASE_D"),
        execution_environment_hash=environment,
    )


def execute_next_l2f2_phase_d_job(*, worker_id: str) -> L2F2DispatchResult | None:
    """THE accepted L2-F2-F validation execution entry. Same core, same truth-free bootstrap.

    Phase D confirms four already-frozen configurations across all ten VALIDATION members. Which
    four was decided before any validation byte existed and is durable as a verified finalist
    artifact and a recorded ``PHASE_D`` authority; this entry asks the database only what a
    truth-free worker may know.

    The runtime check is the one every later phase enforces, for the same reason: validation
    confirms a design chosen from one runtime's numbers, so it must not mix runtimes. A validation
    execution produced under a different GATK is not a confirmation of anything.
    """
    from minos_engine.experiments.execution_contract import GatkRuntimeIdentityError
    from minos_engine.storage.database import create_db_engine
    from minos_engine.storage.l2f_gatk_runner import SubprocessGatkRunner, work_root_from_env
    from minos_engine.storage.l2f_job_claim import validate_worker_id

    validate_worker_id(worker_id)
    runner = SubprocessGatkRunner.from_env()
    environment = runner.preflight()
    engine = create_db_engine()
    try:
        with engine.connect() as conn:
            authorize_validation_runner_connection(conn)
            bootstrap = _resolve_phase_d_runner_bootstrap(conn)
        observed = environment.environment_hash()
        if observed != bootstrap.execution_environment_hash:
            raise GatkRuntimeIdentityError(
                f"this worker's execution environment is {observed}, but the frozen search ran "
                f"under {bootstrap.execution_environment_hash}; validation confirms a design "
                "chosen from that runtime's numbers and must not mix runtimes"
            )
        return _execute_l2f2_job(
            engine,
            bootstrap.authority,
            worker_id=worker_id,
            runner=runner,
            dataset_root=dataset_root_from_env(),
            publisher=ResultArtifactPublisher(result_artifact_root_from_env()),
            work_root=work_root_from_env(),
            execution_environment=environment,
        )
    finally:
        engine.dispose()


def execute_next_l2f2_phase_c_job(*, worker_id: str) -> L2F2DispatchResult | None:
    """THE accepted L2-F2 Phase-C execution entry. Same core, same truth-free bootstrap.

    Phase C confirms ten already-promoted configurations across the whole TRAIN partition. Which
    ten, on which fifty members, was decided by the CONTROL PLANE from a finished Phase-B screen
    and is already durable as a persisted plan and a recorded ``PHASE_C`` authority; this entry
    asks the database only what a truth-free worker may know.

    The runtime check is the same one Phase B enforces, for the same reason: the design being
    confirmed was chosen from the numbers one runtime produced.
    """
    from minos_engine.experiments.execution_contract import GatkRuntimeIdentityError
    from minos_engine.storage.database import create_db_engine
    from minos_engine.storage.l2f_gatk_runner import SubprocessGatkRunner, work_root_from_env
    from minos_engine.storage.l2f_job_claim import validate_worker_id

    validate_worker_id(worker_id)
    runner = SubprocessGatkRunner.from_env()
    environment = runner.preflight()
    engine = create_db_engine()
    try:
        with engine.connect() as conn:
            authorize_baseline_runner_connection(conn)
            bootstrap = _resolve_phase_c_runner_bootstrap(conn)
        observed = environment.environment_hash()
        if observed != bootstrap.execution_environment_hash:
            raise GatkRuntimeIdentityError(
                f"this worker's execution environment is {observed}, but the baseline search's "
                f"completed Phase B ran under {bootstrap.execution_environment_hash}; Phase C "
                "confirms a design chosen from that runtime's numbers and must not mix runtimes"
            )
        return _execute_l2f2_job(
            engine,
            bootstrap.authority,
            worker_id=worker_id,
            runner=runner,
            dataset_root=dataset_root_from_env(),
            publisher=ResultArtifactPublisher(result_artifact_root_from_env()),
            work_root=work_root_from_env(),
            execution_environment=environment,
        )
    finally:
        engine.dispose()


def _resolve_phase_b_runner_bootstrap(conn: Connection) -> _PhaseBRunnerBootstrap:
    """Ask the ONE narrow function which Phase-B plan this worker may consume, and under what.

    The call takes no arguments, so there is no parameter through which a worker could nominate
    another plan or another runtime; every condition behind the answer — one PHASE_B authority
    under the frozen protocol, bound to its exact persisted TRAIN plan at the frozen 10 x 48
    shape, one PHASE_A authority whose campaign is complete and terminal, and exactly one
    execution environment across its outcomes — is enforced inside the database.

    There is no fallback. A permission error here is a provisioning fault to be fixed, never a
    reason to reach for a wider connection.
    """
    row = conn.execute(text(_PHASE_B_BOOTSTRAP_SQL)).mappings().one_or_none()
    if row is None:  # pragma: no cover - the function raises rather than returning empty
        raise L2F2ExecutionError("the Phase-B runner bootstrap returned no row")
    plan_hash = str(row["plan_hash"])
    environment = str(row["execution_environment_hash"])
    if not plan_hash or not environment:
        raise L2F2ExecutionError("the Phase-B runner bootstrap returned an incomplete ticket")
    return _PhaseBRunnerBootstrap(
        authority=_PhaseBRunnerAuthority(plan_hash=plan_hash),
        execution_environment_hash=environment,
    )


def execute_next_l2f2_phase_b_job(*, worker_id: str) -> L2F2DispatchResult | None:
    """THE accepted L2-F2 Phase-B execution entry. Same core, truth-free bootstrap.

    The scientific derivation of Phase B — which 48 configurations, on which ten members, chosen
    from which Phase-A analysis — happens in the CONTROL PLANE, long before any of this, and is
    already durable as a persisted plan and a recorded ``PHASE_B`` execution authority. This entry
    therefore asks the database only what a truth-free worker may know: the plan hash it is
    authorized to claim within, and the execution environment the completed Phase-A campaign ran
    under. It never builds a ``PhaseBAuthority``, never reads the ``evaluation`` schema, and never
    opens an administrative connection.

    The runtime check that follows is Phase B's own: this worker's environment must be the one
    that produced the numbers the design was chosen from, or it refuses BEFORE claiming anything.

    Four things must be true, each failing closed on its own: the store is at the exact
    :data:`BASELINE_REVISION`, a ``PHASE_B`` execution authority is prepared over a persisted
    plan, the completed Phase-A campaign carries a single execution environment, and this worker's
    runtime is that environment.
    """
    from minos_engine.experiments.execution_contract import GatkRuntimeIdentityError
    from minos_engine.storage.database import create_db_engine
    from minos_engine.storage.l2f_gatk_runner import SubprocessGatkRunner, work_root_from_env
    from minos_engine.storage.l2f_job_claim import validate_worker_id

    validate_worker_id(worker_id)
    runner = SubprocessGatkRunner.from_env()
    environment = runner.preflight()
    engine = create_db_engine()
    try:
        with engine.connect() as conn:
            authorize_baseline_runner_connection(conn)
            bootstrap = _resolve_phase_b_runner_bootstrap(conn)
        observed = environment.environment_hash()
        if observed != bootstrap.execution_environment_hash:
            raise GatkRuntimeIdentityError(
                f"this worker's execution environment is {observed}, but the baseline search's "
                f"completed Phase A ran under {bootstrap.execution_environment_hash}; Phase B "
                "explores a design chosen from that runtime's numbers and must not mix runtimes"
            )
        return _execute_l2f2_job(
            engine,
            bootstrap.authority,
            worker_id=worker_id,
            runner=runner,
            dataset_root=dataset_root_from_env(),
            publisher=ResultArtifactPublisher(result_artifact_root_from_env()),
            work_root=work_root_from_env(),
            execution_environment=environment,
        )
    finally:
        engine.dispose()


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
    runner = SubprocessGatkRunner.from_env()
    # THE pre-claim runtime gate, deliberately before any database call. It proves this worker can
    # actually run GATK — pinned launcher, pinned scientific payload, explicit content-verified
    # interpreter, provisioned JVM, and the real bundle reporting the pinned version — and raises
    # without touching a single row if it cannot. A worker whose runtime is broken must never
    # consume a candidate observation: that is precisely how a missing interpreter turned into
    # five candidate failures for configurations GATK never parsed.
    environment = runner.preflight()
    engine = create_db_engine()
    try:
        return _execute_l2f2_job(
            engine,
            authority,
            worker_id=worker_id,
            runner=runner,
            dataset_root=dataset_root_from_env(),
            publisher=ResultArtifactPublisher(result_artifact_root_from_env()),
            work_root=work_root_from_env(),
            execution_environment=environment,
        )
    finally:
        engine.dispose()


def _execute_l2f2_job(
    engine: Engine,
    authority: _PlanAuthority,
    *,
    worker_id: str,
    runner: GatkRunner,
    dataset_root: DatasetRoot,
    publisher: ResultArtifactPublisher,
    work_root: Path,
    execution_environment: GatkExecutionEnvironment,
) -> L2F2DispatchResult | None:
    """PRIVATE least-privilege orchestration core. TEST-ONLY as a direct entry point.

    The accepted production boundary is :func:`execute_next_l2f2_phase_a_job`, which accepts no
    runner, constructs the real one itself and PREFLIGHTS it before claiming anything. This helper
    exists so tests can drive the identical least-privilege sequence with a deterministic runner;
    it is never exported.

    The GATK identity is taken from ``execution_environment`` rather than from three loose
    strings, so the identity a result is recorded under and the runtime that produced it cannot
    disagree.
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
                phase=authority.phase,
                plan_hash=plan_hash,
                job_id=job_id,
                job_key=job_key,
                worker_id=worker_id,
                dataset_root=dataset_root,
                gatk_executable_sha256=execution_environment.gatk_launcher_sha256,
                gatk_runtime_bundle_sha256=execution_environment.gatk_runtime_bundle_sha256,
                gatk_version=execution_environment.gatk_version,
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
        execution_environment=execution_environment,
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
    execution_environment_hash: str,
) -> L2F2DispatchResult:
    """Record ONE durable failure with the evidence that identifies it.

    ``runtime_ms`` is a measurement, never a placeholder: the frozen objective uses mean GATK
    runtime as a tie-break, so a fabricated duration would flow straight into candidate ranking.
    It is elapsed monotonic time for the attempt, and 0 means exactly "no GATK attempt elapsed",
    never "unknown". The runner still holds no direct DML on the failure ledger; the narrow
    SECURITY DEFINER writer is the only path.

    ``exit_code`` and ``stderr_sha256`` arrive from the STRUCTURED exception, never from parsing a
    message, and ``execution_environment_hash`` records which runtime the attempt was made under.
    Together they are what makes a failure diagnosable from the ledger alone: a stored 127 would
    have identified a missing interpreter without re-running anything.
    """
    if runtime_ms < 0:
        raise L2F2ExecutionError(f"elapsed runtime {runtime_ms} is not a measurement")
    with engine.connect() as conn, conn.begin():
        authorize_baseline_runner_connection(conn)
        conn.execute(
            text("SELECT * FROM experiments.minos_l2f_fail_job(:h, :j, :w, :c, :e, :s, :rt, :ee)"),
            {
                "h": plan_hash,
                "j": job_id,
                "w": worker_id,
                "c": failure_code,
                "e": exit_code,
                "s": stderr_sha256,
                "rt": runtime_ms,
                "ee": execution_environment_hash,
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
    """Classify ONE failed attempt by execution STAGE, never by reading stderr text.

    The distinction that matters is whose fault it is. A HaplotypeCaller process that started
    under a verified runtime and exited nonzero is the candidate's configuration failing; a
    runtime that could not be established, or that moved underneath the job, is ours. Both used to
    arrive here as a bare ``GatkExecutionError`` and both were recorded as GATK_NONZERO_EXIT —
    which is how a missing interpreter came to be charged to five candidates.
    """
    from minos_engine.experiments.execution_contract import (
        GatkExecutionError,
        GatkNonzeroExitError,
        GatkRuntimeIdentityError,
        GatkTimeoutError,
    )
    from minos_engine.experiments.execution_contract import (
        GatkOutputError as _OutputError,
    )

    if isinstance(exc, GatkTimeoutError):
        return "GATK_TIMEOUT", None, None
    # OURS, and checked before the GATK families below: the runtime could not be established, or
    # did not stay the one this execution is identified by.
    if isinstance(exc, GatkRuntimeIdentityError):
        return "EXECUTION_ERROR", None, None
    if isinstance(exc, GatkNonzeroExitError):
        # the structured evidence, carried BY the exception. Never parsed out of a message.
        return "GATK_NONZERO_EXIT", exc.exit_code, exc.stderr_sha256
    if isinstance(exc, GatkExecutionError):
        # the process could not be executed at all: no exit code exists, and nothing whatever has
        # been demonstrated about the candidate.
        return "EXECUTION_ERROR", None, None
    if isinstance(exc, _OutputError):
        return "GATK_OUTPUT_INVALID", None, None
    return "EXECUTION_ERROR", None, None


def _run_and_finalize(
    engine: Engine,
    authority: _PlanAuthority,
    prepared: _Prepared,
    *,
    worker_id: str,
    runner: GatkRunner,
    publisher: ResultArtifactPublisher,
    work_root: Path,
    execution_environment: GatkExecutionEnvironment,
) -> L2F2DispatchResult:
    """Run GATK for a RUNNING job and drive it to exactly one durable terminal outcome."""
    plan_hash = authority.plan_hash
    job_id, job_key = prepared.job_id, prepared.job_key
    environment_hash = execution_environment.environment_hash()
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
                # the SAME runtime identity the outcome will be recorded under, re-verified by the
                # runner immediately before and immediately after HaplotypeCaller.
                expected_execution_environment_hash=environment_hash,
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
                execution_environment_hash=environment_hash,
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
                execution_environment_hash=environment_hash,
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
                execution_environment_hash=environment_hash,
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
    authority: _PlanAuthority,
    prepared: _Prepared,
    outcome: GatkExecutionOutcome,
    *,
    worker_id: str,
    publisher: ResultArtifactPublisher,
    vcf_bytes: bytes,
    execution_environment_hash: str,
) -> L2F2DispatchResult:
    """Publish both artifacts, register them narrowly, then transition — no admin role."""
    plan_hash = authority.plan_hash
    result_hash = compute_result_hash_v2(
        plan_hash=plan_hash,
        job_key=prepared.job_key,
        inputs=prepared.inputs,
        config=prepared.config,
        invocation=prepared.invocation,
        outcome=outcome,
        execution_environment_hash=execution_environment_hash,
    )
    manifest = _manifest(
        plan_hash=plan_hash,
        prepared=prepared,
        outcome=outcome,
        worker_id=worker_id,
        result_hash=result_hash,
        execution_environment_hash=execution_environment_hash,
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
                    ":h, :j, :w, :k, :ch, :ps, :ii, :la, :ex, :gv, :va, :vs, :ma, :ms, :rh, :rt, "
                    ":ee)"
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
                    "ee": execution_environment_hash,
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
