"""Pure L2-F2 evaluation contracts — metrics artifact, evaluation identity, typed failures.

Everything here is deterministic and host-independent. The ``evaluation_hash`` binds only
scientific identity: no absolute path, no timestamp, no hostname, no storage-locator UUID. Two
machines evaluating the same execution under the same scoring contract must produce the same
hash, which is what makes the ledger reproducible rather than merely append-only.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from minos_engine.common.canonical_json import canonical_json_bytes
from minos_engine.common.errors import MinosEngineError
from minos_engine.common.hashing import sha256_hex
from minos_engine.evaluation.minos_score import AdvancedScoreBreakdown
from minos_engine.evaluation.scoring_contract import AdmissionCode

__all__ = [
    "EVALUATION_HASH_DOMAIN",
    "EVALUATION_METRICS_MEDIA_TYPE",
    "EVALUATION_METRICS_SCHEMA",
    "FAILURE_CODES",
    "ComparisonScope",
    "EvaluationFailureCode",
    "EvaluationInputs",
    "MetricsArtifact",
    "TruthIdentity",
    "build_metrics_artifact_bytes",
    "compute_evaluation_hash",
]

EVALUATION_METRICS_SCHEMA = "l2f2-evaluation-metrics-v1"
EVALUATION_METRICS_MEDIA_TYPE = "application/vnd.minos.l2f2-evaluation-metrics+json"
EVALUATION_HASH_DOMAIN = "minos:l2f2-evaluation-result:v1\n"

EvaluationFailureCode = Literal[
    "TRUTH_IDENTITY_MISSING",
    "TRUTH_BYTES_MISMATCH",
    "VCF_BYTES_MISMATCH",
    "HAPPY_NONZERO_EXIT",
    "HAPPY_TIMEOUT",
    "HAPPY_OUTPUT_INVALID",
    "SCORER_OUTPUT_INVALID",
    "ARTIFACT_PUBLISH_FAILED",
    "EVALUATION_ERROR",
]

#: mirrors the bounded vocabulary migration 0009 enforces with a CHECK constraint.
FAILURE_CODES: tuple[str, ...] = (
    "TRUTH_IDENTITY_MISSING",
    "TRUTH_BYTES_MISMATCH",
    "VCF_BYTES_MISMATCH",
    "HAPPY_NONZERO_EXIT",
    "HAPPY_TIMEOUT",
    "HAPPY_OUTPUT_INVALID",
    "SCORER_OUTPUT_INVALID",
    "ARTIFACT_PUBLISH_FAILED",
    "EVALUATION_ERROR",
)

_STRICT = ConfigDict(extra="forbid", frozen=True, strict=True)


class EvaluationContractError(MinosEngineError):
    """A malformed evaluation contract object."""


class TruthIdentity(BaseModel):
    """Truth bound by CONTENT, never by path — the F7 stale-path lesson applied to truth."""

    model_config = _STRICT

    truth_vcf_sha256: str = Field(min_length=64, max_length=64)
    truth_tbi_sha256: str = Field(min_length=64, max_length=64)
    mutations_vcf_sha256: str = Field(min_length=64, max_length=64)
    mutations_tbi_sha256: str = Field(min_length=64, max_length=64)


class ComparisonScope(BaseModel):
    """The exact region hap.py was asked to assess."""

    model_config = _STRICT

    chromosome: str = Field(min_length=1)
    region_start0: int = Field(ge=0)
    region_end0_exclusive: int = Field(gt=0)
    region_source: str = Field(min_length=1)


class EvaluationInputs(BaseModel):
    """Everything the evaluation identity is derived from."""

    model_config = _STRICT

    execution_result_hash: str = Field(min_length=64, max_length=64)
    dataset_id: str = Field(min_length=1)
    partition: str = Field(min_length=1)
    vcf_sha256: str = Field(min_length=64, max_length=64)
    truth: TruthIdentity
    scope: ComparisonScope


class MetricsArtifact(BaseModel):
    """The canonical, content-addressed metrics document.

    The full hap.py metric set lives here rather than in SQL: normalising every field would bloat
    the schema without making anything more provable, while the few components the baseline and
    model stages must *query* are promoted to typed columns in ``l2f_evaluation_results``.
    """

    model_config = _STRICT

    schema_version: Literal["l2f2-evaluation-metrics-v1"] = "l2f2-evaluation-metrics-v1"
    execution_result_hash: str = Field(min_length=64, max_length=64)
    scoring_contract_hash: str = Field(min_length=64, max_length=64)
    truth_identity: TruthIdentity
    comparison_scope: ComparisonScope
    happy_metrics: dict[str, Any]
    mutation_only_metrics: dict[str, Any]
    assessed_only_metrics: dict[str, Any]
    overcall: dict[str, Any]
    advanced_scorer: dict[str, float]
    admission: dict[str, Any]
    tool_identity: dict[str, str]


def build_metrics_artifact_bytes(artifact: MetricsArtifact) -> bytes:
    """Canonical bytes — deterministic key order, no timestamps, no paths."""
    return canonical_json_bytes(artifact.model_dump(mode="json"))


def compute_evaluation_hash(
    *,
    inputs: EvaluationInputs,
    scoring_contract_hash: str,
    metrics_artifact_sha256: str,
    breakdown: AdvancedScoreBreakdown,
    admission_code: AdmissionCode,
) -> str:
    """THE frozen L2-F2 evaluation identity.

    Binds only reproducible science: which execution, which dataset/partition, which truth bytes,
    which scoring semantics, which metrics document, and the resulting scores and admission.
    Excludes absolute paths, timestamps, hostnames and database UUIDs, so the same evaluation
    performed on another machine hashes identically.
    """
    content = {
        "admission_code": admission_code,
        "completeness_score": breakdown.completeness_score,
        "core_score": breakdown.core_score,
        "dataset_id": inputs.dataset_id,
        "execution_result_hash": inputs.execution_result_hash,
        "fp_score": breakdown.fp_score,
        "metrics_artifact_sha256": metrics_artifact_sha256,
        "minos_score": breakdown.minos_score,
        "minos_score_100": breakdown.minos_score_100,
        "mutations_tbi_sha256": inputs.truth.mutations_tbi_sha256,
        "mutations_vcf_sha256": inputs.truth.mutations_vcf_sha256,
        "overcall_penalty": breakdown.overcall_penalty,
        "partition": inputs.partition,
        "quality_score": breakdown.quality_score,
        "scope": inputs.scope.model_dump(mode="json"),
        "scoring_contract_hash": scoring_contract_hash,
        "truth_tbi_sha256": inputs.truth.truth_tbi_sha256,
        "truth_vcf_sha256": inputs.truth.truth_vcf_sha256,
        "vcf_sha256": inputs.vcf_sha256,
    }
    return sha256_hex(EVALUATION_HASH_DOMAIN.encode("utf-8") + canonical_json_bytes(content))
