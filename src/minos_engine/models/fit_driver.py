"""Fit ONE spec on ONE outer fold: transforms, both heads, and the nested calibrator.

This is where the leakage rules become code. The scaler is fitted on the outer training rows and
on nothing else; the inner scalers are fitted per inner fold; the isotonic mapping never sees a
row from the outer held-out chromosome. Everything is fitted inside a single-threaded context so
the runtime's SINGLE_THREADED_DETERMINISTIC claim is enforced rather than asserted.
"""

from __future__ import annotations

from typing import Any

from minos_engine.models.calibration import (
    CalibrationError,
    fit_nested_admission_calibrator,
)
from minos_engine.models.estimators import build_admission_estimator, build_score_estimator
from minos_engine.models.oof_runner import TrainingFailure
from minos_engine.models.threading_control import single_threaded

__all__ = ["fit_fold_estimators"]


def _weights_for(meta: list[dict[str, Any]], weights: dict[str, float]) -> Any:
    import numpy as np

    try:
        return np.asarray([weights[m["identity"]] for m in meta], dtype=float)
    except KeyError as exc:  # a cell with no EQUAL_BAM_TOTAL weight must not be fitted
        raise TrainingFailure(f"no equal-BAM weight for cell {exc}") from None


def fit_fold_estimators(
    *,
    spec: Any,
    x_train: Any,
    meta_train: list[dict[str, Any]],
    weights: dict[str, float],
    score_weights: dict[str, float],
    inner_folds: list[tuple[frozenset[str], frozenset[str]]],
    train_bams: frozenset[str],
    held_bams: frozenset[str],
) -> dict[str, Any]:
    """Return the callables the OOF runner needs, plus the BAM set the calibrator saw."""
    import numpy as np
    from sklearn.preprocessing import StandardScaler

    labels = np.asarray([m["admission_label"] for m in meta_train], dtype=int)
    if len(np.unique(labels)) < 2:
        raise TrainingFailure("the admission training fold carries a single class")

    with single_threaded():
        # fitted on OUTER TRAINING rows only -- the held-out chromosome is not in x_train
        scaler = StandardScaler().fit(x_train)
        scaled = scaler.transform(x_train)

        admission = build_admission_estimator(spec)
        admission.fit(scaled, labels, sample_weight=_weights_for(meta_train, weights))

        admitted = [i for i, m in enumerate(meta_train) if m["admitted_score"] is not None]
        if not admitted:
            raise TrainingFailure("the fold carries no ADMITTED example to fit the score head on")
        score = build_score_estimator(spec)
        score.fit(
            scaled[admitted],
            np.asarray([meta_train[i]["admitted_score"] for i in admitted], dtype=float),
            sample_weight=_weights_for([meta_train[i] for i in admitted], score_weights),
        )

        # --- nested calibration: inner OOF pairs from the 40 training BAMs only ------------- #
        inner_probabilities: list[float] = []
        inner_labels: list[int] = []
        calibration_bams: set[str] = set()
        for inner_train, inner_held in inner_folds:
            fit_idx = [i for i, m in enumerate(meta_train) if m["dataset_id"] in inner_train]
            held_idx = [i for i, m in enumerate(meta_train) if m["dataset_id"] in inner_held]
            if not fit_idx or not held_idx:
                continue
            inner_y = labels[fit_idx]
            if len(np.unique(inner_y)) < 2:
                continue
            inner_scaler = StandardScaler().fit(x_train[fit_idx])
            inner_admission = build_admission_estimator(spec)
            inner_admission.fit(
                inner_scaler.transform(x_train[fit_idx]),
                inner_y,
                sample_weight=_weights_for([meta_train[i] for i in fit_idx], weights),
            )
            probabilities = inner_admission.predict_proba(
                inner_scaler.transform(x_train[held_idx])
            )[:, 1]
            inner_probabilities.extend(float(p) for p in probabilities)
            inner_labels.extend(int(labels[i]) for i in held_idx)
            calibration_bams.update(inner_train)

        try:
            calibrator = fit_nested_admission_calibrator(
                inner_probabilities=inner_probabilities,
                inner_labels=inner_labels,
                calibration_bams=frozenset(calibration_bams),
                outer_heldout_bams=held_bams,
                inner_folds=len(inner_folds),
            )
        except CalibrationError as exc:
            raise TrainingFailure(f"nested calibration failed: {exc}") from exc

    if not calibration_bams <= train_bams:  # pragma: no cover - construction guarantees this
        raise TrainingFailure("the calibrator saw a BAM outside the outer training set")

    return {
        "raw_admission": lambda x: admission.predict_proba(scaler.transform(x))[:, 1],
        "raw_score": lambda x: score.predict(scaler.transform(x)),
        "calibrate": calibrator.apply,
        "calibration_bams": frozenset(calibration_bams),
    }
