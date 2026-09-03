"""The Phase-D final-selection rule: what it says, and what it must never quietly contain.

The rule is one line — take rank 0. The tests that matter are the ones proving nothing else is
hiding in it: no seed preference, no margin, no TRAIN input, no threshold, and no observed score
anywhere near the hash.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest

from minos_engine.baseline.phase_d_selection import (
    INHERITED_CANDIDATE_INDEX,
    ORDERED_FINALISTS,
    PHASE_D_SELECTION_DOMAIN,
    PHASE_D_SELECTION_MANIFEST,
    PHASE_D_SELECTION_SCHEMA,
    PHASE_D_SELECTION_STATUS,
    SEED_CONFIG_HASH,
    PhaseDSelectionInterpretationError,
    compute_selection_interpretation_hash,
    load_committed_selection_interpretation,
    select_phase_d_baseline_from_ranked_hashes,
    selection_interpretation_content,
)

_PROTOCOL_HASH = "c548e190571f5e964560cf30021a520ea8aad6674569fa3202af880d7dff77d1"
_A, _B, _C, _SEED = ORDERED_FINALISTS


def _repo() -> Path:
    return Path(__file__).resolve().parents[3]


# --------------------------------------------------------------------------------------------
# the rule itself
# --------------------------------------------------------------------------------------------
def test_the_selected_baseline_is_whatever_ranked_first() -> None:
    assert select_phase_d_baseline_from_ranked_hashes((_A, _B, _C, _SEED)) == _A


def test_the_seed_ranking_last_is_left_last() -> None:
    """§9 — the decisive one. No post-validation rule rescues the configuration in use today."""
    assert select_phase_d_baseline_from_ranked_hashes((_A, _B, _C, _SEED)) == _A
    assert select_phase_d_baseline_from_ranked_hashes((_B, _C, _A, _SEED)) == _B
    assert select_phase_d_baseline_from_ranked_hashes((_C, _A, _B, _SEED)) == _C


def test_the_seed_ranking_first_is_selected_like_any_other_candidate() -> None:
    """Winning naturally is not the same as being protected; both must be visible."""
    assert select_phase_d_baseline_from_ranked_hashes((_SEED, _A, _B, _C)) == _SEED


@pytest.mark.parametrize("position", range(4))
def test_selection_depends_on_position_alone(position: int) -> None:
    """Every finalist, placed first, is selected. Identity carries no weight of its own."""
    order = [_A, _B, _C, _SEED]
    order.insert(0, order.pop(position))
    assert select_phase_d_baseline_from_ranked_hashes(tuple(order)) == order[0]


# --------------------------------------------------------------------------------------------
# the rule cannot be handed something that is not the frozen four
# --------------------------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("ranked", "match"),
    [
        pytest.param((_A, _B, _C), "exactly 4", id="too-few"),
        pytest.param((_A, _B, _C, _SEED, _A), "exactly 4", id="too-many"),
        pytest.param((_A, _A, _B, _C), "duplicate", id="duplicate"),
        pytest.param((_A, _B, _C, "f" * 64), "not a frozen", id="stranger"),
    ],
)
def test_a_population_that_is_not_the_frozen_four_is_refused(
    ranked: tuple[str, ...], match: str
) -> None:
    with pytest.raises(PhaseDSelectionInterpretationError, match=match):
        select_phase_d_baseline_from_ranked_hashes(ranked)


def test_the_selector_takes_no_score_seed_or_threshold_argument() -> None:
    """A rule with nowhere to put a margin cannot grow one."""
    import inspect

    params = inspect.signature(select_phase_d_baseline_from_ranked_hashes).parameters
    assert list(params) == ["ranked_hashes"], params


# --------------------------------------------------------------------------------------------
# §7 — the hash binds authorities and the clarification, never an outcome
# --------------------------------------------------------------------------------------------
def test_the_interpretation_content_holds_no_observed_quantity() -> None:
    """It must be computable before the first score is read, and identical afterwards."""
    blob = json.dumps(selection_interpretation_content(), sort_keys=True).lower()
    for forbidden in (
        "minos_score",
        "score_100",
        "core_score",
        "completeness_score",
        "fp_score",
        "quality_score",
        "overcall",
        "admitted",
        "admission",
        "runtime",
        "evaluation_hash",
        "metrics",
        "winner",
        "cvar_value",
        "objective_value",
    ):
        assert forbidden not in blob, forbidden
    # no bare float anywhere: an observed quantity would have to arrive as one
    assert not re.search(r":\s*-?\d+\.\d+", json.dumps(selection_interpretation_content())), (
        "a floating-point value appeared in the interpretation content"
    )


def test_the_recorded_rules_are_exactly_the_frozen_ones() -> None:
    content = selection_interpretation_content()
    assert content["aggregation_rule"] == "USE_EXISTING_AGGREGATE_CANDIDATE"
    assert content["ranking_rule"] == "USE_EXISTING_RANK_CANDIDATES"
    assert content["final_selection_rule"] == "SELECT_RANK_ZERO"
    assert content["seed_override_after_validation"] == "NONE"
    assert content["train_validation_combination"] == "NONE"
    assert content["new_thresholds"] == "NONE"
    assert content["diagnostics_may_influence_selection"] is False
    assert content["selection_population"] == "EXACT_FOUR_PHASE_D_FINALISTS"
    assert content["selection_observations"] == ("EXACT_TEN_FROZEN_VALIDATION_MEMBERS_PER_FINALIST")


def test_the_inherited_phase_b_indices_are_recorded_not_finalist_positions() -> None:
    """§10 — the third tie-break key. 0..3 would silently change who wins a tie."""
    assert [i for _, i in selection_interpretation_content()["inherited_candidate_index"]] == [
        42,
        25,
        36,
        0,
    ]
    assert INHERITED_CANDIDATE_INDEX[_SEED] == 0
    assert list(INHERITED_CANDIDATE_INDEX.values()) != [0, 1, 2, 3]


def test_the_hash_is_domain_separated_and_stable() -> None:
    from minos_engine.common.canonical_json import canonical_json_bytes
    from minos_engine.common.hashing import sha256_hex

    expected = sha256_hex(
        PHASE_D_SELECTION_DOMAIN.encode("utf-8")
        + canonical_json_bytes(selection_interpretation_content())
    )
    assert compute_selection_interpretation_hash() == expected
    assert compute_selection_interpretation_hash() == compute_selection_interpretation_hash()
    assert PHASE_D_SELECTION_DOMAIN != "minos:l2f2-baseline-search-protocol:v1\n"


@pytest.mark.parametrize(
    "field",
    [
        "final_selection_rule",
        "seed_override_after_validation",
        "train_validation_combination",
        "new_thresholds",
        "phase_d_plan_hash",
        "baseline_protocol_hash",
        "ordered_finalists",
    ],
)
def test_changing_any_recorded_rule_changes_the_hash(field: str, monkeypatch: Any) -> None:
    from minos_engine.baseline import phase_d_selection as mod

    original = mod.selection_interpretation_content
    baseline = compute_selection_interpretation_hash()

    def perturbed() -> dict[str, Any]:
        content = original()
        content[field] = "PERTURBED" if isinstance(content[field], str) else ["perturbed"]
        return content

    monkeypatch.setattr(mod, "selection_interpretation_content", perturbed)
    assert mod.compute_selection_interpretation_hash() != baseline


# --------------------------------------------------------------------------------------------
# §3 / §5 — the original protocol is untouched, and the provenance is stated honestly
# --------------------------------------------------------------------------------------------
def test_the_original_protocol_hash_is_unchanged() -> None:
    from minos_engine.baseline.protocol import build_baseline_protocol, compute_protocol_hash

    assert compute_protocol_hash(build_baseline_protocol()) == _PROTOCOL_HASH
    assert selection_interpretation_content()["baseline_protocol_hash"] == _PROTOCOL_HASH


def test_the_interpretation_is_not_inside_the_protocol_manifest() -> None:
    """It must be a SEPARATE authority. Folding it in would forge the historical identity."""
    protocol = json.loads(
        (_repo() / "manifests/l2f2_baseline_protocol_v1.json").read_text(encoding="utf-8")
    )
    assert protocol["protocol_hash"] == _PROTOCOL_HASH
    blob = json.dumps(protocol).lower()
    assert "select_rank_zero" not in blob
    assert "interpretation" not in blob


def test_the_status_admits_it_is_a_post_collection_clarification() -> None:
    """§5 — the honesty requirement, asserted rather than trusted to prose."""
    assert PHASE_D_SELECTION_STATUS == "OUTCOME_BLIND_POST_COLLECTION_CLARIFICATION"
    assert selection_interpretation_content()["interpretation_status"] == (PHASE_D_SELECTION_STATUS)
    doc = json.loads((_repo() / PHASE_D_SELECTION_MANIFEST).read_text(encoding="utf-8"))
    disclosure = doc["disclosure"].lower()
    assert "not part of l2f2-baseline-search-protocol-v1" in disclosure
    assert _PROTOCOL_HASH in doc["disclosure"]
    assert "had not been read" in disclosure
    # and it must never claim the stronger status
    assert "pre-registration" not in disclosure
    assert "pre-registered" not in disclosure


def test_the_module_never_claims_original_pre_registration() -> None:
    """It must not claim THIS artifact was pre-registered.

    It may — and now must — say that the RULE was, because BASELINE_QUALIFICATION.md section 12
    rule 4 states it and predates every validation score. The distinction is the whole point: the
    rule was pre-registered in prose; the hash binding was not, and is labelled outcome-blind
    instead. Banning the bare phrase would forbid the true statement along with the false one.
    """
    source = (
        (_repo() / "src/minos_engine/baseline/phase_d_selection.py")
        .read_text(encoding="utf-8")
        .lower()
    )
    assert "part of the original frozen protocol, and must never be presented" in source
    assert "do not describe this as" in source
    assert "it does not invent one" in source
    for forged in (
        "this interpretation was pre-registered",
        "this document was pre-registered",
        "hash-bound in c548",
        "part of protocol hash",
    ):
        assert forged not in source, forged


def test_the_committed_manifest_matches_the_code() -> None:
    document = load_committed_selection_interpretation(_repo())
    assert document["selection_interpretation_hash"] == compute_selection_interpretation_hash()
    assert document["schema_version"] == PHASE_D_SELECTION_SCHEMA


def test_a_tampered_manifest_fails_closed(tmp_path: Path) -> None:
    target = tmp_path / PHASE_D_SELECTION_MANIFEST
    target.parent.mkdir(parents=True, exist_ok=True)
    document = json.loads((_repo() / PHASE_D_SELECTION_MANIFEST).read_text(encoding="utf-8"))
    document["content"]["final_selection_rule"] = "SELECT_SEED"
    target.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(PhaseDSelectionInterpretationError, match="hash|differs"):
        load_committed_selection_interpretation(tmp_path)


def test_a_missing_manifest_fails_closed(tmp_path: Path) -> None:
    with pytest.raises(PhaseDSelectionInterpretationError, match="missing"):
        load_committed_selection_interpretation(tmp_path)


# --------------------------------------------------------------------------------------------
# §8 / §10 — this module freezes a rule; it does not compute or reach for anything
# --------------------------------------------------------------------------------------------
def test_the_module_reads_no_database_and_computes_no_ranking() -> None:
    source = (_repo() / "src/minos_engine/baseline/phase_d_selection.py").read_text(
        encoding="utf-8"
    )
    for forbidden in (
        "sqlalchemy",
        "create_engine",
        "create_db_engine",
        "SELECT ",
        "psycopg",
        "aggregate_candidate(",
        "rank_candidates(",
        "objective_value(",
    ):
        assert forbidden not in source, forbidden
    # No ORDERING of candidates. Sorting a set for a deterministic error message is not ranking,
    # so that one call is excluded by name rather than by loosening the rule.
    residue = source.replace("sorted(unknown)", "")
    assert "sorted(" not in residue, "the module orders candidates"
    assert "key=" not in residue, "the module applies a sort key"


def test_the_seed_is_recorded_as_an_identity_only() -> None:
    """Its rank is worth reporting. Its identity must never be a reason to select it."""
    assert ORDERED_FINALISTS[3] == SEED_CONFIG_HASH
    source = (_repo() / "src/minos_engine/baseline/phase_d_selection.py").read_text(
        encoding="utf-8"
    )
    body = source[source.index("def select_phase_d_baseline_from_ranked_hashes") :]
    assert "SEED_CONFIG_HASH" not in body, "the selector consults the seed"
