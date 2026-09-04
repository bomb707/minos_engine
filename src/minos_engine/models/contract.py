"""``l2g-training-contract-v1`` — what L2-G learns, and what it is never allowed to see.

THE LEARNING PROBLEM
--------------------
Given one BAM's production-eligible feature vector and one canonical GATK configuration,
predict the outcome the engine should expect if it chooses that configuration::

    (X_BAM, theta_GATK)  ->  expected utility

At inference the controller has a BAM and a candidate config and nothing else. It does not have
truth, mutations, hap.py output, a MINOS score, an evaluation status, or knowledge of what won
last round. Every one of those is listed in :data:`FORBIDDEN_AT_INFERENCE` and asserted against.

THE TARGET FORMULATION — decided, not defaulted
-----------------------------------------------
The frozen objective treats a candidate failure as utility 0.0 *at the aggregation layer*. It
does not follow that a failed GATK run should be handed to a regressor as a biological score of
zero: a run that crashed produced no score at all, and training a score model on a fabricated 0.0
teaches it that certain configurations produce genomically terrible calls when in fact they
produced none. The 35 ``GATK_NONZERO_EXIT`` rows are execution evidence, not biological evidence.

So v1 freezes formulation **B, joint expected utility**::

    P(success | X, theta)                    -- failure-risk model, all 1175 decided rows
    E[score | success, X, theta]             -- score model, the 1140 evaluated rows only
    E[utility] = P(success) * E[score | success] + (1 - P(success)) * FAILURE_UTILITY

with ``FAILURE_UTILITY = 0.0`` taken from the frozen aggregation semantics rather than invented
here. Formulation A (score-only, with failure modelled separately) is the degenerate case of B
without the combination step, so B subsumes it and is what a controller actually needs.

An INFRASTRUCTURE_INCIDENT is never a label in either component. It is our defect, and a model
that learns from it learns about our infrastructure rather than about genomics.

WHAT THIS MODULE DOES NOT DO
----------------------------
It defines no controller, unblocks no ``select_config``, and touches no TEST row. TEST stays
sealed until L2-I.
"""

from __future__ import annotations

from typing import Any, Final

from minos_engine.common.canonical_json import canonical_json_bytes
from minos_engine.common.errors import MinosEngineError
from minos_engine.common.hashing import sha256_hex

__all__ = [
    "BASELINE_QUALIFIED_GATE_HASH",
    "FAILURE_UTILITY",
    "FORBIDDEN_AT_INFERENCE",
    "SAFE_BASELINE_CONFIG_HASH",
    "TARGET_FORMULATION",
    "TRAINING_CONTRACT_DOMAIN",
    "TRAINING_CONTRACT_SCHEMA",
    "TrainingContractError",
    "compute_training_contract_hash",
    "training_contract_content",
]

TRAINING_CONTRACT_SCHEMA: Final = "l2g-training-contract-v1"
TRAINING_CONTRACT_DOMAIN: Final = "minos:l2g-training-contract:v1\n"

# ---- frozen upstream authorities, all verified elsewhere and bound here ---------------------
BASELINE_QUALIFIED_GATE_HASH: Final = (
    "b9436bf3263925ebe187ed5550c7214cfa92bc75a0dd2607a7766103bfa6befa"
)
BASELINE_QUALIFICATION_HASH: Final = (
    "afbcd418dee7f5521dc52b34e2c0b5d7bd31ea5f5d4ec3b1bf0768ab35babee8"
)
BASELINE_SELECTED_HASH: Final = "b13aef13fecf8e966184d03bad5ee0e6f096fb5649b30e336283e2f50f3eba38"
#: the permanent fallback. L2-G never re-optimises it.
SAFE_BASELINE_CONFIG_HASH: Final = (
    "157d88d1587c13be395c62d60e27d1becdada78fad45e65d883bc1190e51acea"
)
PHASE_D_CLOSURE_HASH: Final = "b3f3a0f6281d0d199a1925bf9c6ca91843256f33646d57f10d845f9bf629100b"
BASELINE_PROTOCOL_HASH: Final = "c548e190571f5e964560cf30021a520ea8aad6674569fa3202af880d7dff77d1"
SCORING_CONTRACT_HASH: Final = "b24a07e208ce8e2fff6672102ae4e61aed93c6f352a5af46ba81c4789adb76d6"
EXECUTION_ENVIRONMENT_HASH: Final = (
    "71e14a49833ac77bb9dc576345fb89c4dd68f4a3ad3673eb098d38593c1ef4d3"
)
MINOS_SUBNET_SHA: Final = "649bb92c6abccebde58a736a2b2af7fd77a701c1"
PARAMETER_SPACE_HASH: Final = "b2d401918084d64023305d9262baf5011a89fe517bee4e0bd33af79fb14aee2e"

