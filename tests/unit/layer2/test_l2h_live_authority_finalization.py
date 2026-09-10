"""Two PRE-MINT authority defects: external-client TOCTOU, and a mutable reference authority.

**The client.** ``verify_subnet_platform_client`` checked the exact official type, ``demo``, the
hotkey and an HTTPS base URL, and then sealed a wrapper around the *live* object. But the official
``MinerPlatformClient`` reads its own mutable state at request time -- ``self.demo`` decides
between ``/v2/round-status`` and ``/v2/demo/round-status``, ``self.config.base_url`` is what the
httpx client is built with (and ``PlatformConfig`` is a plain mutable dataclass, so upstream's
constructor-time HTTPS check does not survive a later write), and ``self.keypair`` signs each
attempt. Verifying once and trusting the wrapper afterwards was a time-of-check/time-of-use gap:
a client verified as live and HTTPS could be flipped to demo and still mint a **sealed production
receipt**, with ``endpoint_path()`` continuing to report ``/v2/round-status``.

**The reference table.** ``ACCEPTED_REFERENCE_IDENTITIES`` was an ordinary dict of writable
objects, so ordinary code could change which genome this engine accepts as GRCh38 chr20 without
touching source. ``typing.Final`` is a type-checker annotation; it enforces nothing at runtime.

Scope note: as in ``docs/layer2/L2H_CAPABILITY_TRUST_MODEL.md``, nothing here claims to resist a
concurrent mutation that is reverted before the next read. Ordinary persistent and concurrent
mutation is what is closed.
"""

from __future__ import annotations

import types
from pathlib import Path
from typing import Any

import pytest

import minos_engine.protocol.round_status as round_status_module
from minos_engine.layer2 import live_round_intake as intake_module
from minos_engine.layer2.live_round_intake import (
    ACCEPTED_REFERENCE_IDENTITIES,
    SUPPORTED_CONTIGS,
    ReferenceIdentity,
    accepted_reference_set_content,
    accepted_reference_set_identity,
)
from minos_engine.protocol.round_status import (
    CLIENT_BINDING_FIELDS,
    PRODUCTION_ENDPOINT_PATH,
    PlatformRoundStatusError,
    ProductionRoundStatusReceipt,
    is_verified_production_receipt,
    production_round_status_transport,
    verify_production_round_status,
    verify_subnet_platform_client,
)

REPO_ROOT = Path(__file__).resolve().parents[3]

ACCEPTED_BASE_URL = "https://platform.example"
ACCEPTED_HOTKEY = "5FakeHotkeyAddressForTests"

LIVE_PAYLOAD: dict[str, Any] = {
    "has_active_round": True,
    "status": "open",
    "round_id": "2026-09-10T00:00:00+00:00",
    "region": "chr20:1-1000",
    "bam_presigned_url": "https://s3.example/round.bam",
    "bam_index_presigned_url": "https://s3.example/round.bam.bai",
}


class OfficialClient:
    """Mirrors ``utils.platform_client.MinerPlatformClient`` where it matters.

    ``_round_status_path`` is a **property** over the live ``self.demo``, and ``config`` is a
    mutable namespace -- exactly as upstream. A stub that froze either would not reproduce the
    defect this module exists to close.
    """

    def __init__(self, *, base_url: str = ACCEPTED_BASE_URL, hotkey: str = ACCEPTED_HOTKEY) -> None:
        self.keypair = types.SimpleNamespace(ss58_address=hotkey)
        self.config = types.SimpleNamespace(base_url=base_url)
        self.demo = False
        #: applied from inside ``get_round_status``, to model mutation while the request is in
        #: flight -- upstream re-reads the hotkey on every retry attempt
        self.mutate_in_flight: Any = None

    @property
    def _round_status_path(self) -> str:
        return "/v2/demo/round-status" if self.demo else "/v2/round-status"

    async def get_round_status(self) -> dict[str, Any]:
        if self.mutate_in_flight is not None:
            self.mutate_in_flight(self)
        return dict(LIVE_PAYLOAD)


