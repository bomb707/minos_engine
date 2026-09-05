"""The ALREADY-FROZEN TRAIN shortlist rule, and the shape of the campaign result it produces.

The rule is not invented here and must not be adjusted after seeing numbers: a promotable spec
enters the shortlist iff its OOF mean regret AND its OOF CVaR-0.25 regret are both no worse than
the best reference's. If nothing clears both bars the shortlist is EMPTY, MODELS-QUALIFIED will
hold, and SAFE_BASELINE remains the fallback — promoting the least-bad contextual model would be
choosing a threshold after the fact.
"""

from __future__ import annotations

from typing import Any, Final

from minos_engine.common.canonical_json import canonical_json_bytes
from minos_engine.common.errors import MinosEngineError
from minos_engine.common.hashing import sha256_hex

__all__ = [
    "SHORTLIST_RESULT_SCHEMA",
    "ShortlistError",
    "derive_train_shortlist",
    "train_campaign_result_content",
]

SHORTLIST_RESULT_SCHEMA: Final = "l2g-train-oof-campaign-result-v1"
SHORTLIST_RESULT_DOMAIN: Final = "minos:l2g-train-oof-campaign-result:v1\n"


class ShortlistError(MinosEngineError):
    """The shortlist could not be derived under the frozen rule."""


def derive_train_shortlist(
    *,
    reference_metrics: dict[str, dict[str, float]],
    candidate_metrics: dict[str, dict[str, float]],
) -> dict[str, Any]:
    """Apply the frozen two-bar rule. Both metrics are lower-is-better regret."""
    if not reference_metrics:
        raise ShortlistError("no reference metrics; the promotion bar is undefined")
    for name, metrics in {**reference_metrics, **candidate_metrics}.items():
        missing = {"mean_regret", "cvar_regret"} - set(metrics)
        if missing:
            raise ShortlistError(f"{name} is missing {sorted(missing)}")

    best_mean = min(m["mean_regret"] for m in reference_metrics.values())
    best_cvar = min(m["cvar_regret"] for m in reference_metrics.values())

    shortlist = sorted(
        name
        for name, metrics in candidate_metrics.items()
        if metrics["mean_regret"] <= best_mean and metrics["cvar_regret"] <= best_cvar
    )
    return {
        "best_reference_mean_regret": best_mean,
        "best_reference_cvar_regret": best_cvar,
        "rule": (
            "mean_regret <= best_reference_mean_regret AND "
            "cvar_regret <= best_reference_cvar_regret"
        ),
        "shortlist": shortlist,
        "shortlist_empty": not shortlist,
        "fallback_if_empty": "SAFE_BASELINE_REMAINS_AND_MODELS_QUALIFIED_HOLDS",
    }


def train_campaign_result_content(
    *,
    source_commit: str,
    source_tree: str,
    prefit_authority_sha256: str,
    training_dataset_hash: str,
    cv_manifest_hash: str,
    training_runtime_hash: str,
    candidate_spec_hashes: tuple[str, ...],
    reference_spec_hashes: tuple[str, ...],
    oof_artifact_hashes: dict[str, str],
    metric_artifact_hashes: dict[str, str],
    training_failures: tuple[dict[str, Any], ...],
    shortlist: dict[str, Any],
    thread_report: tuple[dict[str, Any], ...],
) -> dict[str, Any]:
    """The SHAPE of the future TRAIN OOF result. No real result is produced in this task."""
    if len(candidate_spec_hashes) + len(reference_spec_hashes) != 10:
        raise ShortlistError("a campaign result must bind all ten model-spec hashes")
    return {
        "schema_version": SHORTLIST_RESULT_SCHEMA,
        "source_commit": source_commit,
        "source_tree": source_tree,
        "prefit_authority_sha256": prefit_authority_sha256,
        "training_dataset_hash": training_dataset_hash,
        "cv_manifest_hash": cv_manifest_hash,
        "training_runtime_hash": training_runtime_hash,
        "candidate_spec_hashes": list(candidate_spec_hashes),
        "reference_spec_hashes": list(reference_spec_hashes),
        "oof_artifact_hashes": dict(sorted(oof_artifact_hashes.items())),
        "metric_artifact_hashes": dict(sorted(metric_artifact_hashes.items())),
        "training_failures": list(training_failures),
        "best_reference_mean_regret": shortlist["best_reference_mean_regret"],
        "best_reference_cvar_regret": shortlist["best_reference_cvar_regret"],
        "shortlist": list(shortlist["shortlist"]),
        "shortlist_empty": bool(shortlist["shortlist_empty"]),
        "thread_report": list(thread_report),
        "validation_read": False,
        "test_accessed": False,
    }


def train_campaign_result_identity(content: dict[str, Any]) -> str:
    return sha256_hex(SHORTLIST_RESULT_DOMAIN.encode("utf-8") + canonical_json_bytes(content))
