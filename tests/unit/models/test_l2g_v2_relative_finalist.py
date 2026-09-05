"""L2-G v2: the relative-advantage finalist selector protocol, frozen before any v2 fit.

No v2 model is fitted here. What is pinned is the shape of the decision problem — the exact
four-config domain, the advantage target, the density of the TRAIN slice, and the switch rule —
plus the honest record of how little headroom the problem actually has.
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

import pytest

from minos_engine.models.contract import CV_FOLD_CHROMOSOMES
from minos_engine.models.prefit_loader import load_verified_training_dataset
from minos_engine.models.relative_finalist_contract import (
    ALTERNATIVE_FINALISTS,
    DELTA_SAFE_BASELINE,
    FINALIST_DOMAIN,
    FORBIDDEN_V2_PREDICTORS,
    PARENT_CAMPAIGN_FREEZE_IDENTITY,
    RESEARCH_PROTOCOL_VERSION,
    SAFE_BASELINE_CONFIG_HASH,
    RelativeFinalistError,
    compute_finalist_domain_hash,
    compute_relative_contract_hash,
    verify_finalist_domain,
)
from minos_engine.models.relative_finalist_dataset import (
    RELATIVE_DATASET_SCHEMA,
    AdvantageRow,
    build_relative_finalist_dataset,
)
from minos_engine.models.relative_finalist_protocol import (
    PROMOTION_RULE,
    SWITCH_RULE,
    V2_CANDIDATE_GRID,
    V2_REFERENCES,
    build_v2_spec_hashes,
    compute_relative_protocol_hash,
    relative_protocol_content,
)
from minos_engine.qualification.l2f_accepted_identities import repository_root

_FEASIBILITY = "reports/layer2/l2g-v2-relative-finalist-feasibility.json"


@pytest.fixture(scope="module")
def training() -> Any:
    return load_verified_training_dataset()


@pytest.fixture(scope="module")
def relative(training: Any) -> Any:
    return build_relative_finalist_dataset(training)


@pytest.fixture(scope="module")
def feasibility() -> dict[str, Any]:
    return dict(json.loads((repository_root() / _FEASIBILITY).read_bytes()))


# ---------------------------------------------------------------------------------------- #
# the four-config decision domain
# ---------------------------------------------------------------------------------------- #
def test_the_domain_is_the_four_phase_d_finalists_rederived() -> None:
    from minos_engine.baseline.finalist_freeze import load_finalist_freeze
    from minos_engine.models.relative_finalist_contract import (
        ACCEPTED_FINALIST_FREEZE_SHA256,
    )
    from tests.minos_scratch import CANONICAL_MINOS_ROOT

    artifact = (
        CANONICAL_MINOS_ROOT
        / "minos_l2f2_baseline"
        / ("phase_c_validation_finalists_20260830.json")
    )
    if not artifact.is_file():
        pytest.skip("the Phase-C finalist freeze is not present on this machine")
    freeze = load_finalist_freeze(
        artifact, expected_artifact_sha256=ACCEPTED_FINALIST_FREEZE_SHA256
    )
    assert tuple(freeze.ordered_finalists) == FINALIST_DOMAIN


def test_the_safe_baseline_is_the_first_action(training: Any) -> None:
    assert FINALIST_DOMAIN[0] == SAFE_BASELINE_CONFIG_HASH
    assert len(FINALIST_DOMAIN) == 4
    assert len(ALTERNATIVE_FINALISTS) == 3
    assert SAFE_BASELINE_CONFIG_HASH not in ALTERNATIVE_FINALISTS


def test_the_domain_hash_is_deterministic_and_order_bound() -> None:
    assert compute_finalist_domain_hash() == compute_finalist_domain_hash()
    assert verify_finalist_domain(FINALIST_DOMAIN) == compute_finalist_domain_hash()
    reordered = (FINALIST_DOMAIN[1], FINALIST_DOMAIN[0], *FINALIST_DOMAIN[2:])
    with pytest.raises(RelativeFinalistError, match="not the accepted four"):
        verify_finalist_domain(reordered)


def test_a_fifth_config_is_refused() -> None:
    with pytest.raises(RelativeFinalistError, match="expected 4"):
        compute_finalist_domain_hash((*FINALIST_DOMAIN, "f" * 64))


def test_a_domain_without_the_safe_baseline_first_is_refused() -> None:
    with pytest.raises(RelativeFinalistError, match="must be the first action"):
        compute_finalist_domain_hash((*ALTERNATIVE_FINALISTS, SAFE_BASELINE_CONFIG_HASH))


# ---------------------------------------------------------------------------------------- #
# the dense TRAIN slice
# ---------------------------------------------------------------------------------------- #
def test_the_four_finalist_train_slice_is_completely_dense(training: Any) -> None:
    cells = {(r.dataset_id, r.config_hash) for r in training.rows}
    bams = sorted(training.cv_manifest.bam_chromosome)
    assert len(bams) == 50
    missing = [(b, c) for b in bams for c in FINALIST_DOMAIN if (b, c) not in cells]
    assert missing == [], f"the four-finalist slice is not dense: {len(missing)} missing"


def test_every_finalist_cell_was_admitted(training: Any) -> None:
    by = {(r.dataset_id, r.config_hash): r for r in training.rows}
    bams = sorted(training.cv_manifest.bam_chromosome)
    outcomes = {by[(b, c)].outcome for b in bams for c in FINALIST_DOMAIN}
    assert outcomes == {"ADMITTED"}


def test_the_relative_dataset_is_fifty_by_three(relative: Any) -> None:
    assert relative.schema_version == RELATIVE_DATASET_SCHEMA
    assert len(relative.rows) == 150
    assert len(relative.source_cell_identities) == 200
    assert len(relative.bam_chromosome) == 50
    assert len({r.dataset_id for r in relative.rows}) == 50
    assert {r.config_hash for r in relative.rows} == set(ALTERNATIVE_FINALISTS)


def test_the_dataset_binds_its_parent_and_authorities(relative: Any, training: Any) -> None:
    assert relative.parent_campaign_freeze_identity == PARENT_CAMPAIGN_FREEZE_IDENTITY
    assert relative.source_training_dataset_hash == training.identity()
    assert relative.relative_contract_hash == compute_relative_contract_hash()
    assert relative.finalist_domain_hash == compute_finalist_domain_hash()


def test_the_dataset_identity_is_deterministic(training: Any) -> None:
    a = build_relative_finalist_dataset(training)
    b = build_relative_finalist_dataset(training)
    assert a.identity() == b.identity()


# ---------------------------------------------------------------------------------------- #
# the advantage target
# ---------------------------------------------------------------------------------------- #
def test_delta_is_alternative_minus_safe(relative: Any) -> None:
    for row in relative.rows:
        assert row.delta == pytest.approx(row.alternative_utility - row.safe_utility)


def test_the_safe_baseline_is_never_an_advantage_example() -> None:
    assert DELTA_SAFE_BASELINE == 0.0
    with pytest.raises(RelativeFinalistError, match="reference action"):
        AdvantageRow(
            dataset_id="b",
            chromosome="chr18",
            config_hash=SAFE_BASELINE_CONFIG_HASH,
            safe_utility=0.7,
            alternative_utility=0.7,
            delta=0.0,
        )


def test_a_non_finalist_config_is_never_an_advantage_example() -> None:
    with pytest.raises(RelativeFinalistError, match="not one of the frozen alternative"):
        AdvantageRow(
            dataset_id="b",
            chromosome="chr18",
            config_hash="a" * 64,
            safe_utility=0.7,
            alternative_utility=0.8,
            delta=0.1,
        )


def test_a_delta_that_does_not_match_its_utilities_is_refused() -> None:
    with pytest.raises(RelativeFinalistError, match="not alternative minus safe"):
        AdvantageRow(
            dataset_id="b",
            chromosome="chr18",
            config_hash=ALTERNATIVE_FINALISTS[0],
            safe_utility=0.7,
            alternative_utility=0.8,
            delta=0.5,
        )


def test_campaign_v1_output_can_never_become_a_v2_predictor() -> None:
    for forbidden in (
        "campaign_v1_prediction",
        "campaign_v1_residual",
        "campaign_v1_selected_config",
    ):
        assert forbidden in FORBIDDEN_V2_PREDICTORS
    for identity in ("dataset_id", "chromosome", "minos_score", "truth_vcf"):
        assert identity in FORBIDDEN_V2_PREDICTORS


# ---------------------------------------------------------------------------------------- #
# the frozen protocol
# ---------------------------------------------------------------------------------------- #
def test_this_is_research_protocol_version_two_not_a_v1_continuation() -> None:
    assert RESEARCH_PROTOCOL_VERSION == 2
    content = relative_protocol_content()
    assert content["relative_contract_hash"] == compute_relative_contract_hash()
    assert content["hpo"] == "FINITE_PREDECLARED_GRID_NO_ADAPTIVE_SEARCH"


def test_the_candidate_grid_is_small_and_low_capacity() -> None:
    assert 2 <= len(V2_CANDIDATE_GRID) <= 4
    families = {c["family"] for c in V2_CANDIDATE_GRID}
    assert families == {"RELATIVE_RIDGE_SHARED", "RELATIVE_HISTGB_SHARED"}
    for recipe in V2_CANDIDATE_GRID:
        assert "MLP" not in recipe["implementation"]
        assert "torch" not in recipe["implementation"]


def test_the_v2_spec_hashes_exist_before_any_fit(relative: Any) -> None:
    hashes = build_v2_spec_hashes(relative.identity())
    assert len(hashes) == len(V2_CANDIDATE_GRID)
    assert len(set(hashes)) == len(hashes)
    assert all(len(h) == 64 for h in hashes)
    assert hashes == build_v2_spec_hashes(relative.identity())


def test_the_switch_rule_is_one_frozen_family_learned_inside_training() -> None:
    assert SWITCH_RULE["family"] == "INNER_OOF_RESIDUAL_MARGIN"
    assert SWITCH_RULE["margin_fitted_on"] == "OUTER_TRAINING_BAMS_ONLY"
    assert SWITCH_RULE["applied_to"] == "THE_HELD_OUT_CHROMOSOME"
    assert SWITCH_RULE["default_action"] == "SAFE_BASELINE"
    assert SWITCH_RULE["safe_baseline_always_available"] is True
    assert SWITCH_RULE["never_forced_to_switch"] is True
    # the margin quantiles are predeclared, not searched
    assert {c["margin_quantile"] for c in V2_CANDIDATE_GRID} == {0.75, 0.90}


def test_the_promotion_bar_is_a_deployable_reference() -> None:
    bar = [r for r in V2_REFERENCES if r["is_promotion_bar"]]
    assert len(bar) == 1
    assert bar[0]["name"] == "ALWAYS_SAFE_BASELINE"
    assert bar[0]["deployable"] is True
    oracle = next(r for r in V2_REFERENCES if r["name"] == "ORACLE4")
    assert oracle["deployable"] is False
    assert oracle["is_promotion_bar"] is False


def test_the_promotion_rule_was_not_weakened_because_v1_failed() -> None:
    assert PROMOTION_RULE["bar"] == "ALWAYS_SAFE_BASELINE"
    assert "mean_regret <=" in PROMOTION_RULE["rule"]
    assert "cvar_regret <=" in PROMOTION_RULE["rule"]
    assert PROMOTION_RULE["not_weakened_because_v1_failed"] is True
    assert PROMOTION_RULE["empty_shortlist_is_valid"] is True


def test_the_cv_is_the_same_immutable_chromosome_grouping() -> None:
    content = relative_protocol_content()
    assert content["cv_outer_folds"] == list(CV_FOLD_CHROMOSOMES)
    assert content["cv_grouping"] == "BAM_GROUPED_CHROMOSOME_HELD_OUT"
    assert content["transforms_fitted_on"] == "OUTER_TRAINING_BAMS_ONLY"
    assert content["cvar_tail_rule"] == "CEIL_ALPHA_TIMES_N"
    assert math.ceil(content["cvar_alpha"] * 50) == 13


def test_v2_train_oof_is_declared_development_evidence() -> None:
    """v2's design was informed by v1 TRAIN evidence; claiming otherwise would overclaim."""
    content = relative_protocol_content()
    assert content["train_oof_status"] == "DEVELOPMENT_EVIDENCE_FOR_THIS_PROTOCOL"


