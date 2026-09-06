"""The committed v2 TRAIN campaign freeze, and the rules that make it a record rather than a claim.

These tests read the committed freeze and rebuild it from the real published campaign tree when
that tree is present. Nothing here fits a model: the campaign has already run, and re-running it
to check its own freeze would be a second scientific attempt wearing a bookkeeping disguise.
"""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from minos_engine.common.canonical_json import canonical_json_bytes
from minos_engine.models.relative_finalist_authority import (
    ACCEPTED_RELATIVE_CONTRACT_HASH,
    ACCEPTED_RELATIVE_DATASET_IDENTITY,
    ACCEPTED_V2_PREFIT_AUTHORITY_SHA256,
    PARENT_V1_CAMPAIGN_FREEZE,
    V2_OUTPUT_ROOT,
)
from minos_engine.models.relative_finalist_contract import (
    SAFE_BASELINE_CONFIG_HASH,
    compute_finalist_domain_hash,
)
from minos_engine.models.relative_finalist_freeze import (
    MODELS_QUALIFIED_STATUS_HOLD,
    OUTCOME_NO_CONTEXTUAL_SELECTOR,
    RESEARCH_DISPOSITION_CLOSED,
    V2_FREEZE_PATH,
    V2_FREEZE_SCHEMA,
    V2CampaignFreezeError,
    build_v2_campaign_freeze,
    v2_campaign_freeze_identity,
    verify_v2_campaign_freeze,
)
from minos_engine.models.relative_finalist_protocol import (
    build_v2_spec_hashes,
    compute_relative_protocol_hash,
)
from minos_engine.qualification.l2f_accepted_identities import repository_root
from tests.minos_scratch import CANONICAL_MINOS_ROOT

ACCEPTED_FREEZE_IDENTITY = "42310a97f2e13d516b57789bbfa0cd6ee6e44d7e732747dd44ace3aad9d33de5"
ACCEPTED_CAMPAIGN_RESULT_IDENTITY = (
    "db0348c546e46cd086fe59022a2b2b06f76f8602067f6b8e8dcdbc42c26bf7ba"
)
SAFE_MEAN = 0.014976328450755624
SAFE_CVAR = 0.05608717452333845
RIDGE_MEAN = 0.018421250754884117
RIDGE_CVAR = 0.06846296849085629


@pytest.fixture(scope="module")
def frozen() -> dict[str, Any]:
    return dict(json.loads((repository_root() / V2_FREEZE_PATH).read_bytes()))


# ---------------------------------------------------------------------------------------- #
# the committed freeze
# ---------------------------------------------------------------------------------------- #
def test_the_committed_freeze_verifies_and_keeps_its_identity(frozen: dict[str, Any]) -> None:
    report = verify_v2_campaign_freeze(frozen)
    assert report["ok"] is True
    assert report["complete_spec_count"] == 4
    assert report["shortlist_size"] == 0
    assert v2_campaign_freeze_identity(frozen) == ACCEPTED_FREEZE_IDENTITY


def test_the_committed_freeze_is_canonical_bytes(frozen: dict[str, Any]) -> None:
    """Reload, recanonicalise, recompute: the file is the record, not the object that made it."""
    path = repository_root() / V2_FREEZE_PATH
    raw = path.read_bytes()
    assert canonical_json_bytes(frozen) == raw
    assert v2_campaign_freeze_identity(json.loads(raw)) == ACCEPTED_FREEZE_IDENTITY


