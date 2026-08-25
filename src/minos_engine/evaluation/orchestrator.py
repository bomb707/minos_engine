"""THE production path that evaluates one completed TRAIN execution.

Everything else in this package is a component; this is the single authoritative sequence that
connects them, so "the parts exist" can never be mistaken for "the pipeline exists":

    resolve execution identity from PostgreSQL   (never from the caller)
        -> refuse anything that is not TRAIN     (before any truth path is constructed)
        -> verify the execution's VCF bytes      (against the recorded digest)
        -> resolve + verify TRAIN truth bytes    (against the registered identity)
        -> copy the scoring inputs into the attempt sandbox and re-verify every byte
        -> resolve + verify the reference FASTA   (against the execution's recorded digest)
        -> call the PINNED MINOS_SUBNET scorer    (through the isolated oracle bridge)
        -> take its metrics, score and admission  (verbatim; nothing is recomputed here)
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
* **One scientific score authority, and it is not this repository.** The score, its
  normalization and the admission decision come from executing the pinned MINOS_SUBNET
  implementation. This module runs no hap.py command of its own, parses no metric file and
  applies no scoring formula; MINOS_ENGINE's historical local scorer is not on this path at all.
* **The scorer never touches registered evidence.** The upstream implementation legitimately
  writes intermediates beside the files it is handed, so it is handed COPIES inside the fresh
  attempt workspace — regular-file copies, never hard links, whose bytes are re-hashed and
  required to equal the registered identity before scoring begins.
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
    ScoringInputIdentity,
    TruthIdentity,
    build_metrics_artifact_bytes,
)
from minos_engine.evaluation.evaluator import (
    EvaluationPersistError,
    EvaluationPublishError,
    EvaluationRecordError,
    PersistedEvaluation,
    ScoreRecordingError,
    build_evaluation_record,
    evaluate_metrics,
    record_evaluation_failure,
    record_evaluation_result,
    register_metrics_artifact,
)
from minos_engine.evaluation.minos_subnet_oracle import (
    MinosSubnetAuthorityError,
    MinosSubnetExecutionError,
    MinosSubnetOracle,
    MinosSubnetTimeoutError,
)
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
    #: the reference ROOT, laid out exactly as the pinned validator expects:
    #: ``<reference_root>/<chromosome>/<chromosome>.fa`` and ``<chromosome>.sdf``. There is no
    #: second reference policy here; the chromosome comes from the execution identity.
    reference_root: Path
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


@dataclass(frozen=True)
class ResolvedReference:
    """The reference assets for one chromosome, under the pinned validator's own layout."""

    fasta: Path
    sdf: Path | None
    fasta_sha256: str


@dataclass(frozen=True)
class SandboxedScoringInputs:
    """Private COPIES of every mutable scoring input, inside one fresh attempt workspace."""

    truth_vcf: Path
    truth_tbi: Path
    mutations_vcf: Path
    mutations_tbi: Path
    query_vcf: Path
    truth: TruthIdentity
    query_vcf_sha256: str

    def identity(self, reference: ResolvedReference) -> ScoringInputIdentity:
        """The digests of the bytes the upstream scorer was actually handed."""
        return ScoringInputIdentity(
            truth_vcf_sha256=self.truth.truth_vcf_sha256,
            truth_tbi_sha256=self.truth.truth_tbi_sha256,
            mutations_vcf_sha256=self.truth.mutations_vcf_sha256,
            mutations_tbi_sha256=self.truth.mutations_tbi_sha256,
            query_vcf_sha256=self.query_vcf_sha256,
            reference_fasta_sha256=reference.fasta_sha256,
            reference_sdf_present=reference.sdf is not None,
        )


def _copy_into_sandbox(source: Path, destination: Path, *, expected_sha256: str) -> Path:
    """Copy one scoring input into the attempt sandbox and prove the copy is byte-identical.

    ``shutil.copyfile`` is used deliberately: a hard link would share the source inode, so an
    upstream reindex or rewrite would mutate MINOS_ENGINE's registered immutable evidence. The
    copy is re-hashed afterwards, so a truncated or racing write cannot pass silently.
    """
    import shutil

    if source.is_symlink() or not source.is_file():
        raise OrchestrationError(f"scoring input {source} is missing or a symlink")
    shutil.copyfile(source, destination)
    observed = _sha256_regular_file(destination, label=f"sandboxed {destination.name}")
    if observed != expected_sha256:
        raise OrchestrationError(
            f"sandbox copy {destination.name} hashes {observed}, expected {expected_sha256}"
        )
    return destination


