"""THE production path that evaluates one completed TRAIN execution.

Everything else in this package is a component; this is the single authoritative sequence that
connects them, so "the parts exist" can never be mistaken for "the pipeline exists":

    resolve execution identity from PostgreSQL   (never from the caller)
        -> refuse anything that is not TRAIN     (before any truth path is constructed)
        -> verify the execution's VCF bytes      (against the recorded digest)
        -> resolve + verify TRAIN truth bytes    (against the registered identity)
        -> run hap.py through HappyRunner        (production: digest-pinned container)
        -> parse the audited metric families
        -> score under ONE ScoringAuthority
        -> publish the canonical metrics document (content-addressed, atomic, no-clobber)
        -> register it through the narrow 0010 registrar
        -> build ONE EvaluationRecord and persist it

Design rules that are load-bearing rather than stylistic:

* **Input authority.** The caller supplies an ``execution_result_id`` and provisioning paths.
  Dataset, partition, round, VCF digest and truth digests are READ from the database. An
  operator cannot hand in a partition or a truth identity.
* **TRAIN gate first.** The partition check happens immediately after the execution row is
  resolved — before truth paths are constructed, opened or hashed — so a validation/test
  execution produces no truth filesystem access at all.
* **Failures are durable.** Once a valid TRAIN execution identity exists, a terminal
  infrastructure error is recorded in the failure ledger with a bounded code. A missing row is
  not an acceptable representation of "this evaluation failed": baseline statistics computed
  over silently-absent failures would overstate a configuration's robustness.
* **Crash convergence.** Every step is get-or-create against content identity, so a crashed
  evaluation can be replayed and converges on the same artifact, the same catalog row and the
  same evaluation. A published document is never deleted because a later database step failed —
  another evaluation may legitimately share those exact bytes.
"""

from __future__ import annotations

import hashlib
import os
import stat
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal
from urllib.parse import unquote, urlparse

from minos_engine.common.errors import MinosEngineError
from minos_engine.evaluation.artifact_publisher import EvaluationArtifactPublisher
from minos_engine.evaluation.contracts import (
    ComparisonScope,
    EvaluationInputs,
    TruthIdentity,
    build_metrics_artifact_bytes,
)
from minos_engine.evaluation.evaluator import (
    EvaluationPersistError,
    EvaluationPublishError,
    EvaluationRecordError,
    PersistedEvaluation,
    build_evaluation_record,
    evaluate_metrics,
    record_evaluation_failure,
    record_evaluation_result,
    register_metrics_artifact,
)
from minos_engine.evaluation.happy_metrics import parse_happy_outputs
from minos_engine.evaluation.happy_runner import (
    HappyContainmentError,
    HappyExecutionError,
    HappyOutputError,
    HappyRunner,
    HappyTimeoutError,
)
from minos_engine.evaluation.minos_score import ScoreComputationError
from minos_engine.evaluation.scoring_contract import ScoringAuthority, compute_scoring_contract_hash
from minos_engine.evaluation.truth_registration import (
    TruthRegistrationError,
    hash_truth_bundle,
    refuse_non_train_partition,
)
from minos_engine.storage.attempt_workspace import (
    create_attempt_workspace,
    remove_attempt_workspace,
)

__all__ = [
    "DualTerminalOutcomeError",
    "EvaluationOutcome",
    "EvaluationProvisioning",
    "EvaluationWorkspaceError",
    "OrchestrationError",
    "UnknownExecutionError",
    "evaluate_execution",
]

_CHUNK = 1024 * 1024


class OrchestrationError(MinosEngineError):
    """The evaluation could not be started against a valid execution identity."""


class UnknownExecutionError(OrchestrationError):
    """No completed execution with that id is visible through the evaluator projection."""


class EvaluationWorkspaceError(OrchestrationError):
    """A fresh per-attempt hap.py workspace could not be created and proven private."""


class DualTerminalOutcomeError(OrchestrationError):
    """The ledger presents BOTH a success and a failure for one (execution, contract).

    Migration 0010 makes this unreachable through the write path; observing it anyway means the
    ledger is corrupt, so the evaluator refuses to proceed rather than picking one.
    """


@dataclass(frozen=True)
class EvaluationProvisioning:
    """Runtime provisioning — paths only. None of this enters any scientific identity.

    ``work_dir`` is the evaluation work **root**, not the directory hap.py writes into: every
    actual run gets a fresh private attempt directory beneath it.
    """

    practice_dataset_root: Path
    reference: Path
    region_bed: Path
    work_dir: Path


@dataclass(frozen=True)
class EvaluationOutcome:
    """What one orchestrated evaluation durably produced."""

    execution_result_id: str
    status: Literal["EVALUATED", "FAILED"]
    scoring_contract_hash: str
    persisted: PersistedEvaluation | None = None
    metrics_artifact_id: str | None = None
    metrics_artifact_sha256: str | None = None
    failure_code: str | None = None
    failure_id: str | None = None


