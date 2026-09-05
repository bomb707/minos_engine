"""The four frozen REFERENCES the promotion rule is measured against.

The selection method is ``choose_config``, NOT ``select_config``: the latter is the single
production config-emission entry point on ``Layer2Service`` and an architecture test enforces
that exactly one definition of it exists. A reference is an evaluation baseline, not a second
way for the engine to emit a configuration.

"Beat the best reference" is the whole promotion threshold, so an under-specified reference is a
threshold nobody can reproduce. Each of these is fully determined: what it predicts, what it fits
on, whether it has an admission component, and how it breaks ties.

None of them may see a held-out label while fitting. ``CONSTANT_SAFE_BASELINE`` fits nothing at
all — it always selects the qualified baseline config, which is exactly the fallback a contextual
model has to justify replacing.
"""

from __future__ import annotations

from typing import Any, Final

from minos_engine.common.errors import MinosEngineError
from minos_engine.models.contract import SAFE_BASELINE_CONFIG_HASH
from minos_engine.models.protocol import RANDOM_SEED

__all__ = [
    "BamFeaturesOnlyRidge",
    "ConfigOnlyRidge",
    "ConstantSafeBaseline",
    "GlobalMean",
    "ReferenceError",
    "build_reference_model",
]

_EPSILON: Final = 1e-12


class ReferenceError(MinosEngineError):
    """A reference model was used outside its frozen definition."""


def _equal_bam_weights(dataset_ids: Any) -> Any:
    """EQUAL_BAM_TOTAL over whatever subset is being fitted."""
    import numpy as np

    ids = list(dataset_ids)
    counts: dict[str, int] = {}
    for value in ids:
        counts[value] = counts.get(value, 0) + 1
    return np.asarray([1.0 / counts[value] for value in ids], dtype=float)


class _Reference:
    """Shared prediction surface. Every reference exposes the same three methods."""

    family: str = "REFERENCE"

    def predict_admission_probability(self, meta: Any, x: Any = None) -> Any:  # pragma: no cover
        raise NotImplementedError

    def predict_admitted_score(self, meta: Any, x: Any = None) -> Any:  # pragma: no cover
        raise NotImplementedError

    def predict_expected_utility(self, meta: Any, x: Any = None) -> Any:
        import numpy as np

        p = np.clip(np.asarray(self.predict_admission_probability(meta, x), dtype=float), 0.0, 1.0)
        s = np.clip(np.asarray(self.predict_admitted_score(meta, x), dtype=float), 0.0, 1.0)
        return p * s


class ConstantSafeBaseline(_Reference):
    """Always the qualified baseline config. No contextual ranking, nothing fitted.

    Its admission probability is the safe baseline's own empirical admission rate on the fitting
    BAMs; its score is that config's equal-BAM mean admitted score. Selection is not a ranking —
    it returns the baseline config for every BAM, which is what the deployed fallback does.
    """

    family = "CONSTANT_SAFE_BASELINE"
    config_hash: Final = SAFE_BASELINE_CONFIG_HASH

    def __init__(self, admission_rate: float, admitted_score: float) -> None:
        self.admission_rate = float(admission_rate)
        self.admitted_score = float(admitted_score)

    @classmethod
    def fit(cls, meta: Any) -> ConstantSafeBaseline:
        import numpy as np

        rows = [m for m in meta if m["config_hash"] == cls.config_hash]
        if not rows:
            raise ReferenceError(
                "the safe baseline config does not appear in the fitting evidence; its reference "
                "utility cannot be invented"
            )
        weights = _equal_bam_weights([m["dataset_id"] for m in rows])
        labels = np.asarray([m["admission_label"] for m in rows], dtype=float)
        rate = float(np.average(labels, weights=weights))
        admitted = [m for m in rows if m["admitted_score"] is not None]
        if admitted:
            aw = _equal_bam_weights([m["dataset_id"] for m in admitted])
            score = float(
                np.average(
                    np.asarray([m["admitted_score"] for m in admitted], dtype=float), weights=aw
                )
            )
        else:
            score = 0.0
        return cls(rate, score)

    def choose_config(self, _candidate_hashes: Any) -> str:
        return self.config_hash

    def predict_admission_probability(self, meta: Any, x: Any = None) -> Any:
        import numpy as np

        return np.full(len(list(meta)), self.admission_rate, dtype=float)

    def predict_admitted_score(self, meta: Any, x: Any = None) -> Any:
        import numpy as np

        return np.full(len(list(meta)), self.admitted_score, dtype=float)


