"""Platform round-status receipts and round-bound download provenance.

Two authority gaps are closed here.

**A fixture is not the platform.** Previously any ``RoundStatusTransport`` subclass could mint the
one receipt class, and the production endpoint check was ``startswith("/")`` -- so a fixture, and
even ``/v2/demo/round-status``, produced a capability the production intake accepted. "Some
allowed transport object returned this" is not "the authenticated production platform returned
this". The two are now different capabilities with different private tokens:

===========================  ==========================  ================================
factory                      transport it demands        capability it mints
===========================  ==========================  ================================
verify_production_round_...  ProductionRoundStatus...     ProductionRoundStatusReceipt
observe_fixture_round_...    FixtureRoundStatusTransport  FixtureRoundStatusReceipt
===========================  ==========================  ================================

Neither can mint the other's, inheriting the base transport grants nothing, and the production
factory additionally requires the endpoint to be exactly ``/v2/round-status`` -- demo and sandbox
routes are refused outright. Both factories share :func:`parse_round_status`, so the parsing and
canonicalization a test exercises is literally the production code; only the authority token
differs, and a test asserts the two produce identical parsed content.

**Hashing a local file proves nothing about a round.** ``hash_downloaded_inputs`` used to accept
any two paths, so a BAM unrelated to the round sailed through intake and was only caught later,
against the profile. Downloads are now *round-bound*: :class:`RoundDownloads` is minted only by
handing over the exact URL the file came from, and that URL must be one the receipt actually
carries for that round. Substituting the file, the URL or the receipt all fail here, before an
intake exists.

**The engine does not download.** The official miner already does, with backup-URL fallback,
S3/HTTP handling, caching and optional platform-supplied SHA-256 verification
(``minos_subnet/utils/file_utils.py``, ``neurons/miner.py::_download_bam``). Reimplementing that
would duplicate maintained security logic and drag HTTP and S3 clients into this package. So the
seam is a **handoff**: the miner downloads, and the engine binds *this receipt* to *this URL* to
*this local file* to *these bytes*.

**What a receipt asserts.** The miner signs the REQUEST (hotkey signature, nonce, timestamp,
``X-Minos-Auth-Version: 2``) and the transport is HTTPS-enforced by the subnet client's own
constructor. The response body carries **no digital signature** -- nothing in the subnet verifies
one. A receipt therefore asserts exactly: *this payload was returned by the configured,
HTTPS-protected platform transport in answer to a request this miner signed.* Nothing more.
"""

from __future__ import annotations

import hashlib
from abc import ABC, abstractmethod
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Final, final

from minos_engine.common.canonical_json import canonical_json_bytes
from minos_engine.common.errors import MinosEngineError
from minos_engine.common.hashing import sha256_hex

__all__ = [
    "ACTIVE_ROUND_STATUSES",
    "BAI_URL_SLOTS",
    "BAM_URL_SLOTS",
    "FIXTURE_SCOPE",
    "LOCALLY_INDEXED",
    "OFFICIAL_MINER_DOWNLOAD",
    "PLATFORM_ROUND_STATUS_DOMAIN",
    "PLATFORM_ROUND_STATUS_SCHEMA",
    "PRODUCTION_ENDPOINT_PATH",
    "PRODUCTION_SCOPE",
    "FixtureRoundDownloads",
    "FixtureRoundStatusReceipt",
    "FixtureRoundStatusTransport",
    "ParsedRoundStatus",
    "PlatformRoundStatusError",
    "ProductionRoundDownloads",
    "ProductionRoundStatusReceipt",
    "ProductionRoundStatusTransport",
    "RoundDownloads",
    "RoundStatusReceipt",
    "RoundStatusTransport",
    "VerifiedOfficialMiner",
    "VerifiedProductionPlatformClient",
    "accept_fixture_round_downloads",
    "PROVENANCE_SIDECAR_SCHEMA",
    "download_production_round_inputs",
    "is_fixture_round_downloads",
    "is_fixture_round_receipt",
    "is_verified_official_miner",
    "is_verified_production_downloads",
    "is_verified_production_receipt",
    "receipt_owns_downloads",
    "production_round_status_transport",
    "resolve_official_miner_type",
    "resolve_official_platform_client_type",
    "verify_official_miner",
    "verify_subnet_platform_client",
    "observe_fixture_round_status",
    "parse_round_status",
    "verify_production_round_status",
]

PLATFORM_ROUND_STATUS_SCHEMA: Final = "l2h-platform-round-status-receipt-v2"
PLATFORM_ROUND_STATUS_DOMAIN: Final = "minos:l2h-platform-round-status-receipt:v2\n"

#: The ONLY endpoint a production live receipt may come from. Exact, not a prefix: the demo route
#: ``/v2/demo/round-status`` is a sandbox that accepts ephemeral keypairs and never writes to the
#: live submissions database, so a decision may not be made from it.
PRODUCTION_ENDPOINT_PATH: Final = "/v2/round-status"
DEMO_ENDPOINT_PATH: Final = "/v2/demo/round-status"

PRODUCTION_SCOPE: Final = "production"
FIXTURE_SCOPE: Final = "fixture"

#: Round states in which a decision may legitimately be made.
ACTIVE_ROUND_STATUSES: Final[frozenset[str]] = frozenset({"open"})

#: The URL slots the platform offers, in the order the official miner reads them. Both a primary
#: and a backup exist per artifact; which is preferred is a miner-side backend choice.
BAM_URL_SLOTS: Final[tuple[str, ...]] = ("bam_presigned_url", "bam_presigned_url_backup")
BAI_URL_SLOTS: Final[tuple[str, ...]] = (
    "bam_index_presigned_url",
    "bam_index_presigned_url_backup",
)

