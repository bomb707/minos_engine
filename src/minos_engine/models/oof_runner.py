"""The deterministic five-fold out-of-fold runner. SOURCE ONLY — it is not run on real TRAIN here.

One outer fold per chromosome. Within each fold every transform, every estimator and every
calibrator sees only the 40 training BAMs; the held-out ten are touched exactly once, to be
predicted. Each scientific cell is therefore predicted exactly once, by a model that never saw
its BAM, and every emitted record carries the training and calibration BAM sets so that claim is
checkable rather than asserted.

Failures are recorded, never absorbed. A convergence warning, a numerical exception, a non-finite
prediction, a single-class admission fold or a degenerate calibration are all TRAINING_FAILURE for
that spec and fold — because a candidate that could not be fitted must not be able to win by
having fewer folds counted against it. These are campaign defects and are never confused with
genomic candidate failures.
"""

from __future__ import annotations

import warnings
from typing import Any, Final

from minos_engine.common.canonical_json import canonical_json_bytes
from minos_engine.common.errors import MinosEngineError
from minos_engine.common.hashing import sha256_hex
from minos_engine.models.contract import CV_FOLD_CHROMOSOMES

__all__ = [
    "OOF_RECORD_SCHEMA",
    "TRAINING_FAILURE",
    "OofRecord",
    "OofRunnerError",
    "TrainingFailure",
    "metric_artifact_identity",
    "oof_artifact_identity",
    "run_outer_oof",
]

OOF_RECORD_SCHEMA: Final = "l2g-oof-record-v1"
TRAINING_FAILURE: Final = "TRAINING_FAILURE"


class OofRunnerError(MinosEngineError):
    """The out-of-fold campaign could not be run honestly."""


class TrainingFailure(OofRunnerError):
    """One spec/fold could not be fitted. Recorded as evidence, never silently skipped."""


class OofRecord:
    """One held-out prediction, carrying the proof it was held out."""

    __slots__ = (
        "actual_admitted_score",
        "actual_outcome",
        "actual_utility",
        "calibrated_admission_probability",
        "calibration_bams_identity",
        "chromosome",
        "clipped_score_prediction",
        "config_hash",
        "dataset_id",
        "expected_utility_prediction",
        "family",
        "model_spec_hash",
        "outer_fold",
        "raw_admission_probability",
        "raw_score_prediction",
        "training_bams_identity",
    )

    def __init__(self, **fields: Any) -> None:
        for name in self.__slots__:
            setattr(self, name, fields[name])

    def content(self) -> dict[str, Any]:
        return {name: getattr(self, name) for name in sorted(self.__slots__)}


def _bam_set_identity(bams: frozenset[str]) -> str:
    return sha256_hex(canonical_json_bytes(sorted(bams)))


def oof_artifact_identity(records: list[OofRecord]) -> str:
    """Deterministic identity of a whole OOF artifact, order-independent."""
    digests = sorted(sha256_hex(canonical_json_bytes(r.content())) for r in records)
    return sha256_hex(b"minos:l2g-oof-artifact:v1\n" + canonical_json_bytes({"records": digests}))


def _clip01(values: Any) -> Any:
    import numpy as np

    return np.clip(np.asarray(values, dtype=float), 0.0, 1.0)


def _check_finite(values: Any, what: str) -> Any:
    import numpy as np

    array = np.asarray(values, dtype=float)
    if not np.all(np.isfinite(array)):
        raise TrainingFailure(f"{what} produced a non-finite value")
    return array


def _inner_folds(
    training_bams: frozenset[str], chromosome_of: dict[str, str]
) -> list[tuple[frozenset[str], frozenset[str]]]:
    """Four inner folds over the 40 -- one chromosome is already absent from the outer split."""
    groups: dict[str, set[str]] = {}
    for bam in training_bams:
        groups.setdefault(chromosome_of[bam], set()).add(bam)
    if len(groups) < 2:
        raise TrainingFailure("the inner split has fewer than two chromosome groups")
    folds = []
    for chromosome in sorted(groups):
        held = frozenset(groups[chromosome])
        folds.append((frozenset(training_bams) - held, held))
    return folds


