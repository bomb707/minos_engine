"""``l2g-relative-finalist-dataset-v1`` — the dense 200-cell / 150-advantage learning table.

The four-finalist slice of TRAIN turned out to be completely dense: every one of the 50 BAMs has a
cell for every one of the four finalists, and all 200 were ADMITTED. So this dataset has no
missing-cell policy to argue about — a gap here would be a refusal, not an imputation.

Each row is one ADVANTAGE example: a BAM, an alternative finalist, and
``DELTA = U(BAM, alternative) - U(BAM, safe_baseline)``. The baseline itself contributes no row.
Its advantage is exactly zero by definition, and training a regressor on 50 rows of constant zero
would teach nothing while diluting the loss.
"""

from __future__ import annotations

from typing import Any, Final

from pydantic import BaseModel, ConfigDict, Field

from minos_engine.common.canonical_json import canonical_json_bytes
from minos_engine.common.hashing import sha256_hex
from minos_engine.models.relative_finalist_contract import (
    ALTERNATIVE_FINALISTS,
    DELTA_SAFE_BASELINE,
    FINALIST_DOMAIN,
    FORBIDDEN_V2_PREDICTORS,
    RELATIVE_EXAMPLE_COUNT,
    SAFE_BASELINE_CONFIG_HASH,
    TRAIN_BAM_COUNT,
    RelativeFinalistError,
    compute_finalist_domain_hash,
    compute_relative_contract_hash,
)

__all__ = [
    "RELATIVE_DATASET_DOMAIN",
    "RELATIVE_DATASET_SCHEMA",
    "AdvantageRow",
    "RelativeFinalistDataset",
    "build_relative_finalist_dataset",
]

RELATIVE_DATASET_SCHEMA: Final = "l2g-relative-finalist-dataset-v1"
RELATIVE_DATASET_DOMAIN: Final = "minos:l2g-relative-finalist-dataset:v1\n"

_STRICT = ConfigDict(extra="forbid", frozen=True, strict=True)


class AdvantageRow(BaseModel):
    """One (BAM, alternative finalist) advantage example."""

    model_config = _STRICT

    dataset_id: str = Field(min_length=1)
    chromosome: str = Field(min_length=1)
    config_hash: str = Field(min_length=64, max_length=64)
    safe_utility: float
    alternative_utility: float
    delta: float

    def model_post_init(self, _context: Any) -> None:
        if self.config_hash == SAFE_BASELINE_CONFIG_HASH:
            raise RelativeFinalistError(
                "the safe baseline is the reference action, not an advantage example; its "
                "advantage is zero by definition"
            )
        if self.config_hash not in ALTERNATIVE_FINALISTS:
            raise RelativeFinalistError(
                f"{self.config_hash} is not one of the frozen alternative finalists"
            )
        expected = self.alternative_utility - self.safe_utility
        if abs(self.delta - expected) > 1e-12:
            raise RelativeFinalistError(
                f"delta {self.delta} is not alternative minus safe ({expected})"
            )

    @property
    def switch_helps(self) -> bool:
        return self.delta > 0.0

    def identity(self) -> str:
        return sha256_hex(
            canonical_json_bytes(
                {
                    "alternative_utility": self.alternative_utility,
                    "chromosome": self.chromosome,
                    "config_hash": self.config_hash,
                    "dataset_id": self.dataset_id,
                    "delta": self.delta,
                    "safe_utility": self.safe_utility,
                }
            )
        )