#: The official miner builds the index with samtools when the platform offers none. That is a
#: legitimate provenance and is recorded as such rather than disguised as a download.
LOCALLY_INDEXED: Final = "locally-indexed"

#: Production provenance: the maintained ``Miner._download_bam`` operation produced these bytes.
#: Which URL slot it chose is its own decision, made by rules this engine does not duplicate.
OFFICIAL_MINER_DOWNLOAD: Final = "official-miner-download"

#: Fields the receipt identity is derived from. URLs, timings, nonces, signatures and mutation
#: counts are deliberately absent: they are operational, per-fetch, or secret-bearing.
RECEIPT_IDENTITY_FIELDS: Final[tuple[str, ...]] = (
    "endpoint_path",
    "region_source",
    "round_id",
    "schema_version",
)


class PlatformRoundStatusError(MinosEngineError):
    """The platform did not offer a round this engine may decide for. Emit nothing."""


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise PlatformRoundStatusError(message)


def _is_sealed(candidate: Any, expected_type: type, token: object) -> bool:
    """The ONE rule every production capability crosses a trust boundary under.

    ``isinstance`` is not enough anywhere: a subclass can skip ``__init__``, populate the slots by
    hand, and satisfy it without the private token ever having minted anything. So the check is
    **exact concrete type** -- a subclass is a different type -- **and** the private seal, which
    only the real constructor sets and which a caller cannot obtain.
    """
    return type(candidate) is expected_type and getattr(candidate, "_seal", None) is token


# --------------------------------------------------------------------------- #
# shared parsing: identical for every scope, so a test exercises production code
# --------------------------------------------------------------------------- #
class ParsedRoundStatus:
    """The validated content of a round-status payload. Carries **no authority**.

    Producing one says the payload is well formed and describes an open round. It says nothing
    about who returned it, which is exactly why it is not a capability.
    """

    __slots__ = ("endpoint_path", "expected_bam_sha256", "region_source", "round_id", "urls")

    def __init__(
        self,
        *,
        round_id: str,
        region_source: str,
        endpoint_path: str,
        urls: dict[str, str],
        expected_bam_sha256: str | None,
    ) -> None:
        self.round_id = round_id
        self.region_source = region_source
        self.endpoint_path = endpoint_path
        #: Operational only. Never enters an identity, a report or a decision.
        self.urls = dict(urls)
        self.expected_bam_sha256 = expected_bam_sha256

    def identity_content(self) -> dict[str, Any]:
        return {
            "schema_version": PLATFORM_ROUND_STATUS_SCHEMA,
            "round_id": self.round_id,
            "region_source": self.region_source,
            "endpoint_path": self.endpoint_path,
        }


def parse_round_status(payload: Any, *, endpoint_path: str) -> ParsedRoundStatus:
    """Validate a round-status payload. The single implementation, used by every scope."""
    from minos_engine.common.genomic_region import (
        ONE_BASED_INCLUSIVE,
        normalize_region,
        validate_round_identifier,
    )

    _require(
        isinstance(payload, Mapping) and bool(payload),
        "the platform returned an empty or non-object round status",
    )
    assert isinstance(payload, Mapping)
    _require(
        bool(payload.get("has_active_round")),
        "the platform reports no active round; there is nothing to decide for",
    )
    status = payload.get("status")
    _require(
        isinstance(status, str) and status in ACTIVE_ROUND_STATUSES,
        f"the round status is {status!r}; a decision may only be made for a round in "
        f"{sorted(ACTIVE_ROUND_STATUSES)}",
    )
    raw_round_id = payload.get("round_id")
    _require(
        isinstance(raw_round_id, str) and bool(raw_round_id),
        "the platform round status carries no round_id",
    )
    assert isinstance(raw_round_id, str)
    try:
        round_id = validate_round_identifier(raw_round_id)
    except ValueError as error:
        raise PlatformRoundStatusError(f"the platform round_id is unusable: {error}") from None

    raw_region = payload.get("region")
    _require(
        isinstance(raw_region, str) and bool(raw_region.strip()),
        "the platform round status carries no region",
    )
    assert isinstance(raw_region, str)
    region_source = raw_region.strip()
    try:
        normalize_region(region_source, ONE_BASED_INCLUSIVE)
    except ValueError as error:
        raise PlatformRoundStatusError(f"the platform region is unusable: {error}") from None

    urls: dict[str, str] = {}
    for slot in (*BAM_URL_SLOTS, *BAI_URL_SLOTS):
        value = payload.get(slot)
        if isinstance(value, str) and value.strip():
            urls[slot] = value.strip()
    _require(
        any(slot in urls for slot in BAM_URL_SLOTS),
        "the round offers no BAM URL; there is nothing to download and nothing to decide on",
    )

    expected = payload.get("bam_sha256")
    expected_bam_sha256 = None
    if isinstance(expected, str) and expected.strip():
        candidate = expected.strip().lower()
        _require(
            len(candidate) == 64 and all(c in "0123456789abcdef" for c in candidate),
            "the platform's bam_sha256 is not a SHA-256",
        )
        expected_bam_sha256 = candidate

    _require(
        isinstance(endpoint_path, str) and endpoint_path.startswith("/"),
        "the transport did not report the path it called",
    )
    return ParsedRoundStatus(
        round_id=round_id,
        region_source=region_source,
        endpoint_path=endpoint_path,
        urls=urls,
        expected_bam_sha256=expected_bam_sha256,
    )


# --------------------------------------------------------------------------- #
# transports
# --------------------------------------------------------------------------- #
#: Where the official subnet code lives. The engine imports the REAL classes and checks identity
#: against them; it does not decide what is official by reading a class name.
OFFICIAL_CLIENT_MODULE: Final = "utils.platform_client"
OFFICIAL_CLIENT_CLASS: Final = "MinerPlatformClient"
OFFICIAL_MINER_MODULE: Final = "neurons.miner"
OFFICIAL_MINER_CLASS: Final = "Miner"

