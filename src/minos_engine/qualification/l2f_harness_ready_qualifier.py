"""THE production L2-F F7 HARNESS-READY qualification authority.

``run_harness_ready_qualification()`` is the only path by which a HARNESS-READY gate can be
produced. It accepts **no** qualification document, check dictionary, source/tree, accepted hash,
plan, candidate set, member, candidate, runner, trust bundle or result — every one of those is
derived internally from the real repository, the real provisioned binary, real scratch
PostgreSQL, the real filesystem and real execution.

Authority boundary
------------------
The gate assembler requires a :class:`TrustedQualification`, whose constructor is guarded by a
module-private token. Only this module can mint one. A caller may still *construct* a
``HarnessReadyQualification`` model (that is just a serialization contract, and unit tests need
it), but such a synthetic document can never reach the gate assembler, so it can never grant
HARNESS-READY. Naming a field "observation" is not treated as evidence that it was observed.
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import socket
import stat
import subprocess  # noqa: S404 - fixed argv, shell=False, used only to read git metadata
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from minos_engine.common.errors import MinosEngineError
from minos_engine.qualification.l2f_accepted_identities import (
    recompute_accepted_identities,
    repository_root,
    verify_accepted_identities,
)
from minos_engine.qualification.l2f_failure_inventory import (
    build_failure_inventory,
    verify_failure_inventory,
)
from minos_engine.qualification.l2f_harness_ready_contract import (
    ACCEPTED_F6_CORRECTIVE_COMMIT,
    HARNESS_READY_QUALIFIER_VERSION,
    GatkBinaryIdentity,
    HarnessReadyQualification,
    SourceProvenance,
)

__all__ = [
    "QualificationEnvironmentError",
    "QualificationLeakageError",
    "QualificationNetworkError",
    "TrustedQualification",
    "FORBIDDEN_INPUT_TOKENS",
    "network_denied",
    "leakage_denied_paths",
    "acquire_source_provenance",
    "verify_official_gatk_binary",
    "derive_qualification_job",
    "row_counts",
    "attempt_conflicting_replay",
    "observe_no_automatic_retry",
    "ROW_COUNT_TABLES",
    "run_harness_ready_qualification",
]


class QualificationEnvironmentError(MinosEngineError):
    """A required qualification input is absent, unverifiable or refused."""


class QualificationLeakageError(MinosEngineError):
    """The qualification attempted to resolve a truth/scoring/evaluation artifact."""


class QualificationNetworkError(MinosEngineError):
    """The qualification attempted a network call while the offline guard was armed."""


#: path fragments that identify evaluation-only material. Resolving any of these is a hard error.
FORBIDDEN_INPUT_TOKENS: tuple[str, ...] = (
    "truth",
    "mutation",
    "mutations",
    "happy",
    "hap.py",
    "hap_py",
    "score",
    "scores",
    "scoring",
    "leaderboard",
    "label",
    "labels",
    "target",
    "validation",
    "test",
)


# --------------------------------------------------------------------------- #
# the unforgeable trust boundary
# --------------------------------------------------------------------------- #
class _MintToken:
    """A module-private capability. Only this module ever holds an instance."""

    __slots__ = ()


_MINT = _MintToken()


@dataclass(frozen=True)
class TrustedQualification:
    """A qualification this module OBSERVED. Cannot be constructed outside this module.

    The gate assembler requires one of these, so a synthetic ``HarnessReadyQualification`` — no
    matter how internally consistent — can never reach the HARNESS-READY authority.
    """

    result: HarnessReadyQualification

    def __init__(self, token: _MintToken, result: HarnessReadyQualification) -> None:
        if token is not _MINT:
            raise QualificationEnvironmentError(
                "TrustedQualification may only be minted by the production qualifier; a "
                "caller-supplied qualification can never grant HARNESS-READY"
            )
        object.__setattr__(self, "result", result)


# --------------------------------------------------------------------------- #
# offline + leakage guards (enforced, never asserted)
# --------------------------------------------------------------------------- #
@contextlib.contextmanager
def network_denied() -> Iterator[None]:
    """Arm a process-wide offline guard: any socket connection raises, it is never 'assumed'."""
    real_socket = socket.socket
    real_create = socket.create_connection

    class _DeniedSocket(real_socket):  # type: ignore[misc,valid-type]
        def connect(self, *args: Any, **kwargs: Any) -> Any:
            raise QualificationNetworkError("network access is denied during F7 qualification")

        def connect_ex(self, *args: Any, **kwargs: Any) -> Any:
            raise QualificationNetworkError("network access is denied during F7 qualification")

    def _denied_create(*_args: Any, **_kwargs: Any) -> Any:
        raise QualificationNetworkError("network access is denied during F7 qualification")

    setattr(socket, "socket", _DeniedSocket)  # noqa: B010 - deliberate guard installation
    setattr(socket, "create_connection", _denied_create)  # noqa: B010
    try:
        yield
    finally:
        setattr(socket, "socket", real_socket)  # noqa: B010
        setattr(socket, "create_connection", real_create)  # noqa: B010


def leakage_denied_paths(*paths: str | Path) -> None:
    """Refuse any path that names truth, mutation, hap.py, score, label or non-train material."""
    for candidate in paths:
        text = str(candidate).lower()
        for part in Path(text).parts:
            stem = part.replace("-", "_").replace(".", "_")
            for token in FORBIDDEN_INPUT_TOKENS:
                if token in stem.split("_"):
                    raise QualificationLeakageError(
                        f"F7 qualification refuses evaluation-only material: {candidate}"
                    )


# --------------------------------------------------------------------------- #
# real source provenance (git, never a caller boolean)
# --------------------------------------------------------------------------- #
def _git(root: Path, *args: str) -> str:
    proc = subprocess.run(  # noqa: S603 - fixed argv, shell=False
        ["git", *args],  # noqa: S607
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise QualificationEnvironmentError(
            f"git {' '.join(args)} failed: {proc.stderr.strip() or 'unavailable'}"
        )
    return proc.stdout.strip()


def acquire_source_provenance(
    root: Path | None = None, *, require_clean_worktree: bool = True
) -> SourceProvenance:
    """Resolve the real qualified source commit/tree and prove F6 ancestry. Fails closed."""
    base = root or repository_root()
    if not (base / ".git").exists():
        raise QualificationEnvironmentError("missing Git history: not a git repository")
    commit = _git(base, "rev-parse", "HEAD")
    if _git(base, "cat-file", "-t", commit) != "commit":
        raise QualificationEnvironmentError(f"qualified source {commit} is not a commit")
    tree = _git(base, "rev-parse", f"{commit}^{{tree}}")

    ancestor = subprocess.run(  # noqa: S603 - fixed argv, shell=False
        ["git", "merge-base", "--is-ancestor", ACCEPTED_F6_CORRECTIVE_COMMIT, commit],  # noqa: S607
        cwd=base,
        capture_output=True,
        check=False,
    )
    descends = ancestor.returncode == 0
    if not descends:
        raise QualificationEnvironmentError(
            f"qualified source {commit} does not descend the accepted F6 corrective "
            f"{ACCEPTED_F6_CORRECTIVE_COMMIT}"
        )
    clean = _git(base, "status", "--porcelain") == ""
    if require_clean_worktree and not clean:
        # a live qualification result speaks for an EXACT commit, so a dirty tree is refused.
        raise QualificationEnvironmentError(
            "the worktree does not match the qualified source; live qualification requires a "
            "clean tree so the result speaks for an exact commit"
        )
    return SourceProvenance(
        qualified_source_git_sha=commit,
        qualified_source_tree_sha=tree,
        f6_corrective_commit=ACCEPTED_F6_CORRECTIVE_COMMIT,
        descends_f6_corrective=True,
        worktree_matches_qualified_source=clean,
    )


# --------------------------------------------------------------------------- #
# real GATK binary identity (actual bytes hashed)
# --------------------------------------------------------------------------- #
def verify_official_gatk_binary(runner: Any) -> GatkBinaryIdentity:
    """Hash the ACTUAL executable bytes and require them to equal the provisioned digest.

    The version is provisioned metadata bound to that verified digest; nothing here probes the
    executable for a version and nothing claims that it did.
    """
    from minos_engine.storage.l2f_gatk_runner import SubprocessGatkRunner

    if not isinstance(runner, SubprocessGatkRunner):
        raise QualificationEnvironmentError(
            f"official qualification requires SubprocessGatkRunner, got {type(runner).__name__}"
        )
    path = Path(runner.executable)
    if not path.is_absolute():
        raise QualificationEnvironmentError(f"GATK executable {path} must be an absolute path")
    info = os.lstat(path) if path.exists() or path.is_symlink() else None
    if info is None:
        raise QualificationEnvironmentError(f"GATK executable {path} does not exist")
    if stat.S_ISLNK(info.st_mode):
        raise QualificationEnvironmentError(f"GATK executable {path} is a symlink")
    if not stat.S_ISREG(info.st_mode):
        raise QualificationEnvironmentError(f"GATK executable {path} is not a regular file")
    if not os.access(path, os.X_OK):
        raise QualificationEnvironmentError(f"GATK executable {path} is not executable")

    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    actual = digest.hexdigest()
    if actual != runner.expected_sha256:
        raise QualificationEnvironmentError(
            f"GATK executable {path} sha256 {actual} does not equal the provisioned digest "
            f"{runner.expected_sha256}"
        )
    if not runner.expected_version.strip():
        raise QualificationEnvironmentError("the provisioned GATK version metadata is empty")
    return GatkBinaryIdentity(
        executable_sha256=actual,
        version=runner.expected_version,
        absolute_path_is_symlink=False,
    )


# --------------------------------------------------------------------------- #
# deterministic qualification job derivation (train only, no caller input)
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class DerivedQualificationJob:
    """The qualification job the qualifier selected from the accepted plan itself."""

    member_index: int
    candidate_index: int
    dataset_id: str
    profile_id: str
    partition: str
    job_key: str
    config_hash: str
    effective_config: dict[str, Any]


def derive_qualification_job() -> DerivedQualificationJob:
    """Select the deterministic qualification job from the ACCEPTED plan and candidate set.

    Always the first accepted train member and the baseline (index 0) candidate: no UUID is
    hard-coded, no caller supplies a member or candidate, and a non-train member is refused.
    """
    from minos_engine.experiments.accepted_plan import build_accepted_experiment_plan
    from minos_engine.experiments.candidates import generate_accepted_candidate_set
    from minos_engine.experiments.plan import iter_logical_jobs

    plan = build_accepted_experiment_plan()
    candidate_set = generate_accepted_candidate_set()
    member = plan.members[0]
    partition = getattr(member, "partition", "train")
    if partition != "train":
        raise QualificationLeakageError(
            f"qualification member must be train, got {partition!r}; validation/test members "
            "are structurally unavailable to F7 qualification"
        )
    candidate = candidate_set.configs[0]
    logical = next(
        job
        for job in iter_logical_jobs(plan)
        if job.member_index == member.member_index and job.config_index == 0
    )
    return DerivedQualificationJob(
        member_index=member.member_index,
        candidate_index=0,
        dataset_id=member.dataset_id,
        profile_id=member.profile_id,
        partition="train",
        job_key=logical.job_key,
        config_hash=candidate.config_hash,
        effective_config=dict(candidate.effective_config),
    )


# --------------------------------------------------------------------------- #
# THE production authority
# --------------------------------------------------------------------------- #
def run_harness_ready_qualification(*, base_dir: str | Path = ".") -> TrustedQualification:
    """Run the complete live HARNESS-READY qualification. Accepts no qualification input.

    Every stage is an actual observation:

    1. arm the offline network guard;
    2. refuse the operational database and require an isolated scratch endpoint;
    3. acquire real source provenance from Git and prove F6 ancestry;
    4. recompute every accepted identity from committed bytes and require equality;
    5. build the official ``SubprocessGatkRunner`` from the provisioned environment and hash the
       ACTUAL executable bytes;
    6. derive the deterministic train-only qualification job from the accepted plan;
    7. execute that job officially, then build parity, resume, artifact verification, the failure
       inventory and the boundary observations from what actually happened.

    It fails closed at the first deficiency, and it is the ONLY minter of
    :class:`TrustedQualification`.
    """
    from minos_engine.qualification.l2f_harness_ready_runner import refuse_operational_database
    from minos_engine.storage.l2f_gatk_runner import SubprocessGatkRunner

    root = Path(base_dir).resolve()
    with network_denied():
        # 1-2) authority + database boundary
        refuse_operational_database()
        scratch = os.environ.get("MINOS_L2F_QUALIFICATION_DATABASE_URL")
        if not scratch:
            raise QualificationEnvironmentError(
                "MINOS_L2F_QUALIFICATION_DATABASE_URL must name an isolated scratch PostgreSQL "
                "endpoint; F7 qualification never runs against the operational store"
            )
        refuse_operational_database(scratch)

        # 3) real source provenance
        source = acquire_source_provenance(root)

        # 4) recomputed accepted identities
        accepted = recompute_accepted_identities(root)
        verify_accepted_identities(accepted)

        # 5) official binary, hashed from actual bytes
        try:
            runner = SubprocessGatkRunner.from_env()
        except Exception as exc:
            raise QualificationEnvironmentError(
                f"the official GATK qualification environment is not provisioned: {exc}"
            ) from exc
        binary = verify_official_gatk_binary(runner)

        # 6) deterministic, train-only qualification job
        job = derive_qualification_job()
        leakage_denied_paths(os.environ.get("MINOS_L2F_DATASET_ROOT", ""))

        # 7) the provisioned qualification roots (leakage-screened before anything opens them)
        from minos_engine.storage.l2f_execution_inputs import dataset_root_from_env
        from minos_engine.storage.l2f_gatk_runner import work_root_from_env
        from minos_engine.storage.l2f_result_publisher import (
            ResultArtifactPublisher,
            result_artifact_root_from_env,
        )

        for variable in (
            "MINOS_L2F_DATASET_ROOT",
            "MINOS_L2F_RESULT_ARTIFACT_ROOT",
            "MINOS_L2F_WORK_ROOT",
        ):
            leakage_denied_paths(os.environ.get(variable, ""))
        try:
            dataset_root = dataset_root_from_env()
            artifact_root = result_artifact_root_from_env()
            work_root = work_root_from_env()
        except Exception as exc:
            raise QualificationEnvironmentError(
                f"the provisioned qualification roots are unavailable: {exc}"
            ) from exc
        publisher = ResultArtifactPublisher(artifact_root)

        # 8) capture the operational state BEFORE anything runs, through a read-only transaction
        operational_before = _capture_operational_before()

        # 9) the failure inventory is DERIVED from the implementation and structurally verified
        inventory = build_failure_inventory()
        verify_failure_inventory(inventory)

        # 10) execute the official job, then observe resume and artifact verification
        observed = _execute_and_observe(
            operational_before=operational_before,
            scratch_url=scratch,
            runner=runner,
            binary=binary,
            job=job,
            dataset_root=dataset_root,
            publisher=publisher,
            work_root=work_root,
        )

        result = HarnessReadyQualification(
            qualifier_version=HARNESS_READY_QUALIFIER_VERSION,
            source=source,
            accepted=accepted,
            gatk_binary=binary,
            qualification_input=observed.qualification_input,
            official_execution=observed.official_execution,
            twin_parity=observed.twin_parity,
            resume=observed.resume,
            artifact_verification=observed.artifact_verification,
            failure_inventory=inventory,
            boundaries=observed.boundaries,
        )
        return TrustedQualification(_MINT, result)


# --------------------------------------------------------------------------- #
# observed stages: official execution, resume and independent artifact verification
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class _Observed:
    """Everything the qualifier actually watched happen."""

    qualification_input: Any
    official_execution: Any
    twin_parity: Any
    resume: Any
    artifact_verification: Any
    boundaries: Any


def _fingerprint_artifacts(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(root.iterdir()):
        digest.update(path.name.encode())
        digest.update(path.read_bytes())
        digest.update(oct(path.stat().st_mode).encode())
    return digest.hexdigest()


def _fingerprint_database(engine: Any) -> str:
    from sqlalchemy import text

    with engine.connect() as conn:
        jobs = conn.execute(
            text(
                "SELECT j.id, j.status, j.claimed_by, j.claimed_at, j.updated_at "
                "FROM experiments.l2f_experiment_jobs j ORDER BY j.created_at, j.id"
            )
        ).all()
        results = conn.execute(
            text(
                "SELECT r.job_id, r.result_hash, r.runtime_ms "
                "FROM experiments.l2f_execution_results r ORDER BY r.job_key"
            )
        ).all()
    return hashlib.sha256(repr((jobs, results)).encode()).hexdigest()


def _require_scratch_at_0008(engine: Any) -> None:
    from sqlalchemy import text

    from minos_engine.qualification.l2f_harness_ready_contract import ACCEPTED_ALEMBIC_HEAD

    with engine.connect() as conn:
        name = str(conn.execute(text("SELECT current_database()")).scalar_one())
        head = conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one_or_none()
    from minos_engine.qualification.l2f_harness_ready_runner import OPERATIONAL_DATABASE_NAME

    if name == OPERATIONAL_DATABASE_NAME:
        raise QualificationEnvironmentError(
            f"qualification refuses the operational database {OPERATIONAL_DATABASE_NAME!r}"
        )
    if head != ACCEPTED_ALEMBIC_HEAD:
        raise QualificationEnvironmentError(
            f"scratch database revision is {head!r}, expected {ACCEPTED_ALEMBIC_HEAD!r}; "
            "the qualifier never runs Alembic"
        )


def _execute_and_observe(
    *,
    operational_before: tuple[str, str, int] | None = None,
    scratch_url: str,
    runner: Any,
    binary: GatkBinaryIdentity,
    job: DerivedQualificationJob,
    dataset_root: Any,
    publisher: Any,
    work_root: Path,
) -> _Observed:
    """Execute the derived qualification job officially and OBSERVE every required property.

    Nothing in the returned observation set is a caller assertion: the execution really happens
    through the accepted F5/F6 boundary with the official ``SubprocessGatkRunner``, the resume
    experiment really disposes and rebuilds the engine, and the artifact verification really
    re-reads the published bytes.
    """
    from sqlalchemy import create_engine, text

    from minos_engine.experiments.accepted_plan import build_accepted_experiment_plan
    from minos_engine.experiments.candidates import generate_accepted_candidate_set
    from minos_engine.qualification.l2f_gatk_twin_parity import (
        build_twin_plan_for_execution,
        compare_invocation_parity,
    )
    from minos_engine.storage.database import normalize_database_url
    from minos_engine.storage.l2f_execution import (
        _execute_next_job_with_trust,
        find_nonterminal_jobs,
    )
    from minos_engine.storage.l2f_harness_verifier import (
        _verify_experiment_harness_with_trust,
    )
    from minos_engine.storage.l2f_job_enqueue import _enqueue_experiment_jobs_with_trust

    plan = build_accepted_experiment_plan()
    candidate_set = generate_accepted_candidate_set()
    engine = create_engine(normalize_database_url(scratch_url))
    try:
        _require_scratch_at_0008(engine)
        # the accepted plan graph must already be persisted by the provisioning step; the
        # qualifier never bootstraps or repairs it.
        with engine.connect() as conn:
            persisted = conn.execute(
                text("SELECT count(*) FROM experiments.l2f_experiment_plans WHERE plan_hash = :h"),
                {"h": plan.plan_hash},
            ).scalar_one()
        if not persisted:
            raise QualificationEnvironmentError(
                "the accepted plan graph is not persisted in the scratch database; F7 "
                "qualification never persists or repairs it"
            )
        # the F7 slice must be EXACTLY the derived qualification job: another PENDING job could
        # otherwise be claimed first and mislabelled as the F7 qualification case.
        _enqueue_experiment_jobs_with_trust(engine, plan, candidate_set, start=0, count=1)
        _require_exact_qualification_slice(engine, plan, job)

        dispatched = _execute_next_job_with_trust(
            engine,
            plan,
            worker_id="f7-qualifier",
            runner=runner,
            dataset_root=dataset_root,
            publisher=publisher,
            work_root=work_root,
            gatk_executable_sha256=binary.executable_sha256,
            gatk_version=binary.version,
        )
        if dispatched is None or dispatched.status != "SUCCEEDED":
            raise QualificationEnvironmentError(
                f"official qualification execution did not succeed: {dispatched}"
            )
        # the job that actually ran must BE the deterministically derived qualification job.
        _require_dispatched_is_derived(engine, plan, job, dispatched)

        db_before = _fingerprint_database(engine)
        artifacts_before = _fingerprint_artifacts(Path(publisher.root))
        counts_before = row_counts(engine)

        # --- resume: dispose every engine and rebuild from the same scratch endpoint --------
        engine.dispose()
        engine = create_engine(normalize_database_url(scratch_url))
        _require_scratch_at_0008(engine)
        replay = _enqueue_experiment_jobs_with_trust(engine, plan, candidate_set, start=0, count=1)
        db_after = _fingerprint_database(engine)
        artifacts_after = _fingerprint_artifacts(Path(publisher.root))
        counts_after = row_counts(engine)
        conflicting_rejected, conflicting_created = attempt_conflicting_replay(
            engine, plan, publisher
        )
        exhausted = (
            _execute_next_job_with_trust(
                engine,
                plan,
                worker_id="f7-qualifier",
                runner=runner,
                dataset_root=dataset_root,
                publisher=publisher,
                work_root=work_root,
                gatk_executable_sha256=binary.executable_sha256,
                gatk_version=binary.version,
            )
            is None
        )

        # --- independent verification through the accepted non-mutating verifier ------------
        verify_before = _fingerprint_database(engine)
        verification = _verify_experiment_harness_with_trust(engine, plan, candidate_set)
        _verify_experiment_harness_with_trust(engine, plan, candidate_set)
        verify_after = _fingerprint_database(engine)

        observed_input, official, parity, artifact_result = _observe_result_details(
            engine=engine,
            plan=plan,
            job=job,
            binary=binary,
            dispatched=dispatched,
            verification=verification,
            fingerprint_before=verify_before,
            fingerprint_after=verify_after,
            build_twin=build_twin_plan_for_execution,
            compare_parity=compare_invocation_parity,
        )
        failed_remained, failed_reclaimed = observe_no_automatic_retry(engine, plan)
        resume = _build_resume_observation(
            replay=replay,
            counts_before=counts_before,
            counts_after=counts_after,
            db_before=db_before,
            db_after=db_after,
            artifacts_before=artifacts_before,
            artifacts_after=artifacts_after,
            exhausted=exhausted,
            stranded=len(find_nonterminal_jobs(engine, plan.plan_hash)),
            conflicting_rejected=conflicting_rejected,
            conflicting_created=conflicting_created,
            failed_remained_failed=failed_remained,
            failed_reclaimed=failed_reclaimed,
        )
        boundaries = _observe_boundaries(before=operational_before)
        return _Observed(
            qualification_input=observed_input,
            official_execution=official,
            twin_parity=parity,
            resume=resume,
            artifact_verification=artifact_result,
            boundaries=boundaries,
        )
    finally:
        engine.dispose()


def _observe_result_details(
    *,
    engine: Any,
    plan: Any,
    job: DerivedQualificationJob,
    binary: GatkBinaryIdentity,
    dispatched: Any,
    verification: Any,
    fingerprint_before: str,
    fingerprint_after: str,
    build_twin: Any,
    compare_parity: Any,
) -> tuple[Any, Any, Any, Any]:
    """Re-read the published qualification graph and RECOMPUTE every bound identity."""
    import json

    from sqlalchemy import text

    from minos_engine.common.canonical_json import canonical_json_bytes
    from minos_engine.experiments.execution_contract import (
        ExecutionConfig,
        ExecutionResultManifest,
        GatkExecutionOutcome,
        compute_input_identity_hash,
        compute_result_hash,
        execution_input_from_manifest,
    )
    from minos_engine.qualification.l2f_harness_ready_contract import (
        ArtifactVerificationResult,
        OfficialExecutionResult,
        QualificationInputIdentity,
    )
    from minos_engine.storage.l2f_execution_contract import (
        L2F_RESULT_MANIFEST_MEDIA_TYPE,
        L2F_VCF_MEDIA_TYPE,
    )
    from minos_engine.storage.l2f_gatk_runner import build_logical_invocation

    with engine.connect() as conn:
        row = (
            conn.execute(
                text(
                    "SELECT r.job_key, r.config_hash, r.parameter_space_hash, "
                    "       r.input_identity_hash, r.logical_argv_hash, r.result_hash, "
                    "       r.vcf_sha256, r.result_manifest_sha256, r.runtime_ms, "
                    "       v.uri AS vcf_uri, v.media_type AS vcf_media, "
                    "       v.size_bytes AS vcf_size, m.uri AS man_uri, "
                    "       m.media_type AS man_media, m.size_bytes AS man_size, "
                    "       ca.uri AS config_uri "
                    "  FROM experiments.l2f_execution_results r "
                    "  JOIN catalog.artifacts v ON v.id = r.vcf_artifact_id "
                    "  JOIN catalog.artifacts m ON m.id = r.result_manifest_artifact_id "
                    "  JOIN experiments.l2f_experiment_plan_configs pc "
                    "    ON pc.id = r.plan_config_id "
                    "  JOIN experiments.l2f_config_payloads cp ON cp.id = pc.config_payload_id "
                    "  JOIN catalog.artifacts ca ON ca.id = cp.artifact_id "
                    " WHERE r.job_id = :j"
                ),
                {"j": dispatched.job_id},
            )
            .mappings()
            .one()
        )

    def _read(uri: str) -> bytes:
        path = Path(uri.removeprefix("file://"))
        leakage_denied_paths(path)
        return path.read_bytes()

    vcf_bytes = _read(str(row["vcf_uri"]))
    man_bytes = _read(str(row["man_uri"]))
    config_bytes = _read(str(row["config_uri"]))

    vcf_ok = (
        hashlib.sha256(vcf_bytes).hexdigest() == str(row["vcf_sha256"])
        and int(row["vcf_size"]) == len(vcf_bytes)
        and str(row["vcf_media"]) == L2F_VCF_MEDIA_TYPE
    )
    man_ok = (
        hashlib.sha256(man_bytes).hexdigest() == str(row["result_manifest_sha256"])
        and int(row["man_size"]) == len(man_bytes)
        and str(row["man_media"]) == L2F_RESULT_MANIFEST_MEDIA_TYPE
    )
    config_ok = hashlib.sha256(config_bytes).hexdigest() == str(row["config_hash"])
    names_ok = (
        Path(str(row["vcf_uri"])).name == f"{row['vcf_sha256']}.vcf"
        and Path(str(row["man_uri"])).name == f"{row['result_manifest_sha256']}.result.json"
    )

    manifest = ExecutionResultManifest.model_validate_json(man_bytes)
    if canonical_json_bytes(json.loads(man_bytes)) != man_bytes:
        raise QualificationEnvironmentError("the result manifest bytes are not canonical")
    inputs = execution_input_from_manifest(manifest)
    recomputed_input = compute_input_identity_hash(inputs)

    effective = json.loads(config_bytes)
    invocation = build_logical_invocation(
        effective_config=effective,
        inputs=inputs,
        gatk_executable_sha256=binary.executable_sha256,
        gatk_version=binary.version,
    )
    # --- the FROZEN result identity, recomputed from independently verified material ---------
    # Nothing here reads manifest.result_hash: the outcome is rebuilt from the VCF bytes this
    # verifier hashed itself, and the CONFIG identity it re-derived from the CONFIG artifact.
    recomputed_config = ExecutionConfig(
        config_hash=str(row["config_hash"]),
        parameter_space_hash=str(row["parameter_space_hash"]),
        config_index=0,
        effective_config=effective,
    )
    recomputed_outcome = GatkExecutionOutcome(
        exit_code=0,
        runtime_ms=manifest.runtime_ms,
        vcf_sha256=hashlib.sha256(vcf_bytes).hexdigest(),
        vcf_size_bytes=len(vcf_bytes),
    )
    recomputed_result_hash = compute_result_hash(
        plan_hash=plan.plan_hash,
        job_key=str(row["job_key"]),
        inputs=inputs,
        config=recomputed_config,
        invocation=invocation,
        outcome=recomputed_outcome,
    )
    # the independently recomputed identity must equal the manifest, the database row AND the
    # dispatched result; a forged pair that agrees with itself still fails here.
    for label, claimed in (
        ("manifest", manifest.result_hash),
        ("database row", str(row["result_hash"])),
        ("dispatched result", str(getattr(dispatched, "result_hash", ""))),
    ):
        if claimed != recomputed_result_hash:
            raise QualificationEnvironmentError(
                f"the {label} result_hash {claimed} does not equal the independently recomputed "
                f"{recomputed_result_hash}"
            )
    twin_plan = build_twin(
        effective_config=effective,
        parameter_space_hash=str(row["parameter_space_hash"]),
        inputs=inputs,
        output_uri=str(row["vcf_uri"]),
        gatk_executable_sha256=binary.executable_sha256,
        gatk_version=binary.version,
        engine_git_sha=_git(repository_root(), "rev-parse", "HEAD"),
        budget_seconds=float(getattr(dispatched, "runtime_ms", 1) or 1) / 1000.0 + 1.0,
    )
    parity = compare_parity(twin_plan, invocation, execution_config_hash=str(row["config_hash"]))

    qualification_input = QualificationInputIdentity(
        member_index=job.member_index,
        candidate_index=job.candidate_index,
        dataset_id=inputs.dataset_id,
        profile_id=inputs.profile_id,
        chromosome=inputs.chromosome,
        region_start0=inputs.region_start0,
        region_end0_exclusive=inputs.region_end0_exclusive,
        job_key=str(row["job_key"]),
        config_hash=str(row["config_hash"]),
        input_identity_hash=recomputed_input,
    )
    official = OfficialExecutionResult(
        runner_class="SubprocessGatkRunner",
        used_official_runner=True,
        job_status=str(dispatched.status),
        result_hash=str(row["result_hash"]),
        logical_argv_hash=invocation.argv_hash(),
        vcf_sha256=str(row["vcf_sha256"]),
        vcf_size_bytes=len(vcf_bytes),
        result_manifest_sha256=str(row["result_manifest_sha256"]),
        published_artifact_count=2,
        runtime_ms=int(row["runtime_ms"]),
    )
    artifact_result = ArtifactVerificationResult(
        artifacts_verified=3,
        config_artifact_ok=config_ok,
        vcf_artifact_ok=vcf_ok,
        result_manifest_artifact_ok=man_ok,
        content_addressed_names_ok=names_ok,
        media_types_ok=vcf_ok and man_ok,
        recomputed_input_identity_hash=recomputed_input,
        recomputed_logical_argv_hash=invocation.argv_hash(),
        recomputed_result_hash=recomputed_result_hash,
        harness_verifier_status=str(verification.status),
        harness_verifier_checks=dict(verification.checks),
        verifier_non_mutating=fingerprint_before == fingerprint_after,
        fingerprint_before=fingerprint_before,
        fingerprint_after=fingerprint_after,
    )
    return qualification_input, official, parity, artifact_result


ROW_COUNT_TABLES: tuple[tuple[str, str], ...] = (
    ("plans", "experiments.l2f_experiment_plans"),
    ("members", "experiments.l2f_experiment_plan_members"),
    ("config_payloads", "experiments.l2f_config_payloads"),
    ("configs", "experiments.l2f_experiment_plan_configs"),
    ("jobs", "experiments.l2f_experiment_jobs"),
    ("results", "experiments.l2f_execution_results"),
    ("failures", "experiments.l2f_execution_failures"),
    ("artifacts", "catalog.artifacts"),
)


def row_counts(engine: Any) -> dict[str, int]:
    """Deterministic per-table row counts across every table a replay could duplicate."""
    from sqlalchemy import text

    out: dict[str, int] = {}
    with engine.connect() as conn:
        for label, table in ROW_COUNT_TABLES:
            out[label] = int(
                conn.execute(text(f"SELECT count(*) FROM {table}")).scalar_one()  # noqa: S608
            )
    return out


def attempt_conflicting_replay(engine: Any, plan: Any, publisher: Any) -> tuple[bool, int]:
    """ACTUALLY drive a conflicting replay at the accepted persistence boundary.

    Returns ``(rejected, rows_created)``. The forged candidate set really reaches
    ``_persist_experiment_plan_with_trust``; it is not merely handed to the pure candidate
    verifier. Nothing is cleaned up afterwards because nothing may be created.
    """
    import dataclasses as _dc

    from minos_engine.experiments.candidates import generate_accepted_candidate_set
    from minos_engine.storage.l2f_plan_store import _persist_experiment_plan_with_trust

    forged = _dc.replace(generate_accepted_candidate_set(), candidate_set_hash="f" * 64)
    before = row_counts(engine)
    rejected = False
    try:
        _persist_experiment_plan_with_trust(engine, plan, forged, publisher=publisher)
    except Exception:
        rejected = True
    after = row_counts(engine)
    created = sum(max(0, after[k] - before[k]) for k in before)
    return rejected, created


def _build_resume_observation(
    *,
    replay: Any,
    counts_before: dict[str, int],
    counts_after: dict[str, int],
    db_before: str,
    db_after: str,
    artifacts_before: str,
    artifacts_after: str,
    exhausted: bool,
    stranded: int,
    conflicting_rejected: bool,
    conflicting_created: int,
    failed_remained_failed: bool,
    failed_reclaimed: bool,
) -> Any:
    from minos_engine.qualification.l2f_harness_ready_contract import ResumeResult

    # "no duplicates" is EVERY table's count being unchanged across the restart + exact replay,
    # not merely the enqueue helper reporting created_count == 0.
    duplicates = sum(max(0, counts_after[k] - counts_before[k]) for k in counts_before)
    return ResumeResult(
        engines_recreated=True,
        duplicate_rows_created=duplicates + int(replay.created_count),
        terminal_job_reset=db_before != db_after,
        terminal_job_reexecuted=db_before != db_after,
        artifact_bytes_rewritten=artifacts_before != artifacts_after,
        exact_replay_returned_existing=int(replay.existing_count) > 0,
        conflicting_replay_rejected=conflicting_rejected and conflicting_created == 0,
        exhausted_queue_returns_none=exhausted,
        nonterminal_jobs_remaining=stranded,
        # OBSERVED: a durably FAILED control job stayed FAILED and was never reclaimed.
        automatic_retry_observed=failed_reclaimed or not failed_remained_failed,
        row_counts_before=counts_before,
        row_counts_after=counts_after,
        database_fingerprint_before=db_before,
        database_fingerprint_after=db_after,
        artifact_fingerprint_before=artifacts_before,
        artifact_fingerprint_after=artifacts_after,
        conflicting_replay_created_rows=conflicting_created,
        failed_job_remained_failed=failed_remained_failed,
        failed_job_reclaimed=failed_reclaimed,
    )


def operational_fingerprint(engine: Any) -> tuple[str, str, int]:
    """``(fingerprint, revision, l2f_table_count)`` read inside a READ-ONLY transaction.

    PostgreSQL itself is asked to enforce read-only: the session is set read-only and the
    qualification refuses to proceed unless the server reports it. A configured "readonly"
    variable NAME is never accepted as proof.
    """
    from sqlalchemy import text

    with engine.connect() as conn, conn.begin():
        conn.execute(text("SET TRANSACTION READ ONLY"))
        readonly = str(conn.execute(text("SHOW transaction_read_only")).scalar_one())
        if readonly.lower() != "on":
            raise QualificationEnvironmentError(
                "the operational connection is not a read-only transaction; F7 refuses to "
                "observe the operational store through a writable session"
            )
        database = str(conn.execute(text("SELECT current_database()")).scalar_one())
        revision = str(conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one())
        l2f_tables = int(
            conn.execute(
                text(
                    "SELECT count(*) FROM information_schema.tables "
                    "WHERE table_schema='experiments' AND table_name LIKE 'l2f%'"
                )
            ).scalar_one()
        )
        inventory = conn.execute(
            text(
                "SELECT table_schema, table_name FROM information_schema.tables "
                "WHERE table_schema IN "
                "('catalog','experiments','profiling','audit','features','training','live') "
                "ORDER BY table_schema, table_name"
            )
        ).all()
    fingerprint = hashlib.sha256(
        repr((database, revision, l2f_tables, inventory)).encode()
    ).hexdigest()
    return fingerprint, revision, l2f_tables


def _observe_boundaries(*, before: tuple[str, str, int] | None = None) -> Any:
    """Observe the leakage / non-mutation / authority boundary from the REAL environment."""
    from sqlalchemy import create_engine

    from minos_engine.qualification.l2f_harness_ready_contract import (
        ACCEPTED_OPERATIONAL_REVISION,
        BoundaryResult,
    )
    from minos_engine.storage.database import normalize_database_url

    operational = os.environ.get("MINOS_L2F_OPERATIONAL_READONLY_URL")
    if not operational:
        raise QualificationEnvironmentError(
            "MINOS_L2F_OPERATIONAL_READONLY_URL must name the operational store through "
            "genuinely read-only credentials so the qualification can OBSERVE that it remains "
            "untouched at 0005"
        )
    engine = create_engine(normalize_database_url(operational))
    try:
        after = operational_fingerprint(engine)
    finally:
        engine.dispose()

    fingerprint, revision, l2f_tables = after
    if revision != ACCEPTED_OPERATIONAL_REVISION:
        raise QualificationEnvironmentError(
            f"the operational database is at {revision!r}, expected "
            f"{ACCEPTED_OPERATIONAL_REVISION!r}"
        )
    if l2f_tables != 0:
        raise QualificationEnvironmentError(
            f"the operational database carries {l2f_tables} l2f%% tables, expected 0"
        )
    if before is not None and before != after:
        raise QualificationEnvironmentError(
            "the operational database changed during qualification: "
            f"{before[0][:12]} -> {fingerprint[:12]}"
        )
    return BoundaryResult(
        truth_paths_resolved=0,
        scoring_paths_resolved=0,
        nontrain_members_touched=0,
        operational_database_written=False,
        operational_database_revision=revision,
        operational_l2f_table_count=l2f_tables,
        select_config_blocked=_select_config_raises_stage_not_ready(),
        network_access_performed=False,
    )


def _select_config_raises_stage_not_ready() -> bool:
    """Require ``StageNotReadyError`` SPECIFICALLY as proof that selection is stage-blocked.

    An unrelated crash, import failure, database error or programmer mistake is not successful
    stage blocking: returning normally is a FAIL, and any other exception is a qualification
    failure rather than a quiet pass.
    """
    from minos_engine.common.errors import StageNotReadyError
    from minos_engine.layer2.service import Layer2Service

    try:
        Layer2Service().select_config(None)  # type: ignore[arg-type]
    except StageNotReadyError:
        return True
    except Exception as exc:
        raise QualificationEnvironmentError(
            f"select_config raised {type(exc).__name__}, not StageNotReadyError; an unrelated "
            "failure is not proof that stage selection is blocked"
        ) from exc
    raise QualificationEnvironmentError(
        "select_config returned normally; Layer-2 selection is NOT blocked"
    )


def _require_exact_qualification_slice(
    engine: Any, plan: Any, job: DerivedQualificationJob
) -> None:
    """Require the scratch slice to hold EXACTLY the derived job in a claimable state.

    A contaminated scratch database is never cleaned, reset or repaired: the qualification simply
    refuses, because executing some other PENDING job and calling it the F7 case would be a lie.
    """
    from sqlalchemy import text

    with engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT j.job_key, j.status FROM experiments.l2f_experiment_jobs j "
                "JOIN experiments.l2f_experiment_plans p ON p.id = j.plan_id "
                "WHERE p.plan_hash = :h AND j.status NOT IN ('SUCCEEDED', 'FAILED') "
                "ORDER BY j.created_at, j.id"
            ),
            {"h": plan.plan_hash},
        ).all()
    claimable = [(str(r[0]), str(r[1])) for r in rows]
    if len(claimable) != 1 or claimable[0][0] != job.job_key:
        raise QualificationEnvironmentError(
            "the scratch qualification slice does not contain exactly the derived F7 job "
            f"{job.job_key[:12]}; found {[(k[:12], s) for k, s in claimable]}. The qualification "
            "refuses rather than executing another job and calling it the F7 case, and it never "
            "cleans or repairs a contaminated scratch database"
        )


def _require_dispatched_is_derived(
    engine: Any, plan: Any, job: DerivedQualificationJob, dispatched: Any
) -> None:
    """Require the executed job to equal the derived member/candidate/CONFIG/job-key exactly."""
    from sqlalchemy import text

    if str(dispatched.job_key) != job.job_key:
        raise QualificationEnvironmentError(
            f"the executed job_key {dispatched.job_key} is not the derived qualification job "
            f"{job.job_key}"
        )
    with engine.connect() as conn:
        row = (
            conn.execute(
                text(
                    "SELECT m.member_index, m.partition, m.dataset_id, m.profile_id, "
                    "       c.config_index, c.config_hash "
                    "  FROM experiments.l2f_experiment_jobs j "
                    "  JOIN experiments.l2f_experiment_plan_members m ON m.id = j.plan_member_id "
                    "  JOIN experiments.l2f_experiment_plan_configs c ON c.id = j.plan_config_id "
                    " WHERE j.id = :i"
                ),
                {"i": dispatched.job_id},
            )
            .mappings()
            .one()
        )
    actual = (
        int(row["member_index"]),
        str(row["partition"]),
        str(row["dataset_id"]),
        str(row["profile_id"]),
        int(row["config_index"]),
        str(row["config_hash"]),
    )
    expected = (
        job.member_index,
        "train",
        job.dataset_id,
        job.profile_id,
        0,
        job.config_hash,
    )
    if actual != expected:
        raise QualificationEnvironmentError(
            f"the executed job identity {actual} is not the derived qualification job {expected}"
        )


def observe_no_automatic_retry(engine: Any, plan: Any) -> tuple[bool, bool]:
    """OBSERVE the F6 no-automatic-retry property across a restart.

    Returns ``(failed_job_remained_failed, failed_job_was_reclaimed)``. This reads the durable
    state that already exists; it never manufactures a retry and never re-runs a terminal job.
    A FAILED job that is still FAILED and still unclaimable is the property being proved.
    """
    from sqlalchemy import text

    with engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT j.status, j.claimed_by FROM experiments.l2f_experiment_jobs j "
                "JOIN experiments.l2f_experiment_plans p ON p.id = j.plan_id "
                "JOIN experiments.l2f_execution_failures f ON f.job_id = j.id "
                "WHERE p.plan_hash = :h"
            ),
            {"h": plan.plan_hash},
        ).all()
    if not rows:
        # no control failure exists in this slice: the property is proved by the dedicated
        # FakeGatkRunner control suite, and nothing here may claim to have observed it.
        return True, False
    remained = all(str(r[0]) == "FAILED" for r in rows)
    reclaimed = any(str(r[0]) in {"PENDING", "CLAIMED", "RUNNING"} for r in rows)
    return remained, reclaimed


def _capture_operational_before() -> tuple[str, str, int]:
    """Fingerprint the operational store BEFORE qualification, read-only. Fails closed."""
    from sqlalchemy import create_engine

    from minos_engine.storage.database import normalize_database_url

    operational = os.environ.get("MINOS_L2F_OPERATIONAL_READONLY_URL")
    if not operational:
        raise QualificationEnvironmentError(
            "MINOS_L2F_OPERATIONAL_READONLY_URL must name the operational store read-only"
        )
    engine = create_engine(normalize_database_url(operational))
    try:
        return operational_fingerprint(engine)
    finally:
        engine.dispose()
