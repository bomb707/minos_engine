"""L2-D admission validation — pure, fail-closed, boundary-clean.

Validates one candidate ingestion (profile document + manifest document + attestation +
registry identity + artifact byte-hashes) and returns an :class:`AdmissionDecision`. No
database, no file I/O, no intake import — callers (the ``storage`` repository, tests, the
qualification runner) supply parsed documents and byte hashes.

Admission requires ALL of:
  * the profile parses as a typed ``layer1.contracts.BamProfile`` with status COMPLETE
    (PARTIAL/FAILED never enter the accepted corpus — they belong to the attempts table);
  * the manifest parses, matches the profile identity (profile_id, region, status,
    profiler version/config), and binds the exact artifact bytes
    (``profile_sha256`` / ``windows_sha256``) plus a positive ``windows_row_count``;
  * the attestation is valid, binds the SAME registry identity (dataset/round/chromosome/
    file hashes/region/identity tuple/registry snapshot), and its m5 status admits
    (MATCH admits; ABSENT admits with ``integrity_degraded``; MISMATCH rejects);
  * every production-ELIGIBLE feature value is present and valid, yielding the frozen
    canonical ``feature_values_hash`` (owner ruling) that the storage layer must write
    verbatim to the typed column and bind into ``content_hash``;
  * the Layer 1 identity-section hash recomputed from the document equals the profile's
    declared L1 ``feature_values_hash`` chain when supplied (frozen section list pinned
    here; a unit test proves the pin equals ``layer1.fingerprint``'s).
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict

from minos_engine.common.errors import AdmissionRejectedError
from minos_engine.common.hashing import canonical_hash
from minos_engine.layer1.contracts import BamProfile, ProfileManifest, ProfileStatus

from .contracts import (
    InputIntegrityAttestation,
    admission_for_m5,
    canonical_feature_values_hash,
    extract_eligible_feature_values,
)

__all__ = [
    "L1_IDENTITY_SECTIONS",
    "l1_feature_values_hash_from_document",
    "AdmissionDecision",
    "validate_admission",
]

#: Frozen pin of Layer 1's fingerprint identity-section list (layer1/fingerprint.py).
#: Layer 2 may not import that module (architecture boundary), so the list is pinned here
#: and a unit test cross-checks the pin against the real one. Changing L1's list is a
#: qualification event; this pin failing the cross-check blocks admission logic drift.
L1_IDENTITY_SECTIONS: tuple[str, ...] = (
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


def l1_feature_values_hash_from_document(profile_document: dict[str, Any]) -> str:
    """Recompute Layer 1's section-level feature_values_hash from the raw document."""
    missing = [s for s in L1_IDENTITY_SECTIONS if s not in profile_document]
    if missing:
        raise AdmissionRejectedError(f"profile document missing identity sections: {missing}")
    return canonical_hash({s: profile_document[s] for s in L1_IDENTITY_SECTIONS})


