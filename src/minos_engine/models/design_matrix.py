"""Deterministic predictor construction: X_bam (129) | X_config (28) = X_contextual (157).

Metadata — ``dataset_id``, ``chromosome``, ``config_hash``, outcome and provenance — is carried
BESIDE the matrix, never inside it. A model that can see the BAM id can memorise which BAM it is
looking at, which is precisely the thing chromosome-held-out CV exists to measure.

Column order is fixed by the authorities (``AUTHORITATIVE_COLUMNS`` then the encoder's own order),
so no dict iteration or database row order can move a prediction.
"""

from __future__ import annotations

from typing import Any, Final

from minos_engine.common.errors import MinosEngineError
from minos_engine.models.contract import FEATURE_COLUMN_COUNT

__all__ = [
    "CONFIG_COLUMN_COUNT",
    "CONTEXTUAL_COLUMN_COUNT",
    "DesignMatrix",
    "DesignMatrixError",
    "build_design_matrix",
]

CONFIG_COLUMN_COUNT: Final = 28
CONTEXTUAL_COLUMN_COUNT: Final = FEATURE_COLUMN_COUNT + CONFIG_COLUMN_COUNT


class DesignMatrixError(MinosEngineError):
    """The predictor matrix could not be built deterministically."""


class DesignMatrix:
    """Predictors plus the metadata that must never become a predictor."""

    __slots__ = ("bam_columns", "config_columns", "meta", "x_bam", "x_config")

    def __init__(
        self,
        *,
        x_bam: Any,
        x_config: Any,
        bam_columns: tuple[str, ...],
        config_columns: tuple[str, ...],
        meta: tuple[dict[str, Any], ...],
    ) -> None:
        self.x_bam = x_bam
        self.x_config = x_config
        self.bam_columns = bam_columns
        self.config_columns = config_columns
        self.meta = meta

    @property
    def contextual(self) -> Any:
        """[X_bam | X_config] — the 157 columns a contextual candidate sees."""
        import numpy as np

        return np.hstack([self.x_bam, self.x_config])

    @property
    def columns(self) -> tuple[str, ...]:
        return (*self.bam_columns, *self.config_columns)

    def __len__(self) -> int:
        return len(self.meta)


def build_design_matrix(
    *,
    rows: Any,
    bam_vectors: dict[str, tuple[float, ...]],
    config_vectors: dict[str, tuple[float, ...]],
    bam_columns: tuple[str, ...],
    config_columns: tuple[str, ...],
) -> DesignMatrix:
    """One predictor row per scientific cell, in the caller's row order but column-deterministic.

    ``bam_vectors`` and ``config_vectors`` come from the verified feature matrix and the accepted
    config encoder; this function never derives a feature value itself.
    """
    import numpy as np

    if len(bam_columns) != FEATURE_COLUMN_COUNT:
        raise DesignMatrixError(f"{len(bam_columns)} BAM columns, expected {FEATURE_COLUMN_COUNT}")
    if len(config_columns) != CONFIG_COLUMN_COUNT:
        raise DesignMatrixError(
            f"{len(config_columns)} config columns, expected {CONFIG_COLUMN_COUNT}"
        )

    x_bam, x_config, meta = [], [], []
    for row in rows:
        try:
            bam_vector = bam_vectors[row.dataset_id]
        except KeyError:
            raise DesignMatrixError(f"no verified feature vector for {row.dataset_id}") from None
        try:
            config_vector = config_vectors[row.config_hash]
        except KeyError:
            raise DesignMatrixError(f"no encoded config vector for {row.config_hash}") from None
        if len(bam_vector) != FEATURE_COLUMN_COUNT:
            raise DesignMatrixError(f"{row.dataset_id} has {len(bam_vector)} feature values")
        if len(config_vector) != CONFIG_COLUMN_COUNT:
            raise DesignMatrixError(f"{row.config_hash} encoded to {len(config_vector)} columns")
        x_bam.append(bam_vector)
        x_config.append(config_vector)
        meta.append(
            {
                "dataset_id": row.dataset_id,
                "chromosome": row.chromosome,
                "config_hash": row.config_hash,
                "outcome": row.outcome,
                "admitted_score": row.admitted_score,
                "admission_label": row.admission_label,
                "identity": row.identity(),
            }
        )
    matrix = DesignMatrix(
        x_bam=np.asarray(x_bam, dtype=float),
        x_config=np.asarray(x_config, dtype=float),
        bam_columns=bam_columns,
        config_columns=config_columns,
        meta=tuple(meta),
    )
    if matrix.contextual.shape[1] != CONTEXTUAL_COLUMN_COUNT:
        raise DesignMatrixError(
            f"contextual matrix has {matrix.contextual.shape[1]} columns, expected "
            f"{CONTEXTUAL_COLUMN_COUNT}"
        )
    return matrix
