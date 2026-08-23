"""Deterministic typed-failure classification inventory for the L2-F F5/F6 execution boundary.

Every public execution outcome is classified exactly once: which typed exception surfaces, which
bounded failure code (if any) reaches the database, the state the job was in, the required final
database state, whether an outcome row exists, whether published artifacts are retained, whether
the commit outcome is known or ambiguous — and, always, that automatic retry is forbidden.

The inventory is CLOSED against the implementation: :func:`verify_failure_inventory` re-derives
the set of typed execution exceptions actually exported by the F5/F6 boundary and fails if any is
unclassified, if a case is classified twice, or if any structural rule is violated. No free-text
failure payload is ever introduced here — only the bounded ``L2F_EXECUTION_FAILURE_CODES``.
"""

from __future__ import annotations

from minos_engine.common.errors import MinosEngineError
from minos_engine.qualification.l2f_harness_ready_contract import (
    FailureClassificationEntry,
    FailureClassificationInventory,
)
from minos_engine.storage import l2f_execution as EX
from minos_engine.storage.l2f_execution_contract import L2F_EXECUTION_FAILURE_CODES

__all__ = [
    "FailureInventoryError",
    "FAILURE_CLASSIFICATION",
    "implemented_execution_exceptions",
    "build_failure_inventory",
    "verify_failure_inventory",
]

_PENDING = "PENDING"
_CLAIMED = "CLAIMED"
_RUNNING = "RUNNING"
_SUCCEEDED = "SUCCEEDED"
_FAILED = "FAILED"
_UNKNOWN = "UNKNOWN"
#: no job was ever claimed, so there is no job state to restore.
_NO_JOB = "NO_JOB"


class FailureInventoryError(MinosEngineError):
    """The typed-failure inventory is incomplete, ambiguous or structurally wrong."""


