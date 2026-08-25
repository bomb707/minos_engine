"""Foundational Layer 2 contracts (L2-A).

Every model is frozen and forbids extra fields. Identity strings are validated to
their exact shape (SHA-256 = 64 lowercase hex; git object id = 40 lowercase hex).
Only the *foundational* contracts needed by later stages are defined here — no
candidate generation, model, storage, or controller behavior. ``select_config``
remains blocked in :mod:`minos_engine.layer2.service`.

Breaking change (L2-A): the Stage-0 ``DecisionRequest``/``DecisionResult`` opaque
``dict[str, Any]`` payloads are replaced by typed foundational contracts
(:class:`RoundIdentity`, :class:`Layer1ProfileReference`,
:class:`ParameterSpaceIdentity`, :class:`ArtifactIdentity`,
:class:`ComputeLimits`, :class:`DecisionIdentity`, :class:`FallbackReason`). No
production code constructs these yet (the service is blocked); the acceptance
tests are updated in the same commit.
"""

from __future__ import annotations

import math
import re
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from minos_engine.common.hashing import canonical_hash

__all__ = [
    "ControlMode",
    "FallbackReason",
    "DatasetPartition",
    "FeatureEligibilityState",
    "FeatureValueKind",
    "ArtifactIdentity",
    "AcceptedPrerequisiteIdentity",
    "Layer1ProfileReference",
    "RoundIdentity",
    "ParameterSpaceIdentity",
    "ComputeLimits",
    "DecisionIdentity",
    "FeatureRegistryRecord",
    "PromotionRecord",
    "CanonicalFeatureVector",
    "DecisionRequest",
    "DecisionResult",
]

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_GIT_OID_RE = re.compile(r"^[0-9a-f]{40}$")


def _sha256(v: str) -> str:
    if not _SHA256_RE.match(v):
        raise ValueError("must be 64 lowercase hexadecimal characters (SHA-256)")
    return v


def _git_oid(v: str) -> str:
    if not _GIT_OID_RE.match(v):
        raise ValueError("must be a 40-character lowercase hex git object id")
    return v


def _finite_nonneg(v: float) -> float:
    if not math.isfinite(v):
        raise ValueError("value must be finite (no NaN/Infinity)")
    if v < 0:
        raise ValueError("value must be non-negative")
    return v


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


# --------------------------------------------------------------------------- #
# Enumerations
# --------------------------------------------------------------------------- #
class ControlMode(str, Enum):
    SAFE_BASELINE = "SAFE_BASELINE"
    BOUNDED = "BOUNDED"
    FULL_CONTEXTUAL = "FULL_CONTEXTUAL"
    REFINEMENT = "REFINEMENT"


class FallbackReason(str, Enum):
    NONE = "NONE"
    SAFE_BASELINE_FORCED = "SAFE_BASELINE_FORCED"
    BASELINE_GATE_FAILED = "BASELINE_GATE_FAILED"
    DEADLINE_EXCEEDED = "DEADLINE_EXCEEDED"
    LOW_CONFIDENCE = "LOW_CONFIDENCE"
    GUARDRAIL_TRIGGERED = "GUARDRAIL_TRIGGERED"
    MISSING_IDENTITY = "MISSING_IDENTITY"
    STAGE_NOT_READY = "STAGE_NOT_READY"


class DatasetPartition(str, Enum):
    TRAIN = "train"
    VALIDATION = "validation"
    TEST = "test"


class FeatureEligibilityState(str, Enum):
    ELIGIBLE = "ELIGIBLE"
    CONDITIONAL = "CONDITIONAL"
    RESEARCH_ONLY = "RESEARCH_ONLY"
    FORBIDDEN = "FORBIDDEN"