def _sha256_regular_file(path: Path, *, label: str) -> str:
    """Hash a file, refusing symlinks and anything that is not a regular file.

    ``O_NOFOLLOW`` means a symlink planted at the expected name cannot redirect the read, and the
    size/inode re-check refuses a file swapped underneath us mid-hash.
    """
    if path.is_symlink():
        raise OrchestrationError(f"{label} {path} is a symlink")
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError as exc:
        raise OrchestrationError(f"{label} {path} is unreadable: {exc}") from exc
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode):
            raise OrchestrationError(f"{label} {path} is not a regular file")
        digest = hashlib.sha256()
        size = 0
        while chunk := os.read(fd, _CHUNK):
            digest.update(chunk)
            size += len(chunk)
        after = os.fstat(fd)
    finally:
        os.close(fd)
    if (after.st_size, after.st_ino) != (before.st_size, before.st_ino) or size != after.st_size:
        raise OrchestrationError(f"{label} {path} changed while it was being hashed")
    return digest.hexdigest()


def _local_path_from_uri(uri: str) -> Path:
    """Resolve a supported local artifact URI. Anything else is refused, never fetched."""
    parsed = urlparse(uri)
    if parsed.scheme != "file" or parsed.netloc not in ("", "localhost"):
        raise OrchestrationError(f"unsupported artifact URI {uri!r}; only local file:// is read")
    path = Path(unquote(parsed.path))
    if not path.is_absolute():
        raise OrchestrationError(f"artifact URI {uri!r} does not name an absolute path")
    return path


def _resolve_execution(engine: Any, execution_result_id: str) -> dict[str, Any]:
    """Read the execution's identity from the narrow evaluator projection."""
    from sqlalchemy import text

    with engine.connect() as conn:
        row = (
            conn.execute(
                text(
                    "SELECT execution_result_id, execution_result_hash, dataset_registry_id, "
                    "       partition, dataset_id, round_id, chromosome, region_start0, "
                    "       region_end0_exclusive, vcf_artifact_id, vcf_sha256, vcf_uri "
                    "  FROM evaluation.l2f_completed_execution_inputs "
                    " WHERE execution_result_id = :i"
                ),
                {"i": execution_result_id},
            )
            .mappings()
            .one_or_none()
        )
    if row is None:
        raise UnknownExecutionError(
            f"no completed execution {execution_result_id} is visible to the evaluator"
        )
    return dict(row)


def _resolve_truth_identity(engine: Any, dataset_registry_id: str) -> dict[str, Any] | None:
    from sqlalchemy import text

    with engine.connect() as conn:
        row = (
            conn.execute(
                text(
                    "SELECT truth_vcf_sha256, truth_tbi_sha256, mutations_vcf_sha256, "
                    "       mutations_tbi_sha256 "
                    "  FROM evaluation.dataset_evaluation_identity WHERE dataset_registry_id = :d"
                ),
                {"d": dataset_registry_id},
            )
            .mappings()
            .one_or_none()
        )
    return dict(row) if row is not None else None


def _terminal_outcome(
    engine: Any, execution_result_id: str, contract_hash: str
) -> EvaluationOutcome | None:
    """Read this evaluation's OWN terminal state through the evaluator's granted projections.

    Evaluation outcomes are immutable and mutually exclusive, so a terminal row means the work is
    already done: re-running hap.py could not change the answer and would cost a full container
    execution per replay. ``minos_evaluator`` holds SELECT on both ledgers, so no admin authority
    is involved.
    """
    from sqlalchemy import text

    with engine.connect() as conn:
        success = (
            conn.execute(
                text(
                    "SELECT id, evaluation_hash, metrics_artifact_id, metrics_artifact_sha256 "
                    "  FROM evaluation.l2f_evaluation_results "
                    " WHERE execution_result_id = :e AND scoring_contract_hash = :c"
                ),
                {"e": execution_result_id, "c": contract_hash},
            )
            .mappings()
            .one_or_none()
        )
        failure = (
            conn.execute(
                text(
                    "SELECT id, failure_code FROM evaluation.l2f_evaluation_failures "
                    " WHERE execution_result_id = :e AND scoring_contract_hash = :c"
                ),
                {"e": execution_result_id, "c": contract_hash},
            )
            .mappings()
            .one_or_none()
        )

    if success is not None and failure is not None:
        raise DualTerminalOutcomeError(
            f"execution {execution_result_id} presents BOTH a success and a failure under one "
            "scoring contract; the evaluation ledger is corrupt"
        )
    if success is not None:
        return EvaluationOutcome(
            execution_result_id=execution_result_id,
            status="EVALUATED",
            scoring_contract_hash=contract_hash,
            persisted=PersistedEvaluation(
                evaluation_id=str(success["id"]),
                evaluation_hash=str(success["evaluation_hash"]),
                created=False,
            ),
            metrics_artifact_id=str(success["metrics_artifact_id"]),
            metrics_artifact_sha256=str(success["metrics_artifact_sha256"]),
        )
    if failure is not None:
        return EvaluationOutcome(
            execution_result_id=execution_result_id,
            status="FAILED",
            scoring_contract_hash=contract_hash,
            failure_code=str(failure["failure_code"]),
            failure_id=str(failure["id"]),
        )
    return None


