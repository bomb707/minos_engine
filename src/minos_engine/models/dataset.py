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
    BAMS_PER_CHROMOSOME,
    CANDIDATE_FAILURE_LABELS,
    CV_FOLD_CHROMOSOMES,
    DEDUP_POLICY,
    FEATURE_COLUMN_COUNT,
    FORBIDDEN_AT_INFERENCE,
    FROZEN_FEATURE_SET_HASH,
    OUTCOME_ADMITTED,
    OUTCOME_CLASSES,
    OUTCOME_EXECUTION_FAILURE,
    OUTCOME_NON_ADMISSION,
    TRAIN_BAM_COUNT,
    WEIGHTING_POLICY,
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
    "BamFeatureBinding",
]

TRAINING_DATASET_SCHEMA: Final = "l2g-training-dataset-v2"
TRAINING_DATASET_DOMAIN: Final = "minos:l2g-training-dataset:v2\n"
CV_MANIFEST_SCHEMA: Final = "l2g-cv-manifest-v1"
CV_MANIFEST_DOMAIN: Final = "minos:l2g-cv-manifest:v1\n"

_STRICT = ConfigDict(extra="forbid", frozen=True, strict=True)


class TrainingDatasetError(MinosEngineError):
    """The learning table violates the training contract."""


class BamFeatureBinding(BaseModel):
    """One BAM's row in the qualified production feature matrix, bound by VALUE identity.

    Binding names alone would let a feature VALUE change without moving the dataset identity,
    which is the whole point of having one.
    """

    model_config = _STRICT

    dataset_id: str = Field(min_length=1)
    vector_hash: str = Field(min_length=1)
    feature_values_hash: str = Field(min_length=1)