class GlobalMean(_Reference):
    """One number for every cell: the equal-BAM weighted admission rate and admitted score.

    Every config scores identically, so the selection MUST NOT depend on iteration order — the
    frozen tie-break is the lowest config hash lexicographically.
    """

    family = "GLOBAL_MEAN"
    tie_break: Final = "LOWEST_CONFIG_HASH_LEXICOGRAPHIC"

    def __init__(self, admission_rate: float, admitted_score: float) -> None:
        self.admission_rate = float(admission_rate)
        self.admitted_score = float(admitted_score)

    @classmethod
    def fit(cls, meta: Any) -> GlobalMean:
        import numpy as np

        rows = list(meta)
        if not rows:
            raise ReferenceError("no fitting evidence")
        weights = _equal_bam_weights([m["dataset_id"] for m in rows])
        labels = np.asarray([m["admission_label"] for m in rows], dtype=float)
        rate = float(np.average(labels, weights=weights))
        admitted = [m for m in rows if m["admitted_score"] is not None]
        if not admitted:
            raise ReferenceError("no admitted example to derive a mean score from")
        aw = _equal_bam_weights([m["dataset_id"] for m in admitted])
        score = float(
            np.average(np.asarray([m["admitted_score"] for m in admitted], dtype=float), weights=aw)
        )
        return cls(rate, score)

    def choose_config(self, candidate_hashes: Any) -> str:
        options = sorted(candidate_hashes)
        if not options:
            raise ReferenceError("no candidate configs to select from")
        return str(options[0])

    def predict_admission_probability(self, meta: Any, x: Any = None) -> Any:
        import numpy as np

        return np.full(len(list(meta)), self.admission_rate, dtype=float)

    def predict_admitted_score(self, meta: Any, x: Any = None) -> Any:
        import numpy as np

        return np.full(len(list(meta)), self.admitted_score, dtype=float)


class _LinearBlockReference(_Reference):
    """Ridge score head + NESTED-CALIBRATED logistic admission head over ONE block of columns.

    Their frozen ModelSpecs carry
    ``admission_probability_calibration = NESTED_CROSS_FITTED_WITHIN_EACH_OUTER_FOLD``, so
    returning raw ``predict_proba`` would make the implementation contradict the specification it
    is hashed under. The calibration is therefore built the same way the candidates' is: inner
    BAM-grouped folds over the outer-training BAMs only, isotonic fitted on those inner pairs, and
    the mapping applied to the untouched outer fold.

    Every scaler is fold-local by construction -- each is fitted inside a method that only ever
    receives that fold's training rows.
    """

    family = "BLOCK"
    tie_break: Final = "LOWEST_CONFIG_HASH_LEXICOGRAPHIC"

    def __init__(
        self,
        scaler: Any,
        score_head: Any,
        admission_head: Any,
        calibrator: Any,
        calibration_bams: frozenset[str],
    ) -> None:
        self._scaler = scaler
        self._score = score_head
        self._admission = admission_head
        self._calibrator = calibrator
        self.calibration_bams = calibration_bams

    @classmethod
    def fit(
        cls,
        x: Any,
        meta: Any,
        *,
        inner_folds: list[tuple[frozenset[str], frozenset[str]]],
        outer_heldout_bams: frozenset[str],
    ) -> _LinearBlockReference:
        import numpy as np
        from sklearn.linear_model import LogisticRegression, Ridge
        from sklearn.preprocessing import StandardScaler

        from minos_engine.models.calibration import (
            CalibrationError,
            fit_nested_admission_calibrator,
        )
        from minos_engine.models.oof_runner import TrainingFailure

        rows = list(meta)
        if len(rows) != len(x):
            raise ReferenceError("predictor rows and metadata disagree in length")
        x = np.asarray(x, dtype=float)
        labels = np.asarray([m["admission_label"] for m in rows], dtype=int)
        if len(np.unique(labels)) < 2:
            raise TrainingFailure("the reference admission fold carries a single class")

        scaler = StandardScaler().fit(x)
        scaled = scaler.transform(x)
        weights = _equal_bam_weights([m["dataset_id"] for m in rows])

        admission_head = LogisticRegression(max_iter=1000, random_state=RANDOM_SEED)
        admission_head.fit(scaled, labels, sample_weight=weights)

        # --- nested calibration over the OUTER-TRAINING BAMs only ------------------------- #
        inner_probabilities: list[float] = []
        inner_labels: list[int] = []
        calibration_bams: set[str] = set()
        for inner_train, inner_held in inner_folds:
            fit_idx = [i for i, m in enumerate(rows) if m["dataset_id"] in inner_train]
            held_idx = [i for i, m in enumerate(rows) if m["dataset_id"] in inner_held]
            if not fit_idx or not held_idx:
                raise TrainingFailure("a reference inner fold is empty")
            inner_y = labels[fit_idx]
            if len(np.unique(inner_y)) < 2:
                raise TrainingFailure(
                    "a reference inner admission fold carries a single class; nested calibration "
                    "cannot be executed under the frozen protocol"
                )
            inner_scaler = StandardScaler().fit(x[fit_idx])
            inner_head = LogisticRegression(max_iter=1000, random_state=RANDOM_SEED)
            inner_head.fit(
                inner_scaler.transform(x[fit_idx]),
                inner_y,
                sample_weight=_equal_bam_weights([rows[i]["dataset_id"] for i in fit_idx]),
            )
            probabilities = inner_head.predict_proba(inner_scaler.transform(x[held_idx]))[:, 1]
            if not np.all(np.isfinite(probabilities)):
                raise TrainingFailure("a reference inner fold produced non-finite probabilities")
            inner_probabilities.extend(float(v) for v in probabilities)
            inner_labels.extend(int(labels[i]) for i in held_idx)
            calibration_bams.update(inner_train)

        try:
            calibrator = fit_nested_admission_calibrator(
                inner_probabilities=inner_probabilities,
                inner_labels=inner_labels,
                calibration_bams=frozenset(calibration_bams),
                outer_heldout_bams=outer_heldout_bams,
                inner_folds=len(inner_folds),
            )
        except CalibrationError as exc:
            raise TrainingFailure(f"reference nested calibration failed: {exc}") from exc

        admitted_idx = [i for i, m in enumerate(rows) if m["admitted_score"] is not None]
        if not admitted_idx:
            raise TrainingFailure("no admitted example to fit the reference score head on")
        score_head = Ridge(alpha=1.0, random_state=RANDOM_SEED)
        score_head.fit(
            scaled[admitted_idx],
            np.asarray([rows[i]["admitted_score"] for i in admitted_idx], dtype=float),
            sample_weight=_equal_bam_weights([rows[i]["dataset_id"] for i in admitted_idx]),
        )
        return cls(scaler, score_head, admission_head, calibrator, frozenset(calibration_bams))

    def _scaled(self, x: Any) -> Any:
        import numpy as np

        return self._scaler.transform(np.asarray(x, dtype=float))

    def predict_admission_probability(self, meta: Any, x: Any = None) -> Any:
        """CALIBRATED, as the frozen spec says. Raw predict_proba would contradict it."""
        raw = self._admission.predict_proba(self._scaled(x))[:, 1]
        return self._calibrator.apply(raw)

    def raw_admission_probability(self, x: Any) -> Any:
        """The uncalibrated probability, kept for the OOF record's raw column."""
        probabilities = self._admission.predict_proba(self._scaled(x))[:, 1]
        return probabilities

    def predict_admitted_score(self, meta: Any, x: Any = None) -> Any:
        import numpy as np

        return np.clip(self._score.predict(self._scaled(x)), 0.0, 1.0)

    def choose_config(self, candidate_hashes: Any) -> str:
        options = sorted(candidate_hashes)
        if not options:
            raise ReferenceError("no candidate configs to select from")
        return str(options[0])