def _copy_scoring_inputs(
    *,
    attempt_dir: Path,
    truth_dir: Path,
    vcf_path: Path,
    truth: TruthIdentity,
    observed_vcf_sha: str,
) -> SandboxedScoringInputs:
    """Materialize every mutable scoring input as a verified private copy.

    The registered originals stay read-only and untouched: nothing downstream is ever given a
    path into the practice corpus or the execution artifact store.
    """
    sandbox = attempt_dir / "scoring-inputs"
    sandbox.mkdir(mode=0o750)
    copies = {
        "truth.vcf.gz": (truth_dir / "truth.vcf.gz", truth.truth_vcf_sha256),
        "truth.vcf.gz.tbi": (truth_dir / "truth.vcf.gz.tbi", truth.truth_tbi_sha256),
        "mutations.vcf.gz": (truth_dir / "mutations.vcf.gz", truth.mutations_vcf_sha256),
        "mutations.vcf.gz.tbi": (truth_dir / "mutations.vcf.gz.tbi", truth.mutations_tbi_sha256),
    }
    for name, (source, digest) in copies.items():
        _copy_into_sandbox(source, sandbox / name, expected_sha256=digest)
    query = _copy_into_sandbox(vcf_path, sandbox / "query.vcf.gz", expected_sha256=observed_vcf_sha)
    return SandboxedScoringInputs(
        truth_vcf=sandbox / "truth.vcf.gz",
        truth_tbi=sandbox / "truth.vcf.gz.tbi",
        mutations_vcf=sandbox / "mutations.vcf.gz",
        mutations_tbi=sandbox / "mutations.vcf.gz.tbi",
        query_vcf=query,
        truth=truth,
        query_vcf_sha256=observed_vcf_sha,
    )


def _resolve_reference(
    reference_root: Path, *, chromosome: str, expected_sha256: str
) -> ResolvedReference:
    """Derive the chromosome's reference assets and bind the FASTA to the execution's digest.

    The layout is the pinned validator's own — ``<root>/<chrom>/<chrom>.fa`` beside
    ``<chrom>.sdf`` — never a second reference policy invented here. The SDF is EVALUATION-ONLY:
    it never enters Layer 1, a Layer-2 live feature or the GATK CONFIG search. If the pinned
    scorer needs it and it is absent, evaluation fails closed rather than scoring approximately.
    """
    if not reference_root.is_absolute():
        raise OrchestrationError(f"reference root {reference_root} must be absolute")
    if not chromosome or "/" in chromosome or chromosome.startswith("."):
        raise OrchestrationError(f"unsafe chromosome {chromosome!r}")
    directory = reference_root / chromosome
    fasta = directory / f"{chromosome}.fa"
    sdf = directory / f"{chromosome}.sdf"
    if fasta.is_symlink() or not fasta.is_file():
        raise OrchestrationError(f"reference FASTA {fasta} is missing or a symlink")
    observed = _sha256_regular_file(fasta, label="reference FASTA")
    if observed != expected_sha256:
        raise OrchestrationError(
            f"reference FASTA {fasta} hashes {observed}, but the execution recorded "
            f"{expected_sha256}; the scorer would compare against a different genome"
        )
    if sdf.is_symlink() or not sdf.is_dir():
        raise OrchestrationError(
            f"reference SDF {sdf} is missing or a symlink; the pinned scorer requires it and "
            "evaluation fails closed rather than scoring without it"
        )
    return ResolvedReference(fasta=fasta, sdf=sdf, fasta_sha256=observed)