TARGET_FORMULATION: Final = "B_JOINT_EXPECTED_UTILITY"

#: from the frozen aggregation semantics, not invented here.
FAILURE_UTILITY: Final = 0.0

#: bounded candidate failure codes that ARE valid negative labels for the failure-risk model.
CANDIDATE_FAILURE_LABELS: Final[tuple[str, ...]] = (
    "GATK_NONZERO_EXIT",
    "GATK_TIMEOUT",
    "GATK_OUTPUT_INVALID",
    "GATK_OUTPUT_MISSING",
)

#: anything the model must never require when the controller asks it a question.
FORBIDDEN_AT_INFERENCE: Final[tuple[str, ...]] = (
    "truth_vcf",
    "mutations_vcf",
    "truth_identity",
    "happy_output",
    "minos_score",
    "minos_score_100",
    "admitted",
    "admission_code",
    "evaluation_hash",
    "evaluation_status",
    "winner_config",
    "previous_winning_config",
    "dataset_id",
    "round_id",
    "partition",
    "test_identity",
)

#: the 50 TRAIN BAMs are the only development pool. VALIDATION is selection-only; TEST is sealed.
TRAIN_BAM_COUNT: Final = 50
VALIDATION_BAM_COUNT: Final = 10
CV_FOLD_CHROMOSOMES: Final[tuple[str, ...]] = ("chr18", "chr19", "chr20", "chr21", "chr22")


class TrainingContractError(MinosEngineError):
    """The training contract is violated — a leak, a forbidden input, or an invented label."""


def training_contract_content() -> dict[str, Any]:
    """Exactly what ``training_contract_hash`` covers."""
    return {
        "baseline_protocol_hash": BASELINE_PROTOCOL_HASH,
        "baseline_qualification_hash": BASELINE_QUALIFICATION_HASH,
        "baseline_qualified_gate_hash": BASELINE_QUALIFIED_GATE_HASH,
        "baseline_selected_hash": BASELINE_SELECTED_HASH,
        "candidate_failure_labels": list(CANDIDATE_FAILURE_LABELS),
        "cv_fold_chromosomes": list(CV_FOLD_CHROMOSOMES),
        "cv_protocol": "BAM_GROUPED_CHROMOSOME_HELD_OUT",
        "execution_environment_hash": EXECUTION_ENVIRONMENT_HASH,
        "failure_utility": FAILURE_UTILITY,
        "forbidden_at_inference": list(FORBIDDEN_AT_INFERENCE),
        "infrastructure_incidents_are_labels": False,
        "minos_subnet_sha": MINOS_SUBNET_SHA,
        "parameter_space_hash": PARAMETER_SPACE_HASH,
        "phase_d_closure_hash": PHASE_D_CLOSURE_HASH,
        "safe_baseline_config_hash": SAFE_BASELINE_CONFIG_HASH,
        "schema_version": TRAINING_CONTRACT_SCHEMA,
        "scoring_contract_hash": SCORING_CONTRACT_HASH,
        "target_formulation": TARGET_FORMULATION,
        "train_bam_count": TRAIN_BAM_COUNT,
        "validation_bam_count": VALIDATION_BAM_COUNT,
        "validation_use": "MODEL_SELECTION_ONLY_AFTER_CANDIDATES_FROZEN",
        "test_use": "SEALED_UNTIL_L2_I",
    }


def compute_training_contract_hash() -> str:
    """The domain-separated identity of the frozen training contract."""
    return sha256_hex(
        TRAINING_CONTRACT_DOMAIN.encode("utf-8") + canonical_json_bytes(training_contract_content())
    )
