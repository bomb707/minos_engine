"""Frozen L2-C dataset-registry and split-manifest contracts.

All models are ``frozen`` with ``extra="forbid"`` so an unknown or drifted field is a
hard failure. The canonical manifest is *purely deterministic content* — it contains
no timestamps, engine git sha, machine-specific absolute paths, or any truth/mutation
information — so regeneration from identical inputs is byte-identical. Provenance
(created_at, git sha) lives in the gate/report, never in the manifest.

Canonical serialization rules (single source: ``common/canonical_json``):
  * UTF-8, object keys sorted lexicographically, compact ``(",", ":")`` separators;
  * integers stay integers, no NaN/Infinity;
  * ``samples`` are ordered by ``dataset_id`` (a total, content-derived order);
  * ``manifest_hash`` is SHA-256 over the canonical content with ``manifest_hash``
    itself excluded (no self-reference).
"""

from __future__ import annotations

import re

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from minos_engine.common.hashing import canonical_hash

from .policy import (
    PARTITIONS,
    SALT,
    SPLIT_ALGORITHM,
    SPLIT_POLICY_VERSION,
    SUPPORTED_CHROMOSOMES,
)

__all__ = [
    "SampleIdentity",
    "DatasetSplitManifest",
    "LocalInputInventory",
    "LocalInputEntry",
    "region_hash_for",
    "MANIFEST_SCHEMA_VERSION",
    "INVENTORY_SCHEMA_VERSION",
]

MANIFEST_SCHEMA_VERSION = "layer2-dataset-split-v1"
INVENTORY_SCHEMA_VERSION = "layer2-local-input-inventory-v1"

_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_ZERO_BASED = "zero_based_half_open"


def _sha256(v: str) -> str:
    if not _HEX64.match(v):
        raise ValueError("must be 64 lowercase hex characters (SHA-256)")
    return v


