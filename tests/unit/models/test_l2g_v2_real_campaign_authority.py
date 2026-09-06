"""v2 real-campaign authority: the no-op loophole, trusted evidence, and the bundle correction.

The most important test here is the first one. Every v2 policy can fall back to SAFE_BASELINE on
every BAM simply by learning a large margin, which reproduces ALWAYS_SAFE_BASELINE exactly. Under
the old two-part rule that tie qualified — promoting a selector for demonstrating no contextual
value at all.
"""

from __future__ import annotations

import copy
import hashlib
import inspect
import json
from pathlib import Path
from typing import Any

import pytest

from minos_engine.models.contract import CV_FOLD_CHROMOSOMES
from minos_engine.models.relative_finalist_authority import (
    ACCEPTED_CONFIG_ENCODING_IDENTITY,
    ACCEPTED_FEATURE_MATRIX_HASH,
    ACCEPTED_FEATURE_SET_HASH,
    ACCEPTED_RELATIVE_DATASET_IDENTITY,
    PARENT_V1_CAMPAIGN_FREEZE,
    RelativeAuthorityError,
    build_trusted_final_train_bundle,
    evaluate_future_validation,
    run_real_l2g_v2_train_oof_campaign,
)
from minos_engine.models.relative_finalist_contract import (
    ALTERNATIVE_FINALISTS,
    FINALIST_DOMAIN,
    SAFE_BASELINE_CONFIG_HASH,
    compute_finalist_domain_hash,
    compute_relative_contract_hash,
)
from minos_engine.models.relative_finalist_evidence import (
    _CAMPAIGN_TOKEN,
    V2_CAMPAIGN_RESULT_SCHEMA,
    V2_METRIC_ARTIFACT_SCHEMA,
    V2_OOF_ARTIFACT_SCHEMA,
    V2_OUTPUT_LAYOUT,
    TrustedL2GV2TrainCampaign,
    V2EvidenceError,
    assess_v2_completeness,
    mint_trusted_v2_campaign,
    verify_published_l2g_v2_train_campaign,
    verify_v2_campaign_result,
    write_l2g_v2_train_campaign_outputs,
)
from minos_engine.models.relative_finalist_protocol import (
    FUTURE_VALIDATION_RULE,
    PROMOTION_RULE,
    RELATIVE_PROTOCOL_SCHEMA,
    SPEC_SCHEMA,
    SUPERSEDED_PROTOCOL_V2,
    V2_CANDIDATE_GRID,
    build_v2_spec_content,
    build_v2_spec_hashes,
    compute_relative_protocol_hash,
    qualifies_against_bar,
)
from minos_engine.models.relative_finalist_reconstruction import (
    build_frozen_scientific_reference,
)
from minos_engine.models.relative_finalist_runner import (
    delta_diagnostics,
    policy_metrics,
    reference_decisions,
)
from minos_engine.models.runtime import compute_training_runtime_hash
from minos_engine.qualification.l2f_accepted_identities import repository_root
from minos_engine.qualification.provenance import GitProvenance, read_provenance

_AUTHORITY = "reports/layer2/l2g-v2-prefit-authority.json"


# ---------------------------------------------------------------------------------------- #
# DEFECT A -- the no-op loophole
# ---------------------------------------------------------------------------------------- #
def test_tying_the_safe_baseline_on_both_bars_does_not_qualify() -> None:
    """The whole point: a selector that never switches reproduces SAFE exactly."""
    assert (
        qualifies_against_bar(mean_regret=0.10, cvar_regret=0.20, bar_mean=0.10, bar_cvar=0.20)
        is False
    )
    assert PROMOTION_RULE["tie_on_both_bars_qualifies"] is False
    assert PROMOTION_RULE["no_op_selector_can_qualify"] is False


def test_improving_one_bar_and_tying_the_other_qualifies() -> None:
    assert (
        qualifies_against_bar(mean_regret=0.09, cvar_regret=0.20, bar_mean=0.10, bar_cvar=0.20)
        is True
    )
    assert (
        qualifies_against_bar(mean_regret=0.10, cvar_regret=0.19, bar_mean=0.10, bar_cvar=0.20)
        is True
    )


@pytest.mark.parametrize(("mean", "cvar"), [(0.20, 0.10), (0.09, 0.30), (0.50, 0.50)])
def test_worsening_either_bar_never_qualifies(mean: float, cvar: float) -> None:
    assert (
        qualifies_against_bar(mean_regret=mean, cvar_regret=cvar, bar_mean=0.10, bar_cvar=0.20)
        is False
    )