#: Deployment assumption, stated rather than implied: the subnet package must be importable by the
#: process making live decisions -- installed, on ``PYTHONPATH``, or located by this variable. A
#: miner already satisfies this, because it *is* the subnet process. If it cannot be imported the
#: production path FAILS CLOSED; there is deliberately no structural fallback, because a fallback
#: is exactly the hole this closes.
SUBNET_ROOT_ENV: Final = "MINOS_SUBNET_ROOT"


def _resolve_official_type(module_name: str, class_name: str) -> type:
    """Import the REAL class object, or refuse. Never a name, never a duck type."""
    import importlib
    import os
    import sys

    root = os.environ.get(SUBNET_ROOT_ENV)
    if root and root not in sys.path:
        sys.path.insert(0, root)
    try:
        module = importlib.import_module(module_name)
    except Exception as error:
        raise PlatformRoundStatusError(
            f"the official subnet module {module_name!r} cannot be imported "
            f"({type(error).__name__}: {error}); a live decision requires the real subnet "
            f"package to be importable, and this path does not fall back to matching class names. "
            f"Install the subnet package or set {SUBNET_ROOT_ENV}."
        ) from None
    resolved = getattr(module, class_name, None)
    if not isinstance(resolved, type):
        raise PlatformRoundStatusError(
            f"{module_name}.{class_name} is not a class in the installed subnet package"
        )
    return resolved


def resolve_official_platform_client_type() -> type:
    """The real ``utils.platform_client.MinerPlatformClient``."""
    return _resolve_official_type(OFFICIAL_CLIENT_MODULE, OFFICIAL_CLIENT_CLASS)


def resolve_official_miner_type() -> type:
    """The real ``neurons.miner.Miner``, which owns the maintained download operation."""
    return _resolve_official_type(OFFICIAL_MINER_MODULE, OFFICIAL_MINER_CLASS)


_CLIENT_TOKEN: Final = object()
_MINER_TOKEN: Final = object()
_TRANSPORT_TOKEN: Final = object()


@final
class VerifiedProductionPlatformClient:
    """The real, authenticated subnet client, verified by TYPE and by configuration.

    Minted only by :func:`verify_subnet_platform_client`. The previous version accepted any object
    whose class was *named* ``MinerPlatformClient`` in a module *named* ``platform_client`` -- and
    a test constructed exactly that and was accepted. Names are not identity, so the real class is
    imported and ``isinstance`` is checked against it.
    """

    __slots__ = ("_client", "_seal")

    def __init__(self, token: object, *, client: Any) -> None:
        if token is not _CLIENT_TOKEN:
            raise PlatformRoundStatusError(
                "a production platform client may only be minted by verifying a real one"
            )
        self._client = client
        self._seal = _CLIENT_TOKEN

    @property
    def client(self) -> Any:
        return self._client


def verify_subnet_platform_client(client: Any) -> VerifiedProductionPlatformClient:
    """Exact type identity first, then the conditions that make the client usable live.

    Trust statement, unchanged and not overstated: the miner signs the REQUEST (hotkey signature,
    nonce, timestamp, ``X-Minos-Auth-Version: 2``); HTTPS authenticates and protects the configured
    transport; the response body is **not** digitally signed.
    """
    official = resolve_official_platform_client_type()
    # EXACT type, like every other authority here. A subclass of the official client can override
    # get_round_status and would otherwise earn a genuine sealed capability -- and a subclass is
    # not the runtime integration object this authority claims to authenticate.
    _require(
        type(client) is official,
        f"the production transport requires an actual {OFFICIAL_CLIENT_MODULE}."
        f"{OFFICIAL_CLIENT_CLASS}; an object of type {type(client).__name__!r} is not one, "
        "whatever it is called or inherits from",
    )
    _require(
        not getattr(client, "demo", False),
        "this client is in demo mode, which routes to the sandboxed /v2/demo namespace; a live "
        "decision may not be made from a demo round",
    )
    keypair = getattr(client, "keypair", None)
    _require(
        bool(getattr(keypair, "ss58_address", "")),
        "the platform client has no hotkey to sign the request with",
    )
    base_url = str(getattr(getattr(client, "config", None), "base_url", ""))
    _require(
        base_url.startswith("https://"),
        "the configured platform URL is not HTTPS; the subnet client enforces this and so does "
        "this verification",
    )
    return VerifiedProductionPlatformClient(_CLIENT_TOKEN, client=client)


@final
class VerifiedOfficialMiner:
    """The real ``neurons.miner.Miner``, which owns the maintained download operation."""

    __slots__ = ("_miner", "_seal")

    def __init__(self, token: object, *, miner: Any) -> None:
        if token is not _MINER_TOKEN:
            raise PlatformRoundStatusError(
                "a production download owner may only be minted by verifying a real miner"
            )
        self._miner = miner
        self._seal = _MINER_TOKEN

    @property
    def miner(self) -> Any:
        return self._miner


def is_verified_official_miner(candidate: Any) -> bool:
    """Exact type and seal. A subclass of the capability is not the capability."""
    return bool(_is_sealed(candidate, VerifiedOfficialMiner, _MINER_TOKEN))


def verify_official_miner(miner: Any) -> VerifiedOfficialMiner:
    """Exact type identity against the real Miner. An object with ``_download_bam`` is not one."""
    official = resolve_official_miner_type()
    # EXACT type: a subclass could override _download_bam and hand back any file it liked.
    _require(
        type(miner) is official,
        f"the production download integration requires an actual {OFFICIAL_MINER_MODULE}."
        f"{OFFICIAL_MINER_CLASS}; an object of type {type(miner).__name__!r} is not one, "
        "whatever it inherits from",
    )
    _require(
        callable(getattr(miner, "_download_bam", None)),
        "the official miner does not expose its download operation",
    )
    return VerifiedOfficialMiner(_MINER_TOKEN, miner=miner)