class TrainingRow(BaseModel):
    """One (BAM, config) LEARNING EXAMPLE. Identity is metadata; it is never model input.

    The outcome class is explicit because the three cases mean different things to the two model
    components, and collapsing them was the v1 defect.
    """

    model_config = _STRICT

    dataset_id: str = Field(min_length=1)
    chromosome: str = Field(min_length=1)
    config_hash: str = Field(min_length=64, max_length=64)
    partition: str = Field(min_length=1)
    outcome: str = Field(min_length=1)
    #: present exactly when ADMITTED. Never fabricated, and never taken from a non-admission.
    admitted_score: float | None = None
    admission_code: str | None = None
    execution_failure_code: str | None = None
    #: every job that produced this scientific cell -- provenance, not extra loss weight.
    source_job_keys: tuple[str, ...]
    source_plan_hashes: tuple[str, ...]

    def model_post_init(self, _context: Any) -> None:
        if self.partition != "train":
            raise TrainingDatasetError(
                f"row {self.dataset_id} is partition {self.partition!r}; the L2-G learning table "
                "is TRAIN only, and VALIDATION/TEST rows are refused rather than filtered"
            )
        if self.outcome not in OUTCOME_CLASSES:
            raise TrainingDatasetError(
                f"{self.outcome!r} is not a decided outcome class; an infrastructure incident is "
                "our defect and is never a training label"
            )
        if not self.source_job_keys:
            raise TrainingDatasetError("a learning example must cite the evidence that produced it")

        if self.outcome == OUTCOME_ADMITTED:
            if self.admitted_score is None:
                raise TrainingDatasetError("an ADMITTED example must carry its persisted score")
            if not 0.0 <= self.admitted_score <= 1.0:
                raise TrainingDatasetError(f"score {self.admitted_score} is outside [0, 1]")
            if self.execution_failure_code is not None:
                raise TrainingDatasetError("an ADMITTED example cannot carry a failure code")
        elif self.outcome == OUTCOME_NON_ADMISSION:
            if self.admitted_score is not None:
                raise TrainingDatasetError(
                    "a non-admitted evaluation's minos_score is NOT utility evidence; the frozen "
                    "objective refuses to consume it, so it must not become a regression label"
                )
            if not self.admission_code or self.admission_code == "ADMITTED":
                raise TrainingDatasetError(
                    "a non-admission must preserve the admission_code that explains it"
                )
            if self.execution_failure_code is not None:
                raise TrainingDatasetError(
                    "a non-admission is not a GATK crash; dressing it up as a bounded execution "
                    "failure code would erase the distinction the objective depends on"
                )
        else:  # CANDIDATE_EXECUTION_FAILURE
            if self.admitted_score is not None:
                raise TrainingDatasetError(
                    "a crashed run produced no score; inventing one teaches the model it produced "
                    "a terrible one"
                )
            if self.execution_failure_code not in CANDIDATE_FAILURE_LABELS:
                raise TrainingDatasetError(
                    f"failure code {self.execution_failure_code!r} is not a bounded CANDIDATE "
                    "failure; an infrastructure incident is never a training label"
                )

    @property
    def admission_label(self) -> int:
        """The admission model's target: 1 for ADMITTED, 0 for either kind of candidate failure."""
        return 1 if self.outcome == OUTCOME_ADMITTED else 0

    @property
    def is_score_example(self) -> bool:
        """Only ADMITTED examples train the biological score regressor."""
        return self.outcome == OUTCOME_ADMITTED

    def identity(self) -> str:
        return sha256_hex(
            canonical_json_bytes(
                {
                    "admission_code": self.admission_code,
                    "admitted_score": self.admitted_score,
                    "chromosome": self.chromosome,
                    "config_hash": self.config_hash,
                    "dataset_id": self.dataset_id,
                    "execution_failure_code": self.execution_failure_code,
                    "outcome": self.outcome,
                    # provenance is sorted so phase ORDER cannot move the scientific identity
                    "source_job_keys": sorted(self.source_job_keys),
                    "source_plan_hashes": sorted(set(self.source_plan_hashes)),
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
        # "50 total" alone would admit 46/1/1/1/1, which is five folds in name only.
        for chromosome in CV_FOLD_CHROMOSOMES:
            count = sum(1 for c in self.bam_chromosome.values() if c == chromosome)
            if count != BAMS_PER_CHROMOSOME:
                raise TrainingDatasetError(
                    f"{chromosome} holds {count} TRAIN BAMs, expected {BAMS_PER_CHROMOSOME}; a "
                    "lopsided split is five folds in name only"
                )

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
    feature_set_hash: str = Field(min_length=64, max_length=64)
    feature_matrix_hash: str = Field(min_length=64, max_length=64)
    feature_matrix_artifact_sha256: str = Field(min_length=64, max_length=64)
    #: the 50 TRAIN BAMs' feature VALUE identities. A changed value moves the dataset identity.
    bam_features: tuple[BamFeatureBinding, ...]
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
        if self.feature_set_hash != FROZEN_FEATURE_SET_HASH:
            raise TrainingDatasetError(
                f"feature_set_hash {self.feature_set_hash} is not the qualified production set "
                f"{FROZEN_FEATURE_SET_HASH}; a feature promotion must re-qualify the matrix "
                "before it can train a model"
            )
        if len(self.feature_names) != FEATURE_COLUMN_COUNT:
            raise TrainingDatasetError(
                f"{len(self.feature_names)} predictor columns are not the qualified "
                f"{FEATURE_COLUMN_COUNT}; the registry's eligible field list is wider than the "
                "matrix that was actually qualified for production"
            )
        if len(set(self.feature_names)) != len(self.feature_names):
            raise TrainingDatasetError("a predictor column name appears twice")
        bound: set[str] = set()
        for binding in self.bam_features:
            if binding.dataset_id in bound:
                raise TrainingDatasetError(
                    f"{binding.dataset_id} appears twice in the feature bindings"
                )
            bound.add(binding.dataset_id)
        manifest_bams = set(self.cv_manifest.bam_chromosome)
        if bound != manifest_bams:
            raise TrainingDatasetError(
                "the feature bindings and the CV manifest describe different BAM sets: "
                f"{sorted(bound ^ manifest_bams)}"
            )
        uncovered = sorted(manifest_bams - bams)
        if uncovered:
            raise TrainingDatasetError(
                f"manifest BAMs contribute no learning example: {uncovered}; a fold that holds "
                "out a BAM with no rows measures nothing"
            )
        seen: set[tuple[str, str]] = set()
        for row in self.rows:
            pair = (row.dataset_id, row.config_hash)
            if pair in seen:
                raise TrainingDatasetError(
                    f"the (BAM, config) cell {pair} appears more than once; the campaign "
                    "scheduled some pairs repeatedly and a cell must not gain weight for that"
                )
            seen.add(pair)

    @property
    def score_examples(self) -> tuple[TrainingRow, ...]:
        """Only ADMITTED examples train the score regressor."""
        return tuple(r for r in self.rows if r.is_score_example)

    @property
    def admission_examples(self) -> tuple[TrainingRow, ...]:
        """Every decided outcome trains the admission model, positive and negative alike."""
        return self.rows

    def admission_weights(self) -> dict[str, float]:
        """EQUAL_BAM_TOTAL: every BAM contributes the same total admission-loss weight.

        Without this, the ten Phase-B BAMs (up to 80 examples each) would dominate the forty that
        carry ten — the model would fit the BAMs that happened to be scheduled most, not the
        population.
        """
        per_bam: dict[str, int] = {}
        for row in self.rows:
            per_bam[row.dataset_id] = per_bam.get(row.dataset_id, 0) + 1
        return {r.identity(): 1.0 / per_bam[r.dataset_id] for r in self.rows}

    def score_weights(self) -> dict[str, float]:
        """EQUAL_BAM_TOTAL over ADMITTED examples only.

        A BAM with no admitted example contributes nothing rather than dividing by zero, and is
        reported by :meth:`bams_without_score_examples` rather than silently dropped.
        """
        per_bam: dict[str, int] = {}
        for row in self.score_examples:
            per_bam[row.dataset_id] = per_bam.get(row.dataset_id, 0) + 1
        return {r.identity(): 1.0 / per_bam[r.dataset_id] for r in self.score_examples}

    def bams_without_score_examples(self) -> tuple[str, ...]:
        """BAMs the score regressor cannot learn from. Declared, never quietly ignored."""
        with_scores = {r.dataset_id for r in self.score_examples}
        return tuple(sorted(set(self.cv_manifest.bam_chromosome) - with_scores))

    def content(self) -> dict[str, Any]:
        return {
            "baseline_qualified_gate_hash": self.baseline_qualified_gate_hash,
            "baseline_selected_hash": self.baseline_selected_hash,
            "bam_count": len({r.dataset_id for r in self.rows}),
            "admitted_example_count": len(self.score_examples),
            "non_admission_example_count": sum(
                1 for r in self.rows if r.outcome == OUTCOME_NON_ADMISSION
            ),
            "execution_failure_example_count": sum(
                1 for r in self.rows if r.outcome == OUTCOME_EXECUTION_FAILURE
            ),
            "bams_without_score_examples": list(self.bams_without_score_examples()),
            "dedup_policy": DEDUP_POLICY,
            "weighting_policy": WEIGHTING_POLICY,
            "feature_set_hash": self.feature_set_hash,
            "feature_column_count": len(self.feature_names),
            "feature_matrix_hash": self.feature_matrix_hash,
            "feature_matrix_artifact_sha256": self.feature_matrix_artifact_sha256,
            "bam_feature_bindings": [
                b.model_dump(mode="json")
                for b in sorted(self.bam_features, key=lambda x: x.dataset_id)
            ],
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
            "learning_example_count": len(self.rows),
            "schema_version": self.schema_version,
            "scoring_contract_hash": self.scoring_contract_hash,
            "train_plan_hashes": list(self.train_plan_hashes),
        }

    def identity(self) -> str:
        return sha256_hex(
            TRAINING_DATASET_DOMAIN.encode("utf-8") + canonical_json_bytes(self.content())
        )
