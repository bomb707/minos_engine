"""Immutable Stage 1 (Validator Twin) contracts.

All models are frozen pydantic v2 with ``extra="forbid"``. Content-addressed
artifacts compute a self hash from their canonical content excluding operational
metadata (``created_at``). Required identities fail closed.
"""

from __future__ import annotations

import math
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from minos_engine.common.timestamps import is_iso8601_utc
from minos_engine.intake.contracts import Region

from .identities import SHA256_RE, ToolIdentity, content_hash
from .unavailable import AvailabilityStatus, ReasonCode

__all__ = [
    "ParityLevel",
    "PARITY_ORDER",
    "DECLARED_PARITY_LEVEL",
    "parity_rank",
    "ToolInvocation",
    "TwinExecutionRequest",
    "GatkExecutionPlan",
    "ComparisonRequest",
    "VariantClassCounts",
    "ComparisonMetrics",
    "ScoreInputs",
    "ScoreComponents",
    "TwinScoreResult",
    "ParityDifferenceKind",
    "ParityDifference",
    "ParityExpectation",
    "ParityObservation",
    "TwinParityReport",
    "TwinRunManifest",
]


class ParityLevel(str, Enum):
    STRUCTURAL = "STRUCTURAL"
    FIXTURE_REPLAY = "FIXTURE_REPLAY"
    TOOL_EXECUTION = "TOOL_EXECUTION"
    VALIDATOR_CONFIRMED = "VALIDATOR_CONFIRMED"


PARITY_ORDER = (
    ParityLevel.STRUCTURAL,
    ParityLevel.FIXTURE_REPLAY,
    ParityLevel.TOOL_EXECUTION,
    ParityLevel.VALIDATOR_CONFIRMED,
)
# Stage 1 honestly achieves fixture replay (no real GATK/hap.py; scorer unknown).
DECLARED_PARITY_LEVEL = ParityLevel.FIXTURE_REPLAY


def parity_rank(level: ParityLevel) -> int:
    return PARITY_ORDER.index(level)


def _require_sha(v: str, field: str) -> str:
    s = v.strip()
    if not SHA256_RE.match(s):
        raise ValueError(f"{field} must be 64 lowercase hex characters")
    return s


# --------------------------------------------------------------------------- #
# Execution planning
# --------------------------------------------------------------------------- #


class ToolInvocation(BaseModel):
    """A side-effect-free description of an external tool invocation."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    tool: ToolIdentity
    argv: tuple[str, ...]
    declared_inputs: dict[str, str] = Field(default_factory=dict)
    declared_outputs: dict[str, str] = Field(default_factory=dict)

    @field_validator("argv")
    @classmethod
    def _argv_nonempty(cls, v: tuple[str, ...]) -> tuple[str, ...]:
        if not v:
            raise ValueError("argv must be a non-empty token vector")
        if any(not isinstance(tok, str) for tok in v):
            raise ValueError("argv tokens must be strings")
        return v

    def redacted_command(self) -> str:
        """Human-readable command with credential-like tokens redacted."""
        redacted = []
        for tok in self.argv:
            low = tok.lower()
            if any(s in low for s in ("token=", "signature=", "x-amz-", "sig=", "://")) and (
                "http" in low or "s3" in low or "token=" in low or "sig" in low
            ):
                redacted.append("<redacted>")
            else:
                redacted.append(tok)
        return " ".join(redacted)


class TwinExecutionRequest(BaseModel):
    """Everything needed to build a GATK execution plan for the Twin."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: str = Field(default="twin-execution-request-v1")
    round_id: str = Field(min_length=1)
    region: Region
    requested_config: dict[str, Any]
    parameter_space_hash: str
    protocol_snapshot_hash: str
    reference_sha256: str
    bam_sha256: str | None = None
    output_uri: str = Field(min_length=1)
    budget_seconds: float = Field(gt=0)
    gatk_tool: ToolIdentity
    engine_git_sha: str = Field(min_length=1)

    @field_validator("parameter_space_hash", "protocol_snapshot_hash")
    @classmethod
    def _hash_shape(cls, v: str, info: Any) -> str:
        return _require_sha(v, info.field_name)

    @field_validator("reference_sha256")
    @classmethod
    def _ref(cls, v: str) -> str:
        return _require_sha(v, "reference_sha256")

    @field_validator("bam_sha256")
    @classmethod
    def _bam(cls, v: str | None) -> str | None:
        return None if v is None else _require_sha(v, "bam_sha256")

    def content_hash(self) -> str:
        return content_hash(self)


