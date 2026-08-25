"""Pure L2-F2 evaluation contracts — metrics artifact, evaluation identity, typed failures.

Everything here is deterministic and host-independent. The ``evaluation_hash`` binds only
scientific identity: no absolute path, no timestamp, no hostname, no storage-locator UUID. Two
machines evaluating the same execution under the same scoring contract must produce the same
hash, which is what makes the ledger reproducible rather than merely append-only.

This module imports nothing from MINOS_ENGINE's historical local scorer. The scientific values it
carries all originate upstream: see :mod:`minos_engine.evaluation.minos_subnet_oracle`.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from minos_engine.common.canonical_json import canonical_json_bytes
from minos_engine.common.errors import MinosEngineError
from minos_engine.common.hashing import sha256_hex

__all__ = [
    "EVALUATION_HASH_DOMAIN",
    "EVALUATION_METRICS_MEDIA_TYPE",
    "EVALUATION_METRICS_SCHEMA",
    "FAILURE_CODES",
    "ComparisonScope",
    "EvaluationFailureCode",
    "EvaluationInputs",
    "MetricsArtifact",
    "ScoringInputIdentity",
    "TruthIdentity",
    "UpstreamScoreOutput",
    "build_metrics_artifact_bytes",
    "compute_evaluation_hash",
]

EVALUATION_METRICS_SCHEMA = "l2f2-evaluation-metrics-v2"
EVALUATION_METRICS_MEDIA_TYPE = "application/vnd.minos.l2f2-evaluation-metrics+json"
#: v2 starts a NEW evaluation-identity domain. v1 bound MINOS_ENGINE's own AdvancedScorer
#: component values; the pinned upstream scorer does not expose those, so the identity now
#: binds the exact upstream outputs and the exact upstream source it came from instead.
#: No evaluation was ever persisted under v1, so nothing is invalidated by the change.
EVALUATION_HASH_DOMAIN = "minos:l2f2-evaluation-result:v2\n"

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


class ScoringInputIdentity(BaseModel):
    """The bytes actually handed to the upstream scorer, hashed after being sandbox-copied.

    The upstream scorer legitimately writes intermediates beside the files it is given, so it is
    never given the registered originals. These digests prove the copies it *did* receive were
    byte-identical to the registered evidence.
    """

    model_config = _STRICT

    truth_vcf_sha256: str = Field(min_length=64, max_length=64)
    truth_tbi_sha256: str = Field(min_length=64, max_length=64)
    mutations_vcf_sha256: str = Field(min_length=64, max_length=64)
    mutations_tbi_sha256: str = Field(min_length=64, max_length=64)
    query_vcf_sha256: str = Field(min_length=64, max_length=64)
    reference_fasta_sha256: str = Field(min_length=64, max_length=64)
    reference_sdf_present: bool


class UpstreamScoreOutput(BaseModel):
    """EXACTLY what the pinned MINOS_SUBNET implementation returned. Nothing is recomputed here.

    Every field is a value that crossed the oracle boundary from upstream. MINOS_ENGINE adds no
    metric, derives no component and adjusts no score — if a number is not in this object,
    upstream did not produce it.
    """

    model_config = _STRICT

    repository: str = Field(min_length=1)
    commit: str = Field(min_length=40, max_length=40)
    source_sha256: dict[str, str]
    metrics: dict[str, Any]
    advanced_score_100: float
    minos_score: float
    minos_score_accepted: bool
    zero_input_fingerprint: bool
    admitted: bool
    admission_code: str = Field(min_length=1)
    happy_docker_image: str = Field(min_length=1)
    bcftools_docker_image: str = Field(min_length=1)


class MetricsArtifact(BaseModel):
    """The canonical, content-addressed metrics document.

    It is deliberately split in two. ``upstream`` is the scientific output of the pinned
    MINOS_SUBNET scorer, verbatim. Everything else is MINOS_ENGINE provenance: which execution,
    which truth bytes, which region, which scoring contract, and which bytes the scorer was
    handed. Nothing MINOS_ENGINE computed is ever placed inside ``upstream``.

    The full metric set lives here rather than in SQL: normalising every field would bloat the
    schema without making anything more provable, while the values the baseline and model stages
    must *query* are promoted to typed columns in ``l2f_evaluation_results``.
    """

    model_config = _STRICT

    schema_version: Literal["l2f2-evaluation-metrics-v2"] = "l2f2-evaluation-metrics-v2"
    execution_result_hash: str = Field(min_length=64, max_length=64)
    scoring_contract_hash: str = Field(min_length=64, max_length=64)
    truth_identity: TruthIdentity
    comparison_scope: ComparisonScope
    scoring_inputs: ScoringInputIdentity
    upstream: UpstreamScoreOutput


def build_metrics_artifact_bytes(artifact: MetricsArtifact) -> bytes:
    """Canonical bytes — deterministic key order, no timestamps, no paths."""
    return canonical_json_bytes(artifact.model_dump(mode="json"))


def compute_evaluation_hash(
    *,
    inputs: EvaluationInputs,
    scoring_contract_hash: str,
    metrics_artifact_sha256: str,
    upstream: UpstreamScoreOutput,
) -> str:
    """THE frozen L2-F2 evaluation identity (domain v2).

    Binds only reproducible science: which execution, which dataset/partition, which truth bytes,
    which region, which scoring semantics, which metrics document, WHICH UPSTREAM SOURCE produced
    the score, and the exact score and admission that source returned. Excludes absolute paths,
    timestamps, hostnames and database UUIDs, so the same evaluation performed on another machine
    hashes identically.

    It deliberately does NOT bind AdvancedScorer component values. The pinned upstream scorer
    does not expose them, and reconstructing them locally to feed an identity would make that
    identity depend on a second implementation of the very formula it is meant to attest.
    """
    content = {
        "admission_code": upstream.admission_code,
        "advanced_score_100": upstream.advanced_score_100,
        "dataset_id": inputs.dataset_id,
        "execution_result_hash": inputs.execution_result_hash,
        "metrics_artifact_sha256": metrics_artifact_sha256,
        "minos_score": upstream.minos_score,
        "minos_score_accepted": upstream.minos_score_accepted,
        "mutations_tbi_sha256": inputs.truth.mutations_tbi_sha256,
        "mutations_vcf_sha256": inputs.truth.mutations_vcf_sha256,
        "overcall_penalty": _upstream_overcall_penalty(upstream),
        "partition": inputs.partition,
        "scope": inputs.scope.model_dump(mode="json"),
        "scoring_contract_hash": scoring_contract_hash,
        "truth_tbi_sha256": inputs.truth.truth_tbi_sha256,
        "truth_vcf_sha256": inputs.truth.truth_vcf_sha256,
        "upstream_commit": upstream.commit,
        "upstream_source_sha256": dict(sorted(upstream.source_sha256.items())),
        "vcf_sha256": inputs.vcf_sha256,
    }
    return sha256_hex(EVALUATION_HASH_DOMAIN.encode("utf-8") + canonical_json_bytes(content))


def _upstream_overcall_penalty(upstream: UpstreamScoreOutput) -> float:
    """The penalty UPSTREAM applied, read out of the upstream metrics — never recomputed."""
    return float(upstream.metrics.get("overcall_penalty", 0.0) or 0.0)
