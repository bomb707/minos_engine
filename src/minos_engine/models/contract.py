"""``l2g-training-contract-v1`` — what L2-G learns, and what it is never allowed to see.

THE LEARNING PROBLEM
--------------------
Given one BAM's production-eligible feature vector and one canonical GATK configuration,
predict the outcome the engine should expect if it chooses that configuration::

    (X_BAM, theta_GATK)  ->  expected utility

At inference the controller has a BAM and a candidate config and nothing else. It does not have
truth, mutations, hap.py output, a MINOS score, an evaluation status, or knowledge of what won
last round. Every one of those is listed in :data:`FORBIDDEN_AT_INFERENCE` and asserted against.

THE TARGET FORMULATION — corrected in v2
----------------------------------------
v1 froze the probability term as ``P(GATK succeeds)`` and let the score regressor consume all
1140 evaluations. **That did not match the frozen baseline objective**, and no model was fitted
under it before the error was found.

``BaselineObservation`` already defines candidate utility precisely:

* GATK succeeds, evaluation succeeds, ``admitted = true`` -> utility is the persisted score;
* GATK succeeds, evaluation succeeds, ``admitted = false`` -> ``minos_score`` is **not consumed**,
  the observation carries ``None``, and utility is 0 — a non-admission is a candidate failure;
* GATK execution fails -> no score, utility 0;
* infrastructure incident -> not a candidate label at all.

So execution success is the wrong event. The real campaign has 986 admitted and **154
non-admitted** evaluations, so v1 would have trained the score regressor on 154 rows whose number
the objective explicitly refuses to treat as utility evidence — about one in eight.

v2 freezes ``A = "produced an ADMITTED evaluation under the frozen scoring contract"``::

    P(A | X, theta)                          -- admission model, EVERY decided candidate outcome
    E[S | A, X, theta]                       -- score model, ADMITTED examples only
    E[U] = P(A) * E[S | A] + (1 - P(A)) * FAILURE_UTILITY

``FAILURE_UTILITY = 0.0`` comes from the frozen aggregation semantics. The admission model's
negatives are non-admissions **and** bounded execution failures alike; the score regressor never
sees either. A non-admission keeps its ``admission_code`` as provenance and is never dressed up
as a bounded GATK failure — the two mean different things.

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
    "BAMS_PER_CHROMOSOME",
    "BASELINE_QUALIFIED_GATE_HASH",
    "DEDUP_POLICY",
    "FEATURE_COLUMN_COUNT",
    "FROZEN_FEATURE_SET_HASH",
    "OUTCOME_ADMITTED",
    "OUTCOME_CLASSES",
    "OUTCOME_EXECUTION_FAILURE",
    "OUTCOME_NON_ADMISSION",
    "WEIGHTING_POLICY",
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

TRAINING_CONTRACT_SCHEMA: Final = "l2g-training-contract-v2"
TRAINING_CONTRACT_DOMAIN: Final = "minos:l2g-training-contract:v2\n"

#: v1 froze P(GATK success) as the probability term. That was wrong -- see the docstring.
#: No model was ever fitted under it, so it is superseded rather than migrated.
SUPERSEDED_CONTRACT_V1: Final = "SUPERSEDED_BEFORE_FIRST_MODEL_FIT"

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

TARGET_FORMULATION: Final = "B_JOINT_EXPECTED_UTILITY_OVER_ADMISSION"

#: from the frozen aggregation semantics, not invented here.
FAILURE_UTILITY: Final = 0.0

#: the three outcome classes a decided candidate can land in. Non-admission and execution
#: failure are BOTH admission-model negatives, and neither is a score-model example.
OUTCOME_ADMITTED: Final = "ADMITTED"
OUTCOME_NON_ADMISSION: Final = "CANDIDATE_NON_ADMISSION"
OUTCOME_EXECUTION_FAILURE: Final = "CANDIDATE_EXECUTION_FAILURE"
OUTCOME_CLASSES: Final[tuple[str, ...]] = (
    OUTCOME_ADMITTED,
    OUTCOME_NON_ADMISSION,
    OUTCOME_EXECUTION_FAILURE,
)

#: one learning example per (BAM, config). The campaign ran some pairs in more than one phase;
#: a scientific cell must not gain loss weight for having been scheduled twice.
DEDUP_POLICY: Final = "ONE_EXAMPLE_PER_BAM_CONFIG_PAIR"

#: the generalisation unit is the BAM, and the campaign is unbalanced (10-80 pairs per BAM).
#: Each BAM therefore contributes the same TOTAL loss weight to each component.
WEIGHTING_POLICY: Final = "EQUAL_BAM_TOTAL"

#: bounded candidate failure codes that ARE valid negative labels for the admission model.
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
#: exactly ten TRAIN BAMs per chromosome. "50 total" alone would admit 46/1/1/1/1.
BAMS_PER_CHROMOSOME: Final = 10

#: THE qualified production feature matrix -- 129 columns, not the wider 141-field registry
#: result. Widening the model input is a feature promotion and needs its own freeze.
FROZEN_FEATURE_SET_HASH: Final = "7e867dfa5633044b69869be8a87fac564431a73a183aa0ab0b1b13158a7c176f"
FEATURE_COLUMN_COUNT: Final = 129


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
        "dedup_policy": DEDUP_POLICY,
        "cv_fold_chromosomes": list(CV_FOLD_CHROMOSOMES),
        "cv_protocol": "BAM_GROUPED_CHROMOSOME_HELD_OUT",
        "execution_environment_hash": EXECUTION_ENVIRONMENT_HASH,
        "failure_utility": FAILURE_UTILITY,
        "feature_set_hash": FROZEN_FEATURE_SET_HASH,
        "feature_column_count": FEATURE_COLUMN_COUNT,
        "forbidden_at_inference": list(FORBIDDEN_AT_INFERENCE),
        "infrastructure_incidents_are_labels": False,
        "minos_subnet_sha": MINOS_SUBNET_SHA,
        "parameter_space_hash": PARAMETER_SPACE_HASH,
        "phase_d_closure_hash": PHASE_D_CLOSURE_HASH,
        "safe_baseline_config_hash": SAFE_BASELINE_CONFIG_HASH,
        "schema_version": TRAINING_CONTRACT_SCHEMA,
        "scoring_contract_hash": SCORING_CONTRACT_HASH,
        "target_formulation": TARGET_FORMULATION,
        "outcome_classes": list(OUTCOME_CLASSES),
        "score_model_examples": "ADMITTED_ONLY",
        "admission_model_examples": "EVERY_DECIDED_OUTCOME",
        "superseded": {"l2g-training-contract-v1": SUPERSEDED_CONTRACT_V1},
        "train_bam_count": TRAIN_BAM_COUNT,
        "weighting_policy": WEIGHTING_POLICY,
        "validation_bam_count": VALIDATION_BAM_COUNT,
        "validation_use": "MODEL_SELECTION_ONLY_AFTER_CANDIDATES_FROZEN",
        "test_use": "SEALED_UNTIL_L2_I",
    }


def compute_training_contract_hash() -> str:
    """The domain-separated identity of the frozen training contract."""
    return sha256_hex(
        TRAINING_CONTRACT_DOMAIN.encode("utf-8") + canonical_json_bytes(training_contract_content())
    )