class PathClient(OfficialClient):
    """A client whose round-status path can move independently of ``demo``."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.path = PRODUCTION_ENDPOINT_PATH

    @property
    def _round_status_path(self) -> str:
        return self.path


@pytest.fixture
def official(monkeypatch: pytest.MonkeyPatch) -> type:
    monkeypatch.setattr(
        round_status_module, "resolve_official_platform_client_type", lambda: OfficialClient
    )
    return OfficialClient


@pytest.fixture
def official_path(monkeypatch: pytest.MonkeyPatch) -> type:
    monkeypatch.setattr(
        round_status_module, "resolve_official_platform_client_type", lambda: PathClient
    )
    return PathClient


# --------------------------------------------------------------------------- #
# §B: the snapshot
# --------------------------------------------------------------------------- #
def test_the_verified_client_snapshots_its_whole_security_binding(official) -> None:
    verified = verify_subnet_platform_client(OfficialClient())
    assert verified.verified_base_url == ACCEPTED_BASE_URL
    assert verified.verified_hotkey_ss58 == ACCEPTED_HOTKEY
    assert verified.verified_round_status_path == PRODUCTION_ENDPOINT_PATH
    assert verified.verified_demo is False
    assert set(verified.verified_binding()) == set(CLIENT_BINDING_FIELDS)


def test_the_snapshot_holds_engine_owned_primitives(official) -> None:
    """Not references to the external object's values, whose ``__eq__`` could lie."""

    class Liar:
        def __eq__(self, other: object) -> bool:
            return True

        def __str__(self) -> str:
            return ACCEPTED_BASE_URL

    client = OfficialClient()
    client.config.base_url = Liar()
    verified = verify_subnet_platform_client(client)
    snapshot = verified.verified_binding()
    assert type(snapshot["base_url"]) is str
    assert type(snapshot["demo"]) is bool
    assert type(snapshot["hotkey_ss58"]) is str
    assert type(snapshot["round_status_path"]) is str


def test_the_snapshot_is_detached_and_read_only(official) -> None:
    verified = verify_subnet_platform_client(OfficialClient())
    handed = verified.verified_binding()
    handed["base_url"] = "https://evil.example"
    assert verified.verified_base_url == ACCEPTED_BASE_URL
    with pytest.raises(TypeError):
        verified._binding["base_url"] = "https://evil.example"
    with pytest.raises(PlatformRoundStatusError):
        verified._binding = {}


def test_a_client_that_hides_its_round_status_path_is_refused(monkeypatch) -> None:
    """Fail closed: an object that cannot say where it will send the request is not usable."""

    class Opaque:
        def __init__(self) -> None:
            self.keypair = types.SimpleNamespace(ss58_address=ACCEPTED_HOTKEY)
            self.config = types.SimpleNamespace(base_url=ACCEPTED_BASE_URL)
            self.demo = False

    monkeypatch.setattr(
        round_status_module, "resolve_official_platform_client_type", lambda: Opaque
    )
    with pytest.raises(PlatformRoundStatusError, match="round-status path is None"):
        verify_subnet_platform_client(Opaque())


def test_a_client_with_no_demo_attribute_at_all_is_refused(monkeypatch) -> None:
    """Absent means "cannot prove it is not a demo client"."""

    class NoDemo:
        _round_status_path = PRODUCTION_ENDPOINT_PATH

        def __init__(self) -> None:
            self.keypair = types.SimpleNamespace(ss58_address=ACCEPTED_HOTKEY)
            self.config = types.SimpleNamespace(base_url=ACCEPTED_BASE_URL)

    monkeypatch.setattr(
        round_status_module, "resolve_official_platform_client_type", lambda: NoDemo
    )
    with pytest.raises(PlatformRoundStatusError, match="demo mode"):
        verify_subnet_platform_client(NoDemo())


