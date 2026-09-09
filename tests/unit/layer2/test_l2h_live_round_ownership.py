"""LIVE round ownership: authority comes from the platform, not from caller content."""

from __future__ import annotations

import ast
import json
from pathlib import Path
from typing import Any

import pytest

from minos_engine.common.canonical_json import canonical_json_bytes
from minos_engine.layer2.contracts import ControlMode
from minos_engine.layer2.live_round_authority import (
    LIVE_OWNERSHIP_DOMAIN,
    LIVE_OWNERSHIP_SCHEMA,
    LiveRoundOwnershipError,
    VerifiedLiveProfileBinding,
    load_verified_live_round_ownership,
    verify_live_profile_binding,
)
from minos_engine.layer2.live_round_intake import (
    LiveRoundIntakeError,
    observe_fixture_live_round_intake,
    require_production_scope,
    verify_live_round_intake,
)
from minos_engine.layer2.round_profile_authority import (
    LIVE_PARTITION,
    TRAIN_SCHEDULE_PATH,
    OwnedRoundProfile,
    RoundProfileAuthorityError,
    VerifiedRoundProfileAuthority,
    load_verified_round_profile_corpus,
    own_verified_live_round,
)
from minos_engine.protocol.round_status import (
    BAI_URL_SLOTS,
    BAM_URL_SLOTS,
    FIXTURE_SCOPE,
    LOCALLY_INDEXED,
    PRODUCTION_ENDPOINT_PATH,
    PRODUCTION_SCOPE,
    FixtureRoundDownloads,
    FixtureRoundStatusReceipt,
    FixtureRoundStatusTransport,
    PlatformRoundStatusError,
    ProductionRoundDownloads,
    ProductionRoundStatusReceipt,
    ProductionRoundStatusTransport,
    RoundStatusTransport,
    SubnetPlatformRoundStatusTransport,
    accept_fixture_round_downloads,
    accept_production_round_downloads,
    observe_fixture_round_status,
    parse_round_status,
    verify_production_round_status,
)
from tests.conftest import REPO_ROOT
from tests.layer2_live_replay import (
    ABSENT,
    FIXTURE_BAI_URL,
    FIXTURE_BAM_URL,
    FRESH_LIVE_ROUND_ID,
    accept_synthetic_reference,
    build_live_dataset,
    build_live_replay,
    live_ownership,
    round_status_payload,
)

ACCEPTED_TRAIN_CORPUS_IDENTITY = "9cc53b5d28c8a8da34c25095362c09d8cb1fb57533ff0a0b3e1fdf7000970b03"