class GatkExecutionPlan(BaseModel):
    """Deterministic, side-effect-free GATK execution plan (no execution)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: str = Field(default="twin-execution-plan-v1")
    round_id: str = Field(min_length=1)
    caller: str
    region: Region
    effective_config: dict[str, Any]
    config_hash: str
    parameter_space_hash: str
    invocation: ToolInvocation
    plan_hash: str = Field(default="")

    @field_validator("caller")
    @classmethod
    def _gatk_only(cls, v: str) -> str:
        if v != "gatk":
            raise ValueError(f"caller must be 'gatk' (GATK-only policy), got {v!r}")
        return v

    @field_validator("config_hash", "parameter_space_hash")
    @classmethod
    def _hashes(cls, v: str, info: Any) -> str:
        return _require_sha(v, info.field_name)

    @model_validator(mode="after")
    def _identity(self) -> GatkExecutionPlan:
        expected = content_hash(self, exclude={"plan_hash"})
        if self.plan_hash == "":
            object.__setattr__(self, "plan_hash", expected)
        elif self.plan_hash != expected:
            raise ValueError(f"plan_hash does not match canonical content (expected {expected})")
        return self


# --------------------------------------------------------------------------- #
# Comparison (hap.py-style)
# --------------------------------------------------------------------------- #


class ComparisonRequest(BaseModel):
    """Offline comparison request. Truth identities appear ONLY here / offline."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: str = Field(default="twin-comparison-request-v1")
    round_id: str = Field(min_length=1)
    region: Region
    reference_sha256: str
    truth_vcf_sha256: str
    query_vcf_sha256: str
    tool: ToolIdentity

    @field_validator("reference_sha256", "truth_vcf_sha256", "query_vcf_sha256")
    @classmethod
    def _sha(cls, v: str, info: Any) -> str:
        return _require_sha(v, info.field_name)

    def content_hash(self) -> str:
        return content_hash(self)