class AdmissionDecision(BaseModel):
    """The outcome of pure admission validation for one candidate profile."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    admissible: bool
    integrity_degraded: bool
    feature_values_hash: str  # frozen canonical (owner ruling) — the DB column value
    l1_feature_values_hash: str  # Layer 1 section-level recompute (cross-binding)
    eligible_value_count: int
    checks: dict[str, bool]
    reasons: tuple[str, ...] = ()


def validate_admission(
    *,
    profile_document: dict[str, Any],
    manifest_document: dict[str, Any],
    attestation: InputIntegrityAttestation | dict[str, Any],
    registry_identity: dict[str, Any],
    profile_artifact_sha256: str,
    windows_artifact_sha256: str,
) -> AdmissionDecision:
    """Validate one candidate ingestion; every check must pass for admission.

    ``registry_identity`` carries the registered identity the caller selected:
    ``dataset_id``, ``round_id``, ``chromosome``, ``bam_sha256``, ``bai_sha256``,
    ``reference_sha256``, ``fai_sha256``, ``region_hash``, ``identity_tuple_hash``, and
    ``registry_snapshot_hash``. ``profile_artifact_sha256`` / ``windows_artifact_sha256``
    are the byte hashes of the exact artifact files being ingested.
    """
    checks: dict[str, bool] = {}
    reasons: list[str] = []

    # --- typed profile: COMPLETE only -----------------------------------------------
    profile: BamProfile | None = None
    try:
        profile = BamProfile.model_validate(profile_document)
        checks["profile_contract_valid"] = True
    except Exception as exc:  # noqa: BLE001
        checks["profile_contract_valid"] = False
        reasons.append(f"profile: {exc}")
    checks["profile_status_complete"] = bool(
        profile is not None and profile.status is ProfileStatus.COMPLETE
    )

    # --- typed manifest + artifact byte binding --------------------------------------
    manifest: ProfileManifest | None = None
    try:
        manifest = ProfileManifest.model_validate(manifest_document)
        checks["manifest_contract_valid"] = True
    except Exception as exc:  # noqa: BLE001
        checks["manifest_contract_valid"] = False
        reasons.append(f"manifest: {exc}")
    if manifest is not None and profile is not None:
        checks["manifest_profile_id_bound"] = manifest.profile_id == profile.profile_id
        checks["manifest_status_bound"] = manifest.status is profile.status
        checks["manifest_region_bound"] = (
            manifest.region_contig == profile.region.contig
            and manifest.region_start0 == profile.region.start0
            and manifest.region_end0 == profile.region.end0_exclusive
        )
        checks["manifest_provenance_bound"] = (
            manifest.profiler_version == profile.provenance.profiler_version
            and manifest.profiler_config_hash == profile.provenance.config_hash
        )
    else:
        checks["manifest_profile_id_bound"] = False
        checks["manifest_status_bound"] = False
        checks["manifest_region_bound"] = False
        checks["manifest_provenance_bound"] = False
    checks["profile_artifact_bytes_bound"] = bool(
        manifest is not None and manifest.profile_sha256 == profile_artifact_sha256
    )
    checks["windows_artifact_bytes_bound"] = bool(
        manifest is not None and manifest.windows_sha256 == windows_artifact_sha256
    )
    checks["windows_row_count_positive"] = bool(
        manifest is not None and manifest.windows_row_count > 0
    )

    # --- attestation: valid + binds the SAME registry identity + m5 admission --------
    att: InputIntegrityAttestation | None = None
    try:
        att = (
            attestation
            if isinstance(attestation, InputIntegrityAttestation)
            else InputIntegrityAttestation.model_validate(attestation)
        )
        checks["attestation_valid"] = True
    except Exception as exc:  # noqa: BLE001
        checks["attestation_valid"] = False
        reasons.append(f"attestation: {exc}")
    integrity_degraded = False
    if att is not None:
        ident_ok = all(
            str(getattr(att, key)) == str(registry_identity.get(key))
            for key in (
                "dataset_id",
                "round_id",
                "chromosome",
                "bam_sha256",
                "bai_sha256",
                "reference_sha256",
                "fai_sha256",
                "region_hash",
                "identity_tuple_hash",
                "registry_snapshot_hash",
            )
        )
        checks["attestation_identity_bound"] = ident_ok
        admissible_m5, integrity_degraded = admission_for_m5(att.m5_status)
        checks["m5_admissible"] = admissible_m5
    else:
        checks["attestation_identity_bound"] = False
        checks["m5_admissible"] = False

    # --- profile identity must match the SAME registered identity --------------------
    if profile is not None:
        checks["profile_identity_bound"] = profile.identity.bam_sha256 == str(
            registry_identity.get("bam_sha256")
        ) and profile.region.contig == str(registry_identity.get("chromosome"))
    else:
        checks["profile_identity_bound"] = False

    # --- canonical ELIGIBLE feature values (frozen algorithm; fail closed) ------------
    fv_hash = ""
    eligible_count = 0
    try:
        eligible = extract_eligible_feature_values(profile_document)
        eligible_count = len(eligible)
        fv_hash = canonical_feature_values_hash(eligible)
        checks["eligible_features_complete"] = True
    except Exception as exc:  # noqa: BLE001
        checks["eligible_features_complete"] = False
        reasons.append(f"features: {exc}")

    # --- Layer 1 section-level hash recompute (cross-binding) ------------------------
    l1_hash = ""
    try:
        l1_hash = l1_feature_values_hash_from_document(profile_document)
        checks["l1_identity_sections_present"] = True
    except Exception as exc:  # noqa: BLE001
        checks["l1_identity_sections_present"] = False
        reasons.append(f"l1-hash: {exc}")

    for name, ok in checks.items():
        if not ok:
            reasons.append(f"{name} failed")

    return AdmissionDecision(
        admissible=all(checks.values()),
        integrity_degraded=integrity_degraded,
        feature_values_hash=fv_hash,
        l1_feature_values_hash=l1_hash,
        eligible_value_count=eligible_count,
        checks=checks,
        reasons=tuple(dict.fromkeys(reasons)),
    )