def test_the_rule_uses_exact_comparisons_with_no_epsilon() -> None:
    assert PROMOTION_RULE["epsilon"] is None
    assert PROMOTION_RULE["comparison"] == "EXACT_FULL_PRECISION"
    tiny = 1e-15
    assert (
        qualifies_against_bar(mean_regret=0.1 - tiny, cvar_regret=0.2, bar_mean=0.1, bar_cvar=0.2)
        is True
    )


def test_the_future_validation_rule_closes_the_same_loophole() -> None:
    assert FUTURE_VALIDATION_RULE["tie_on_both_bars_qualifies"] is False
    assert FUTURE_VALIDATION_RULE["no_op_selector_can_qualify"] is False
    result = evaluate_future_validation(
        train_shortlist=("a" * 64,),
        selector_metrics={"a" * 64: {"mean_regret": 0.10, "cvar_regret": 0.20}},
        safe_baseline_metrics={"mean_regret": 0.10, "cvar_regret": 0.20},
    )
    assert result["qualified"] == []
    assert result["models_qualified_status"] == "HOLD_NO_VALIDATION_QUALIFIED_SELECTOR"


def test_a_validation_selector_improving_one_bar_qualifies() -> None:
    result = evaluate_future_validation(
        train_shortlist=("a" * 64,),
        selector_metrics={"a" * 64: {"mean_regret": 0.09, "cvar_regret": 0.20}},
        safe_baseline_metrics={"mean_regret": 0.10, "cvar_regret": 0.20},
    )
    assert result["qualified"] == ["a" * 64]


# ---------------------------------------------------------------------------------------- #
# versioning and estimator semantics
# ---------------------------------------------------------------------------------------- #
def test_the_protocol_and_spec_moved_to_v3() -> None:
    assert RELATIVE_PROTOCOL_SCHEMA == "l2g-relative-finalist-protocol-v3"
    assert SPEC_SCHEMA == "l2g-relative-finalist-spec-v3"
    assert SUPERSEDED_PROTOCOL_V2 == "SUPERSEDED_BEFORE_FIRST_V2_MODEL_FIT"


def test_the_relative_contract_and_dataset_did_not_move() -> None:
    from minos_engine.models.prefit_loader import load_verified_training_dataset
    from minos_engine.models.relative_finalist_dataset import (
        build_relative_finalist_dataset,
    )

    dataset = build_relative_finalist_dataset(load_verified_training_dataset())
    assert dataset.identity() == ACCEPTED_RELATIVE_DATASET_IDENTITY
    assert compute_relative_contract_hash() == (
        "dd5aca807bf0499411b9b4279c206d7a0e9a6d50c85bf75c5d319cf7ba2d394e"
    )
    assert compute_finalist_domain_hash() == (
        "11f712430e94bd533e2f330c6f54323731e6787b6235ae1d74fd07f76b9bedb5"
    )


def test_histgb_disables_early_stopping_explicitly() -> None:
    """An internal validation split would sit outside the grouped evaluation design."""
    trees = [c for c in V2_CANDIDATE_GRID if c["family"] == "RELATIVE_HISTGB_SHARED"]
    assert len(trees) == 2
    for recipe in trees:
        params = recipe["hyperparameters"]
        assert params["early_stopping"] is False
        assert params["loss"] == "squared_error"
        assert params["max_depth"] == 2
        assert params["max_iter"] == 100
        assert params["learning_rate"] == 0.05


def test_the_constructed_histgb_carries_those_parameters() -> None:
    from minos_engine.models.relative_finalist_protocol import build_v2_spec_content
    from minos_engine.models.relative_finalist_runner import build_estimator

    recipe = next(c for c in V2_CANDIDATE_GRID if c["family"] == "RELATIVE_HISTGB_SHARED")
    estimator = build_estimator(build_v2_spec_content(recipe, dataset_identity="d" * 64))
    assert estimator.early_stopping is False
    assert estimator.loss == "squared_error"
    assert estimator.random_state == 20260904


def test_ridge_remains_alpha_one() -> None:
    ridge = [c for c in V2_CANDIDATE_GRID if c["family"] == "RELATIVE_RIDGE_SHARED"]
    assert all(c["hyperparameters"]["alpha"] == 1.0 for c in ridge)