class RoundStatusTransport(ABC):
    """Something that can ask for a round status. Inheriting this grants NO authority."""

    transport_kind: str = "abstract"

    @abstractmethod
    def endpoint_path(self) -> str:
        """The path that was called."""

    @abstractmethod
    def fetch_round_status(self) -> Mapping[str, Any]:
        """Return the platform's round-status payload verbatim."""


@final
class ProductionRoundStatusTransport(RoundStatusTransport):
    """The one transport that may speak for the live platform.

    **Inheritance is not authority.** The previous version required only
    ``isinstance(transport, ProductionRoundStatusTransport)``, and that class was a public
    subclassing point -- so a caller could subclass it, return whatever payload it liked from
    ``fetch_round_status``, and mint a genuine production receipt. The class is now ``@final``, its
    constructor demands a module-private token, it carries a seal only that constructor sets, and
    the verifier checks **exact type identity** rather than ``isinstance``. A subclass is a
    different type; a subclass that skips ``__init__`` has no seal; a subclass that calls
    ``super().__init__`` needs a token it cannot reach.

    The engine does not reimplement the HTTP call. Re-deriving the request signing, nonce and auth
    headers would fork a security boundary maintained next door and would need the miner's keypair.
    """

    __slots__ = ("_seal", "_verified_client")

    transport_kind = "subnet-platform-client"

    def __init__(self, token: object, *, verified_client: VerifiedProductionPlatformClient) -> None:
        if token is not _TRANSPORT_TOKEN:
            raise PlatformRoundStatusError(
                "a production transport may only be minted from a verified subnet platform "
                "client; subclassing this type grants nothing"
            )
        self._verified_client = verified_client
        self._seal = _TRANSPORT_TOKEN

    def endpoint_path(self) -> str:
        return PRODUCTION_ENDPOINT_PATH

    def fetch_round_status(self) -> Mapping[str, Any]:
        import asyncio

        try:
            asyncio.get_running_loop()
        except RuntimeError:
            pass
        else:
            raise PlatformRoundStatusError(
                "the platform round status cannot be fetched from inside a running event loop"
            )
        try:
            payload = asyncio.run(self._verified_client.client.get_round_status())
        except PlatformRoundStatusError:
            raise
        except Exception as error:
            raise PlatformRoundStatusError(
                f"the platform round status could not be retrieved: {type(error).__name__}"
            ) from None
        _require(isinstance(payload, Mapping), "the platform returned a non-object round status")
        assert isinstance(payload, Mapping)
        return payload


def production_round_status_transport(client: Any) -> ProductionRoundStatusTransport:
    """Verify the real subnet client, then mint the only transport production accepts."""
    return ProductionRoundStatusTransport(
        _TRANSPORT_TOKEN, verified_client=verify_subnet_platform_client(client)
    )


class FixtureRoundStatusTransport(RoundStatusTransport):
    """A deterministic transport for tests. Deliberately NOT a production transport."""

    transport_kind = "deterministic-fixture"

    def __init__(
        self, payload: Mapping[str, Any], *, endpoint_path: str = PRODUCTION_ENDPOINT_PATH
    ) -> None:
        self._payload = dict(payload)
        self._endpoint_path = endpoint_path

    def endpoint_path(self) -> str:
        return self._endpoint_path

    def fetch_round_status(self) -> Mapping[str, Any]:
        return dict(self._payload)


# --------------------------------------------------------------------------- #
# receipts: one capability per scope, one token each
# --------------------------------------------------------------------------- #
_PRODUCTION_RECEIPT_TOKEN: Final = object()
_FIXTURE_RECEIPT_TOKEN: Final = object()


class RoundStatusReceipt:
    """Common shape. Neither subclass can be built without its own scope's token."""

    __slots__ = ("_operational_binding", "_parsed", "_seal", "identity", "scope")

    def __init__(self, token: object, *, parsed: ParsedRoundStatus, expected: object) -> None:
        if token is not expected:
            raise PlatformRoundStatusError(
                "a round-status receipt may only be minted by fetching one through the transport "
                "its scope requires; a dictionary of round fields is not a statement by anyone"
            )
        self._parsed = parsed
        #: A per-INSTANCE sentinel. The scientific identity deliberately excludes the operational
        #: URLs and the platform's expected BAM hash, so two responses for the same round, region
        #: and endpoint share one identity even when they offer different download sources. This
        #: object distinguishes them at runtime. It is an ``object()``: it cannot be serialized,
        #: compared across processes, or leak into evidence -- which is the point.
        self._operational_binding = object()
        #: Set only here, by the constructor a caller cannot reach.
        self._seal = expected
        self.identity = sha256_hex(
            PLATFORM_ROUND_STATUS_DOMAIN.encode("utf-8")
            + canonical_json_bytes(parsed.identity_content())
        )
        self.scope = ""

    @property
    def round_id(self) -> str:
        return self._parsed.round_id

    @property
    def region_source(self) -> str:
        return self._parsed.region_source

    @property
    def endpoint_path(self) -> str:
        return self._parsed.endpoint_path

    @property
    def expected_bam_sha256(self) -> str | None:
        return self._parsed.expected_bam_sha256

    def _url_for(self, slot: str) -> str | None:
        """Operational only. Kept private so a URL cannot drift into an identity or a report."""
        return self._parsed.urls.get(slot)

    def _owns_downloads(self, downloads: Any) -> bool:
        """Does this exact receipt INSTANCE own those downloads?

        The sentinel is deliberately not a public attribute. Exposing it would hand a caller the
        one ingredient a fabricated proof is missing, and nothing outside this module needs to
        hold it -- callers ask the question instead of fetching the answer.
        """
        return getattr(downloads, "_operational_binding", None) is self._operational_binding

    def _operational_round_data(self) -> dict[str, Any]:
        """The round fields the official downloader reads. Private, and never returned publicly."""
        data: dict[str, Any] = dict(self._parsed.urls)
        if self._parsed.expected_bam_sha256 is not None:
            data["bam_sha256"] = self._parsed.expected_bam_sha256
        return data

    def offered_slots(self) -> tuple[str, ...]:
        """Which URL slots this round offers -- names only, never the URLs themselves."""
        return tuple(sorted(self._parsed.urls))

    def content(self) -> dict[str, Any]:
        return self._parsed.identity_content()

    def observation(self) -> dict[str, Any]:
        return {**self.content(), "scope": self.scope, "offered_slots": list(self.offered_slots())}