@pytest.fixture(scope="module")
def dataset(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    """Real files and a real Layer 1 profile. Built once; ~0.4s."""
    return build_live_dataset(tmp_path_factory.mktemp("live_round"))


@pytest.fixture(autouse=True)
def _accepted_reference(monkeypatch: pytest.MonkeyPatch, dataset: dict[str, Any]) -> None:
    accept_synthetic_reference(monkeypatch, dataset)


@pytest.fixture
def replay(dataset: dict[str, Any]) -> dict[str, Any]:
    built = build_live_replay(dataset)
    built["ownership"] = live_ownership(built)
    return built


# --------------------------------------------------------------------------- #
# BLOCKER 1: a fixture is not the platform
# --------------------------------------------------------------------------- #
def test_the_replay_receipt_is_a_fixture_not_a_production_receipt(replay):
    receipt = replay["receipt"]
    assert isinstance(receipt, FixtureRoundStatusReceipt)
    assert not isinstance(receipt, ProductionRoundStatusReceipt)
    assert receipt.scope == FIXTURE_SCOPE
    assert receipt.round_id == FRESH_LIVE_ROUND_ID
    assert len(receipt.identity) == 64


def test_a_fixture_transport_cannot_mint_a_production_receipt(dataset):
    transport = FixtureRoundStatusTransport(round_status_payload(region=dataset["region"]))
    assert not isinstance(transport, ProductionRoundStatusTransport)
    with pytest.raises(PlatformRoundStatusError, match="production transport"):
        verify_production_round_status(transport)


def test_inheriting_the_base_transport_grants_nothing(dataset):
    """ "Some allowed transport object returned this" must not be production authority."""

    class HomeMade(RoundStatusTransport):
        transport_kind = "home-made"

        def endpoint_path(self) -> str:
            return PRODUCTION_ENDPOINT_PATH

        def fetch_round_status(self) -> dict[str, Any]:
            return round_status_payload(region=dataset["region"])

    assert isinstance(HomeMade(), RoundStatusTransport)
    with pytest.raises(PlatformRoundStatusError, match="production transport"):
        verify_production_round_status(HomeMade())


def test_a_duck_typed_transport_is_refused(dataset):
    class Quacks:
        transport_kind = "production"

        def endpoint_path(self) -> str:
            return PRODUCTION_ENDPOINT_PATH

        def fetch_round_status(self) -> dict[str, Any]:
            return round_status_payload(region=dataset["region"])

    with pytest.raises(PlatformRoundStatusError, match="production transport"):
        verify_production_round_status(Quacks())


@pytest.mark.parametrize(
    "endpoint", ["/v2/demo/round-status", "/other/path", "/", "/v2/round-status/../demo", ""]
)
def test_a_production_receipt_requires_exactly_the_production_endpoint(dataset, endpoint):
    """The demo namespace is a sandbox; it may not authorise a live decision."""

    class AtEndpoint(ProductionRoundStatusTransport):
        def endpoint_path(self) -> str:
            return endpoint

        def fetch_round_status(self) -> dict[str, Any]:
            return round_status_payload(region=dataset["region"])

    with pytest.raises(PlatformRoundStatusError) as caught:
        verify_production_round_status(AtEndpoint())
    assert "exactly /v2/round-status" in str(caught.value)
    if endpoint == "/v2/demo/round-status":
        assert "sandbox" in str(caught.value)


def test_neither_receipt_can_be_built_from_a_dictionary(replay):
    parsed = parse_round_status(replay["payload"], endpoint_path=PRODUCTION_ENDPOINT_PATH)
    for cls in (ProductionRoundStatusReceipt, FixtureRoundStatusReceipt):
        with pytest.raises(PlatformRoundStatusError, match="may only be minted"):
            cls(object(), parsed=parsed)


@pytest.mark.parametrize(
    "client,match",
    [
        (object(), "MinerPlatformClient"),
        (type("MinerPlatformClient", (), {})(), "platform_client module"),
    ],
)
def test_a_fake_platform_client_cannot_become_a_production_transport(client, match):
    with pytest.raises(PlatformRoundStatusError, match=match):
        SubnetPlatformRoundStatusTransport(client)


def test_a_lookalike_platform_client_is_refused_on_every_property():
    """Two attributes are not the authenticated, HTTPS-enforcing subnet client."""
    import types

    module = types.ModuleType("some_pkg.platform_client")

    def make(*, base_url: str = "https://platform.example", demo: bool = False, hotkey: str = "5F"):
        cls = type("MinerPlatformClient", (), {})
        cls.__module__ = "some_pkg.platform_client"
        client = cls()
        client.get_round_status = lambda: {}
        client.keypair = types.SimpleNamespace(ss58_address=hotkey)
        client.config = types.SimpleNamespace(base_url=base_url)
        client.demo = demo
        return client

    assert module  # the module object exists only to make the intent explicit
    # the fully-shaped one is accepted -- the check is structural, and this is documented
    SubnetPlatformRoundStatusTransport(make())
    with pytest.raises(PlatformRoundStatusError, match="not HTTPS"):
        SubnetPlatformRoundStatusTransport(make(base_url="http://platform.example"))
    with pytest.raises(PlatformRoundStatusError, match="demo mode"):
        SubnetPlatformRoundStatusTransport(make(demo=True))
    with pytest.raises(PlatformRoundStatusError, match="no hotkey"):
        SubnetPlatformRoundStatusTransport(make(hotkey=""))


def test_the_production_transport_reports_only_the_production_endpoint():
    import types

    cls = type("MinerPlatformClient", (), {})
    cls.__module__ = "utils.platform_client"
    client = cls()
    client.get_round_status = lambda: {}
    client.keypair = types.SimpleNamespace(ss58_address="5F")
    client.config = types.SimpleNamespace(base_url="https://platform.example")
    client.demo = False
    transport = SubnetPlatformRoundStatusTransport(client)
    assert transport.endpoint_path() == PRODUCTION_ENDPOINT_PATH
    assert isinstance(transport, ProductionRoundStatusTransport)


def test_the_two_scopes_share_one_parser(dataset):
    """Equivalence: what a test exercises IS the production validation."""
    payload = round_status_payload(region=dataset["region"])
    fixture = observe_fixture_round_status(FixtureRoundStatusTransport(payload))
    parsed = parse_round_status(payload, endpoint_path=PRODUCTION_ENDPOINT_PATH)
    assert fixture.content() == parsed.identity_content()
    assert fixture.round_id == parsed.round_id
    assert fixture.region_source == parsed.region_source
    assert fixture.expected_bam_sha256 == parsed.expected_bam_sha256
    assert fixture.offered_slots() == tuple(sorted(parsed.urls))


def test_a_fixture_receipt_cannot_reach_the_production_intake(replay):
    with pytest.raises(LiveRoundIntakeError, match="production platform receipt"):
        verify_live_round_intake(receipt=replay["receipt"], downloads=replay["downloads"])
    with pytest.raises(LiveRoundIntakeError, match="production platform receipt"):
        verify_live_round_intake(receipt={"round_id": FRESH_LIVE_ROUND_ID}, downloads=None)


def test_the_production_scope_guard_refuses_a_fixture_chain(replay):
    assert require_production_scope is not None
    with pytest.raises(LiveRoundIntakeError, match="fixture"):
        require_production_scope(replay["intake"])
    with pytest.raises(LiveRoundIntakeError, match="scope"):
        require_production_scope(object())


# --------------------------------------------------------------------------- #
# BLOCKER 2: downloads are bound to the round they came from
# --------------------------------------------------------------------------- #
def test_downloads_are_bound_to_the_receipt(replay):
    downloads = replay["downloads"]
    assert isinstance(downloads, FixtureRoundDownloads)
    assert downloads.receipt_identity == replay["receipt"].identity
    assert downloads.scope == FIXTURE_SCOPE
    assert downloads.bam_source_slot == "bam_presigned_url"
    assert downloads.bai_source_slot == "bam_index_presigned_url"
    assert downloads.byte_counts["bam"] > 0


def test_an_arbitrary_local_file_cannot_become_a_download(replay, dataset, tmp_path):
    """The exact gap: hashing a local file is not provenance."""
    stray = tmp_path / "stray.bam"
    stray.write_bytes(b"not from this round")
    with pytest.raises(PlatformRoundStatusError, match="does not offer"):
        accept_fixture_round_downloads(
            receipt=replay["receipt"],
            bam_source_url="https://somewhere.else/input.bam",
            bam_path=stray.resolve(),
            bai_source_url=FIXTURE_BAI_URL,
            bai_path=Path(dataset["bai"]).resolve(),
        )
    with pytest.raises(PlatformRoundStatusError, match="does not offer"):
        accept_fixture_round_downloads(
            receipt=replay["receipt"],
            bam_source_url=FIXTURE_BAM_URL,
            bam_path=Path(dataset["bam"]).resolve(),
            bai_source_url="https://somewhere.else/input.bai",
            bai_path=Path(dataset["bai"]).resolve(),
        )


def test_an_empty_or_missing_source_url_is_refused(replay, dataset):
    with pytest.raises(PlatformRoundStatusError, match="source URL is empty"):
        accept_fixture_round_downloads(
            receipt=replay["receipt"],
            bam_source_url="   ",
            bam_path=Path(dataset["bam"]).resolve(),
            bai_path=Path(dataset["bai"]).resolve(),
        )


def test_downloads_for_one_round_cannot_be_used_for_another(dataset):
    """receipt A + downloads minted for receipt B."""
    first = build_live_replay(dataset)
    second = build_live_replay(dataset, live_round_id="2026-12-01T00:00:00+00:00")
    assert first["receipt"].identity != second["receipt"].identity
    with pytest.raises(LiveRoundIntakeError, match="different round"):
        observe_fixture_live_round_intake(receipt=first["receipt"], downloads=second["downloads"])


def test_a_locally_built_index_is_recorded_as_such(replay, dataset):
    """The official miner builds the index with samtools when no URL is offered."""
    payload = round_status_payload(region=dataset["region"], bam_index_presigned_url=ABSENT)
    receipt = observe_fixture_round_status(FixtureRoundStatusTransport(payload))
    downloads = accept_fixture_round_downloads(
        receipt=receipt,
        bam_source_url=FIXTURE_BAM_URL,
        bam_path=Path(dataset["bam"]).resolve(),
        bai_path=Path(dataset["bai"]).resolve(),
    )
    assert downloads.bai_source_slot == LOCALLY_INDEXED
    assert "bam_index_presigned_url" not in receipt.offered_slots()


def test_the_platform_published_bam_hash_is_enforced_when_present(dataset):
    """`neurons/miner.py` passes it to the downloader; the engine re-applies it."""
    payload = round_status_payload(region=dataset["region"], bam_sha256="a" * 64)
    receipt = observe_fixture_round_status(FixtureRoundStatusTransport(payload))
    assert receipt.expected_bam_sha256 == "a" * 64
    with pytest.raises(PlatformRoundStatusError, match="SHA-256 the platform published"):
        accept_fixture_round_downloads(
            receipt=receipt,
            bam_source_url=FIXTURE_BAM_URL,
            bam_path=Path(dataset["bam"]).resolve(),
            bai_source_url=FIXTURE_BAI_URL,
            bai_path=Path(dataset["bai"]).resolve(),
        )


def test_a_download_object_cannot_be_hand_built(replay):
    for cls in (ProductionRoundDownloads, FixtureRoundDownloads):
        with pytest.raises(PlatformRoundStatusError, match="may only be minted"):
            cls(
                object(),
                expected=object(),
                scope=PRODUCTION_SCOPE,
                receipt_identity=replay["receipt"].identity,
                bam_sha256="0" * 64,
                bai_sha256="1" * 64,
                bam_source_slot="bam_presigned_url",
                bai_source_slot=LOCALLY_INDEXED,
                byte_counts={},
            )


def test_fixture_downloads_cannot_be_bound_to_a_production_receipt_and_vice_versa(replay, dataset):
    with pytest.raises(PlatformRoundStatusError, match="production receipt"):
        accept_production_round_downloads(
            receipt=replay["receipt"],
            bam_source_url=FIXTURE_BAM_URL,
            bam_path=Path(dataset["bam"]).resolve(),
            bai_path=Path(dataset["bai"]).resolve(),
        )


def test_no_url_reaches_any_identity_or_observation(replay):
    """Presigned URLs expire and carry signatures; they stay operational."""
    surfaces = [
        json.dumps(replay["receipt"].content()),
        json.dumps(replay["receipt"].observation()),
        json.dumps(replay["downloads"].observation()),
        json.dumps(replay["intake"].content()),
        json.dumps(replay["ownership"].anchors),
    ]
    for blob in surfaces:
        lowered = blob.lower()
        for pattern in ("http", "://", "sig=", "?", "fixture.invalid"):
            assert pattern not in lowered, f"{pattern!r} leaked into {blob[:120]}"
    assert set(BAM_URL_SLOTS) and set(BAI_URL_SLOTS)


def test_the_engine_does_not_reimplement_the_downloader():
    source = (REPO_ROOT / "src/minos_engine/protocol/round_status.py").read_text()
    for token in ("httpx", "requests", "urllib", "boto3", "urlretrieve", "s3://"):
        assert token not in source, token


# --------------------------------------------------------------------------- #
# D: the platform must actually be offering a round
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "override,match",
    [
        ({"has_active_round": False}, "no active round"),
        ({"status": "pending"}, "round status"),
        ({"status": "scoring"}, "round status"),
        ({"status": "completed"}, "round status"),
        ({"status": ABSENT}, "round status"),
        ({"round_id": ABSENT}, "no round_id"),
        ({"round_id": ""}, "no round_id"),
        ({"round_id": "2026-01-21T12:00:00"}, "unusable"),
        ({"round_id": "../escape"}, "unusable"),
        ({"region": ABSENT}, "no region"),
        ({"region": "   "}, "no region"),
        ({"region": "chr18"}, "region is unusable"),
        ({"region": "chr18:1M-2M"}, "region is unusable"),
    ],
)
def test_a_round_status_that_is_not_an_open_round_is_refused(dataset, override, match):
    payload = round_status_payload(region=dataset["region"])
    payload = {**payload, **override}
    payload = {k: v for k, v in payload.items() if v is not ABSENT}
    with pytest.raises(PlatformRoundStatusError, match=match):
        observe_fixture_round_status(FixtureRoundStatusTransport(payload))