# ---------------------------------------------------------------------------------------- #
# the committed authority
# ---------------------------------------------------------------------------------------- #
@pytest.fixture(scope="module")
def authority() -> dict[str, Any]:
    return dict(json.loads((repository_root() / _AUTHORITY).read_bytes()))


def test_the_authority_binds_the_v3_procedure(authority: dict[str, Any]) -> None:
    """The authority is v4; the PROTOCOL it binds is still v3 and must not move."""
    assert authority["schema_version"] == "l2g-v2-prefit-authority-v4"
    assert authority["protocol_hash"] == compute_relative_protocol_hash()
    assert authority["relative_dataset_identity"] == ACCEPTED_RELATIVE_DATASET_IDENTITY
    recorded = [e["spec_hash"] for e in authority["candidate_spec_hashes"]]
    assert recorded == list(build_v2_spec_hashes(ACCEPTED_RELATIVE_DATASET_IDENTITY))
    assert authority["promotion_rule"]["no_op_selector_can_qualify"] is False
    assert authority["future_validation_rule"]["tie_on_both_bars_qualifies"] is False
    assert authority["no_v2_model_fitted"] is True


def test_the_authority_records_the_explicit_histgb_parameters(authority: dict[str, Any]) -> None:
    trees = [
        e for e in authority["candidate_spec_hashes"] if e["family"] == "RELATIVE_HISTGB_SHARED"
    ]
    assert all(e["hyperparameters"]["early_stopping"] is False for e in trees)


# ---------------------------------------------------------------------------------------- #
# DEFECT B -- trusted evidence
# ---------------------------------------------------------------------------------------- #
def _accepted_spec(spec_hash: str) -> dict[str, Any]:
    """The accepted ModelSpec for a frozen hash, so no fixture invents a family."""
    hashes = list(build_v2_spec_hashes(ACCEPTED_RELATIVE_DATASET_IDENTITY))
    return build_v2_spec_content(
        V2_CANDIDATE_GRID[hashes.index(spec_hash)],
        dataset_identity=ACCEPTED_RELATIVE_DATASET_IDENTITY,
    )


