"""E2 pure extraction + verification: exact profile bytes → FeatureVector → logical matrix.

Pure functions only — no migration, no PostgreSQL, no grants, no artifact registration,
no Parquet storage, no gates (E2 scope). The extraction boundary receives the EXACT
profile JSON bytes plus the expected snapshot-member metadata: it recomputes
``profile_sha256`` from those bytes (a caller-supplied hash is never accepted as a
substitute), UTF-8 decodes and JSON-parses the same bytes rejecting duplicate keys,
validates the document against the accepted ``bam-profile-v1`` schema, binds every
available profile/snapshot identity field, extracts exactly the authoritative 129
BAM ELIGIBLE paths in canonical feature-manifest order (the 12 window-profile fields
are bound by the parquet artifact hash and are ignored completely here), and enforces
the four-way feature integrity binding before a :class:`FeatureVector` is constructed.

Snapshot trust is INDEPENDENT, not asserted: :class:`FrozenSnapshot` recomputes its own
``snapshot_hash`` with the accepted PROFILE-SNAPSHOT-FROZEN freeze formula and rejects
any supplied hash that differs; :func:`load_member_manifest` derives a snapshot ONLY
from exact committed member-manifest bytes — recomputing ``member_manifest_hash`` from
the parsed canonical content (which binds ``profile_sha256`` and every other provenance
field), re-verifying ``snapshot_hash``, and binding the accepted profiler identity and
the manifest's registry snapshot. Matrix assembly consumes partition assignments
VERBATIM: no percentages, no allocation, and test members are never read —
``partition="test"`` is rejected before any member payload is inspected. Counts derive
from exact selected membership; ``column_count`` stays the frozen 129.

Verification recomputes rather than trusts and never repairs: :func:`verify_matrix`
(logical) re-derives membership, ordering, counts, bindings, vector/matrix hashes AND
the canonical feature-values hash from each vector's own values;
:func:`verify_matrix_payload` starts from verified member-manifest bytes and re-extracts
every member's exact profile bytes.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Sequence
from typing import Any, Literal, cast

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from minos_engine.common.errors import AdmissionRejectedError, ContractValidationError
from minos_engine.common.hashing import canonical_hash
from minos_engine.layer2.feature_registry import REGISTRY_HASH
from minos_engine.layer2.ingest.contracts import (
    canonical_feature_values_hash,
    extract_eligible_feature_values,
)
from minos_engine.layer2.ingest.validation import l1_feature_values_hash_from_document
from minos_engine.layer2.prerequisites import PROFILER_CONFIG_HASH, PROFILER_VERSION
from minos_engine.schema_registry import validate_against

from .contracts import (
    AUTHORITATIVE_COLUMNS,
    EXPECTED_COLUMN_COUNT,
    FROZEN_FEATURE_SET_HASH,
    FeatureMatrix,
    FeatureVector,
    MatrixMember,
    Partition,
    canonical_feature_set,
    matrix_hash,
    validate_value_for_kind,
    vector_hash,
)
from .errors import (
    FeatureExtractionError,
    FeatureValuesHashMismatchError,
    ForbiddenPartitionError,
    InvalidMemberManifestError,
    InvalidProfileDocumentError,
    MatrixAssemblyError,
    MemberManifestHashMismatchError,
    MissingFeatureError,
    ProfileArtifactHashMismatchError,
    ProfilerIdentityMismatchError,
    RegistrySnapshotMismatchError,
    SnapshotHashMismatchError,
    SnapshotIdentityMismatchError,
)

__all__ = [
    "PROFILE_SCHEMA_NAME",
    "MEMBER_MANIFEST_SCHEMA_VERSION",
    "MATRIX_PARTITIONS",
    "SUPPORTED_CHROMOSOMES",
    "SnapshotMember",
    "FrozenSnapshot",
    "ManifestMember",
    "MemberManifestDocument",
    "ExtractionResult",
    "MatrixBuild",
    "VerificationResult",
    "PayloadProvider",
    "load_member_manifest",
    "extract_profile_features",
    "build_feature_vector",
    "assemble_matrix",
    "build_partition_matrix",
    "verify_matrix",
    "verify_matrix_payload",
]

PROFILE_SCHEMA_NAME = "bam-profile-v1"
MEMBER_MANIFEST_SCHEMA_VERSION = "profile-snapshot-members-v1"

#: The only partitions a matrix may ever be built for. Test is structurally forbidden.
MATRIX_PARTITIONS: tuple[str, ...] = ("train", "validation")

#: The dataset registry supports exactly these chromosome identities.
SUPPORTED_CHROMOSOMES: tuple[str, ...] = ("chr18", "chr19", "chr20", "chr21", "chr22")

MemberPartition = Literal["train", "validation", "test"]
Chromosome = Literal["chr18", "chr19", "chr20", "chr21", "chr22"]

_HEX64 = r"^[0-9a-f]{64}$"


class SnapshotMember(BaseModel):
    """One frozen snapshot member: identity + partition assignment, consumed VERBATIM.

    The core fields participate in the snapshot freeze formula; the provenance fields
    (present whenever the member was derived from a verified member manifest) bind the
    exact artifact bytes and profiler identity consumed later.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    dataset_id: str = Field(min_length=1)
    profile_id: str = Field(min_length=1)
    partition: MemberPartition
    content_hash: str = Field(pattern=_HEX64)
    feature_values_hash: str = Field(pattern=_HEX64)
    profile_sha256: str = Field(pattern=_HEX64)
    l1_feature_values_hash: str | None = Field(default=None, pattern=_HEX64)
    chromosome: Chromosome | None = None
    profile_status: Literal["COMPLETE"] = "COMPLETE"
    # provenance (from the verified member manifest)
    round_id: str | None = Field(default=None, pattern=r"^[0-9a-f]+$")
    identity_tuple_hash: str | None = Field(default=None, pattern=_HEX64)
    registry_snapshot_hash: str | None = Field(default=None, pattern=_HEX64)
    profile_manifest_sha256: str | None = Field(default=None, pattern=_HEX64)
    windows_sha256: str | None = Field(default=None, pattern=_HEX64)
    attestation_hash: str | None = Field(default=None, pattern=_HEX64)
    profiler_version: str | None = None
    profiler_config_hash: str | None = Field(default=None, pattern=_HEX64)
    integrity_degraded: bool | None = None
    m5_status: Literal["MATCH", "ABSENT"] | None = None