@pytest.mark.parametrize("payload", [{}, {"has_active_round": True}])
def test_a_malformed_round_status_response_is_refused(payload):
    with pytest.raises(PlatformRoundStatusError):
        observe_fixture_round_status(FixtureRoundStatusTransport(payload))


def test_a_changed_round_id_or_region_changes_the_receipt_and_the_intake(dataset):
    base = build_live_replay(dataset)
    other_round = build_live_replay(dataset, live_round_id="2026-10-01T09:30:00+00:00")
    assert other_round["receipt"].identity != base["receipt"].identity
    assert other_round["intake"].identity != base["intake"].identity

    # a region the platform did not offer is a different round; the receipt and the intake both
    # move, and the real attestation producer would then refuse it against the BAM's own header
    shrunk = dataset["region"].replace(":1-", ":2-")
    moved_receipt = observe_fixture_round_status(
        FixtureRoundStatusTransport(round_status_payload(region=shrunk))
    )
    moved_downloads = accept_fixture_round_downloads(
        receipt=moved_receipt,
        bam_source_url=FIXTURE_BAM_URL,
        bam_path=Path(dataset["bam"]).resolve(),
        bai_source_url=FIXTURE_BAI_URL,
        bai_path=Path(dataset["bai"]).resolve(),
    )
    moved_intake = observe_fixture_live_round_intake(
        receipt=moved_receipt, downloads=moved_downloads
    )
    assert moved_receipt.identity != base["receipt"].identity
    assert moved_intake.identity != base["intake"].identity
    assert moved_intake.region_source == shrunk