def _campaign(modes: tuple[str, str, str, str]) -> tuple[Any, dict[str, Any], Any]:
    """REAL identities, REAL labels, REAL utilities; the POLICIES are synthetic.

    The offline verifier reconstructs the frozen dataset and checks every published label, utility
    and decision against it, so a fixture cannot invent utilities any more -- and should not have
    been able to before. What it may choose is how each synthetic candidate behaves: ``noop`` sets
    a margin nothing can exceed and therefore keeps SAFE everywhere, while ``good`` sets a margin
    of zero and switches wherever the frozen advantage is positive. Both publish decisions that
    genuinely follow from their own predictions under the frozen switch rule.
    """
    frozen = build_frozen_scientific_reference()
    bams = frozen.chromosome_of
    utility = frozen.utility
    delta = frozen.delta
    cells = [[b, c] for b in sorted(bams) for c in ALTERNATIVE_FINALISTS]
    # a margin no predicted advantage can exceed, so "noop" keeps SAFE under the strict rule
    never = max(delta.values()) + 1.0

    references: dict[str, Any] = {}
    for name in ("ALWAYS_SAFE_BASELINE", "GLOBAL_BEST_FINALIST_FROM_OUTER_TRAIN", "ORACLE4"):
        decisions = []
        for chromosome in CV_FOLD_CHROMOSOMES:
            held = sorted(b for b, c in bams.items() if c == chromosome)
            train = sorted(b for b in bams if b not in set(held))
            decisions.extend(
                reference_decisions(
                    name,
                    utility=utility,
                    held_bams=held,
                    training_bams=train,
                    chromosome_of=bams,
                    outer_fold=chromosome,
                )
            )
        references[name] = {
            "metrics": policy_metrics(decisions),
            # the decisions themselves: the promotion bar is recomputed from them, never trusted
            "decisions": [d.content() for d in decisions],
            "decision_count": len(decisions),
        }

    class _D:
        def __init__(self, d: dict[str, Any]) -> None:
            self.__dict__.update(d)

    hashes = build_v2_spec_hashes(ACCEPTED_RELATIVE_DATASET_IDENTITY)
    per_spec: dict[str, Any] = {}
    for spec_hash, mode in zip(hashes, modes, strict=True):
        margin = never if mode == "noop" else 0.0
        records, decisions = [], []
        for b in sorted(bams):
            ch = bams[b]
            predictions = {c: delta[(b, c)] for c in ALTERNATIVE_FINALISTS}
            for c in ALTERNATIVE_FINALISTS:
                records.append(
                    {
                        "spec_hash": spec_hash,
                        "dataset_id": b,
                        "chromosome": ch,
                        "alternative_config": c,
                        "actual_delta": delta[(b, c)],
                        "predicted_delta": predictions[c],
                        "outer_fold": ch,
                    }
                )
            best = max(predictions.values())
            argmax = sorted(c for c, v in predictions.items() if v == best)[0]
            switched = best > margin
            selected = argmax if switched else SAFE_BASELINE_CONFIG_HASH
            oracle = max(utility[(b, c)] for c in FINALIST_DOMAIN)
            decisions.append(
                {
                    "spec_hash": spec_hash,
                    "dataset_id": b,
                    "chromosome": ch,
                    "outer_fold": ch,
                    "predictions": dict(sorted(predictions.items())),
                    "margin": margin,
                    "selected_config": selected,
                    "switched": switched,
                    "safe_utility": utility[(b, SAFE_BASELINE_CONFIG_HASH)],
                    "selected_utility": utility[(b, selected)],
                    "oracle4_utility": oracle,
                    "regret": oracle - utility[(b, selected)],
                    "actual_selected_delta": (
                        utility[(b, selected)] - utility[(b, SAFE_BASELINE_CONFIG_HASH)]
                    ),
                }
            )

        class _R:
            def __init__(self, d: dict[str, Any]) -> None:
                self.actual_delta = d["actual_delta"]
                self.predicted_delta = d["predicted_delta"]

        per_spec[spec_hash] = {
            "spec_hash": spec_hash,
            "family": _accepted_spec(spec_hash)["family"],
            "implementation": _accepted_spec(spec_hash)["implementation"],
            "records": records,
            "decisions": decisions,
            "margins": dict.fromkeys(CV_FOLD_CHROMOSOMES, margin),
            "metrics": policy_metrics([_D(d) for d in decisions]),
            # the frozen diagnostic definition, over these exact records
            "diagnostics": delta_diagnostics([_R(r) for r in records]),
            "training_failures": [],
            "expected_cell_set": cells,
        }
    bar = references["ALWAYS_SAFE_BASELINE"]["metrics"]
    shortlist = tuple(
        sorted(
            h
            for h, e in per_spec.items()
            if qualifies_against_bar(
                mean_regret=e["metrics"]["mean_regret"],
                cvar_regret=e["metrics"]["cvar_regret"],
                bar_mean=bar["mean_regret"],
                bar_cvar=bar["cvar_regret"],
            )
        )
    )
    real = read_provenance(repository_root())
    authority = {
        "execution_source_commit": real.head_sha,
        "execution_source_tree": real.tree_sha,
        "prefit_authority_sha256": hashlib.sha256(
            (repository_root() / _AUTHORITY).read_bytes()
        ).hexdigest(),
        "parent_campaign_freeze_identity": PARENT_V1_CAMPAIGN_FREEZE,
        "relative_dataset_identity": ACCEPTED_RELATIVE_DATASET_IDENTITY,
        "relative_protocol_hash": compute_relative_protocol_hash(),
        "relative_contract_hash": compute_relative_contract_hash(),
        "finalist_domain_hash": compute_finalist_domain_hash(),
        "feature_set_hash": ACCEPTED_FEATURE_SET_HASH,
        "feature_matrix_hash": ACCEPTED_FEATURE_MATRIX_HASH,
        "config_encoding_identity": ACCEPTED_CONFIG_ENCODING_IDENTITY,
        "training_runtime_hash": compute_training_runtime_hash(),
        "candidate_spec_hashes": list(hashes),
        "thread_report": [
            {"internal_api": "openblas", "num_threads": 1, "prefix": "l", "user_api": "blas"}
        ],
    }
    trusted = mint_trusted_v2_campaign(
        _CAMPAIGN_TOKEN,
        authority=authority,
        per_spec=per_spec,
        references=references,
        shortlist=shortlist,
    )
    return trusted, per_spec, hashes


@pytest.fixture(scope="module")
def campaign() -> tuple[Any, dict[str, Any], Any]:
    return _campaign(("noop", "good", "noop", "good"))


