"""``l2f2-phase-d-selection-interpretation-v1`` — the Phase-D final-selection semantics.

WHAT THIS IS, STATED PLAINLY
----------------------------
This is **not** part of the original frozen protocol, and must never be presented as though it
were. ``l2f2-baseline-search-protocol-v1`` (hash
``c548e190571f5e964560cf30021a520ea8aad6674569fa3202af880d7dff77d1``) fixes the robust objective,
the total tie-break, the Phase-D 4x10 VALIDATION design and the absence of Phase-D racing, and the
roadmap records that L2-F2-F exits with a baseline selected. What it does **not** contain is one
explicit sentence mapping the final Phase-D ranking to the selected baseline. A closure task
stopped on exactly that gap rather than inventing the rule.

This module records the operator's chosen reading, and its status is:

    OUTCOME_BLIND_POST_COLLECTION_CLARIFICATION

The forty real Phase-D observations already EXISTED when this was written. Their scores, runtimes
and admission outcomes had NOT been read, and none of them appears here or influenced a line of
it. That is a weaker guarantee than genuine pre-registration and a stronger one than a rule chosen
after seeing results; it is recorded as exactly that, so later qualification evidence can weigh it
honestly. Do not describe this as "original pre-registration", and do not claim it lives inside
``c548e190...``.

THE READING
-----------
Once all four frozen finalists have complete Phase-D VALIDATION observations, each finalist is
aggregated over the exact ten frozen VALIDATION members using the already-frozen L2-F2 objective;
the four complete aggregates are ordered by the already-frozen total tie-break; and
``ranking[0]`` IS the selected L2-F2 baseline.

TRAIN searched, raced and froze the four finalists. It is not a second final-selection input: the
Phase-D aggregate is over the ten VALIDATION members only. The seed-control rule belongs to
PROMOTION and has no post-validation effect.

WHY THIS READING, FROM SOURCE THAT ALREADY EXISTED
--------------------------------------------------
Every fact below was committed at ``274765331a4307a4a9d7282b0563a6bb2315183f`` (tree
``6c09f1742991f05f2f5189d968a2e6940d009840``) — the source state that ran the real Phase-D loop.
None was edited to strengthen the argument afterwards.

A. ``baseline/protocol.py`` calls itself "the FROZEN baseline-selection protocol" and states that
   every rule deciding which GATK config becomes the baseline "is fixed here, hashed, and
   committed *before the first real score exists*". The protocol's own intent is that the
   selection rule is already settled.
B. The frozen objective declares higher-is-better.
C. The frozen total order is complete and total: higher objective; lower mean GATK runtime; lower
   candidate index in the frozen phase design; lexicographically smaller config hash.
D. The validation block fixes four finalists, ten VALIDATION members, forty evaluations, and NO
   racing — a design that produces exactly one complete aggregate per finalist and nothing else.
E. ``docs/layer2/BASELINE_QUALIFICATION.md`` records L2-F2-E exiting with "finalists ranked on all
   50 TRAIN BAMs", L2-F2-F exiting with "baseline selected", and qualification only at L2-F2-G.
   Selection is F's stated exit.
F. ``FinalistFreeze`` is documented as "Identity only - no score, no ranking input": the TRAIN
   ranking is deliberately NOT carried into Phase D, so there is no TRAIN ordering here for a
   confirmation reading to fall back to.
G. There is no pre-existing post-validation seed override, minimum validation score, minimum
   improvement over seed, TRAIN/VALIDATION weighted objective, generalization-gap threshold,
   significance test, validation rejection threshold, or fallback-to-TRAIN-winner rule.

Reading A is therefore the LEAST-ADDITIVE completion of the existing design: it introduces no new
quantity, threshold or comparison. The alternative — that Phase D merely confirms an earlier
choice — would require inventing both a fallback target and a rejection criterion, neither of
which exists anywhere, and F shows the fallback target was deliberately discarded.

WHAT THIS MODULE DOES NOT DO
----------------------------
It computes no ranking, reads no database, and touches no score. It freezes the RULE. The later
closure authority proves that the tuple handed to :func:`select_phase_d_baseline_from_ranked_hashes`
came from ``aggregate_candidate`` and then ``rank_candidates``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Final

from minos_engine.common.canonical_json import canonical_json_bytes
from minos_engine.common.errors import MinosEngineError
from minos_engine.common.hashing import sha256_hex

__all__ = [
    "INHERITED_CANDIDATE_INDEX",
    "ORDERED_FINALISTS",
    "PHASE_D_SELECTION_DOMAIN",
    "PHASE_D_SELECTION_MANIFEST",
    "PHASE_D_SELECTION_SCHEMA",
    "PHASE_D_SELECTION_STATUS",
    "SEED_CONFIG_HASH",
    "PhaseDSelectionInterpretationError",
    "compute_selection_interpretation_hash",
    "load_committed_selection_interpretation",
    "select_phase_d_baseline_from_ranked_hashes",
    "selection_interpretation_content",
]

PHASE_D_SELECTION_SCHEMA: Final = "l2f2-phase-d-selection-interpretation-v1"
PHASE_D_SELECTION_DOMAIN: Final = "minos:l2f2-phase-d-selection-interpretation:v1\n"
PHASE_D_SELECTION_MANIFEST: Final = "manifests/l2f2_phase_d_selection_interpretation_v1.json"

#: The honest provenance of this rule. Not "pre-registered": the observations existed, their
#: contents did not enter this decision.
PHASE_D_SELECTION_STATUS: Final = "OUTCOME_BLIND_POST_COLLECTION_CLARIFICATION"

BASELINE_PROTOCOL_HASH: Final = "c548e190571f5e964560cf30021a520ea8aad6674569fa3202af880d7dff77d1"
PHASE_D_PLAN_HASH: Final = "f6bd1e450c38d789dcfcdafaaf357dad2f7602f53fc8ec779c5be40c71e6d7ce"
FINALIST_FREEZE_SHA256: Final = "540aeca0640871ca91e3ec771ec66d2df4b96d38210ec3265f944dee3e0433f3"
PHASE_C_CLOSURE_SHA256: Final = "5de368eec327b66c868737d1819cc1b1a590eaf185b28e53d1cfecae59b593ca"

#: The frozen four, in frozen order. Identity only — no score, no ranking input.
ORDERED_FINALISTS: Final[tuple[str, ...]] = (
    "157d88d1587c13be395c62d60e27d1becdada78fad45e65d883bc1190e51acea",
    "0972930f8d8c562be15382203e123b2909094e7eac46e84321d36c67abf8345e",
    "22a1f1fd9ddf02a97776d991f11280b3982673693a4f357479098a99fb411a16",
    "4251cb85e5cd58b7eabfe530b9df23ea7d1d14fd882114b488d67cbd81b751b8",
)

#: The INHERITED Phase-B indices. These, not 0..3, are the frozen third tie-break key; substituting
#: finalist position would silently change which candidate wins an objective+runtime tie.
INHERITED_CANDIDATE_INDEX: Final[dict[str, int]] = {
    ORDERED_FINALISTS[0]: 42,
    ORDERED_FINALISTS[1]: 25,
    ORDERED_FINALISTS[2]: 36,
    ORDERED_FINALISTS[3]: 0,
}

#: The configuration in use today. It is here as an IDENTITY so the closure can report its rank —
#: never as a rule that protects it.
SEED_CONFIG_HASH: Final = ORDERED_FINALISTS[3]


class PhaseDSelectionInterpretationError(MinosEngineError):
    """The recorded interpretation is absent, altered, or contradicted by the code."""


def selection_interpretation_content() -> dict[str, Any]:
    """Exactly what ``selection_interpretation_hash`` covers.

    Outcome-independent authorities and the clarification itself. No observed score, runtime,
    admission, evaluation hash, metric or derived winner may appear — the hash must be computable
    before the first score is read, and identical afterwards.
    """
    return {
        "aggregation_rule": "USE_EXISTING_AGGREGATE_CANDIDATE",
        "baseline_protocol_hash": BASELINE_PROTOCOL_HASH,
        "diagnostics_may_influence_selection": False,
        "final_selection_rule": "SELECT_RANK_ZERO",
        "finalist_freeze_sha256": FINALIST_FREEZE_SHA256,
        "inherited_candidate_index": [[h, INHERITED_CANDIDATE_INDEX[h]] for h in ORDERED_FINALISTS],
        "interpretation_status": PHASE_D_SELECTION_STATUS,
        "new_thresholds": "NONE",
        "ordered_finalists": list(ORDERED_FINALISTS),
        "phase_c_closure_sha256": PHASE_C_CLOSURE_SHA256,
        "phase_d_plan_hash": PHASE_D_PLAN_HASH,
        "ranking_rule": "USE_EXISTING_RANK_CANDIDATES",
        "schema_version": PHASE_D_SELECTION_SCHEMA,
        "seed_config_hash": SEED_CONFIG_HASH,
        "seed_override_after_validation": "NONE",
        "selection_observations": "EXACT_TEN_FROZEN_VALIDATION_MEMBERS_PER_FINALIST",
        "selection_population": "EXACT_FOUR_PHASE_D_FINALISTS",
        "source_state": {
            "commit": "274765331a4307a4a9d7282b0563a6bb2315183f",
            "tree": "6c09f1742991f05f2f5189d968a2e6940d009840",
        },
        "train_validation_combination": "NONE",
    }


def compute_selection_interpretation_hash() -> str:
    """The domain-separated identity of this interpretation."""
    return sha256_hex(
        PHASE_D_SELECTION_DOMAIN.encode("utf-8")
        + canonical_json_bytes(selection_interpretation_content())
    )


def select_phase_d_baseline_from_ranked_hashes(ranked_hashes: tuple[str, ...]) -> str:
    """Return the selected baseline: the first entry of an ALREADY-RANKED finalist tuple.

    Deliberately trivial, and deliberately blind. It receives no aggregate, no score and no seed,
    so there is nowhere for a margin, a threshold or a seed preference to hide: whatever the
    frozen ranking authority put first is the baseline. The caller must have produced
    ``ranked_hashes`` with ``aggregate_candidate`` then ``rank_candidates``; this function checks
    only that the tuple is the frozen four.
    """
    if len(ranked_hashes) != len(ORDERED_FINALISTS):
        raise PhaseDSelectionInterpretationError(
            f"Phase-D selection requires exactly {len(ORDERED_FINALISTS)} ranked finalists, "
            f"got {len(ranked_hashes)}"
        )
    if len(set(ranked_hashes)) != len(ranked_hashes):
        raise PhaseDSelectionInterpretationError("the ranked finalists contain a duplicate")
    unknown = set(ranked_hashes) - set(ORDERED_FINALISTS)
    if unknown:
        raise PhaseDSelectionInterpretationError(
            f"not a frozen Phase-D finalist: {sorted(unknown)}"
        )
    return ranked_hashes[0]


def load_committed_selection_interpretation(root: Path | None = None) -> dict[str, Any]:
    """Read the committed manifest and verify it against the code. Fails closed."""
    import json

    from minos_engine.qualification.l2f_accepted_identities import repository_root

    path = (root or repository_root()) / PHASE_D_SELECTION_MANIFEST
    if not path.is_file():
        raise PhaseDSelectionInterpretationError(f"interpretation manifest is missing: {path}")
    try:
        document: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise PhaseDSelectionInterpretationError(
            f"interpretation manifest is not JSON: {exc}"
        ) from exc
    expected = compute_selection_interpretation_hash()
    if document.get("selection_interpretation_hash") != expected:
        raise PhaseDSelectionInterpretationError(
            f"committed interpretation hash {document.get('selection_interpretation_hash')!r} "
            f"does not match the code's {expected!r}"
        )
    if document.get("content") != selection_interpretation_content():
        raise PhaseDSelectionInterpretationError(
            "the committed interpretation content differs from the code"
        )
    return document
