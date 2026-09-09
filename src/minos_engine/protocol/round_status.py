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
from typing import Any, Final

from minos_engine.common.canonical_json import canonical_json_bytes
from minos_engine.common.errors import MinosEngineError
from minos_engine.common.hashing import sha256_hex

__all__ = [
    "ACTIVE_ROUND_STATUSES",
    "BAI_URL_SLOTS",
    "BAM_URL_SLOTS",
    "FIXTURE_SCOPE",
    "LOCALLY_INDEXED",
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
    "SubnetPlatformRoundStatusTransport",
    "accept_fixture_round_downloads",
    "accept_production_round_downloads",
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
class RoundStatusTransport(ABC):
    """Something that can ask for a round status. Inheriting this grants NO authority."""

    transport_kind: str = "abstract"

    @abstractmethod
    def endpoint_path(self) -> str:
        """The path that was called."""

    @abstractmethod
    def fetch_round_status(self) -> Mapping[str, Any]:
        """Return the platform's round-status payload verbatim."""


class ProductionRoundStatusTransport(RoundStatusTransport):
    """A transport that reaches the real, authenticated platform.

    Subclassing this is not a formality a caller can perform casually: the production factory also
    requires the exact production endpoint, and the only implementation shipped here validates
    that it was handed the subnet's own client.
    """

    transport_kind = "production"


class SubnetPlatformRoundStatusTransport(ProductionRoundStatusTransport):
    """The production transport: the subnet's own authenticated platform client.

    The engine deliberately does not reimplement the HTTP call -- re-deriving the request signing,
    nonce and auth headers would be a second implementation of a security boundary maintained next
    door, and it would need the miner's keypair.

    Accepting *any* object with two attributes was too weak, so the client is checked structurally:
    its class must be named ``MinerPlatformClient`` and defined in a ``platform_client`` module,
    it must carry a keypair with an ``ss58_address``, its configured base URL must be HTTPS, and it
    must not be in demo mode. This is a structural check, not a cryptographic one -- it stops a
    casual or accidental substitution, and the real guarantee remains that a deployment constructs
    the genuine client. Demo mode is refused outright because that route is a sandbox.
    """

    transport_kind = "subnet-platform-client"

    def __init__(self, client: Any) -> None:
        classes = {base.__name__ for base in type(client).__mro__}
        _require(
            "MinerPlatformClient" in classes,
            "the production transport requires the subnet's MinerPlatformClient; an object that "
            "merely exposes get_round_status is not the authenticated platform client",
        )
        _require(
            type(client).__module__.split(".")[-1] == "platform_client",
            f"the client's class comes from {type(client).__module__!r}, not a platform_client "
            "module",
        )
        for required in ("get_round_status", "keypair", "config"):
            _require(hasattr(client, required), f"the platform client does not expose {required!r}")
        _require(
            bool(getattr(client.keypair, "ss58_address", "")),
            "the platform client has no hotkey to sign the request with",
        )
        base_url = str(getattr(client.config, "base_url", ""))
        _require(
            base_url.startswith("https://"),
            f"the platform base URL {base_url!r} is not HTTPS; the subnet client enforces this and "
            "so does this transport",
        )
        _require(
            not getattr(client, "demo", False),
            "this client is in demo mode, which routes to the sandboxed /v2/demo namespace; a live "
            "decision may not be made from a demo round",
        )
        self._client = client

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
            payload = asyncio.run(self._client.get_round_status())
        except PlatformRoundStatusError:
            raise
        except Exception as error:
            raise PlatformRoundStatusError(
                f"the platform round status could not be retrieved: {type(error).__name__}"
            ) from None
        _require(isinstance(payload, Mapping), "the platform returned a non-object round status")
        assert isinstance(payload, Mapping)
        return payload


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

    __slots__ = ("_parsed", "identity", "scope")

    def __init__(self, token: object, *, parsed: ParsedRoundStatus, expected: object) -> None:
        if token is not expected:
            raise PlatformRoundStatusError(
                "a round-status receipt may only be minted by fetching one through the transport "
                "its scope requires; a dictionary of round fields is not a statement by anyone"
            )
        self._parsed = parsed
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


def verify_production_round_status(transport: Any) -> ProductionRoundStatusReceipt:
    """Mint a PRODUCTION receipt. Only the real transport, only the production endpoint."""
    _require(
        isinstance(transport, ProductionRoundStatusTransport),
        "a production live receipt requires a production transport; inheriting the base transport "
        "or implementing its methods is not authority to speak for the platform",
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
        isinstance(transport, FixtureRoundStatusTransport),
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
        self.scope = scope
        self.receipt_identity = receipt_identity
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
        bam_sha256=bam_sha,
        bai_sha256=bai_sha,
        bam_source_slot=bam_slot,
        bai_source_slot=bai_slot,
        byte_counts={"bam": bam_size, "bai": bai_size},
    )


def accept_production_round_downloads(
    *,
    receipt: Any,
    bam_source_url: str,
    bam_path: Any,
    bai_path: Any,
    bai_source_url: str | None = None,
) -> ProductionRoundDownloads:
    """Bind files the miner downloaded to the production round they were downloaded for.

    ``bai_source_url`` is ``None`` when the index was built locally with samtools, which is what
    the official miner does when the platform offers no index URL.
    """
    _require(
        isinstance(receipt, ProductionRoundStatusReceipt),
        "production downloads must be bound to a production receipt",
    )
    result = _accept_downloads(
        token=_PRODUCTION_DOWNLOAD_TOKEN,
        expected=_PRODUCTION_DOWNLOAD_TOKEN,
        factory=ProductionRoundDownloads,
        scope=PRODUCTION_SCOPE,
        receipt=receipt,
        bam_source_url=bam_source_url,
        bam_path=bam_path,
        bai_path=bai_path,
        bai_source_url=bai_source_url,
    )
    assert isinstance(result, ProductionRoundDownloads)
    return result


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
        isinstance(receipt, FixtureRoundStatusReceipt),
        "fixture downloads must be bound to a fixture receipt",
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