def test_the_protocol_hash_is_deterministic() -> None:
    assert compute_relative_protocol_hash() == compute_relative_protocol_hash()


# ---------------------------------------------------------------------------------------- #
# the feasibility record
# ---------------------------------------------------------------------------------------- #
def test_the_feasibility_artifact_records_a_dense_domain(feasibility: dict[str, Any]) -> None:
    coverage = feasibility["coverage"]
    assert coverage["present_cells"] == 200
    assert coverage["missing_cells"] == 0
    assert coverage["admitted"] == 200
    assert coverage["bam_count"] == 50


def test_the_headroom_is_recorded_honestly(feasibility: dict[str, Any]) -> None:
    """A perfect oracle over these four configs gains 0.0150 mean utility. That is the ceiling."""
    assert feasibility["oracle4_mean_gain_over_safe"] == pytest.approx(0.014976328450755626)
    assert feasibility["safe_baseline_four_domain_cvar_regret"] == pytest.approx(
        0.05608717452333845
    )
    assert feasibility["safe_strictly_best_bams"] == 34
    assert feasibility["another_finalist_better_bams"] == 16
    assert feasibility["safe_baseline_four_domain_zero_regret_fraction"] == pytest.approx(0.68)


def test_every_alternative_loses_more_often_than_it_wins(feasibility: dict[str, Any]) -> None:
    """The asymmetry the switch margin exists to respect."""
    for config, stats in feasibility["per_alternative"].items():
        assert stats["loss"] > stats["win"], config
        assert stats["mean_delta"] < 0.0, config


