"""``l2g-relative-finalist-contract-v1`` — the L2-G v2 learning problem.

Campaign v1 asked each model to predict expected utility for any of 80 configs and then pick the
best. It predicted the score creditably and still chose worse configs than "always use the
qualified baseline". The diagnosis is that most of the signal in ``U(BAM, config)`` is BAM
difficulty, which is common to every config and therefore cancels in the decision.

v2 removes it. The target is the RELATIVE ADVANTAGE of deviating from the safe baseline:

    DELTA(i, theta) = U(i, theta) - U(i, theta_safe)

Positive means switching would have helped, negative means it would have hurt. A model that
predicts BAM difficulty perfectly and advantage not at all now scores zero, which is the honest
reflection of how useful it is for choosing a configuration.

The action domain is the four Phase-D finalists and nothing else. That is not a convenience: the
existing VALIDATION cohort carries outcome labels for exactly those four configs on its ten BAMs,
so a policy that could select any of the 80 TRAIN configs could never be evaluated end-to-end
without running new VALIDATION executions. Restricting the domain is what makes a later one-shot
VALIDATION check honest rather than partial.

Campaign v1 is the PARENT evidence for this hypothesis and is bound as lineage. Its predictions,
residuals and selections are not inputs: motivation is not a feature.
"""

from __future__ import annotations

from typing import Any, Final

from minos_engine.common.canonical_json import canonical_json_bytes
from minos_engine.common.errors import MinosEngineError
from minos_engine.common.hashing import sha256_hex

__all__ = [
    "ALTERNATIVE_FINALISTS",
    "FINALIST_DOMAIN",
    "RELATIVE_CONTRACT_DOMAIN",
    "RELATIVE_CONTRACT_SCHEMA",
    "RESEARCH_PROTOCOL_VERSION",
    "SAFE_BASELINE_CONFIG_HASH",
    "RelativeFinalistError",
    "compute_finalist_domain_hash",
    "compute_relative_contract_hash",
    "relative_contract_content",
]

RELATIVE_CONTRACT_SCHEMA: Final = "l2g-relative-finalist-contract-v1"
RELATIVE_CONTRACT_DOMAIN: Final = "minos:l2g-relative-finalist-contract:v1\n"
FINALIST_DOMAIN_IDENTITY_DOMAIN: Final = "minos:l2g-finalist-decision-domain:v1\n"

#: L2-G research campaign 2. Campaign v1 is complete, frozen and closed; this is a new protocol,
#: not a continuation, and nothing here may relax a v1 threshold.
RESEARCH_PROTOCOL_VERSION: Final = 2

#: the parent evidence: the frozen v1 campaign whose result motivated this hypothesis
PARENT_CAMPAIGN_FREEZE_IDENTITY: Final = (
    "1c2039dec2f3fbb51a8058c947bbf8de9f9c6d235a133b5948aa6b33ac516673"
)

SAFE_BASELINE_CONFIG_HASH: Final = (
    "157d88d1587c13be395c62d60e27d1becdada78fad45e65d883bc1190e51acea"
)
#: the ordered four, as the accepted Phase-C finalist freeze emits them
FINALIST_DOMAIN: Final[tuple[str, ...]] = (
    SAFE_BASELINE_CONFIG_HASH,
    "0972930f8d8c562be15382203e123b2909094e7eac46e84321d36c67abf8345e",
    "22a1f1fd9ddf02a97776d991f11280b3982673693a4f357479098a99fb411a16",
    "4251cb85e5cd58b7eabfe530b9df23ea7d1d14fd882114b488d67cbd81b751b8",
)
ALTERNATIVE_FINALISTS: Final[tuple[str, ...]] = FINALIST_DOMAIN[1:]

ACCEPTED_FINALIST_FREEZE_SHA256: Final = (
    "540aeca0640871ca91e3ec771ec66d2df4b96d38210ec3265f944dee3e0433f3"
)

RELATIVE_TARGET: Final = "DELTA_UTILITY_VERSUS_SAFE_BASELINE"
UTILITY_SEMANTICS: Final = "ADMITTED_PERSISTED_MINOS_SCORE_ELSE_ZERO"
#: the safe baseline is an action, not a learned quantity: its own advantage is exactly zero
DELTA_SAFE_BASELINE: Final = 0.0

