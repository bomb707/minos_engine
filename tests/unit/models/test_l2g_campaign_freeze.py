"""The frozen record of the first real TRAIN OOF campaign.

Campaign v1 completed every spec and every fold and produced an EMPTY shortlist. These tests pin
that outcome and, more importantly, pin the things that would let it be quietly softened later:
the bars, the per-candidate verdicts, and the fact that VALIDATION is not authorized when there is
no shortlisted candidate for it to choose among.
"""

from __future__ import annotations

import copy
import hashlib
import json
from typing import Any

import pytest

from minos_engine.models.campaign import (
    ACCEPTED_CANDIDATE_SPEC_HASHES,
    ACCEPTED_REFERENCE_SPECS,
)
from minos_engine.models.campaign_freeze import (
    CAMPAIGN_FREEZE_DOMAIN,
    CAMPAIGN_FREEZE_PATH,
    CAMPAIGN_FREEZE_SCHEMA,
    MODELS_QUALIFIED_STATUS_HOLD,
    OUTCOME_NO_CONTEXTUAL_MODEL,
    CampaignFreezeError,
    campaign_freeze_identity,
    verify_campaign_freeze,
)
from minos_engine.models.shortlist import (
    ACCEPTED_AUTHORITIES,
    ACCEPTED_PREFIT_AUTHORITY_SHA256,
)
from minos_engine.qualification.l2f_accepted_identities import repository_root

_FREEZE_IDENTITY = "1c2039dec2f3fbb51a8058c947bbf8de9f9c6d235a133b5948aa6b33ac516673"
_CAMPAIGN_RESULT_IDENTITY = "eddc30a1d5a2d5f4af8ed3c81fadd0d82c192879a8b058606a11e84d866fdede"
_CAMPAIGN_RESULT_FILE_SHA = "4ac6500f0a9f846d724202d38791267d131fe37b3b3f86a40a0321e272cf575f"
_EXECUTION_COMMIT = "c9618bcda752cb2e1c7faa4d5fced92c62db326f"
_EXECUTION_TREE = "30ef183f7a8a0ecc435d189d6bf6f9b745bb1648"


@pytest.fixture(scope="module")
def freeze() -> dict[str, Any]:
    return dict(json.loads((repository_root() / CAMPAIGN_FREEZE_PATH).read_bytes()))


def test_the_committed_freeze_verifies_and_keeps_its_identity(freeze: dict[str, Any]) -> None:
    assert freeze["schema_version"] == CAMPAIGN_FREEZE_SCHEMA
    assert CAMPAIGN_FREEZE_DOMAIN == "minos:l2g-train-oof-campaign-freeze:v1\n"
    report = verify_campaign_freeze(freeze)
    assert report["ok"] is True
    assert report["spec_count"] == 10
    assert campaign_freeze_identity(freeze) == _FREEZE_IDENTITY


def test_the_freeze_binds_the_execution_source_and_campaign_result(freeze: dict[str, Any]) -> None:
    assert freeze["execution_source_commit"] == _EXECUTION_COMMIT
    assert freeze["execution_source_tree"] == _EXECUTION_TREE
    assert freeze["campaign_result_identity"] == _CAMPAIGN_RESULT_IDENTITY
    assert freeze["campaign_result_file_sha256"] == _CAMPAIGN_RESULT_FILE_SHA
    assert freeze["campaign_result_size_bytes"] == 12031
    assert freeze["prefit_authority_sha256"] == ACCEPTED_PREFIT_AUTHORITY_SHA256


def test_the_freeze_binds_every_protected_authority(freeze: dict[str, Any]) -> None:
    assert freeze["authorities"] == dict(ACCEPTED_AUTHORITIES)