def evaluate_execution(
    engine: Any,
    *,
    execution_result_id: str,
    authority: ScoringAuthority,
    happy_runner: HappyRunner,
    publisher: EvaluationArtifactPublisher,
    provisioning: EvaluationProvisioning,
) -> EvaluationOutcome:
    """Evaluate ONE completed TRAIN execution end to end. See the module docstring for the chain.

    Returns an outcome describing what was durably recorded. Raises only when no valid TRAIN
    execution identity exists — an unknown execution, or a partition this stage must not touch.
    """
    contract_hash = compute_scoring_contract_hash(authority)
    execution = _resolve_execution(engine, execution_result_id)

    # THE partition gate: before any truth path is constructed, opened or hashed.
    refuse_non_train_partition(str(execution["partition"]))

    # An already-terminal evaluation is never re-executed: no truth hashing, no container, no
    # republication. A crash BEFORE a durable terminal row is a different case entirely and does
    # get a fresh attempt below.
    already = _terminal_outcome(engine, execution_result_id, contract_hash)
    if already is not None:
        return already

    def _fail(
        code: str, *, exit_code: int | None = None, stderr: str | None = None
    ) -> EvaluationOutcome:
        failure_id, _created = record_evaluation_failure(
            engine,
            execution_result_id=execution_result_id,
            scoring_contract_hash=contract_hash,
            failure_code=code,
            tool_exit_code=exit_code,
            stderr_sha256=stderr,
        )
        return EvaluationOutcome(
            execution_result_id=execution_result_id,
            status="FAILED",
            scoring_contract_hash=contract_hash,
            failure_code=code,
            failure_id=failure_id,
        )

    # 1. the execution's own VCF must still be the bytes the execution ledger recorded.
    try:
        vcf_path = _local_path_from_uri(str(execution["vcf_uri"]))
        observed_vcf_sha = _sha256_regular_file(vcf_path, label="execution VCF")
    except OrchestrationError:
        return _fail("VCF_BYTES_MISMATCH")
    if observed_vcf_sha != str(execution["vcf_sha256"]):
        return _fail("VCF_BYTES_MISMATCH")

    # 2. truth identity must already be registered for this dataset.
    identity = _resolve_truth_identity(engine, str(execution["dataset_registry_id"]))
    if identity is None:
        return _fail("TRUTH_IDENTITY_MISSING")

    # 3. the truth bytes on disk must be exactly the registered bytes.
    try:
        bundle = hash_truth_bundle(
            dataset_registry_id=str(execution["dataset_registry_id"]),
            dataset_id=str(execution["dataset_id"]),
            round_id=str(execution["round_id"]),
            dataset_root=provisioning.practice_dataset_root,
        )
    except TruthRegistrationError:
        return _fail("TRUTH_BYTES_MISMATCH")
    truth = TruthIdentity(
        truth_vcf_sha256=bundle.truth_vcf_sha256,
        truth_tbi_sha256=bundle.truth_tbi_sha256,
        mutations_vcf_sha256=bundle.mutations_vcf_sha256,
        mutations_tbi_sha256=bundle.mutations_tbi_sha256,
    )
    registered = TruthIdentity(
        truth_vcf_sha256=str(identity["truth_vcf_sha256"]),
        truth_tbi_sha256=str(identity["truth_tbi_sha256"]),
        mutations_vcf_sha256=str(identity["mutations_vcf_sha256"]),
        mutations_tbi_sha256=str(identity["mutations_tbi_sha256"]),
    )
    if truth != registered:
        return _fail("TRUTH_BYTES_MISMATCH")

    truth_dir = provisioning.practice_dataset_root / f"round_{execution['round_id']}"

    # 4. a FRESH private attempt directory. Reusing one namespace per execution meant a crashed
    # or timed-out attempt could leave partial hap.py output that a later retry would read as if
    # it were its own; a new attempt inode per invocation makes that impossible.
    try:
        attempt = create_attempt_workspace(
            provisioning.work_dir,
            name=f"eval-{execution_result_id}-{contract_hash[:8]}-{uuid.uuid4().hex}",
            error=EvaluationWorkspaceError,
        )
    except EvaluationWorkspaceError:
        return _fail("EVALUATION_ERROR")

    try:
        return _evaluate_in_attempt(
            engine,
            execution=execution,
            execution_result_id=execution_result_id,
            contract_hash=contract_hash,
            authority=authority,
            happy_runner=happy_runner,
            publisher=publisher,
            provisioning=provisioning,
            truth=truth,
            truth_dir=truth_dir,
            vcf_path=vcf_path,
            observed_vcf_sha=observed_vcf_sha,
            attempt_dir=attempt.path,
            fail=_fail,
        )
    finally:
        # raw hap.py output is a runtime intermediate, never scientific evidence: the published
        # metrics document and the ledger rows are what survive. Removal goes through the
        # retained descriptor, so only the inode this call created is ever removed.
        remove_attempt_workspace(attempt)


