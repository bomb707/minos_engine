"""L2-D ingestion contracts — the content-addressed input-integrity attestation and the
frozen canonical feature-values hash.

**Attestation.** The :class:`InputIntegrityAttestation` is a *content-addressed integrity
attestation* (not a digital signature): a deterministic statement, produced by the intake
package where the genomic files live, that the BAM/BAI/FASTA/FAI bytes, region, and
identity tuple match a registered identity — so Layer 2 never opens genomic files. The
consumer (this package) owns the contract; ``minos_engine.intake`` imports it from here.
The deterministic ``attestation_hash`` covers only content identity — never timestamps,
paths, URIs, or machine provenance.

**m5 admission rule** (owner ruling): ``MATCH`` admits; ``ABSENT`` admits with the
``integrity_degraded`` flag (recorded as ``BAM_SQ_M5_ABSENT``); ``MISMATCH`` always
rejects; any identity/hash mismatch or malformed attestation rejects.

**Canonical feature-values hash** (owner ruling): one frozen, domain-separated, versioned
algorithm::

    SHA256("minos:canonical-feature-values:v1\\n" + canonical_json(eligible_values))

where ``eligible_values`` maps every production-ELIGIBLE scalar field path (frozen L2-A
feature registry) to its raw JSON value from the profile document, validated per the
registry. Field inclusion is exactly the ELIGIBLE scalar set; keys are sorted by
``canonical_json``; numeric representation is the canonical-JSON shortest round-trip;
a missing or null ELIGIBLE field fails closed (no silent exclusion). Artifact URIs,
timestamps, paths, and partition labels never enter the hash.
"""

from __future__ import annotations

import hashlib
import re
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from minos_engine.common.canonical_json import canonical_json_str
from minos_engine.common.errors import ContractValidationError
from minos_engine.common.hashing import canonical_hash
from minos_engine.layer2.feature_registry import (
    production_eligible_fields,
    record_for,
    validate_scalar_value,
)

__all__ = [
    "ATTESTATION_SCHEMA_VERSION",
    "CANONICAL_FEATURE_VALUES_DOMAIN",
    "M5Status",
    "InputIntegrityAttestation",
    "admission_for_m5",
    "canonical_feature_values_hash",
    "extract_eligible_feature_values",
]

ATTESTATION_SCHEMA_VERSION = "input-integrity-attestation-v1"
CANONICAL_FEATURE_VALUES_DOMAIN = "minos:canonical-feature-values:v1"

_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_HEX32 = re.compile(r"^[0-9a-f]{32}$")


class M5Status(str, Enum):
    """Outcome of comparing the BAM ``@SQ:M5`` tag to the computed reference-contig MD5."""

    MATCH = "MATCH"
    ABSENT = "ABSENT"
    MISMATCH = "MISMATCH"


def admission_for_m5(status: M5Status) -> tuple[bool, bool]:
    """Return ``(admissible, integrity_degraded)`` for an m5 status (owner ruling)."""
    if status is M5Status.MATCH:
        return True, False
    if status is M5Status.ABSENT:
        return True, True  # admit, flagged BAM_SQ_M5_ABSENT / degraded integrity
    return False, False  # MISMATCH: always reject


