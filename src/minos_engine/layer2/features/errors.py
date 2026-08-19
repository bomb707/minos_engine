"""Typed E2 extraction/assembly failures — every rejection path has a named error.

All inherit :class:`FeatureExtractionError` (itself a ``MinosEngineError``) so callers
can catch the family; verification NEVER raises these to repair anything — the verifier
returns named boolean checks and leaves invalid inputs untouched.
"""

from __future__ import annotations

from minos_engine.common.errors import MinosEngineError

__all__ = [
    "FeatureExtractionError",
    "ProfileArtifactHashMismatchError",
    "SnapshotIdentityMismatchError",
    "FeatureValuesHashMismatchError",
    "InvalidProfileDocumentError",
    "MissingFeatureError",
    "ForbiddenPartitionError",
    "MatrixAssemblyError",
    "InvalidMemberManifestError",
    "MemberManifestHashMismatchError",
    "SnapshotHashMismatchError",
    "RegistrySnapshotMismatchError",
    "ProfilerIdentityMismatchError",
]


class FeatureExtractionError(MinosEngineError):
    """Base class for all L2-E extraction/binding/assembly failures."""


class ProfileArtifactHashMismatchError(FeatureExtractionError):
    """The received profile bytes do not hash to the snapshot member's profile_sha256."""


class SnapshotIdentityMismatchError(FeatureExtractionError):
    """A profile/vector identity field does not match the snapshot member binding."""


class FeatureValuesHashMismatchError(FeatureExtractionError):
    """A recomputed canonical feature-values hash disagrees with a bound hash."""


class InvalidProfileDocumentError(FeatureExtractionError):
    """The profile bytes are not a valid bam-profile-v1 document (encoding, JSON,
    duplicate keys, or accepted-schema violation)."""


class MissingFeatureError(FeatureExtractionError):
    """An authoritative feature is missing, null, non-finite, out of range, or
    otherwise invalid in the profile document."""


class ForbiddenPartitionError(FeatureExtractionError):
    """The test partition (or any non-train/validation partition) was requested —
    rejected before any member payload is inspected."""


class MatrixAssemblyError(FeatureExtractionError):
    """The supplied vector set does not exactly cover the snapshot partition
    membership (missing, duplicate, or extra vectors)."""


class InvalidMemberManifestError(FeatureExtractionError):
    """The member-manifest bytes are not a valid profile-snapshot-members-v1 document
    (encoding, JSON, duplicate keys, or structural violation)."""


class MemberManifestHashMismatchError(FeatureExtractionError):
    """The recomputed canonical member_manifest_hash disagrees with the declared or
    expected accepted value."""


class SnapshotHashMismatchError(FeatureExtractionError):
    """A supplied snapshot_hash does not equal the hash recomputed with the accepted
    PROFILE-SNAPSHOT-FROZEN freeze formula."""


class RegistrySnapshotMismatchError(FeatureExtractionError):
    """A member's registry_snapshot_hash disagrees with the manifest binding or the
    expected pinned registry snapshot."""


class ProfilerIdentityMismatchError(FeatureExtractionError):
    """A member's profiler_version/profiler_config_hash is not the accepted profiler
    identity pinned in layer2.prerequisites."""
