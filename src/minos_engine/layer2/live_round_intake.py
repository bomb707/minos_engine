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
* :func:`observe_fixture_live_round_intake` is the test seam — the *same* builder, its own
  capability and its own private token.

so that::

    the platform proves WHICH ROUND and WHICH REGION were offered
    round-bound downloads prove WHICH BYTES came from that round's own sources
    the accepted reference table proves WHICH REFERENCE this engine profiles against

No field of a verified intake originates in a caller-chosen string, and no presigned URL — which
expires, carries a signature and varies between equivalent fetches — enters the scientific
identity. Production and fixture are two separate capabilities with two separate private tokens, so a
fixture chain can never be mistaken for -- or relabelled into -- a live one:
:func:`require_production_scope` is the guard the live service will use, and it asks for the exact
production type and its seal, never for a string.

``parameter_space_hash`` is deliberately absent. It defines what the controller may *do*, not what
the round *is*, and the controller already checks the request's parameter space against the
accepted live space. Binding it here would break the profile binding whenever the parameter space
moved even though the inputs had not.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Final

from minos_engine.common.canonical_json import canonical_json_bytes
from minos_engine.common.errors import MinosEngineError
from minos_engine.common.frozen_state import FrozenAfterMint, frozen_map
from minos_engine.common.hashing import canonical_hash, sha256_hex