class InputIntegrityAttestation(BaseModel):
    """Content-addressed statement binding input bytes to a registered identity."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: str = ATTESTATION_SCHEMA_VERSION
    generator: str = Field(min_length=1)  # e.g. "minos-engine intake attest-input"
    generator_version: str = Field(min_length=1)
    dataset_id: str = Field(min_length=1)
    round_id: str = Field(min_length=1, pattern=r"^[0-9a-f]+$")
    chromosome: str = Field(min_length=1)
    registry_snapshot_hash: str = Field(min_length=64, max_length=64)
    bam_sha256: str = Field(min_length=64, max_length=64)
    bai_sha256: str = Field(min_length=64, max_length=64)
    reference_sha256: str = Field(min_length=64, max_length=64)
    fai_sha256: str = Field(min_length=64, max_length=64)
    region_hash: str = Field(min_length=64, max_length=64)
    identity_tuple_hash: str = Field(min_length=64, max_length=64)
    bam_sq_m5: str | None = None  # the BAM header @SQ M5 tag, when present
    computed_reference_m5: str = Field(min_length=32, max_length=32)
    m5_status: M5Status
    attestation_hash: str = Field(default="")

    @model_validator(mode="after")
    def _validate(self) -> InputIntegrityAttestation:
        if self.schema_version != ATTESTATION_SCHEMA_VERSION:
            raise ValueError(f"unsupported attestation schema {self.schema_version!r}")
        for name in (
            "registry_snapshot_hash",
            "bam_sha256",
            "bai_sha256",
            "reference_sha256",
            "fai_sha256",
            "region_hash",
            "identity_tuple_hash",
        ):
            if not _HEX64.match(getattr(self, name)):
                raise ValueError(f"{name} must be 64 lowercase hex chars")
        if not _HEX32.match(self.computed_reference_m5):
            raise ValueError("computed_reference_m5 must be 32 lowercase hex chars")
        if self.bam_sq_m5 is not None and not _HEX32.match(self.bam_sq_m5):
            raise ValueError("bam_sq_m5 must be 32 lowercase hex chars when present")
        # m5_status must be CONSISTENT with the tag pair — never trusted independently.
        expected = (
            M5Status.ABSENT
            if self.bam_sq_m5 is None
            else (
                M5Status.MATCH
                if self.bam_sq_m5 == self.computed_reference_m5
                else M5Status.MISMATCH
            )
        )
        if self.m5_status is not expected:
            raise ValueError(
                f"m5_status {self.m5_status.value} inconsistent (expected {expected.value})"
            )
        # content-addressed: the hash is over content identity only, minus itself.
        computed = canonical_hash(self._content())
        if self.attestation_hash == "":
            object.__setattr__(self, "attestation_hash", computed)
        elif self.attestation_hash != computed:
            raise ValueError("attestation_hash does not match canonical content")
        return self

    def _content(self) -> dict[str, Any]:
        return self.model_dump(mode="json", exclude={"attestation_hash"})


def extract_eligible_feature_values(profile_document: dict[str, Any]) -> dict[str, Any]:
    """Extract ``{field_path: raw JSON value}`` for every production-ELIGIBLE scalar field
    whose source is the profile document (``bam-profile-v1``).

    Window-level ELIGIBLE features (source ``window-profile-v1``) are per-window rows in
    the parquet artifact — they are bound by the artifact byte hash, not by this scalar
    hash. Values are taken verbatim from the profile document (dotted-path traversal) and
    validated against the frozen L2-A feature registry. A missing or null ELIGIBLE field
    fails closed — completeness is a precondition of admission, never silently patched.
    """
    out: dict[str, Any] = {}
    for path in production_eligible_fields():
        record_source = record_for(path)
        if record_source is not None and record_source.source_schema != "bam-profile-v1":
            continue  # window-level features live in the hashed parquet artifact
        node: Any = profile_document
        for part in path.split("."):
            if not isinstance(node, dict) or part not in node:
                raise ContractValidationError(f"ELIGIBLE field missing from profile: {path}")
            node = node[part]
        if node is None:
            raise ContractValidationError(f"ELIGIBLE field is null in profile: {path}")
        record = record_for(path)
        if record is None:  # pragma: no cover - registry always has ELIGIBLE records
            raise ContractValidationError(f"no registry record for {path}")
        validate_scalar_value(record, node)  # type-checks; raises on violation
        out[path] = node
    return out


def canonical_feature_values_hash(eligible_values: dict[str, Any]) -> str:
    """The frozen, domain-separated canonical feature-values hash (owner ruling)."""
    payload = CANONICAL_FEATURE_VALUES_DOMAIN + "\n" + canonical_json_str(eligible_values)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