def test_a_changed_endpoint_changes_the_receipt_identity(dataset):
    payload = round_status_payload(region=dataset["region"])
    live = observe_fixture_round_status(FixtureRoundStatusTransport(payload))
    demo = observe_fixture_round_status(
        FixtureRoundStatusTransport(payload, endpoint_path="/v2/demo/round-status")
    )
    assert live.identity != demo.identity


def test_the_receipt_identity_excludes_urls_and_timings(replay):
    content = replay["receipt"].content()
    assert set(content) == {"schema_version", "round_id", "region_source", "endpoint_path"}
    blob = json.dumps(content).lower()
    for pattern in ("http", "presigned", "sig=", "time_remaining", "num_mutations"):
        assert pattern not in blob, pattern


def test_the_intake_identity_excludes_operational_values(replay):
    blob = json.dumps(replay["intake"].content()).lower()
    for pattern in ("http", "://", "/home/", "/tmp/", "presigned", "num_mutations"):
        assert pattern not in blob, pattern


def test_substituted_bytes_are_refused_before_an_intake_exists(replay, dataset, tmp_path):
    """The corrective: forged bytes no longer reach ownership to be caught by the profile."""
    forged = tmp_path / "forged.bam"
    forged.write_bytes(Path(dataset["bam"]).read_bytes() + b"\x00")
    # the file is not what this round's BAM URL served, and the only way in names that URL
    downloads = accept_fixture_round_downloads(
        receipt=replay["receipt"],
        bam_source_url=FIXTURE_BAM_URL,
        bam_path=forged.resolve(),
        bai_source_url=FIXTURE_BAI_URL,
        bai_path=Path(dataset["bai"]).resolve(),
    )
    substituted = observe_fixture_live_round_intake(receipt=replay["receipt"], downloads=downloads)
    assert substituted.bam_sha256 != replay["intake"].bam_sha256
    assert substituted.identity != replay["intake"].identity
    # and with a platform-published hash, it never gets even this far
    payload = round_status_payload(
        region=dataset["region"], bam_sha256=replay["downloads"].bam_sha256
    )
    receipt = observe_fixture_round_status(FixtureRoundStatusTransport(payload))
    with pytest.raises(PlatformRoundStatusError, match="SHA-256 the platform published"):
        accept_fixture_round_downloads(
            receipt=receipt,
            bam_source_url=FIXTURE_BAM_URL,
            bam_path=forged.resolve(),
            bai_source_url=FIXTURE_BAI_URL,
            bai_path=Path(dataset["bai"]).resolve(),
        )


