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
from minos_engine.models.oof_runner import (
    metric_artifact_identity,
    oof_artifact_identity,
    run_outer_oof,
)
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
    "TrustedL2GTrainCampaign",
    "assess_completeness",
    "run_real_l2g_train_oof_campaign",
]

EXPECTED_OUTER_FOLDS: Final = 5
EXPECTED_OOF_RECORDS_PER_SPEC: Final = 1040
EXPECTED_BAM_COUNT: Final = 50

STATUS_COMPLETE: Final = "COMPLETE"
STATUS_TRAINING_FAILURE: Final = "TRAINING_FAILURE"


#: Nobody outside this module holds it, so nobody outside this module can mint a trusted
#: campaign. A dict is evidence of nothing: it is whatever its author typed.
_MINT_TOKEN: Final = object()


class _FrozenRecord:
    """An OOF record snapshotted by value, so retained evidence cannot be edited in place."""

    __slots__ = ("_content",)

    def __init__(self, content: dict[str, Any]) -> None:
        object.__setattr__(self, "_content", dict(content))

    def content(self) -> dict[str, Any]:
        return dict(self._content)

    def __getattr__(self, name: str) -> Any:
        try:
            return self._content[name]
        except KeyError:
            raise AttributeError(name) from None


class TrustedL2GTrainCampaign:
    """A campaign that ACTUALLY ran under the sealed authority, with its evidence retained.

    The OOF records are held here rather than in the small canonical result: 10 x 1040 records do
    not belong in a summary document, but discarding them after hashing would leave an
    ``oof_artifact_hash`` with nothing behind it -- a claim about evidence that no longer exists.

    Construction requires a private token, so a caller-built dictionary can never become
    publishable no matter how well-formed it looks.
    """

    __slots__ = (
        "_closure",
        "_failures",
        "_metrics",
        "_records",
        "execution_source_commit",
        "execution_source_tree",
    )

    #: the checkout that ACTUALLY fitted the models, captured before the first fit
    execution_source_commit: str
    execution_source_tree: str

    def __init__(
        self,
        token: object,
        *,
        closure: dict[str, Any],
        records: dict[str, list[Any]],
        metrics: dict[str, dict[str, Any]],
        failures: dict[str, list[dict[str, Any]]],
        execution_source_commit: str = "",
        execution_source_tree: str = "",
    ) -> None:
        import copy as _copy

        if token is not _MINT_TOKEN:
            raise CampaignError(
                "a trusted campaign may only be minted by the sealed production entry; a "
                "caller-built object is not evidence that a campaign ran"
            )
        # Snapshotted BY VALUE at mint. A caller who later mutates what it passed in -- or a
        # record handed back by an accessor -- cannot reach the retained evidence.
        self._closure = _copy.deepcopy(closure)
        self._records = {
            spec: [_FrozenRecord(r.content()) for r in rows] for spec, rows in records.items()
        }
        self._metrics = _copy.deepcopy(metrics)
        self._failures = _copy.deepcopy(failures)
        self.execution_source_commit = execution_source_commit
        self.execution_source_tree = execution_source_tree

    @property
    def closure(self) -> dict[str, Any]:
        """A defensive copy: the trusted state is not editable through its accessor."""
        import copy

        return copy.deepcopy(self._closure)

    def complete_spec_hashes(self) -> tuple[str, ...]:
        return tuple(sorted(self._records))

    def records_for(self, spec_hash: str) -> list[Any]:
        """Deep copies: mutating a returned record cannot reach the retained evidence."""
        import copy as _copy

        try:
            rows = self._records[spec_hash]
        except KeyError:
            raise CampaignError(f"{spec_hash} did not complete; it has no OOF evidence") from None
        return [_FrozenRecord(_copy.deepcopy(r.content())) for r in rows]

    def metrics_for(self, spec_hash: str) -> dict[str, Any]:
        import copy as _copy

        try:
            return _copy.deepcopy(self._metrics[spec_hash])
        except KeyError:
            raise CampaignError(f"{spec_hash} did not complete; it has no metrics") from None

    def failures_for(self, spec_hash: str) -> list[dict[str, Any]]:
        import copy as _copy

        return _copy.deepcopy(list(self._failures.get(spec_hash, ())))

    def spec_entry(self, spec_hash: str) -> dict[str, Any]:
        import copy as _copy

        return _copy.deepcopy(self._closure["per_spec"][spec_hash])


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
    expected_cell_set: frozenset[tuple[str, str]] | None = None,
) -> dict[str, Any]:
    """COMPLETE means every cell predicted exactly once across all five folds. Nothing less.

    ``expected_cell_set`` closes the last gap a count cannot: one frozen cell missing and one
    foreign cell substituted still totals 1040.
    """
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

    exact_cell_set_verified = False
    if expected_cell_set is not None:
        observed = frozenset(seen)
        missing = expected_cell_set - observed
        foreign = observed - expected_cell_set
        if missing or foreign:
            reasons.append(
                f"the predicted cell set is not the frozen one: {len(missing)} missing, "
                f"{len(foreign)} foreign"
            )
        else:
            exact_cell_set_verified = True

    return {
        "status": STATUS_COMPLETE if not reasons else STATUS_TRAINING_FAILURE,
        "exact_cell_set_verified": exact_cell_set_verified,
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


def _run_l2g_train_oof_core(
    *,
    dataset: Any,
    design: Any,
    candidate_specs: tuple[Any, ...],
    reference_specs: tuple[Any, ...],
    fit_estimators: Any,
    fit_reference: Any,
    thread_report: tuple[dict[str, Any], ...] = (),
) -> dict[str, Any]:
    """INTERNAL, injectable core. NOT the production boundary.

    Dependency injection is what makes the structural properties testable on a small synthetic
    grid, and that same flexibility is exactly why this must not be what the real campaign calls:
    a caller who can supply the dataset, the specs and the fit callables can supply a different
    experiment. :func:`run_real_l2g_train_oof_campaign` is the sealed entry; this is its engine.
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

    expected_cell_set = frozenset((r.dataset_id, r.config_hash) for r in dataset.rows)
    chromosome_of = dict(dataset.cv_manifest.bam_chromosome)
    weights = dataset.admission_weights()
    score_weights = dataset.score_weights()
    rows = list(dataset.rows)

    per_spec: dict[str, dict[str, Any]] = {}
    complete_metrics: dict[str, dict[str, Any]] = {}
    retained_records: dict[str, list[Any]] = {}
    retained_failures: dict[str, list[dict[str, Any]]] = {}

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
            expected_cell_set=expected_cell_set,
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
                # both identities are DERIVED from the evidence, never supplied by a caller
                entry["oof_artifact_hash"] = oof_artifact_identity(records)
                entry["metric_artifact_hash"] = metric_artifact_identity(
                    metrics, spec_hash=spec.identity()
                )
                entry["metrics"] = metrics
                complete_metrics[spec.identity()] = metrics
                # the ACTUAL records are kept until publication succeeds
                retained_records[spec.identity()] = list(records)
        if failures:
            retained_failures[spec.identity()] = list(failures)
        per_spec[spec.identity()] = entry

    reference_entries = {s.identity(): per_spec[s.identity()] for s in reference_specs}
    incomplete_references = sorted(
        f"{e['family']}({', '.join(e['reasons'])})"
        for e in reference_entries.values()
        if e["status"] != STATUS_COMPLETE
    )
    all_references_complete = not incomplete_references

    result: dict[str, Any] = {
        "_records": retained_records,
        "_failures": retained_failures,
        "per_spec": per_spec,
        # the publisher re-checks every artifact against this, so it travels with the closure
        "expected_cell_set": sorted(expected_cell_set),
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


# ---------------------------------------------------------------------------------------- #
# THE SEALED PRODUCTION BOUNDARY
# ---------------------------------------------------------------------------------------- #
#: the exact accepted candidate identities, in their frozen order
ACCEPTED_CANDIDATE_SPEC_HASHES: Final[tuple[str, ...]] = (
    "e962fb55c99b8a866f2f808febae95d4d407008ea4af6a8d4aa05ecec5d1003f",
    "c8ff4aa819e6efa6de5e4d94bee35579e1c9029a12431a5f12e28d8a88546db2",
    "2328e0c11652d8c57c0f48997ab9c4e2b3c0395bb0f30563d9475e9d7f37be4c",
    "5e8f905c16aede23d00ee924fd42900512aa5cb890243dba50001cb19577d92e",
    "32539b329d4721a8fd2e7aa9065009e94e074b0a849367632020b30e83a7183c",
    "4e7f488e5de5633e4479c7d0a268b0ead9a23bf202683e0f77536bf3b80fd195",
)
#: the exact accepted reference identities, with the family each one must be
ACCEPTED_REFERENCE_SPECS: Final[tuple[tuple[str, str], ...]] = (
    ("CONSTANT_SAFE_BASELINE", "9a70ae658498db5c4347ce2203c879698d7b4739a54c406532d4bf1e2b26fe70"),
    ("GLOBAL_MEAN", "7cb963c0d503d107e3a5eda4e0e0fbba0dadf8dd7835f04c4bacdc90e32780b2"),
    ("CONFIG_ONLY", "b3be5e5c5c3e1fa10f2e42ae31a7260b3292050fd012330bcb3e6d87a2e1b826"),
    ("BAM_FEATURES_ONLY", "0e3342455c6868189830b5dc736b827eeb48e70ef8ffe5b74467a29e27cf54eb"),
)
ACCEPTED_CANDIDATE_FAMILIES: Final[tuple[str, ...]] = (
    "LINEAR_REGULARIZED",
    "LINEAR_REGULARIZED",
    "LINEAR_REGULARIZED",
    "TREE_ENSEMBLE",
    "TREE_ENSEMBLE",
    "COMPACT_MLP",
)

ACCEPTED_FEATURE_MATRIX_HASH: Final = (
    "c6a8db848318e5c78839474fa62a4e8e408157a1e6f5cb1bdd18c9cd3d0118b2"
)
ACCEPTED_CONFIG_ENCODING_IDENTITY: Final = (
    "3053fed09a1a7fdc9462a963871564275c88e4eca5fe3a898d2d6821c36b1fe4"
)
REQUIRED_THREAD_POLICY: Final = "SINGLE_THREADED_DETERMINISTIC"


def _verify_exact_specs(
    specs: tuple[Any, ...], *, expected: tuple[str, ...], families: tuple[str, ...], role: str
) -> None:
    """Exact identities and families -- not a count, and not a family set."""
    observed = tuple(s.identity() for s in specs)
    if len(set(observed)) != len(observed):
        raise CampaignError(f"a {role} spec identity appears twice")
    if observed != expected:
        raise CampaignError(
            f"the {role} spec identities are not the accepted frozen set.\n"
            f"  expected: {list(expected)}\n  derived:  {list(observed)}"
        )
    observed_families = tuple(s.family for s in specs)
    if observed_families != families:
        raise CampaignError(
            f"the {role} families are {list(observed_families)}, expected {list(families)}"
        )


def run_real_l2g_train_oof_campaign(
    *,
    feature_matrix_artifact_path: Any,
    workspace: Any = None,
    config_payload_root: Any = None,
    root: Any = None,
) -> TrustedL2GTrainCampaign:
    """THE trusted production entry for the real TRAIN OOF campaign.

    It accepts no dataset, no design matrix, no spec, no fit callable, no metric, no threshold,
    no shortlist and no thread report. Every one of those is derived here from the committed
    authority and the frozen bundle, because a boundary that accepts a scientific object cannot
    promise which experiment ran.

    The four parameters are operational handles only: where the bytes live. Each is verified
    against an identity the caller does not control, so pointing at the wrong file fails rather
    than substitutes.
    """
    from pathlib import Path

    from minos_engine.models.config_table import load_verified_config_vectors
    from minos_engine.models.design_matrix import (
        CONFIG_COLUMN_COUNT,
        CONTEXTUAL_COLUMN_COUNT,
        build_design_matrix,
    )
    from minos_engine.models.feature_values import load_verified_feature_values
    from minos_engine.models.fit_driver import fit_fold_estimators, fit_reference_fold
    from minos_engine.models.prefit_loader import (
        ACCEPTED_CV_MANIFEST_HASH,
        load_accepted_prefit_authority,
        load_verified_training_dataset,
    )
    from minos_engine.models.runtime import verify_training_runtime
    from minos_engine.models.spec_factory import (
        build_accepted_l2g_model_specs,
        build_accepted_l2g_reference_specs,
    )
    from minos_engine.models.threading_control import observe_thread_pools, single_threaded
    from minos_engine.qualification.git_tree import commit_tree_sha, is_commit

    # 0: the source that is ABOUT to fit the models, captured now. Reading Git at publication
    # time instead would let a later checkout be relabelled as the one that produced the result.
    from minos_engine.qualification.l2f_accepted_identities import repository_root
    from minos_engine.qualification.provenance import read_provenance

    source_root = Path(root) if root is not None else repository_root()
    provenance = read_provenance(source_root)
    if not provenance.head_sha or not provenance.tree_sha:
        raise CampaignError("the execution source provenance could not be read from Git")
    if not is_commit(source_root, provenance.head_sha):
        raise CampaignError(f"HEAD {provenance.head_sha} is not a commit in this repository")
    if commit_tree_sha(source_root, provenance.head_sha) != provenance.tree_sha:
        raise CampaignError("HEAD's recorded tree is not its actual tree")
    if not provenance.worktree_clean:
        raise CampaignError(
            "the worktree is dirty; a campaign fitted from uncommitted source cannot name the "
            "commit that produced it"
        )

    # 1-2: the committed authority, with its recomputable identities re-derived
    authority = load_accepted_prefit_authority(root)

    # 3-5: the dataset, rebuilt from bundle BYTES and required to hash to the accepted identity
    dataset = load_verified_training_dataset(workspace=workspace, root=root)
    if dataset.identity() != ACCEPTED_TRAINING_DATASET_HASH:
        raise CampaignError("the reconstructed dataset is not the accepted training dataset")
    if dataset.cv_manifest.identity() != ACCEPTED_CV_MANIFEST_HASH:
        raise CampaignError("the reconstructed CV manifest is not the accepted one")
    if dataset.feature_matrix_hash != ACCEPTED_FEATURE_MATRIX_HASH:
        raise CampaignError("the dataset cites a foreign feature matrix")
    if dataset.config_encoding_identity != ACCEPTED_CONFIG_ENCODING_IDENTITY:
        raise CampaignError("the dataset cites a foreign config encoding")

    # 6-7: the ACTUAL numeric matrix, hashed from the file the fit will consume
    bam_vectors = load_verified_feature_values(
        artifact_path=Path(feature_matrix_artifact_path), dataset=dataset
    )

    # 8-10: the 80 canonical payloads, each hashed to the name it is stored under
    config_hashes = tuple(sorted({r.config_hash for r in dataset.rows}))
    config_vectors, config_columns = load_verified_config_vectors(
        config_hashes=config_hashes,
        payload_root=Path(config_payload_root) if config_payload_root else None,
    )

    # 11: the design matrix is DERIVED, never accepted
    design = build_design_matrix(
        rows=dataset.rows,
        bam_vectors=bam_vectors,
        config_vectors=config_vectors,
        bam_columns=tuple(dataset.feature_names),
        config_columns=config_columns,
    )
    if design.x_bam.shape[1] != len(dataset.feature_names):
        raise CampaignError("the design matrix does not carry the qualified BAM columns")
    if design.x_config.shape[1] != CONFIG_COLUMN_COUNT:
        raise CampaignError("the design matrix does not carry the 28 config columns")
    if design.contextual.shape[1] != CONTEXTUAL_COLUMN_COUNT:
        raise CampaignError("the contextual matrix is not 157 columns")
    if len(design) != EXPECTED_OOF_RECORDS_PER_SPEC:
        raise CampaignError(
            f"the design matrix has {len(design)} rows, expected {EXPECTED_OOF_RECORDS_PER_SPEC}"
        )
    # cell-set equality, not merely equal lengths
    design_cells = {(m["dataset_id"], m["config_hash"]) for m in design.meta}
    dataset_cells = {(r.dataset_id, r.config_hash) for r in dataset.rows}
    if design_cells != dataset_cells:
        raise CampaignError(
            "the design matrix does not describe the frozen scientific cells: "
            f"{len(dataset_cells - design_cells)} missing, "
            f"{len(design_cells - dataset_cells)} foreign"
        )
    if len(design.meta) != len(design_cells):
        raise CampaignError("the design matrix repeats a scientific cell")

    # 12-14: specs derived internally, then required to BE the accepted identities
    candidate_specs = build_accepted_l2g_model_specs(dataset)
    reference_specs = build_accepted_l2g_reference_specs(dataset)
    _verify_exact_specs(
        candidate_specs,
        expected=ACCEPTED_CANDIDATE_SPEC_HASHES,
        families=ACCEPTED_CANDIDATE_FAMILIES,
        role="candidate",
    )
    _verify_exact_specs(
        reference_specs,
        expected=tuple(h for _, h in ACCEPTED_REFERENCE_SPECS),
        families=tuple(f for f, _ in ACCEPTED_REFERENCE_SPECS),
        role="reference",
    )
    recorded_candidates = tuple(e["spec_hash"] for e in authority["candidate_spec_hashes"])
    recorded_references = tuple(e["spec_hash"] for e in authority["reference_spec_hashes"])
    if recorded_candidates != ACCEPTED_CANDIDATE_SPEC_HASHES:
        raise CampaignError("the committed authority records different candidate specs")
    if recorded_references != tuple(h for _, h in ACCEPTED_REFERENCE_SPECS):
        raise CampaignError("the committed authority records different reference specs")

    # 15: the exact runtime, and 8: thread evidence OBSERVED here, never supplied
    runtime = verify_training_runtime()
    with single_threaded():
        observed_pools = [p for p in observe_thread_pools() if p["user_api"] in ("blas", "openmp")]
    if not observed_pools:
        raise CampaignError(
            "no BLAS/OpenMP pool was observable, so the SINGLE_THREADED_DETERMINISTIC claim "
            "cannot be evidenced"
        )
    if any(p["num_threads"] != 1 for p in observed_pools):
        raise CampaignError(f"thread enforcement did not bind: {observed_pools}")
    thread_report = tuple(
        {
            "user_api": p["user_api"],
            "internal_api": p["internal_api"],
            "num_threads": p["num_threads"],
            "prefix": p["prefix"],
        }
        for p in observed_pools
    )

    # 16-17: the committed fit implementations, and the internal core
    result = _run_l2g_train_oof_core(
        dataset=dataset,
        design=design,
        candidate_specs=candidate_specs,
        reference_specs=reference_specs,
        fit_estimators=fit_fold_estimators,
        fit_reference=fit_reference_fold,
        thread_report=thread_report,
    )
    result["authority"] = {
        "prefit_authority_sha256": _prefit_authority_sha256(root),
        "training_dataset_hash": dataset.identity(),
        "cv_manifest_hash": dataset.cv_manifest.identity(),
        "feature_matrix_hash": dataset.feature_matrix_hash,
        "feature_matrix_artifact_sha256": dataset.feature_matrix_artifact_sha256,
        "config_encoding_identity": dataset.config_encoding_identity,
        "training_contract_hash": dataset.training_contract_hash,
        "training_protocol_hash": dataset.training_protocol_hash,
        "training_runtime_hash": runtime["runtime_hash"],
    }
    result["thread_policy"] = REQUIRED_THREAD_POLICY
    result["candidate_spec_hashes"] = list(ACCEPTED_CANDIDATE_SPEC_HASHES)
    result["reference_spec_hashes"] = [h for _, h in ACCEPTED_REFERENCE_SPECS]

    # the retained evidence is lifted out of the closure and into the trusted object; a summary
    # dictionary is not the place for 10 x 1040 records
    records = result.pop("_records")
    failures = result.pop("_failures")
    metrics = {
        spec_hash: entry["metrics"]
        for spec_hash, entry in result["per_spec"].items()
        if entry["status"] == STATUS_COMPLETE
    }
    if set(records) != set(metrics):
        raise CampaignError(
            "the retained OOF evidence and the metric set disagree about which specs completed"
        )
    for spec_hash, spec_records in records.items():
        if not spec_records:
            raise CampaignError(f"{spec_hash} is COMPLETE but retained no OOF records")
    return TrustedL2GTrainCampaign(
        _MINT_TOKEN,
        closure=result,
        records=records,
        metrics=metrics,
        failures=failures,
        execution_source_commit=provenance.head_sha,
        execution_source_tree=provenance.tree_sha,
    )


def _prefit_authority_sha256(root: Any = None) -> str:
    """From the committed file's own bytes. Never a string a caller typed."""
    import hashlib
    from pathlib import Path

    from minos_engine.models.prefit_loader import PREFIT_AUTHORITY_PATH
    from minos_engine.qualification.l2f_accepted_identities import repository_root

    base = Path(root) if root is not None else repository_root()
    return hashlib.sha256((base / PREFIT_AUTHORITY_PATH).read_bytes()).hexdigest()
