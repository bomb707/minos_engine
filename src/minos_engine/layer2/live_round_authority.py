"""``l2h-live-round-profile-ownership-v1`` — ownership for ONE authenticated live round.

**Why this is separate from the TRAIN authority.** ``l2h-round-profile-ownership-v2`` proves that
a request names one of the fifty frozen research rounds, and it does that by anchoring to the
TRAIN schedule, the Phase-A execution authority, the split manifest and the frozen registry
snapshot. That is correct for a qualification campaign and wrong for production: a live round has
none of those, and a live round is not admissible because it appeared in a historical experiment.

So the two authorities are kept apart and neither is weakened:

* the TRAIN loader is untouched and still anchors to exactly what it anchored to before;
* nothing here reads ``manifests/l2f2_train_schedule_v1.json``, the split manifest, the frozen
  profile snapshot, VALIDATION or TEST -- a live round is admissible because its **inputs
  authenticate against its own intake**.

**Round-scoped, not corpus-scoped.** Deciding one incoming challenge must not require loading an
ever-growing history and searching it for the current round. This factory takes exactly one
verified intake and exactly one profile, and mints an authority owning exactly that round. There
is no list to scan and no row to pick.

**What is proved.** The chain the specification asks for, end to end::

    verified live intake
        == the attestation's declared inputs
        == the profile manifest's inputs
        == the profile document's own identity
        == the DecisionRequest's profile reference

Every link is recomputed from bytes rather than believed, with one stated exception: the
manifest's ``fingerprint_hash`` is an L1-declared value that cannot be re-derived here. Rebuilding
it needs L1's sampling-plan and read-filter-policy identities, and Layer 2 may not import
``layer1.fingerprint`` at all (the architecture boundary the leakage suite enforces). What *is*
re-derived is ``profile_manifest_sha256`` -- the exact bytes of the manifest that declared it --
so a substituted manifest changes the identity the request must then match. The frozen TRAIN
authority carries the field on exactly the same terms. The accepted L2-D admission authority
``validate_admission`` is called unchanged -- not reimplemented -- and the ``registry_identity``
it validates against is reconstructed from the verified intake, so no caller is on the trust path.
Any mismatch raises; nothing is emitted, no config is returned, no decision is made.
"""

from __future__ import annotations

import hashlib
import json
from typing import Final

from minos_engine.common.canonical_json import canonical_json_bytes
from minos_engine.common.hashing import canonical_hash, sha256_hex
from minos_engine.layer2.live_round_intake import (
    LiveRoundIntakeError,
    VerifiedLiveRoundIntake,
)
from minos_engine.layer2.round_profile_authority import (
    LIVE_PARTITION,
    OwnedRoundProfile,
    VerifiedRoundProfileAuthority,
    mint_verified_ownership,
)

__all__ = [
    "LIVE_OWNERSHIP_DOMAIN",
    "LIVE_OWNERSHIP_SCHEMA",
    "LiveRoundOwnershipError",
    "load_verified_live_round_ownership",
]

LIVE_OWNERSHIP_SCHEMA: Final = "l2h-live-round-profile-ownership-v1"
LIVE_OWNERSHIP_DOMAIN: Final = "minos:l2h-live-round-profile-ownership:v1\n"


class LiveRoundOwnershipError(LiveRoundIntakeError):
    """A live profile cannot be proved to belong to its live round. Emit nothing."""


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise LiveRoundOwnershipError(message)


