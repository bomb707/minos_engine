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

**Two authority domains, not one with a label.** Every stage here exists twice -- a production
capability and a fixture capability, each with its own module-private token. The offline replay
runs the identical validation through :func:`observe_fixture_live_profile_binding` and ends in
:class:`FixtureRoundProfileAuthority`, which the safe controller does not accept. It previously
ended in a genuine ``VerifiedRoundProfileAuthority(partition="live")``, because one class carried a
mutable ``scope`` string and the guards below asked only ``isinstance``. A test fixture must never
be able to mint live authority, so behaviour is shared through a common base and shared builders
while authority is not shared at all.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Final

from minos_engine.common.canonical_json import canonical_json_bytes
from minos_engine.common.hashing import canonical_hash, sha256_hex
from minos_engine.layer2.live_round_intake import (
    LiveRoundIntakeError,
    VerifiedLiveRoundIntake,
)
from minos_engine.layer2.round_profile_authority import (
    LIVE_PARTITION,
    OwnedRoundCorpus,
    OwnedRoundProfile,
    VerifiedRoundProfileAuthority,
)

__all__ = [
    "LIVE_OWNERSHIP_DOMAIN",
    "LIVE_OWNERSHIP_SCHEMA",
    "FixtureLiveProfileBinding",
    "FixtureRoundProfileAuthority",
    "LiveRoundOwnershipError",
    "VerifiedLiveProfileBinding",
    "VerifiedProductionLiveProfileBinding",
    "is_verified_production_live_binding",
    "load_verified_live_round_ownership",
    "observe_fixture_live_profile_binding",
    "observe_fixture_live_round_ownership",
    "verify_live_profile_binding",
]

#: One token per authority domain, exactly as for the intake.
_PRODUCTION_BINDING_TOKEN: Final = object()
_FIXTURE_BINDING_TOKEN: Final = object()


class VerifiedLiveProfileBinding:
    """Proof that ONE live profile belongs to ONE live round.

    ``round_profile_authority.own_verified_live_round`` accepts only the **production** subclass,
    by exact type and private seal. An earlier version used ``isinstance`` here, which a subclass
    skipping ``__init__`` and populating ``owned``/``anchors``/``identity`` would have satisfied --
    the generic raw-data mint again, through inheritance.
    """

    __slots__ = ("_seal", "anchors", "identity", "owned")

    scope: str = ""
    _expected_token: Any = None

    def __init__(
        self,
        token: object,
        *,
        owned: OwnedRoundProfile,
        anchors: dict[str, str],
        identity: str,
    ) -> None:
        if token is not type(self)._expected_token or token is None:
            raise LiveRoundOwnershipError(
                "a live profile binding may only be minted by verifying one; a map of owned "
                "profile fields is not a proof that a profile belongs to a round"
            )
        self.owned = owned
        self.anchors = dict(anchors)
        self.identity = identity
        self._seal = token


class VerifiedProductionLiveProfileBinding(VerifiedLiveProfileBinding):
    """The only binding production ownership will accept."""

    __slots__ = ()
    scope = "production"
    _expected_token = _PRODUCTION_BINDING_TOKEN


class FixtureLiveProfileBinding(VerifiedLiveProfileBinding):
    """The offline result. It mints no ownership and reaches no controller."""

    __slots__ = ()
    scope = "fixture"
    _expected_token = _FIXTURE_BINDING_TOKEN


def is_verified_production_live_binding(candidate: Any) -> bool:
    """Exact concrete type and private seal."""
    return (
        type(candidate) is VerifiedProductionLiveProfileBinding
        and getattr(candidate, "_seal", None) is _PRODUCTION_BINDING_TOKEN
    )


LIVE_OWNERSHIP_SCHEMA: Final = "l2h-live-round-profile-ownership-v1"
LIVE_OWNERSHIP_DOMAIN: Final = "minos:l2h-live-round-profile-ownership:v1\n"


class LiveRoundOwnershipError(LiveRoundIntakeError):
    """A live profile cannot be proved to belong to its live round. Emit nothing."""


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise LiveRoundOwnershipError(message)


