"""``l2f2-baseline-search-protocol-v1`` — the FROZEN baseline-selection protocol.

This is a **pre-registration**. Every rule that decides which GATK configuration becomes the
baseline is fixed here, hashed, and committed *before the first real score exists*. That ordering
is the entire point: a protocol chosen after seeing scores can always be chosen to favour a
particular answer, and no amount of later care recovers the lost guarantee.

The protocol hash therefore binds only *decisions*, never *outcomes*. Deliberately excluded are
the repository SHA, timestamps, hostnames, filesystem paths, database identifiers, observed
scores, the six dimensions Phase A will select, and the Phase-B configurations those dimensions
will generate. Those are results produced *under* the protocol; binding them would make the
identity circular and unverifiable.

It is a separate identity from ``l2f2-minos-scoring-v1``. The scoring contract answers *how one
execution is scored*; this answers *how scored executions are compared and promoted*. Mixing the
two hashes would mean a change to either invalidated evidence about the other.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from minos_engine.baseline.design import (
    INFLUENTIAL_DIMENSION_COUNT,
    LHS_DOMAIN,
    LHS_PROPOSAL_CEILING,
    PHASE_B_ANCHOR_COUNT,
    PHASE_B_CANDIDATE_COUNT,
    PHASE_B_LHS_COUNT,
)
from minos_engine.baseline.objective import (
    CANDIDATE_EXECUTION_FAILURE_CODES,
    CVAR_ALPHA,
    CVAR_WEIGHT,
    FAILURE_PENALTY,
    FLOOR_WEIGHT,
    INFRASTRUCTURE_EVALUATION_FAILURE_CODES,
    INFRASTRUCTURE_EXECUTION_FAILURE_CODES,
    MEAN_WEIGHT,
)
from minos_engine.baseline.racing import (
    PHASE_B_SURVIVOR_COUNT,
    VALIDATION_FINALIST_COUNT,
)
from minos_engine.baseline.schedule import (
    BATCH_COUNT,
    CHROMOSOMES,
    SPLIT_MANIFEST_PATH,
    TRAIN_COUNT,
    TRAIN_PER_CHROMOSOME,
    split_manifest_sha256,
)
from minos_engine.common.canonical_json import canonical_json_bytes
from minos_engine.common.errors import MinosEngineError
from minos_engine.common.hashing import sha256_hex

__all__ = [
    "BASELINE_PROTOCOL_DOMAIN",
    "BASELINE_PROTOCOL_MANIFEST",
    "BASELINE_PROTOCOL_VERSION",
    "GATK_TIMEOUT_SECONDS",
    "HAPPY_TIMEOUT_SECONDS",
    "INFRASTRUCTURE_ABORT_THRESHOLD",
    "MAX_EVALUATION_BUDGET",
    "PHASE_A_CANDIDATE_COUNT",
    "PHASE_A_CANDIDATE_SET_HASH",
    "PHASE_D_MEMBER_COUNT",
    "PARAMETER_SPACE_HASH",
    "BaselineProtocol",
    "BaselineProtocolError",
    "build_baseline_protocol",
    "compute_protocol_hash",
    "load_committed_protocol",
]

BASELINE_PROTOCOL_VERSION = "l2f2-baseline-search-protocol-v1"
BASELINE_PROTOCOL_DOMAIN = "minos:l2f2-baseline-search-protocol:v1\n"
BASELINE_PROTOCOL_MANIFEST = "manifests/l2f2_baseline_protocol_v1.json"

#: D4 — timeouts are execution boundaries, not weighted objective terms.
GATK_TIMEOUT_SECONDS = 3600
HAPPY_TIMEOUT_SECONDS = 3600

#: the immutable L2-F1 Phase-A candidate authority.
PHASE_A_CANDIDATE_SET_HASH = "50d5f36918758de204e4b34cdd3fc8560a14debfcdb25869f713690c6085057d"
PHASE_A_CANDIDATE_COUNT = 39

#: the committed live GATK parameter-space scientific identity.
PARAMETER_SPACE_HASH = "b2d401918084d64023305d9262baf5011a89fe517bee4e0bd33af79fb14aee2e"

#: D5 — STANDARD budget. A protocol maximum, never a target.
PHASE_D_MEMBER_COUNT = 10
_PHASE_BUDGET = {
    "phase_a": PHASE_A_CANDIDATE_COUNT * TRAIN_PER_CHROMOSOME // 2,  # 39 x 5
    "phase_b": PHASE_B_CANDIDATE_COUNT * 10,  # 48 x 10
    "phase_c": PHASE_B_SURVIVOR_COUNT * TRAIN_COUNT,  # 10 x 50
    "phase_d": VALIDATION_FINALIST_COUNT * PHASE_D_MEMBER_COUNT,  # 4 x 10
}
MAX_EVALUATION_BUDGET = sum(_PHASE_BUDGET.values())  # 1215

#: a phase aborts when OUR infrastructure is failing, never because candidates score badly.
INFRASTRUCTURE_ABORT_THRESHOLD = 0.05

_STRICT = ConfigDict(extra="forbid", frozen=True, strict=True)


class BaselineProtocolError(MinosEngineError):
    """The committed baseline protocol is absent, malformed or inconsistent."""


class BaselineProtocol(BaseModel):
    """The frozen protocol. Its ``protocol_hash`` is the identity every phase cites."""

    model_config = _STRICT

    schema_version: Literal["l2f2-baseline-search-protocol-v1"] = "l2f2-baseline-search-protocol-v1"
    split_manifest_sha256: str = Field(min_length=64, max_length=64)

    def decisions(self) -> dict[str, str]:
        """D1-D8, resolved. Protocol decisions — deterministic pre-registration, not approval."""
        return {
            "D1_primary_optimization_target": "LEVEL_ROBUST_PRIMARY_RANK_DIAGNOSTIC",
            "D2_objective_form": "OPTION_B_CVAR_FLOOR_MEAN_FAILURE_PENALTY",
            "D3_robustness_parameters": (
                f"alpha={CVAR_ALPHA};cvar={CVAR_WEIGHT};floor={FLOOR_WEIGHT};"
                f"mean={MEAN_WEIGHT};failure_penalty={FAILURE_PENALTY}"
            ),
            "D4_runtime_treatment": (
                "NOT_A_WEIGHTED_TERM;bounded_timeouts;gatk_runtime_is_tie_break_only"
            ),
            "D5_compute_budget": "STANDARD",
            "D6_validation_timing": "L2F2F_AFTER_TRAIN_RANKING_FINAL",
            "D7_platform_reward_modelling": "NO_SIMULATED_OPPONENT_DISTRIBUTION",
            "D8_phase_b_design_family": "DETERMINISTIC_MIXED_DOMAIN_LATIN_HYPERCUBE",
        }

    def content(self) -> dict[str, Any]:
        """EXACTLY what the protocol hash covers. Decisions only — never observations."""
        return {
            "schema_version": self.schema_version,
            "decisions": self.decisions(),
            "objective": {
                "form": "J = w_cvar*CVaR_alpha + w_floor*min_k(chr_mean_k) + w_mean*mean "
                "- lambda*failure_rate",
                "cvar_alpha": CVAR_ALPHA,
                "weight_cvar": CVAR_WEIGHT,
                "weight_floor": FLOOR_WEIGHT,
                "weight_mean": MEAN_WEIGHT,
                "failure_penalty_lambda": FAILURE_PENALTY,
                "higher_is_better": True,
                "aggregation_utility_rule": (
                    "admitted -> minos_score in [0,1]; known candidate failure or non-admission "
                    "-> 0.0 for AGGREGATION ONLY, never written to the evaluation ledger"
                ),
                "missing_rule": (
                    "a missing evaluation is neither zero nor failure; the candidate is not "
                    "complete and not finally rankable, and may participate only through the "
                    "frozen racing bounds"
                ),
                "failure_rate_denominator": "required_member_count",
            },
            "tie_break": [
                "higher_objective",
                "lower_mean_gatk_runtime_ms",
                "lower_candidate_index_in_frozen_phase_design",
                "lexicographically_smaller_config_hash",
            ],
            "timeouts_seconds": {
                "gatk": GATK_TIMEOUT_SECONDS,
                "happy": HAPPY_TIMEOUT_SECONDS,
            },
            "budget": {
                "tier": "STANDARD",
                "phase_maximums": dict(sorted(_PHASE_BUDGET.items())),
                "maximum_evaluation_pairs": MAX_EVALUATION_BUDGET,
                "reuse_and_racing_may_only_reduce": True,
            },
            "train_schedule": {
                "split_manifest": SPLIT_MANIFEST_PATH,
                "split_manifest_sha256": self.split_manifest_sha256,
                "chromosomes": list(CHROMOSOMES),
                "train_count": TRAIN_COUNT,
                "train_per_chromosome": TRAIN_PER_CHROMOSOME,
                "batch_count": BATCH_COUNT,
                "batch_size": len(CHROMOSOMES),
                "batch_rule": (
                    "batch j takes the j-th TRAIN member of each chromosome in committed "
                    "manifest order; every batch holds exactly one member per chromosome"
                ),
                "phase_a_batches": 1,
                "phase_b_batches": 2,
                "phase_c_batches": BATCH_COUNT,
            },
            "phase_a": {
                "candidate_set_hash": PHASE_A_CANDIDATE_SET_HASH,
                "candidate_count": PHASE_A_CANDIDATE_COUNT,
                "purpose": "SENSITIVITY_SCREEN_NOT_FINAL_OPTIMISATION",
                "impact_rule": (
                    "delta_i(a)=|u_i(a)-u_i(seed)|; impact(a)=mean_i delta_i(a); "
                    "impact(p)=max over alternatives of p"
                ),
                "influential_dimension_count": INFLUENTIAL_DIMENSION_COUNT,
                "dimension_order": [
                    "descending_impact",
                    "ascending_live_parameter_index",
                    "lexicographically_smaller_name",
                ],
                "anchor_rule": [
                    "higher_phase_a_objective",
                    "lower_mean_gatk_runtime_ms",
                    "lower_accepted_candidate_index",
                    "lexicographically_smaller_config_hash",
                ],
            },
            "phase_b": {
                "design_family": "DETERMINISTIC_MIXED_DOMAIN_LATIN_HYPERCUBE",
                "lhs_domain": LHS_DOMAIN,
                "lhs_proposal_ceiling": LHS_PROPOSAL_CEILING,
                "candidate_count": PHASE_B_CANDIDATE_COUNT,
                "composition": {
                    "seed": 1,
                    "anchors": PHASE_B_ANCHOR_COUNT,
                    "lhs": PHASE_B_LHS_COUNT,
                },
                "parameter_space_hash": PARAMETER_SPACE_HASH,
                "varying_dimensions": INFLUENTIAL_DIMENSION_COUNT,
                "non_selected_parameters": "REMAIN_EXACTLY_AT_SEED",
                "mapping": {
                    "bool_and_enum": "quantize into the canonical allowed-value order",
                    "int": "canonical min/max, uniform strata, clamped to max",
                    "float": "canonical bounds, linear map",
                    "singleton_domain": "FIXED, never varied",
                },
                "invalid_or_duplicate_proposal": "SKIP_DETERMINISTICALLY_AND_CONTINUE",
                "insufficient_valid_proposals": "FAIL_CLOSED_NEVER_SHRINK",
                "entropy_sources_forbidden": [
                    "system_random_seed",
                    "current_time",
                    "python_hash",
                    "hostname",
                    "pid",
                ],
            },
            "racing": {
                "optimistic_unseen_utility": 1.0,
                "optimistic_unseen_failure": False,
                "pessimistic_unseen_utility": 0.0,
                "pessimistic_unseen_failure": True,
                "evaluated_only_on_complete_balanced_batches": True,
                "elimination_rule": (
                    "eliminate only when optimistic_J < threshold pessimistic_J, STRICT; a "
                    "candidate able to tie the threshold survives"
                ),
                "phase_b_threshold_rank": PHASE_B_SURVIVOR_COUNT,
                "phase_c_threshold_rank": VALIDATION_FINALIST_COUNT,
                "seed_never_eliminated": True,
            },
            "promotion": {
                "phase_c_survivors": PHASE_B_SURVIVOR_COUNT,
                "validation_finalists": VALIDATION_FINALIST_COUNT,
                "seed_control": (
                    "if the seed ranks naturally within the promoted count take the natural top; "
                    "otherwise take the top count-1 plus the seed. The count never changes."
                ),
            },
            "validation": {
                "stage": "L2-F2-F",
                "precondition": (
                    "Phase C complete, TRAIN ranking final, four finalist config hashes frozen, "
                    "protocol hash already frozen"
                ),
                "forbidden_before": ["canary", "phase_a", "phase_b", "phase_c"],
                "member_count": PHASE_D_MEMBER_COUNT,
                "evaluations": VALIDATION_FINALIST_COUNT * PHASE_D_MEMBER_COUNT,
                "racing": "NONE_EVERY_FINALIST_RECEIVES_EVERY_MEMBER",
                "isolation": (
                    "a SEPARATE validation closure/workspace/database; the TRAIN baseline store "
                    "stays permanently TRAIN-only for this search"
                ),
            },
            "test_lock": {
                "member_count": 15,
                "l2f2_usage": "ZERO",
                "forbidden": [
                    "registration",
                    "path_resolution",
                    "hashing",
                    "execution",
                    "evaluation",
                    "ranking",
                ],
                "sealed_until": "L2-I",
            },
            "failure_classification": {
                "candidate_failure_codes": list(CANDIDATE_EXECUTION_FAILURE_CODES),
                "non_admission_is_candidate_failure": True,
                "infrastructure_incident_codes": sorted(
                    [
                        *INFRASTRUCTURE_EXECUTION_FAILURE_CODES,
                        *INFRASTRUCTURE_EVALUATION_FAILURE_CODES,
                    ]
                ),
                "infrastructure_abort_threshold": INFRASTRUCTURE_ABORT_THRESHOLD,
                "candidate_failures_never_count_toward_infrastructure_abort": True,
            },
            "rank_diagnostics": {
                "policy": "DIAGNOSTIC_ONLY",
                "permitted": ["within_set_win_rate", "top_k_frequency", "median_candidate_rank"],
                "may_influence": [],
                "must_never_influence": [
                    "objective",
                    "racing",
                    "promotion",
                    "baseline_selection",
                ],
            },
        }

    @property
    def protocol_hash(self) -> str:
        return compute_protocol_hash(self)


def compute_protocol_hash(protocol: BaselineProtocol) -> str:
    """The domain-separated identity of the frozen protocol."""
    return sha256_hex(
        BASELINE_PROTOCOL_DOMAIN.encode("utf-8") + canonical_json_bytes(protocol.content())
    )


def build_baseline_protocol(root: Path | None = None) -> BaselineProtocol:
    """Build the protocol from committed authorities alone."""
    return BaselineProtocol(split_manifest_sha256=split_manifest_sha256(root))


def load_committed_protocol(root: Path | None = None) -> dict[str, Any]:
    """Read the committed protocol manifest and verify it against the code. Fails closed."""
    import json

    from minos_engine.qualification.l2f_accepted_identities import repository_root

    base = root or repository_root()
    path = base / BASELINE_PROTOCOL_MANIFEST
    if not path.is_file():
        raise BaselineProtocolError(f"committed baseline protocol manifest is missing: {path}")
    try:
        document: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise BaselineProtocolError(f"baseline protocol manifest is not valid JSON: {exc}") from exc

    protocol = build_baseline_protocol(base)
    if document.get("protocol_hash") != protocol.protocol_hash:
        raise BaselineProtocolError(
            "committed baseline protocol hash does not match the protocol the code defines"
        )
    if document.get("content") != protocol.content():
        raise BaselineProtocolError(
            "committed baseline protocol content does not match the protocol the code defines"
        )
    return document
