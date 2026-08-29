"""THE production path that evaluates one completed VALIDATION execution.

This is a thin, named seam over :func:`minos_engine.evaluation.orchestrator.evaluate_execution` —
deliberately thin, because the one thing L2-F2-F must not do is score differently from L2-F2-E. The
finalists were chosen by a specific scorer, pinned to a specific MINOS_SUBNET commit and two
specific container digests; a validation stage that recomputed the score its own way would not be
confirming the search, it would be running a second, incomparable one.

So there is no scoring logic here. What this module adds is exactly two things:

* the **VALIDATION partition gate**, applied in the same place the TRAIN gate is applied — after
  the execution row is resolved and before any truth path is constructed, opened or hashed. TRAIN
  is refused as firmly as TEST: a validation ranking computed over the search's own TRAIN evidence
  would be worthless, and TEST stays sealed until L2-I;
* a **named entry**, so a caller picks a stage rather than passing a partition. There is no
  argument on this function by which the wrong partition could be admitted.

Failure semantics are inherited unchanged, because they are already right: a candidate execution
failure and a validator non-admission are scientific observations about a finalist and the campaign
continues past them; a bounded evaluation failure is OUR infrastructure incident and the campaign
holds. No fabricated zero score is ever persisted for a candidate failure — a zero utility exists
only inside the committed aggregate, never as a recorded MINOS score.
"""

from __future__ import annotations

from typing import Any

from minos_engine.evaluation.artifact_publisher import EvaluationArtifactPublisher
from minos_engine.evaluation.minos_subnet_oracle import MinosSubnetOracle
from minos_engine.evaluation.orchestrator import (
    EvaluationOutcome,
    EvaluationProvisioning,
    evaluate_execution,
)
from minos_engine.evaluation.scoring_contract import ScoringAuthority
from minos_engine.evaluation.truth_registration import refuse_non_validation_partition

__all__ = ["evaluate_validation_execution"]


def evaluate_validation_execution(
    engine: Any,
    *,
    execution_result_id: str,
    authority: ScoringAuthority,
    oracle: MinosSubnetOracle,
    publisher: EvaluationArtifactPublisher,
    provisioning: EvaluationProvisioning,
) -> EvaluationOutcome:
    """Evaluate ONE completed VALIDATION execution with the EXACT pinned scorer.

    Identical to the TRAIN path in every respect except which partition it admits. Raises when the
    execution is unknown, or when its partition is anything other than ``validation`` — including
    ``train``.
    """
    return evaluate_execution(
        engine,
        execution_result_id=execution_result_id,
        authority=authority,
        oracle=oracle,
        publisher=publisher,
        provisioning=provisioning,
        partition_gate=refuse_non_validation_partition,
    )
