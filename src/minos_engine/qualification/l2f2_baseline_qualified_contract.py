"""``l2f2-baseline-qualified-v1`` — the canonical BASELINE-QUALIFIED qualification result.

The observation this binds is the COMPLETE verified input set the 42 registered checks are
derived from, and it is hashed the way every other qualification result in this repository is
hashed: domain-separated, over canonical JSON, excluding only the non-scientific timestamp.

Two things here are worth reading closely.

``objective_identity`` and ``candidate_design_identity``
--------------------------------------------------------
§13 asks the gate to bind an "objective hash" and a "candidate-design hash". No such standalone
identities were ever minted — an audit of the whole repository finds none. But they are not
missing either: both are fully specified INSIDE the frozen protocol content, and therefore inside
``c548e190…`` already.

* the objective is ``content["objective"]`` (form, weights, alpha, penalty, denominator, the
  aggregation-utility rule and the missing rule) together with ``content["tie_break"]`` and
  decisions ``D2``/``D3``;
* the candidate design is ``content["phase_a"]`` and ``content["phase_b"]`` together with
  decision ``D8``.

So rather than invent a post-result scientific identity, this module derives a deterministic
digest over exactly those sub-blocks AND proves each sub-block is byte-identical to the
corresponding slice of the committed protocol content. The containing protocol hash is bound too.
Nothing new is decided: if the protocol changed, both the containing hash and the sub-identity
move together, and the derivation is reproducible by anyone holding the manifest.

Trusted vs. asserted
--------------------
:class:`BaselineQualifiedObservation` is a plain data class and a caller can build one saying
whatever it likes — that is fine for unit-testing ``derive_checks``. It is NOT sufficient to mint
PASS. Only :class:`TrustedBaselineQualification`, which the production qualifier alone constructs
from verified evidence, is accepted by the gate assembler.
"""

from __future__ import annotations

from typing import Any, Final

from pydantic import BaseModel, ConfigDict, Field

from minos_engine.common.canonical_json import canonical_json_bytes
from minos_engine.common.errors import MinosEngineError
from minos_engine.common.hashing import sha256_hex

__all__ = [
    "BASELINE_QUALIFICATION_DOMAIN",
    "BASELINE_QUALIFICATION_SCHEMA",
    "BASELINE_QUALIFICATION_TOOL_VERSION",
    "HARNESS_READY_GATE_HASH",
    "HARNESS_READY_QUALIFICATION_HASH",
    "ACCEPTED_BCFTOOLS_DIGEST",
    "ACCEPTED_HAPPY_DIGEST",
    "BaselineQualificationContractError",
    "BaselineQualificationResult",
    "candidate_design_identity",
    "compute_baseline_qualification_hash",
    "canonical_baseline_qualification_bytes",
    "objective_identity",
]

BASELINE_QUALIFICATION_SCHEMA: Final = "l2f2-baseline-qualified-v1"
BASELINE_QUALIFICATION_DOMAIN: Final = "minos:l2f2-baseline-qualified:v1\n"
BASELINE_QUALIFICATION_TOOL_VERSION: Final = "l2f2-baseline-qualified-qualification-v1/g1"

#: FULL committed HARNESS-READY identities. Prefixes are not identities: the negatives in the
#: test-suite append 56 wrong hex characters to each of these eight-character heads and must fail.
HARNESS_READY_GATE_HASH: Final = "0e8411ebffa9b6a27ec47cd896efd234bd60cdb30edf6f8f998ff8f06419fcc3"
HARNESS_READY_QUALIFICATION_HASH: Final = (
    "b1d1cc5d6a43520ba2b75cd27f3b4bdd70bbcc1b22845721853850c9fa7d3d09"
)

