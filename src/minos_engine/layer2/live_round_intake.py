"""``l2h-live-round-intake-v2`` — what LIVE challenge the engine is servicing, and on what bytes.

**What the first attempt got wrong, and what the second still got wrong.** The first let a caller
supply a round id, a region and four content hashes, checked they agreed with each other, and
minted the capability — internal consistency is not provenance. The second fixed that by demanding
a platform receipt, but any transport could mint one (a fixture, and even the demo endpoint), and
the download digests proved only that *some* local files had been hashed, not that those bytes
came from this round's own sources.

So authority now arrives as two proofs, each scope-separated and each round-bound:

* a **production** platform receipt, which only the authenticated subnet transport at exactly
  ``/v2/round-status`` can mint;
* **production round downloads**, minted only by handing over the exact URL each file came from,
  which must be a URL that receipt actually carries.

and the builder is split from the authority:

* :func:`canonical_live_round_content` canonicalizes and mints **nothing**;
* :func:`verify_live_round_intake` mints and has no ``content`` parameter at all;
* :func:`observe_fixture_live_round_intake` is the test seam — the *same* builder, its own scope.

so that::

    the platform proves WHICH ROUND and WHICH REGION were offered
    round-bound downloads prove WHICH BYTES came from that round's own sources
    the accepted reference table proves WHICH REFERENCE this engine profiles against

No field of a verified intake originates in a caller-chosen string, and no presigned URL — which
expires, carries a signature and varies between equivalent fetches — enters the scientific
identity. The intake carries its scope, so a fixture chain can never be mistaken for a live one:
:func:`require_production_scope` is the guard the live service will use.

``parameter_space_hash`` is deliberately absent. It defines what the controller may *do*, not what
the round *is*, and the controller already checks the request's parameter space against the
accepted live space. Binding it here would break the profile binding whenever the parameter space
moved even though the inputs had not.
"""

from __future__ import annotations

from typing import Any, Final

from minos_engine.common.canonical_json import canonical_json_bytes
from minos_engine.common.errors import MinosEngineError
from minos_engine.common.hashing import canonical_hash, sha256_hex

__all__ = [
    "ACCEPTED_REFERENCE_IDENTITIES",
    "observe_fixture_live_round_intake",
    "require_production_scope",
    "LIVE_INTAKE_DOMAIN",
    "LIVE_INTAKE_FIELDS",
    "LIVE_INTAKE_SCHEMA",
    "SUPPORTED_CONTIGS",
    "LiveRoundIntakeError",
    "ReferenceIdentity",
    "VerifiedLiveRoundIntake",
    "canonical_live_round_content",
    "live_dataset_id_for",
    "live_round_intake_identity",
    "verify_live_round_intake",
]

LIVE_INTAKE_SCHEMA: Final = "l2h-live-round-intake-v2"
LIVE_INTAKE_DOMAIN: Final = "minos:l2h-live-round-intake:v2\n"

#: The CLOSED content set. A missing field and an unknown field are both refusals: a document
#: carrying fields this version does not understand has not been understood.
LIVE_INTAKE_FIELDS: Final[tuple[str, ...]] = (
    "bai_sha256",
    "bam_sha256",
    "chromosome",
    "fai_sha256",
    "identity_tuple_hash",
    "platform_receipt_identity",
    "reference_sha256",
    "region_coordinate_system",
    "region_end0_exclusive",
    "region_hash",
    "region_length_bp",
    "region_source",
    "region_start0",
    "round_id",
    "schema_version",
)

_HEX64: Final = frozenset("0123456789abcdef")
_ONE_BASED: Final = "one_based_inclusive"


class LiveRoundIntakeError(MinosEngineError):
    """A live round could not be authenticated. No profile, no ownership, no decision."""


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise LiveRoundIntakeError(message)


class ReferenceIdentity:
    """The reference FASTA and index this engine profiles a given contig against."""

    __slots__ = ("contig", "fai_sha256", "reference_m5", "reference_sha256")

    def __init__(
        self, *, contig: str, reference_sha256: str, fai_sha256: str, reference_m5: str
    ) -> None:
        self.contig = contig
        self.reference_sha256 = reference_sha256
        self.fai_sha256 = fai_sha256
        self.reference_m5 = reference_m5


