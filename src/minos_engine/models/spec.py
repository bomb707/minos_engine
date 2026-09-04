"""``l2g-model-spec-v1`` / ``l2g-model-bundle-v1`` — what was fitted, and what it produced.

Every model that reaches selection is described by a hashed :class:`ModelSpec` before it is
fitted, so the candidate set is frozen rather than discovered. A :class:`ModelBundle` then binds
the spec to the artifacts it produced, each by SHA-256 — there is no opaque pickle whose contents
are known only to the process that wrote it.

Model families are ordered by capacity deliberately. The campaign has 1175 rows but only **50
independent BAMs**, and the between-BAM spread of mean score (0.191) is larger than the
between-config spread (0.166) — most of the variance is which BAM you got, not which config you
chose. That is the regime where a high-capacity model memorises 50 contexts and reports a
flattering in-fold number, so the trivial references are not a formality: they are the thing a
contextual model has to beat.
"""

from __future__ import annotations

from typing import Any, Final

from pydantic import BaseModel, ConfigDict, Field

from minos_engine.common.canonical_json import canonical_json_bytes
from minos_engine.common.errors import MinosEngineError
from minos_engine.common.hashing import sha256_hex

__all__ = [
    "MODEL_BUNDLE_DOMAIN",
    "MODEL_BUNDLE_SCHEMA",
    "MODEL_FAMILIES",
    "MODEL_SPEC_DOMAIN",
    "MODEL_SPEC_SCHEMA",
    "SELECTION_ORDER",
    "ArtifactRef",
    "ModelBundle",
    "ModelSpec",
    "ModelSpecError",
]

MODEL_SPEC_SCHEMA: Final = "l2g-model-spec-v1"
MODEL_SPEC_DOMAIN: Final = "minos:l2g-model-spec:v1\n"
MODEL_BUNDLE_SCHEMA: Final = "l2g-model-bundle-v1"
MODEL_BUNDLE_DOMAIN: Final = "minos:l2g-model-bundle:v1\n"

#: ordered by capacity, lowest first. The first three are references a contextual model must beat.
MODEL_FAMILIES: Final[tuple[str, ...]] = (
    "CONSTANT_SAFE_BASELINE",
    "GLOBAL_MEAN",
    "CONFIG_ONLY",
    "BAM_FEATURES_ONLY",
    "LINEAR_REGULARIZED",
    "TREE_ENSEMBLE",
    "COMPACT_MLP",
)

#: frozen BEFORE any VALIDATION number is looked at.
SELECTION_ORDER: Final[tuple[str, ...]] = (
    "no_leakage_and_complete_folds",
    "no_training_or_infrastructure_failure",
    "calibration_within_tolerance",
    "downside_not_worse_than_safe_baseline",
    "lowest_bam_grouped_regret",
    "simpler_family_on_scientific_tie",
    "deterministic_spec_hash_tie_break",
)

_STRICT = ConfigDict(extra="forbid", frozen=True, strict=True)


class ModelSpecError(MinosEngineError):
    """The model specification or bundle is incomplete or unverifiable."""


class ArtifactRef(BaseModel):
    """One external artifact, identified by content rather than by filename."""

    model_config = _STRICT

    path: str = Field(min_length=1)
    sha256: str = Field(min_length=64, max_length=64)
    media_type: str = Field(min_length=1)
    size_bytes: int = Field(ge=0)


class ModelSpec(BaseModel):
    """A candidate model, fully described and hashed BEFORE it is fitted."""

    model_config = _STRICT

    schema_version: str = MODEL_SPEC_SCHEMA
    family: str = Field(min_length=1)
    implementation: str = Field(min_length=1)
    target_formulation: str = Field(min_length=1)
    feature_schema_hash: str = Field(min_length=64, max_length=64)
    config_schema_hash: str = Field(min_length=64, max_length=64)
    transform_specification: dict[str, Any]
    hyperparameters: dict[str, Any]
    random_seed: int
    loss: str = Field(min_length=1)
    failure_risk_formulation: str = Field(min_length=1)
    calibration_method: str = Field(min_length=1)
    ood_method: str = Field(min_length=1)
    training_dataset_hash: str = Field(min_length=64, max_length=64)
    cv_manifest_hash: str = Field(min_length=64, max_length=64)

    def model_post_init(self, _context: Any) -> None:
        if self.family not in MODEL_FAMILIES:
            raise ModelSpecError(
                f"{self.family!r} is not a frozen model family; the candidate set is decided "
                f"before fitting, not discovered during it. Known: {list(MODEL_FAMILIES)}"
            )

    def content(self) -> dict[str, Any]:
        return {
            "calibration_method": self.calibration_method,
            "config_schema_hash": self.config_schema_hash,
            "cv_manifest_hash": self.cv_manifest_hash,
            "failure_risk_formulation": self.failure_risk_formulation,
            "family": self.family,
            "feature_schema_hash": self.feature_schema_hash,
            "hyperparameters": self.hyperparameters,
            "implementation": self.implementation,
            "loss": self.loss,
            "ood_method": self.ood_method,
            "random_seed": self.random_seed,
            "schema_version": self.schema_version,
            "target_formulation": self.target_formulation,
            "training_dataset_hash": self.training_dataset_hash,
            "transform_specification": self.transform_specification,
        }

    def identity(self) -> str:
        return sha256_hex(MODEL_SPEC_DOMAIN.encode("utf-8") + canonical_json_bytes(self.content()))


class ModelBundle(BaseModel):
    """A fitted model and everything needed to re-verify or replay it."""

    model_config = _STRICT

    schema_version: str = MODEL_BUNDLE_SCHEMA
    spec_hash: str = Field(min_length=64, max_length=64)
    baseline_qualified_gate_hash: str = Field(min_length=64, max_length=64)
    safe_baseline_config_hash: str = Field(min_length=64, max_length=64)
    training_dataset_hash: str = Field(min_length=64, max_length=64)
    cv_manifest_hash: str = Field(min_length=64, max_length=64)
    model_artifact: ArtifactRef
    transform_artifact: ArtifactRef
    oof_prediction_artifact: ArtifactRef
    cv_metric_artifact: ArtifactRef
    calibration_artifact: ArtifactRef
    ood_artifact: ArtifactRef | None = None
    runtime: dict[str, str]

    def content(self) -> dict[str, Any]:
        return {
            "baseline_qualified_gate_hash": self.baseline_qualified_gate_hash,
            "calibration_artifact": self.calibration_artifact.model_dump(mode="json"),
            "cv_manifest_hash": self.cv_manifest_hash,
            "cv_metric_artifact": self.cv_metric_artifact.model_dump(mode="json"),
            "model_artifact": self.model_artifact.model_dump(mode="json"),
            "oof_prediction_artifact": self.oof_prediction_artifact.model_dump(mode="json"),
            "ood_artifact": (
                self.ood_artifact.model_dump(mode="json") if self.ood_artifact else None
            ),
            "runtime": dict(sorted(self.runtime.items())),
            "safe_baseline_config_hash": self.safe_baseline_config_hash,
            "schema_version": self.schema_version,
            "spec_hash": self.spec_hash,
            "training_dataset_hash": self.training_dataset_hash,
            "transform_artifact": self.transform_artifact.model_dump(mode="json"),
        }

    def identity(self) -> str:
        return sha256_hex(
            MODEL_BUNDLE_DOMAIN.encode("utf-8") + canonical_json_bytes(self.content())
        )