def _evaluate_in_attempt(
    engine: Any,
    *,
    execution: dict[str, Any],
    execution_result_id: str,
    contract_hash: str,
    authority: ScoringAuthority,
    happy_runner: HappyRunner,
    publisher: EvaluationArtifactPublisher,
    provisioning: EvaluationProvisioning,
    truth: TruthIdentity,
    truth_dir: Path,
    vcf_path: Path,
    observed_vcf_sha: str,
    attempt_dir: Path,
    fail: Callable[..., EvaluationOutcome],
) -> EvaluationOutcome:
    """Everything that reads or writes hap.py output, confined to ONE fresh attempt directory."""
    _fail = fail
    output_prefix = attempt_dir / "happy"

    # 5. hap.py, through the runner boundary only.
    try:
        outcome = happy_runner.run(
            truth_vcf=truth_dir / "truth.vcf.gz",
            query_vcf=vcf_path,
            reference=provisioning.reference,
            region_bed=provisioning.region_bed,
            output_prefix=output_prefix,
            work_dir=attempt_dir,
        )
    except HappyContainmentError:
        # the container could not be PROVEN stopped: never reported as an ordinary timeout.
        return _fail("EVALUATION_ERROR")
    except HappyTimeoutError:
        return _fail("HAPPY_TIMEOUT")
    except HappyExecutionError:
        return _fail("HAPPY_NONZERO_EXIT")

    # 6. parse every metric family the scoring contract needs.
    try:
        parsed = parse_happy_outputs(output_prefix, truth_dir / "mutations.vcf.gz")
    except HappyOutputError:
        return _fail("HAPPY_OUTPUT_INVALID", exit_code=outcome.exit_code)

    inputs = EvaluationInputs(
        execution_result_hash=str(execution["execution_result_hash"]),
        dataset_id=str(execution["dataset_id"]),
        partition=str(execution["partition"]),
        vcf_sha256=observed_vcf_sha,
        truth=truth,
        scope=ComparisonScope(
            chromosome=str(execution["chromosome"]),
            region_start0=int(execution["region_start0"]),
            region_end0_exclusive=int(execution["region_end0_exclusive"]),
            region_source=(
                f"{execution['chromosome']}:{int(execution['region_start0']) + 1}"
                f"-{int(execution['region_end0_exclusive'])}"
            ),
        ),
    )

    # 7. score under exactly one authority.
    try:
        artifact, breakdown, admission, _hash = evaluate_metrics(
            inputs=inputs,
            happy_metrics=parsed.happy_metrics,
            mutation_only_metrics=parsed.mutation_only_metrics,
            assessed_only_metrics=parsed.assessed_only_metrics,
            overcall=parsed.overcall,
            authority=authority,
        )
    except ScoreComputationError:
        return _fail("SCORER_OUTPUT_INVALID", exit_code=outcome.exit_code)

    # 8. publish the canonical document, then register it through the narrow registrar.
    try:
        published = publisher.publish(build_metrics_artifact_bytes(artifact))
    except EvaluationPublishError:
        return _fail("ARTIFACT_PUBLISH_FAILED", exit_code=outcome.exit_code)

    try:
        artifact_id, _created = register_metrics_artifact(engine, published)
        record = build_evaluation_record(
            execution_result_id=execution_result_id,
            inputs=inputs,
            artifact=artifact,
            breakdown=breakdown,
            admission_code=admission,
            authority=authority,
            metrics_artifact_id=artifact_id,
            metrics=published,
        )
        persisted = record_evaluation_result(engine, record)
    except (EvaluationRecordError, EvaluationPersistError):
        raise
    except Exception:
        # the published document is deliberately NOT unpublished: it is content-addressed and
        # another evaluation may legitimately reuse those exact bytes.
        return _fail("EVALUATION_ERROR", exit_code=outcome.exit_code)

    return EvaluationOutcome(
        execution_result_id=execution_result_id,
        status="EVALUATED",
        scoring_contract_hash=contract_hash,
        persisted=persisted,
        metrics_artifact_id=artifact_id,
        metrics_artifact_sha256=published.sha256,
    )