# --------------------------------------------------------------------------- #
# §F: the TOCTOU negatives
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "label,mutate,expected",
    [
        ("demo flipped on", lambda c: setattr(c, "demo", True), "demo mode"),
        (
            "base url swapped for another https platform",
            lambda c: setattr(c.config, "base_url", "https://different.example"),
            "base_url changed after it was verified",
        ),
        (
            "base url downgraded to cleartext",
            lambda c: setattr(c.config, "base_url", "http://localhost"),
            "not HTTPS",
        ),
        (
            "hotkey replaced",
            lambda c: setattr(c.keypair, "ss58_address", "5AttackerHotkey"),
            "hotkey_ss58 changed after it was verified",
        ),
        ("hotkey removed", lambda c: setattr(c, "keypair", None), "no hotkey"),
    ],
)
def test_mutating_the_client_after_verification_refuses_the_fetch(
    official, label: str, mutate: Any, expected: str
) -> None:
    client = OfficialClient()
    transport = production_round_status_transport(client)
    mutate(client)
    with pytest.raises(PlatformRoundStatusError, match=expected):
        transport.fetch_round_status()
    with pytest.raises(PlatformRoundStatusError, match=expected):
        verify_production_round_status(transport)


def test_moving_the_round_status_path_after_verification_refuses_the_fetch(official_path) -> None:
    """§E: the client picks its own path, so the engine's constant cannot be the proof."""
    client = PathClient()
    transport = production_round_status_transport(client)
    assert transport.endpoint_path() == PRODUCTION_ENDPOINT_PATH

    client.path = "/v2/demo/round-status"
    with pytest.raises(PlatformRoundStatusError, match="round-status path is"):
        transport.fetch_round_status()
    # the engine's own constant is unmoved, which is exactly why it proves nothing by itself
    assert transport.endpoint_path() == PRODUCTION_ENDPOINT_PATH


def test_mutation_during_the_request_discards_the_payload(official) -> None:
    """§D: the object stays mutable while the request is in flight."""
    client = OfficialClient()
    transport = production_round_status_transport(client)
    client.mutate_in_flight = lambda c: setattr(c, "demo", True)
    with pytest.raises(PlatformRoundStatusError, match="demo mode"):
        transport.fetch_round_status()


def test_mutation_during_the_request_is_caught_even_when_only_the_url_moved(official) -> None:
    client = OfficialClient()
    transport = production_round_status_transport(client)
    client.mutate_in_flight = lambda c: setattr(c.config, "base_url", "https://elsewhere.example")
    with pytest.raises(PlatformRoundStatusError, match="base_url changed"):
        transport.fetch_round_status()


def test_a_client_that_stops_being_the_official_type_is_refused(official, monkeypatch) -> None:
    client = OfficialClient()
    transport = production_round_status_transport(client)

    class Other:
        pass

    monkeypatch.setattr(round_status_module, "resolve_official_platform_client_type", lambda: Other)
    with pytest.raises(PlatformRoundStatusError, match="no longer the official subnet client"):
        transport.fetch_round_status()


def test_an_unchanged_client_still_reaches_a_production_receipt(official) -> None:
    """The supported path must keep working; hardening that breaks it is not hardening."""
    client = OfficialClient()
    transport = production_round_status_transport(client)
    payload = transport.fetch_round_status()
    assert payload["round_id"] == LIVE_PAYLOAD["round_id"]

    receipt = verify_production_round_status(transport)
    assert type(receipt) is ProductionRoundStatusReceipt
    assert is_verified_production_receipt(receipt)
    assert receipt.round_id == LIVE_PAYLOAD["round_id"]
    assert receipt.region_source == LIVE_PAYLOAD["region"]