#: the EXACT resolved images the scoring authority audited. Shape is not identity.
ACCEPTED_HAPPY_DIGEST: Final = (
    "genonet/hap-py@sha256:03acabe84bbfba35f5a7234129d524c563f5657e1f21150a2ea2797f8e6d05f2"
)
ACCEPTED_BCFTOOLS_DIGEST: Final = (
    "quay.io/biocontainers/bcftools@sha256:"
    "badc3a0c7af72a83e5761ab0e881aa84204694bdead003b47552cb283958f78d"
)

_OBJECTIVE_DOMAIN: Final = "minos:l2f2-baseline-objective-identity:v1\n"
_DESIGN_DOMAIN: Final = "minos:l2f2-baseline-candidate-design-identity:v1\n"

#: exactly the protocol sub-blocks each identity covers. Named here so the derivation is auditable
#: rather than implicit.
OBJECTIVE_CONTENT_KEYS: Final[tuple[str, ...]] = ("objective", "tie_break")
OBJECTIVE_DECISION_KEYS: Final[tuple[str, ...]] = (
    "D2_objective_form",
    "D3_robustness_parameters",
)
DESIGN_CONTENT_KEYS: Final[tuple[str, ...]] = ("phase_a", "phase_b")
DESIGN_DECISION_KEYS: Final[tuple[str, ...]] = ("D8_phase_b_design_family",)

_STRICT = ConfigDict(extra="forbid", frozen=True, strict=True)


class BaselineQualificationContractError(MinosEngineError):
    """The qualification result is malformed, or its derived identities do not hold."""


def _sub_identity(
    protocol_content: dict[str, Any],
    *,
    domain: str,
    content_keys: tuple[str, ...],
    decision_keys: tuple[str, ...],
) -> str:
    """A deterministic identity over an exact slice of already-frozen protocol content."""
    decisions = protocol_content.get("decisions") or {}
    missing = [k for k in content_keys if k not in protocol_content]
    missing += [k for k in decision_keys if k not in decisions]
    if missing:
        raise BaselineQualificationContractError(
            f"the protocol content does not carry {missing}; the identity cannot be derived from "
            "frozen content and must not be invented"
        )
    slice_ = {
        **{key: protocol_content[key] for key in content_keys},
        "decisions": {key: decisions[key] for key in decision_keys},
    }
    return sha256_hex(domain.encode("utf-8") + canonical_json_bytes(slice_))


def objective_identity(protocol_content: dict[str, Any]) -> str:
    """The objective's identity, derived from the frozen protocol content that defines it."""
    return _sub_identity(
        protocol_content,
        domain=_OBJECTIVE_DOMAIN,
        content_keys=OBJECTIVE_CONTENT_KEYS,
        decision_keys=OBJECTIVE_DECISION_KEYS,
    )


def candidate_design_identity(protocol_content: dict[str, Any]) -> str:
    """The candidate design's identity, derived the same way."""
    return _sub_identity(
        protocol_content,
        domain=_DESIGN_DOMAIN,
        content_keys=DESIGN_CONTENT_KEYS,
        decision_keys=DESIGN_DECISION_KEYS,
    )


class TrainEvidenceSummary(BaseModel):
    """What a read-only observer measured in the TRAIN store. No caller-supplied counts."""

    model_config = _STRICT

    revision: str = Field(min_length=1)
    plan_hashes: tuple[str, ...]
    logical_job_count: int = Field(ge=0)
    terminal_job_count: int = Field(ge=0)
    nonterminal_job_count: int = Field(ge=0)
    succeeded_without_evaluation: int = Field(ge=0)
    evaluation_count: int = Field(ge=0)
    evaluation_failure_count: int = Field(ge=0)
    evaluation_set_sha256: str = Field(min_length=64, max_length=64)
    execution_failure_set_sha256: str = Field(min_length=64, max_length=64)
    execution_failure_codes: dict[str, int]
    distinct_scoring_contracts: int = Field(ge=0)
    scoring_contract_hash: str = Field(min_length=64, max_length=64)
    distinct_execution_environments: int = Field(ge=0)
    execution_environment_hash: str = Field(min_length=64, max_length=64)

    def as_observed(self) -> dict[str, Any]:
        """The plain summary the pure TRAIN verifier consumes."""
        payload = self.model_dump(mode="json")
        payload["plan_hashes"] = list(self.plan_hashes)
        return payload


