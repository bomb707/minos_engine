"""Nested isotonic calibration of P(ADMITTED). Scoped to the admission head, nothing else.

The obvious procedure leaks. Fit isotonic on the outer out-of-fold probabilities and then report
calibration error and regret on those same pairs, and each held-out chromosome's own labels have
built the mapping applied to it: the calibrator has seen the answer.

So for every outer fold the mapping is fitted on INNER out-of-fold pairs drawn only from the 40
training BAMs — four remaining chromosome groups, one chromosome already absent — and applied to
the untouched held-out chromosome. The score regression is never calibrated here; only the
admission probability is.
"""

from __future__ import annotations

from typing import Any

from minos_engine.common.errors import MinosEngineError

__all__ = ["CalibrationError", "NestedAdmissionCalibrator", "fit_nested_admission_calibrator"]


class CalibrationError(MinosEngineError):
    """The calibration mapping could not be fitted honestly."""


class NestedAdmissionCalibrator:
    """An isotonic mapping plus the exact BAM set it was allowed to see."""

    __slots__ = ("_isotonic", "calibration_bams", "inner_folds")

    def __init__(self, isotonic: Any, calibration_bams: frozenset[str], inner_folds: int) -> None:
        self._isotonic = isotonic
        self.calibration_bams = calibration_bams
        self.inner_folds = inner_folds

    def apply(self, probabilities: Any) -> Any:
        import numpy as np

        calibrated = np.clip(
            self._isotonic.predict(np.asarray(probabilities, dtype=float)), 0.0, 1.0
        )
        if not np.all(np.isfinite(calibrated)):
            raise CalibrationError("the calibrated probabilities are not finite")
        return calibrated

    def refuses(self, dataset_id: str) -> bool:
        """True when this BAM was NOT visible to the calibrator — the property that matters."""
        return dataset_id not in self.calibration_bams


def fit_nested_admission_calibrator(
    *,
    inner_probabilities: Any,
    inner_labels: Any,
    calibration_bams: frozenset[str],
    outer_heldout_bams: frozenset[str],
    inner_folds: int,
) -> NestedAdmissionCalibrator:
    """Fit isotonic on INNER pairs only, and prove the outer fold contributed none of them."""
    import numpy as np
    from sklearn.isotonic import IsotonicRegression

    leaked = calibration_bams & outer_heldout_bams
    if leaked:
        raise CalibrationError(
            f"the calibration set contains held-out BAMs {sorted(leaked)}; their labels would "
            "build the mapping that is then applied to them"
        )
    probabilities = np.asarray(inner_probabilities, dtype=float)
    labels = np.asarray(inner_labels, dtype=float)
    if probabilities.shape != labels.shape:
        raise CalibrationError("inner probabilities and labels differ in shape")
    if probabilities.size == 0:
        raise CalibrationError("no inner out-of-fold pairs to calibrate from")
    if len(np.unique(labels)) < 2:
        # isotonic on a single class is not a calibration, it is a constant dressed as one
        raise CalibrationError(
            "the inner calibration labels are degenerate (one class only); this is a training "
            "failure, not something to paper over"
        )
    isotonic = IsotonicRegression(out_of_bounds="clip", y_min=0.0, y_max=1.0)
    isotonic.fit(probabilities, labels)
    return NestedAdmissionCalibrator(isotonic, frozenset(calibration_bams), inner_folds)
