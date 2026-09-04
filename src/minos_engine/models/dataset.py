"""``l2g-training-dataset-v1`` and ``l2g-cv-manifest-v1`` — what may be learned from, and how split.

The dataset identity is order-independent: rows are folded into the hash through a sorted digest
of their per-row identities, so re-reading the same evidence in a different order produces the
same dataset. A dataset whose identity moved because a query returned rows differently would be
worthless as provenance.

The CV protocol is BAM-grouped and chromosome-held-out, five folds, one per chromosome. The
grouping unit is the BAM, never the row: one BAM contributes between 10 and 97 config rows in
this campaign, and splitting them would put the same BAM's features on both sides of a fold. The
resulting estimate would measure memorisation, not generalisation to an unseen BAM.

Nothing here reads TEST, and a TEST row reaching either contract is a refusal rather than a
filter.
"""

from __future__ import annotations

from typing import Any, Final

from pydantic import BaseModel, ConfigDict, Field

from minos_engine.common.canonical_json import canonical_json_bytes
from minos_engine.common.errors import MinosEngineError
from minos_engine.common.hashing import sha256_hex
from minos_engine.models.contract import (
    CANDIDATE_FAILURE_LABELS,
    CV_FOLD_CHROMOSOMES,
    FORBIDDEN_AT_INFERENCE,
    TRAIN_BAM_COUNT,
)

__all__ = [
    "CV_MANIFEST_DOMAIN",
    "CV_MANIFEST_SCHEMA",
    "TRAINING_DATASET_DOMAIN",
    "TRAINING_DATASET_SCHEMA",
    "CvManifest",
    "TrainingDataset",
    "TrainingDatasetError",
    "TrainingRow",
]

TRAINING_DATASET_SCHEMA: Final = "l2g-training-dataset-v1"
TRAINING_DATASET_DOMAIN: Final = "minos:l2g-training-dataset:v1\n"
CV_MANIFEST_SCHEMA: Final = "l2g-cv-manifest-v1"
CV_MANIFEST_DOMAIN: Final = "minos:l2g-cv-manifest:v1\n"

_STRICT = ConfigDict(extra="forbid", frozen=True, strict=True)


class TrainingDatasetError(MinosEngineError):
    """The learning table violates the training contract."""


class TrainingRow(BaseModel):
    """One (BAM, config) observation. Identity is metadata; it is never model input."""

    model_config = _STRICT

    dataset_id: str = Field(min_length=1)
    chromosome: str = Field(min_length=1)
    config_hash: str = Field(min_length=64, max_length=64)
    job_key: str = Field(min_length=64, max_length=64)
    partition: str = Field(min_length=1)
    #: True when GATK produced a scored result; False for a bounded candidate failure.
    succeeded: bool
    #: present exactly when ``succeeded`` -- never fabricated for a failure.
    minos_score: float | None = None
    failure_code: str | None = None

    def model_post_init(self, _context: Any) -> None:
        if self.partition != "train":
            raise TrainingDatasetError(
                f"row {self.dataset_id} is partition {self.partition!r}; the L2-G learning table "
                "is TRAIN only, and VALIDATION/TEST rows are refused rather than filtered"
            )
        if self.succeeded:
            if self.minos_score is None:
                raise TrainingDatasetError("a succeeded row must carry a minos_score")
            if self.failure_code is not None:
                raise TrainingDatasetError("a succeeded row must not carry a failure_code")
            if not 0.0 <= self.minos_score <= 1.0:
                raise TrainingDatasetError(f"minos_score {self.minos_score} is outside [0, 1]")
        else:
            if self.minos_score is not None:
                raise TrainingDatasetError(
                    "a failed row must not carry a minos_score; a crashed run produced no score, "
                    "and inventing 0.0 would teach the model that it produced a terrible one"
                )
            if self.failure_code not in CANDIDATE_FAILURE_LABELS:
                raise TrainingDatasetError(
                    f"failure_code {self.failure_code!r} is not a bounded CANDIDATE failure; "
                    "an infrastructure incident is our defect and is never a training label"
                )

    def identity(self) -> str:
        return sha256_hex(
            canonical_json_bytes(
                {
                    "config_hash": self.config_hash,
                    "dataset_id": self.dataset_id,
                    "failure_code": self.failure_code,
                    "job_key": self.job_key,
                    "minos_score": self.minos_score,
                    "succeeded": self.succeeded,
                }
            )
        )