def test_the_binding_is_rechecked_before_and_after_every_request(official) -> None:
    """Two independent checks, not one: count them."""
    client = OfficialClient()
    transport = production_round_status_transport(client)
    verified = transport._verified_client
    calls: list[str] = []
    original = type(verified).require_binding_unchanged

    def counted(self: Any) -> None:
        calls.append("check")
        original(self)

    object.__setattr__(client, "_probe", None)
    type(verified).require_binding_unchanged = counted  # type: ignore[method-assign]
    try:
        transport.fetch_round_status()
    finally:
        type(verified).require_binding_unchanged = original  # type: ignore[method-assign]
    assert len(calls) == 2, "the binding must be checked before AND after the request"


def test_the_operational_binding_never_reaches_a_scientific_identity(official) -> None:
    """A base URL and a hotkey describe how the platform was reached, not what the round is."""
    client = OfficialClient()
    receipt = verify_production_round_status(production_round_status_transport(client))
    identity_content = receipt._parsed.identity_content()
    serialized = repr(identity_content)
    assert ACCEPTED_BASE_URL not in serialized
    assert ACCEPTED_HOTKEY not in serialized
    assert set(identity_content) == {
        "schema_version",
        "round_id",
        "region_source",
        "endpoint_path",
    }


# --------------------------------------------------------------------------- #
# §J: the accepted reference authority
# --------------------------------------------------------------------------- #
def test_the_supported_contigs_are_exactly_the_qualified_five() -> None:
    assert SUPPORTED_CONTIGS == ("chr18", "chr19", "chr20", "chr21", "chr22")
    assert set(ACCEPTED_REFERENCE_IDENTITIES) == set(SUPPORTED_CONTIGS)


@pytest.mark.parametrize("field", ["contig", "reference_sha256", "fai_sha256", "reference_m5"])
def test_a_reference_identity_cannot_be_edited(field: str) -> None:
    reference = ACCEPTED_REFERENCE_IDENTITIES["chr20"]
    before = getattr(reference, field)
    with pytest.raises(AttributeError):
        setattr(reference, field, "0" * 64)
    with pytest.raises(AttributeError):
        delattr(reference, field)
    assert getattr(ACCEPTED_REFERENCE_IDENTITIES["chr20"], field) == before


def test_the_accepted_reference_table_cannot_be_edited() -> None:
    replacement = ReferenceIdentity(
        contig="chr20", reference_sha256="0" * 64, fai_sha256="1" * 64, reference_m5="2" * 32
    )

    def _replace() -> None:
        ACCEPTED_REFERENCE_IDENTITIES["chr20"] = replacement  # type: ignore[index]

    def _delete() -> None:
        del ACCEPTED_REFERENCE_IDENTITIES["chr20"]  # type: ignore[attr-defined]

    def _insert() -> None:
        ACCEPTED_REFERENCE_IDENTITIES["chr23"] = replacement  # type: ignore[index]

    # a read-only mapping raises TypeError on assignment and has no __delitem__ at all
    for action in (_replace, _delete, _insert):
        with pytest.raises((TypeError, AttributeError)):
            action()
    assert set(ACCEPTED_REFERENCE_IDENTITIES) == set(SUPPORTED_CONTIGS)
    assert ACCEPTED_REFERENCE_IDENTITIES["chr20"].reference_sha256 != "0" * 64


def test_the_table_has_no_mutating_methods() -> None:
    for method in ("update", "clear", "pop", "popitem", "setdefault"):
        assert not hasattr(ACCEPTED_REFERENCE_IDENTITIES, method)


def test_a_reference_identity_is_a_value_and_compares_by_content() -> None:
    """Frozen means it can be a value; two equal references are interchangeable and hashable."""
    a = ACCEPTED_REFERENCE_IDENTITIES["chr21"]
    b = ReferenceIdentity(**a.content())
    assert a == b
    assert hash(a) == hash(b)
    assert a.content() == b.content()


