"""``l2h-platform-round-status-receipt-v1`` — which round the PLATFORM actually offered.

Canonicalizing a round id and a region proves they are internally consistent. It does not prove
they are the round this miner was served, and a caller that can invent both can invent a round.
This module is the authority that closes that: a receipt exists only because a transport went and
asked the platform, and it cannot be built from a dictionary.

**What the receipt does and does not assert.** Read from the subnet source
(``minos_subnet/utils/platform_client.py``):

* the miner **signs the request** -- ``_auth_body`` adds a hotkey signature over
  ``(method, path, body, timestamp)`` plus a nonce, sent with ``X-Minos-Auth-Version: 2``;
* the transport is **HTTPS-enforced** -- ``PlatformClient.__init__`` refuses a non-HTTPS base URL
  for anything but localhost;
* the response is **plain JSON with no digital signature**. Nothing in the subnet verifies a
  signature over the response body.

So a receipt asserts exactly this and no more: *this payload was returned by the configured,
HTTPS-protected platform transport in answer to a request this miner signed.* It is a
transport-authenticated receipt, not a cryptographically signed document, and it is not described
as one anywhere.

**The seam is the transport, not the authority.** Tests substitute
:class:`FixtureRoundStatusTransport` for the network, and still go through
:func:`verify_platform_round_status` — the same shape checks, the same refusals, the same mint.
Nothing in the engine can obtain a receipt by another route.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping
from typing import Any, Final

from minos_engine.common.canonical_json import canonical_json_bytes
from minos_engine.common.errors import MinosEngineError
from minos_engine.common.hashing import sha256_hex

__all__ = [
    "ACTIVE_ROUND_STATUSES",
    "PLATFORM_ROUND_STATUS_DOMAIN",
    "PLATFORM_ROUND_STATUS_SCHEMA",
    "ROUND_STATUS_PATH",
    "FixtureRoundStatusTransport",
    "PlatformRoundStatusError",
    "RoundStatusTransport",
    "SubnetPlatformRoundStatusTransport",
    "VerifiedPlatformRoundStatus",
    "verify_platform_round_status",
]

PLATFORM_ROUND_STATUS_SCHEMA: Final = "l2h-platform-round-status-receipt-v1"
PLATFORM_ROUND_STATUS_DOMAIN: Final = "minos:l2h-platform-round-status-receipt:v1\n"

#: The endpoint the subnet's ``MinerPlatformClient`` calls. Recorded so a receipt says where it
#: came from, and asserted against the transport so a demo/sandbox route cannot pass as live.
ROUND_STATUS_PATH: Final = "/v2/round-status"

#: Round states in which a decision may legitimately be made. ``pending`` has not opened and
#: ``scoring``/``completed`` are past submission, so none of them is a round to decide for.
ACTIVE_ROUND_STATUSES: Final[frozenset[str]] = frozenset({"open"})

#: Fields the receipt's identity is derived from. Presigned URLs, timings, nonces, signatures and
#: mutation counts are deliberately absent: they are operational, per-fetch or secret.
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


class RoundStatusTransport(ABC):
    """The one seam: something that can ask the platform what round is open.

    Implementations differ only in *how the bytes arrive*. They never decide whether the answer is
    acceptable -- that is :func:`verify_platform_round_status`'s job, and it is the same code for
    every transport.
    """

    #: How the payload was obtained. Recorded in the observation, never in the identity.
    transport_kind: str = "abstract"

    @abstractmethod
    def endpoint_path(self) -> str:
        """The path that was called, e.g. ``/v2/round-status``."""

    @abstractmethod
    def fetch_round_status(self) -> Mapping[str, Any]:
        """Return the platform's round-status payload verbatim."""


class SubnetPlatformRoundStatusTransport(RoundStatusTransport):
    """The production transport: the subnet's own authenticated platform client.

    The engine deliberately does **not** reimplement the HTTP call. Re-deriving the request
    signing, nonce and auth-version headers here would be a second implementation of a security
    boundary that already exists and is maintained next door, and it would need the miner's
    keypair. Instead the caller passes the subnet's ``MinerPlatformClient``, whose constructor has
    already refused a non-HTTPS base URL, and whose ``get_round_status()`` signs the request.

    The client is asynchronous; this runs its coroutine to completion. A client already inside a
    running event loop is refused rather than silently deadlocked.
    """

    transport_kind = "subnet-platform-client"

    def __init__(self, client: Any, *, demo: bool = False) -> None:
        for required in ("get_round_status", "keypair"):
            if not hasattr(client, required):
                raise PlatformRoundStatusError(
                    f"the platform client does not expose {required!r}; this is not the subnet's "
                    "authenticated MinerPlatformClient"
                )
        if getattr(client, "demo", False) and not demo:
            raise PlatformRoundStatusError(
                "this client is in demo mode, which routes to the sandboxed /v2/demo namespace; a "
                "live decision may not be made from a demo round"
            )
        self._client = client
        self._demo = demo

    def endpoint_path(self) -> str:
        return "/v2/demo/round-status" if self._demo else ROUND_STATUS_PATH

    def fetch_round_status(self) -> Mapping[str, Any]:
        import asyncio

        try:
            asyncio.get_running_loop()
        except RuntimeError:
            pass
        else:
            raise PlatformRoundStatusError(
                "the platform round status cannot be fetched from inside a running event loop; "
                "await the client directly and pass the payload to a transport that returns it"
            )
        try:
            payload = asyncio.run(self._client.get_round_status())
        except PlatformRoundStatusError:
            raise
        except Exception as error:  # the platform, the network, or authentication said no
            raise PlatformRoundStatusError(
                f"the platform round status could not be retrieved: {type(error).__name__}"
            ) from None
        if not isinstance(payload, Mapping):
            raise PlatformRoundStatusError("the platform returned a non-object round status")
        return payload