@pytest.fixture
def clean_source(monkeypatch: Any) -> None:
    import minos_engine.models.relative_finalist_evidence as module

    real = read_provenance(repository_root())
    monkeypatch.setattr(
        module,
        "read_provenance",
        lambda root: GitProvenance(
            head_sha=real.head_sha,
            tree_sha=real.tree_sha,
            worktree_clean=True,
            parent_sha=real.parent_sha,
        ),
    )


def test_a_no_op_spec_ties_the_bar_exactly_and_is_excluded(
    campaign: tuple[Any, dict[str, Any], Any],
) -> None:
    trusted, per_spec, hashes = campaign
    bar = trusted.references()["ALWAYS_SAFE_BASELINE"]["metrics"]
    for spec_hash in (hashes[0], hashes[2]):
        metrics = per_spec[spec_hash]["metrics"]
        assert metrics["switch_fraction"] == 0.0
        assert metrics["mean_regret"] == bar["mean_regret"]
        assert metrics["cvar_regret"] == bar["cvar_regret"]
        assert spec_hash not in trusted.shortlist
    assert len(trusted.shortlist) == 2


def test_a_caller_cannot_mint_a_trusted_v2_campaign() -> None:
    with pytest.raises(V2EvidenceError, match="may only be minted"):
        TrustedL2GV2TrainCampaign(object(), authority={}, per_spec={}, references={}, shortlist=())


def test_a_raw_dict_cannot_be_published(tmp_path: Path) -> None:
    with pytest.raises(V2EvidenceError, match="only a trusted v2 campaign"):
        write_l2g_v2_train_campaign_outputs({"per_spec": {}}, output_dir=tmp_path / "x")


def test_the_sealed_v2_entry_returns_a_trusted_campaign() -> None:
    assert inspect.signature(run_real_l2g_v2_train_oof_campaign).return_annotation == "Any"
    source = inspect.getsource(run_real_l2g_v2_train_oof_campaign)
    assert "mint_trusted_v2_campaign" in source
    assert "read_provenance(source_root)" in source
    capture = source.index("read_provenance(source_root)")
    # fitting now happens inside the isolating helper, which the entry calls after provenance
    first_fit = source.index("run_frozen_candidates(")
    assert capture < first_fit, "provenance is captured after fitting"


def test_the_trusted_campaign_hands_out_copies(campaign: tuple[Any, dict[str, Any], Any]) -> None:
    trusted, _, hashes = campaign
    borrowed = trusted.spec(hashes[0])
    borrowed["metrics"]["mean_regret"] = -999.0
    assert trusted.spec(hashes[0])["metrics"]["mean_regret"] != -999.0


# ---------------------------------------------------------------------------------------- #
# completeness
# ---------------------------------------------------------------------------------------- #
def _entry(campaign: tuple[Any, dict[str, Any], Any]) -> dict[str, Any]:
    _, per_spec, hashes = campaign
    return copy.deepcopy(per_spec[hashes[1]])


def test_a_complete_spec_has_150_records_50_decisions_and_5_margins(
    campaign: tuple[Any, dict[str, Any], Any],
) -> None:
    report = assess_v2_completeness(_entry(campaign))
    assert report["status"] == "COMPLETE"
    assert report["observed_oof_record_count"] == 150
    assert report["observed_decision_count"] == 50
    assert report["observed_margin_count"] == 5
    assert report["exact_cell_set_verified"] is True


@pytest.mark.parametrize(
    ("mutate", "match"),
    [
        pytest.param(lambda e: e["records"].pop(), "149 of 150", id="149-records"),
        pytest.param(lambda e: e["decisions"].pop(), "49 of 50", id="49-decisions"),
        pytest.param(lambda e: e["margins"].pop("chr22"), "4 of 5", id="4-margins"),
    ],
)
def test_an_incomplete_spec_is_refused(
    campaign: tuple[Any, dict[str, Any], Any], mutate: Any, match: str
) -> None:
    entry = _entry(campaign)
    mutate(entry)
    report = assess_v2_completeness(entry)
    assert report["status"] == "TRAINING_FAILURE"
    assert any(match in reason for reason in report["reasons"])


def test_a_duplicate_relative_cell_is_refused(campaign: tuple[Any, dict[str, Any], Any]) -> None:
    entry = _entry(campaign)
    entry["records"][1] = dict(entry["records"][0])
    report = assess_v2_completeness(entry)
    assert report["status"] == "TRAINING_FAILURE"
    assert report["duplicate_cell_count"] == 1