def test_the_feasibility_artifact_records_no_fit_and_no_validation(
    feasibility: dict[str, Any],
) -> None:
    assert feasibility["no_model_fitted"] is True
    assert feasibility["validation_read"] is False
    assert feasibility["test_accessed"] is False
    assert feasibility["models_qualified_status"] == "HOLD_NO_TRAIN_PROMOTABLE_MODEL"


def test_the_feasibility_artifact_binds_the_v1_freeze_as_parent(
    feasibility: dict[str, Any],
) -> None:
    assert feasibility["parent_campaign_freeze_identity"] == PARENT_CAMPAIGN_FREEZE_IDENTITY


# ---------------------------------------------------------------------------------------- #
# locks
# ---------------------------------------------------------------------------------------- #
def test_campaign_v1_is_untouched() -> None:
    from minos_engine.models.campaign_freeze import (
        CAMPAIGN_FREEZE_PATH,
        campaign_freeze_identity,
        verify_campaign_freeze,
    )

    freeze = json.loads((repository_root() / CAMPAIGN_FREEZE_PATH).read_bytes())
    assert verify_campaign_freeze(freeze)["ok"] is True
    assert campaign_freeze_identity(freeze) == PARENT_CAMPAIGN_FREEZE_IDENTITY
    assert freeze["shortlist"] == []
    assert freeze["validation_authorized_for_campaign_v1"] is False