TRAIN_BAM_COUNT: Final = 50
FINALIST_COUNT: Final = 4
DENSE_CELL_COUNT: Final = TRAIN_BAM_COUNT * FINALIST_COUNT
RELATIVE_EXAMPLE_COUNT: Final = TRAIN_BAM_COUNT * (FINALIST_COUNT - 1)

#: v1 artefacts that must never become v2 predictors
FORBIDDEN_V2_PREDICTORS: Final[tuple[str, ...]] = (
    "campaign_v1_prediction",
    "campaign_v1_residual",
    "campaign_v1_selected_config",
    "minos_score",
    "admitted",
    "admission_code",
    "truth_vcf",
    "evaluation_hash",
    "dataset_id",
    "chromosome",
    "round_id",
    "partition",
)


class RelativeFinalistError(MinosEngineError):
    """The relative-advantage contract was violated."""


def compute_finalist_domain_hash(finalists: tuple[str, ...] = FINALIST_DOMAIN) -> str:
    """Identity of the exact ordered decision domain."""
    if len(finalists) != FINALIST_COUNT:
        raise RelativeFinalistError(
            f"the decision domain holds {len(finalists)} configs, expected {FINALIST_COUNT}"
        )
    if len(set(finalists)) != len(finalists):
        raise RelativeFinalistError("a finalist appears twice in the decision domain")
    if finalists[0] != SAFE_BASELINE_CONFIG_HASH:
        raise RelativeFinalistError(
            "the safe baseline must be the first action in the domain; it is the fallback every "
            "switch is measured against"
        )
    return sha256_hex(
        FINALIST_DOMAIN_IDENTITY_DOMAIN.encode("utf-8")
        + canonical_json_bytes(
            {
                "alternatives": list(finalists[1:]),
                "ordered_finalists": list(finalists),
                "safe_baseline": SAFE_BASELINE_CONFIG_HASH,
                "finalist_freeze_sha256": ACCEPTED_FINALIST_FREEZE_SHA256,
            }
        )
    )


def verify_finalist_domain(finalists: tuple[str, ...]) -> str:
    """Re-derive the domain hash and require the accepted four."""
    if tuple(finalists) != FINALIST_DOMAIN:
        raise RelativeFinalistError(
            "the decision domain is not the accepted four Phase-D finalists in their frozen order"
        )
    return compute_finalist_domain_hash(finalists)


def relative_contract_content() -> dict[str, Any]:
    return {
        "schema_version": RELATIVE_CONTRACT_SCHEMA,
        "research_protocol_version": RESEARCH_PROTOCOL_VERSION,
        "parent_campaign_freeze_identity": PARENT_CAMPAIGN_FREEZE_IDENTITY,
        "safe_baseline_config_hash": SAFE_BASELINE_CONFIG_HASH,
        "finalist_domain": list(FINALIST_DOMAIN),
        "finalist_domain_hash": compute_finalist_domain_hash(),
        "finalist_freeze_sha256": ACCEPTED_FINALIST_FREEZE_SHA256,
        "relative_target": RELATIVE_TARGET,
        "utility_semantics": UTILITY_SEMANTICS,
        "delta_safe_baseline": DELTA_SAFE_BASELINE,
        "dense_cell_count": DENSE_CELL_COUNT,
        "relative_example_count": RELATIVE_EXAMPLE_COUNT,
        "train_bam_count": TRAIN_BAM_COUNT,
        "forbidden_predictors": list(FORBIDDEN_V2_PREDICTORS),
        "domain_rationale": (
            "VALIDATION carries outcome labels for exactly these four configs on its ten BAMs; a "
            "policy over the full 80-config TRAIN domain could not be evaluated end-to-end "
            "without new VALIDATION executions"
        ),
        "v1_relationship": "PARENT_EVIDENCE_ONLY_NOT_A_PREDICTOR",
    }


def compute_relative_contract_hash() -> str:
    return sha256_hex(
        RELATIVE_CONTRACT_DOMAIN.encode("utf-8") + canonical_json_bytes(relative_contract_content())
    )
