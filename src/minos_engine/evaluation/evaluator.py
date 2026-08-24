"""Offline truth-aware evaluation: scoring, one record identity, immutable persistence.

This module is the only place truth meets an execution result, and it runs under
``minos_evaluator`` authority alone. It never runs GATK, never touches experiment jobs or
results, never imports live-controller code, and never lets a truth-derived value escape into a
Layer-1/Layer-2 live feature contract.

Two identity rules are structural rather than advisory:

* **One scoring authority.** Every authority-derived field — contract hash, upstream commit,
  both source digests, both container digests — comes from ONE :class:`ScoringAuthority`. There
  is no public path on which a caller supplies contract hash A alongside authority metadata B.
* **One evaluation record.** :class:`EvaluationRecord` owns the execution identity, truth
  identity, scoring contract, published metrics artifact, component scores and admission, and
  ``evaluation_hash`` is computed *from that record*. A caller cannot hand in scores and an
  independently chosen hash.

Persistence is transactional and idempotent through the ``SECURITY DEFINER`` functions of
migrations 0009/0010: an exact replay under the same scoring contract returns the existing row,
and a conflicting replay raises. There is deliberately no UPDATE/DELETE correction path — a
corrected evaluation is a NEW scoring contract, not a rewritten row.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any

from minos_engine.common.errors import MinosEngineError
from minos_engine.evaluation.artifact_publisher import (
    ENV_EVALUATION_ARTIFACT_ROOT,
    EVALUATION_METRICS_PROVENANCE,
    EvaluationArtifactPublisher,
    EvaluationPublishError,
    PublishedMetricsArtifact,
    evaluation_artifact_root_from_env,
)
from minos_engine.evaluation.contracts import (
    EVALUATION_METRICS_MEDIA_TYPE,
    EvaluationInputs,
    MetricsArtifact,
    build_metrics_artifact_bytes,
    compute_evaluation_hash,
)
from minos_engine.evaluation.minos_score import (
    AdvancedScoreBreakdown,
    compute_advanced_score,
    decide_admission,
)
from minos_engine.evaluation.scoring_contract import (
    AdmissionCode,
    ScoringAuthority,
    compute_scoring_contract_hash,
)

__all__ = [
    "ENV_EVALUATION_ARTIFACT_ROOT",
    "EvaluationArtifactPublisher",
    "EvaluationPersistError",
    "EvaluationPublishError",
    "EvaluationRecord",
    "EvaluationRecordError",
    "PersistedEvaluation",
    "PublishedMetricsArtifact",
    "build_evaluation_record",
    "evaluate_metrics",
    "evaluation_artifact_root_from_env",
    "register_metrics_artifact",
    "record_evaluation_failure",
    "record_evaluation_result",
]


class EvaluationPersistError(MinosEngineError):
    """The evaluation ledger refused the write."""


class EvaluationRecordError(MinosEngineError):
    """The evaluation record is internally inconsistent and must not be persisted."""


@dataclass(frozen=True)
class PersistedEvaluation:
    """The durable outcome of one evaluation."""

    evaluation_id: str
    evaluation_hash: str
    created: bool


@dataclass(frozen=True)
class EvaluationRecord:
    """THE single construction path for a persistable evaluation.

    Built only by :func:`build_evaluation_record`, which proves every internal consistency
    condition first, so nothing downstream has to re-check that (for example) the artifact this
    row cites is the document whose bytes were actually published.
    """

    execution_result_id: str
    inputs: EvaluationInputs
    artifact: MetricsArtifact
    breakdown: AdvancedScoreBreakdown
    admission_code: AdmissionCode
    authority: ScoringAuthority
    metrics_artifact_id: str
    metrics: PublishedMetricsArtifact

    @property
    def scoring_contract_hash(self) -> str:
        """Always recomputed from the authority — never a separately supplied value."""
        return compute_scoring_contract_hash(self.authority)

    @property
    def evaluation_hash(self) -> str:
        """The frozen evaluation identity, computed from exactly this record."""
        return compute_evaluation_hash(
            inputs=self.inputs,
            scoring_contract_hash=self.scoring_contract_hash,
            metrics_artifact_sha256=self.metrics.sha256,
            breakdown=self.breakdown,
            admission_code=self.admission_code,
        )


def evaluate_metrics(
    *,
    inputs: EvaluationInputs,
    happy_metrics: dict[str, Any],
    mutation_only_metrics: dict[str, Any],
    assessed_only_metrics: dict[str, Any],
    overcall: dict[str, Any],
    authority: ScoringAuthority,
) -> tuple[MetricsArtifact, AdvancedScoreBreakdown, AdmissionCode, str]:
    """Score parsed metrics and build the canonical artifact. Pure — no I/O, no database."""
    breakdown = compute_advanced_score(happy_metrics)
    admission = decide_admission(happy_metrics, breakdown)
    contract_hash = compute_scoring_contract_hash(authority)
    artifact = MetricsArtifact(
        execution_result_hash=inputs.execution_result_hash,
        scoring_contract_hash=contract_hash,
        truth_identity=inputs.truth,
        comparison_scope=inputs.scope,
        happy_metrics=happy_metrics,
        mutation_only_metrics=mutation_only_metrics,
        assessed_only_metrics=assessed_only_metrics,
        overcall=overcall,
        advanced_scorer={
            "completeness_score": breakdown.completeness_score,
            "core_score": breakdown.core_score,
            "fp_score": breakdown.fp_score,
            "minos_score": breakdown.minos_score,
            "minos_score_100": breakdown.minos_score_100,
            "overcall_penalty": breakdown.overcall_penalty,
            "quality_score": breakdown.quality_score,
        },
        admission={"admitted": admission == "ADMITTED", "admission_code": admission},
        tool_identity={
            "bcftools_image": authority.bcftools_image,
            "happy_image": authority.happy_image,
            "scoring_py_sha256": authority.scoring_py_sha256,
            "upstream_commit": authority.upstream_commit,
            "validator_py_sha256": authority.validator_py_sha256,
        },
    )
    return artifact, breakdown, admission, contract_hash


def build_evaluation_record(
    *,
    execution_result_id: str,
    inputs: EvaluationInputs,
    artifact: MetricsArtifact,
    breakdown: AdvancedScoreBreakdown,
    admission_code: AdmissionCode,
    authority: ScoringAuthority,
    metrics_artifact_id: str,
    metrics: PublishedMetricsArtifact,
) -> EvaluationRecord:
    """Construct the record, refusing every internally inconsistent combination.

    These are the substitutions the checks make unrepresentable: an artifact scored under a
    different contract, an artifact describing a different execution, a published document whose
    bytes are not the artifact's bytes, and a document classified as anything other than the
    L2-F2 metrics media type.
    """
    contract_hash = compute_scoring_contract_hash(authority)
    if artifact.scoring_contract_hash != contract_hash:
        raise EvaluationRecordError(
            "metrics artifact was scored under a different scoring contract than the authority "
            "supplied for persistence"
        )
    if artifact.execution_result_hash != inputs.execution_result_hash:
        raise EvaluationRecordError(
            "metrics artifact describes a different execution than the evaluation inputs"
        )
    if artifact.truth_identity != inputs.truth or artifact.comparison_scope != inputs.scope:
        raise EvaluationRecordError(
            "metrics artifact truth identity or comparison scope disagrees with the inputs"
        )
    expected_sha = hashlib.sha256(build_metrics_artifact_bytes(artifact)).hexdigest()
    if metrics.sha256 != expected_sha:
        raise EvaluationRecordError(
            "the published metrics document is not this artifact's bytes; the evaluation row "
            "would cite a document it did not produce"
        )
    if metrics.media_type != EVALUATION_METRICS_MEDIA_TYPE:
        raise EvaluationRecordError(
            f"metrics artifact media type {metrics.media_type!r} is not the L2-F2 metrics type"
        )
    if metrics.provenance != EVALUATION_METRICS_PROVENANCE:
        raise EvaluationRecordError(
            f"metrics artifact provenance {metrics.provenance!r} is not "
            f"{EVALUATION_METRICS_PROVENANCE!r}"
        )
    return EvaluationRecord(
        execution_result_id=execution_result_id,
        inputs=inputs,
        artifact=artifact,
        breakdown=breakdown,
        admission_code=admission_code,
        authority=authority,
        metrics_artifact_id=metrics_artifact_id,
        metrics=metrics,
    )


def register_metrics_artifact(engine: Any, published: PublishedMetricsArtifact) -> tuple[str, bool]:
    """Register the published metrics document through the narrow 0010 registrar.

    ``minos_evaluator`` has no ``INSERT`` on ``catalog.artifacts`` by design. The registrar takes
    only content identity — digest, URI, size — and fixes media type and provenance itself, so
    this path cannot be used to register some other kind of artifact. Returns
    ``(artifact_id, created)``; an exact re-registration returns the existing id.
    """
    from sqlalchemy import text

    with engine.connect() as conn, conn.begin():
        row = conn.execute(
            text(
                "SELECT artifact_id, created FROM evaluation.l2f_register_metrics_artifact("
                ":sha, :uri, :size)"
            ),
            {"sha": published.sha256, "uri": published.uri, "size": published.size_bytes},
        ).one()
    return str(row[0]), bool(row[1])


def record_evaluation_result(engine: Any, record: EvaluationRecord) -> PersistedEvaluation:
    """Persist one immutable evaluation. Idempotent per (execution, scoring contract).

    Dataset, partition and truth identity are NOT passed: the ``SECURITY DEFINER`` function
    derives them from the execution's own lineage, so a caller cannot score execution A against
    dataset or truth B. Everything else comes from ``record`` alone.
    """
    from sqlalchemy import text

    authority = record.authority
    breakdown = record.breakdown
    with engine.connect() as conn, conn.begin():
        row = conn.execute(
            text(
                "SELECT evaluation_id, created FROM evaluation.l2f_record_evaluation_result("
                ":exec_id, :contract, :commit, :scoring_py, :validator_py, :happy, :bcftools, "
                ":artifact_id, :artifact_sha, :media_type, :core, :completeness, :fp, :quality, "
                ":overcall, :score100, :score, :admission, :eval_hash)"
            ),
            {
                "exec_id": record.execution_result_id,
                "contract": record.scoring_contract_hash,
                "commit": authority.upstream_commit,
                "scoring_py": authority.scoring_py_sha256,
                "validator_py": authority.validator_py_sha256,
                "happy": authority.happy_image,
                "bcftools": authority.bcftools_image,
                "artifact_id": record.metrics_artifact_id,
                "artifact_sha": record.metrics.sha256,
                "media_type": record.metrics.media_type,
                "core": breakdown.core_score,
                "completeness": breakdown.completeness_score,
                "fp": breakdown.fp_score,
                "quality": breakdown.quality_score,
                "overcall": breakdown.overcall_penalty,
                "score100": breakdown.minos_score_100,
                "score": breakdown.minos_score,
                "admission": record.admission_code,
                "eval_hash": record.evaluation_hash,
            },
        ).one()
    return PersistedEvaluation(
        evaluation_id=str(row[0]), evaluation_hash=record.evaluation_hash, created=bool(row[1])
    )


def record_evaluation_failure(
    engine: Any,
    *,
    execution_result_id: str,
    scoring_contract_hash: str,
    failure_code: str,
    tool_exit_code: int | None = None,
    stderr_sha256: str | None = None,
) -> tuple[str, bool]:
    """Persist one immutable evaluation failure. Idempotent; a conflicting replay raises.

    An infrastructure failure must never disappear as a missing row: baseline statistics that
    silently omit failures would overstate a candidate's robustness.
    """
    from sqlalchemy import text

    with engine.connect() as conn, conn.begin():
        row = conn.execute(
            text(
                "SELECT failure_id, created FROM evaluation.l2f_record_evaluation_failure("
                ":exec_id, :contract, :code, :exit_code, :stderr)"
            ),
            {
                "exec_id": execution_result_id,
                "contract": scoring_contract_hash,
                "code": failure_code,
                "exit_code": tool_exit_code,
                "stderr": stderr_sha256,
            },
        ).one()
    return str(row[0]), bool(row[1])