def test_a_relative_or_symlinked_path_is_refused(replay, dataset, tmp_path):
    link = tmp_path / "link.bam"
    link.symlink_to(Path(dataset["bam"]).resolve())
    with pytest.raises(PlatformRoundStatusError, match="symlink"):
        accept_fixture_round_downloads(
            receipt=replay["receipt"],
            bam_source_url=FIXTURE_BAM_URL,
            bam_path=link,
            bai_source_url=FIXTURE_BAI_URL,
            bai_path=Path(dataset["bai"]).resolve(),
        )
    relative = tmp_path / "relative.bam"
    relative.write_bytes(b"present but named relatively")
    import os

    previous = Path.cwd()
    os.chdir(tmp_path)
    try:
        with pytest.raises(PlatformRoundStatusError, match="absolute path"):
            accept_fixture_round_downloads(
                receipt=replay["receipt"],
                bam_source_url=FIXTURE_BAM_URL,
                bam_path=Path("relative.bam"),
                bai_source_url=FIXTURE_BAI_URL,
                bai_path=Path(dataset["bai"]).resolve(),
            )
    finally:
        os.chdir(previous)


# --------------------------------------------------------------------------- #
# a fresh live round becomes authoritative
# --------------------------------------------------------------------------- #
def test_a_round_absent_from_the_train_schedule_can_be_owned(replay):
    schedule = json.loads((REPO_ROOT / TRAIN_SCHEDULE_PATH).read_bytes())
    scheduled = {str(row["round_id"]) for batch in schedule["batches"] for row in batch}
    assert len(scheduled) == 50
    assert FRESH_LIVE_ROUND_ID not in scheduled

    ownership = replay["ownership"]
    assert isinstance(ownership, VerifiedRoundProfileAuthority)
    assert ownership.rounds() == (FRESH_LIVE_ROUND_ID,)
    assert ownership.scope == "live"
    assert ownership.partition == LIVE_PARTITION
    assert len(ownership) == 1


def test_the_live_authority_binds_the_whole_identity_chain(replay):
    intake = replay["intake"]
    owned = replay["ownership"].owned(FRESH_LIVE_ROUND_ID)
    manifest = json.loads(replay["manifest_bytes"])
    profile = json.loads(replay["profile_bytes"])

    assert owned.bam_sha256 == str(intake.bam_sha256) == replay["downloads"].bam_sha256
    assert owned.bai_sha256 == str(intake.bai_sha256) == replay["downloads"].bai_sha256
    assert owned.region_hash == str(intake.region_hash)
    assert owned.identity_tuple_hash == str(intake.identity_tuple_hash)
    assert owned.registry_snapshot_hash == intake.identity
    assert owned.dataset_id == replay["dataset_id"]
    assert owned.profile_id == str(profile["profile_id"]) == str(manifest["profile_id"])
    assert owned.attestation_hash == str(replay["attestation"]["attestation_hash"])


def test_the_live_ownership_identity_is_domain_separated_and_round_scoped(replay):
    from minos_engine.common.hashing import sha256_hex

    ownership = replay["ownership"]
    owned = ownership.owned(FRESH_LIVE_ROUND_ID)
    expected = canonical_json_bytes(
        {
            "schema_version": LIVE_OWNERSHIP_SCHEMA,
            "partition": LIVE_PARTITION,
            "anchors": dict(sorted(ownership.anchors.items())),
            "member_count": 1,
            "members": [owned.content()],
        }
    )
    assert ownership.corpus_identity == sha256_hex(LIVE_OWNERSHIP_DOMAIN.encode("utf-8") + expected)
    assert "train_schedule_manifest_sha256" not in ownership.anchors
    assert "split_manifest_sha256" not in ownership.anchors