def region_hash_for(
    contig: str, start0: int, end0_exclusive: int, coordinate_system: str = _ZERO_BASED
) -> str:
    """Canonical region hash over the normalized region structure."""
    return canonical_hash(
        {
            "contig": contig,
            "start0": start0,
            "end0_exclusive": end0_exclusive,
            "length_bp": end0_exclusive - start0,
            "coordinate_system": coordinate_system,
        }
    )


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class SampleIdentity(_Frozen):
    """Immutable identity + partition assignment for one complete sample.

    No absolute paths and no truth/mutation information are present. ``dataset_id`` is
    a stable, path-independent identifier derived from ``chromosome`` and ``round_id``.
    ``identity_tuple_hash`` binds the exact ``(bam, bai, reference, fai, region)``
    input identity (same definition as the profiles identity tuple).
    """

    dataset_id: str = Field(min_length=1)
    round_id: str = Field(min_length=1)
    chromosome: str
    region_source: str = Field(min_length=1)
    region_contig: str = Field(min_length=1)
    region_start0: int = Field(ge=0)
    region_end0_exclusive: int = Field(gt=0)
    region_length_bp: int = Field(gt=0)
    region_coordinate_system: str = _ZERO_BASED
    region_hash: str
    bam_sha256: str
    bai_sha256: str
    reference_sha256: str
    fai_sha256: str
    bam_size_bytes: int = Field(ge=0)
    parameter_space_hash: str
    feature_registry_hash: str
    split_algorithm_version: str
    split_salt: str
    allocation_digest: str
    identity_tuple_hash: str = ""
    partition: str
    sort_order: int = Field(ge=0)

    @field_validator(
        "region_hash",
        "bam_sha256",
        "bai_sha256",
        "reference_sha256",
        "fai_sha256",
        "parameter_space_hash",
        "feature_registry_hash",
        "allocation_digest",
    )
    @classmethod
    def _v_sha(cls, v: str) -> str:
        return _sha256(v)

    @field_validator("chromosome")
    @classmethod
    def _v_chrom(cls, v: str) -> str:
        if v not in SUPPORTED_CHROMOSOMES:
            raise ValueError(f"unsupported chromosome {v!r}")
        return v

    @field_validator("partition")
    @classmethod
    def _v_part(cls, v: str) -> str:
        if v not in PARTITIONS:
            raise ValueError(f"invalid partition {v!r}")
        return v

    @model_validator(mode="after")
    def _bind(self) -> SampleIdentity:
        if self.region_length_bp != self.region_end0_exclusive - self.region_start0:
            raise ValueError("region_length_bp inconsistent with region bounds")
        if self.region_contig != self.chromosome:
            raise ValueError("region_contig must equal chromosome")
        expected_region = region_hash_for(
            self.region_contig,
            self.region_start0,
            self.region_end0_exclusive,
            self.region_coordinate_system,
        )
        if self.region_hash != expected_region:
            raise ValueError("region_hash does not match canonical region")
        expected_tuple = canonical_hash(
            {
                "bam_sha256": self.bam_sha256,
                "bai_sha256": self.bai_sha256,
                "reference_sha256": self.reference_sha256,
                "fai_sha256": self.fai_sha256,
                "region_hash": self.region_hash,
            }
        )
        if self.identity_tuple_hash == "":
            object.__setattr__(self, "identity_tuple_hash", expected_tuple)
        elif self.identity_tuple_hash != expected_tuple:
            raise ValueError("identity_tuple_hash does not match canonical identity tuple")
        return self

    def identity_content(self) -> dict[str, object]:
        """Identity-only projection (excludes partition/sort_order) for registry hash."""
        return {
            "dataset_id": self.dataset_id,
            "round_id": self.round_id,
            "chromosome": self.chromosome,
            "region_source": self.region_source,
            "region_contig": self.region_contig,
            "region_start0": self.region_start0,
            "region_end0_exclusive": self.region_end0_exclusive,
            "region_length_bp": self.region_length_bp,
            "region_coordinate_system": self.region_coordinate_system,
            "region_hash": self.region_hash,
            "bam_sha256": self.bam_sha256,
            "bai_sha256": self.bai_sha256,
            "reference_sha256": self.reference_sha256,
            "fai_sha256": self.fai_sha256,
            "bam_size_bytes": self.bam_size_bytes,
            "parameter_space_hash": self.parameter_space_hash,
            "feature_registry_hash": self.feature_registry_hash,
            "identity_tuple_hash": self.identity_tuple_hash,
        }

    def canonical_record(self) -> dict[str, object]:
        """Full canonical sample record (identity + allocation)."""
        rec = self.identity_content()
        rec.update(
            {
                "split_algorithm_version": self.split_algorithm_version,
                "split_salt": self.split_salt,
                "allocation_digest": self.allocation_digest,
                "partition": self.partition,
                "sort_order": self.sort_order,
            }
        )
        return rec


def _registry_hash(samples: tuple[SampleIdentity, ...]) -> str:
    ordered = sorted((s.identity_content() for s in samples), key=lambda r: str(r["dataset_id"]))
    return canonical_hash(ordered)