def _freeze_formula_hash(
    epoch: int,
    split_manifest_hash: str,
    registry_snapshot_hash: str,
    members: Sequence[SnapshotMember],
) -> str:
    """The accepted PROFILE-SNAPSHOT-FROZEN freeze formula (storage layer, verbatim)."""
    return canonical_hash(
        {
            "epoch": epoch,
            "split_manifest_hash": split_manifest_hash,
            "registry_snapshot_hash": registry_snapshot_hash,
            "members": [
                {
                    "dataset_id": m.dataset_id,
                    "partition": m.partition,
                    "content_hash": m.content_hash,
                    "feature_values_hash": m.feature_values_hash,
                }
                for m in sorted(members, key=lambda m: m.dataset_id)
            ],
        }
    )


class FrozenSnapshot(BaseModel):
    """An explicit frozen snapshot representation, INDEPENDENTLY self-binding.

    ``snapshot_hash`` is always recomputed with the accepted freeze formula from
    ``epoch`` + ``split_manifest_hash`` + ``registry_snapshot_hash`` + the ordered core
    member fields; a supplied hash that differs is rejected (typed). Partition
    assignments are consumed verbatim — this module NEVER applies percentages or
    performs split allocation.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    epoch: int = Field(ge=1)
    split_manifest_hash: str = Field(pattern=_HEX64)
    registry_snapshot_hash: str = Field(pattern=_HEX64)
    members: tuple[SnapshotMember, ...] = Field(min_length=1)
    snapshot_hash: str = Field(default="")

    @model_validator(mode="after")
    def _bind(self) -> FrozenSnapshot:
        ids = [m.dataset_id for m in self.members]
        if len(set(ids)) != len(ids):
            raise ValueError("duplicate dataset_id in snapshot members")
        pids = [m.profile_id for m in self.members]
        if len(set(pids)) != len(pids):
            raise ValueError("duplicate profile_id in snapshot members")
        expected = _freeze_formula_hash(
            self.epoch, self.split_manifest_hash, self.registry_snapshot_hash, self.members
        )
        if self.snapshot_hash == "":
            object.__setattr__(self, "snapshot_hash", expected)
        elif self.snapshot_hash != expected:
            raise SnapshotHashMismatchError(
                "supplied snapshot_hash does not equal the hash recomputed with the "
                "accepted freeze formula"
            )
        return self

    def members_for(self, partition: str) -> tuple[SnapshotMember, ...]:
        """All members of a train/validation partition, ordered by dataset_id.

        Test membership is never enumerated through this API (fail-closed).
        """
        _require_matrix_partition(partition)
        return tuple(
            sorted(
                (m for m in self.members if m.partition == partition),
                key=lambda m: m.dataset_id,
            )
        )


class ManifestMember(BaseModel):
    """One member row of the committed profile_snapshot_epoch_members manifest —
    every provenance field is REQUIRED and strictly validated."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    dataset_id: str = Field(min_length=1)
    round_id: str = Field(pattern=r"^[0-9a-f]+$")
    chromosome: Chromosome
    partition: MemberPartition
    identity_tuple_hash: str = Field(pattern=_HEX64)
    content_hash: str = Field(pattern=_HEX64)
    feature_values_hash: str = Field(pattern=_HEX64)
    l1_feature_values_hash: str = Field(pattern=_HEX64)
    profile_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    profile_sha256: str = Field(pattern=_HEX64)
    profile_manifest_sha256: str = Field(pattern=_HEX64)
    windows_sha256: str = Field(pattern=_HEX64)
    attestation_hash: str = Field(pattern=_HEX64)
    m5_status: Literal["MATCH", "ABSENT"]  # MISMATCH never enters an accepted snapshot
    integrity_degraded: bool
    profile_status: Literal["COMPLETE"]
    profiler_version: str = Field(min_length=1)
    profiler_config_hash: str = Field(pattern=_HEX64)
    registry_snapshot_hash: str = Field(pattern=_HEX64)


