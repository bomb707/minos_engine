"""Frozen L2-F F7 HARNESS-READY qualification contract (pure; no I/O, no database).

This module defines *what a HARNESS-READY qualification must bind* and nothing else. It performs
no filesystem, database, network or process work, and it never references truth VCF/BED, mutation
manifests, hap.py output, TP/FP/FN, scores, labels, leaderboard data or training targets.

HARNESS-READY proves exactly four things, each of which must be an OBSERVATION rather than a
caller assertion:

1. official GATK execution and GATK/Twin invocation parity;
2. idempotent resume;
3. independent artifact-hash verification;
4. complete typed failure classification.

The qualification result is immutable and strictly validated (``extra="forbid"``, strict types),
its canonical serialization is deterministic, and its JSON loader rejects duplicate keys and
unknown fields. Nothing here decides promotion: assembling a gate is a separate, later step, and
the F7-A source commit deliberately ships **no** ``gates/harness-ready.json``.
"""

from __future__ import annotations

import json
import re
from typing import Annotated, Any, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, Field

from minos_engine.common.canonical_json import canonical_json_bytes
from minos_engine.common.errors import MinosEngineError
from minos_engine.common.hashing import sha256_hex

__all__ = [
    "HARNESS_READY_GATE",
    "HARNESS_READY_GATE_PATH",
    "HARNESS_READY_QUALIFIER_SCHEMA",
    "HARNESS_READY_QUALIFIER_VERSION",
    "HARNESS_READY_QUALIFICATION_DOMAIN",
    "ACCEPTED_F6_CORRECTIVE_COMMIT",
    "ACCEPTED_E5_GATES",
    "ACCEPTED_MIGRATION_SHAS",
    "ACCEPTED_F5_CONTRACT_HASH",
    "ACCEPTED_PARAMETER_SPACE_HASH",
    "ACCEPTED_CANDIDATE_SET_HASH",
    "ACCEPTED_CANDIDATE_COUNT",
    "ACCEPTED_PLAN_HASH",
    "ACCEPTED_LOGICAL_JOB_COUNT",
    "ACCEPTED_LIVE_GATK_SOURCE_ARTIFACT_SHA256",
    "ACCEPTED_LIVE_GATK_PARAMETER_SPACE_ARTIFACT_SHA256",
    "ACCEPTED_POLICY_HASH",
    "ACCEPTED_E5_GATE_HASHES",
    "ACCEPTED_ALEMBIC_HEAD",
    "HarnessReadyContractError",
    "SourceProvenance",
    "AcceptedIdentities",
    "GatkBinaryIdentity",
    "QualificationInputIdentity",
    "OfficialExecutionResult",
    "ParityDifference",
    "TwinParityResult",
    "ResumeResult",
    "ArtifactVerificationResult",
    "FailureClassificationEntry",
    "FailureClassificationInventory",
    "BoundaryResult",
    "HarnessReadyQualification",
    "canonical_qualification_bytes",
    "compute_qualification_hash",
    "load_qualification_json",
]

HARNESS_READY_GATE = "HARNESS-READY"
HARNESS_READY_GATE_PATH = "gates/harness-ready.json"

#: versioned F7 qualifier identity (bumping this changes every qualification hash).
HARNESS_READY_QUALIFIER_SCHEMA = "l2f-harness-ready-qualification-v1"
HARNESS_READY_QUALIFIER_VERSION = "f7a-1"
#: domain separation for the qualification identity hash.
HARNESS_READY_QUALIFICATION_DOMAIN = "minos:l2f-harness-ready-qualification:v1\n"

#: the accepted F6 corrective commit every F7 qualification must descend from.
ACCEPTED_F6_CORRECTIVE_COMMIT = "695d9227ed83c595e3ed03375a935fbe801aadbd"

#: the accepted L2-E gates whose identities a qualification must bind.
ACCEPTED_E5_GATES: tuple[str, ...] = ("FEATURE-VIEW-READY", "FEATURE-MATRIX-FROZEN-1")