#: Accepted per-contig reference identities. These are not caller input: a live round's reference
#: is whichever one this engine has been qualified against for that chromosome, and if the local
#: reference is a different build the round is refused rather than profiled against the wrong
#: genome. A unit test cross-checks every entry against the frozen L2-D corpus attestations, which
#: is where they come from -- all fifty members agree, one reference per chromosome.
ACCEPTED_REFERENCE_IDENTITIES: Final[dict[str, ReferenceIdentity]] = {
    "chr18": ReferenceIdentity(
        contig="chr18",
        reference_sha256="4c37db9609b3e865e35128fb065f61eea1c83c815386c4631b03820f9b8265d2",
        fai_sha256="1e9dac505c1b48f7a1ca90c5ec7e75ed257a19d4c03b21f4c40e4dfa29b806b5",
        reference_m5="11eeaa801f6b0e2e36a1138616b8ee9a",
    ),
    "chr19": ReferenceIdentity(
        contig="chr19",
        reference_sha256="e00b74f7cd48f6c94395c40ae7b4c13d1c3255e2db1be366eb5a87441577ec5c",
        fai_sha256="ce0ee961cd23439f944459c8483b3a11ab58b87e201272862a24d67fb7707805",
        reference_m5="85f9f4fc152c58cb7913c06d6b98573a",
    ),
    "chr20": ReferenceIdentity(
        contig="chr20",
        reference_sha256="61eba5b05ef7d9ae5310e756c1143fa48072de3856d36871bb14e57aa2435ff3",
        fai_sha256="295950bb320e5f27b37360000d77303187e6399bf8e7705aa26fd4a1c88ba115",
        reference_m5="b18e6c531b0bd70e949a7fc20859cb01",
    ),
    "chr21": ReferenceIdentity(
        contig="chr21",
        reference_sha256="c218d98e3bf58fa3551c3f5f12bc829c798c42fd301f8ed6021c35aa231f39f8",
        fai_sha256="838e3d562353d9a90084416731de18de5848dbb982939ae59af9ce37fd19e0de",
        reference_m5="974dc7aec0b755b19f031418fdedf293",
    ),
    "chr22": ReferenceIdentity(
        contig="chr22",
        reference_sha256="8d440d7b863c6d0af1bc438385a770b0a861ddc0f5187b7466d893e2728a9b54",
        fai_sha256="7623e1c5091eda09847bd72b64bf57b1abe0bfab3cbef8c4e687975e8ee09e2c",
        reference_m5="ac37ec46683600f808cdd41eac1d55cd",
    ),
}

#: The chromosomes this engine profiles. A live round outside them is refused, never guessed.
SUPPORTED_CONTIGS: Final[tuple[str, ...]] = tuple(sorted(ACCEPTED_REFERENCE_IDENTITIES))

_INTAKE_TOKEN: Final = object()


def live_dataset_id_for(*, chromosome: str, identity_tuple_hash: str) -> str:
    """A content-addressed dataset identity for a live round.

    A live round has no registered research dataset behind it -- no ``catalog.dataset_registry``
    row, no split allocation, no epoch. What it does have is an exact input set, so the dataset
    identity is derived from that and prefixed ``live-`` so it can never be mistaken for a
    registry-backed research id such as ``minos-chr21-0279a3b8042f848b``.
    """
    return f"live-{chromosome}-{identity_tuple_hash[:16]}"


def live_round_intake_identity(content: dict[str, Any]) -> str:
    """Domain-separated identity over the canonical intake content."""
    return sha256_hex(LIVE_INTAKE_DOMAIN.encode("utf-8") + canonical_json_bytes(content))