def _resolve_execution(engine: Any, execution_result_id: str) -> dict[str, Any]:
    """Read the execution's identity from the narrow evaluator projection."""
    from sqlalchemy import text

    with engine.connect() as conn:
        row = (
            conn.execute(
                text(
                    "SELECT execution_result_id, execution_result_hash, dataset_registry_id, "
                    "       partition, dataset_id, round_id, chromosome, region_start0, "
                    "       region_end0_exclusive, reference_sha256, vcf_artifact_id, "
                    "       vcf_sha256, vcf_uri "
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
    oracle: MinosSubnetOracle,
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
            oracle=oracle,
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
    oracle: MinosSubnetOracle,
    publisher: EvaluationArtifactPublisher,
    provisioning: EvaluationProvisioning,
    truth: TruthIdentity,
    truth_dir: Path,
    vcf_path: Path,
    observed_vcf_sha: str,
    attempt_dir: Path,
    fail: Callable[..., EvaluationOutcome],
) -> EvaluationOutcome:
    """Everything the upstream scorer reads or writes, confined to ONE fresh attempt directory."""
    _fail = fail
    chromosome = str(execution["chromosome"])

    # 5. sandbox the scoring inputs. The upstream implementation defensively reindexes and writes
    #    intermediates beside the paths it is given; the registered evidence must never be what it
    #    writes beside. These are regular-file COPIES — never hard links, which would share the
    #    inode and let upstream mutate the registered bytes.
    try:
        sandbox = _copy_scoring_inputs(
            attempt_dir=attempt_dir,
            truth_dir=truth_dir,
            vcf_path=vcf_path,
            truth=truth,
            observed_vcf_sha=observed_vcf_sha,
        )
    except OrchestrationError:
        return _fail("TRUTH_BYTES_MISMATCH")

    # 6. the reference, under the pinned validator's own layout, bound to the execution's digest.
    try:
        reference = _resolve_reference(
            provisioning.reference_root,
            chromosome=chromosome,
            expected_sha256=str(execution["reference_sha256"]),
        )
    except OrchestrationError:
        return _fail("EVALUATION_ERROR")

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

    # 7. THE score — produced by the pinned MINOS_SUBNET implementation, not by this repository.
    #    Whatever internal tooling that implementation chooses to run (hap.py, Docker, bcftools,
    #    RTG) is its own business and is deliberately opaque here.
    try:
        oracle_result = oracle.score(
            truth_vcf=sandbox.truth_vcf,
            query_vcf=sandbox.query_vcf,
            mutations_vcf=sandbox.mutations_vcf,
            reference_fasta=reference.fasta,
            reference_sdf=reference.sdf,
            confident_bed=None,  # the mutations VCF defines the scope, exactly as upstream does
            region=inputs.scope.region_source,
            work_dir=attempt_dir,
        )
    except MinosSubnetAuthorityError:
        # the checkout is not provably the pinned authority: never scored under a substitute.
        return _fail("EVALUATION_ERROR")
    except MinosSubnetTimeoutError:
        return _fail("HAPPY_TIMEOUT")
    except MinosSubnetExecutionError:
        return _fail("HAPPY_NONZERO_EXIT")

    if not oracle_result.scored:
        # upstream ran but declined to produce metrics; that is a bounded, durable failure and
        # emphatically not a zero score.
        return _fail("HAPPY_OUTPUT_INVALID")

    # 8. record exactly what came back. No local scoring, no local admission rule.
    try:
        artifact, admission, _hash = evaluate_metrics(
            inputs=inputs,
            oracle_result=oracle_result,
            scoring_inputs=sandbox.identity(reference),
            authority=authority,
        )
    except ScoreRecordingError:
        return _fail("SCORER_OUTPUT_INVALID")

    # 9. publish the canonical document, then register it through the narrow registrar.
    try:
        published = publisher.publish(build_metrics_artifact_bytes(artifact))
    except EvaluationPublishError:
        return _fail("ARTIFACT_PUBLISH_FAILED")

    try:
        artifact_id, _created = register_metrics_artifact(engine, published)
        record = build_evaluation_record(
            execution_result_id=execution_result_id,
            inputs=inputs,
            artifact=artifact,
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
        return _fail("EVALUATION_ERROR")

    return EvaluationOutcome(
        execution_result_id=execution_result_id,
        status="EVALUATED",
        scoring_contract_hash=contract_hash,
        persisted=persisted,
        metrics_artifact_id=artifact_id,
        metrics_artifact_sha256=published.sha256,
    )