def test_a_foreign_cell_is_refused(campaign: tuple[Any, dict[str, Any], Any]) -> None:
    entry = _entry(campaign)
    entry["records"][0]["alternative_config"] = "f" * 64
    report = assess_v2_completeness(entry)
    assert report["status"] == "TRAINING_FAILURE"
    assert any("foreign" in reason for reason in report["reasons"])


# ---------------------------------------------------------------------------------------- #
# publication and offline verification
# ---------------------------------------------------------------------------------------- #
@pytest.fixture(scope="module")
def published(
    campaign: tuple[Any, dict[str, Any], Any], tmp_path_factory: pytest.TempPathFactory
) -> dict[str, Any]:
    import minos_engine.models.relative_finalist_evidence as module

    trusted = campaign[0]
    real = read_provenance(repository_root())
    original = module.read_provenance
    module.read_provenance = lambda root: GitProvenance(  # type: ignore[assignment]
        head_sha=real.head_sha,
        tree_sha=real.tree_sha,
        worktree_clean=True,
        parent_sha=real.parent_sha,
    )
    try:
        out = tmp_path_factory.mktemp("v2") / V2_OUTPUT_LAYOUT["root"]
        manifest = write_l2g_v2_train_campaign_outputs(trusted, output_dir=out)
    finally:
        module.read_provenance = original  # type: ignore[assignment]
    return {"manifest": manifest, "dir": out}


def test_publication_writes_artifacts_only_for_complete_specs(published: dict[str, Any]) -> None:
    out = published["dir"]
    assert len(list((out / "oof").glob("*.json"))) == 4
    assert len(list((out / "metrics").glob("*.json"))) == 4
    assert (out / "campaign-result.json").is_file()
    assert oct(out.stat().st_mode & 0o777) == "0o750"
    for path in (out / "oof").glob("*.json"):
        assert oct(path.stat().st_mode & 0o777) == "0o640"


def test_the_whole_tree_verifier_passes(published: dict[str, Any]) -> None:
    report = verify_published_l2g_v2_train_campaign(published["dir"])
    assert report["ok"] is True
    assert report["complete_spec_count"] == 4
    assert report["shortlist_size"] == 2


def test_the_result_schemas_are_the_v2_ones(published: dict[str, Any]) -> None:
    result = json.loads((published["dir"] / "campaign-result.json").read_bytes())
    assert result["schema_version"] == V2_CAMPAIGN_RESULT_SCHEMA
    oof = json.loads(next((published["dir"] / "oof").glob("*.json")).read_bytes())
    assert oof["schema_version"] == V2_OOF_ARTIFACT_SCHEMA
    metric = json.loads(next((published["dir"] / "metrics").glob("*.json")).read_bytes())
    assert metric["schema_version"] == V2_METRIC_ARTIFACT_SCHEMA


def test_a_shortlist_edit_fails_even_when_rehashed(published: dict[str, Any]) -> None:
    result = json.loads((published["dir"] / "campaign-result.json").read_bytes())
    tampered = copy.deepcopy(result)
    tampered["shortlist"] = [e["spec_hash"] for e in tampered["per_spec"]]
    with pytest.raises(V2EvidenceError, match="not what the frozen"):
        verify_v2_campaign_result(tampered)


def test_an_edited_oof_artifact_fails_the_tree_verifier(published: dict[str, Any]) -> None:
    path = next((published["dir"] / "oof").glob("*.json"))
    original = path.read_bytes()
    payload = json.loads(original)
    payload["records"][0]["predicted_delta"] = 99.0
    path.write_bytes(json.dumps(payload).encode("utf-8"))
    try:
        with pytest.raises(V2EvidenceError):
            verify_published_l2g_v2_train_campaign(published["dir"])
    finally:
        path.write_bytes(original)


def test_an_unexpected_evidence_file_is_refused(published: dict[str, Any]) -> None:
    intruder = published["dir"] / "oof" / f"{'f' * 64}.json"
    intruder.write_text("{}")
    try:
        # the whole-tree layout check now names it directly
        with pytest.raises(V2EvidenceError, match="unexpected file"):
            verify_published_l2g_v2_train_campaign(published["dir"])
    finally:
        intruder.unlink()