class ProductionRoundStatusReceipt(RoundStatusReceipt):
    """The platform, over the authenticated production transport, at the production endpoint."""

    __slots__ = ()

    def __init__(self, token: object, *, parsed: ParsedRoundStatus) -> None:
        super().__init__(token, parsed=parsed, expected=_PRODUCTION_RECEIPT_TOKEN)
        object.__setattr__(self, "scope", PRODUCTION_SCOPE)


class FixtureRoundStatusReceipt(RoundStatusReceipt):
    """A deterministic observation for tests. Never accepted on the production path."""

    __slots__ = ()

    def __init__(self, token: object, *, parsed: ParsedRoundStatus) -> None:
        super().__init__(token, parsed=parsed, expected=_FIXTURE_RECEIPT_TOKEN)
        object.__setattr__(self, "scope", FIXTURE_SCOPE)


def is_verified_production_receipt(candidate: Any) -> bool:
    """Exact type and seal. A ``ProductionRoundStatusReceipt`` subclass is refused."""
    return _is_sealed(candidate, ProductionRoundStatusReceipt, _PRODUCTION_RECEIPT_TOKEN)


def is_verified_production_downloads(candidate: Any) -> bool:
    """Exact type and seal. The most important one: a hand-populated subclass carrying a real
    receipt identity and a real operational binding must still be refused."""
    return _is_sealed(candidate, ProductionRoundDownloads, _PRODUCTION_DOWNLOAD_TOKEN)


def is_fixture_round_receipt(candidate: Any) -> bool:
    return _is_sealed(candidate, FixtureRoundStatusReceipt, _FIXTURE_RECEIPT_TOKEN)


def is_fixture_round_downloads(candidate: Any) -> bool:
    return _is_sealed(candidate, FixtureRoundDownloads, _FIXTURE_DOWNLOAD_TOKEN)


def receipt_owns_downloads(receipt: Any, downloads: Any) -> bool:
    """Ask the receipt whether it owns those downloads, without handing out the sentinel."""
    owner = getattr(receipt, "_owns_downloads", None)
    return bool(callable(owner) and owner(downloads))


def verify_production_round_status(transport: Any) -> ProductionRoundStatusReceipt:
    """Mint a PRODUCTION receipt. Only the real transport, only the production endpoint."""
    _require(
        type(transport) is ProductionRoundStatusTransport
        and getattr(transport, "_seal", None) is _TRANSPORT_TOKEN,
        "a production live receipt requires the sealed production transport; subclassing it, "
        "implementing its methods, or inheriting the base transport is not authority to speak for "
        "the platform",
    )
    endpoint_path = transport.endpoint_path()
    _require(
        endpoint_path == PRODUCTION_ENDPOINT_PATH,
        f"a production live receipt requires exactly {PRODUCTION_ENDPOINT_PATH}, not "
        f"{endpoint_path!r}"
        + (
            "; the demo namespace is a sandbox and may not authorise a live decision"
            if endpoint_path == DEMO_ENDPOINT_PATH
            else ""
        ),
    )
    parsed = parse_round_status(transport.fetch_round_status(), endpoint_path=endpoint_path)
    return ProductionRoundStatusReceipt(_PRODUCTION_RECEIPT_TOKEN, parsed=parsed)


def observe_fixture_round_status(transport: Any) -> FixtureRoundStatusReceipt:
    """Mint a FIXTURE observation. Same parsing, deliberately different authority."""
    _require(
        type(transport) is FixtureRoundStatusTransport,
        "a fixture observation requires a fixture transport",
    )
    parsed = parse_round_status(
        transport.fetch_round_status(), endpoint_path=transport.endpoint_path()
    )
    return FixtureRoundStatusReceipt(_FIXTURE_RECEIPT_TOKEN, parsed=parsed)


# --------------------------------------------------------------------------- #
# round-bound download provenance
# --------------------------------------------------------------------------- #
_PRODUCTION_DOWNLOAD_TOKEN: Final = object()
_FIXTURE_DOWNLOAD_TOKEN: Final = object()


class RoundDownloads:
    """Proof that these bytes came from THIS round's own download sources.

    Bound to the receipt identity, so downloads accepted for one round cannot be presented for
    another, and to the URL the file actually came from, so a source swapped after the receipt is
    refused. The URLs themselves stay here and never reach an identity or a report -- a presigned
    URL expires, carries a signature, and varies between equivalent fetches.
    """

    __slots__ = (
        "bai_sha256",
        "bai_source_slot",
        "bam_sha256",
        "bam_source_slot",
        "_operational_binding",
        "_seal",
        "byte_counts",
        "receipt_identity",
        "scope",
    )

    def __init__(
        self,
        token: object,
        *,
        expected: object,
        scope: str,
        receipt_identity: str,
        operational_binding: object,
        bam_sha256: str,
        bai_sha256: str,
        bam_source_slot: str,
        bai_source_slot: str,
        byte_counts: dict[str, int],
    ) -> None:
        if token is not expected:
            raise PlatformRoundStatusError(
                "round downloads may only be minted by handing over the exact source each file "
                "came from; two local paths are not provenance"
            )
        self._seal = expected
        self.scope = scope
        self.receipt_identity = receipt_identity
        #: The exact receipt INSTANCE these files were obtained for. Private: see
        #: ``RoundStatusReceipt._owns_downloads``.
        self._operational_binding = operational_binding
        self.bam_sha256 = bam_sha256
        self.bai_sha256 = bai_sha256
        self.bam_source_slot = bam_source_slot
        self.bai_source_slot = bai_source_slot
        self.byte_counts = dict(byte_counts)

    def observation(self) -> dict[str, Any]:
        """Slot NAMES only. No URL, no query string, no signature."""
        return {
            "scope": self.scope,
            "receipt_identity": self.receipt_identity,
            "bam_source_slot": self.bam_source_slot,
            "bai_source_slot": self.bai_source_slot,
            "byte_counts": dict(self.byte_counts),
        }