__all__ = [
    "ACCEPTED_REFERENCE_IDENTITIES",
    "ACCEPTED_REFERENCE_SET_DOMAIN",
    "ACCEPTED_REFERENCE_SET_SCHEMA",
    "accepted_reference_set_content",
    "accepted_reference_set_identity",
    "FixtureLiveRoundIntake",
    "VerifiedProductionLiveRoundIntake",
    "is_fixture_intake",
    "is_verified_production_intake",
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


@dataclass(frozen=True, slots=True)
class ReferenceIdentity:
    """The reference FASTA and index this engine profiles a given contig against.

    **Immutable.** This is an accepted authority, not a configuration record: a live round's
    reference identity decides which genome the round is profiled against, and it enters the
    scientific identity. It used to be writable field by field, so ordinary code could do
    ``ACCEPTED_REFERENCE_IDENTITIES["chr20"].reference_sha256 = ...`` and change what this engine
    accepts as GRCh38 chr20 without touching source. ``typing.Final`` is a type-checker
    annotation and enforces nothing at runtime.
    """

    contig: str
    reference_sha256: str
    fai_sha256: str
    reference_m5: str

    def content(self) -> dict[str, str]:
        """Canonical, field-ordered. The audit representation; no runtime state in it."""
        return {
            "contig": self.contig,
            "fai_sha256": self.fai_sha256,
            "reference_m5": self.reference_m5,
            "reference_sha256": self.reference_sha256,
        }


#: Accepted per-contig reference identities. These are not caller input: a live round's reference
#: is whichever one this engine has been qualified against for that chromosome, and if the local
#: reference is a different build the round is refused rather than profiled against the wrong
#: genome. A unit test cross-checks every entry against the frozen L2-D corpus attestations, which
#: is where they come from -- all fifty members agree, one reference per chromosome.
_ACCEPTED_REFERENCE_IDENTITIES: Final[dict[str, ReferenceIdentity]] = {
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

#: Read-only. Insertion, deletion and replacement all raise, so the set of contigs this engine
#: accepts -- and which reference each one means -- can be changed only by changing this source.
ACCEPTED_REFERENCE_IDENTITIES: Final[Mapping[str, ReferenceIdentity]] = frozen_map(
    _ACCEPTED_REFERENCE_IDENTITIES
)

#: The chromosomes this engine profiles. A live round outside them is refused, never guessed.
SUPPORTED_CONTIGS: Final[tuple[str, ...]] = tuple(sorted(ACCEPTED_REFERENCE_IDENTITIES))

#: Audit only. A deterministic name for "the reference set this engine accepts", so a change to
#: it is visible in a test and in a review. Deliberately NOT a field of ``LIVE_INTAKE_SCHEMA``:
#: the intake already carries the per-round ``reference_sha256`` and ``fai_sha256`` it was
#: actually profiled against, and adding a set-wide identity to the schema would change what a
#: live intake means for no scientific reason.
ACCEPTED_REFERENCE_SET_SCHEMA: Final = "l2h-accepted-reference-set-v1"
ACCEPTED_REFERENCE_SET_DOMAIN: Final = "minos:l2h-accepted-reference-set:v1\n"


def accepted_reference_set_content(
    table: Mapping[str, ReferenceIdentity] | None = None,
) -> dict[str, Any]:
    """The accepted reference set as canonical data."""
    entries = ACCEPTED_REFERENCE_IDENTITIES if table is None else table
    return {
        "schema_version": ACCEPTED_REFERENCE_SET_SCHEMA,
        "contig_count": len(entries),
        "contigs": [entries[contig].content() for contig in sorted(entries)],
    }


def accepted_reference_set_identity(
    table: Mapping[str, ReferenceIdentity] | None = None,
) -> str:
    """Domain-separated identity of the accepted reference set. For audit and regression only."""
    return sha256_hex(
        ACCEPTED_REFERENCE_SET_DOMAIN.encode("utf-8")
        + canonical_json_bytes(accepted_reference_set_content(table))
    )


#: One token per authority domain. Production and fixture are different capabilities, not one
#: capability wearing a label -- a label is a string, and a string can be assigned.
_PRODUCTION_INTAKE_TOKEN: Final = object()
_FIXTURE_INTAKE_TOKEN: Final = object()


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


class VerifiedLiveRoundIntake(FrozenAfterMint):
    """Proof that the platform offered this round and that these are the bytes it names.

    Abstract: it has no token of its own and cannot be constructed. The two concrete capabilities
    below each demand their own module-private token, named directly in their own constructor.

    An earlier version held the expected token in a class attribute, ``_expected_token``, so
    ``VerifiedProductionLiveRoundIntake._expected_token`` handed the real production mint token to
    any caller that asked -- which made the whole production/fixture separation ornamental. No
    exported class carries its token any more; see :mod:`docs/layer2/L2H_CAPABILITY_TRUST_MODEL`
    for what that does and does not claim.
    """

    _frozen_error = LiveRoundIntakeError
    _frozen_noun = "live round intake"

    __slots__ = ("_content", "_frozen", "_seal", "dataset_id", "identity", "receipt_identity")

    #: Overridden by each concrete capability. A CLASS attribute, so it cannot be reassigned into
    #: another authority domain the way an instance attribute could.
    scope: str = ""

    def __init__(self, token: object, *, content: dict[str, Any], identity: str) -> None:
        # ``_mint_token`` is a module-level lookup keyed by exact type, not a class attribute, so
        # reading the class gives a caller nothing.
        expected = _mint_token(type(self))
        if expected is None or token is not expected:
            raise LiveRoundIntakeError(
                "a live round intake may only be minted from a verified platform receipt and real "
                "download digests; canonical content has been checked against nothing"
            )
        # a read-only view over a defensive copy: refusing ``intake.field = ...`` while internal
        # code could still write ``intake._content[...]`` would not be immutability.
        self._content = frozen_map(content)
        self.identity = identity
        self.receipt_identity = str(content["platform_receipt_identity"])
        self.dataset_id = live_dataset_id_for(
            chromosome=str(content["chromosome"]),
            identity_tuple_hash=str(content["identity_tuple_hash"]),
        )
        self._seal = token
        # LAST: nothing above can be rewritten from here on.
        self._freeze()

    def __getattr__(self, name: str) -> Any:
        # only content fields are proxied. Private names must never route here, or a lookup during
        # construction -- before ``_content`` exists -- recurses.
        if name.startswith("_"):
            raise AttributeError(name)
        try:
            content = object.__getattribute__(self, "_content")
        except AttributeError:
            raise AttributeError(name) from None
        try:
            return content[name]
        except KeyError:
            raise AttributeError(name) from None

    def content(self) -> dict[str, Any]:
        """A plain ``dict`` copy. Canonicalized downstream, so it must not be the frozen view."""
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


class VerifiedProductionLiveRoundIntake(VerifiedLiveRoundIntake):
    """The live authority. Only :func:`verify_live_round_intake` can mint one."""

    __slots__ = ()
    #: the transport's ``PRODUCTION_SCOPE``. Not imported at module scope (that import is kept
    #: function-local throughout this module); the builder proves the two agree by requiring
    #: ``downloads.scope == receipt.scope == <this class>.scope`` before anything is minted.
    scope = "production"


class FixtureLiveRoundIntake(VerifiedLiveRoundIntake):
    """The offline observation. Structurally identical, and a different capability entirely."""

    __slots__ = ()
    scope = "fixture"


#: Which token each concrete capability demands. A module-private mapping keyed by exact type:
#: reading an exported class yields nothing, and a subclass is absent rather than inheriting.
_MINT_TOKENS: Final[dict[type, object]] = {
    VerifiedProductionLiveRoundIntake: _PRODUCTION_INTAKE_TOKEN,
    FixtureLiveRoundIntake: _FIXTURE_INTAKE_TOKEN,
}


def _mint_token(cls: type) -> object | None:
    """The token for an EXACT class. Never inherited -- a subclass gets ``None`` and is refused."""
    return _MINT_TOKENS.get(cls)


def is_verified_production_intake(candidate: Any) -> bool:
    """Exact concrete type and private seal."""
    return (
        type(candidate) is VerifiedProductionLiveRoundIntake
        and getattr(candidate, "_seal", None) is _PRODUCTION_INTAKE_TOKEN
    )


def is_fixture_intake(candidate: Any) -> bool:
    return (
        type(candidate) is FixtureLiveRoundIntake
        and getattr(candidate, "_seal", None) is _FIXTURE_INTAKE_TOKEN
    )


def _validated_intake_content(*, receipt: Any, downloads: Any, scope: str) -> dict[str, Any]:
    """The single scientific implementation. It validates, and it **mints nothing**.

    Shared validation and authority minting used to be one generic function that took a factory
    and read the factory's own token off it. That put the mint token one attribute access away
    from any caller. They are separate concerns and are now separate functions: this one returns
    ordinary validated data, and the two scope-specific entry points below each name their own
    capability class and their own private token explicitly.
    """
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
    # would let downloads obtained under one of them be presented for the other. The receipt is
    # ASKED whether it owns them; the sentinel is never handed out.
    from minos_engine.protocol.round_status import receipt_owns_downloads

    _require(
        receipt_owns_downloads(receipt, downloads),
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
    return content


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
        is_verified_production_downloads,
        is_verified_production_receipt,
    )

    # exact type AND private seal, at every production boundary. `isinstance` would let a subclass
    # that skips __init__ and populates the slots by hand walk straight through.
    _require(
        is_verified_production_receipt(receipt),
        "a production live intake requires a sealed production platform receipt; a fixture "
        "observation, a demo round, a dictionary or a subclass of the receipt is not the platform "
        "speaking about a live round",
    )
    _require(
        is_verified_production_downloads(downloads),
        "a production live intake requires sealed production round downloads; a subclass carrying "
        "a real receipt identity and a real operational binding is a forgery, not provenance",
    )
    content = _validated_intake_content(
        receipt=receipt, downloads=downloads, scope=VerifiedProductionLiveRoundIntake.scope
    )
    return VerifiedProductionLiveRoundIntake(
        _PRODUCTION_INTAKE_TOKEN,
        content=content,
        identity=live_round_intake_identity(content),
    )


def observe_fixture_live_round_intake(*, receipt: Any, downloads: Any) -> VerifiedLiveRoundIntake:
    """The deterministic test seam. Same builder, same refusals, different scope."""
    from minos_engine.protocol.round_status import (
        is_fixture_round_downloads,
        is_fixture_round_receipt,
    )

    _require(
        is_fixture_round_receipt(receipt),
        "a fixture live intake requires a sealed fixture round-status observation",
    )
    _require(
        is_fixture_round_downloads(downloads),
        "a fixture live intake requires sealed fixture round downloads",
    )
    content = _validated_intake_content(
        receipt=receipt, downloads=downloads, scope=FixtureLiveRoundIntake.scope
    )
    return FixtureLiveRoundIntake(
        _FIXTURE_INTAKE_TOKEN,
        content=content,
        identity=live_round_intake_identity(content),
    )


def require_production_scope(intake: Any) -> VerifiedLiveRoundIntake:
    """The guard the live service will use. Exact type, private seal, production scope.

    A fixture-scoped chain never passes, and neither does a subclass of the intake that copies the
    scope string: the seal is set only by the constructor a caller cannot reach.
    """
    from minos_engine.protocol.round_status import PRODUCTION_SCOPE

    _require(
        is_verified_production_intake(intake),
        "the live boundary accepts only a sealed PRODUCTION live intake; a fixture observation is "
        "a different capability, and no amount of relabelling turns one into the other",
    )
    verified: VerifiedLiveRoundIntake = intake
    # explicit, not `assert`: `python -O` removes asserts, and the production/fixture boundary may
    # not depend on whether the interpreter was started with optimizations.
    _require(
        verified.scope == PRODUCTION_SCOPE,
        f"this intake reports {verified.scope!r} scope, not {PRODUCTION_SCOPE!r}",
    )
    return verified