def canonical_live_round_content(
    *,
    round_id: str,
    region_source: str,
    platform_receipt_identity: str,
    bam_sha256: str,
    bai_sha256: str,
    reference_sha256: str,
    fai_sha256: str,
    region_coordinate_system: str = _ONE_BASED,
) -> dict[str, Any]:
    """Canonicalize one live round's inputs. **This mints no authority.**

    It is a pure builder: the region is parsed once under an explicit convention by the shared rule
    a unit test holds to ``intake.contracts.Region``, and the identity tuple is derived. Passing
    its output to anything does not make that output true -- only
    :func:`verify_live_round_intake`, from a platform receipt and real download digests, produces a
    capability.
    """
    from minos_engine.common.genomic_region import normalize_region

    def _hex64(value: Any, name: str) -> str:
        text = str(value)
        _require(
            len(text) == 64 and set(text) <= _HEX64,
            f"{name} must be 64 lowercase hex characters, got {value!r}",
        )
        return text

    try:
        contig, start0, end0_exclusive = normalize_region(region_source, region_coordinate_system)
    except ValueError as error:
        raise LiveRoundIntakeError(f"the live region is not usable: {error}") from None
    _require(
        contig in SUPPORTED_CONTIGS,
        f"contig {contig!r} is not one this engine profiles {SUPPORTED_CONTIGS}",
    )
    hashes = {
        "bam_sha256": _hex64(bam_sha256, "bam_sha256"),
        "bai_sha256": _hex64(bai_sha256, "bai_sha256"),
        "reference_sha256": _hex64(reference_sha256, "reference_sha256"),
        "fai_sha256": _hex64(fai_sha256, "fai_sha256"),
    }
    region_hash = canonical_hash(
        {
            "contig": contig,
            "start0": start0,
            "end0_exclusive": end0_exclusive,
            "length_bp": end0_exclusive - start0,
            "coordinate_system": "zero_based_half_open",
        }
    )
    return {
        "schema_version": LIVE_INTAKE_SCHEMA,
        "round_id": str(round_id),
        "platform_receipt_identity": _hex64(platform_receipt_identity, "platform_receipt_identity"),
        "chromosome": contig,
        "region_source": region_source.strip(),
        "region_coordinate_system": region_coordinate_system,
        "region_start0": start0,
        "region_end0_exclusive": end0_exclusive,
        "region_length_bp": end0_exclusive - start0,
        "region_hash": region_hash,
        # exactly the tuple the L2-D attestation recomputes, so the two cannot disagree silently
        "identity_tuple_hash": canonical_hash({**hashes, "region_hash": region_hash}),
        **hashes,
    }


class VerifiedLiveRoundIntake:
    """Proof that the platform offered this round and that these are the bytes it names.

    Minted only by :func:`verify_live_round_intake`. Canonical content alone cannot produce one:
    the constructor demands a private token, and the only holder needs a platform receipt and real
    download digests.
    """

    __slots__ = ("_content", "dataset_id", "identity", "receipt_identity", "scope")

    def __init__(
        self, token: object, *, content: dict[str, Any], identity: str, scope: str
    ) -> None:
        if token is not _INTAKE_TOKEN:
            raise LiveRoundIntakeError(
                "a live round intake may only be minted from a verified platform receipt and real "
                "download digests; canonical content has been checked against nothing"
            )
        self._content = dict(content)
        self.identity = identity
        #: ``production`` or ``fixture``. Carried so a test chain can never be mistaken for a live
        #: one further down, and checked by :func:`require_production_scope`.
        self.scope = scope
        self.receipt_identity = str(content["platform_receipt_identity"])
        self.dataset_id = live_dataset_id_for(
            chromosome=str(content["chromosome"]),
            identity_tuple_hash=str(content["identity_tuple_hash"]),
        )

    def __getattr__(self, name: str) -> Any:
        try:
            return self._content[name]
        except KeyError:
            raise AttributeError(name) from None

    def content(self) -> dict[str, Any]:
        return dict(self._content)

    def registry_identity(self) -> dict[str, Any]:
        """The registered identity an attestation for this round must match.

        For a research round that identity comes from ``catalog.dataset_registry`` and the
        attestation cites the registry snapshot it was matched against. A live round has no
        registry, and the document registering what its inputs are is this intake -- so the
        intake's own identity takes that role. The field keeps its exact meaning, "the identity of
        the registered input set this attestation was checked against"; only which document
        registers it is different.
        """
        return {
            "dataset_id": self.dataset_id,
            "round_id": str(self._content["round_id"]),
            "chromosome": str(self._content["chromosome"]),
            "bam_sha256": str(self._content["bam_sha256"]),
            "bai_sha256": str(self._content["bai_sha256"]),
            "reference_sha256": str(self._content["reference_sha256"]),
            "fai_sha256": str(self._content["fai_sha256"]),
            "region_hash": str(self._content["region_hash"]),
            "identity_tuple_hash": str(self._content["identity_tuple_hash"]),
            "registry_snapshot_hash": self.identity,
        }