class CvManifest(BaseModel):
    """Deterministic BAM-grouped, chromosome-held-out folds. No randomness, no row-level split."""

    model_config = _STRICT

    schema_version: str = CV_MANIFEST_SCHEMA
    #: dataset_id -> chromosome. The fold index is derived, never stored loosely.
    bam_chromosome: dict[str, str]

    def model_post_init(self, _context: Any) -> None:
        if len(self.bam_chromosome) != TRAIN_BAM_COUNT:
            raise TrainingDatasetError(
                f"the CV manifest holds {len(self.bam_chromosome)} BAMs, expected {TRAIN_BAM_COUNT}"
            )
        unknown = sorted(set(self.bam_chromosome.values()) - set(CV_FOLD_CHROMOSOMES))
        if unknown:
            raise TrainingDatasetError(f"unknown fold chromosomes: {unknown}")

    def fold_of(self, dataset_id: str) -> int:
        """The held-out fold this BAM belongs to."""
        try:
            chromosome = self.bam_chromosome[dataset_id]
        except KeyError:
            raise TrainingDatasetError(f"{dataset_id} is not in the CV manifest") from None
        return CV_FOLD_CHROMOSOMES.index(chromosome)

    def folds(self) -> tuple[tuple[frozenset[str], frozenset[str]], ...]:
        """``(train_bams, heldout_bams)`` per fold, one fold per chromosome."""
        result = []
        for chromosome in CV_FOLD_CHROMOSOMES:
            held = frozenset(b for b, c in self.bam_chromosome.items() if c == chromosome)
            train = frozenset(self.bam_chromosome) - held
            if not held or not train:
                raise TrainingDatasetError(f"fold {chromosome} is degenerate")
            if train & held:  # pragma: no cover - set algebra guarantees this
                raise TrainingDatasetError("a BAM appears on both sides of a fold")
            result.append((train, held))
        return tuple(result)

    def content(self) -> dict[str, Any]:
        return {
            "bam_chromosome": dict(sorted(self.bam_chromosome.items())),
            "fold_chromosomes": list(CV_FOLD_CHROMOSOMES),
            "grouping_unit": "BAM_DATASET_ID",
            "randomised": False,
            "schema_version": self.schema_version,
        }

    def identity(self) -> str:
        return sha256_hex(CV_MANIFEST_DOMAIN.encode("utf-8") + canonical_json_bytes(self.content()))


class TrainingDataset(BaseModel):
    """The immutable L2-G learning table, bound to every upstream authority it depends on."""

    model_config = _STRICT

    schema_version: str = TRAINING_DATASET_SCHEMA
    baseline_qualified_gate_hash: str = Field(min_length=64, max_length=64)
    baseline_selected_hash: str = Field(min_length=64, max_length=64)
    feature_registry_hash: str = Field(min_length=64, max_length=64)
    config_encoding_identity: str = Field(min_length=64, max_length=64)
    parameter_space_hash: str = Field(min_length=64, max_length=64)
    scoring_contract_hash: str = Field(min_length=64, max_length=64)
    execution_environment_hash: str = Field(min_length=64, max_length=64)
    train_plan_hashes: tuple[str, ...]
    feature_names: tuple[str, ...]
    config_feature_names: tuple[str, ...]
    rows: tuple[TrainingRow, ...]
    cv_manifest: CvManifest

    def model_post_init(self, _context: Any) -> None:
        leaked = sorted(set(self.feature_names) & set(FORBIDDEN_AT_INFERENCE))
        if leaked:
            raise TrainingDatasetError(
                f"the predictor matrix carries fields the model may never see at inference: "
                f"{leaked}"
            )
        for forbidden in ("dataset_id", "round_id", "partition", "chromosome"):
            if any(
                name == forbidden or name.endswith(f".{forbidden}") for name in self.feature_names
            ):
                raise TrainingDatasetError(
                    f"{forbidden!r} is metadata, not a predictor; using it invites memorisation "
                    "of the training BAMs rather than generalisation to a new one"
                )
        bams = {row.dataset_id for row in self.rows}
        missing = sorted(bams - set(self.cv_manifest.bam_chromosome))
        if missing:
            raise TrainingDatasetError(
                f"rows reference BAMs absent from the CV manifest: {missing}"
            )

    @property
    def scored_rows(self) -> tuple[TrainingRow, ...]:
        """Only these train the SCORE model. A failure has no score to learn from."""
        return tuple(r for r in self.rows if r.succeeded)

    @property
    def decided_rows(self) -> tuple[TrainingRow, ...]:
        """All of these train the FAILURE-RISK model: success and bounded failure alike."""
        return self.rows

    def content(self) -> dict[str, Any]:
        return {
            "baseline_qualified_gate_hash": self.baseline_qualified_gate_hash,
            "baseline_selected_hash": self.baseline_selected_hash,
            "bam_count": len({r.dataset_id for r in self.rows}),
            "candidate_failure_count": sum(1 for r in self.rows if not r.succeeded),
            "config_count": len({r.config_hash for r in self.rows}),
            "config_encoding_identity": self.config_encoding_identity,
            "config_feature_names": list(self.config_feature_names),
            "cv_manifest_identity": self.cv_manifest.identity(),
            "execution_environment_hash": self.execution_environment_hash,
            "feature_names": list(self.feature_names),
            "feature_registry_hash": self.feature_registry_hash,
            "parameter_space_hash": self.parameter_space_hash,
            # ORDER-INDEPENDENT: the identity is of the row SET, not of any traversal of it.
            "row_identity_digest": sha256_hex(
                ",".join(sorted(r.identity() for r in self.rows)).encode("utf-8")
            ),
            "row_count": len(self.rows),
            "schema_version": self.schema_version,
            "scored_row_count": len(self.scored_rows),
            "scoring_contract_hash": self.scoring_contract_hash,
            "train_plan_hashes": list(self.train_plan_hashes),
        }

    def identity(self) -> str:
        return sha256_hex(
            TRAINING_DATASET_DOMAIN.encode("utf-8") + canonical_json_bytes(self.content())
        )
