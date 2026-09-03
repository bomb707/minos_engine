"""``l2f2-baseline-selected-v1`` — the RESULT of Phase D, frozen.

This is not a decision-rule manifest. Every rule that produced it was already fixed: the objective
and total order in ``l2f2-baseline-search-protocol-v1``, and the final-selection rule in
``docs/layer2/BASELINE_QUALIFICATION.md`` §12 rule 4 — *"The baseline is the best finalist on
VALIDATION under the same frozen J"* — which was committed long before the first validation score
was read. ``l2f2-phase-d-selection-interpretation-v1`` (``4c169912…``) added hash-bound authority
to that already-documented rule; it did not invent it.

What this module freezes is the OUTCOME those rules produced, and its whole job is to make that
outcome impossible to restate incorrectly. Nothing here is a fresh judgement: every field is
verified against the canonical Phase-D closure artifact before it is accepted, so a hand-copied
winner that disagrees with the closure is refused rather than believed.

``§12 rule 5`` is worth stating plainly, because it applies here: *"If VALIDATION contradicts
TRAIN, that is recorded, not re-optimised."* The selected baseline is not the seed, and the seed
ranked last. That is recorded as the result. It is not a reason to revisit anything.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Final

from minos_engine.common.canonical_json import canonical_json_bytes
from minos_engine.common.errors import MinosEngineError
from minos_engine.common.hashing import sha256_hex

__all__ = [
    "BASELINE_SELECTED_DOMAIN",
    "BASELINE_SELECTED_MANIFEST",
    "BASELINE_SELECTED_SCHEMA",
    "PHASE_D_CLOSURE_ARTIFACT_SHA256",
    "PHASE_D_CLOSURE_EVIDENCE_SHA256",
    "PHASE_D_CLOSURE_HASH",
    "SELECTED_CONFIG_HASH",
    "BaselineSelectedError",
    "baseline_selected_content",
    "compute_baseline_selected_hash",
    "load_committed_baseline_selected",
    "verify_closure_artifact",
]

BASELINE_SELECTED_SCHEMA: Final = "l2f2-baseline-selected-v1"
BASELINE_SELECTED_DOMAIN: Final = "minos:l2f2-baseline-selected:v1\n"
BASELINE_SELECTED_MANIFEST: Final = "manifests/l2f2_baseline_selected_v1.json"

BASELINE_PROTOCOL_HASH: Final = "c548e190571f5e964560cf30021a520ea8aad6674569fa3202af880d7dff77d1"
SELECTION_INTERPRETATION_HASH: Final = (
    "4c169912f67877d6ba254fb280dbd2ff44aa4aaaf65bedfa1bca9975f1efebbd"
)
PHASE_D_PLAN_HASH: Final = "f6bd1e450c38d789dcfcdafaaf357dad2f7602f53fc8ec779c5be40c71e6d7ce"
PHASE_D_CLOSURE_HASH: Final = "b3f3a0f6281d0d199a1925bf9c6ca91843256f33646d57f10d845f9bf629100b"
PHASE_D_CLOSURE_ARTIFACT_SHA256: Final = (
    "4eaf622baa5755829e936588003277aa277b9d999db089ddc2c94adae4bb9f89"
)
PHASE_D_CLOSURE_EVIDENCE_SHA256: Final = (
    "90f0f53577c78ded8e876cad35ed30e4ba0ba784316635a0d424aebee2f6bb24"
)
FINALIST_FREEZE_SHA256: Final = "540aeca0640871ca91e3ec771ec66d2df4b96d38210ec3265f944dee3e0433f3"
PHASE_C_CLOSURE_SHA256: Final = "5de368eec327b66c868737d1819cc1b1a590eaf185b28e53d1cfecae59b593ca"
SCORING_CONTRACT_HASH: Final = "b24a07e208ce8e2fff6672102ae4e61aed93c6f352a5af46ba81c4789adb76d6"
EXECUTION_ENVIRONMENT_HASH: Final = (
    "71e14a49833ac77bb9dc576345fb89c4dd68f4a3ad3673eb098d38593c1ef4d3"
)
MINOS_SUBNET_SHA: Final = "649bb92c6abccebde58a736a2b2af7fd77a701c1"

#: THE result. Non-seed: validation selected a different configuration than the one in use today.
SELECTED_CONFIG_HASH: Final = "157d88d1587c13be395c62d60e27d1becdada78fad45e65d883bc1190e51acea"
SELECTED_INHERITED_CANDIDATE_INDEX: Final = 42
SELECTED_RANK: Final = 0
SEED_CONFIG_HASH: Final = "4251cb85e5cd58b7eabfe530b9df23ea7d1d14fd882114b488d67cbd81b751b8"
SEED_RANK: Final = 3

#: best-first, exactly as the closure ordered them.
ORDERED_RANKING: Final[tuple[str, ...]] = (
    "157d88d1587c13be395c62d60e27d1becdada78fad45e65d883bc1190e51acea",
    "22a1f1fd9ddf02a97776d991f11280b3982673693a4f357479098a99fb411a16",
    "0972930f8d8c562be15382203e123b2909094e7eac46e84321d36c67abf8345e",
    "4251cb85e5cd58b7eabfe530b9df23ea7d1d14fd882114b488d67cbd81b751b8",
)

#: the winner's robust statistics, exactly as the closure computed them.
SELECTED_CVAR: Final = 0.6323350350370124
SELECTED_FLOOR: Final = 0.6214557122587683
SELECTED_MEAN: Final = 0.7232714749391697
SELECTED_FAILURE_RATE: Final = 0.0
SELECTED_CANDIDATE_FAILURE_COUNT: Final = 0
SELECTED_MEAN_GATK_RUNTIME_MS: Final = 67065.1
SELECTED_OBJECTIVE: Final = 0.6472585261839707


class BaselineSelectedError(MinosEngineError):
    """The frozen result is absent, altered, or contradicted by the closure."""


def baseline_selected_content() -> dict[str, Any]:
    """Exactly what ``baseline_selected_hash`` covers.

    Scientific identities and the outcome. No timestamp, hostname, database URL, operator or
    filesystem path: the same result frozen on another machine must hash the same.
    """
    return {
        "baseline_protocol_hash": BASELINE_PROTOCOL_HASH,
        "execution_environment_hash": EXECUTION_ENVIRONMENT_HASH,
        "finalist_freeze_sha256": FINALIST_FREEZE_SHA256,
        "minos_subnet_sha": MINOS_SUBNET_SHA,
        "ordered_ranking": list(ORDERED_RANKING),
        "phase_c_closure_sha256": PHASE_C_CLOSURE_SHA256,
        "phase_d_closure_artifact_sha256": PHASE_D_CLOSURE_ARTIFACT_SHA256,
        "phase_d_closure_evidence_sha256": PHASE_D_CLOSURE_EVIDENCE_SHA256,
        "phase_d_closure_hash": PHASE_D_CLOSURE_HASH,
        "phase_d_plan_hash": PHASE_D_PLAN_HASH,
        "schema_version": BASELINE_SELECTED_SCHEMA,
        "scoring_contract_hash": SCORING_CONTRACT_HASH,
        "seed_config_hash": SEED_CONFIG_HASH,
        "seed_rank": SEED_RANK,
        "selected_candidate_failure_count": SELECTED_CANDIDATE_FAILURE_COUNT,
        "selected_config_hash": SELECTED_CONFIG_HASH,
        "selected_cvar": SELECTED_CVAR,
        "selected_failure_rate": SELECTED_FAILURE_RATE,
        "selected_floor": SELECTED_FLOOR,
        "selected_inherited_candidate_index": SELECTED_INHERITED_CANDIDATE_INDEX,
        "selected_mean": SELECTED_MEAN,
        "selected_mean_gatk_runtime_ms": SELECTED_MEAN_GATK_RUNTIME_MS,
        "selected_objective": SELECTED_OBJECTIVE,
        "selected_rank": SELECTED_RANK,
        "selection_interpretation_hash": SELECTION_INTERPRETATION_HASH,
    }


def compute_baseline_selected_hash() -> str:
    """The domain-separated identity of the frozen result."""
    return sha256_hex(
        BASELINE_SELECTED_DOMAIN.encode("utf-8") + canonical_json_bytes(baseline_selected_content())
    )


def verify_closure_artifact(path: str | Path) -> dict[str, Any]:
    """Verify the canonical Phase-D closure artifact and its agreement with this freeze.

    Every constant above is checked against the closure rather than trusted. A hand-copied winner
    that the closure does not support is refused here, which is the only reason those constants
    are safe to state at all.
    """
    from minos_engine.baseline.phase_d_observations import (
        PHASE_D_CLOSURE_SCHEMA,
        PhaseDClosure,
        compute_phase_d_closure_hash,
    )

    artifact = Path(path)
    if artifact.is_symlink() or not artifact.is_file():
        raise BaselineSelectedError(f"closure artifact {artifact} is missing or a symlink")
    payload = artifact.read_bytes()
    observed = sha256_hex(payload)
    if observed != PHASE_D_CLOSURE_ARTIFACT_SHA256:
        raise BaselineSelectedError(
            f"closure artifact hashes {observed}, expected {PHASE_D_CLOSURE_ARTIFACT_SHA256}"
        )
    try:
        content = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BaselineSelectedError(f"closure artifact is not canonical JSON: {exc}") from exc
    if content.get("schema_version") != PHASE_D_CLOSURE_SCHEMA:
        raise BaselineSelectedError(
            f"closure schema is {content.get('schema_version')!r}, expected "
            f"{PHASE_D_CLOSURE_SCHEMA!r}"
        )

    # the contract is strict and its sequence fields are tuples; canonical JSON necessarily
    # serialises them as arrays, so they are restored rather than the model loosened.
    restored = dict(content)
    for field in ("candidates", "observations", "ordered_ranking"):
        value = restored.get(field)
        if isinstance(value, list):
            restored[field] = tuple(value)
    closure = PhaseDClosure.model_validate(restored)
    recomputed = compute_phase_d_closure_hash(closure)
    if recomputed != PHASE_D_CLOSURE_HASH:
        raise BaselineSelectedError(
            f"the closure recomputes to {recomputed}, not the accepted {PHASE_D_CLOSURE_HASH}"
        )

    for label, got, want in (
        ("baseline protocol", closure.baseline_protocol_hash, BASELINE_PROTOCOL_HASH),
        (
            "selection interpretation",
            closure.selection_interpretation_hash,
            SELECTION_INTERPRETATION_HASH,
        ),
        ("Phase-D plan", closure.phase_d_plan_hash, PHASE_D_PLAN_HASH),
        ("finalist freeze", closure.finalist_freeze_sha256, FINALIST_FREEZE_SHA256),
        ("Phase-C closure", closure.phase_c_closure_sha256, PHASE_C_CLOSURE_SHA256),
        ("scoring contract", closure.scoring_contract_hash, SCORING_CONTRACT_HASH),
        (
            "execution environment",
            closure.execution_environment_hash,
            EXECUTION_ENVIRONMENT_HASH,
        ),
        ("MINOS_SUBNET", closure.minos_subnet_sha, MINOS_SUBNET_SHA),
        ("selected config", closure.selected_config_hash, SELECTED_CONFIG_HASH),
        ("seed config", closure.seed_config_hash, SEED_CONFIG_HASH),
    ):
        if got != want:
            raise BaselineSelectedError(f"closure {label} is {got}, expected {want}")

    if (closure.candidate_count, closure.member_count, closure.observation_count) != (4, 10, 40):
        raise BaselineSelectedError(
            f"closure shape is {closure.candidate_count}x{closure.member_count} with "
            f"{closure.observation_count} observations, expected 4x10 with 40"
        )
    if list(closure.ordered_ranking) != list(ORDERED_RANKING):
        raise BaselineSelectedError("the closure ranking is not the frozen ordered ranking")
    if closure.seed_rank != SEED_RANK:
        raise BaselineSelectedError(
            f"the closure ranks the seed {closure.seed_rank}, expected {SEED_RANK}"
        )

    winner = next(c for c in closure.candidates if c.config_hash == SELECTED_CONFIG_HASH)
    if winner.rank != SELECTED_RANK:
        raise BaselineSelectedError(
            f"the selected config ranks {winner.rank}, not {SELECTED_RANK}; the selected baseline "
            "must be the closure's rank zero"
        )
    numeric: tuple[tuple[str, Any, Any], ...] = (
        ("inherited index", winner.inherited_candidate_index, SELECTED_INHERITED_CANDIDATE_INDEX),
        ("cvar", winner.cvar, SELECTED_CVAR),
        ("floor", winner.floor, SELECTED_FLOOR),
        ("mean", winner.mean, SELECTED_MEAN),
        ("failure rate", winner.failure_rate, SELECTED_FAILURE_RATE),
        ("failure count", winner.candidate_failure_count, SELECTED_CANDIDATE_FAILURE_COUNT),
        ("mean runtime", winner.mean_gatk_runtime_ms, SELECTED_MEAN_GATK_RUNTIME_MS),
        ("objective", winner.objective, SELECTED_OBJECTIVE),
    )
    for label, got, want in numeric:
        if got != want:
            raise BaselineSelectedError(
                f"the selected candidate's {label} is {got}, expected {want}"
            )

    for candidate in closure.candidates:
        if candidate.observed_count != closure.member_count:
            raise BaselineSelectedError(
                f"candidate {candidate.config_hash} is incomplete "
                f"({candidate.observed_count}/{closure.member_count})"
            )
        if candidate.infrastructure_incident_count:
            raise BaselineSelectedError(
                f"candidate {candidate.config_hash} carries "
                f"{candidate.infrastructure_incident_count} infrastructure incidents"
            )
    return dict(content)


def load_committed_baseline_selected(root: Path | None = None) -> dict[str, Any]:
    """Read the committed manifest and verify it against the code. Fails closed."""
    from minos_engine.qualification.l2f_accepted_identities import repository_root

    path = (root or repository_root()) / BASELINE_SELECTED_MANIFEST
    if not path.is_file():
        raise BaselineSelectedError(f"baseline-selected manifest is missing: {path}")
    try:
        document: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise BaselineSelectedError(f"baseline-selected manifest is not JSON: {exc}") from exc
    expected = compute_baseline_selected_hash()
    if document.get("baseline_selected_hash") != expected:
        raise BaselineSelectedError(
            f"committed baseline-selected hash {document.get('baseline_selected_hash')!r} does "
            f"not match the code's {expected!r}"
        )
    if document.get("content") != baseline_selected_content():
        raise BaselineSelectedError("the committed baseline-selected content differs from the code")
    return document