class FixtureRoundStatusTransport(RoundStatusTransport):
    """A deterministic transport for tests and replay.

    It replaces the **transport** and nothing else: the payload it returns still goes through the
    same verification, and a fixture cannot mint a receipt any more directly than the network can.
    """

    transport_kind = "deterministic-fixture"

    def __init__(self, payload: Mapping[str, Any], *, endpoint_path: str = ROUND_STATUS_PATH):
        self._payload = dict(payload)
        self._endpoint_path = endpoint_path

    def endpoint_path(self) -> str:
        return self._endpoint_path

    def fetch_round_status(self) -> Mapping[str, Any]:
        return dict(self._payload)


_RECEIPT_TOKEN: Final = object()


class VerifiedPlatformRoundStatus:
    """Proof that the platform offered THIS round, over an authenticated transport.

    Minted only by :func:`verify_platform_round_status`. A dictionary, a hand-built lookalike, or a
    caller-chosen round id and region cannot become one.
    """

    __slots__ = (
        "endpoint_path",
        "identity",
        "region_source",
        "round_id",
        "status",
        "transport_kind",
    )

    def __init__(
        self,
        token: object,
        *,
        round_id: str,
        region_source: str,
        status: str,
        endpoint_path: str,
        transport_kind: str,
        identity: str,
    ) -> None:
        if token is not _RECEIPT_TOKEN:
            raise PlatformRoundStatusError(
                "a platform round-status receipt may only be minted by fetching one; a dictionary "
                "of round fields is not a statement by the platform"
            )
        self.round_id = round_id
        self.region_source = region_source
        self.status = status
        self.endpoint_path = endpoint_path
        self.transport_kind = transport_kind
        self.identity = identity

    def content(self) -> dict[str, Any]:
        """The identity-bearing content. No URL, no timing, no nonce, no signature."""
        return {
            "schema_version": PLATFORM_ROUND_STATUS_SCHEMA,
            "round_id": self.round_id,
            "region_source": self.region_source,
            "endpoint_path": self.endpoint_path,
        }

    def observation(self) -> dict[str, Any]:
        return {**self.content(), "status": self.status, "transport_kind": self.transport_kind}


def verify_platform_round_status(transport: RoundStatusTransport) -> VerifiedPlatformRoundStatus:
    """Ask the platform what round is open, and refuse anything that is not one.

    Every refusal below is a fail-closed: no round, no intake, no profile, no decision.
    """
    from minos_engine.common.genomic_region import (
        ONE_BASED_INCLUSIVE,
        normalize_region,
        validate_round_identifier,
    )

    _require(
        isinstance(transport, RoundStatusTransport),
        "a round status may only be fetched through a RoundStatusTransport; an object that merely "
        "has a fetch method has not been through this seam",
    )
    payload = transport.fetch_round_status()
    _require(
        isinstance(payload, Mapping) and bool(payload),
        "the platform returned an empty or non-object round status",
    )

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
    assert isinstance(status, str)

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

    endpoint_path = transport.endpoint_path()
    _require(
        isinstance(endpoint_path, str) and endpoint_path.startswith("/"),
        "the transport did not report the path it called",
    )
    content = {
        "schema_version": PLATFORM_ROUND_STATUS_SCHEMA,
        "round_id": round_id,
        "region_source": region_source,
        "endpoint_path": endpoint_path,
    }
    assert tuple(sorted(content)) == RECEIPT_IDENTITY_FIELDS
    return VerifiedPlatformRoundStatus(
        _RECEIPT_TOKEN,
        round_id=round_id,
        region_source=region_source,
        status=status,
        endpoint_path=endpoint_path,
        transport_kind=str(getattr(transport, "transport_kind", "unknown")),
        identity=sha256_hex(
            PLATFORM_ROUND_STATUS_DOMAIN.encode("utf-8") + canonical_json_bytes(content)
        ),
    )