def verify_live_profile_binding(
    *,
    intake: Any,
    profile_bytes: bytes,
    manifest_bytes: bytes,
    attestation_bytes: bytes,
    windows_bytes: bytes,
) -> VerifiedProductionLiveProfileBinding:
    """THE production entry point. A PRODUCTION intake, or nothing.

    A fixture observation is refused here, before any production binding exists -- it is a
    different capability, not a production one wearing a label. Offline replay uses
    :func:`observe_fixture_live_profile_binding`, which shares this implementation exactly and
    mints a fixture capability instead.
    """
    from minos_engine.layer2.live_round_intake import is_verified_production_intake

    _require(
        is_verified_production_intake(intake),
        "a production live profile binding requires a sealed PRODUCTION live intake; a fixture "
        "observation cannot become live authority by any route",
    )
    result = _build_live_profile_binding(
        intake=intake,
        profile_bytes=profile_bytes,
        manifest_bytes=manifest_bytes,
        attestation_bytes=attestation_bytes,
        windows_bytes=windows_bytes,
        factory=VerifiedProductionLiveProfileBinding,
    )
    assert isinstance(result, VerifiedProductionLiveProfileBinding)
    return result


def observe_fixture_live_profile_binding(
    *,
    intake: Any,
    profile_bytes: bytes,
    manifest_bytes: bytes,
    attestation_bytes: bytes,
    windows_bytes: bytes,
) -> FixtureLiveProfileBinding:
    """The offline seam. Identical validation, a capability that ends here."""
    from minos_engine.layer2.live_round_intake import is_fixture_intake

    _require(
        is_fixture_intake(intake),
        "a fixture live profile binding requires a sealed fixture live intake",
    )
    result = _build_live_profile_binding(
        intake=intake,
        profile_bytes=profile_bytes,
        manifest_bytes=manifest_bytes,
        attestation_bytes=attestation_bytes,
        windows_bytes=windows_bytes,
        factory=FixtureLiveProfileBinding,
    )
    assert isinstance(result, FixtureLiveProfileBinding)
    return result


def _build_live_profile_binding(
    *,
    intake: Any,
    profile_bytes: bytes,
    manifest_bytes: bytes,
    attestation_bytes: bytes,
    windows_bytes: bytes,
    factory: type[VerifiedLiveProfileBinding],
) -> VerifiedLiveProfileBinding:
    """Prove one live profile belongs to one live round. Shared by both authority domains.

    The four Layer 1 outputs are taken as **bytes**, not as parsed documents: a caller that hands
    over a dict has already decided what the bytes mean. Hashing happens here, and the documents
    are decoded from the exact bytes that were hashed.

    The factory carries its own token, so there is no argument through which the fixture path
    could ask for the production one. Which domain's *intake* is acceptable was already decided by
    the caller above; this function only re-proves the science, identically for both.
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
        # carried so a fixture-scoped chain is visible in the ownership identity itself and can
        # never be mistaken downstream for a production one
        "live_intake_scope": str(intake.scope),
        "platform_receipt_identity": str(intake.receipt_identity),
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
    return factory(factory._expected_token, owned=owned, anchors=anchors, identity=identity)


class FixtureRoundProfileAuthority(OwnedRoundCorpus):
    """Where the offline chain ENDS.

    It carries the same lookups and the same identity arithmetic as production ownership, so a
    replay can exercise ``require_owned_request`` and the manifest refusal -- and it is a different
    type, so ``is_verified_round_profile_authority`` rejects it and the safe controller will not
    take it. A fixture must be able to prove the logic without ever becoming the authority.
    """

    __slots__ = ()


def load_verified_live_round_ownership(
    *,
    intake: Any,
    profile_bytes: bytes,
    manifest_bytes: bytes,
    attestation_bytes: bytes,
    windows_bytes: bytes,
) -> VerifiedRoundProfileAuthority:
    """PRODUCTION: verify the binding, then have the token-owning module mint ownership."""
    from minos_engine.layer2.round_profile_authority import own_verified_live_round

    return own_verified_live_round(
        verify_live_profile_binding(
            intake=intake,
            profile_bytes=profile_bytes,
            manifest_bytes=manifest_bytes,
            attestation_bytes=attestation_bytes,
            windows_bytes=windows_bytes,
        )
    )


def observe_fixture_live_round_ownership(
    *,
    intake: Any,
    profile_bytes: bytes,
    manifest_bytes: bytes,
    attestation_bytes: bytes,
    windows_bytes: bytes,
) -> FixtureRoundProfileAuthority:
    """FIXTURE: the same validation, ending in a capability the controller will not accept."""
    binding = observe_fixture_live_profile_binding(
        intake=intake,
        profile_bytes=profile_bytes,
        manifest_bytes=manifest_bytes,
        attestation_bytes=attestation_bytes,
        windows_bytes=windows_bytes,
    )
    return FixtureRoundProfileAuthority(
        by_round={binding.owned.round_id: binding.owned},
        anchors=dict(binding.anchors),
        corpus_identity=binding.identity,
        partition=LIVE_PARTITION,
    )