def test_it_binds_the_campaign_it_froze(frozen: dict[str, Any]) -> None:
    assert frozen["schema_version"] == V2_FREEZE_SCHEMA
    assert frozen["campaign_result_identity"] == ACCEPTED_CAMPAIGN_RESULT_IDENTITY
    assert (
        frozen["campaign_result_file_sha256"]
        == "caf3dfee9b0165a5382cb5d25082e85a43800ef7a1591ef875ee4d00233bf3b1"
    )
    assert frozen["campaign_result_size_bytes"] == 68783
    assert frozen["execution_source_commit"] == "3d1d8b8cc8c69e2ff89a2b8a0d5b056120c81958"
    assert frozen["execution_source_tree"] == "1db1ca4421c746fe22eec06a2fdd628b292246f3"
    assert frozen["prefit_authority_sha256"] == ACCEPTED_V2_PREFIT_AUTHORITY_SHA256
    assert frozen["prefit_authority_schema"] == "l2g-v2-prefit-authority-v4"
    assert frozen["parent_campaign_freeze_identity"] == PARENT_V1_CAMPAIGN_FREEZE


def test_it_binds_the_frozen_science(frozen: dict[str, Any]) -> None:
    a = frozen["authorities"]
    assert a["relative_protocol_hash"] == compute_relative_protocol_hash()
    assert a["relative_contract_hash"] == ACCEPTED_RELATIVE_CONTRACT_HASH
    assert a["relative_dataset_identity"] == ACCEPTED_RELATIVE_DATASET_IDENTITY
    assert a["finalist_domain_hash"] == compute_finalist_domain_hash()
    assert (
        a["expected_relative_cell_set_hash"]
        == "2142e4e3d31f09f727891a0d0555f70fc52a26e82f2fdfc76aa3c57c7f4d7f85"
    )
    assert (
        a["expected_bam_chromosome_set_hash"]
        == "8b28b073d7a25bb0e37a06418b4fe87ef6543b0215554d0c8707ac3ff199cd3c"
    )
    assert list(frozen["candidate_spec_hashes"]) == list(
        build_v2_spec_hashes(ACCEPTED_RELATIVE_DATASET_IDENTITY)
    )


# ---------------------------------------------------------------------------------------- #
# the scientific result
# ---------------------------------------------------------------------------------------- #
def test_all_four_specs_completed_and_none_failed(frozen: dict[str, Any]) -> None:
    assert frozen["training_failure_spec_hashes"] == []
    assert len(frozen["eligible_complete_spec_hashes"]) == 4
    for entry in frozen["per_spec"]:
        assert entry["status"] == "COMPLETE"
        assert entry["outer_fold_count"] == 5
        assert entry["observed_oof_record_count"] == 150
        assert entry["observed_decision_count"] == 50
        assert entry["observed_margin_count"] == 5
        assert len(entry["margins"]) == 5


def test_the_two_histgb_selectors_never_switched_and_therefore_tied(
    frozen: dict[str, Any],
) -> None:
    """A no-op selector reproduces SAFE exactly. Tying both bars is what the third part refuses."""
    histgb = [e for e in frozen["per_spec"] if e["family"] == "RELATIVE_HISTGB_SHARED"]
    assert len(histgb) == 2
    for entry in histgb:
        assert entry["switch_behaviour"]["switch_count"] == 0
        assert entry["switch_behaviour"]["kept_safe_count"] == 50
        assert entry["promotion"]["mean_regret"] == SAFE_MEAN
        assert entry["promotion"]["cvar_regret"] == SAFE_CVAR
        assert entry["promotion"]["mean_no_worse_than_safe"] is True
        assert entry["promotion"]["cvar_no_worse_than_safe"] is True
        assert entry["promotion"]["strictly_improves_mean"] is False
        assert entry["promotion"]["strictly_improves_cvar"] is False
        assert entry["promotion"]["passes_three_part_rule"] is False
        assert entry["promotion"]["shortlisted"] is False