class ProductionRoundDownloads(RoundDownloads):
    __slots__ = ()


class FixtureRoundDownloads(RoundDownloads):
    __slots__ = ()


def _stream_sha256(path: Path, *, label: str) -> tuple[str, int]:
    _require(path.is_absolute(), f"the downloaded {label} must be an absolute path: {path}")
    _require(path.is_file(), f"the downloaded {label} is missing: {path}")
    _require(not path.is_symlink(), f"the downloaded {label} is a symlink: {path}")
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        while chunk := handle.read(1 << 20):
            digest.update(chunk)
            size += len(chunk)
    _require(size > 0, f"the downloaded {label} is empty: {path}")
    return digest.hexdigest(), size


def _match_slot(
    receipt: RoundStatusReceipt, *, source_url: str, slots: tuple[str, ...], label: str
) -> str:
    """Find which of THIS round's URL slots the file came from. No match is a refusal."""
    _require(
        isinstance(source_url, str) and bool(source_url.strip()),
        f"the {label} source URL is empty; the engine must be told where the bytes came from",
    )
    candidate = source_url.strip()
    for slot in slots:
        offered = receipt._url_for(slot)  # noqa: SLF001 - the receipt is the authority here
        if offered is not None and offered == candidate:
            return slot
    raise PlatformRoundStatusError(
        f"the {label} was fetched from a URL this round does not offer; a source swapped after "
        f"the round status was received is not this round's {label}"
    )


def _accept_downloads(
    *,
    token: object,
    expected: object,
    factory: type[RoundDownloads],
    scope: str,
    receipt: RoundStatusReceipt,
    bam_source_url: str,
    bam_path: Any,
    bai_path: Any,
    bai_source_url: str | None,
) -> RoundDownloads:
    bam_slot = _match_slot(receipt, source_url=bam_source_url, slots=BAM_URL_SLOTS, label="BAM")
    if bai_source_url is None:
        # the official miner builds the index with samtools when the platform offers none
        bai_slot = LOCALLY_INDEXED
    else:
        bai_slot = _match_slot(
            receipt, source_url=bai_source_url, slots=BAI_URL_SLOTS, label="BAM index"
        )

    bam_sha, bam_size = _stream_sha256(Path(bam_path), label="BAM")
    bai_sha, bai_size = _stream_sha256(Path(bai_path), label="BAM index")
    _require(bam_sha != bai_sha, "the BAM and its index cannot be the same bytes")
    expected_bam = receipt.expected_bam_sha256
    if expected_bam is not None:
        _require(
            bam_sha == expected_bam,
            "the downloaded BAM does not hash to the SHA-256 the platform published for this round",
        )
    return factory(
        token,
        expected=expected,
        scope=scope,
        receipt_identity=receipt.identity,
        operational_binding=receipt._operational_binding,  # noqa: SLF001
        bam_sha256=bam_sha,
        bai_sha256=bai_sha,
        bam_source_slot=bam_slot,
        bai_source_slot=bai_slot,
        byte_counts={"bam": bam_size, "bai": bai_size},
    )


#: Written by this integration beside the BAM the official miner produced. It is the engine's own
#: record and does not modify the subnet: nothing upstream reads it.
PROVENANCE_SIDECAR_SUFFIX: Final = ".minos-provenance.json"
PROVENANCE_SIDECAR_SCHEMA: Final = "l2h-live-download-provenance-v1"


def _operational_source_digest(receipt: RoundStatusReceipt) -> str:
    """A digest of the exact operational sources this round offered.

    A digest, not the URLs: the sidecar must be able to say "the same sources as before" without
    ever writing a presigned URL, its signature or its query string to disk.
    """
    return sha256_hex(
        b"minos:l2h-live-download-source-set:v1\n"
        + canonical_json_bytes(receipt._operational_round_data())  # noqa: SLF001
    )


def _sidecar_path(bam_path: Path) -> Path:
    return Path(str(bam_path) + PROVENANCE_SIDECAR_SUFFIX)


def _official_output_bam_path(round_id: str) -> Path | None:
    """Where the official miner will put this round's BAM, derived from ITS OWN values.

    ``Miner._download_bam`` computes ``BASE_DIR / "output" / safe_round_dir_name(round_id) /
    "input.bam"``. Both halves are read from the official modules -- ``BASE_DIR`` as imported into
    ``neurons.miner``, and ``safe_round_dir_name`` from ``utils.path_utils`` -- so nothing about
    backend selection, fallback, downloading or indexing is reimplemented. Only *where the file
    lands* is derived, and only so its state can be observed before the call.

    Returns ``None`` when the layout cannot be read; the caller then treats freshness as unknown
    rather than guessing.
    """
    import importlib

    try:
        miner_module = importlib.import_module(OFFICIAL_MINER_MODULE)
        path_utils = importlib.import_module("utils.path_utils")
        base_dir = miner_module.BASE_DIR
        safe_name = path_utils.safe_round_dir_name
    except Exception:
        return None
    try:
        return Path(base_dir) / "output" / str(safe_name(round_id)) / "input.bam"
    except Exception:
        return None


