"""Deterministic ContextFingerprint construction (Layer 1 spec §15, §17).

The fingerprint binds only semantic identities and canonical feature values —
never machine paths, timestamps, elapsed runtime, hostnames, temp filenames, or
process/thread ids. Repeated runs over identical semantic inputs produce the same
fingerprint hash; changing any semantic input changes the appropriate identity.
"""

from __future__ import annotations

from typing import Any

from minos_engine.common.hashing import canonical_hash

from .contracts import BamProfile, ContextFingerprint

__all__ = ["feature_values_hash", "build_fingerprint"]

# Measurement families that constitute content identity (volatile sections such
# as stage_timings, runtime_complexity, degradation, warnings, and provenance are
# excluded on purpose).
_IDENTITY_SECTIONS = (
    "reads",
    "coverage",
    "mapping_quality",
    "base_quality",
    "read_length",
    "pairing",
    "alignment",
    "variant_evidence",
    "reference_context",
    "spatial",
    "difficulty",
    "confidence",
    "completion",
)


def feature_values_hash(profile: BamProfile) -> str:
    dumped: dict[str, Any] = profile.model_dump(mode="json")
    values = {section: dumped[section] for section in _IDENTITY_SECTIONS}
    return canonical_hash(values)


def build_fingerprint(
    profile: BamProfile,
    *,
    sampling_plan_hash: str,
    read_filter_policy_hash: str,
) -> ContextFingerprint:
    identity = profile.identity
    return ContextFingerprint(
        profile_schema_version=profile.schema_version,
        profiler_algorithm_version=profile.provenance.profiler_version,
        profiler_config_hash=profile.provenance.config_hash,
        bam_sha256=identity.bam_sha256,
        index_status=identity.index_status,
        index_sha256=identity.index_sha256,
        reference_status=identity.reference_status,
        reference_sha256=identity.reference_sha256,
        region=profile.region,
        sampling_plan_hash=sampling_plan_hash,
        read_filter_policy_hash=read_filter_policy_hash,
        completed_families=profile.completion.completed_families,
        degradation_status=profile.status,
        feature_values_hash=feature_values_hash(profile),
    )