def test_the_two_ridge_selectors_switched_three_times_and_all_three_hurt(
    frozen: dict[str, Any],
) -> None:
    ridge = [e for e in frozen["per_spec"] if e["family"] == "RELATIVE_RIDGE_SHARED"]
    assert len(ridge) == 2
    for entry in ridge:
        behaviour = entry["switch_behaviour"]
        assert behaviour["switch_count"] == 3
        assert behaviour["helpful_switch_count"] == 0
        assert behaviour["harmful_switch_count"] == 3
        assert behaviour["switch_precision"] == 0.0
        assert behaviour["total_realised_delta_vs_safe"] == -0.17224611520642463
        assert behaviour["worst_harmful_switch_delta"] == -0.07185638738326411
        assert entry["promotion"]["mean_regret"] == RIDGE_MEAN
        assert entry["promotion"]["cvar_regret"] == RIDGE_CVAR
        assert entry["promotion"]["mean_no_worse_than_safe"] is False
        assert entry["promotion"]["cvar_no_worse_than_safe"] is False
        assert entry["promotion"]["passes_three_part_rule"] is False


def test_the_references_are_the_frozen_bars(frozen: dict[str, Any]) -> None:
    safe = frozen["reference_metrics"]["ALWAYS_SAFE_BASELINE"]
    assert safe["mean_regret"] == SAFE_MEAN == frozen["safe_baseline_mean_regret"]
    assert safe["cvar_regret"] == SAFE_CVAR == frozen["safe_baseline_cvar_regret"]
    assert safe["max_regret"] == 0.18246926709464156
    assert safe["zero_regret_fraction"] == 0.68
    assert safe["switch_fraction"] == 0.0
    oracle = frozen["reference_metrics"]["ORACLE4"]
    assert oracle["mean_regret"] == 0.0
    assert oracle["cvar_regret"] == 0.0


def test_the_outcome_closes_contextual_research(frozen: dict[str, Any]) -> None:
    assert frozen["shortlist"] == []
    assert frozen["shortlist_empty"] is True
    assert frozen["campaign_outcome"] == OUTCOME_NO_CONTEXTUAL_SELECTOR
    assert frozen["research_disposition"] == RESEARCH_DISPOSITION_CLOSED
    assert frozen["models_qualified_status"] == MODELS_QUALIFIED_STATUS_HOLD
    assert frozen["production_fallback_config_hash"] == SAFE_BASELINE_CONFIG_HASH
    assert frozen["validation_authorized_for_v2"] is False
    assert frozen["validation_read"] is False
    assert frozen["test_accessed"] is False


def test_the_thread_evidence_is_single_threaded(frozen: dict[str, Any]) -> None:
    assert frozen["thread_policy"] == "SINGLE_THREADED_DETERMINISTIC"
    assert frozen["thread_report"]
    for pool in frozen["thread_report"]:
        assert pool["num_threads"] == 1


# ---------------------------------------------------------------------------------------- #
# the verifier fails closed
# ---------------------------------------------------------------------------------------- #
def _edited(frozen: dict[str, Any], mutate: Any) -> dict[str, Any]:
    copied = copy.deepcopy(frozen)
    mutate(copied)
    return copied


def _shortlist_a_tie(content: dict[str, Any]) -> None:
    entry = next(e for e in content["per_spec"] if e["family"] == "RELATIVE_HISTGB_SHARED")
    content["shortlist"] = [entry["spec_hash"]]
    content["shortlist_empty"] = False
    entry["promotion"]["shortlisted"] = True
    entry["promotion"]["passes_three_part_rule"] = True


def _soften_the_bar(content: dict[str, Any]) -> None:
    content["safe_baseline_mean_regret"] = 1.0
    content["safe_baseline_cvar_regret"] = 1.0


def _claim_a_strict_improvement(content: dict[str, Any]) -> None:
    content["per_spec"][0]["promotion"]["strictly_improves_mean"] = True


def _open_validation(content: dict[str, Any]) -> None:
    content["validation_authorized_for_v2"] = True


def _reopen_research(content: dict[str, Any]) -> None:
    content["research_disposition"] = "CONTEXTUAL_SELECTOR_RESEARCH_CONTINUES"


def _swap_the_fallback(content: dict[str, Any]) -> None:
    content["production_fallback_config_hash"] = "0" * 64