#: byte SHA-256 of every accepted L2-F migration (0001-0005 predate L2-F and are unchanged).
ACCEPTED_MIGRATION_SHAS: dict[str, str] = {
    "migrations/versions/0006_l2f_experiment_plan.py": (
        "1eb3a12b502a5f247a2dc662642fd71931dcada815923e95d18504220445c3c6"
    ),
    "migrations/versions/0007_l2f_job_claiming.py": (
        "bc247e0a68f82ad6e52868e115db3f1e237b637def98567c596e3cc0a4e42625"
    ),
    "migrations/versions/0008_l2f_execution_results.py": (
        "95614d67fbfbafb735a0651275dd06f1949ae513b43b96b3776a5a90c436f3ff"
    ),
}

ACCEPTED_F5_CONTRACT_HASH = "8b7d8e8961934f46d295646b4bc049bf118ba352c644d6e5d4d5d256dd201bdc"
ACCEPTED_PARAMETER_SPACE_HASH = "b2d401918084d64023305d9262baf5011a89fe517bee4e0bd33af79fb14aee2e"
ACCEPTED_CANDIDATE_SET_HASH = "50d5f36918758de204e4b34cdd3fc8560a14debfcdb25869f713690c6085057d"
ACCEPTED_CANDIDATE_COUNT = 39
ACCEPTED_PLAN_HASH = "eb8de84db2e35074957ed2f812cbb4f9495195cadb99563780d00d3cfe2b5d0a"
ACCEPTED_LOGICAL_JOB_COUNT = 1950

#: byte SHA-256 of the two committed live-GATK artifacts. These are CHECKED, not merely stored.
ACCEPTED_LIVE_GATK_SOURCE_ARTIFACT_SHA256 = (
    "14b97a52ee82e05ed49606cdf9d41ae52ab9ce7a267be62c50b63e38f7498a94"
)
ACCEPTED_LIVE_GATK_PARAMETER_SPACE_ARTIFACT_SHA256 = (
    "b6a65cebbf120e24b409499fb95b28b80e2eb2c7404131133d1d17bf7172f6cd"
)
#: the accepted experiment-parameter policy identity bound by the candidate set.
ACCEPTED_POLICY_HASH = "a40321a676422121460bb110250812eacc8f1e203e788d244c661ec7c854daed"
#: the accepted L2-E gate hashes, recomputed from the committed gate artifacts.
ACCEPTED_E5_GATE_HASHES: dict[str, str] = {
    "FEATURE-VIEW-READY": "c0ff49856689c994499dd3a7c04d7a1fb8ba0992b2eb1e099672bf828d515234",
    "FEATURE-MATRIX-FROZEN-1": "cd34bdf96f3e7853039b2719e74a12a95740904c1b15f2f5c747516e0260d3ef",
}
ACCEPTED_ALEMBIC_HEAD = "0008_l2f_execution_results"

_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_HEX40 = re.compile(r"^[0-9a-f]{40}$")


class HarnessReadyContractError(MinosEngineError):
    """The HARNESS-READY qualification contract was violated."""


def _hex64(v: str) -> str:
    if not _HEX64.fullmatch(v):
        raise ValueError("must be a lowercase 64-character hex digest")
    return v


def _hex40(v: str) -> str:
    if not _HEX40.fullmatch(v):
        raise ValueError("must be a lowercase 40-character git object id")
    return v


Hex64 = Annotated[str, AfterValidator(_hex64)]
GitSha = Annotated[str, AfterValidator(_hex40)]

_STRICT = ConfigDict(frozen=True, extra="forbid", strict=True)


class SourceProvenance(BaseModel):
    """The exact source commit/tree a qualification speaks for, and its F6 ancestry."""

    model_config = _STRICT

    qualified_source_git_sha: GitSha
    qualified_source_tree_sha: GitSha
    #: the accepted F6 corrective this source must descend from (inclusive of itself).
    f6_corrective_commit: GitSha
    descends_f6_corrective: bool
    worktree_matches_qualified_source: bool


class AcceptedIdentities(BaseModel):
    """Every accepted identity the qualification binds, recomputed rather than asserted."""

    model_config = _STRICT

    e5_gate_hashes: dict[str, Hex64]
    migration_sha256: dict[str, Hex64]
    f5_contract_hash: Hex64
    live_gatk_source_artifact_sha256: Hex64
    live_gatk_parameter_space_artifact_sha256: Hex64
    parameter_space_hash: Hex64
    policy_hash: Hex64
    candidate_set_hash: Hex64
    candidate_count: int = Field(ge=1)
    plan_hash: Hex64
    logical_job_count: int = Field(ge=1)
    alembic_head: str = Field(min_length=1)