class MemberManifestDocument(BaseModel):
    """The committed profile-snapshot-members-v1 manifest, strictly modeled."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["profile-snapshot-members-v1"]
    epoch: int = Field(ge=1)
    member_count: int = Field(ge=1)
    member_manifest_hash: str = Field(pattern=_HEX64)
    members: tuple[ManifestMember, ...] = Field(min_length=1)
    registry_snapshot_hash: str = Field(pattern=_HEX64)
    snapshot_hash: str = Field(pattern=_HEX64)
    split_manifest_hash: str = Field(pattern=_HEX64)

    @model_validator(mode="after")
    def _bind(self) -> MemberManifestDocument:
        if self.member_count != len(self.members):
            raise ValueError("member_count does not match members")
        ids = [m.dataset_id for m in self.members]
        if len(set(ids)) != len(ids):
            raise ValueError("duplicate dataset_id in manifest members")
        return self


class ExtractionResult(BaseModel):
    """The ONLY extraction output: ordered values + recomputed canonical hash.

    The raw profile document is deliberately not exposed here or anywhere downstream.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    values: tuple[float, ...]
    feature_values_hash: str = Field(pattern=_HEX64)

    @model_validator(mode="after")
    def _bind(self) -> ExtractionResult:
        if len(self.values) != EXPECTED_COLUMN_COUNT:
            raise ValueError(f"extraction must yield exactly {EXPECTED_COLUMN_COUNT} values")
        return self


class MatrixBuild(BaseModel):
    """A built logical matrix plus its constituent vectors (dataset_id order)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    matrix: FeatureMatrix
    vectors: tuple[FeatureVector, ...]


class VerificationResult(BaseModel):
    """Named, recomputed verification checks. Never repairs an invalid matrix."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    checks: dict[str, bool]

    @property
    def ok(self) -> bool:
        return bool(self.checks) and all(self.checks.values())

    def failed(self) -> tuple[str, ...]:
        return tuple(sorted(name for name, passed in self.checks.items() if not passed))


PayloadProvider = Callable[[SnapshotMember], bytes]


def _require_matrix_partition(partition: str) -> Partition:
    if partition not in MATRIX_PARTITIONS:
        raise ForbiddenPartitionError(
            f"partition {partition!r} is forbidden: matrices exist only for "
            f"{MATRIX_PARTITIONS} (test members are sealed and never read)"
        )
    return cast(Partition, partition)


def _no_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in pairs:
        if key in out:
            raise InvalidProfileDocumentError(f"duplicate JSON key: {key}")
        out[key] = value
    return out