class RelativeFinalistDataset(BaseModel):
    """The v2 learning table, bound to every authority it depends on."""

    model_config = _STRICT

    schema_version: str = RELATIVE_DATASET_SCHEMA
    parent_campaign_freeze_identity: str = Field(min_length=64, max_length=64)
    source_training_dataset_hash: str = Field(min_length=64, max_length=64)
    relative_contract_hash: str = Field(min_length=64, max_length=64)
    finalist_domain_hash: str = Field(min_length=64, max_length=64)
    feature_set_hash: str = Field(min_length=64, max_length=64)
    feature_matrix_hash: str = Field(min_length=64, max_length=64)
    config_encoding_identity: str = Field(min_length=64, max_length=64)
    bam_chromosome: dict[str, str]
    #: the 200 dense source cells, by identity, so the slice is provable
    source_cell_identities: tuple[str, ...]
    safe_utility: dict[str, float]
    rows: tuple[AdvantageRow, ...]

    def model_post_init(self, _context: Any) -> None:
        if len(self.bam_chromosome) != TRAIN_BAM_COUNT:
            raise RelativeFinalistError(
                f"{len(self.bam_chromosome)} BAMs, expected {TRAIN_BAM_COUNT}"
            )
        if len(self.source_cell_identities) != TRAIN_BAM_COUNT * len(FINALIST_DOMAIN):
            raise RelativeFinalistError(
                f"{len(self.source_cell_identities)} source cells, expected "
                f"{TRAIN_BAM_COUNT * len(FINALIST_DOMAIN)}; the four-finalist slice must be dense"
            )
        if len(set(self.source_cell_identities)) != len(self.source_cell_identities):
            raise RelativeFinalistError("a source cell appears twice")
        if len(self.rows) != RELATIVE_EXAMPLE_COUNT:
            raise RelativeFinalistError(
                f"{len(self.rows)} advantage examples, expected {RELATIVE_EXAMPLE_COUNT}"
            )
        seen: set[tuple[str, str]] = set()
        for row in self.rows:
            if row.dataset_id not in self.bam_chromosome:
                raise RelativeFinalistError(f"{row.dataset_id} is not a frozen TRAIN BAM")
            if self.bam_chromosome[row.dataset_id] != row.chromosome:
                raise RelativeFinalistError(f"{row.dataset_id} disagrees on its chromosome")
            if row.safe_utility != self.safe_utility[row.dataset_id]:
                raise RelativeFinalistError(
                    f"{row.dataset_id} cites two different safe-baseline utilities"
                )
            pair = (row.dataset_id, row.config_hash)
            if pair in seen:
                raise RelativeFinalistError(f"{pair} appears twice")
            seen.add(pair)
        if len(seen) != RELATIVE_EXAMPLE_COUNT:
            raise RelativeFinalistError("the advantage table is not the dense 50 x 3 slice")

    def content(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "parent_campaign_freeze_identity": self.parent_campaign_freeze_identity,
            "source_training_dataset_hash": self.source_training_dataset_hash,
            "relative_contract_hash": self.relative_contract_hash,
            "finalist_domain_hash": self.finalist_domain_hash,
            "feature_set_hash": self.feature_set_hash,
            "feature_matrix_hash": self.feature_matrix_hash,
            "config_encoding_identity": self.config_encoding_identity,
            "bam_chromosome": dict(sorted(self.bam_chromosome.items())),
            "source_cell_identity_digest": sha256_hex(
                canonical_json_bytes(sorted(self.source_cell_identities))
            ),
            "safe_utility": dict(sorted(self.safe_utility.items())),
            "delta_safe_baseline": DELTA_SAFE_BASELINE,
            "row_identity_digest": sha256_hex(
                canonical_json_bytes(sorted(r.identity() for r in self.rows))
            ),
            "row_count": len(self.rows),
            "forbidden_predictors": list(FORBIDDEN_V2_PREDICTORS),
        }

    def identity(self) -> str:
        return sha256_hex(
            RELATIVE_DATASET_DOMAIN.encode("utf-8") + canonical_json_bytes(self.content())
        )


def build_relative_finalist_dataset(training_dataset: Any) -> RelativeFinalistDataset:
    """Slice the frozen TRAIN dataset down to the four-finalist advantage table.

    Derived from the accepted dataset only; a missing finalist cell is a refusal, because an
    advantage that cannot be measured must not be invented.
    """
    cells = {(r.dataset_id, r.config_hash): r for r in training_dataset.rows}
    chromosome = dict(training_dataset.cv_manifest.bam_chromosome)
    bams = sorted(chromosome)

    def utility(dataset_id: str, config_hash: str) -> float:
        try:
            row = cells[(dataset_id, config_hash)]
        except KeyError:
            raise RelativeFinalistError(
                f"TRAIN has no cell for ({dataset_id}, {config_hash}); the four-finalist slice is "
                "not dense and the advantage cannot be measured"
            ) from None
        return float(row.admitted_score) if row.outcome == "ADMITTED" else 0.0

    source_cells = tuple(
        cells[(b, c)].identity() for b in bams for c in FINALIST_DOMAIN if (b, c) in cells
    )
    safe = {b: utility(b, SAFE_BASELINE_CONFIG_HASH) for b in bams}
    rows = tuple(
        AdvantageRow(
            dataset_id=b,
            chromosome=chromosome[b],
            config_hash=c,
            safe_utility=safe[b],
            alternative_utility=utility(b, c),
            delta=utility(b, c) - safe[b],
        )
        for b in bams
        for c in ALTERNATIVE_FINALISTS
    )
    from minos_engine.models.relative_finalist_contract import (
        PARENT_CAMPAIGN_FREEZE_IDENTITY,
    )

    return RelativeFinalistDataset(
        parent_campaign_freeze_identity=PARENT_CAMPAIGN_FREEZE_IDENTITY,
        source_training_dataset_hash=training_dataset.identity(),
        relative_contract_hash=compute_relative_contract_hash(),
        finalist_domain_hash=compute_finalist_domain_hash(),
        feature_set_hash=training_dataset.feature_set_hash,
        feature_matrix_hash=training_dataset.feature_matrix_hash,
        config_encoding_identity=training_dataset.config_encoding_identity,
        bam_chromosome=chromosome,
        source_cell_identities=source_cells,
        safe_utility=safe,
        rows=rows,
    )