#: The complete classification. Order is fixed, so the inventory serializes deterministically.
FAILURE_CLASSIFICATION: tuple[FailureClassificationEntry, ...] = (
    FailureClassificationEntry(
        case="successful_execution",
        exception_type="None",
        failure_code=None,
        state_before_failure=_RUNNING,
        required_final_state=_SUCCEEDED,
        outcome_row_exists=True,
        artifacts_retained=True,
        commit_outcome="known",
    ),
    FailureClassificationEntry(
        case="worker_identity_rejected_before_any_access",
        exception_type="InvalidWorkerIdError",
        failure_code=None,
        state_before_failure=_NO_JOB,
        required_final_state=_NO_JOB,
        outcome_row_exists=False,
        artifacts_retained=False,
        commit_outcome="known",
    ),
    FailureClassificationEntry(
        case="claim_commit_ambiguous",
        exception_type="AmbiguousClaimCommitError",
        failure_code=None,
        state_before_failure=_PENDING,
        required_final_state=_UNKNOWN,
        outcome_row_exists=False,
        artifacts_retained=False,
        commit_outcome="ambiguous",
    ),
    FailureClassificationEntry(
        case="preparation_failed_while_claimed",
        exception_type="PreTerminalExecutionError",
        failure_code=None,
        state_before_failure=_CLAIMED,
        required_final_state=_PENDING,
        outcome_row_exists=False,
        artifacts_retained=False,
        commit_outcome="known",
    ),
    FailureClassificationEntry(
        case="release_commit_ambiguous",
        exception_type="AmbiguousRecoveryCommitError",
        failure_code=None,
        state_before_failure=_CLAIMED,
        required_final_state=_UNKNOWN,
        outcome_row_exists=False,
        artifacts_retained=False,
        commit_outcome="ambiguous",
    ),
    FailureClassificationEntry(
        case="release_itself_failed",
        exception_type="ExecutionRecoveryError",
        failure_code=None,
        state_before_failure=_CLAIMED,
        required_final_state=_UNKNOWN,
        outcome_row_exists=False,
        artifacts_retained=False,
        commit_outcome="known",
    ),
    FailureClassificationEntry(
        case="start_commit_ambiguous",
        exception_type="AmbiguousStartCommitError",
        failure_code=None,
        state_before_failure=_CLAIMED,
        required_final_state=_UNKNOWN,
        outcome_row_exists=False,
        artifacts_retained=False,
        commit_outcome="ambiguous",
    ),
    FailureClassificationEntry(
        case="gatk_nonzero_exit",
        exception_type="GatkExecutionError",
        failure_code="GATK_NONZERO_EXIT",
        state_before_failure=_RUNNING,
        required_final_state=_FAILED,
        outcome_row_exists=True,
        artifacts_retained=False,
        commit_outcome="known",
    ),
    FailureClassificationEntry(
        case="gatk_timeout",
        exception_type="GatkTimeoutError",
        failure_code="GATK_TIMEOUT",
        state_before_failure=_RUNNING,
        required_final_state=_FAILED,
        outcome_row_exists=True,
        artifacts_retained=False,
        commit_outcome="known",
    ),
    FailureClassificationEntry(
        case="gatk_output_invalid",
        exception_type="GatkOutputError",
        failure_code="GATK_OUTPUT_INVALID",
        state_before_failure=_RUNNING,
        required_final_state=_FAILED,
        outcome_row_exists=True,
        artifacts_retained=False,
        commit_outcome="known",
    ),
    FailureClassificationEntry(
        case="workspace_or_invocation_failed_after_running",
        exception_type="ExecutionRecordedFailureError",
        failure_code="EXECUTION_ERROR",
        state_before_failure=_RUNNING,
        required_final_state=_FAILED,
        outcome_row_exists=True,
        artifacts_retained=False,
        commit_outcome="known",
    ),
    FailureClassificationEntry(
        case="workspace_refused_before_running",
        exception_type="ExecutionWorkspaceError",
        failure_code="EXECUTION_ERROR",
        state_before_failure=_RUNNING,
        required_final_state=_FAILED,
        outcome_row_exists=True,
        artifacts_retained=False,
        commit_outcome="known",
    ),
    FailureClassificationEntry(
        case="gatk_invocation_could_not_be_built",
        exception_type="GatkInvocationError",
        failure_code="EXECUTION_ERROR",
        state_before_failure=_RUNNING,
        required_final_state=_FAILED,
        outcome_row_exists=True,
        artifacts_retained=False,
        commit_outcome="known",
    ),
    FailureClassificationEntry(
        case="input_resolution_failed",
        exception_type="InputResolutionError",
        failure_code=None,
        state_before_failure=_CLAIMED,
        required_final_state=_PENDING,
        outcome_row_exists=False,
        artifacts_retained=False,
        commit_outcome="known",
    ),
    FailureClassificationEntry(
        case="config_artifact_rejected",
        exception_type="ConfigArtifactError",
        failure_code=None,
        state_before_failure=_CLAIMED,
        required_final_state=_PENDING,
        outcome_row_exists=False,
        artifacts_retained=False,
        commit_outcome="known",
    ),
    FailureClassificationEntry(
        case="terminal_commit_ambiguous",
        exception_type="AmbiguousExecutionCommitError",
        failure_code=None,
        state_before_failure=_RUNNING,
        required_final_state=_UNKNOWN,
        outcome_row_exists=False,
        artifacts_retained=True,
        commit_outcome="ambiguous",
    ),
    FailureClassificationEntry(
        case="wrapper_failed_after_confirmed_commit",
        exception_type="PostCommitWrapperError",
        failure_code=None,
        state_before_failure=_RUNNING,
        required_final_state=_SUCCEEDED,
        outcome_row_exists=True,
        artifacts_retained=True,
        commit_outcome="known",
    ),
    FailureClassificationEntry(
        case="conflicting_durable_outcome_exists",
        exception_type="ExecutionResultConflictError",
        failure_code=None,
        state_before_failure=_RUNNING,
        required_final_state=_SUCCEEDED,
        outcome_row_exists=True,
        artifacts_retained=True,
        commit_outcome="known",
    ),
    FailureClassificationEntry(
        case="invalid_transition_requested",
        exception_type="InvalidJobTransitionError",
        failure_code=None,
        state_before_failure=_RUNNING,
        required_final_state=_UNKNOWN,
        outcome_row_exists=False,
        artifacts_retained=False,
        commit_outcome="known",
    ),
    FailureClassificationEntry(
        case="accepted_plan_graph_absent",
        exception_type="JobPlanMissingError",
        failure_code=None,
        state_before_failure=_CLAIMED,
        required_final_state=_PENDING,
        outcome_row_exists=False,
        artifacts_retained=False,
        commit_outcome="known",
    ),
)