def test_no_v2_model_was_fitted() -> None:
    root = repository_root()
    assert not (root / "reports/layer2/l2g-v2-train-oof-campaign-result.json").exists()
    from tests.minos_scratch import CANONICAL_MINOS_ROOT

    assert not (CANONICAL_MINOS_ROOT / "minos_l2g_v2_train_oof").exists()


def test_the_v2_source_reads_no_validation_and_no_test() -> None:
    root = repository_root() / "src/minos_engine/models"
    for name in (
        "relative_finalist_contract.py",
        "relative_finalist_dataset.py",
        "relative_finalist_protocol.py",
    ):
        source = (root / name).read_text(encoding="utf-8")
        assert "l2f2_validation" not in source
        assert "phase_d_selection" not in source
        assert "TEST_TRUTH" not in source
    content = relative_protocol_content()
    assert content["new_validation_gatk_authorized"] is False
    assert content["test_lock"] == "SEALED_UNTIL_L2_I"


def test_models_qualified_still_absent_and_select_config_blocked() -> None:
    from minos_engine.layer2.service import Layer2Service

    assert not (repository_root() / "gates/models-qualified.json").exists()
    with pytest.raises(Exception) as excinfo:
        Layer2Service().select_config(None)  # type: ignore[arg-type]
    assert "StageNotReady" in type(excinfo.value).__name__


def test_the_feasibility_artifact_is_committed_and_parses() -> None:
    path = repository_root() / _FEASIBILITY
    assert path.is_file()
    assert isinstance(json.loads(path.read_bytes()), dict)
    assert Path(path).stat().st_size > 0