def run_outer_oof(
    *,
    spec: Any,
    rows: Any,
    design: Any,
    chromosome_of: dict[str, str],
    weights: dict[str, float],
    score_weights: dict[str, float],
    fit_estimators: Any,
) -> tuple[list[OofRecord], list[dict[str, Any]]]:
    """Produce one held-out prediction per eligible cell, plus the fold failures.

    ``fit_estimators`` is injected so the structural guarantees below can be exercised without a
    real estimator: the leakage properties belong to the runner, not to scikit-learn.
    """
    import numpy as np

    meta = list(design.meta)
    if len(meta) != len(rows):
        raise OofRunnerError("design metadata and rows disagree in length")
    x = design.contextual
    by_index = dict(enumerate(meta))

    records: list[OofRecord] = []
    failures: list[dict[str, Any]] = []
    predicted: set[str] = set()

    for chromosome in CV_FOLD_CHROMOSOMES:
        held_bams = frozenset(b for b, c in chromosome_of.items() if c == chromosome)
        train_bams = frozenset(chromosome_of) - held_bams
        if not held_bams or not train_bams:
            raise OofRunnerError(f"outer fold {chromosome} is degenerate")

        train_idx = [i for i, m in by_index.items() if m["dataset_id"] in train_bams]
        held_idx = [i for i, m in by_index.items() if m["dataset_id"] in held_bams]
        if not held_idx:
            continue

        inner = _inner_folds(train_bams, chromosome_of)
        try:
            with warnings.catch_warnings():
                # a model that did not converge has not been fitted; treating its predictions as
                # a result would let it win a comparison it never actually entered
                warnings.simplefilter("error", category=UserWarning)
                try:
                    from sklearn.exceptions import ConvergenceWarning

                    warnings.simplefilter("error", category=ConvergenceWarning)
                except ImportError:  # pragma: no cover - sklearn is a hard dependency
                    pass
                fitted = fit_estimators(
                    spec=spec,
                    x_train=x[train_idx],
                    meta_train=[by_index[i] for i in train_idx],
                    weights=weights,
                    score_weights=score_weights,
                    inner_folds=inner,
                    train_bams=train_bams,
                    held_bams=held_bams,
                )
        except TrainingFailure as exc:
            failures.append({"fold": chromosome, "reason": str(exc), "class": TRAINING_FAILURE})
            continue
        except Exception as exc:  # numerical blow-ups are failures, not results
            failures.append(
                {
                    "fold": chromosome,
                    "reason": f"{type(exc).__name__}: {exc}",
                    "class": TRAINING_FAILURE,
                }
            )
            continue

        try:
            raw_p = _check_finite(fitted["raw_admission"](x[held_idx]), "admission probability")
            cal_p = _check_finite(fitted["calibrate"](raw_p), "calibrated admission probability")
            raw_s = _check_finite(fitted["raw_score"](x[held_idx]), "score prediction")
        except TrainingFailure as exc:
            failures.append({"fold": chromosome, "reason": str(exc), "class": TRAINING_FAILURE})
            continue
        clipped_s = _clip01(raw_s)
        cal_p = _clip01(cal_p)
        utility = cal_p * clipped_s

        calibration_bams = frozenset(fitted["calibration_bams"])
        if calibration_bams & held_bams:
            raise OofRunnerError(
                f"fold {chromosome}: the calibrator saw held-out BAMs "
                f"{sorted(calibration_bams & held_bams)}"
            )
        train_identity = _bam_set_identity(train_bams)
        calibration_identity = _bam_set_identity(calibration_bams)

        for position, index in enumerate(held_idx):
            m = by_index[index]
            if m["dataset_id"] in train_bams:  # pragma: no cover - set algebra guarantees this
                raise OofRunnerError("a held-out cell belongs to the training BAM set")
            if m["identity"] in predicted:
                raise OofRunnerError(
                    f"cell {m['identity']} was predicted more than once; every cell is held out "
                    "in exactly one fold"
                )
            predicted.add(m["identity"])
            records.append(
                OofRecord(
                    model_spec_hash=spec.identity(),
                    family=spec.family,
                    dataset_id=m["dataset_id"],
                    chromosome=m["chromosome"],
                    config_hash=m["config_hash"],
                    actual_outcome=m["outcome"],
                    actual_admitted_score=m["admitted_score"],
                    actual_utility=float(m["admitted_score"] or 0.0)
                    if m["admission_label"] == 1
                    else 0.0,
                    raw_admission_probability=float(raw_p[position]),
                    calibrated_admission_probability=float(cal_p[position]),
                    raw_score_prediction=float(np.asarray(raw_s)[position]),
                    clipped_score_prediction=float(clipped_s[position]),
                    expected_utility_prediction=float(utility[position]),
                    outer_fold=chromosome,
                    training_bams_identity=train_identity,
                    calibration_bams_identity=calibration_identity,
                )
            )
    return records, failures


def metric_artifact_identity(metrics: dict[str, Any], *, spec_hash: str) -> str:
    """Identity of one spec's metric artifact, BOUND to the spec it describes.

    Binding the spec hash is what makes a swap detectable: two artifacts exchanged between specs
    keep their values and their uniqueness, so only an identity that depends on WHOSE artifact it
    is can catch the exchange.
    """
    return sha256_hex(
        b"minos:l2g-metric-artifact:v1\n"
        + canonical_json_bytes({"metrics": _jsonable(metrics), "model_spec_hash": spec_hash})
    )


def _jsonable(value: Any) -> Any:
    """Canonical JSON cannot carry NaN; a missing diagnostic is recorded as absent, not as 0."""
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in sorted(value.items())}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if isinstance(value, float) and value != value:
        return None
    return value