class FeatureValueKind(str, Enum):
    """The value shape of a registry record.

    ``FRACTION``/``REAL``/``COUNT`` are scalar numeric leaves (candidate model
    features). ``BOOL``/``CATEGORICAL``/``IDENTIFIER``/``OPERATIONAL`` are scalar
    non-numeric leaves that are never model features. ``CONTAINER`` is a structured
    map/array documented for completeness and never a model feature.
    """

    FRACTION = "FRACTION"  # finite float in [0, 1]
    REAL = "REAL"  # finite float (phred, depth, insert bp, entropy, quantile leaf, density)
    COUNT = "COUNT"  # non-negative integer magnitude
    BOOL = "BOOL"
    CATEGORICAL = "CATEGORICAL"
    IDENTIFIER = "IDENTIFIER"
    OPERATIONAL = "OPERATIONAL"
    CONTAINER = "CONTAINER"


_NUMERIC_KINDS = frozenset(
    {FeatureValueKind.FRACTION, FeatureValueKind.REAL, FeatureValueKind.COUNT}
)


# --------------------------------------------------------------------------- #
# Identity contracts
# --------------------------------------------------------------------------- #
class ArtifactIdentity(_Frozen):
    """A stored artifact referenced by URI + content SHA-256 (never bytes)."""

    uri: str = Field(min_length=1)
    sha256: str

    @field_validator("sha256")
    @classmethod
    def _v(cls, v: str) -> str:
        return _sha256(v)


class AcceptedPrerequisiteIdentity(_Frozen):
    """Repository-owned accepted Layer 1 prerequisite identity (see prerequisites.py).

    Gate hashes and Layer 1 identity hashes are SHA-256 (64 hex); git commit/tree
    object ids are 40 hex. This is the *only* shape that carries accepted
    identities into the entry-gate verifier; callers cannot supply their own.
    """

    l1_gate_hash: str
    protocol_gate_hash: str
    twin_gate_hash: str
    layer1_schema_hash: str
    profiler_config_hash: str
    profiler_version: str = Field(min_length=1)
    qualified_source_commit: str
    qualified_source_tree: str
    artifact_commit: str
    artifact_tree: str
    v2_framework_commit: str
    v2_evidence_commit: str
    owner_commit: str

    @field_validator(
        "l1_gate_hash",
        "protocol_gate_hash",
        "twin_gate_hash",
        "layer1_schema_hash",
        "profiler_config_hash",
    )
    @classmethod
    def _v_sha(cls, v: str) -> str:
        return _sha256(v)

    @field_validator(
        "qualified_source_commit",
        "qualified_source_tree",
        "artifact_commit",
        "artifact_tree",
        "v2_framework_commit",
        "v2_evidence_commit",
        "owner_commit",
    )
    @classmethod
    def _v_oid(cls, v: str) -> str:
        return _git_oid(v)


class Layer1ProfileReference(_Frozen):
    """Reference to a Layer 1 profile by identity only — never the profile bytes.

    Production-ready contract: **all** identities are mandatory and fail closed
    when missing, empty, uppercase, or malformed. Layer 2 consumes profiles through
    this typed reference (identity + input content hashes); it does not open
    BAM/BAI/reference files. Truth, mutations, and scores are never referenced. A
    future degraded/reference-less profile needs its own separately-typed contract;
    this one is never weakened. ``identity_tuple_hash`` is derived canonically from
    the (bam, bai, reference, fai, region) tuple and binds the exact input identity.
    """

    profile_id: str = Field(min_length=1)
    profile_manifest_hash: str
    fingerprint_hash: str
    region_hash: str
    bam_sha256: str
    bai_sha256: str
    reference_sha256: str
    fai_sha256: str
    identity_tuple_hash: str = ""

    @field_validator(
        "profile_manifest_hash",
        "fingerprint_hash",
        "region_hash",
        "bam_sha256",
        "bai_sha256",
        "reference_sha256",
        "fai_sha256",
    )
    @classmethod
    def _v_req(cls, v: str) -> str:
        return _sha256(v)

    @model_validator(mode="after")
    def _bind_tuple(self) -> Layer1ProfileReference:
        expected = canonical_hash(
            {
                "bam_sha256": self.bam_sha256,
                "bai_sha256": self.bai_sha256,
                "reference_sha256": self.reference_sha256,
                "fai_sha256": self.fai_sha256,
                "region_hash": self.region_hash,
            }
        )
        if self.identity_tuple_hash == "":
            object.__setattr__(self, "identity_tuple_hash", expected)
        elif self.identity_tuple_hash != expected:
            raise ValueError("identity_tuple_hash does not match canonical identity tuple")
        return self