class ConfigOnlyRidge(_LinearBlockReference):
    """The 28 config columns, blind to the BAM."""

    family = "CONFIG_ONLY"


class BamFeaturesOnlyRidge(_LinearBlockReference):
    """The 129 BAM columns, blind to the config.

    Deliberately cannot rank configs at all: it measures how much of the score is simply "which
    BAM did you get", which is the thing the campaign must beat rather than reproduce. Its
    selection is therefore the frozen lexicographic tie-break, not a ranking.
    """

    family = "BAM_FEATURES_ONLY"


def build_reference_model(
    family: str,
    *,
    x: Any,
    meta: Any,
    inner_folds: list[tuple[frozenset[str], frozenset[str]]] | None = None,
    outer_heldout_bams: frozenset[str] | None = None,
) -> Any:
    """Fit one frozen reference on FOLD-TRAINING evidence only.

    The two constant references take no calibration, exactly as their frozen specs say; the two
    block references take the nested calibrator theirs demand, which is why they require the inner
    folds and the outer fold identity.
    """
    if family == "CONSTANT_SAFE_BASELINE":
        return ConstantSafeBaseline.fit(meta)
    if family == "GLOBAL_MEAN":
        return GlobalMean.fit(meta)
    if family in ("CONFIG_ONLY", "BAM_FEATURES_ONLY"):
        if inner_folds is None or outer_heldout_bams is None:
            raise ReferenceError(
                f"{family} is nested-calibrated and cannot be fitted without its inner folds"
            )
        cls = ConfigOnlyRidge if family == "CONFIG_ONLY" else BamFeaturesOnlyRidge
        return cls.fit(x, meta, inner_folds=inner_folds, outer_heldout_bams=outer_heldout_bams)
    raise ReferenceError(f"{family!r} is not a frozen reference family")