class GatkBinaryIdentity(BaseModel):
    """The pinned GATK binary.

    ``version`` is PROVISIONED METADATA BOUND TO ``executable_sha256`` — it is never measured by
    probing the executable, and this contract does not claim otherwise.
    """

    model_config = _STRICT

    executable_sha256: Hex64
    version: str = Field(min_length=1)
    version_provenance: Literal["provisioned_metadata_bound_to_digest"] = (
        "provisioned_metadata_bound_to_digest"
    )
    absolute_path_is_symlink: bool = False


class QualificationInputIdentity(BaseModel):
    """The deterministically derived qualification job (train partition only)."""

    model_config = _STRICT

    member_index: int = Field(ge=0)
    candidate_index: int = Field(ge=0)
    partition: Literal["train"] = "train"
    dataset_id: str = Field(min_length=1)
    profile_id: str = Field(min_length=1)
    chromosome: str = Field(min_length=1)
    region_start0: int = Field(ge=0)
    region_end0_exclusive: int = Field(ge=0)
    job_key: Hex64
    config_hash: Hex64
    input_identity_hash: Hex64


class OfficialExecutionResult(BaseModel):
    """The outcome of ONE official SubprocessGatkRunner execution (never a fake runner)."""

    model_config = _STRICT

    runner_class: str = Field(min_length=1)
    used_official_runner: bool
    shell_used: Literal[False] = False
    job_status: str = Field(min_length=1)
    result_hash: Hex64
    logical_argv_hash: Hex64
    vcf_sha256: Hex64
    vcf_size_bytes: int = Field(gt=0)
    result_manifest_sha256: Hex64
    published_artifact_count: int = Field(ge=0)
    runtime_ms: int = Field(ge=0)


class ParityDifference(BaseModel):
    """The FIRST differing semantic token/field between the Twin plan and the F5 invocation."""

    model_config = _STRICT

    field: str = Field(min_length=1)
    index: int | None = None
    twin_value: str | None = None
    execution_value: str | None = None


class TwinParityResult(BaseModel):
    """Semantic GATK/Twin invocation parity (never coerced to PASS)."""

    model_config = _STRICT

    adapter_version: str = Field(min_length=1)
    caller: Literal["gatk"] = "gatk"
    subcommand: Literal["HaplotypeCaller"] = "HaplotypeCaller"
    parity_ok: bool
    compared_token_count: int = Field(ge=0)
    twin_plan_hash: Hex64
    twin_config_hash: Hex64
    execution_config_hash: Hex64
    region_token: str = Field(min_length=1)
    normalized_path_tokens: tuple[str, ...] = ()
    first_difference: ParityDifference | None = None


class ResumeResult(BaseModel):
    """Idempotent resume across a simulated process/engine restart."""

    model_config = _STRICT

    engines_recreated: bool
    duplicate_rows_created: int = Field(ge=0)
    terminal_job_reset: bool
    terminal_job_reexecuted: bool
    artifact_bytes_rewritten: bool
    exact_replay_returned_existing: bool
    conflicting_replay_rejected: bool
    exhausted_queue_returns_none: bool
    nonterminal_jobs_remaining: int = Field(ge=0)
    automatic_retry_observed: bool


class ArtifactVerificationResult(BaseModel):
    """Independent re-read and recomputation of the committed qualification graph."""

    model_config = _STRICT

    artifacts_verified: int = Field(ge=0)
    config_artifact_ok: bool
    vcf_artifact_ok: bool
    result_manifest_artifact_ok: bool
    content_addressed_names_ok: bool
    media_types_ok: bool
    recomputed_input_identity_hash: Hex64
    recomputed_logical_argv_hash: Hex64
    recomputed_result_hash: Hex64
    harness_verifier_status: str = Field(min_length=1)
    harness_verifier_checks: dict[str, bool]
    verifier_non_mutating: bool
    fingerprint_before: Hex64
    fingerprint_after: Hex64