# --------------------------------------------------------------------------- #
# verified member-manifest boundary (snapshot provenance)
# --------------------------------------------------------------------------- #
def load_member_manifest(
    manifest_bytes: bytes,
    *,
    expected_member_manifest_hash: str | None = None,
    expected_registry_snapshot_hash: str | None = None,
) -> FrozenSnapshot:
    """Derive a FrozenSnapshot ONLY from exact, verified member-manifest bytes.

    Recomputes ``member_manifest_hash`` from the exact parsed canonical content (binding
    ``profile_sha256`` and every other provenance field — a caller cannot replace any of
    them while keeping a trusted hash), independently re-verifies ``snapshot_hash`` with
    the accepted freeze formula, requires every member to carry the manifest's registry
    snapshot, and binds the accepted profiler identity pinned in prerequisites.
    """
    try:
        text = manifest_bytes.decode("utf-8")
        raw = json.loads(text, object_pairs_hook=_no_duplicate_keys)
    except InvalidProfileDocumentError as exc:
        raise InvalidMemberManifestError(str(exc)) from exc
    except (UnicodeDecodeError, ValueError) as exc:
        raise InvalidMemberManifestError(f"invalid member-manifest bytes: {exc}") from exc
    if not isinstance(raw, dict):
        raise InvalidMemberManifestError("member manifest is not a JSON object")
    try:
        document = MemberManifestDocument.model_validate(raw)
    except ValidationError as exc:
        raise InvalidMemberManifestError(f"member manifest is structurally invalid: {exc}") from exc

    # the canonical hash is recomputed from the EXACT parsed content, never trusted.
    content = {k: v for k, v in raw.items() if k != "member_manifest_hash"}
    recomputed_manifest_hash = canonical_hash(content)
    if recomputed_manifest_hash != document.member_manifest_hash:
        raise MemberManifestHashMismatchError(
            "recomputed member_manifest_hash does not match the declared value"
        )
    if (
        expected_member_manifest_hash is not None
        and recomputed_manifest_hash != expected_member_manifest_hash
    ):
        raise MemberManifestHashMismatchError(
            "recomputed member_manifest_hash does not match the expected accepted value"
        )

    members = tuple(
        SnapshotMember(
            dataset_id=m.dataset_id,
            profile_id=m.profile_id,
            partition=m.partition,
            content_hash=m.content_hash,
            feature_values_hash=m.feature_values_hash,
            profile_sha256=m.profile_sha256,
            l1_feature_values_hash=m.l1_feature_values_hash,
            chromosome=m.chromosome,
            profile_status=m.profile_status,
            round_id=m.round_id,
            identity_tuple_hash=m.identity_tuple_hash,
            registry_snapshot_hash=m.registry_snapshot_hash,
            profile_manifest_sha256=m.profile_manifest_sha256,
            windows_sha256=m.windows_sha256,
            attestation_hash=m.attestation_hash,
            profiler_version=m.profiler_version,
            profiler_config_hash=m.profiler_config_hash,
            integrity_degraded=m.integrity_degraded,
            m5_status=m.m5_status,
        )
        for m in document.members
    )

    # snapshot_hash is verified INDEPENDENTLY with the accepted freeze formula.
    recomputed_snapshot_hash = _freeze_formula_hash(
        document.epoch, document.split_manifest_hash, document.registry_snapshot_hash, members
    )
    if recomputed_snapshot_hash != document.snapshot_hash:
        raise SnapshotHashMismatchError(
            "manifest snapshot_hash does not equal the freeze-formula recompute"
        )

    for m in document.members:
        if m.registry_snapshot_hash != document.registry_snapshot_hash:
            raise RegistrySnapshotMismatchError(
                f"{m.dataset_id}: member registry_snapshot_hash differs from the manifest"
            )
        if m.profiler_version != PROFILER_VERSION or m.profiler_config_hash != PROFILER_CONFIG_HASH:
            raise ProfilerIdentityMismatchError(
                f"{m.dataset_id}: profiler identity is not the accepted "
                f"{PROFILER_VERSION}/{PROFILER_CONFIG_HASH[:12]}…"
            )
    if (
        expected_registry_snapshot_hash is not None
        and document.registry_snapshot_hash != expected_registry_snapshot_hash
    ):
        raise RegistrySnapshotMismatchError(
            "manifest registry_snapshot_hash does not match the expected pinned value"
        )

    return FrozenSnapshot(
        epoch=document.epoch,
        split_manifest_hash=document.split_manifest_hash,
        registry_snapshot_hash=document.registry_snapshot_hash,
        members=members,
        snapshot_hash=document.snapshot_hash,
    )


