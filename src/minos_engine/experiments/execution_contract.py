"""L2-F F5 pure execution contracts (no I/O, no database, no subprocess).

Frozen, strictly-validated contracts for one GATK HaplotypeCaller execution of one accepted
logical job, plus the frozen ``result_hash`` formula. This module is a PURE domain layer: it
performs no filesystem, database, network or process work, and it never references truth VCF/BED,
mutation files, hap.py output, TP/FP/FN, scores, labels or leaderboard data.

Frozen result identity
----------------------
``result_hash = sha256(RESULT_HASH_DOMAIN_bytes + canonical_json(scientific_result_content))``

The preimage binds ONLY reproducible science: the schema version, the plan and job identity, the
member identity, the CONFIG identity, the exact input byte identities, the region, the logical
argv, the GATK executable identity and version, and the produced VCF digest and size. Host paths,
UUIDs, timestamps, runtime and worker identity are deliberately EXCLUDED, so the same job executed
by a different worker on a different host at a different time yields the same ``result_hash``.
The canonical result-manifest bytes may additionally carry the job UUID, runtime, worker id, a
generated timestamp and the ``result_hash`` itself; the manifest artifact's own SHA-256 is
therefore a separate value from ``result_hash``.
"""

from __future__ import annotations

import re
from typing import Annotated, Any, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, Field

from minos_engine.common.canonical_json import canonical_json_bytes
from minos_engine.common.errors import MinosEngineError
from minos_engine.common.hashing import sha256_hex

__all__ = [
    "EXECUTION_RESULT_SCHEMA",
    "RESULT_HASH_DOMAIN",
    "INPUT_IDENTITY_DOMAIN",
    "LOGICAL_ARGV_DOMAIN",
    "GATK_RUNTIME_BUNDLE_DOMAIN",
    "ARGV_REFERENCE_PLACEHOLDER",
    "ARGV_BAM_PLACEHOLDER",
    "ARGV_OUTPUT_PLACEHOLDER",
    "FailureCode",
    "L2FExecutionError",
    "InputResolutionError",
    "ConfigArtifactError",
    "GatkInvocationError",
    "GatkExecutionError",
    "GatkTimeoutError",
    "GatkOutputError",
    "ExecutionInput",
    "ExecutionConfig",
    "LogicalGatkInvocation",
    "GatkExecutionOutcome",
    "ExecutionResultManifest",
    "ExecutionFailure",
    "execution_input_from_manifest",
    "compute_input_identity_hash",
    "compute_logical_argv_hash",
    "compute_gatk_runtime_bundle_sha256",
    "compute_result_hash",
    "build_result_manifest_bytes",
]

EXECUTION_RESULT_SCHEMA = "l2f-gatk-execution-result-v1"
#: domain-separation prefixes prepended (as bytes) before the canonical-JSON preimage.
RESULT_HASH_DOMAIN = "minos:l2f-gatk-execution-result:v1\n"
INPUT_IDENTITY_DOMAIN = "minos:l2f-execution-input:v1\n"
LOGICAL_ARGV_DOMAIN = "minos:l2f-logical-argv:v1\n"
#: domain for the GATK *execution bundle* identity: the launcher alone is a ~21 KB dispatcher, so
#: the scientific payload (the local JAR it actually runs) must be bound too.
GATK_RUNTIME_BUNDLE_DOMAIN = "minos:l2f-gatk-runtime-bundle:v1\n"

#: stable path placeholders so the logical argv hash is independent of the host filesystem.
ARGV_REFERENCE_PLACEHOLDER = "<reference.fa>"
ARGV_BAM_PLACEHOLDER = "<input.bam>"
ARGV_OUTPUT_PLACEHOLDER = "<output.vcf>"

_HEX64 = re.compile(r"^[0-9a-f]{64}$")


def _validate_hex64(v: str) -> str:
    if not _HEX64.fullmatch(v):
        raise ValueError("must be a lowercase 64-character hex string")
    return v


Hex64 = Annotated[str, AfterValidator(_validate_hex64)]
_STRICT = ConfigDict(frozen=True, extra="forbid", strict=True)

FailureCode = Literal[
    "PREPARATION_FAILED",
    "GATK_NONZERO_EXIT",
    "GATK_TIMEOUT",
    "GATK_OUTPUT_INVALID",
    "GATK_OUTPUT_MISSING",
    "EXECUTION_ERROR",
]


class L2FExecutionError(MinosEngineError):
    """Base error for L2-F F5 execution."""


class InputResolutionError(L2FExecutionError):
    """A required accepted input was missing, ambiguous, substituted or changed."""


class ConfigArtifactError(L2FExecutionError):
    """The persisted CONFIG artifact bytes are not the accepted canonical CONFIG."""


class GatkInvocationError(L2FExecutionError):
    """The logical GATK invocation could not be built or verified."""


class GatkExecutionError(L2FExecutionError):
    """GATK exited nonzero, or the runner could not execute it."""


class GatkTimeoutError(L2FExecutionError):
    """GATK exceeded its wall-clock budget and its process group was terminated."""