def test_an_existing_final_tree_is_refused(
    campaign: tuple[Any, dict[str, Any], Any], tmp_path: Path, clean_source: None
) -> None:
    existing = tmp_path / V2_OUTPUT_LAYOUT["root"]
    existing.mkdir()
    assert clean_source is None
    with pytest.raises(V2EvidenceError, match="refusing to overwrite"):
        write_l2g_v2_train_campaign_outputs(campaign[0], output_dir=existing)


def test_a_partial_failure_leaves_no_final_tree(
    campaign: tuple[Any, dict[str, Any], Any],
    tmp_path: Path,
    monkeypatch: Any,
    clean_source: None,
) -> None:
    import minos_engine.models.relative_finalist_evidence as module

    calls = {"n": 0}
    real = module._write_atomic

    def flaky(path: Path, payload: dict[str, Any]) -> dict[str, Any]:
        calls["n"] += 1
        if calls["n"] > 3:
            raise OSError("disk full")
        return real(path, payload)

    monkeypatch.setattr(module, "_write_atomic", flaky)
    out = tmp_path / V2_OUTPUT_LAYOUT["root"]
    assert clean_source is None
    with pytest.raises(OSError, match="disk full"):
        write_l2g_v2_train_campaign_outputs(campaign[0], output_dir=out)
    assert not out.exists()
    assert not list(tmp_path.glob("*.tmp.*"))


# ---------------------------------------------------------------------------------------- #
# DEFECT C -- the bundle authority
# ---------------------------------------------------------------------------------------- #
def test_the_bundle_now_requires_a_verified_published_campaign() -> None:
    """The in-memory campaign is no longer enough; see the producer->publisher suite for the
    end-to-end bundle proofs, which can supply a verified capability."""
    import inspect

    parameters = set(inspect.signature(build_trusted_final_train_bundle).parameters)
    assert parameters == {
        "verified_campaign",
        "spec_hash",
        "estimator_artifact_sha256",
        "transform_artifact_sha256",
    }
    assert not (parameters & {"oof_residuals", "deployment_margin", "trusted_campaign"})
    with pytest.raises(RelativeAuthorityError, match="VERIFIED published"):
        build_trusted_final_train_bundle(
            verified_campaign={"shortlist": ("a" * 64,)},
            spec_hash="a" * 64,
            estimator_artifact_sha256="a" * 64,
            transform_artifact_sha256=None,
        )


def test_the_bundle_never_falls_back_to_a_zero_evidence_identity() -> None:
    import inspect

    source = inspect.getsource(build_trusted_final_train_bundle)
    assert 'entry.get("oof_scientific_hash", "0" * 64)' not in source
    assert "is not a real artifact identity" in source


# ---------------------------------------------------------------------------------------- #
# locks
# ---------------------------------------------------------------------------------------- #
def test_no_real_v2_campaign_output_exists() -> None:
    from tests.minos_scratch import CANONICAL_MINOS_ROOT

    assert not (CANONICAL_MINOS_ROOT / V2_OUTPUT_LAYOUT["root"]).exists()


def test_campaign_v1_is_untouched() -> None:
    from minos_engine.models.campaign_freeze import (
        CAMPAIGN_FREEZE_PATH,
        campaign_freeze_identity,
        verify_campaign_freeze,
    )

    freeze = json.loads((repository_root() / CAMPAIGN_FREEZE_PATH).read_bytes())
    assert verify_campaign_freeze(freeze)["ok"] is True
    assert campaign_freeze_identity(freeze) == PARENT_V1_CAMPAIGN_FREEZE


def test_v2_source_reads_no_validation_and_no_test() -> None:
    root = repository_root() / "src/minos_engine/models"
    for name in (
        "relative_finalist_contract.py",
        "relative_finalist_dataset.py",
        "relative_finalist_protocol.py",
        "relative_finalist_runner.py",
        "relative_finalist_authority.py",
        "relative_finalist_evidence.py",
    ):
        source = (root / name).read_text(encoding="utf-8")
        assert "l2f2_validation" not in source
        assert "phase_d_selection" not in source
        assert "TEST_TRUTH" not in source


def test_models_qualified_absent_and_select_config_blocked() -> None:
    from minos_engine.layer2.service import Layer2Service

    assert not (repository_root() / "gates/models-qualified.json").exists()
    with pytest.raises(Exception) as excinfo:
        Layer2Service().select_config(None)  # type: ignore[arg-type]
    assert "StageNotReady" in type(excinfo.value).__name__