class VariantClassCounts(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    tp: int = Field(ge=0)
    fp: int = Field(ge=0)
    fn: int = Field(ge=0)

    @property
    def query_total(self) -> int:
        return self.tp + self.fp

    @property
    def truth_total(self) -> int:
        return self.tp + self.fn


class ComparisonMetrics(BaseModel):
    """Normalized hap.py-style comparison metrics with recomputed rates."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: str = Field(default="twin-comparison-result-v1")
    round_id: str = Field(min_length=1)
    region: Region
    reference_sha256: str
    snp: VariantClassCounts
    indel: VariantClassCounts
    snp_precision: float
    snp_recall: float
    snp_f1: float
    indel_precision: float
    indel_recall: float
    indel_f1: float
    total_calls: int = Field(ge=0)
    ti_tv: float | None = None
    het_hom: float | None = None
    truth_vcf_sha256: str
    query_vcf_sha256: str
    tool: ToolIdentity
    raw_result_hash: str

    @field_validator("reference_sha256", "truth_vcf_sha256", "query_vcf_sha256", "raw_result_hash")
    @classmethod
    def _sha(cls, v: str, info: Any) -> str:
        return _require_sha(v, info.field_name)

    @field_validator(
        "snp_precision",
        "snp_recall",
        "snp_f1",
        "indel_precision",
        "indel_recall",
        "indel_f1",
        "ti_tv",
        "het_hom",
    )
    @classmethod
    def _finite_rate(cls, v: float | None, info: Any) -> float | None:
        if v is None:
            return None
        if not math.isfinite(v):
            raise ValueError(f"{info.field_name} must be finite")
        if info.field_name in {"ti_tv", "het_hom"}:
            if v < 0:
                raise ValueError(f"{info.field_name} must be >= 0")
            return v
        if not (0.0 <= v <= 1.0):
            raise ValueError(f"{info.field_name} must be in [0, 1]")
        return v

    @model_validator(mode="after")
    def _consistency(self) -> ComparisonMetrics:
        expected_calls = self.snp.query_total + self.indel.query_total
        if self.total_calls != expected_calls:
            raise ValueError(
                f"total_calls ({self.total_calls}) != SNP+INDEL (TP+FP) ({expected_calls})"
            )
        return self

    def content_hash(self) -> str:
        return content_hash(self)


# --------------------------------------------------------------------------- #
# Scoring
# --------------------------------------------------------------------------- #


class ScoreInputs(BaseModel):
    """Normalized inputs a scorer would consume (authoritative, standard defs)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: str = Field(default="twin-score-inputs-v1")
    round_id: str = Field(min_length=1)
    snp_f1: float
    indel_f1: float
    mean_recall: float
    total_truth: int = Field(ge=0)
    total_calls: int = Field(ge=0)
    fp_total: int = Field(ge=0)

    @field_validator("snp_f1", "indel_f1", "mean_recall")
    @classmethod
    def _rate(cls, v: float, info: Any) -> float:
        if not math.isfinite(v) or not (0.0 <= v <= 1.0):
            raise ValueError(f"{info.field_name} must be a finite rate in [0, 1]")
        return v

    def content_hash(self) -> str:
        return content_hash(self)


class ScoreComponents(BaseModel):
    """Score components — populated only when an authoritative scorer exists."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    core: float
    completeness: float
    fp: float
    quality: float


class TwinScoreResult(BaseModel):
    """Scoring outcome. Composite score is UNAVAILABLE until an authoritative
    AdvancedScorer formula is provided (Overall spec references but does not
    define it)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: str = Field(default="twin-score-result-v1")
    round_id: str = Field(min_length=1)
    status: AvailabilityStatus
    reason_code: ReasonCode | None = None
    scorer_identity: str | None = None
    score_inputs: ScoreInputs
    components: ScoreComponents | None = None
    final_score: float | None = None

    @model_validator(mode="after")
    def _coherent(self) -> TwinScoreResult:
        if self.status is AvailabilityStatus.AVAILABLE:
            if self.components is None or self.final_score is None:
                raise ValueError("AVAILABLE score must include components and final_score")
            if self.scorer_identity is None:
                raise ValueError("AVAILABLE score must name a scorer_identity")
        else:
            if self.reason_code is None:
                raise ValueError("UNAVAILABLE score must carry a reason_code")
            if self.components is not None or self.final_score is not None:
                raise ValueError("UNAVAILABLE score must not carry components/final_score")
        return self

    def content_hash(self) -> str:
        return content_hash(self)


# --------------------------------------------------------------------------- #
# Parity
# --------------------------------------------------------------------------- #


class ParityDifferenceKind(str, Enum):
    MISSING_EXPECTED = "MISSING_EXPECTED"
    UNEXPECTED = "UNEXPECTED"
    HASH_MISMATCH = "HASH_MISMATCH"
    VALUE_MISMATCH = "VALUE_MISMATCH"
    TOOL_VERSION_MISMATCH = "TOOL_VERSION_MISMATCH"
    PROTOCOL_VERSION_MISMATCH = "PROTOCOL_VERSION_MISMATCH"


class ParityDifference(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    field: str
    kind: ParityDifferenceKind
    expected: str | None = None
    observed: str | None = None


class ParityExpectation(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(min_length=1)
    expected_hash: str | None = None
    fields: dict[str, str] = Field(default_factory=dict)
    tool_version: str | None = None
    protocol_version: str | None = None


class ParityObservation(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(min_length=1)
    observed_hash: str | None = None
    fields: dict[str, str] = Field(default_factory=dict)
    tool_version: str | None = None
    protocol_version: str | None = None


class TwinParityReport(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: str = Field(default="twin-parity-report-v1")
    name: str = Field(min_length=1)
    declared_level: ParityLevel
    matched: bool
    differences: tuple[ParityDifference, ...] = ()
    created_at: str

    @field_validator("created_at")
    @classmethod
    def _ts(cls, v: str) -> str:
        if not is_iso8601_utc(v):
            raise ValueError("created_at must be timezone-aware ISO-8601")
        return v

    @model_validator(mode="after")
    def _match_consistency(self) -> TwinParityReport:
        if self.matched and self.differences:
            raise ValueError("a matched parity report cannot contain differences")
        if not self.matched and not self.differences:
            raise ValueError("a mismatched parity report must list differences")
        return self

    def content_hash(self) -> str:
        # created_at is operational metadata, excluded from content identity.
        return content_hash(self, exclude={"created_at"})


# --------------------------------------------------------------------------- #
# Run manifest
# --------------------------------------------------------------------------- #


class TwinRunManifest(BaseModel):
    """Immutable manifest of one Twin run. Required identities fail closed."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: str = Field(default="twin-run-manifest-v1")
    round_id: str = Field(min_length=1)
    region: Region
    engine_git_sha: str = Field(min_length=1)
    protocol_snapshot_hash: str
    parameter_space_hash: str
    config_hash: str
    plan_hash: str
    comparison_hash: str | None = None
    score_hash: str
    parity_hash: str | None = None
    scorer_status: AvailabilityStatus
    declared_parity_level: ParityLevel
    fixture_hash: str | None = None
    prerequisite_gate_hash: str
    created_at: str
    manifest_hash: str = Field(default="")

    @field_validator(
        "protocol_snapshot_hash",
        "parameter_space_hash",
        "config_hash",
        "plan_hash",
        "score_hash",
        "prerequisite_gate_hash",
    )
    @classmethod
    def _sha(cls, v: str, info: Any) -> str:
        return _require_sha(v, info.field_name)

    @field_validator("comparison_hash", "parity_hash", "fixture_hash")
    @classmethod
    def _opt_sha(cls, v: str | None, info: Any) -> str | None:
        return None if v is None else _require_sha(v, info.field_name)

    @field_validator("created_at")
    @classmethod
    def _ts(cls, v: str) -> str:
        if not is_iso8601_utc(v):
            raise ValueError("created_at must be timezone-aware ISO-8601")
        return v

    @model_validator(mode="after")
    def _identity(self) -> TwinRunManifest:
        expected = content_hash(self, exclude={"manifest_hash", "created_at"})
        if self.manifest_hash == "":
            object.__setattr__(self, "manifest_hash", expected)
        elif self.manifest_hash != expected:
            raise ValueError(f"manifest_hash does not match content (expected {expected})")
        return self