# --------------------------------------------------------------------------- #
# exact-byte extraction boundary
# --------------------------------------------------------------------------- #
def extract_profile_features(profile_bytes: bytes, member: SnapshotMember) -> ExtractionResult:
    """Exact-byte extraction boundary: hash the RECEIVED bytes (never a caller-supplied
    hash), decode/parse those same bytes, validate against the accepted schema, bind
    identity, and extract exactly the authoritative 129 BAM ELIGIBLE paths in canonical
    feature-manifest order."""
    received_sha256 = hashlib.sha256(profile_bytes).hexdigest()
    if received_sha256 != member.profile_sha256:
        raise ProfileArtifactHashMismatchError(
            f"{member.dataset_id}: profile bytes hash {received_sha256} does not match "
            f"snapshot member profile_sha256 {member.profile_sha256}"
        )
    try:
        text = profile_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise InvalidProfileDocumentError(f"{member.dataset_id}: not UTF-8: {exc}") from exc
    try:
        document = json.loads(text, object_pairs_hook=_no_duplicate_keys)
    except InvalidProfileDocumentError:
        raise
    except ValueError as exc:
        raise InvalidProfileDocumentError(f"{member.dataset_id}: invalid JSON: {exc}") from exc
    if not isinstance(document, dict):
        raise InvalidProfileDocumentError(f"{member.dataset_id}: profile is not a JSON object")
    try:
        validate_against(PROFILE_SCHEMA_NAME, document)
    except ContractValidationError as exc:
        raise InvalidProfileDocumentError(f"{member.dataset_id}: {exc}") from exc
    # identity bindings: every available profile/snapshot identity field.
    if document.get("profile_id") != member.profile_id:
        raise SnapshotIdentityMismatchError(
            f"{member.dataset_id}: profile_id {document.get('profile_id')!r} does not "
            f"match snapshot member {member.profile_id!r}"
        )
    if document.get("status") != member.profile_status:
        raise SnapshotIdentityMismatchError(
            f"{member.dataset_id}: profile status {document.get('status')!r} does not "
            f"match snapshot member {member.profile_status!r}"
        )
    if member.l1_feature_values_hash is not None:
        try:
            recomputed_l1 = l1_feature_values_hash_from_document(document)
        except AdmissionRejectedError as exc:
            raise SnapshotIdentityMismatchError(f"{member.dataset_id}: {exc}") from exc
        if recomputed_l1 != member.l1_feature_values_hash:
            raise SnapshotIdentityMismatchError(
                f"{member.dataset_id}: recomputed L1 feature_values_hash does not match "
                "the snapshot member binding"
            )
    # authoritative extraction: exactly the 129 BAM ELIGIBLE paths (window fields are
    # bound by the parquet artifact hash and never enter this scalar boundary).
    try:
        eligible = extract_eligible_feature_values(document)
    except (ContractValidationError, ValueError) as exc:
        raise MissingFeatureError(f"{member.dataset_id}: {exc}") from exc
    if set(eligible) != set(AUTHORITATIVE_COLUMNS):
        raise MissingFeatureError(
            f"{member.dataset_id}: extracted paths do not equal the authoritative set"
        )
    recomputed = canonical_feature_values_hash(eligible)
    declared = document.get("feature_values_hash")
    if declared is not None and declared != recomputed:
        raise FeatureValuesHashMismatchError(
            f"{member.dataset_id}: profile document's declared feature hash does not "
            "match the recomputed canonical feature-values hash"
        )
    ordered: list[float] = []
    for column in canonical_feature_set().columns:
        try:
            ordered.append(
                validate_value_for_kind(column.value_kind, eligible[column.path], column.path)
            )
        except ValueError as exc:
            raise MissingFeatureError(f"{member.dataset_id}: {exc}") from exc
    return ExtractionResult(values=tuple(ordered), feature_values_hash=recomputed)