class _FileState:
    """Enough of a file's inode state to tell "rewritten" from "left alone"."""

    __slots__ = ("ctime_ns", "device", "inode", "mtime_ns", "size")

    inode: int
    device: int
    size: int
    mtime_ns: int
    ctime_ns: int

    def __init__(self, stat: Any) -> None:
        self.inode = int(stat.st_ino)
        self.device = int(stat.st_dev)
        self.size = int(stat.st_size)
        self.mtime_ns = int(stat.st_mtime_ns)
        self.ctime_ns = int(stat.st_ctime_ns)

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, _FileState):
            return NotImplemented
        return (
            self.inode == other.inode
            and self.device == other.device
            and self.size == other.size
            and self.mtime_ns == other.mtime_ns
            and self.ctime_ns == other.ctime_ns
        )

    def __hash__(self) -> int:  # pragma: no cover - equality is the point
        return hash((self.inode, self.device, self.size, self.mtime_ns, self.ctime_ns))


def _observe(path: Path | None) -> _FileState | None:
    if path is None:
        return None
    try:
        return _FileState(path.stat())
    except OSError:
        return None


def download_production_round_inputs(*, receipt: Any, miner: Any) -> ProductionRoundDownloads:
    """Have the OFFICIAL miner download this round, then prove the bytes are this round's.

    **Why there is no ``bam_source_url`` or ``bam_path`` here.** An earlier API took a URL string
    and a local path, checked the string against the round's offered URLs, and hashed whatever file
    it was pointed at. Those two facts never met. So the production path takes neither: it hands
    the round's own operational data to ``neurons.miner.Miner._download_bam`` -- the maintained
    operation that selects primary or backup by ``STORAGE_PRIMARY_BACKEND``, falls back, passes the
    platform's ``bam_sha256`` to the verified downloader, fetches the index when one is offered and
    builds it with samtools when it is not -- and hashes exactly what that operation produced.

    **The cache problem, and why a sidecar closes it.** ``download_file_verified`` returns an
    existing file untouched when no ``expected_sha256`` is supplied ("Cache hit (no hash check)"),
    and ``_download_bam`` keys its output directory on ``round_id`` alone. The current official
    LIVE contract does **not** guarantee ``bam_sha256``: ``/v2/round-status`` does not document it,
    it is documented only for the practice endpoint and only "when configured", and both the miner
    and the validator read it with ``.get``. So two round-status responses for the same round with
    different URLs and no digest can legitimately return the *first* response's bytes.

    Nothing here deletes or re-downloads to force the issue -- that would discard a possibly
    multi-gigabyte cache the miner is deliberately keeping, and the operational consequences of
    doing that on every decision are not this module's to impose. Instead each authoritative
    handoff records, beside the BAM, which operational source set produced those bytes. On a later
    handoff:

    * the file was written during this call -- provenance is this round's fetch, record it;
    * the platform published a ``bam_sha256`` and the bytes match it -- the content is
      authoritative regardless of which fetch produced it, record it;
    * a sidecar names this same source set and the same bytes -- provenance carries over;
    * otherwise the bytes on disk cannot be shown to have come from this response, and the handoff
      **fails closed** with an actionable message rather than guessing.

    **Freshness is decided by pre/post inode state, not by a clock.** An earlier version compared
    the BAM's mtime against a marker written just before the call and treated ``mtime >= marker``
    as proof of a write. Equality is ambiguous -- a coarse filesystem clock can stamp a file
    written moments earlier with exactly the marker's value -- so a cache hit could be declared
    fresh and skip sidecar validation. That is a fail-open boundary, and no tolerance window fixes
    it, because a window only makes ambiguity larger.

    So the file is *observed* instead. Its path is derived from the official miner's own
    ``BASE_DIR`` and ``safe_round_dir_name`` -- nothing about downloading, fallback or indexing is
    reimplemented -- and its inode state (device, inode, size, mtime, ctime) is snapshotted before
    the call and compared after:

    * absent before, present after      -> this call created it: fresh;
    * present and state CHANGED         -> this call rewrote it: fresh;
    * present and state UNCHANGED       -> the downloader cached: NOT fresh;
    * path underivable or unexpected    -> unknown, therefore NOT fresh.

    Timestamps never decide anything on their own, so an equal mtime and a future mtime both land
    in "unchanged", which is the safe answer. A false negative costs a refusal and a retry; there
    is no false positive to trade it against.
    """
    _require(
        is_verified_production_receipt(receipt),
        "production downloads must be bound to a sealed production receipt; a subclass that "
        "populates the same fields is not one",
    )
    if is_verified_official_miner(miner):
        verified = miner
    else:
        _require(
            not isinstance(miner, VerifiedOfficialMiner),
            "this is a subclass of the verified-miner capability, not the capability; a forged "
            "wrapper does not become the official miner by inheriting from its proof",
        )
        verified = verify_official_miner(miner)

    round_data = receipt._operational_round_data()  # noqa: SLF001 - the receipt owns this
    source_digest = _operational_source_digest(receipt)
    expected_path = _official_output_bam_path(receipt.round_id)
    before = _observe(expected_path)
    try:
        returned = verified.miner._download_bam(round_data, receipt.round_id)  # noqa: SLF001
    except Exception as error:
        raise PlatformRoundStatusError(
            f"the official miner could not download this round: {type(error).__name__}"
        ) from None
    _require(
        returned is not None,
        "the official miner reported that this round's BAM could not be downloaded",
    )
    bam_path = Path(str(returned)).resolve()
    bai_path = Path(str(bam_path) + ".bai")
    _require(bam_path.is_file(), f"the official miner returned {bam_path}, which is not a file")
    _require(
        bai_path.is_file(),
        "the official miner produced no BAM index; a round cannot be profiled without one",
    )

    bam_sha, bam_size = _stream_sha256(bam_path, label="BAM")
    bai_sha, bai_size = _stream_sha256(bai_path, label="BAM index")
    _require(bam_sha != bai_sha, "the BAM and its index cannot be the same bytes")

    expected_bam = receipt.expected_bam_sha256
    if expected_bam is not None:
        _require(
            bam_sha == expected_bam,
            "the downloaded BAM does not hash to the SHA-256 the platform published for this round",
        )

    freshly_written = _written_during_this_call(
        bam_path, expected_path=expected_path, before=before
    )
    if not (freshly_written or expected_bam is not None):
        _require_cached_bytes_belong_to_this_round(
            bam_path, source_digest=source_digest, bam_sha256=bam_sha
        )
    _write_provenance_sidecar(
        bam_path,
        source_digest=source_digest,
        round_id=receipt.round_id,
        bam_sha256=bam_sha,
        bai_sha256=bai_sha,
    )

    return ProductionRoundDownloads(
        _PRODUCTION_DOWNLOAD_TOKEN,
        expected=_PRODUCTION_DOWNLOAD_TOKEN,
        scope=PRODUCTION_SCOPE,
        receipt_identity=receipt.identity,
        operational_binding=receipt._operational_binding,  # noqa: SLF001
        bam_sha256=bam_sha,
        bai_sha256=bai_sha,
        bam_source_slot=OFFICIAL_MINER_DOWNLOAD,
        bai_source_slot=OFFICIAL_MINER_DOWNLOAD,
        byte_counts={"bam": bam_size, "bai": bai_size},
    )