def test_a_request_naming_the_live_round_is_proved_against_it(replay):
    from minos_engine.layer2.safe_controller import load_verified_safe_baseline_authority
    from minos_engine.layer2.safe_controller_qualification import _request_for

    authority = load_verified_safe_baseline_authority(repo_root=REPO_ROOT)
    ownership = replay["ownership"]
    owned = ownership.owned(FRESH_LIVE_ROUND_ID)
    request = _request_for(owned, authority=authority, mode=ControlMode.SAFE_BASELINE)
    assert ownership.require_owned_request(request) is not None


# --------------------------------------------------------------------------- #
# BLOCKER 2: there is no generic raw-data mint
# --------------------------------------------------------------------------- #
def test_the_generic_raw_data_mint_is_gone():
    """The exact bypass the corrective removes: hand-built maps reaching the token."""
    import minos_engine.layer2.round_profile_authority as module

    assert not hasattr(module, "mint_verified_ownership")
    assert "mint_verified_ownership" not in module.__all__
    for name in dir(module):
        attribute = getattr(module, name)
        if not callable(attribute) or name.startswith("__"):
            continue
        parameters = (
            set(getattr(attribute, "__code__", None).co_varnames[:4])
            if getattr(attribute, "__code__", None)
            else set()
        )
        assert not {"by_round", "corpus_identity"} <= parameters, (
            f"{name} still accepts raw ownership data"
        )


def test_arbitrary_owned_profiles_cannot_mint_ownership(replay):
    """The direct bypass, executed. It must be refused."""
    real = replay["ownership"].owned(FRESH_LIVE_ROUND_ID)
    forged = OwnedRoundProfile(**{**real.content(), "round_id": "2099-01-01T00:00:00+00:00"})

    class ForgedBinding:
        owned = forged
        anchors: dict[str, str] = {}
        identity = "0" * 64

    with pytest.raises(RoundProfileAuthorityError, match="verified live profile binding"):
        own_verified_live_round(ForgedBinding())
    with pytest.raises(RoundProfileAuthorityError, match="verified live profile binding"):
        own_verified_live_round(
            {"by_round": {forged.round_id: forged}, "anchors": {}, "corpus_identity": "0" * 64}
        )
    with pytest.raises(LiveRoundOwnershipError, match="may only be minted by verifying"):
        VerifiedLiveProfileBinding(object(), owned=forged, anchors={}, identity="0" * 64)


def test_the_private_token_constructor_is_still_closed():
    with pytest.raises(RoundProfileAuthorityError, match="verifying loader"):
        VerifiedRoundProfileAuthority(object(), by_round={}, anchors={}, corpus_identity="x" * 64)


def test_a_lookalike_object_is_refused_by_the_controller(replay):
    from minos_engine.layer2.safe_controller import (
        SafeControllerAuthorityError,
        load_verified_safe_baseline_authority,
        select_safe_baseline,
    )
    from minos_engine.layer2.safe_controller_qualification import _request_for

    authority = load_verified_safe_baseline_authority(repo_root=REPO_ROOT)
    owned = replay["ownership"].owned(FRESH_LIVE_ROUND_ID)
    request = _request_for(owned, authority=authority, mode=ControlMode.SAFE_BASELINE)

    class Lookalike:
        scope = "train"
        partition = "train"
        corpus_identity = "0" * 64
        anchors: dict[str, str] = {}

        def owned(self, round_id: str) -> Any:
            return owned

        def require_owned_request(self, request: Any) -> Any:
            return owned

    with pytest.raises(SafeControllerAuthorityError, match="verified round/profile corpus"):
        select_safe_baseline(request=request, authority=authority, ownership=Lookalike())


def test_only_the_live_factory_and_the_train_loader_reach_the_token():
    source = (REPO_ROOT / "src/minos_engine/layer2/round_profile_authority.py").read_text()
    assert source.count("_CORPUS_TOKEN,") == 2


# --------------------------------------------------------------------------- #
# the binding, and the tamper matrix
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "field,value",
    [
        ("round_id", "2026-01-01T00:00:00+00:00"),
        ("dataset_id", "minos-chr21-0279a3b8042f848b"),
        ("chromosome", "chr19"),
        ("bam_sha256", "0" * 64),
        ("bai_sha256", "1" * 64),
        ("reference_sha256", "2" * 64),
        ("fai_sha256", "3" * 64),
        ("region_hash", "4" * 64),
        ("identity_tuple_hash", "5" * 64),
        ("registry_snapshot_hash", "6" * 64),
    ],
)
def test_an_attestation_that_disagrees_with_the_intake_is_refused(dataset, field, value):
    replay = build_live_replay(dataset, attestation_override={field: value})
    with pytest.raises(LiveRoundOwnershipError):
        live_ownership(replay)


def test_an_attestation_that_does_not_hash_to_itself_is_refused(dataset):
    replay = build_live_replay(dataset, attestation_override={"attestation_hash": "0" * 64})
    with pytest.raises(LiveRoundOwnershipError, match="own recorded identity"):
        live_ownership(replay)