class GatkOutputError(L2FExecutionError):
    """The produced output is missing, misplaced or not a valid single-sample VCF."""


class ExecutionInput(BaseModel):
    """The complete, byte-verified accepted input identity for one train member."""

    model_config = _STRICT

    dataset_id: str = Field(min_length=1)
    round_id: str = Field(min_length=1)
    chromosome: str = Field(min_length=1)
    profile_id: str = Field(min_length=1)
    content_hash: Hex64
    feature_values_hash: Hex64
    bam_sha256: Hex64
    bai_sha256: Hex64
    reference_sha256: Hex64
    fai_sha256: Hex64
    #: execution provenance only — NEVER part of the frozen dataset identity.
    dictionary_sha256: Hex64
    bam_size_bytes: int = Field(ge=0)
    region_hash: Hex64
    region_start0: int = Field(ge=0)
    region_end0_exclusive: int = Field(ge=0)

    def identity_hash(self) -> str:
        return compute_input_identity_hash(self)


class ExecutionConfig(BaseModel):
    """The validated accepted CONFIG payload for one candidate."""

    model_config = _STRICT

    config_hash: Hex64
    parameter_space_hash: Hex64
    config_index: int = Field(ge=0)
    effective_config: dict[str, Any]


class LogicalGatkInvocation(BaseModel):
    """The host-independent logical GATK invocation (tokenized argv, never a shell string)."""

    model_config = _STRICT

    tool: Literal["HaplotypeCaller"]
    region_token: str = Field(min_length=1)
    #: argv with stable path placeholders substituted for host paths.
    logical_argv: tuple[str, ...] = Field(min_length=1)
    #: SHA-256 of the GATK launcher script alone (a dispatcher, NOT the scientific payload).
    gatk_executable_sha256: Hex64
    #: SHA-256 over launcher + the local JAR the launcher actually runs + the version. This is
    #: the value that makes the execution identity depend on the real GATK implementation.
    gatk_runtime_bundle_sha256: Hex64
    gatk_version: str = Field(min_length=1)

    def argv_hash(self) -> str:
        return compute_logical_argv_hash(self)


class GatkExecutionOutcome(BaseModel):
    """The byte-verified outcome of one GATK process (validated from the produced bytes)."""

    model_config = _STRICT

    exit_code: int
    runtime_ms: int = Field(ge=0)
    vcf_sha256: Hex64
    vcf_size_bytes: int = Field(gt=0)
    stderr_sha256: Hex64 | None = None


class ExecutionResultManifest(BaseModel):
    """The canonical execution-result manifest (its own artifact bytes)."""

    model_config = _STRICT

    schema_version: Literal["l2f-gatk-execution-result-v1"]
    plan_hash: Hex64
    job_id: str = Field(min_length=1)
    job_key: Hex64
    dataset_id: str = Field(min_length=1)
    round_id: str = Field(min_length=1)
    profile_id: str = Field(min_length=1)
    content_hash: Hex64
    feature_values_hash: Hex64
    config_hash: Hex64
    parameter_space_hash: Hex64
    input_identity_hash: Hex64
    #: the COMPLETE raw input identity, so an independent verifier can reconstruct a strict
    #: :class:`ExecutionInput` and recompute BOTH ``input_identity_hash`` and ``result_hash``
    #: from the manifest bytes alone. ``dictionary_sha256`` and ``bam_size_bytes`` are execution
    #: provenance: they enter ``input_identity_hash`` but NOT the frozen ``result_hash``.
    bam_sha256: Hex64
    bai_sha256: Hex64
    reference_sha256: Hex64
    fai_sha256: Hex64
    dictionary_sha256: Hex64
    bam_size_bytes: int = Field(ge=0)
    region_hash: Hex64
    region_start0: int = Field(ge=0)
    region_end0_exclusive: int = Field(ge=0)
    chromosome: str = Field(min_length=1)
    logical_argv_hash: Hex64
    gatk_executable_sha256: Hex64
    gatk_runtime_bundle_sha256: Hex64
    gatk_version: str = Field(min_length=1)
    vcf_sha256: Hex64
    vcf_size_bytes: int = Field(gt=0)
    result_hash: Hex64
    runtime_ms: int = Field(ge=0)
    worker_id: str = Field(min_length=1)
    generated_at: str = Field(min_length=1)


class ExecutionFailure(BaseModel):
    """A bounded, non-scientific failure record (no free-text reason, no stderr bytes)."""

    model_config = _STRICT

    failure_code: FailureCode
    exit_code: int | None = None
    stderr_sha256: Hex64 | None = None


