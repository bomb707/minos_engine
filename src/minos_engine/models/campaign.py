"""THE production TRAIN OOF campaign boundary. One call; the caller wires nothing together.

An operator assembling ``run_outer_oof``, the metrics and the shortlist by hand can — without
meaning to — compare a four-fold candidate against five-fold references, drop a failed reference
and take the best of the rest, or metricise a spec that never covered every cell. Each of those
changes the shortlist while every individual component still behaves correctly.

So completeness is a first-class property here. A spec is COMPLETE only when all five outer folds
succeeded AND it produced exactly one prediction for every one of the 1040 scientific cells across
all 50 BAMs, with no duplicate and none missing. Anything less is recorded as campaign evidence
and is INELIGIBLE: it is never metricised for promotion, and a failed REFERENCE means the
promotion bar was never fully observed, which is a HOLD rather than a smaller reference set.

This module runs no real campaign by itself; it is the boundary the future real run must use.
"""

from __future__ import annotations

from typing import Any, Final

from minos_engine.common.errors import MinosEngineError
from minos_engine.models.contract import SAFE_BASELINE_CONFIG_HASH
from minos_engine.models.oof_metrics import (
    ReferenceSelectionUnavailable,
    admission_metrics,
    bam_selection_regret,
    score_metrics,
    summarise_oof,
)
from minos_engine.models.oof_runner import oof_artifact_identity, run_outer_oof
from minos_engine.models.prefit_loader import ACCEPTED_TRAINING_DATASET_HASH
from minos_engine.models.spec import REFERENCE_FAMILIES

__all__ = [
    "EXPECTED_BAM_COUNT",
    "EXPECTED_OOF_RECORDS_PER_SPEC",
    "EXPECTED_OUTER_FOLDS",
    "STATUS_COMPLETE",
    "STATUS_TRAINING_FAILURE",
    "CampaignError",
    "ReferenceThresholdUnavailable",
    "assess_completeness",
    "run_l2g_train_oof_campaign",
]

EXPECTED_OUTER_FOLDS: Final = 5
EXPECTED_OOF_RECORDS_PER_SPEC: Final = 1040
EXPECTED_BAM_COUNT: Final = 50

STATUS_COMPLETE: Final = "COMPLETE"
STATUS_TRAINING_FAILURE: Final = "TRAINING_FAILURE"


class CampaignError(MinosEngineError):
    """The TRAIN OOF campaign cannot be run or closed honestly."""


class ReferenceThresholdUnavailable(CampaignError):
    """A required reference did not complete, so the promotion bar was never fully observed."""


def assess_completeness(
    *,
    records: list[Any],
    failures: list[dict[str, Any]],
    expected_cells: int = EXPECTED_OOF_RECORDS_PER_SPEC,
    expected_bams: int = EXPECTED_BAM_COUNT,
) -> dict[str, Any]:
    """COMPLETE means every cell predicted exactly once across all five folds. Nothing less."""
    seen: dict[tuple[str, str], int] = {}
    for record in records:
        key = (record.dataset_id, record.config_hash)
        seen[key] = seen.get(key, 0) + 1
    duplicates = sorted(k for k, n in seen.items() if n > 1)
    bams = {r.dataset_id for r in records}
    folds = {r.outer_fold for r in records}

    reasons: list[str] = []
    if failures:
        reasons.append(f"{len(failures)} fold(s) failed to train")
    if len(folds) != EXPECTED_OUTER_FOLDS:
        reasons.append(f"{len(folds)} of {EXPECTED_OUTER_FOLDS} outer folds produced predictions")
    if len(records) != expected_cells:
        reasons.append(f"{len(records)} of {expected_cells} scientific cells predicted")
    if duplicates:
        reasons.append(f"{len(duplicates)} cell(s) predicted more than once")
    if len(bams) != expected_bams:
        reasons.append(f"{len(bams)} of {expected_bams} BAMs represented")

    return {
        "status": STATUS_COMPLETE if not reasons else STATUS_TRAINING_FAILURE,
        "expected_outer_fold_count": EXPECTED_OUTER_FOLDS,
        "successful_outer_fold_count": len(folds),
        "failed_folds": sorted(f["fold"] for f in failures),
        "expected_oof_record_count": expected_cells,
        "observed_oof_record_count": len(records),
        "unique_bam_count": len(bams),
        "duplicate_cell_count": len(duplicates),
        "reasons": reasons,
    }