def _build_verified_intake(*, receipt: Any, downloads: Any, scope: str) -> VerifiedLiveRoundIntake:
    """The single implementation. Both scopes run exactly this."""
    from minos_engine.common.genomic_region import normalize_region

    # the SCIENTIFIC link ...
    _require(
        downloads.receipt_identity == receipt.identity,
        "these downloads were accepted for a different round; a file fetched for one round is not "
        "evidence about another",
    )
    # ... and the RUNTIME one. The scientific identity excludes the operational URLs and the
    # platform's expected BAM hash, so two responses for the same round, region and endpoint share
    # an identity even when they offer different download sources. Matching the identity alone
    # would let downloads obtained under one of them be presented for the other.
    _require(
        downloads.operational_binding is receipt.operational_binding,
        "these downloads were obtained for a different round-status response; the scientific "
        "identity matches but the operational source does not, and provenance is about which "
        "response was actually served",
    )
    _require(
        downloads.scope == receipt.scope == scope,
        f"the receipt and its downloads must both be {scope} scope",
    )
    try:
        contig, _start0, _end0 = normalize_region(receipt.region_source, _ONE_BASED)
    except ValueError as error:  # pragma: no cover - the receipt already normalized it
        raise LiveRoundIntakeError(f"the receipt's region is not usable: {error}") from None
    reference = ACCEPTED_REFERENCE_IDENTITIES.get(contig)
    _require(
        reference is not None,
        f"this engine has no accepted reference identity for contig {contig!r}",
    )
    assert reference is not None

    content = canonical_live_round_content(
        round_id=receipt.round_id,
        region_source=receipt.region_source,
        platform_receipt_identity=receipt.identity,
        bam_sha256=downloads.bam_sha256,
        bai_sha256=downloads.bai_sha256,
        reference_sha256=reference.reference_sha256,
        fai_sha256=reference.fai_sha256,
    )
    observed = tuple(sorted(content))
    _require(
        observed == LIVE_INTAKE_FIELDS,
        f"the live intake carries {observed}, not exactly {LIVE_INTAKE_FIELDS}",
    )
    return VerifiedLiveRoundIntake(
        _INTAKE_TOKEN,
        content=content,
        identity=live_round_intake_identity(content),
        scope=scope,
    )


def verify_live_round_intake(*, receipt: Any, downloads: Any) -> VerifiedLiveRoundIntake:
    """THE production entry point. Production receipt and production downloads, or nothing.

    There is no ``content`` parameter and there is no way to supply one. The round id and region
    come from the platform receipt; the BAM and BAI identities come from files bound to that
    receipt's own download sources; the reference and its index come from the accepted per-contig
    table. A caller contributes nothing that ends up in the identity.

    A fixture receipt or fixture downloads are refused here by type. Tests use
    :func:`observe_fixture_live_round_intake`, which runs the identical builder in its own scope.
    """
    from minos_engine.protocol.round_status import (
        PRODUCTION_SCOPE,
        ProductionRoundDownloads,
        ProductionRoundStatusReceipt,
    )

    _require(
        isinstance(receipt, ProductionRoundStatusReceipt),
        "a production live intake requires a production platform receipt; a fixture observation, "
        "a demo round or a dictionary is not the platform speaking about a live round",
    )
    _require(
        isinstance(downloads, ProductionRoundDownloads),
        "a production live intake requires production round downloads bound to that receipt",
    )
    return _build_verified_intake(receipt=receipt, downloads=downloads, scope=PRODUCTION_SCOPE)


def observe_fixture_live_round_intake(*, receipt: Any, downloads: Any) -> VerifiedLiveRoundIntake:
    """The deterministic test seam. Same builder, same refusals, different scope."""
    from minos_engine.protocol.round_status import (
        FIXTURE_SCOPE,
        FixtureRoundDownloads,
        FixtureRoundStatusReceipt,
    )

    _require(
        isinstance(receipt, FixtureRoundStatusReceipt),
        "a fixture live intake requires a fixture round-status observation",
    )
    _require(
        isinstance(downloads, FixtureRoundDownloads),
        "a fixture live intake requires fixture round downloads",
    )
    return _build_verified_intake(receipt=receipt, downloads=downloads, scope=FIXTURE_SCOPE)


def require_production_scope(capability: Any) -> Any:
    """The guard the live service will use. A fixture-scoped chain never passes it."""
    from minos_engine.protocol.round_status import PRODUCTION_SCOPE

    scope = getattr(capability, "scope", None)
    _require(
        scope == PRODUCTION_SCOPE,
        f"this capability is {scope!r} scope; the live boundary accepts only {PRODUCTION_SCOPE}",
    )
    return capability