def load_verified_live_round_ownership(
    *,
    intake: VerifiedLiveRoundIntake,
    profile_bytes: bytes,
    manifest_bytes: bytes,
    attestation_bytes: bytes,
    windows_bytes: bytes,
) -> VerifiedRoundProfileAuthority:
    """Prove one live profile belongs to one live round, then mint round-scoped ownership.

    The four Layer 1 outputs are taken as **bytes**, not as parsed documents: a caller that hands
    over a dict has already decided what the bytes mean. Hashing happens here, and the documents
    are decoded from the exact bytes that were hashed.
    """
    from minos_engine.layer2.ingest.validation import validate_admission

    _require(
        isinstance(intake, VerifiedLiveRoundIntake),
        "live ownership may only be built from a verified live intake; a dictionary of round "
        "fields has been checked against nothing",
    )
    for name, raw in (
        ("profile", profile_bytes),
        ("manifest", manifest_bytes),
        ("attestation", attestation_bytes),
        ("windows", windows_bytes),
    ):
        _require(isinstance(raw, bytes) and bool(raw), f"the live {name} artifact is empty")

    profile_sha = hashlib.sha256(profile_bytes).hexdigest()
    manifest_sha = hashlib.sha256(manifest_bytes).hexdigest()
    windows_sha = hashlib.sha256(windows_bytes).hexdigest()
    try:
        profile_document = json.loads(profile_bytes)
        manifest_document = json.loads(manifest_bytes)
        attestation = json.loads(attestation_bytes)
    except json.JSONDecodeError as error:
        raise LiveRoundOwnershipError(f"a live artifact is not JSON: {error}") from None
    for name, document in (
        ("profile", profile_document),
        ("manifest", manifest_document),
        ("attestation", attestation),
    ):
        _require(isinstance(document, dict), f"the live {name} document is not an object")

    # --- the attestation is self-anchoring ------------------------------------------------
    recomputed = canonical_hash(
        {k: v for k, v in sorted(attestation.items()) if k != "attestation_hash"}
    )
    _require(
        recomputed == str(attestation.get("attestation_hash")),
        "the live attestation does not hash to its own recorded identity",
    )
    _require(
        canonical_hash(
            {
                "bam_sha256": attestation.get("bam_sha256"),
                "bai_sha256": attestation.get("bai_sha256"),
                "reference_sha256": attestation.get("reference_sha256"),
                "fai_sha256": attestation.get("fai_sha256"),
                "region_hash": attestation.get("region_hash"),
            }
        )
        == str(attestation.get("identity_tuple_hash")),
        "the live attestation's identity tuple does not recompute from its own components",
    )

    # --- ... and anchored to THIS live intake, not to itself and not to a research registry ---
    registry_identity = intake.registry_identity()
    for field, expected in registry_identity.items():
        observed = attestation.get(field)
        _require(
            str(observed) == str(expected),
            f"the live attestation's {field} is {observed!r}, but this round's intake registers "
            f"{expected!r}",
        )

    # --- the profile manifest must describe the SAME region and the same profile -------------
    _require(
        str(manifest_document.get("region_contig")) == str(intake.chromosome)
        and int(manifest_document.get("region_start0", -1)) == int(intake.region_start0)
        and int(manifest_document.get("region_end0", -1)) == int(intake.region_end0_exclusive),
        "the live profile manifest describes a different region than this round's intake",
    )
    _require(
        str(manifest_document.get("profile_sha256")) == profile_sha
        and str(manifest_document.get("windows_sha256")) == windows_sha,
        "the live profile manifest does not describe the artifact bytes it was given",
    )

    # --- THE accepted L2-D admission authority, unchanged and not reimplemented ---------------
    decision = validate_admission(
        profile_document=profile_document,
        manifest_document=manifest_document,
        attestation=attestation,
        registry_identity=registry_identity,
        profile_artifact_sha256=profile_sha,
        windows_artifact_sha256=windows_sha,
    )
    _require(
        decision.admissible,
        "the live profile is not admissible: " + ("; ".join(decision.reasons) or "no reason given"),
    )

    owned = OwnedRoundProfile(
        round_id=str(intake.round_id),
        dataset_id=intake.dataset_id,
        chromosome=str(intake.chromosome),
        partition=LIVE_PARTITION,
        profile_id=str(profile_document["profile_id"]),
        profile_sha256=profile_sha,
        profile_manifest_sha256=manifest_sha,
        fingerprint_hash=str(manifest_document["fingerprint_hash"]),
        attestation_hash=str(attestation["attestation_hash"]),
        registry_snapshot_hash=intake.identity,
        identity_tuple_hash=str(intake.identity_tuple_hash),
        region_hash=str(intake.region_hash),
        bam_sha256=str(intake.bam_sha256),
        bai_sha256=str(intake.bai_sha256),
        reference_sha256=str(intake.reference_sha256),
        fai_sha256=str(intake.fai_sha256),
        integrity_degraded=bool(decision.integrity_degraded),
    )
    anchors = {
        "live_intake_identity": intake.identity,
        "live_intake_schema": str(intake.schema_version),
        "profile_manifest_sha256": manifest_sha,
        "profile_sha256": profile_sha,
        "windows_sha256": windows_sha,
    }
    identity = sha256_hex(
        LIVE_OWNERSHIP_DOMAIN.encode("utf-8")
        + canonical_json_bytes(
            {
                "schema_version": LIVE_OWNERSHIP_SCHEMA,
                "partition": LIVE_PARTITION,
                "anchors": dict(sorted(anchors.items())),
                "member_count": 1,
                "members": [owned.content()],
            }
        )
    )
    return mint_verified_ownership(
        by_round={owned.round_id: owned},
        anchors=anchors,
        corpus_identity=identity,
        partition=LIVE_PARTITION,
    )