class DatasetSplitManifest(_Frozen):
    """The frozen, immutable dataset registry + split allocation manifest."""

    schema_version: str = MANIFEST_SCHEMA_VERSION
    split_algorithm: str = SPLIT_ALGORITHM
    salt: str = SALT
    split_policy_version: str = SPLIT_POLICY_VERSION
    split_policy_hash: str
    counts: dict[str, int]
    per_chromosome: dict[str, dict[str, int]]
    parameter_space_hash: str
    feature_registry_hash: str
    dataset_registry_hash: str = ""
    samples: tuple[SampleIdentity, ...]
    manifest_hash: str = ""

    @field_validator("split_policy_hash", "parameter_space_hash", "feature_registry_hash")
    @classmethod
    def _v_sha(cls, v: str) -> str:
        return _sha256(v)

    def _content_for_hash(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "split_algorithm": self.split_algorithm,
            "salt": self.salt,
            "split_policy_version": self.split_policy_version,
            "split_policy_hash": self.split_policy_hash,
            "counts": dict(sorted(self.counts.items())),
            "per_chromosome": {
                c: dict(sorted(self.per_chromosome[c].items())) for c in sorted(self.per_chromosome)
            },
            "parameter_space_hash": self.parameter_space_hash,
            "feature_registry_hash": self.feature_registry_hash,
            "dataset_registry_hash": self.dataset_registry_hash,
            "samples": sorted(
                (s.canonical_record() for s in self.samples),
                key=lambda r: str(r["dataset_id"]),
            ),
        }

    def compute_manifest_hash(self) -> str:
        return canonical_hash(self._content_for_hash())

    def to_canonical(self) -> dict[str, object]:
        """The complete canonical manifest object (with ``manifest_hash`` included)."""
        content = self._content_for_hash()
        content["manifest_hash"] = self.manifest_hash
        return content

    @model_validator(mode="after")
    def _bind(self) -> DatasetSplitManifest:
        if len(self.samples) == 0:
            raise ValueError("manifest must contain samples")
        # dataset_id / round_id uniqueness
        ds_ids = [s.dataset_id for s in self.samples]
        if len(set(ds_ids)) != len(ds_ids):
            raise ValueError("duplicate dataset_id in manifest")
        # per-chromosome round_id uniqueness
        seen: dict[str, set[str]] = {}
        for s in self.samples:
            if s.round_id in seen.setdefault(s.chromosome, set()):
                raise ValueError(f"duplicate round_id {s.round_id} within {s.chromosome}")
            seen[s.chromosome].add(s.round_id)
        # partitions disjoint & exactly one per dataset is structural (one row each).
        expected_registry = _registry_hash(self.samples)
        if self.dataset_registry_hash == "":
            object.__setattr__(self, "dataset_registry_hash", expected_registry)
        elif self.dataset_registry_hash != expected_registry:
            raise ValueError("dataset_registry_hash does not match canonical samples")
        expected_manifest = self.compute_manifest_hash()
        if self.manifest_hash == "":
            object.__setattr__(self, "manifest_hash", expected_manifest)
        elif self.manifest_hash != expected_manifest:
            raise ValueError("manifest_hash does not match canonical content")
        return self


class LocalInputEntry(_Frozen):
    """A resolver row: dataset identity → dataset-root-relative file paths.

    Paths are POSIX and *relative to the dataset root* (never absolute), so the
    inventory reproduces byte-identically on any machine with the same layout.
    """

    dataset_id: str = Field(min_length=1)
    round_id: str = Field(min_length=1)
    chromosome: str
    bam_relpath: str = Field(min_length=1)
    bai_relpath: str = Field(min_length=1)
    reference_relpath: str = Field(min_length=1)
    fai_relpath: str = Field(min_length=1)

    @field_validator("bam_relpath", "bai_relpath", "reference_relpath", "fai_relpath")
    @classmethod
    def _v_rel(cls, v: str) -> str:
        if v.startswith("/") or ".." in v.split("/"):
            raise ValueError("inventory paths must be dataset-root-relative (no abs/traversal)")
        return v


class LocalInputInventory(_Frozen):
    """Noncanonical local resolver, separate from the canonical manifest.

    It carries operational (relative) paths and its own reproducible hash. The
    absolute dataset root is supplied at resolution time and is never stored here.
    """

    schema_version: str = INVENTORY_SCHEMA_VERSION
    entries: tuple[LocalInputEntry, ...]
    inventory_hash: str = ""

    def _content_for_hash(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "entries": sorted(
                (e.model_dump(mode="json") for e in self.entries),
                key=lambda r: str(r["dataset_id"]),
            ),
        }

    def compute_inventory_hash(self) -> str:
        return canonical_hash(self._content_for_hash())

    def to_canonical(self) -> dict[str, object]:
        content = self._content_for_hash()
        content["inventory_hash"] = self.inventory_hash
        return content

    @model_validator(mode="after")
    def _bind(self) -> LocalInputInventory:
        ds_ids = [e.dataset_id for e in self.entries]
        if len(set(ds_ids)) != len(ds_ids):
            raise ValueError("duplicate dataset_id in inventory")
        expected = self.compute_inventory_hash()
        if self.inventory_hash == "":
            object.__setattr__(self, "inventory_hash", expected)
        elif self.inventory_hash != expected:
            raise ValueError("inventory_hash does not match canonical content")
        return self