# --------------------------------------------------------------------------- #
# §I / §M: the accepted values did not move
# --------------------------------------------------------------------------- #
#: Read off entry HEAD 29ca6d2bb2b119a6d720cca4035365734ec24c0a, field for field.
ENTRY_HEAD_REFERENCES: dict[str, dict[str, str]] = {
    "chr18": {
        "reference_sha256": "4c37db9609b3e865e35128fb065f61eea1c83c815386c4631b03820f9b8265d2",
        "fai_sha256": "1e9dac505c1b48f7a1ca90c5ec7e75ed257a19d4c03b21f4c40e4dfa29b806b5",
        "reference_m5": "11eeaa801f6b0e2e36a1138616b8ee9a",
    },
    "chr19": {
        "reference_sha256": "e00b74f7cd48f6c94395c40ae7b4c13d1c3255e2db1be366eb5a87441577ec5c",
        "fai_sha256": "ce0ee961cd23439f944459c8483b3a11ab58b87e201272862a24d67fb7707805",
        "reference_m5": "85f9f4fc152c58cb7913c06d6b98573a",
    },
    "chr20": {
        "reference_sha256": "61eba5b05ef7d9ae5310e756c1143fa48072de3856d36871bb14e57aa2435ff3",
        "fai_sha256": "295950bb320e5f27b37360000d77303187e6399bf8e7705aa26fd4a1c88ba115",
        "reference_m5": "b18e6c531b0bd70e949a7fc20859cb01",
    },
    "chr21": {
        "reference_sha256": "c218d98e3bf58fa3551c3f5f12bc829c798c42fd301f8ed6021c35aa231f39f8",
        "fai_sha256": "838e3d562353d9a90084416731de18de5848dbb982939ae59af9ce37fd19e0de",
        "reference_m5": "974dc7aec0b755b19f031418fdedf293",
    },
    "chr22": {
        "reference_sha256": "8d440d7b863c6d0af1bc438385a770b0a861ddc0f5187b7466d893e2728a9b54",
        "fai_sha256": "7623e1c5091eda09847bd72b64bf57b1abe0bfab3cbef8c4e687975e8ee09e2c",
        "reference_m5": "ac37ec46683600f808cdd41eac1d55cd",
    },
}


@pytest.mark.parametrize("contig", sorted(ENTRY_HEAD_REFERENCES))
def test_every_accepted_reference_is_field_for_field_what_it_was(contig: str) -> None:
    reference = ACCEPTED_REFERENCE_IDENTITIES[contig]
    expected = ENTRY_HEAD_REFERENCES[contig]
    assert reference.contig == contig
    assert reference.reference_sha256 == expected["reference_sha256"]
    assert reference.fai_sha256 == expected["fai_sha256"]
    assert reference.reference_m5 == expected["reference_m5"]


def test_the_accepted_reference_set_has_a_deterministic_audit_identity() -> None:
    """A name for "the reference set this engine accepts", so a change to it is visible."""
    content = accepted_reference_set_content()
    assert content["contig_count"] == 5
    assert [entry["contig"] for entry in content["contigs"]] == list(SUPPORTED_CONTIGS)
    assert accepted_reference_set_identity() == (
        "adc72ec35db69320ceb9f12a471c0f630cf284d4b400a05e7d3d482ba61fb456"
    )


def test_the_reference_set_identity_is_not_a_live_intake_field() -> None:
    """§I: an audit name, deliberately NOT part of what a live intake means."""
    from minos_engine.layer2.live_round_intake import LIVE_INTAKE_FIELDS, LIVE_INTAKE_SCHEMA

    assert LIVE_INTAKE_SCHEMA.endswith("v2")
    assert not any("reference_set" in field for field in LIVE_INTAKE_FIELDS)


def test_the_live_intake_still_resolves_the_same_reference_for_every_contig() -> None:
    """§J: the intake reads the table the same way it always did."""
    for contig in SUPPORTED_CONTIGS:
        reference = intake_module.ACCEPTED_REFERENCE_IDENTITIES[contig]
        expected = ENTRY_HEAD_REFERENCES[contig]
        assert reference.reference_sha256 == expected["reference_sha256"]
        assert reference.fai_sha256 == expected["fai_sha256"]