#: the typed execution exceptions the F5/F6 boundary actually exports, which the inventory closes
#: over. ``L2FExecutionError`` is the abstract base and is deliberately not a classifiable case.
_ABSTRACT_BASES = frozenset({"L2FExecutionError", "JobClaimError", "MinosEngineError"})


def implemented_execution_exceptions() -> frozenset[str]:
    """Re-derive the typed execution exceptions exported by the live F5/F6 boundary."""
    from minos_engine.experiments import execution_contract as EC
    from minos_engine.storage import l2f_job_claim as JC

    found: set[str] = set()
    for module, names in ((EX, EX.__all__), (EC, EC.__all__), (JC, JC.__all__)):
        for name in names:
            obj = getattr(module, name, None)
            if (
                isinstance(obj, type)
                and issubclass(obj, BaseException)
                and name not in _ABSTRACT_BASES
            ):
                found.add(name)
    return frozenset(found)


def build_failure_inventory() -> FailureClassificationInventory:
    """Assemble the inventory and CLOSE it against the live implementation."""
    implemented = implemented_execution_exceptions()
    classified = [e.exception_type for e in FAILURE_CLASSIFICATION if e.exception_type != "None"]
    unclassified = sorted(implemented - set(classified))
    duplicate_cases = len({e.case for e in FAILURE_CLASSIFICATION}) != len(FAILURE_CLASSIFICATION)
    return FailureClassificationInventory(
        entries=FAILURE_CLASSIFICATION,
        implemented_exception_types=tuple(sorted(implemented)),
        complete=not unclassified,
        unambiguous=not duplicate_cases,
    )


def verify_failure_inventory(inventory: FailureClassificationInventory) -> None:
    """Fail closed unless the inventory is complete, unambiguous and structurally correct."""
    implemented = implemented_execution_exceptions()
    classified = {e.exception_type for e in inventory.entries if e.exception_type != "None"}
    missing = sorted(implemented - classified)
    if missing:
        raise FailureInventoryError(f"implemented typed exceptions are unclassified: {missing}")
    cases = [e.case for e in inventory.entries]
    if len(set(cases)) != len(cases):
        raise FailureInventoryError("a case is classified more than once")

    for entry in inventory.entries:
        if entry.automatic_retry_allowed is not False:  # pragma: no cover - Literal[False]
            raise FailureInventoryError(f"{entry.case}: automatic retry may never be allowed")
        if entry.failure_code is not None and entry.failure_code not in L2F_EXECUTION_FAILURE_CODES:
            raise FailureInventoryError(
                f"{entry.case}: {entry.failure_code!r} is not a bounded failure code"
            )
        # a NON-ambiguous failure from RUNNING must leave a durable FAILED row.
        if (
            entry.state_before_failure == _RUNNING
            and entry.required_final_state == _FAILED
            and entry.commit_outcome == "known"
            and not entry.outcome_row_exists
        ):
            raise FailureInventoryError(
                f"{entry.case}: a non-ambiguous RUNNING failure must record a durable FAILED row"
            )
        # a preparation failure must return to PENDING.
        if (
            entry.state_before_failure == _CLAIMED
            and entry.commit_outcome == "known"
            and entry.required_final_state not in {_PENDING, _UNKNOWN}
        ):
            raise FailureInventoryError(
                f"{entry.case}: a preparation failure must return to PENDING"
            )
        # an ambiguous commit may never be reported as a definite success or failure.
        if entry.commit_outcome == "ambiguous" and entry.required_final_state != _UNKNOWN:
            raise FailureInventoryError(
                f"{entry.case}: an ambiguous commit may not claim a definite final state"
            )
        # a post-commit wrapper error must not change the committed result.
        if entry.exception_type == "PostCommitWrapperError" and (
            entry.required_final_state != _SUCCEEDED
            or not entry.outcome_row_exists
            or not entry.artifacts_retained
        ):
            raise FailureInventoryError(
                "PostCommitWrapperError must preserve the committed result and its artifacts"
            )
        # no case may leave a job stranded in CLAIMED or RUNNING.
        if entry.required_final_state in {_CLAIMED, _RUNNING}:
            raise FailureInventoryError(
                f"{entry.case}: a job may never be left stranded in {entry.required_final_state}"
            )
    if not inventory.complete or not inventory.unambiguous:
        raise FailureInventoryError("the inventory reports itself incomplete or ambiguous")