def _metricise(records: list[Any], *, family: str, expected_bams: int) -> dict[str, Any]:
    regret = bam_selection_regret(
        records, family=family, safe_baseline_config=SAFE_BASELINE_CONFIG_HASH
    )
    if len(regret["per_bam_regret"]) != expected_bams:
        raise CampaignError(
            f"{family} produced regret for {len(regret['per_bam_regret'])} BAMs, expected "
            f"{expected_bams}"
        )
    summary = summarise_oof(regret)
    utilities = list(regret["selected_actual_utility"].values())
    metrics: dict[str, Any] = {
        **summary,
        "selected_actual_utility_mean": sum(utilities) / len(utilities),
        "catastrophic_regression_count": regret["catastrophic_regression_count"],
        "selection_policy": regret["selection_policy"],
    }
    # diagnostics are secondary: a model that cannot produce them is not thereby disqualified
    try:
        metrics.update(score_metrics(records))
    except Exception as exc:  # noqa: BLE001 - recorded, never fatal
        metrics["score_diagnostics_unavailable"] = str(exc)
    try:
        metrics.update(admission_metrics(records))
    except Exception as exc:  # noqa: BLE001
        metrics["admission_diagnostics_unavailable"] = str(exc)
    return metrics


def run_l2g_train_oof_campaign(
    *,
    dataset: Any,
    design: Any,
    candidate_specs: tuple[Any, ...],
    reference_specs: tuple[Any, ...],
    fit_estimators: Any,
    fit_reference: Any,
    thread_report: tuple[dict[str, Any], ...] = (),
) -> dict[str, Any]:
    """Run all ten frozen specs, prove completeness, and derive the shortlist — or HOLD.

    The caller supplies the frozen specs and the fitting callables; it does not choose a spec
    subset, a fold subset, a metric, a threshold, an exclusion or a shortlist.
    """
    if len(candidate_specs) != 6:
        raise CampaignError(f"{len(candidate_specs)} candidate specs, expected the frozen 6")
    if len(reference_specs) != 4:
        raise CampaignError(f"{len(reference_specs)} reference specs, expected the frozen 4")
    if {s.family for s in reference_specs} != set(REFERENCE_FAMILIES):
        raise CampaignError("the reference set is not the frozen four families")

    # Derived from the dataset, never from a module constant a caller could patch. For the
    # accepted frozen dataset these ARE 1040 and 50, and that is asserted rather than assumed.
    expected_cells = len(dataset.rows)
    expected_bams = len(dataset.cv_manifest.bam_chromosome)
    if getattr(dataset, "identity", None) is not None:
        try:
            is_accepted = dataset.identity() == ACCEPTED_TRAINING_DATASET_HASH
        except Exception:  # noqa: BLE001 - a synthetic stand-in has no accepted identity
            is_accepted = False
        if is_accepted and (
            expected_cells != EXPECTED_OOF_RECORDS_PER_SPEC or expected_bams != EXPECTED_BAM_COUNT
        ):
            raise CampaignError(
                f"the accepted dataset must carry {EXPECTED_OOF_RECORDS_PER_SPEC} cells over "
                f"{EXPECTED_BAM_COUNT} BAMs, found {expected_cells}/{expected_bams}"
            )

    chromosome_of = dict(dataset.cv_manifest.bam_chromosome)
    weights = dataset.admission_weights()
    score_weights = dataset.score_weights()
    rows = list(dataset.rows)

    per_spec: dict[str, dict[str, Any]] = {}
    complete_metrics: dict[str, dict[str, Any]] = {}

    for spec, is_reference in [(s, False) for s in candidate_specs] + [
        (s, True) for s in reference_specs
    ]:
        runner = fit_reference if is_reference else fit_estimators
        try:
            records, failures = run_outer_oof(
                spec=spec,
                rows=rows,
                design=design,
                chromosome_of=chromosome_of,
                weights=weights,
                score_weights=score_weights,
                fit_estimators=runner,
            )
        except ReferenceSelectionUnavailable as exc:
            records, failures = (
                [],
                [{"fold": "ALL", "reason": str(exc), "class": STATUS_TRAINING_FAILURE}],
            )
        completeness = assess_completeness(
            records=records,
            failures=failures,
            expected_cells=expected_cells,
            expected_bams=expected_bams,
        )
        entry: dict[str, Any] = {
            "spec_hash": spec.identity(),
            "family": spec.family,
            "role": "REFERENCE" if is_reference else "CANDIDATE",
            "training_failures": failures,
            **completeness,
        }
        if completeness["status"] == STATUS_COMPLETE:
            try:
                metrics = _metricise(records, family=spec.family, expected_bams=expected_bams)
            except ReferenceSelectionUnavailable as exc:
                entry["status"] = STATUS_TRAINING_FAILURE
                entry["reasons"] = [*entry["reasons"], str(exc)]
            else:
                entry["oof_artifact_hash"] = oof_artifact_identity(records)
                entry["metrics"] = metrics
                complete_metrics[spec.identity()] = metrics
        per_spec[spec.identity()] = entry

    reference_entries = {s.identity(): per_spec[s.identity()] for s in reference_specs}
    incomplete_references = sorted(
        f"{e['family']}({', '.join(e['reasons'])})"
        for e in reference_entries.values()
        if e["status"] != STATUS_COMPLETE
    )
    all_references_complete = not incomplete_references

    result: dict[str, Any] = {
        "per_spec": per_spec,
        "all_required_references_complete": all_references_complete,
        "reference_threshold_available": all_references_complete,
        "expected_oof_record_count": expected_cells,
        "expected_bam_count": expected_bams,
        "thread_report": list(thread_report),
        "validation_read": False,
        "test_accessed": False,
    }

    if not all_references_complete:
        # Dropping the failed reference and taking the best of the rest would silently LOWER the
        # bar, which is the one direction a promotion threshold must never move by accident.
        result["hold_reason"] = (
            "a required reference did not complete, so the promotion bar was never fully "
            f"observed: {incomplete_references}"
        )
        result["shortlist"] = []
        result["shortlist_empty"] = True
        result["eligible_candidates"] = []
        result["ineligible_candidates"] = sorted(
            s.identity()
            for s in candidate_specs
            if per_spec[s.identity()]["status"] != STATUS_COMPLETE
        )
        raise ReferenceThresholdUnavailable(result["hold_reason"])

    from minos_engine.models.shortlist import derive_verified_train_shortlist

    eligible = {
        s.identity(): complete_metrics[s.identity()]
        for s in candidate_specs
        if per_spec[s.identity()]["status"] == STATUS_COMPLETE
    }
    ineligible = sorted(
        s.identity() for s in candidate_specs if per_spec[s.identity()]["status"] != STATUS_COMPLETE
    )
    shortlist = derive_verified_train_shortlist(
        reference_metrics={s.identity(): complete_metrics[s.identity()] for s in reference_specs},
        candidate_metrics=eligible,
        reference_spec_hashes=tuple(s.identity() for s in reference_specs),
        candidate_spec_hashes=tuple(s.identity() for s in candidate_specs),
        ineligible_candidate_hashes=tuple(ineligible),
    )
    result.update(shortlist)
    result["eligible_candidates"] = sorted(eligible)
    result["ineligible_candidates"] = ineligible
    return result