def test_the_freeze_binds_all_ten_specs_as_complete(freeze: dict[str, Any]) -> None:
    assert tuple(freeze["candidate_spec_hashes"]) == ACCEPTED_CANDIDATE_SPEC_HASHES
    assert tuple(freeze["reference_spec_hashes"]) == tuple(h for _, h in ACCEPTED_REFERENCE_SPECS)
    assert len(freeze["per_spec"]) == 10
    for entry in freeze["per_spec"]:
        assert entry["status"] == "COMPLETE"
        assert entry["successful_outer_fold_count"] == 5
        assert entry["observed_oof_record_count"] == 1040
        assert entry["unique_bam_count"] == 50
        assert entry["duplicate_cell_count"] == 0
        assert entry["exact_cell_set_verified"] is True
        for field in (
            "oof_scientific_hash",
            "oof_file_sha256",
            "metric_scientific_hash",
            "metric_file_sha256",
        ):
            assert len(entry[field]) == 64


def test_both_reference_bars_were_set_by_the_safe_baseline(freeze: dict[str, Any]) -> None:
    """The fallback the campaign had to beat turned out to be the hardest thing in the field."""
    assert freeze["best_reference_mean_achieved_by"] == ["CONSTANT_SAFE_BASELINE"]
    assert freeze["best_reference_cvar_achieved_by"] == ["CONSTANT_SAFE_BASELINE"]
    assert freeze["best_reference_mean_regret"] == pytest.approx(0.022133686444521378)
    assert freeze["best_reference_cvar_regret"] == pytest.approx(0.07825312618460009)


def test_no_candidate_cleared_either_bar(freeze: dict[str, Any]) -> None:
    rows = freeze["candidate_bar_evaluation"]
    assert len(rows) == 6
    for row in rows:
        assert row["mean_bar_pass"] is False
        assert row["cvar_bar_pass"] is False
        assert row["shortlisted"] is False


def test_the_shortlist_is_empty_and_the_fallback_stands(freeze: dict[str, Any]) -> None:
    assert freeze["shortlist"] == []
    assert freeze["shortlist_empty"] is True
    assert len(freeze["eligible_candidate_hashes"]) == 6
    assert freeze["ineligible_candidate_hashes"] == []
    assert freeze["fallback_if_empty"] == "SAFE_BASELINE_REMAINS_AND_MODELS_QUALIFIED_HOLDS"


def test_the_outcome_is_not_recorded_as_a_training_failure(freeze: dict[str, Any]) -> None:
    """Training succeeded completely; the promotion hypothesis is what failed."""
    assert freeze["campaign_outcome"] == OUTCOME_NO_CONTEXTUAL_MODEL
    assert freeze["campaign_outcome"] == "NO_CONTEXTUAL_MODEL_QUALIFIED_ON_TRAIN"
    assert "FAIL" not in freeze["campaign_outcome"]
    assert all(e["status"] == "COMPLETE" for e in freeze["per_spec"])


def test_validation_is_not_authorized_for_campaign_v1(freeze: dict[str, Any]) -> None:
    """With no shortlisted candidate, opening VALIDATION could only rescue a rejected model."""
    assert freeze["validation_authorized_for_campaign_v1"] is False
    assert freeze["validation_read"] is False
    assert freeze["test_accessed"] is False


def test_models_qualified_is_held_not_passed(freeze: dict[str, Any]) -> None:
    assert freeze["models_qualified_status"] == MODELS_QUALIFIED_STATUS_HOLD
    assert not (repository_root() / "gates/models-qualified.json").exists()


def test_the_thread_evidence_is_single_threaded(freeze: dict[str, Any]) -> None:
    assert freeze["thread_policy"] == "SINGLE_THREADED_DETERMINISTIC"
    assert freeze["thread_report"]
    assert all(p["num_threads"] == 1 for p in freeze["thread_report"])


# ------------------------------------------------------------------------------------------ #
# the verifier fails closed
# ------------------------------------------------------------------------------------------ #
def test_a_manufactured_shortlist_entry_is_refused(freeze: dict[str, Any]) -> None:
    """The one edit that would matter most: promoting the best candidate anyway."""
    tampered = copy.deepcopy(freeze)
    best = min(tampered["candidate_bar_evaluation"], key=lambda r: r["mean_regret"])
    tampered["shortlist"] = [best["spec_hash"]]
    tampered["shortlist_empty"] = False
    with pytest.raises(CampaignFreezeError, match="not what the two-bar rule gives"):
        verify_campaign_freeze(tampered)


