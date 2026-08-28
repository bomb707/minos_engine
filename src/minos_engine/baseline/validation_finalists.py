"""The four validation finalists and their TRAIN-side identity.

L2-F2-F must be able to prove that these four configurations were frozen BEFORE any validation
truth was reachable. A promise is not proof; a hash computed entirely inside the TRAIN closure is.
So this identity binds the protocol, the Phase-C plan and candidate set, the complete TRAIN result
the ranking came from, the four hashes in promotion order and — because it is the frozen tie-break
key — each finalist's inherited Phase-B design index.

It contains no validation observation of any kind, and it cannot: everything it binds exists
before validation begins. That is what makes it evidence rather than an assertion.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from minos_engine.common.canonical_json import canonical_json_bytes
from minos_engine.common.errors import MinosEngineError
from minos_engine.common.hashing import sha256_hex

if TYPE_CHECKING:
    from sqlalchemy import Engine

    from minos_engine.baseline.objective import BaselineObservation

__all__ = [
    "VALIDATION_FINALISTS_DOMAIN",
    "VALIDATION_FINALISTS_SCHEMA",
    "ValidationFinalistError",
    "ValidationFinalistSet",
    "compute_validation_finalist_set_hash",
    "derive_validation_finalist_set",
]

VALIDATION_FINALISTS_SCHEMA = "l2f2-validation-finalists-v1"
VALIDATION_FINALISTS_DOMAIN = "minos:l2f2-validation-finalists:v1\n"


class ValidationFinalistError(MinosEngineError):
    """The four validation finalists cannot be frozen from the state Phase C is in."""


@dataclass(frozen=True)
class ValidationFinalistSet:
    """Exactly four configurations, frozen inside the TRAIN closure, seed always among them."""

    ordered_config_hashes: tuple[str, ...]
    inherited_candidate_index: dict[str, int]
    seed_config_hash: str
    phase_c_plan_hash: str
    phase_c_candidate_set_hash: str
    phase_c_result_hash: str
    finalist_set_hash: str


def _observation_content(observation: BaselineObservation) -> dict[str, Any]:
    return {
        "config_hash": observation.config_hash,
        "dataset_id": observation.dataset_id,
        "chromosome": observation.chromosome,
        "outcome": observation.outcome,
        "admitted": observation.admitted,
        "minos_score": observation.minos_score,
        "failure_code": observation.failure_code,
        "gatk_runtime_ms": observation.gatk_runtime_ms,
    }


def compute_validation_finalist_set_hash(
    *,
    protocol_hash: str,
    phase_c_plan_hash: str,
    phase_c_candidate_set_hash: str,
    phase_c_result_hash: str,
    ordered_config_hashes: tuple[str, ...],
    inherited_candidate_index: dict[str, int],
    seed_config_hash: str,
) -> str:
    """The domain-separated identity of the four finalists. TRAIN-only by construction."""
    content = {
        "schema_version": VALIDATION_FINALISTS_SCHEMA,
        "baseline_protocol_hash": protocol_hash,
        "phase_c_plan_hash": phase_c_plan_hash,
        "phase_c_candidate_set_hash": phase_c_candidate_set_hash,
        "phase_c_train_result_hash": phase_c_result_hash,
        "seed_config_hash": seed_config_hash,
        "finalist_count": len(ordered_config_hashes),
        "ordered_config_hashes": list(ordered_config_hashes),
        "inherited_phase_b_candidate_index": [
            {"config_hash": h, "phase_b_candidate_index": inherited_candidate_index[h]}
            for h in ordered_config_hashes
        ],
    }
    return sha256_hex(VALIDATION_FINALISTS_DOMAIN.encode("utf-8") + canonical_json_bytes(content))


def derive_validation_finalist_set(engine: Engine) -> ValidationFinalistSet:
    """Freeze the four finalists from a TRAIN-complete Phase C. No caller may supply anything."""
    from minos_engine.baseline.phase_c import build_l2f2_phase_c_authority
    from minos_engine.baseline.phase_c_observations import load_phase_c_observations
    from minos_engine.baseline.racing import VALIDATION_FINALIST_COUNT
    from minos_engine.storage.l2f2_phase_c_control import select_l2f2_validation_finalists

    authority = build_l2f2_phase_c_authority(engine)
    finalists = select_l2f2_validation_finalists(engine)
    if len(finalists) != VALIDATION_FINALIST_COUNT or len(set(finalists)) != len(finalists):
        raise ValidationFinalistError(
            f"the promotion returned {len(finalists)} configurations, expected "
            f"{VALIDATION_FINALIST_COUNT} distinct ones"
        )
    if authority.seed_config_hash not in finalists:
        raise ValidationFinalistError("the seed is absent from the finalist set")

    snapshot = load_phase_c_observations(engine, authority=authority)
    ordered = sorted(
        (_observation_content(o) for o in snapshot.observations),
        key=lambda row: (row["config_hash"], row["dataset_id"]),
    )
    result_hash = sha256_hex(
        VALIDATION_FINALISTS_DOMAIN.encode("utf-8")
        + canonical_json_bytes(
            {
                "phase_c_plan_hash": authority.plan_hash,
                "execution_environment_hash": authority.execution_environment_hash,
                "observation_count": len(ordered),
                "observations": ordered,
            }
        )
    )
    inherited = {h: authority.inherited_candidate_index[h] for h in finalists}
    return ValidationFinalistSet(
        ordered_config_hashes=finalists,
        inherited_candidate_index=inherited,
        seed_config_hash=authority.seed_config_hash,
        phase_c_plan_hash=authority.plan_hash,
        phase_c_candidate_set_hash=authority.phase_c_candidate_set_hash,
        phase_c_result_hash=result_hash,
        finalist_set_hash=compute_validation_finalist_set_hash(
            protocol_hash=authority.baseline_protocol_hash,
            phase_c_plan_hash=authority.plan_hash,
            phase_c_candidate_set_hash=authority.phase_c_candidate_set_hash,
            phase_c_result_hash=result_hash,
            ordered_config_hashes=finalists,
            inherited_candidate_index=inherited,
            seed_config_hash=authority.seed_config_hash,
        ),
    )