def test_a_profile_belonging_to_another_live_round_is_refused(dataset, replay):
    other = build_live_replay(dataset, live_round_id="2026-11-11T11:11:11+00:00")
    with pytest.raises(LiveRoundOwnershipError):
        load_verified_live_round_ownership(
            intake=replay["intake"],
            profile_bytes=other["profile_bytes"],
            manifest_bytes=other["manifest_bytes"],
            attestation_bytes=other["attestation_bytes"],
            windows_bytes=other["windows_bytes"],
        )


@pytest.mark.parametrize("artifact", ["profile", "manifest", "attestation", "windows"])
def test_an_empty_artifact_is_refused(replay, artifact):
    payload = {
        "intake": replay["intake"],
        "profile_bytes": replay["profile_bytes"],
        "manifest_bytes": replay["manifest_bytes"],
        "attestation_bytes": replay["attestation_bytes"],
        "windows_bytes": replay["windows_bytes"],
    }
    payload[f"{artifact}_bytes"] = b""
    with pytest.raises(LiveRoundOwnershipError, match="empty"):
        load_verified_live_round_ownership(**payload)


def test_tampered_artifact_bytes_are_refused(replay):
    for field in ("profile_bytes", "windows_bytes"):
        payload = {
            "intake": replay["intake"],
            "profile_bytes": replay["profile_bytes"],
            "manifest_bytes": replay["manifest_bytes"],
            "attestation_bytes": replay["attestation_bytes"],
            "windows_bytes": replay["windows_bytes"],
        }
        payload[field] = payload[field] + b"\x00"
        with pytest.raises(LiveRoundOwnershipError):
            load_verified_live_round_ownership(**payload)


def test_unparseable_artifact_bytes_are_refused(replay):
    with pytest.raises(LiveRoundOwnershipError, match="not JSON"):
        load_verified_live_round_ownership(
            intake=replay["intake"],
            profile_bytes=b"{not json",
            manifest_bytes=replay["manifest_bytes"],
            attestation_bytes=replay["attestation_bytes"],
            windows_bytes=replay["windows_bytes"],
        )


def test_a_binding_is_required_and_is_itself_verified(replay):
    binding = verify_live_profile_binding(
        intake=replay["intake"],
        profile_bytes=replay["profile_bytes"],
        manifest_bytes=replay["manifest_bytes"],
        attestation_bytes=replay["attestation_bytes"],
        windows_bytes=replay["windows_bytes"],
    )
    assert isinstance(binding, VerifiedLiveProfileBinding)
    assert binding.owned.partition == LIVE_PARTITION
    assert own_verified_live_round(binding).rounds() == (FRESH_LIVE_ROUND_ID,)
    with pytest.raises(LiveRoundOwnershipError, match="verified live intake"):
        verify_live_profile_binding(
            intake={"round_id": FRESH_LIVE_ROUND_ID},
            profile_bytes=replay["profile_bytes"],
            manifest_bytes=replay["manifest_bytes"],
            attestation_bytes=replay["attestation_bytes"],
            windows_bytes=replay["windows_bytes"],
        )


@pytest.mark.parametrize(
    "field",
    [
        "profile_id",
        "region_hash",
        "bam_sha256",
        "bai_sha256",
        "reference_sha256",
        "fai_sha256",
        "fingerprint_hash",
        "profile_manifest_sha256",
    ],
)
def test_a_request_that_disagrees_with_the_owned_live_profile_is_refused(replay, field):
    from minos_engine.layer2.safe_controller import load_verified_safe_baseline_authority
    from minos_engine.layer2.safe_controller_qualification import _request_for

    authority = load_verified_safe_baseline_authority(repo_root=REPO_ROOT)
    owned = replay["ownership"].owned(FRESH_LIVE_ROUND_ID)
    override = {field: "0" * (32 if field == "profile_id" else 64)}
    request = _request_for(owned, authority=authority, mode=ControlMode.SAFE_BASELINE, **override)
    with pytest.raises(RoundProfileAuthorityError):
        replay["ownership"].require_owned_request(request)


def test_a_request_for_a_round_this_authority_does_not_own_is_refused(replay):
    with pytest.raises(RoundProfileAuthorityError, match="not in the accepted profile snapshot"):
        replay["ownership"].owned("2099-01-01T00:00:00+00:00")


# --------------------------------------------------------------------------- #
# F: the TRAIN schedule is not consulted for LIVE
# --------------------------------------------------------------------------- #
FORBIDDEN_FOR_LIVE = (
    "l2f2_train_schedule_v1.json",
    "l2f2_phase_a_execution_authority_v1.json",
    "layer2_dataset_split_v1.json",
    "layer2_dataset_split_v2_epoch1.json",
    "profile_snapshot_epoch1_members.json",
    "profile_snapshot_epoch1_selections.json",
)