def _written_during_this_call(
    returned: Path, *, expected_path: Path | None, before: _FileState | None
) -> bool:
    """Did THIS call write the file? Answered from inode state, never from a clock.

    Unknowable cases -- the official layout could not be read, or the miner returned a path other
    than the one that layout predicts -- answer ``False``. A file this call did not demonstrably
    write must then prove itself through a provenance record or a published digest.
    """
    if expected_path is None:
        return False
    try:
        if returned.resolve() != expected_path.resolve():
            return False
    except OSError:  # pragma: no cover - resolve on a live file
        return False
    after = _observe(returned)
    if after is None:  # pragma: no cover - the caller already required a file
        return False
    if before is None:
        return True  # it did not exist before this call, and does now
    return after != before


def _require_cached_bytes_belong_to_this_round(
    bam_path: Path, *, source_digest: str, bam_sha256: str
) -> None:
    """A cache hit only carries provenance if a sidecar says these bytes came from these sources."""
    import json

    sidecar = _sidecar_path(bam_path)
    _require(
        sidecar.is_file(),
        "the official miner returned a cached BAM, this round published no bam_sha256, and no "
        "provenance record exists beside the file -- so these bytes cannot be shown to have come "
        "from this round's download sources. Remove the cached BAM to force an authoritative "
        "fetch, or run a round whose status carries bam_sha256.",
    )
    _require(not sidecar.is_symlink(), f"{sidecar} is a symlink")
    try:
        record = json.loads(sidecar.read_bytes())
    except json.JSONDecodeError:
        raise PlatformRoundStatusError(
            "the download provenance record beside the cached BAM is not readable"
        ) from None
    _require(
        isinstance(record, dict) and record.get("schema_version") == PROVENANCE_SIDECAR_SCHEMA,
        "the download provenance record is not one this engine wrote",
    )
    assert isinstance(record, dict)
    _require(
        str(record.get("operational_source_digest")) == source_digest,
        "the cached BAM was obtained under a DIFFERENT round-status response; the same round and "
        "region can be served from different sources, and bytes fetched under one response are "
        "not provenance for another",
    )
    _require(
        str(record.get("bam_sha256")) == bam_sha256,
        "the cached BAM no longer hashes to what its provenance record states",
    )


def _write_provenance_sidecar(
    bam_path: Path, *, source_digest: str, round_id: str, bam_sha256: str, bai_sha256: str
) -> None:
    """Record which operational source set produced these bytes. Digest only, never a URL."""
    import os
    import tempfile

    payload = canonical_json_bytes(
        {
            "schema_version": PROVENANCE_SIDECAR_SCHEMA,
            "round_id": round_id,
            "operational_source_digest": source_digest,
            "bam_sha256": bam_sha256,
            "bai_sha256": bai_sha256,
        }
    )
    sidecar = _sidecar_path(bam_path)
    handle, staged = tempfile.mkstemp(dir=str(sidecar.parent), prefix=".minos-prov.")
    try:
        with os.fdopen(handle, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(staged, sidecar)
    except BaseException:
        Path(staged).unlink(missing_ok=True)
        raise


def accept_fixture_round_downloads(
    *,
    receipt: Any,
    bam_source_url: str,
    bam_path: Any,
    bai_path: Any,
    bai_source_url: str | None = None,
) -> FixtureRoundDownloads:
    """The same binding, in the fixture scope. Never accepted on the production path."""
    _require(
        is_fixture_round_receipt(receipt),
        "fixture downloads must be bound to a sealed fixture receipt",
    )
    result = _accept_downloads(
        token=_FIXTURE_DOWNLOAD_TOKEN,
        expected=_FIXTURE_DOWNLOAD_TOKEN,
        factory=FixtureRoundDownloads,
        scope=FIXTURE_SCOPE,
        receipt=receipt,
        bam_source_url=bam_source_url,
        bam_path=bam_path,
        bai_path=bai_path,
        bai_source_url=bai_source_url,
    )
    assert isinstance(result, FixtureRoundDownloads)
    return result