def _drop_a_spec(content: dict[str, Any]) -> None:
    content["per_spec"] = content["per_spec"][:3]


def _multithread(content: dict[str, Any]) -> None:
    content["thread_report"] = [dict(p, num_threads=2) for p in content["thread_report"]]


def _foreign_protocol(content: dict[str, Any]) -> None:
    content["authorities"]["relative_protocol_hash"] = "a" * 64


def _non_zero_oracle(content: dict[str, Any]) -> None:
    content["reference_metrics"]["ORACLE4"]["mean_regret"] = 0.01


EDITS = (
    ("shortlisting_a_tie", _shortlist_a_tie),
    ("softening_the_bar", _soften_the_bar),
    ("claiming_a_strict_improvement", _claim_a_strict_improvement),
    ("opening_validation", _open_validation),
    ("reopening_research", _reopen_research),
    ("swapping_the_fallback", _swap_the_fallback),
    ("dropping_a_spec", _drop_a_spec),
    ("multithreading", _multithread),
    ("foreign_protocol", _foreign_protocol),
    ("non_zero_oracle", _non_zero_oracle),
)


@pytest.mark.parametrize("label,mutate", EDITS, ids=[e[0] for e in EDITS])
def test_an_edited_freeze_is_refused(frozen: dict[str, Any], label: str, mutate: Any) -> None:
    with pytest.raises(V2CampaignFreezeError):
        verify_v2_campaign_freeze(_edited(frozen, mutate))


# ---------------------------------------------------------------------------------------- #
# rebuilt from the real published tree
# ---------------------------------------------------------------------------------------- #
def _campaign_root() -> Path:
    return CANONICAL_MINOS_ROOT / V2_OUTPUT_ROOT


@pytest.mark.skipif(
    not (CANONICAL_MINOS_ROOT / V2_OUTPUT_ROOT / "campaign-result.json").is_file(),
    reason="the published v2 campaign tree is not on this machine",
)
def test_rebuilding_from_the_published_tree_reproduces_the_committed_freeze() -> None:
    """Byte-for-byte, from the verified tree. The freeze is derived, never authored."""
    rebuilt = build_v2_campaign_freeze(
        campaign_root=_campaign_root(), repository_root=repository_root()
    )
    committed = json.loads((repository_root() / V2_FREEZE_PATH).read_bytes())
    assert canonical_json_bytes(rebuilt) == canonical_json_bytes(committed)
    assert v2_campaign_freeze_identity(rebuilt) == ACCEPTED_FREEZE_IDENTITY


@pytest.mark.skipif(
    not (CANONICAL_MINOS_ROOT / V2_OUTPUT_ROOT / "campaign-result.json").is_file(),
    reason="the published v2 campaign tree is not on this machine",
)
def test_the_frozen_artifact_hashes_match_the_published_files() -> None:
    committed = json.loads((repository_root() / V2_FREEZE_PATH).read_bytes())
    root = _campaign_root()
    for entry in committed["per_spec"]:
        for stem, sha_field, size_field in (
            ("oof", "oof_file_sha256", "oof_size_bytes"),
            ("metrics", "metric_file_sha256", "metric_size_bytes"),
        ):
            path = root / stem / f"{entry['spec_hash']}.json"
            raw = path.read_bytes()
            assert hashlib.sha256(raw).hexdigest() == entry[sha_field]
            assert len(raw) == entry[size_field]


def test_the_freeze_module_fits_nothing() -> None:
    """§8: evidence freezing only. No runner, no estimator, anywhere in this module."""
    import inspect

    from minos_engine.models import relative_finalist_freeze as module

    source = inspect.getsource(module)
    for forbidden in (
        "run_real_l2g_v2_train_oof_campaign",
        "run_relative_outer_oof",
        "run_frozen_candidates",
        "build_estimator",
        ".fit(",
    ):
        assert forbidden not in source, f"the freeze module references {forbidden}"