class FailureClassificationEntry(BaseModel):
    """One public F5/F6 execution outcome, classified exactly once."""

    model_config = _STRICT

    case: str = Field(min_length=1)
    exception_type: str = Field(min_length=1)
    failure_code: str | None = None
    state_before_failure: str = Field(min_length=1)
    required_final_state: str = Field(min_length=1)
    outcome_row_exists: bool
    artifacts_retained: bool
    commit_outcome: Literal["known", "ambiguous"]
    automatic_retry_allowed: Literal[False] = False


class FailureClassificationInventory(BaseModel):
    """The complete, deterministic typed-failure inventory."""

    model_config = _STRICT

    entries: tuple[FailureClassificationEntry, ...] = Field(min_length=1)
    implemented_exception_types: tuple[str, ...] = Field(min_length=1)
    complete: bool
    unambiguous: bool


class BoundaryResult(BaseModel):
    """Leakage / non-mutation / authority boundary observations."""

    model_config = _STRICT

    truth_paths_resolved: int = Field(ge=0)
    scoring_paths_resolved: int = Field(ge=0)
    nontrain_members_touched: int = Field(ge=0)
    operational_database_written: bool
    operational_database_revision: str = Field(min_length=1)
    operational_l2f_table_count: int = Field(ge=0)
    select_config_blocked: bool
    network_access_performed: bool


class HarnessReadyQualification(BaseModel):
    """The immutable HARNESS-READY qualification result.

    Every field is an observation produced by the qualifier from real execution and real
    recomputation. Nothing here is a caller-supplied boolean, hash, plan or candidate set.
    """

    model_config = _STRICT

    schema_version: Literal["l2f-harness-ready-qualification-v1"] = (
        "l2f-harness-ready-qualification-v1"
    )
    qualifier_version: str = Field(min_length=1)
    gate_name: Literal["HARNESS-READY"] = "HARNESS-READY"
    source: SourceProvenance
    accepted: AcceptedIdentities
    gatk_binary: GatkBinaryIdentity
    qualification_input: QualificationInputIdentity
    official_execution: OfficialExecutionResult
    twin_parity: TwinParityResult
    resume: ResumeResult
    artifact_verification: ArtifactVerificationResult
    failure_inventory: FailureClassificationInventory
    boundaries: BoundaryResult

    def qualification_hash(self) -> str:
        return compute_qualification_hash(self)


def canonical_qualification_bytes(result: HarnessReadyQualification) -> bytes:
    """The exact canonical bytes of a qualification result (deterministic, timestamp-free)."""
    return canonical_json_bytes(result.model_dump(mode="json"))


def compute_qualification_hash(result: HarnessReadyQualification) -> str:
    """Domain-separated identity of the complete qualification observation set."""
    return sha256_hex(
        HARNESS_READY_QUALIFICATION_DOMAIN.encode("utf-8") + canonical_qualification_bytes(result)
    )


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    seen: dict[str, Any] = {}
    for key, value in pairs:
        if key in seen:
            raise HarnessReadyContractError(f"duplicate JSON key {key!r} in a qualification result")
        seen[key] = value
    return seen


def load_qualification_json(raw: bytes) -> HarnessReadyQualification:
    """Strictly parse qualification bytes.

    Rejects duplicate JSON keys, non-canonical bytes and unknown fields; the parsed document must
    equal its own canonical serialization exactly.
    """
    try:
        document = json.loads(raw.decode("utf-8"), object_pairs_hook=_reject_duplicate_keys)
    except HarnessReadyContractError:
        raise
    except Exception as exc:
        raise HarnessReadyContractError(f"qualification bytes are not valid JSON: {exc}") from exc
    if not isinstance(document, dict):
        raise HarnessReadyContractError("a qualification result must be a JSON object")
    if canonical_json_bytes(document) != raw:
        raise HarnessReadyContractError(
            "qualification bytes are not the canonical serialization of their content"
        )
    try:
        # JSON-mode validation: strict about types and unknown fields, but a JSON array is the
        # canonical wire form of a tuple field, so it is accepted here and nowhere else.
        return HarnessReadyQualification.model_validate_json(raw)
    except HarnessReadyContractError:
        raise
    except Exception as exc:
        raise HarnessReadyContractError(f"qualification result is invalid: {exc}") from exc