class RoundIdentity(_Frozen):
    """Round join key. ``round_id`` is an identity, never a model feature."""

    round_id: str = Field(min_length=1)


class ParameterSpaceIdentity(_Frozen):
    """Bound GATK parameter-space identity. Only the GATK caller is permitted."""

    parameter_space_hash: str
    caller: str = "gatk"

    @field_validator("parameter_space_hash")
    @classmethod
    def _v(cls, v: str) -> str:
        return _sha256(v)

    @field_validator("caller")
    @classmethod
    def _gatk_only(cls, v: str) -> str:
        if v != "gatk":
            raise ValueError("parameter space caller must be 'gatk' (GATK-only policy)")
        return v


class ComputeLimits(_Frozen):
    """Compute/deadline budget for a live decision (fail-closed downstream)."""

    remaining_seconds: float = Field(ge=0)
    wall_clock_budget_seconds: float = Field(gt=0)
    cpu_limit: int = Field(ge=1)
    memory_limit_bytes: int = Field(gt=0)

    @field_validator("remaining_seconds", "wall_clock_budget_seconds")
    @classmethod
    def _finite(cls, v: float) -> float:
        return _finite_nonneg(v)


class DecisionIdentity(_Frozen):
    """Identity of a produced decision (append-only decision manifest binding)."""

    decision_id: str = Field(min_length=1)
    config_hash: str
    decision_manifest_hash: str

    @field_validator("config_hash", "decision_manifest_hash")
    @classmethod
    def _v(cls, v: str) -> str:
        return _sha256(v)


# --------------------------------------------------------------------------- #
# Feature eligibility contracts
# --------------------------------------------------------------------------- #
class FeatureRegistryRecord(_Frozen):
    """One Layer 1 field's eligibility classification (code-owned, canonical).

    Invariants:
      * ``truth_derived`` may be True only for a FORBIDDEN field;
      * ``model_feature`` is True only for a scalar numeric leaf
        (FRACTION/REAL/COUNT) whose state is ELIGIBLE/CONDITIONAL/RESEARCH_ONLY —
        containers, identifiers, operational and categorical leaves are never
        model features;
      * ``production_allowed`` is True only for an ELIGIBLE scalar model feature.
    """

    field_path: str = Field(min_length=1)
    state: FeatureEligibilityState
    family: str = Field(min_length=1)
    value_kind: FeatureValueKind
    model_feature: bool
    source_schema: str = Field(min_length=1)
    source_schema_hash: str
    config_bound: bool = False
    truth_derived: bool = False

    @field_validator("source_schema_hash")
    @classmethod
    def _v_sha(cls, v: str) -> str:
        return _sha256(v)

    @model_validator(mode="after")
    def _invariants(self) -> FeatureRegistryRecord:
        if self.truth_derived and self.state is not FeatureEligibilityState.FORBIDDEN:
            raise ValueError("a truth-derived field must be FORBIDDEN, never eligible")
        eligible_state = self.state in (
            FeatureEligibilityState.ELIGIBLE,
            FeatureEligibilityState.CONDITIONAL,
            FeatureEligibilityState.RESEARCH_ONLY,
        )
        expect_feature = eligible_state and self.value_kind in _NUMERIC_KINDS
        if self.model_feature != expect_feature:
            raise ValueError(
                "model_feature must be True iff a scalar numeric leaf in an "
                "eligible/conditional/research state; containers are never features"
            )
        if self.value_kind is FeatureValueKind.CONTAINER and self.model_feature:
            raise ValueError("a container can never be a scalar model feature")
        return self

    @property
    def production_allowed(self) -> bool:
        return self.state is FeatureEligibilityState.ELIGIBLE and self.model_feature