def build_feature_vector(
    profile_bytes: bytes,
    member: SnapshotMember,
    *,
    epoch: int,
    snapshot_hash: str,
) -> FeatureVector:
    """Four-way feature integrity binding: construct a FeatureVector only after every
    check passes. Test members are rejected BEFORE their bytes are inspected."""
    if member.partition not in MATRIX_PARTITIONS:
        raise ForbiddenPartitionError(
            f"{member.dataset_id}: partition {member.partition!r} is forbidden — "
            "no vector is ever constructed for a test member"
        )
    result = extract_profile_features(profile_bytes, member)
    if result.feature_values_hash != member.feature_values_hash:
        raise FeatureValuesHashMismatchError(
            f"{member.dataset_id}: recomputed canonical feature-values hash does not "
            "match the snapshot member feature_values_hash"
        )
    return FeatureVector(
        epoch=epoch,
        dataset_id=member.dataset_id,
        profile_id=member.profile_id,
        content_hash=member.content_hash,
        feature_values_hash=member.feature_values_hash,
        partition=cast(Partition, member.partition),
        snapshot_hash=snapshot_hash,
        registry_hash=REGISTRY_HASH,
        feature_set_hash=FROZEN_FEATURE_SET_HASH,
        value_count=EXPECTED_COLUMN_COUNT,
        values=result.values,
    )


def _check_vector_bindings(
    vector: FeatureVector, member: SnapshotMember, snapshot: FrozenSnapshot, partition: str
) -> None:
    if vector.partition != partition:
        raise SnapshotIdentityMismatchError(
            f"{vector.dataset_id}: vector partition {vector.partition!r} does not match "
            f"the requested partition {partition!r}"
        )
    if vector.epoch != snapshot.epoch or vector.snapshot_hash != snapshot.snapshot_hash:
        raise SnapshotIdentityMismatchError(
            f"{vector.dataset_id}: vector epoch/snapshot binding does not match the snapshot"
        )
    if vector.registry_hash != REGISTRY_HASH:
        raise SnapshotIdentityMismatchError(
            f"{vector.dataset_id}: vector registry_hash is not the accepted registry"
        )
    if vector.feature_set_hash != FROZEN_FEATURE_SET_HASH:
        raise SnapshotIdentityMismatchError(
            f"{vector.dataset_id}: vector feature_set_hash is not the frozen feature set"
        )
    if vector.profile_id != member.profile_id:
        raise SnapshotIdentityMismatchError(f"{vector.dataset_id}: profile_id binding mismatch")
    if vector.content_hash != member.content_hash:
        raise SnapshotIdentityMismatchError(f"{vector.dataset_id}: content_hash binding mismatch")
    if vector.feature_values_hash != member.feature_values_hash:
        raise FeatureValuesHashMismatchError(
            f"{vector.dataset_id}: feature_values_hash binding mismatch"
        )


def _recompute_feature_values_hash_from_vector(vector: FeatureVector) -> str:
    """Recompute the canonical feature-values hash from the vector's OWN values:
    exactly 129 values, each validated against its canonical column kind, keyed by
    canonical column path in manifest order."""
    columns = canonical_feature_set().columns
    if len(vector.values) != EXPECTED_COLUMN_COUNT:
        raise MissingFeatureError(
            f"{vector.dataset_id}: vector does not carry exactly {EXPECTED_COLUMN_COUNT} values"
        )
    payload: dict[str, Any] = {}
    for column, value in zip(columns, vector.values, strict=True):
        try:
            validate_value_for_kind(column.value_kind, value, column.path)
        except ValueError as exc:
            raise MissingFeatureError(f"{vector.dataset_id}: {exc}") from exc
        payload[column.path] = value
    return canonical_feature_values_hash(payload)