def execution_input_from_manifest(manifest: ExecutionResultManifest) -> ExecutionInput:
    """Reconstruct the STRICT :class:`ExecutionInput` from the manifest's own fields.

    Every component of ``input_identity_hash`` is carried by the manifest, so an independent
    verifier can rebuild the validated contract object and recompute the identity itself instead
    of trusting either the manifest's or the database row's stored hash.
    """
    return ExecutionInput(
        dataset_id=manifest.dataset_id,
        round_id=manifest.round_id,
        chromosome=manifest.chromosome,
        profile_id=manifest.profile_id,
        content_hash=manifest.content_hash,
        feature_values_hash=manifest.feature_values_hash,
        bam_sha256=manifest.bam_sha256,
        bai_sha256=manifest.bai_sha256,
        reference_sha256=manifest.reference_sha256,
        fai_sha256=manifest.fai_sha256,
        dictionary_sha256=manifest.dictionary_sha256,
        bam_size_bytes=manifest.bam_size_bytes,
        region_hash=manifest.region_hash,
        region_start0=manifest.region_start0,
        region_end0_exclusive=manifest.region_end0_exclusive,
    )


def compute_input_identity_hash(inputs: ExecutionInput) -> str:
    """Domain-separated identity of every accepted input byte-stream + region."""
    content = {
        "dataset_id": inputs.dataset_id,
        "profile_id": inputs.profile_id,
        "content_hash": inputs.content_hash,
        "feature_values_hash": inputs.feature_values_hash,
        "bam_sha256": inputs.bam_sha256,
        "bai_sha256": inputs.bai_sha256,
        "reference_sha256": inputs.reference_sha256,
        "fai_sha256": inputs.fai_sha256,
        "dictionary_sha256": inputs.dictionary_sha256,
        "bam_size_bytes": inputs.bam_size_bytes,
        "region_hash": inputs.region_hash,
        "region_start0": inputs.region_start0,
        "region_end0_exclusive": inputs.region_end0_exclusive,
        "chromosome": inputs.chromosome,
    }
    return sha256_hex(INPUT_IDENTITY_DOMAIN.encode("utf-8") + canonical_json_bytes(content))


def compute_logical_argv_hash(invocation: LogicalGatkInvocation) -> str:
    """Domain-separated identity of the host-independent tokenized invocation."""
    content = {
        "tool": invocation.tool,
        "region_token": invocation.region_token,
        "logical_argv": list(invocation.logical_argv),
        "gatk_executable_sha256": invocation.gatk_executable_sha256,
        "gatk_version": invocation.gatk_version,
    }
    return sha256_hex(LOGICAL_ARGV_DOMAIN.encode("utf-8") + canonical_json_bytes(content))


def compute_gatk_runtime_bundle_sha256(
    *, launcher_sha256: str, local_jar_sha256: str, gatk_version: str
) -> str:
    """The deterministic, HOST-INDEPENDENT GATK execution-bundle identity.

    Binds the launcher bytes, the local JAR bytes the launcher actually executes, and the version.
    Absolute paths, uid/gid, timestamps and hostnames are deliberately excluded, so the digest is
    reproducible on any host that has the same official GATK bundle.
    """
    content = {
        "launcher_sha256": launcher_sha256,
        "local_jar_sha256": local_jar_sha256,
        "gatk_version": gatk_version,
    }
    return sha256_hex(GATK_RUNTIME_BUNDLE_DOMAIN.encode("utf-8") + canonical_json_bytes(content))


def compute_result_hash(
    *,
    plan_hash: str,
    job_key: str,
    inputs: ExecutionInput,
    config: ExecutionConfig,
    invocation: LogicalGatkInvocation,
    outcome: GatkExecutionOutcome,
) -> str:
    """THE frozen L2-F execution-result identity.

    Binds only reproducible science; excludes host paths, UUIDs, timestamps, runtime and
    worker identity.
    """
    content = {
        "schema_version": EXECUTION_RESULT_SCHEMA,
        "plan_hash": plan_hash,
        "job_key": job_key,
        "dataset_id": inputs.dataset_id,
        "profile_id": inputs.profile_id,
        "content_hash": inputs.content_hash,
        "feature_values_hash": inputs.feature_values_hash,
        "config_hash": config.config_hash,
        "parameter_space_hash": config.parameter_space_hash,
        "bam_sha256": inputs.bam_sha256,
        "bai_sha256": inputs.bai_sha256,
        "reference_sha256": inputs.reference_sha256,
        "fai_sha256": inputs.fai_sha256,
        "region_hash": inputs.region_hash,
        "region_start0": inputs.region_start0,
        "region_end0_exclusive": inputs.region_end0_exclusive,
        "chromosome": inputs.chromosome,
        "logical_argv_hash": invocation.argv_hash(),
        "gatk_executable_sha256": invocation.gatk_executable_sha256,
        # the scientific payload: a different local JAR CANNOT reproduce this result identity.
        "gatk_runtime_bundle_sha256": invocation.gatk_runtime_bundle_sha256,
        "gatk_version": invocation.gatk_version,
        "vcf_sha256": outcome.vcf_sha256,
        "vcf_size_bytes": outcome.vcf_size_bytes,
    }
    return sha256_hex(RESULT_HASH_DOMAIN.encode("utf-8") + canonical_json_bytes(content))


def build_result_manifest_bytes(manifest: ExecutionResultManifest) -> bytes:
    """The exact canonical result-manifest artifact bytes."""
    return canonical_json_bytes(manifest.model_dump(mode="json"))