def test_live_ownership_verifies_while_the_research_authorities_are_unreadable(
    dataset, monkeypatch
):
    import builtins
    import io

    opened: list[str] = []
    original_open, original_io = builtins.open, io.open
    original_read_bytes = Path.read_bytes

    def guard(name: str) -> None:
        if name in FORBIDDEN_FOR_LIVE:
            opened.append(name)
            raise AssertionError(f"the live path opened a research authority: {name}")

    monkeypatch.setattr(
        builtins,
        "open",
        lambda f, *a, **k: (guard(Path(str(f)).name), original_open(f, *a, **k))[1],
    )
    monkeypatch.setattr(
        io, "open", lambda f, *a, **k: (guard(Path(str(f)).name), original_io(f, *a, **k))[1]
    )
    monkeypatch.setattr(
        Path, "read_bytes", lambda self: (guard(self.name), original_read_bytes(self))[1]
    )

    fresh = build_live_replay(dataset, live_round_id="2026-10-01T09:30:00+00:00")
    ownership = live_ownership(fresh)
    assert opened == []
    assert ownership.rounds() == ("2026-10-01T09:30:00+00:00",)


def test_the_live_modules_never_name_a_research_authority():
    """Checked against real string literals; the docstrings explain what is avoided."""
    for name in ("live_round_intake.py", "live_round_authority.py"):
        tree = ast.parse((REPO_ROOT / "src/minos_engine/layer2" / name).read_text())
        docstrings = {
            id(node.body[0].value)
            for node in ast.walk(tree)
            if isinstance(node, ast.Module | ast.FunctionDef | ast.ClassDef)
            and node.body
            and isinstance(node.body[0], ast.Expr)
            and isinstance(node.body[0].value, ast.Constant)
            and isinstance(node.body[0].value.value, str)
        }
        literals = [
            node.value
            for node in ast.walk(tree)
            if isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and id(node) not in docstrings
        ]
        for literal in literals:
            for forbidden in (*FORBIDDEN_FOR_LIVE, "train_schedule", "split_manifest"):
                assert forbidden not in literal, f"{name} names {forbidden}"


# --------------------------------------------------------------------------- #
# H / M: manifest version, and the TRAIN authority
# --------------------------------------------------------------------------- #
def test_a_live_authority_cannot_produce_a_v1_decision_manifest(replay):
    from minos_engine.layer2.safe_controller import (
        SafeControllerAuthorityError,
        load_verified_safe_baseline_authority,
        select_safe_baseline,
    )
    from minos_engine.layer2.safe_controller_qualification import _request_for

    authority = load_verified_safe_baseline_authority(repo_root=REPO_ROOT)
    owned = replay["ownership"].owned(FRESH_LIVE_ROUND_ID)
    request = _request_for(owned, authority=authority, mode=ControlMode.SAFE_BASELINE)
    with pytest.raises(SafeControllerAuthorityError, match="decision-manifest v2"):
        select_safe_baseline(request=request, authority=authority, ownership=replay["ownership"])


def test_the_train_corpus_identity_has_not_moved():
    train = load_verified_round_profile_corpus(root=REPO_ROOT)
    assert train.corpus_identity == ACCEPTED_TRAIN_CORPUS_IDENTITY
    assert len(train) == 50
    assert train.scope == "train"
    assert "train_schedule_manifest_sha256" in train.anchors


def test_the_train_route_still_produces_a_v1_manifest():
    from minos_engine.layer2.safe_controller import (
        SAFE_DECISION_MANIFEST_SCHEMA,
        load_verified_safe_baseline_authority,
        safe_decision_manifest_content,
        select_safe_baseline,
    )
    from minos_engine.layer2.safe_controller_qualification import _request_for

    authority = load_verified_safe_baseline_authority(repo_root=REPO_ROOT)
    train = load_verified_round_profile_corpus(root=REPO_ROOT)
    owned = train.owned(train.rounds()[0])
    request = _request_for(owned, authority=authority, mode=ControlMode.SAFE_BASELINE)
    result = select_safe_baseline(request=request, authority=authority, ownership=train)
    assert result.selected_config.sha256 == authority.baseline_config_hash
    manifest = safe_decision_manifest_content(request=request, authority=authority, ownership=train)
    assert manifest["schema_version"] == SAFE_DECISION_MANIFEST_SCHEMA
    assert manifest["profile_corpus_identity"] == ACCEPTED_TRAIN_CORPUS_IDENTITY


def test_the_train_authority_still_refuses_a_live_round():
    train = load_verified_round_profile_corpus(root=REPO_ROOT)
    with pytest.raises(RoundProfileAuthorityError):
        train.owned(FRESH_LIVE_ROUND_ID)


def test_the_service_is_still_blocked_and_no_gate_was_issued():
    from minos_engine.common.errors import StageNotReadyError
    from minos_engine.layer2.service import Layer2Service

    with pytest.raises(StageNotReadyError):
        Layer2Service().select_config(None)
    assert not (REPO_ROOT / "gates/models-qualified.json").exists()
    assert not (REPO_ROOT / "gates/controller-frozen.json").exists()


def test_the_transport_abstraction_is_the_only_seam():
    assert issubclass(FixtureRoundStatusTransport, RoundStatusTransport)
    source = (REPO_ROOT / "src/minos_engine/protocol/round_status.py").read_text()
    assert "httpx" not in source and "requests" not in source