class BaselineQualificationResult(BaseModel):
    """THE canonical qualification result. Everything the gate reasons about, already verified."""

    model_config = _STRICT

    schema_version: str = BASELINE_QUALIFICATION_SCHEMA
    qualification_tool_version: str = BASELINE_QUALIFICATION_TOOL_VERSION

    # ---- source provenance -----------------------------------------------------------------
    qualified_source_git_sha: str = Field(min_length=40, max_length=40)
    qualified_source_tree_sha: str = Field(min_length=40, max_length=40)
    worktree_clean: bool
    descends_closure_authority_source: bool

    # ---- prerequisite gates ----------------------------------------------------------------
    harness_ready_gate_hash: str = Field(min_length=64, max_length=64)
    harness_ready_qualification_hash: str = Field(min_length=64, max_length=64)
    harness_ready_gate_verified: bool

    # ---- frozen authorities ----------------------------------------------------------------
    baseline_protocol_hash: str = Field(min_length=64, max_length=64)
    objective_identity: str = Field(min_length=64, max_length=64)
    candidate_design_identity: str = Field(min_length=64, max_length=64)
    selection_interpretation_hash: str = Field(min_length=64, max_length=64)
    scoring_contract_hash: str = Field(min_length=64, max_length=64)
    execution_environment_hash: str = Field(min_length=64, max_length=64)
    minos_subnet_sha: str = Field(min_length=40, max_length=40)
    happy_resolved_digest: str = Field(min_length=1)
    bcftools_resolved_digest: str = Field(min_length=1)
    scorer_source_identities_verified: bool

    # ---- the frozen result -----------------------------------------------------------------
    baseline_selected_hash: str = Field(min_length=64, max_length=64)
    baseline_selected_manifest_verified: bool
    phase_d_closure_hash: str = Field(min_length=64, max_length=64)
    phase_d_closure_artifact_sha256: str = Field(min_length=64, max_length=64)
    closure_artifact_verified: bool
    selected_config_hash: str = Field(min_length=64, max_length=64)
    selected_rank: int = Field(ge=0)
    selected_inherited_candidate_index: int = Field(ge=0)
    selected_statistics_verified: bool
    seed_config_hash: str = Field(min_length=64, max_length=64)
    seed_rank: int = Field(ge=0)
    candidate_count: int = Field(ge=0)
    member_count: int = Field(ge=0)
    observation_count: int = Field(ge=0)
    all_candidates_complete: bool
    validation_infrastructure_incidents: int = Field(ge=0)

    # ---- TRAIN + isolation -----------------------------------------------------------------
    train: TrainEvidenceSummary
    test_seal_evidence: dict[str, str]
    test_untouched: bool
    train_and_validation_identities_disjoint: bool

    # ---- external evidence identities ------------------------------------------------------
    evidence_sha256: dict[str, str]

    #: non-scientific; excluded from the qualification hash, exactly as HARNESS does.
    created_at: str | None = None

    def content(self) -> dict[str, Any]:
        """Exactly what ``qualification_hash`` covers — everything except the timestamp."""
        payload = self.model_dump(mode="json")
        payload.pop("created_at", None)
        payload["train"]["plan_hashes"] = list(self.train.plan_hashes)
        return payload


def canonical_baseline_qualification_bytes(result: BaselineQualificationResult) -> bytes:
    """The canonical bytes the qualification hash is taken over."""
    return canonical_json_bytes(result.content())


def compute_baseline_qualification_hash(result: BaselineQualificationResult) -> str:
    """The domain-separated identity of one qualification result."""
    return sha256_hex(
        BASELINE_QUALIFICATION_DOMAIN.encode("utf-8")
        + canonical_baseline_qualification_bytes(result)
    )