class PromotionRecord(_Frozen):
    """Descriptive future-stage contract. **Authorizes nothing in L2-A.**

    A caller-constructed ``PromotionRecord`` must never authorize a CONDITIONAL or
    RESEARCH_ONLY feature. A real promotion (a future stage) requires a
    repository-owned, hash-bound, git-bound accepted promotion *artifact* binding
    field path, previous/new states, registry hash, split-manifest hash, training
    and validation evidence hashes, qualification report, accepted commit/tree, and
    explicit authorization — test-set evidence may never authorize a
    promotion. This descriptive object carries none of that authority.
    """

    field_path: str = Field(min_length=1)
    to_state: FeatureEligibilityState = FeatureEligibilityState.ELIGIBLE
    evidence_partition: DatasetPartition
    justification: str = Field(min_length=1)
    approved_by: str = Field(min_length=1)

    @model_validator(mode="after")
    def _no_test_evidence(self) -> PromotionRecord:
        if self.to_state is not FeatureEligibilityState.ELIGIBLE:
            raise ValueError("a promotion must target ELIGIBLE")
        if self.evidence_partition is DatasetPartition.TEST:
            raise ValueError("promotion cannot be justified by test-set results")
        return self


class CanonicalFeatureVector(_Frozen):
    """A frozen, deterministic, canonically-hashable production feature vector.

    Direct construction cannot produce an invalid vector that would bypass
    :func:`~minos_engine.layer2.feature_registry.validate_production_feature_mapping`:
    every value is a finite, non-bool number; fields are sorted and unique; fields
    and values have equal length; the registry hash has a valid shape; and the
    vector hash binds the validated canonical content. Field-specific
    COUNT/FRACTION semantics live at the registry boundary (they require the
    registry classification and would be a circular dependency here); this generic
    contract enforces the representation-level invariants only.
    """

    fields: tuple[str, ...]
    values: tuple[float, ...]
    registry_hash: str
    vector_hash: str = ""

    @field_validator("values", mode="before")
    @classmethod
    def _v_values(cls, v: object) -> tuple[float, ...]:
        if not isinstance(v, (list, tuple)):
            raise ValueError("values must be a sequence of numbers")
        out: list[float] = []
        for x in v:
            if isinstance(x, bool):
                raise ValueError("bool is not a valid feature value")
            if not isinstance(x, (int, float)):
                raise ValueError("feature value must be a built-in int or float")
            fx = float(x)
            if not math.isfinite(fx):
                raise ValueError("feature value must be finite (no NaN/Infinity)")
            out.append(fx)
        return tuple(out)

    @field_validator("registry_hash")
    @classmethod
    def _v_sha(cls, v: str) -> str:
        return _sha256(v)

    @model_validator(mode="after")
    def _bind(self) -> CanonicalFeatureVector:
        if len(self.fields) != len(self.values):
            raise ValueError("fields and values length mismatch")
        if list(self.fields) != sorted(self.fields):
            raise ValueError("fields must be in deterministic (sorted) order")
        if len(set(self.fields)) != len(self.fields):
            raise ValueError("duplicate feature field in vector")
        expected = canonical_hash(
            {
                "fields": list(self.fields),
                "values": list(self.values),
                "registry_hash": self.registry_hash,
            }
        )
        if self.vector_hash == "":
            object.__setattr__(self, "vector_hash", expected)
        elif self.vector_hash != expected:
            raise ValueError("vector_hash does not match canonical content")
        return self


# --------------------------------------------------------------------------- #
# Decision request / result (typed; consumed only once select_config unblocks)
# --------------------------------------------------------------------------- #
class DecisionRequest(_Frozen):
    round: RoundIdentity
    profile_ref: Layer1ProfileReference
    parameter_space: ParameterSpaceIdentity
    safe_baseline: ArtifactIdentity
    controller_version: str = Field(min_length=1)
    limits: ComputeLimits
    model_bundle_id: str | None = None
    requested_mode: ControlMode = ControlMode.SAFE_BASELINE


class DecisionResult(_Frozen):
    decision: DecisionIdentity
    mode: ControlMode
    selected_config: ArtifactIdentity
    fallback_reason: FallbackReason = FallbackReason.NONE
