"""``l2g-train-oof-campaign-freeze-v1`` — the first real TRAIN OOF campaign, frozen into Git.

The published campaign tree lives outside the repository, so nothing in Git would otherwise pin
what the first real model campaign actually produced. This freeze binds its identities: the
execution source, the campaign result, every artifact hash, the exact reference bars and the
exact per-candidate promotion metrics.

Every value is derived from the VERIFIED published tree, never from a caller: the builder runs the
whole-tree verifier first and reads the metric artifacts back from disk. A freeze assembled from
someone's summary would be a claim about a campaign rather than a record of one.

The frozen outcome of campaign v1 is an EMPTY shortlist. That is not a training failure — all ten
specs completed, every fold succeeded, and every one of the 1040 cells was predicted exactly once.
It is the promotion hypothesis failing under the frozen TRAIN criterion, which is a different
statement and is recorded as such.
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
    "CAMPAIGN_FREEZE_DOMAIN",
    "CAMPAIGN_FREEZE_PATH",
    "CAMPAIGN_FREEZE_SCHEMA",
    "CampaignFreezeError",
    "build_campaign_freeze",
    "campaign_freeze_identity",
    "verify_campaign_freeze",
]

CAMPAIGN_FREEZE_SCHEMA: Final = "l2g-train-oof-campaign-freeze-v1"
CAMPAIGN_FREEZE_DOMAIN: Final = "minos:l2g-train-oof-campaign-freeze:v1\n"
CAMPAIGN_FREEZE_PATH: Final = "reports/layer2/l2g-train-oof-campaign-freeze-v1.json"

#: campaign v1's frozen outcome. Deliberately NOT "MODEL_TRAINING_FAILED": training succeeded
#: completely and the promotion hypothesis is what failed.
OUTCOME_NO_CONTEXTUAL_MODEL: Final = "NO_CONTEXTUAL_MODEL_QUALIFIED_ON_TRAIN"

#: With an empty shortlist there is no frozen contextual candidate for VALIDATION to choose
#: among, so opening VALIDATION could only serve to rescue a model the TRAIN criterion rejected.
VALIDATION_AUTHORIZED_FOR_CAMPAIGN_V1: Final = False

MODELS_QUALIFIED_STATUS_HOLD: Final = "HOLD_NO_TRAIN_PROMOTABLE_MODEL"

_PROMOTION_METRICS: Final = ("mean_regret", "cvar_regret")


class CampaignFreezeError(MinosEngineError):
    """The campaign freeze could not be built or does not verify."""


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise CampaignFreezeError(message)


def campaign_freeze_identity(content: dict[str, Any]) -> str:
    """Domain-separated identity of a campaign freeze."""
    return sha256_hex(CAMPAIGN_FREEZE_DOMAIN.encode("utf-8") + canonical_json_bytes(content))


def build_campaign_freeze(
    *, campaign_root: Path, repository_root: Path | None = None
) -> dict[str, Any]:
    """Derive the freeze from the VERIFIED published campaign tree.

    The whole-tree verifier runs first, so a freeze cannot be built over evidence that does not
    itself verify; the promotion metrics are then read back from the metric artifacts rather than
    taken from the campaign result's summary of them.
    """
    from minos_engine.models.campaign_evidence import (
        OUTPUT_LAYOUT,
        verify_published_l2g_train_campaign,
    )
    from minos_engine.models.shortlist import (
        ACCEPTED_AUTHORITIES,
        verify_prefit_authority_bytes,
    )

    root = Path(campaign_root)
    repo = Path(repository_root) if repository_root is not None else None
    tree_report = verify_published_l2g_train_campaign(root, repository_root=repo)
    _require(bool(tree_report["ok"]), "the published campaign tree does not verify")

    result_path = root / OUTPUT_LAYOUT["campaign_result"]
    result_bytes = result_path.read_bytes()
    result = json.loads(result_bytes)

    from minos_engine.models.shortlist import campaign_result_identity

    prefit = verify_prefit_authority_bytes(repo)
    _require(
        result["prefit_authority_sha256"] == prefit,
        "the campaign result cites a different pre-fit authority than the committed file",
    )

    by_hash = {e["spec_hash"]: e for e in result["per_spec"]}
    candidates = list(result["candidate_spec_hashes"])
    references = list(result["reference_spec_hashes"])

    per_spec: list[dict[str, Any]] = []
    metrics: dict[str, dict[str, float]] = {}
    for spec_hash in candidates + references:
        entry = by_hash[spec_hash]
        _require(
            entry["status"] == "COMPLETE",
            f"{spec_hash} is {entry['status']}; campaign v1 completed all ten specs",
        )
        artifact = json.loads(
            (root / OUTPUT_LAYOUT["metrics_dir"] / f"{spec_hash}.json").read_bytes()
        )
        observed = {name: float(artifact["metrics"][name]) for name in _PROMOTION_METRICS}
        _require(
            observed == {k: float(v) for k, v in entry["promotion_metrics"].items()},
            f"{spec_hash}: the metric artifact and the campaign result disagree",
        )
        metrics[spec_hash] = observed
        per_spec.append(
            {
                "spec_hash": spec_hash,
                "family": entry["family"],
                "role": entry["role"],
                "status": entry["status"],
                "successful_outer_fold_count": entry["successful_outer_fold_count"],
                "observed_oof_record_count": entry["observed_oof_record_count"],
                "unique_bam_count": entry["unique_bam_count"],
                "duplicate_cell_count": entry["duplicate_cell_count"],
                "exact_cell_set_verified": entry["exact_cell_set_verified"],
                "oof_scientific_hash": entry["oof_scientific_hash"],
                "oof_file_sha256": entry["oof_file_sha256"],
                "oof_size_bytes": entry["oof_size_bytes"],
                "metric_scientific_hash": entry["metric_scientific_hash"],
                "metric_file_sha256": entry["metric_file_sha256"],
                "metric_size_bytes": entry["metric_size_bytes"],
                "promotion_metrics": observed,
            }
        )

    best_mean = min(metrics[h]["mean_regret"] for h in references)
    best_cvar = min(metrics[h]["cvar_regret"] for h in references)
    _require(
        result["best_reference_mean_regret"] == best_mean
        and result["best_reference_cvar_regret"] == best_cvar,
        "the recorded reference bars are not what the verified metric artifacts give",
    )

    candidate_rows = [
        {
            "spec_hash": h,
            "family": by_hash[h]["family"],
            "mean_regret": metrics[h]["mean_regret"],
            "cvar_regret": metrics[h]["cvar_regret"],
            "mean_bar_pass": metrics[h]["mean_regret"] <= best_mean,
            "cvar_bar_pass": metrics[h]["cvar_regret"] <= best_cvar,
            "shortlisted": h in result["shortlist"],
        }
        for h in candidates
    ]

    content = {
        "schema_version": CAMPAIGN_FREEZE_SCHEMA,
        "execution_source_commit": result["source_commit"],
        "execution_source_tree": result["source_tree"],
        "prefit_authority_sha256": prefit,
        "campaign_result_identity": campaign_result_identity(result),
        "campaign_result_file_sha256": hashlib.sha256(result_bytes).hexdigest(),
        "campaign_result_size_bytes": len(result_bytes),
        "authorities": {name: result[name] for name in sorted(ACCEPTED_AUTHORITIES)},
        "candidate_spec_hashes": candidates,
        "reference_spec_hashes": references,
        "per_spec": sorted(per_spec, key=lambda e: e["spec_hash"]),
        "best_reference_mean_regret": best_mean,
        "best_reference_cvar_regret": best_cvar,
        "best_reference_mean_achieved_by": sorted(
            by_hash[h]["family"] for h in references if metrics[h]["mean_regret"] == best_mean
        ),
        "best_reference_cvar_achieved_by": sorted(
            by_hash[h]["family"] for h in references if metrics[h]["cvar_regret"] == best_cvar
        ),
        "candidate_bar_evaluation": candidate_rows,
        "eligible_candidate_hashes": list(result["eligible_candidate_hashes"]),
        "ineligible_candidate_hashes": list(result["ineligible_candidate_hashes"]),
        "shortlist": list(result["shortlist"]),
        "shortlist_empty": bool(result["shortlist_empty"]),
        "fallback_if_empty": result["fallback_if_empty"],
        "campaign_outcome": OUTCOME_NO_CONTEXTUAL_MODEL,
        "models_qualified_status": MODELS_QUALIFIED_STATUS_HOLD,
        "validation_authorized_for_campaign_v1": VALIDATION_AUTHORIZED_FOR_CAMPAIGN_V1,
        "validation_read": False,
        "test_accessed": False,
        "whole_tree_verify": True,
        "thread_policy": result["thread_policy"],
        "thread_report": list(result["thread_report"]),
    }
    verify_campaign_freeze(content)
    return content


def verify_campaign_freeze(content: dict[str, Any]) -> dict[str, Any]:
    """Check a freeze against itself. Fails closed."""
    from minos_engine.models.campaign import (
        ACCEPTED_CANDIDATE_SPEC_HASHES,
        ACCEPTED_REFERENCE_SPECS,
    )
    from minos_engine.models.shortlist import (
        ACCEPTED_AUTHORITIES,
        ACCEPTED_PREFIT_AUTHORITY_SHA256,
    )

    _require(
        content.get("schema_version") == CAMPAIGN_FREEZE_SCHEMA,
        f"unexpected freeze schema {content.get('schema_version')!r}",
    )
    _require(
        content.get("prefit_authority_sha256") == ACCEPTED_PREFIT_AUTHORITY_SHA256,
        "the freeze cites a foreign pre-fit authority",
    )
    for name, expected in ACCEPTED_AUTHORITIES.items():
        _require(
            content["authorities"].get(name) == expected,
            f"{name} in the freeze is not the accepted authority",
        )
    _require(
        tuple(content["candidate_spec_hashes"]) == ACCEPTED_CANDIDATE_SPEC_HASHES,
        "the freeze does not bind the accepted six candidates",
    )
    _require(
        tuple(content["reference_spec_hashes"]) == tuple(h for _, h in ACCEPTED_REFERENCE_SPECS),
        "the freeze does not bind the accepted four references",
    )

    by_hash = {e["spec_hash"]: e for e in content["per_spec"]}
    _require(len(by_hash) == 10, "a freeze must record all ten specs")
    for spec_hash, entry in by_hash.items():
        _require(entry["status"] == "COMPLETE", f"{spec_hash} is not COMPLETE")
        _require(entry["successful_outer_fold_count"] == 5, f"{spec_hash} did not run five folds")
        _require(entry["observed_oof_record_count"] == 1040, f"{spec_hash} lacks 1040 records")
        _require(entry["unique_bam_count"] == 50, f"{spec_hash} lacks 50 BAMs")
        _require(entry["duplicate_cell_count"] == 0, f"{spec_hash} has duplicate cells")
        _require(bool(entry["exact_cell_set_verified"]), f"{spec_hash} lacks a cell-set proof")
        for field in (
            "oof_scientific_hash",
            "oof_file_sha256",
            "metric_scientific_hash",
            "metric_file_sha256",
        ):
            _require(bool(entry.get(field)), f"{spec_hash} is missing {field}")

    references = list(content["reference_spec_hashes"])
    best_mean = min(by_hash[h]["promotion_metrics"]["mean_regret"] for h in references)
    best_cvar = min(by_hash[h]["promotion_metrics"]["cvar_regret"] for h in references)
    _require(
        content["best_reference_mean_regret"] == best_mean,
        "the frozen mean bar is not the minimum over the frozen references",
    )
    _require(
        content["best_reference_cvar_regret"] == best_cvar,
        "the frozen CVaR bar is not the minimum over the frozen references",
    )

    # the two-bar rule, re-run over the frozen numbers
    shortlisted = sorted(
        row["spec_hash"]
        for row in content["candidate_bar_evaluation"]
        if row["mean_regret"] <= best_mean and row["cvar_regret"] <= best_cvar
    )
    _require(
        sorted(content["shortlist"]) == shortlisted,
        f"the frozen shortlist is not what the two-bar rule gives: {shortlisted}",
    )
    for row in content["candidate_bar_evaluation"]:
        _require(
            row["mean_bar_pass"] == (row["mean_regret"] <= best_mean)
            and row["cvar_bar_pass"] == (row["cvar_regret"] <= best_cvar),
            f"{row['spec_hash']}: a recorded bar verdict disagrees with its own metrics",
        )
        _require(
            row["shortlisted"] == (row["spec_hash"] in content["shortlist"]),
            f"{row['spec_hash']}: shortlisted flag disagrees with the shortlist",
        )
    _require(
        bool(content["shortlist_empty"]) == (not content["shortlist"]),
        "shortlist_empty disagrees with the shortlist",
    )
    if not content["shortlist"]:
        _require(
            content["campaign_outcome"] == OUTCOME_NO_CONTEXTUAL_MODEL,
            "an empty shortlist must be recorded as the promotion hypothesis failing",
        )
        _require(
            content["models_qualified_status"] == MODELS_QUALIFIED_STATUS_HOLD,
            "an empty shortlist cannot accompany a qualified status",
        )
        _require(
            content["validation_authorized_for_campaign_v1"] is False,
            "with no shortlisted candidate there is nothing for VALIDATION to select among",
        )
    _require(content["validation_read"] is False, "the freeze records a VALIDATION read")
    _require(content["test_accessed"] is False, "the freeze records a TEST access")
    _require(bool(content["whole_tree_verify"]), "the freeze records a failed tree verification")
    for pool in content["thread_report"]:
        _require(int(pool["num_threads"]) == 1, "a recorded pool ran multi-threaded")
    return {
        "ok": True,
        "spec_count": len(by_hash),
        "shortlist_size": len(content["shortlist"]),
        "freeze_identity": campaign_freeze_identity(content),
    }
