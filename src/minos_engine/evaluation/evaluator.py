"""Offline truth-aware evaluation: artifact publishing and immutable persistence.

This module is the only place truth meets an execution result, and it runs under
``minos_evaluator`` authority alone. It never runs GATK, never touches experiment jobs or
results, never imports live-controller code, and never lets a truth-derived value escape into a
Layer-1/Layer-2 live feature contract.

Persistence is transactional and idempotent through migration 0009's ``SECURITY DEFINER``
functions: an exact replay under the same scoring contract returns the existing row, and a
conflicting replay raises. There is deliberately no UPDATE/DELETE correction path — a corrected
evaluation is a NEW scoring contract, not a rewritten row.
"""

from __future__ import annotations

import hashlib
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from minos_engine.common.errors import MinosEngineError
from minos_engine.evaluation.contracts import (
    EVALUATION_METRICS_MEDIA_TYPE,
    EvaluationInputs,
    MetricsArtifact,
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
    "PersistedEvaluation",
    "evaluate_metrics",
    "evaluation_artifact_root_from_env",
    "record_evaluation_failure",
    "record_evaluation_result",
]

ENV_EVALUATION_ARTIFACT_ROOT = "MINOS_L2F2_EVALUATION_ARTIFACT_ROOT"

#: artifact roots are setgid group-readable, matching the L2-F1 artifact policy.
_ARTIFACT_ROOT_MODE = 0o2750
_ARTIFACT_FILE_MODE = 0o640


class EvaluationPublishError(MinosEngineError):
    """The evaluation artifact root or a published file is unsafe."""


class EvaluationPersistError(MinosEngineError):
    """The evaluation ledger refused the write."""


@dataclass(frozen=True)
class PersistedEvaluation:
    """The durable outcome of one evaluation."""

    evaluation_id: str
    evaluation_hash: str
    created: bool


def evaluation_artifact_root_from_env() -> Path:
    """Resolve and validate the evaluation artifact root from the environment."""
    raw = os.environ.get(ENV_EVALUATION_ARTIFACT_ROOT)
    if raw is None or not raw.strip():
        raise EvaluationPublishError(
            f"{ENV_EVALUATION_ARTIFACT_ROOT} is not set; the artifact root must be provisioned"
        )
    return _require_root(Path(raw.strip()))


def _require_root(root: Path) -> Path:
    if not root.is_absolute():
        raise EvaluationPublishError(f"evaluation artifact root {root} must be absolute")
    info = os.lstat(root) if root.exists() or root.is_symlink() else None
    if info is None:
        raise EvaluationPublishError(f"evaluation artifact root {root} does not exist")
    if stat.S_ISLNK(info.st_mode):
        raise EvaluationPublishError(f"evaluation artifact root {root} is a symlink")
    if not stat.S_ISDIR(info.st_mode):
        raise EvaluationPublishError(f"evaluation artifact root {root} is not a directory")
    if info.st_uid != os.geteuid():
        raise EvaluationPublishError(f"evaluation artifact root {root} is not owned by this user")
    if stat.S_IMODE(info.st_mode) != _ARTIFACT_ROOT_MODE:
        raise EvaluationPublishError(
            f"evaluation artifact root {root} has mode {stat.S_IMODE(info.st_mode):#o}, "
            f"expected {_ARTIFACT_ROOT_MODE:#o}"
        )
    return root


@dataclass(frozen=True)
class EvaluationArtifactPublisher:
    """Publishes the canonical metrics document under its own content address."""

    root: Path

    def publish(self, payload: bytes) -> tuple[str, str]:
        """Write ``<sha256>.metrics.json`` exactly once. Returns ``(sha256, uri)``."""
        root = _require_root(self.root)
        digest = hashlib.sha256(payload).hexdigest()
        path = root / f"{digest}.metrics.json"
        if path.exists():
            # content-addressed: identical bytes are already published, which is not an error.
            if hashlib.sha256(path.read_bytes()).hexdigest() != digest:  # pragma: no cover
                raise EvaluationPublishError(f"published artifact {path} does not match its name")
            return digest, path.as_uri()
        tmp = root / f".{digest}.partial"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, _ARTIFACT_FILE_MODE)
        try:
            os.write(fd, payload)
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(tmp, path)
        return digest, path.as_uri()


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


def record_evaluation_result(
    engine: Any,
    *,
    execution_result_id: str,
    inputs: EvaluationInputs,
    artifact: MetricsArtifact,
    breakdown: AdvancedScoreBreakdown,
    admission_code: AdmissionCode,
    authority: ScoringAuthority,
    metrics_artifact_id: str,
    metrics_artifact_sha256: str,
) -> PersistedEvaluation:
    """Persist one immutable evaluation. Idempotent per (execution, scoring contract).

    Dataset, partition and truth identity are NOT passed: the ``SECURITY DEFINER`` function
    derives them from the execution's own lineage, so a caller cannot score execution A against
    dataset or truth B.
    """
    from sqlalchemy import text

    contract_hash = compute_scoring_contract_hash(authority)
    evaluation_hash = compute_evaluation_hash(
        inputs=inputs,
        scoring_contract_hash=contract_hash,
        metrics_artifact_sha256=metrics_artifact_sha256,
        breakdown=breakdown,
        admission_code=admission_code,
    )
    with engine.connect() as conn, conn.begin():
        row = conn.execute(
            text(
                "SELECT evaluation_id, created FROM evaluation.l2f_record_evaluation_result("
                ":exec_id, :contract, :commit, :scoring_py, :validator_py, :happy, :bcftools, "
                ":artifact_id, :artifact_sha, :media_type, :core, :completeness, :fp, :quality, "
                ":overcall, :score100, :score, :admission, :eval_hash)"
            ),
            {
                "exec_id": execution_result_id,
                "contract": contract_hash,
                "commit": authority.upstream_commit,
                "scoring_py": authority.scoring_py_sha256,
                "validator_py": authority.validator_py_sha256,
                "happy": authority.happy_image,
                "bcftools": authority.bcftools_image,
                "artifact_id": metrics_artifact_id,
                "artifact_sha": metrics_artifact_sha256,
                "media_type": EVALUATION_METRICS_MEDIA_TYPE,
                "core": breakdown.core_score,
                "completeness": breakdown.completeness_score,
                "fp": breakdown.fp_score,
                "quality": breakdown.quality_score,
                "overcall": breakdown.overcall_penalty,
                "score100": breakdown.minos_score_100,
                "score": breakdown.minos_score,
                "admission": admission_code,
                "eval_hash": evaluation_hash,
            },
        ).one()
    _ = artifact  # bound into evaluation_hash via metrics_artifact_sha256
    return PersistedEvaluation(
        evaluation_id=str(row[0]), evaluation_hash=evaluation_hash, created=bool(row[1])
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