def test_a_relaxed_reference_bar_is_refused(freeze: dict[str, Any]) -> None:
    tampered = copy.deepcopy(freeze)
    tampered["best_reference_mean_regret"] = 0.5
    with pytest.raises(CampaignFreezeError, match="not the minimum over the frozen references"):
        verify_campaign_freeze(tampered)


def test_a_flipped_bar_verdict_is_refused(freeze: dict[str, Any]) -> None:
    tampered = copy.deepcopy(freeze)
    tampered["candidate_bar_evaluation"][0]["mean_bar_pass"] = True
    with pytest.raises(CampaignFreezeError, match="disagrees with its own metrics"):
        verify_campaign_freeze(tampered)


def test_authorizing_validation_with_an_empty_shortlist_is_refused(freeze: dict[str, Any]) -> None:
    tampered = copy.deepcopy(freeze)
    tampered["validation_authorized_for_campaign_v1"] = True
    with pytest.raises(CampaignFreezeError, match="nothing for VALIDATION to select among"):
        verify_campaign_freeze(tampered)


def test_recording_a_qualified_status_with_an_empty_shortlist_is_refused(
    freeze: dict[str, Any],
) -> None:
    tampered = copy.deepcopy(freeze)
    tampered["models_qualified_status"] = "PASS"
    with pytest.raises(CampaignFreezeError, match="cannot accompany a qualified status"):
        verify_campaign_freeze(tampered)


def test_relabelling_the_outcome_as_a_training_failure_is_refused(freeze: dict[str, Any]) -> None:
    tampered = copy.deepcopy(freeze)
    tampered["campaign_outcome"] = "MODEL_TRAINING_FAILED"
    with pytest.raises(CampaignFreezeError, match="promotion hypothesis failing"):
        verify_campaign_freeze(tampered)


@pytest.mark.parametrize("field", sorted(ACCEPTED_AUTHORITIES))
def test_a_foreign_authority_is_refused(freeze: dict[str, Any], field: str) -> None:
    tampered = copy.deepcopy(freeze)
    tampered["authorities"][field] = "f" * 64
    with pytest.raises(CampaignFreezeError, match="not the accepted authority"):
        verify_campaign_freeze(tampered)


def test_a_foreign_spec_set_is_refused(freeze: dict[str, Any]) -> None:
    tampered = copy.deepcopy(freeze)
    tampered["candidate_spec_hashes"] = ["f" * 64, *tampered["candidate_spec_hashes"][1:]]
    with pytest.raises(CampaignFreezeError, match="accepted six candidates"):
        verify_campaign_freeze(tampered)


def test_a_downgraded_spec_status_is_refused(freeze: dict[str, Any]) -> None:
    tampered = copy.deepcopy(freeze)
    tampered["per_spec"][0]["successful_outer_fold_count"] = 4
    with pytest.raises(CampaignFreezeError, match="did not run five folds"):
        verify_campaign_freeze(tampered)


def test_any_edit_moves_the_freeze_identity(freeze: dict[str, Any]) -> None:
    tampered = copy.deepcopy(freeze)
    tampered["shortlist"] = ["x"]
    assert campaign_freeze_identity(tampered) != campaign_freeze_identity(freeze)


def test_the_committed_file_hashes_to_what_this_test_pins() -> None:
    path = repository_root() / CAMPAIGN_FREEZE_PATH
    assert path.is_file()
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    assert len(digest) == 64
    assert campaign_freeze_identity(json.loads(path.read_bytes())) == _FREEZE_IDENTITY


def test_the_published_campaign_tree_still_verifies() -> None:
    from minos_engine.models.campaign_evidence import verify_published_l2g_train_campaign
    from tests.minos_scratch import CANONICAL_MINOS_ROOT

    root = CANONICAL_MINOS_ROOT / "minos_l2g_train_oof"
    if not root.is_dir():
        pytest.skip("the published campaign tree is not present on this machine")
    report = verify_published_l2g_train_campaign(root, repository_root=repository_root())
    assert report["ok"] is True
    assert report["complete_spec_count"] == 10
    assert report["shortlist_size"] == 0