def assemble_matrix(
    snapshot: FrozenSnapshot, partition: str, vectors: Sequence[FeatureVector]
) -> FeatureMatrix:
    """Snapshot-derived matrix assembly: exactly all snapshot members of the requested
    train/validation partition, verbatim, no percentages, no test access."""
    checked = _require_matrix_partition(partition)
    expected = {m.dataset_id: m for m in snapshot.members_for(checked)}
    ids = [v.dataset_id for v in vectors]
    if len(set(ids)) != len(ids):
        duplicates = sorted({i for i in ids if ids.count(i) > 1})
        raise MatrixAssemblyError(f"duplicate vectors for dataset_ids: {duplicates}")
    missing = sorted(set(expected) - set(ids))
    if missing:
        raise MatrixAssemblyError(f"missing vectors for snapshot members: {missing}")
    extra = sorted(set(ids) - set(expected))
    if extra:
        raise MatrixAssemblyError(f"extra vectors not in snapshot partition: {extra}")
    for vector in vectors:
        _check_vector_bindings(vector, expected[vector.dataset_id], snapshot, checked)
    # order by dataset_id BEFORE constructing the rejection-ordered contract.
    ordered = sorted(vectors, key=lambda v: v.dataset_id)
    return FeatureMatrix(
        epoch=snapshot.epoch,
        snapshot_hash=snapshot.snapshot_hash,
        partition=checked,
        registry_hash=REGISTRY_HASH,
        feature_set_hash=FROZEN_FEATURE_SET_HASH,
        row_count=len(ordered),
        column_count=EXPECTED_COLUMN_COUNT,
        members=tuple(
            MatrixMember(dataset_id=v.dataset_id, vector_hash=v.vector_hash) for v in ordered
        ),
    )


def build_partition_matrix(
    snapshot: FrozenSnapshot, partition: str, payload_provider: PayloadProvider
) -> MatrixBuild:
    """Extract, bind and assemble one partition matrix from exact member payloads.

    ``partition="test"`` is rejected BEFORE the payload provider is ever invoked, and
    the provider is only ever called for members of the requested partition.
    """
    checked = _require_matrix_partition(partition)  # BEFORE any payload access
    members = snapshot.members_for(checked)
    vectors = tuple(
        build_feature_vector(
            payload_provider(member),
            member,
            epoch=snapshot.epoch,
            snapshot_hash=snapshot.snapshot_hash,
        )
        for member in members
    )
    matrix = assemble_matrix(snapshot, checked, vectors)
    return MatrixBuild(matrix=matrix, vectors=vectors)


# --------------------------------------------------------------------------- #
# non-mutating verification: recompute, never repair
# --------------------------------------------------------------------------- #
def verify_matrix(
    matrix: FeatureMatrix,
    snapshot: FrozenSnapshot,
    vectors: Sequence[FeatureVector],
) -> VerificationResult:
    """LOGICAL verification: recompute rather than trust — membership, ordering,
    uniqueness, counts, identity bindings, vector/matrix hashes, and the canonical
    feature-values hash re-derived from each vector's own values."""
    checks: dict[str, bool] = {}
    checks["partition_is_train_or_validation"] = matrix.partition in MATRIX_PARTITIONS
    checks["registry_identity_accepted"] = matrix.registry_hash == REGISTRY_HASH
    checks["feature_set_identity_frozen"] = matrix.feature_set_hash == FROZEN_FEATURE_SET_HASH
    checks["column_count_is_frozen_129"] = matrix.column_count == EXPECTED_COLUMN_COUNT
    checks["epoch_binds_snapshot"] = matrix.epoch == snapshot.epoch
    checks["snapshot_hash_binds_snapshot"] = matrix.snapshot_hash == snapshot.snapshot_hash

    member_ids = [m.dataset_id for m in matrix.members]
    checks["members_ordered_and_unique"] = member_ids == sorted(set(member_ids)) and len(
        set(member_ids)
    ) == len(member_ids)

    if checks["partition_is_train_or_validation"]:
        expected = {m.dataset_id: m for m in snapshot.members_for(matrix.partition)}
    else:  # never enumerate test membership, even to diagnose an invalid matrix
        expected = {}
    checks["partition_membership_matches_snapshot"] = member_ids == sorted(expected)
    checks["row_count_matches_membership"] = matrix.row_count == len(matrix.members) and (
        not expected or matrix.row_count == len(expected)
    )

    by_id: dict[str, FeatureVector] = {}
    duplicate_vectors = False
    for vector in vectors:
        if vector.dataset_id in by_id:
            duplicate_vectors = True
        by_id[vector.dataset_id] = vector
    checks["vectors_cover_members_exactly"] = not duplicate_vectors and set(by_id) == set(
        member_ids
    )

    vector_hashes_ok = True
    bindings_ok = True
    feature_hash_bindings_ok = True
    members_bind_vectors_ok = True
    kinds_ok = True
    fvh_recompute_ok = True
    for matrix_member in matrix.members:
        row_vector = by_id.get(matrix_member.dataset_id)
        snapshot_member = expected.get(matrix_member.dataset_id)
        if row_vector is None or snapshot_member is None:
            vector_hashes_ok = bindings_ok = feature_hash_bindings_ok = False
            members_bind_vectors_ok = kinds_ok = fvh_recompute_ok = False
            continue
        if vector_hash(row_vector) != row_vector.vector_hash:
            vector_hashes_ok = False
        if matrix_member.vector_hash != row_vector.vector_hash:
            members_bind_vectors_ok = False
        try:
            _check_vector_bindings(row_vector, snapshot_member, snapshot, matrix.partition)
        except FeatureValuesHashMismatchError:
            feature_hash_bindings_ok = False
        except FeatureExtractionError:
            bindings_ok = False
        # recompute the canonical feature-values hash from the vector's OWN values.
        try:
            recomputed_fvh = _recompute_feature_values_hash_from_vector(row_vector)
        except FeatureExtractionError:
            kinds_ok = False
            fvh_recompute_ok = False
            continue
        if (
            recomputed_fvh != row_vector.feature_values_hash
            or recomputed_fvh != snapshot_member.feature_values_hash
        ):
            fvh_recompute_ok = False
    checks["vector_hashes_recomputed_match"] = vector_hashes_ok
    checks["matrix_members_bind_vector_hashes"] = members_bind_vectors_ok
    checks["vector_bindings_match_snapshot"] = bindings_ok
    checks["feature_values_hash_bindings_match"] = feature_hash_bindings_ok
    checks["vector_values_valid_for_column_kinds"] = kinds_ok
    checks["feature_values_hashes_recomputed_from_vectors"] = fvh_recompute_ok
    checks["matrix_hash_recomputed_match"] = matrix_hash(matrix) == matrix.matrix_hash

    return VerificationResult(checks=checks)


