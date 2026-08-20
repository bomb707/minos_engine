"""Typed error hierarchy for MINOS_ENGINE.

Errors are typed so that fail-closed behavior is explicit and testable. Never
raise a bare ``Exception`` or ``ValueError`` from domain code where one of these
applies; callers rely on the type to decide fallback behavior.
"""

from __future__ import annotations


class MinosEngineError(Exception):
    """Root of all engine errors."""


class ContractValidationError(MinosEngineError):
    """A contract/model failed validation (bad or missing required field)."""


class CanonicalizationError(MinosEngineError):
    """Value cannot be canonically serialized (e.g. NaN/Infinity, bad type)."""


class ConfigValidationError(MinosEngineError):
    """A GATK CONFIG failed validation (unknown key, wrong type, range, enum, coupling)."""


class ParameterSpaceError(MinosEngineError):
    """Parameter-space snapshot is incomplete, stale, or incompatible."""


class ProtocolError(MinosEngineError):
    """Base for protocol-layer failures."""


class SnapshotIncompleteError(ProtocolError):
    """A required protocol-snapshot identity is missing or empty (fail closed)."""


class StaleStateError(ProtocolError):
    """Cached protocol state is stale and the fallback policy does not permit it."""


class UnavailableError(ProtocolError):
    """A required external fact/endpoint is unavailable and must not be fabricated."""


class PolicyViolationError(MinosEngineError):
    """An action violates a binding policy (e.g. GATK-only caller policy)."""


class StageNotReadyError(MinosEngineError):
    """A stage was invoked before it is implemented/qualified."""


class GateError(MinosEngineError):
    """A stage-gate artifact is invalid or a mandatory check is false/missing."""


class ManifestError(MinosEngineError):
    """A release manifest is missing a required identity or has an empty one."""


class UnsupportedRuntimeError(MinosEngineError):
    """The running Python interpreter is not the supported runtime (CPython 3.12)."""


class TwinError(MinosEngineError):
    """Base for Validator Twin (Stage 1) failures."""


class ComparisonError(TwinError):
    """A comparison (hap.py-style) result is malformed, incomplete, or inconsistent."""


class ScoringError(TwinError):
    """A scoring input/result is invalid."""


class ParityError(TwinError):
    """A parity assessment is invalid or violates its declared level."""


class IngestionError(MinosEngineError):
    """Base for L2-D Layer 1 profile-ingestion failures (fail-closed admission)."""


class AttestationMismatchError(IngestionError):
    """Computed input identity does not match the registered identity (reject)."""


class AdmissionRejectedError(IngestionError):
    """A profile failed an admission rule (m5 MISMATCH, hash inequality, incomplete)."""


class FeatureHashConflictError(IngestionError):
    """The 4-way feature_values_hash equality (recomputed/JSON/artifact/DB) failed."""


class ContentConflictError(IngestionError):
    """Same ingestion_key resubmitted with DIFFERENT content (never overwritten)."""


class ProfileIdConflictError(IngestionError):
    """Same profile_id already accepted with a different identity or content."""


class EpochMembershipError(IngestionError):
    """The dataset is not a member of the requested split epoch's allocation set."""


class ArtifactMetadataConflictError(IngestionError):
    """An existing artifact row with this sha256 has conflicting size/media/kind."""


class MatrixConflictError(IngestionError):
    """A feature matrix with the same logical identity (snapshot, partition, feature
    set) exists with a DIFFERENT matrix_hash or artifact binding (never overwritten)."""


class MatrixAccessError(MinosEngineError):
    """The partition-scoped matrix artifact retrieval boundary rejected the request
    (unknown role, invisible matrix, path escape, wrong partition, or byte mismatch)."""
