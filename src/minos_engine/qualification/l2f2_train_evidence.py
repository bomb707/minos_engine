"""The TRAIN evidence set the BASELINE-QUALIFIED gate binds, defined exactly.

§13 asks for *"every TRAIN evaluation required by the protocol (by ``evaluation_hash``)"*, and
§12 rule 1 makes clear what "required" means: a candidate raced out part-way through a phase
legitimately stops there, and *"its unseen remainder is never fabricated to complete its
aggregate."* So the required set is not "every pair the parameter space could have produced" — it
is every LOGICAL JOB the three TRAIN plans actually materialised, each of which must be terminal.

For this campaign the two coincide: each plan's job count equals its ``logical_job_count``, so no
racing reduction happened at the job level and there is no unexecuted remainder to reason about.
That is a fact about this campaign, checked rather than assumed, and it is why the completeness
rule can be stated so simply here:

* every logical job is terminal — SUCCEEDED or FAILED, none pending, claimed or running;
* every SUCCEEDED execution carries exactly one evaluation under the frozen scoring contract;
* every FAILED execution carries a bounded CANDIDATE failure code;
* no evaluation failure exists, and no INFRASTRUCTURE_INCIDENT of any kind;
* one scoring contract and one execution environment across the whole campaign.

A missing evaluation is never read as a zero, and a failed one is never silently dropped: both
are refusals.
"""

from __future__ import annotations

from typing import Any, Final

from minos_engine.common.errors import MinosEngineError

__all__ = [
    "TRAIN_CANDIDATE_FAILURE_COUNT",
    "TRAIN_EVALUATION_COUNT",
    "TRAIN_EVALUATION_SET_SHA256",
    "TRAIN_EXECUTION_FAILURE_SET_SHA256",
    "TRAIN_LOGICAL_JOB_COUNT",
    "TRAIN_PLAN_HASHES",
    "TrainEvidenceError",
    "verify_train_evidence",
]

TRAIN_DATABASE: Final = "minos_l2f2_baseline"
TRAIN_REVISION: Final = "0020_l2f2_phase_c_execution"

#: Phase A (5x39), Phase B (10x48), Phase C (50x10), in creation order.
TRAIN_PLAN_HASHES: Final[tuple[str, ...]] = (
    "97ba598778a5fc634345ded0901e4975af9c6b875c5b70fc7e76f2ae482e1b9a",
    "e80594043580334ddf2504577e2fa030dff0c1217ac334804d9304a0ec72596b",
    "03b846e735e5817a8df7d5c37ae15778a955828a56513b16cef8ff2193a0aa43",
)
TRAIN_LOGICAL_JOB_COUNT: Final = 1175
TRAIN_EVALUATION_COUNT: Final = 1140
TRAIN_CANDIDATE_FAILURE_COUNT: Final = 35

#: sha256 over the comma-joined, lexicographically sorted evaluation_hash set. Order-independent
#: by construction, so it identifies the SET rather than any traversal of it.
TRAIN_EVALUATION_SET_SHA256: Final = (
    "37a8f73a585fe36e50b82b7217acf9c52a1d8c99e7031754b4eb686803c32e68"
)
#: the same, over the failed jobs' job_key set — so a dropped failure is as visible as a dropped
#: success.
TRAIN_EXECUTION_FAILURE_SET_SHA256: Final = (
    "c4e3f8b76e2f5e2cbce30d55a2d7497ad4a6054a3431fa88487b3b695433754f"
)

SCORING_CONTRACT_HASH: Final = "b24a07e208ce8e2fff6672102ae4e61aed93c6f352a5af46ba81c4789adb76d6"
EXECUTION_ENVIRONMENT_HASH: Final = (
    "71e14a49833ac77bb9dc576345fb89c4dd68f4a3ad3673eb098d38593c1ef4d3"
)


class TrainEvidenceError(MinosEngineError):
    """The TRAIN evidence set is incomplete, contradicted, or not reconstructible."""


def verify_train_evidence(observed: dict[str, Any]) -> dict[str, bool]:
    """Check an observed TRAIN summary against the frozen evidence identity.

    ``observed`` is a plain summary — counts, set digests, failure classification — read from the
    immutable TRAIN ledgers by the caller. This function performs no I/O so it can be exercised
    against synthetic summaries without a database.
    """
    from minos_engine.baseline.objective import classify_failure_code

    checks: dict[str, bool] = {}
    checks["train_revision_exact"] = observed.get("revision") == TRAIN_REVISION
    checks["train_plans_exact"] = list(observed.get("plan_hashes", ())) == list(TRAIN_PLAN_HASHES)
    checks["train_every_logical_job_terminal"] = (
        observed.get("logical_job_count") == TRAIN_LOGICAL_JOB_COUNT
        and observed.get("terminal_job_count") == TRAIN_LOGICAL_JOB_COUNT
        and observed.get("nonterminal_job_count") == 0
    )
    checks["train_every_success_evaluated"] = (
        observed.get("succeeded_without_evaluation") == 0
        and observed.get("evaluation_count") == TRAIN_EVALUATION_COUNT
    )
    checks["train_evaluation_set_exact"] = (
        observed.get("evaluation_set_sha256") == TRAIN_EVALUATION_SET_SHA256
    )
    checks["train_execution_failure_set_exact"] = (
        observed.get("execution_failure_set_sha256") == TRAIN_EXECUTION_FAILURE_SET_SHA256
    )

    codes = dict(observed.get("execution_failure_codes", {}))
    try:
        classified = {code: classify_failure_code(code) for code in codes}
    except Exception:  # an unknown code is refused, never guessed at
        classified = {}
        checks["train_failure_codes_bounded"] = False
    else:
        checks["train_failure_codes_bounded"] = bool(codes)
    checks["train_no_infrastructure_incident"] = (
        all(outcome == "CANDIDATE_FAILURE" for outcome in classified.values())
        and observed.get("evaluation_failure_count") == 0
        and sum(codes.values()) == TRAIN_CANDIDATE_FAILURE_COUNT
    )
    checks["train_no_failed_evaluation_silently_ignored"] = (
        observed.get("evaluation_failure_count") == 0
        and sum(codes.values()) + observed.get("evaluation_count", -1) == TRAIN_LOGICAL_JOB_COUNT
    )
    checks["train_single_scoring_contract"] = (
        observed.get("distinct_scoring_contracts") == 1
        and observed.get("scoring_contract_hash") == SCORING_CONTRACT_HASH
    )
    checks["train_single_execution_environment"] = (
        observed.get("distinct_execution_environments") == 1
        and observed.get("execution_environment_hash") == EXECUTION_ENVIRONMENT_HASH
    )
    return checks