def verify_matrix_payload(
    matrix: FeatureMatrix,
    member_manifest_bytes: bytes,
    vectors: Sequence[FeatureVector],
    payload_provider: PayloadProvider,
    *,
    expected_member_manifest_hash: str | None = None,
    expected_registry_snapshot_hash: str | None = None,
) -> VerificationResult:
    """PAYLOAD verification, starting from VERIFIED member-manifest bytes (never from
    arbitrary SnapshotMember instances): re-derives the snapshot through
    :func:`load_member_manifest`, runs the full logical verification, then re-extracts
    every member's exact profile bytes — proving the bytes match the manifest-bound
    ``profile_sha256``, the extracted values match the vector, the recomputed
    feature-values hash matches both vector and manifest, and the profile identity and
    L1 hash match the full member binding."""
    snapshot = load_member_manifest(
        member_manifest_bytes,
        expected_member_manifest_hash=expected_member_manifest_hash,
        expected_registry_snapshot_hash=expected_registry_snapshot_hash,
    )
    checks = dict(verify_matrix(matrix, snapshot, vectors).checks)
    checks["member_manifest_verified"] = True  # load_member_manifest is fail-closed

    by_id = {v.dataset_id: v for v in vectors}
    if matrix.partition in MATRIX_PARTITIONS:
        expected = {m.dataset_id: m for m in snapshot.members_for(matrix.partition)}
    else:
        expected = {}
    payload_ok = True
    values_ok = True
    for matrix_member in matrix.members:
        row_vector = by_id.get(matrix_member.dataset_id)
        snapshot_member = expected.get(matrix_member.dataset_id)
        if row_vector is None or snapshot_member is None:
            payload_ok = values_ok = False
            continue
        try:
            # binds exact bytes (profile_sha256), profile identity and L1 hash.
            result = extract_profile_features(payload_provider(snapshot_member), snapshot_member)
        except FeatureExtractionError:
            payload_ok = values_ok = False
            continue
        if (
            result.feature_values_hash != snapshot_member.feature_values_hash
            or result.feature_values_hash != row_vector.feature_values_hash
        ):
            payload_ok = False
        if result.values != row_vector.values:
            values_ok = False
    checks["feature_values_hashes_recomputed_from_bytes"] = payload_ok
    checks["vector_values_match_recomputed_extraction"] = values_ok

    return VerificationResult(checks=checks)
