"""``l2g-v2-train-oof-campaign-freeze-v1`` — the real v2 relative-selector campaign, frozen.

The published v2 tree lives outside the repository, so without this nothing in Git records what
the relative-advantage experiment actually produced. Every value here is derived from the VERIFIED
published campaign: the builder demands a ``VerifiedPublishedL2GV2TrainCampaign``, which only the
whole-tree verifier mints and only after every published label, utility, action, regret, reference
and metric has been re-derived from the frozen dataset. A freeze assembled from a summary would be
a claim about a campaign rather than a record of one.

The frozen outcome is an EMPTY shortlist, and it is worth being precise about why, because the two
halves of it failed differently. Both HistGB selectors never switched at all: their learned
margins exceeded every predicted advantage, so they reproduced ``ALWAYS_SAFE_BASELINE`` exactly and
tied it on both bars. Under the three-part rule a tie on both bars does not qualify -- that rule
exists precisely so a selector cannot be promoted for demonstrating no contextual value. Both
Ridge selectors did switch, on the same three chromosome-19 BAMs, and all three switches were
harmful. Nothing failed to train; the promotion hypothesis failed, which is a different statement.

With the whole oracle headroom at 0.0150 mean utility, this closes contextual-selector research
for L2-G. The safe baseline remains the production fallback.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Final

from minos_engine.common.canonical_json import canonical_json_bytes
from minos_engine.common.errors import MinosEngineError
from minos_engine.common.hashing import sha256_hex

__all__ = [
    "MODELS_QUALIFIED_STATUS_HOLD",
    "OUTCOME_NO_CONTEXTUAL_SELECTOR",
    "RESEARCH_DISPOSITION_CLOSED",
    "V2_FREEZE_DOMAIN",
    "V2_FREEZE_PATH",
    "V2_FREEZE_SCHEMA",
    "V2CampaignFreezeError",
    "build_v2_campaign_freeze",
    "v2_campaign_freeze_identity",
    "verify_v2_campaign_freeze",
]

V2_FREEZE_SCHEMA: Final = "l2g-v2-train-oof-campaign-freeze-v1"
V2_FREEZE_DOMAIN: Final = "minos:l2g-v2-train-oof-campaign-freeze:v1\n"
V2_FREEZE_PATH: Final = "reports/layer2/l2g-v2-train-oof-campaign-freeze-v1.json"

#: Training succeeded completely for all four specs. What failed is the promotion hypothesis.
OUTCOME_NO_CONTEXTUAL_SELECTOR: Final = "NO_CONTEXTUAL_SELECTOR_QUALIFIED_ON_TRAIN_V2"

#: v1 asked whether a model could predict utility; v2 asked the strictly easier question of
#: whether it could predict ADVANTAGE over the safe baseline. Both answered no on TRAIN.
RESEARCH_DISPOSITION_CLOSED: Final = "CONTEXTUAL_SELECTOR_RESEARCH_CLOSED"

MODELS_QUALIFIED_STATUS_HOLD: Final = "HOLD_NO_TRAIN_PROMOTABLE_CONTEXTUAL_MODEL"

#: An empty shortlist leaves no frozen contextual candidate for VALIDATION to choose among, so
#: opening VALIDATION could only serve to rescue a selector the TRAIN criterion rejected.
VALIDATION_AUTHORIZED_FOR_V2: Final = False

_DIAGNOSTICS: Final = ("delta_mae", "delta_rmse", "delta_r2", "delta_spearman")
_METRIC_NAMES: Final = (
    "cvar_regret",
    "cvar_tail_count",
    "decision_count",
    "harmful_switch_count",
    "max_regret",
    "mean_gain_on_switch",
    "mean_loss_on_harmful_switch",
    "mean_regret",
    "safe_baseline_kept_fraction",
    "switch_fraction",
    "switch_precision",
    "worst_switch_delta",
    "zero_regret_fraction",
)
_REFERENCES: Final = (
    "ALWAYS_SAFE_BASELINE",
    "GLOBAL_BEST_FINALIST_FROM_OUTER_TRAIN",
    "ORACLE4",
)


class V2CampaignFreezeError(MinosEngineError):
    """The v2 campaign freeze could not be built or does not verify."""


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise V2CampaignFreezeError(message)


def v2_campaign_freeze_identity(content: dict[str, Any]) -> str:
    """Domain-separated identity of a v2 campaign freeze."""
    return sha256_hex(V2_FREEZE_DOMAIN.encode("utf-8") + canonical_json_bytes(content))


def _switch_behaviour(decisions: list[dict[str, Any]]) -> dict[str, Any]:
    """What the policy actually did, recomputed from its own published decisions."""
    switched = [d for d in decisions if d["switched"]]
    helpful = [d for d in switched if float(d["actual_selected_delta"]) > 0.0]
    harmful = [d for d in switched if float(d["actual_selected_delta"]) < 0.0]
    per_chromosome: dict[str, dict[str, int]] = {}
    for decision in decisions:
        fold = str(decision["outer_fold"])
        row = per_chromosome.setdefault(
            fold, {"decisions": 0, "kept_safe": 0, "switched": 0, "helpful": 0, "harmful": 0}
        )
        row["decisions"] += 1
        if not decision["switched"]:
            row["kept_safe"] += 1
            continue
        row["switched"] += 1
        delta = float(decision["actual_selected_delta"])
        if delta > 0.0:
            row["helpful"] += 1
        elif delta < 0.0:
            row["harmful"] += 1
    return {
        "decision_count": len(decisions),
        "kept_safe_count": len(decisions) - len(switched),
        "switch_count": len(switched),
        "helpful_switch_count": len(helpful),
        "harmful_switch_count": len(harmful),
        "switch_precision": (len(helpful) / len(switched)) if switched else None,
        "total_realised_delta_vs_safe": sum(float(d["actual_selected_delta"]) for d in decisions),
        "mean_selected_utility": sum(float(d["selected_utility"]) for d in decisions)
        / len(decisions),
        "worst_harmful_switch_delta": (
            min(float(d["actual_selected_delta"]) for d in harmful) if harmful else None
        ),
        "switched_datasets": sorted(str(d["dataset_id"]) for d in switched),
        "per_chromosome": dict(sorted(per_chromosome.items())),
    }


def build_v2_campaign_freeze(
    *, campaign_root: Path, repository_root: Path | None = None
) -> dict[str, Any]:
    """Derive the freeze from the VERIFIED published v2 campaign. Fits nothing.

    This never touches an estimator: the campaign has already run, and re-running it to produce a
    freeze would be a second scientific attempt wearing a bookkeeping disguise.
    """
    from minos_engine.models.relative_finalist_authority import (
        ACCEPTED_V2_PREFIT_AUTHORITY_SHA256,
        PARENT_V1_CAMPAIGN_FREEZE,
        verify_v2_prefit_authority,
    )
    from minos_engine.models.relative_finalist_contract import SAFE_BASELINE_CONFIG_HASH
    from minos_engine.models.relative_finalist_evidence import (
        STATUS_COMPLETE,
        V2_OUTPUT_LAYOUT,
        load_verified_published_l2g_v2_campaign,
    )
    from minos_engine.models.relative_finalist_protocol import qualifies_against_bar

    root = Path(campaign_root)
    repo = Path(repository_root) if repository_root is not None else None
    # this is the whole point: only the verifier mints the capability, and only after every
    # published label, action and metric has been re-derived from the frozen dataset
    verified = load_verified_published_l2g_v2_campaign(root, repository_root=repo)
    result = verified.result

    prefit = verify_v2_prefit_authority(repo)
    _require(
        result["prefit_authority_sha256"] == prefit == ACCEPTED_V2_PREFIT_AUTHORITY_SHA256,
        "the campaign cites a different pre-fit authority than the committed file",
    )
    _require(
        result["parent_campaign_freeze_identity"] == PARENT_V1_CAMPAIGN_FREEZE,
        "the campaign does not bind the campaign-v1 freeze as its parent",
    )
    authority = json.loads(
        ((repo or Path(".")) / "reports/layer2/l2g-v2-prefit-authority.json").read_bytes()
    )

    result_path = root / V2_OUTPUT_LAYOUT["campaign_result"]
    result_bytes = result_path.read_bytes()

    from minos_engine.models.relative_finalist_evidence import v2_campaign_result_identity

    safe_mean = float(result["safe_baseline_mean_regret"])
    safe_cvar = float(result["safe_baseline_cvar_regret"])

    per_spec: list[dict[str, Any]] = []
    for entry in sorted(result["per_spec"], key=lambda e: e["spec_hash"]):
        spec_hash = entry["spec_hash"]
        row: dict[str, Any] = {
            "spec_hash": spec_hash,
            "family": entry["family"],
            "implementation": entry["implementation"],
            "status": entry["status"],
            "training_failures": list(entry.get("training_failures") or ()),
        }
        if entry["status"] != STATUS_COMPLETE:
            for stem in (V2_OUTPUT_LAYOUT["oof_dir"], V2_OUTPUT_LAYOUT["metrics_dir"]):
                _require(
                    not (root / stem / f"{spec_hash}.json").exists(),
                    f"{spec_hash} failed but carries a scientific artifact",
                )
            _require(spec_hash not in result["shortlist"], f"{spec_hash} failed but shortlisted")
            per_spec.append(row)
            continue

        oof_path = root / V2_OUTPUT_LAYOUT["oof_dir"] / f"{spec_hash}.json"
        metric_path = root / V2_OUTPUT_LAYOUT["metrics_dir"] / f"{spec_hash}.json"
        oof_bytes = oof_path.read_bytes()
        metric_bytes = metric_path.read_bytes()
        oof = json.loads(oof_bytes)
        metric = json.loads(metric_bytes)
        _require(
            hashlib.sha256(oof_bytes).hexdigest() == entry["oof_file_sha256"]
            and hashlib.sha256(metric_bytes).hexdigest() == entry["metric_file_sha256"],
            f"{spec_hash}: an artifact does not hash to what the result records",
        )
        observed = {
            name: metric["metrics"][name] for name in _METRIC_NAMES if name in metric["metrics"]
        }
        _require(
            float(observed["mean_regret"]) == float(entry["promotion_metrics"]["mean_regret"])
            and float(observed["cvar_regret"]) == float(entry["promotion_metrics"]["cvar_regret"]),
            f"{spec_hash}: the metric artifact and the campaign result disagree",
        )
        mean = float(observed["mean_regret"])
        cvar = float(observed["cvar_regret"])
        row.update(
            observed_oof_record_count=entry["observed_oof_record_count"],
            observed_decision_count=entry["observed_decision_count"],
            observed_margin_count=entry["observed_margin_count"],
            unique_bam_count=entry["unique_bam_count"],
            duplicate_cell_count=entry["duplicate_cell_count"],
            exact_cell_set_verified=bool(entry["exact_cell_set_verified"]),
            outer_fold_count=len({d["outer_fold"] for d in oof["decisions"]}),
            margins=dict(sorted(oof["margins"].items())),
            metrics=observed,
            diagnostics={name: metric["diagnostics"][name] for name in _DIAGNOSTICS},
            switch_behaviour=_switch_behaviour(list(oof["decisions"])),
            promotion={
                "mean_regret": mean,
                "cvar_regret": cvar,
                "mean_no_worse_than_safe": mean <= safe_mean,
                "cvar_no_worse_than_safe": cvar <= safe_cvar,
                "strictly_improves_mean": mean < safe_mean,
                "strictly_improves_cvar": cvar < safe_cvar,
                "passes_three_part_rule": qualifies_against_bar(
                    mean_regret=mean,
                    cvar_regret=cvar,
                    bar_mean=safe_mean,
                    bar_cvar=safe_cvar,
                ),
                "shortlisted": spec_hash in result["shortlist"],
            },
            oof_scientific_hash=entry["oof_scientific_hash"],
            oof_file_sha256=entry["oof_file_sha256"],
            oof_size_bytes=len(oof_bytes),
            metric_scientific_hash=entry["metric_scientific_hash"],
            metric_file_sha256=entry["metric_file_sha256"],
            metric_size_bytes=len(metric_bytes),
        )
        per_spec.append(row)

    complete = [e["spec_hash"] for e in per_spec if e["status"] == STATUS_COMPLETE]
    failed = [e["spec_hash"] for e in per_spec if e["status"] != STATUS_COMPLETE]

    content = {
        "schema_version": V2_FREEZE_SCHEMA,
        "execution_source_commit": result["execution_source_commit"],
        "execution_source_tree": result["execution_source_tree"],
        "prefit_authority_schema": authority["schema_version"],
        "prefit_authority_sha256": prefit,
        "parent_campaign_freeze_identity": result["parent_campaign_freeze_identity"],
        "authorities": {
            "config_encoding_identity": result["config_encoding_identity"],
            "expected_bam_chromosome_set_hash": authority["expected_bam_chromosome_set_hash"],
            "expected_relative_cell_set_hash": authority["expected_relative_cell_set_hash"],
            "feature_matrix_hash": result["feature_matrix_hash"],
            "feature_set_hash": result["feature_set_hash"],
            "finalist_domain_hash": result["finalist_domain_hash"],
            "relative_contract_hash": result["relative_contract_hash"],
            "relative_dataset_identity": result["relative_dataset_identity"],
            "relative_protocol_hash": result["relative_protocol_hash"],
            "training_runtime_hash": result["training_runtime_hash"],
        },
        "campaign_result_identity": v2_campaign_result_identity(result),
        "campaign_result_file_sha256": hashlib.sha256(result_bytes).hexdigest(),
        "campaign_result_size_bytes": len(result_bytes),
        "candidate_spec_hashes": list(result["candidate_spec_hashes"]),
        "per_spec": per_spec,
        "safe_baseline_mean_regret": safe_mean,
        "safe_baseline_cvar_regret": safe_cvar,
        "reference_metrics": {
            name: {
                key: value
                for key, value in result["reference_metrics"][name].items()
                if key in _METRIC_NAMES
            }
            for name in _REFERENCES
        },
        "eligible_complete_spec_hashes": sorted(complete),
        "training_failure_spec_hashes": sorted(failed),
        "shortlist": list(result["shortlist"]),
        "shortlist_empty": bool(result["shortlist_empty"]),
        "campaign_outcome": OUTCOME_NO_CONTEXTUAL_SELECTOR,
        "research_disposition": RESEARCH_DISPOSITION_CLOSED,
        "production_fallback_config_hash": SAFE_BASELINE_CONFIG_HASH,
        "models_qualified_status": MODELS_QUALIFIED_STATUS_HOLD,
        "validation_authorized_for_v2": VALIDATION_AUTHORIZED_FOR_V2,
        "validation_read": bool(result["validation_read"]),
        "test_accessed": bool(result["test_accessed"]),
        "whole_tree_verify": True,
        "verified_published_capability": True,
        "thread_policy": "SINGLE_THREADED_DETERMINISTIC",
        "thread_report": [dict(sorted(p.items())) for p in result["thread_report"]],
    }
    verify_v2_campaign_freeze(content)
    return content


def verify_v2_campaign_freeze(content: dict[str, Any]) -> dict[str, Any]:
    """Check a v2 freeze against itself and against this source. Fails closed."""
    from minos_engine.models.relative_finalist_authority import (
        ACCEPTED_RELATIVE_CONTRACT_HASH,
        ACCEPTED_RELATIVE_DATASET_IDENTITY,
        ACCEPTED_V2_PREFIT_AUTHORITY_SHA256,
        PARENT_V1_CAMPAIGN_FREEZE,
    )
    from minos_engine.models.relative_finalist_contract import (
        SAFE_BASELINE_CONFIG_HASH,
        compute_finalist_domain_hash,
    )
    from minos_engine.models.relative_finalist_evidence import STATUS_COMPLETE
    from minos_engine.models.relative_finalist_protocol import (
        build_v2_spec_hashes,
        compute_relative_protocol_hash,
        qualifies_against_bar,
    )

    _require(
        content.get("schema_version") == V2_FREEZE_SCHEMA,
        f"unexpected v2 freeze schema {content.get('schema_version')!r}",
    )
    _require(
        content.get("prefit_authority_sha256") == ACCEPTED_V2_PREFIT_AUTHORITY_SHA256,
        "the freeze cites a foreign v2 pre-fit authority",
    )
    _require(
        content.get("prefit_authority_schema") == "l2g-v2-prefit-authority-v4",
        "the freeze cites a foreign v2 pre-fit authority schema",
    )
    _require(
        content.get("parent_campaign_freeze_identity") == PARENT_V1_CAMPAIGN_FREEZE,
        "the freeze does not bind the campaign-v1 freeze as its parent",
    )
    authorities = content["authorities"]
    for name, expected in (
        ("relative_protocol_hash", compute_relative_protocol_hash()),
        ("relative_contract_hash", ACCEPTED_RELATIVE_CONTRACT_HASH),
        ("relative_dataset_identity", ACCEPTED_RELATIVE_DATASET_IDENTITY),
        ("finalist_domain_hash", compute_finalist_domain_hash()),
    ):
        _require(
            authorities.get(name) == expected,
            f"the freeze's {name} is {authorities.get(name)!r}, expected {expected}",
        )
    _require(
        list(content["candidate_spec_hashes"])
        == list(build_v2_spec_hashes(ACCEPTED_RELATIVE_DATASET_IDENTITY)),
        "the freeze does not bind the accepted four v2 specs in their frozen order",
    )

    by_hash = {e["spec_hash"]: e for e in content["per_spec"]}
    _require(len(by_hash) == 4, "a v2 freeze must record all four frozen specs")
    _require(
        sorted(by_hash) == sorted(content["candidate_spec_hashes"]),
        "the freeze records specs the campaign did not run",
    )

    safe_mean = float(content["safe_baseline_mean_regret"])
    safe_cvar = float(content["safe_baseline_cvar_regret"])
    safe_reference = content["reference_metrics"]["ALWAYS_SAFE_BASELINE"]
    _require(
        float(safe_reference["mean_regret"]) == safe_mean
        and float(safe_reference["cvar_regret"]) == safe_cvar,
        "the frozen SAFE bar is not the SAFE reference's own metrics",
    )
    _require(
        float(safe_reference["switch_fraction"]) == 0.0,
        "the SAFE reference switched, which it cannot",
    )
    oracle = content["reference_metrics"]["ORACLE4"]
    _require(
        float(oracle["mean_regret"]) == 0.0 and float(oracle["cvar_regret"]) == 0.0,
        "ORACLE4 has non-zero regret, so it is not an oracle",
    )

    shortlisted: list[str] = []
    for spec_hash, entry in sorted(by_hash.items()):
        if entry["status"] != STATUS_COMPLETE:
            _require(
                bool(entry["training_failures"]),
                f"{spec_hash} is not COMPLETE but records no failure reason",
            )
            _require(
                "promotion" not in entry and "metrics" not in entry,
                f"{spec_hash} failed but carries promotion metrics",
            )
            _require(spec_hash not in content["shortlist"], f"{spec_hash} failed but shortlisted")
            continue
        _require(entry["outer_fold_count"] == 5, f"{spec_hash} did not run five outer folds")
        _require(entry["observed_oof_record_count"] == 150, f"{spec_hash} lacks 150 records")
        _require(entry["observed_decision_count"] == 50, f"{spec_hash} lacks 50 decisions")
        _require(entry["observed_margin_count"] == 5, f"{spec_hash} lacks 5 margins")
        _require(len(entry["margins"]) == 5, f"{spec_hash} lacks 5 recorded margins")
        _require(entry["unique_bam_count"] == 50, f"{spec_hash} lacks 50 BAMs")
        _require(entry["duplicate_cell_count"] == 0, f"{spec_hash} has duplicate cells")
        _require(bool(entry["exact_cell_set_verified"]), f"{spec_hash} lacks a cell-set proof")
        for name in _DIAGNOSTICS:
            _require(name in entry["diagnostics"], f"{spec_hash} is missing diagnostic {name}")
        for field in (
            "oof_scientific_hash",
            "oof_file_sha256",
            "metric_scientific_hash",
            "metric_file_sha256",
        ):
            _require(bool(entry.get(field)), f"{spec_hash} is missing {field}")

        promotion = entry["promotion"]
        mean = float(promotion["mean_regret"])
        cvar = float(promotion["cvar_regret"])
        _require(
            mean == float(entry["metrics"]["mean_regret"])
            and cvar == float(entry["metrics"]["cvar_regret"]),
            f"{spec_hash}: the promotion metrics are not its own recorded metrics",
        )
        # the three-part rule, re-run over the frozen numbers
        verdicts: tuple[tuple[str, bool], ...] = (
            ("mean_no_worse_than_safe", mean <= safe_mean),
            ("cvar_no_worse_than_safe", cvar <= safe_cvar),
            ("strictly_improves_mean", mean < safe_mean),
            ("strictly_improves_cvar", cvar < safe_cvar),
            (
                "passes_three_part_rule",
                qualifies_against_bar(
                    mean_regret=mean, cvar_regret=cvar, bar_mean=safe_mean, bar_cvar=safe_cvar
                ),
            ),
        )
        for verdict, truth in verdicts:
            _require(
                bool(promotion[verdict]) is truth,
                f"{spec_hash}: recorded {verdict} disagrees with its own metrics",
            )
        _require(
            bool(promotion["shortlisted"]) == (spec_hash in content["shortlist"]),
            f"{spec_hash}: the shortlisted flag disagrees with the shortlist",
        )
        if promotion["passes_three_part_rule"]:
            shortlisted.append(spec_hash)

        behaviour = entry["switch_behaviour"]
        _require(
            behaviour["kept_safe_count"] + behaviour["switch_count"] == 50,
            f"{spec_hash}: the switch accounting does not cover 50 BAMs",
        )
        _require(
            behaviour["helpful_switch_count"] + behaviour["harmful_switch_count"]
            <= behaviour["switch_count"],
            f"{spec_hash}: more helpful and harmful switches than switches",
        )

    _require(
        sorted(content["shortlist"]) == sorted(shortlisted),
        f"the frozen shortlist is not what the three-part rule gives: {sorted(shortlisted)}",
    )
    _require(
        bool(content["shortlist_empty"]) == (not content["shortlist"]),
        "shortlist_empty disagrees with the shortlist",
    )
    _require(
        sorted(content["eligible_complete_spec_hashes"])
        == sorted(h for h, e in by_hash.items() if e["status"] == STATUS_COMPLETE),
        "the eligible COMPLETE specs are not what the per-spec statuses give",
    )
    _require(
        sorted(content["training_failure_spec_hashes"])
        == sorted(h for h, e in by_hash.items() if e["status"] != STATUS_COMPLETE),
        "the recorded training failures are not what the per-spec statuses give",
    )

    if not content["shortlist"]:
        _require(
            content["campaign_outcome"] == OUTCOME_NO_CONTEXTUAL_SELECTOR
            and content["research_disposition"] == RESEARCH_DISPOSITION_CLOSED
            and content["models_qualified_status"] == MODELS_QUALIFIED_STATUS_HOLD
            and content["validation_authorized_for_v2"] is False,
            "an empty shortlist must freeze the closed outcome and withhold VALIDATION",
        )
    _require(
        content["production_fallback_config_hash"] == SAFE_BASELINE_CONFIG_HASH,
        "the production fallback is not the frozen safe baseline",
    )
    _require(
        content["validation_read"] is False and content["test_accessed"] is False,
        "the freeze records a VALIDATION read or a TEST access",
    )
    _require(
        bool(content["whole_tree_verify"]) and bool(content["verified_published_capability"]),
        "the freeze does not record both offline proofs",
    )
    pools = list(content["thread_report"])
    _require(bool(pools), "the freeze records no thread evidence")
    for pool in pools:
        _require(
            isinstance(pool.get("num_threads"), int)
            and not isinstance(pool.get("num_threads"), bool)
            and pool["num_threads"] == 1,
            "a scientific thread pool did not run single-threaded",
        )
    return {
        "ok": True,
        "schema_version": V2_FREEZE_SCHEMA,
        "freeze_identity": v2_campaign_freeze_identity(content),
        "complete_spec_count": len(content["eligible_complete_spec_hashes"]),
        "shortlist_size": len(content["shortlist"]),
    }
