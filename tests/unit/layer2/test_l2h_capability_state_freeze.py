"""Post-mint immutability, and the removal of token-bearing class attributes.

Two defects, both of which left a *correctly minted, correctly sealed* capability compromised.

1. **Sealed but mutable.** The seal attested to the state a verifier checked, and nothing stopped
   any later statement from replacing that state. A legitimately loaded
   ``VerifiedRoundProfileAuthority`` accepted ``partition = "live"``, a forged ``corpus_identity``,
   an inserted ``_by_round`` member and an edited ``OwnedRoundProfile`` -- and still answered True
   to its own seal check.

2. **`_expected_token` handed the mint token out.** ``VerifiedProductionLiveRoundIntake``
   published the real production token as a class attribute, so ordinary code could read it and
   mint a genuine production capability from a fixture chain. That reversed the entire
   production/fixture split the previous corrective established.

The trust boundary these tests operate inside is written down in
``docs/layer2/L2H_CAPABILITY_TRUST_MODEL.md``. Nothing here claims that code which explicitly
imports a module-private underscore global can be stopped; that is outside the documented model
and is stated as such.
"""

from __future__ import annotations

import importlib
from pathlib import Path
from typing import Any

import pytest

from minos_engine.layer2.contracts import ControlMode
from minos_engine.layer2.live_round_authority import (
    FixtureLiveProfileBinding,
    LiveRoundOwnershipError,
    VerifiedLiveProfileBinding,
    VerifiedProductionLiveProfileBinding,
    observe_fixture_live_profile_binding,
)
from minos_engine.layer2.live_round_intake import (
    FixtureLiveRoundIntake,
    LiveRoundIntakeError,
    VerifiedLiveRoundIntake,
    VerifiedProductionLiveRoundIntake,
)
from minos_engine.layer2.round_profile_authority import (
    OWNED_PROFILE_FIELDS,
    RoundProfileAuthorityError,
    VerifiedRoundProfileAuthority,
    is_verified_round_profile_authority,
    load_verified_round_profile_corpus,
)
from minos_engine.layer2.safe_controller import (
    SafeBaselineController,
    SafeControllerAuthorityError,
    VerifiedSafeBaselineAuthority,
    is_verified_safe_baseline_authority,
    load_verified_safe_baseline_authority,
    select_safe_baseline,
)
from minos_engine.protocol.round_status import (
    FixtureRoundDownloads,
    FixtureRoundStatusReceipt,
    PlatformRoundStatusError,
    ProductionRoundDownloads,
    ProductionRoundStatusReceipt,
    ProductionRoundStatusTransport,
    VerifiedOfficialMiner,
    VerifiedProductionPlatformClient,
    is_verified_production_downloads,
    is_verified_production_receipt,
)
from tests.layer2_live_replay import (
    accept_synthetic_reference,
    build_live_dataset,
    build_live_replay,
    live_ownership,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
ACCEPTED_TRAIN_CORPUS_IDENTITY = "9cc53b5d28c8a8da34c25095362c09d8cb1fb57533ff0a0b3e1fdf7000970b03"

#: Every sealed capability in the chain, so §E's audit is executable rather than prose.
SEALED_CAPABILITIES: tuple[type, ...] = (
    VerifiedProductionPlatformClient,
    VerifiedOfficialMiner,
    ProductionRoundStatusTransport,
    ProductionRoundStatusReceipt,
    FixtureRoundStatusReceipt,
    ProductionRoundDownloads,
    FixtureRoundDownloads,
    VerifiedProductionLiveRoundIntake,
    FixtureLiveRoundIntake,
    VerifiedProductionLiveProfileBinding,
    FixtureLiveProfileBinding,
    VerifiedRoundProfileAuthority,
    VerifiedSafeBaselineAuthority,
)


@pytest.fixture(scope="module")
def dataset(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    return build_live_dataset(tmp_path_factory.mktemp("freeze_live"))


@pytest.fixture(autouse=True)
def _accepted_reference(monkeypatch: pytest.MonkeyPatch, dataset: dict[str, Any]) -> None:
    accept_synthetic_reference(monkeypatch, dataset)


@pytest.fixture
def replay(dataset: dict[str, Any]) -> dict[str, Any]:
    built = build_live_replay(dataset)
    built["ownership"] = live_ownership(built)
    return built


@pytest.fixture(scope="module")
def train() -> Any:
    return load_verified_round_profile_corpus(root=REPO_ROOT)


@pytest.fixture(scope="module")
def safe_authority() -> Any:
    return load_verified_safe_baseline_authority(repo_root=REPO_ROOT)


def _refuses(capability: Any, field: str, value: Any, error: type[Exception]) -> None:
    with pytest.raises(error):
        setattr(capability, field, value)


# --------------------------------------------------------------------------- #
# BLOCKER 4 / §I: no exported class carries its own mint token
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("capability", SEALED_CAPABILITIES, ids=lambda c: c.__name__)
def test_no_exported_capability_publishes_its_mint_token(capability: type) -> None:
    """The exact attack: ``token = SomeCapability._expected_token`` then mint.

    ``VerifiedProductionLiveRoundIntake._expected_token`` was the real production token. Reading
    it and calling the constructor produced a genuine production intake that
    ``require_production_scope`` accepted -- from a fixture chain, with nothing else forged.
    """
    assert not hasattr(capability, "_expected_token")
    # nor under any other name: no class attribute may BE a bare sentinel object
    for name in dir(capability):
        if name.startswith("__"):
            continue
        value = getattr(capability, name, None)
        assert type(value) is not object, (
            f"{capability.__name__}.{name} is a bare object() on an exported class, which is what "
            "a mint token looks like"
        )


@pytest.mark.parametrize(
    "module_name",
    [
        "minos_engine.layer2.live_round_intake",
        "minos_engine.layer2.live_round_authority",
        "minos_engine.layer2.round_profile_authority",
        "minos_engine.layer2.safe_controller",
        "minos_engine.protocol.round_status",
    ],
)
def test_no_public_function_returns_a_mint_token(module_name: str) -> None:
    """No exported name is a token, and no public callable hands one back."""
    module = importlib.import_module(module_name)
    for name in getattr(module, "__all__", ()):
        assert not name.startswith("_")
        assert type(getattr(module, name)) is not object, f"{module_name}.{name} is a bare token"


def test_the_token_tables_are_keyed_by_exact_type_so_a_subclass_inherits_nothing() -> None:
    """A subclass is absent from the table rather than inheriting a token from its base."""
    from minos_engine.layer2.live_round_intake import _mint_token as intake_token

    class Sub(VerifiedProductionLiveRoundIntake):
        pass

    assert intake_token(VerifiedProductionLiveRoundIntake) is not None
    assert intake_token(Sub) is None
    assert intake_token(VerifiedLiveRoundIntake) is None, "the abstract base mints nothing"


def test_the_abstract_bases_cannot_be_constructed(replay) -> None:
    with pytest.raises(LiveRoundIntakeError, match="may only be minted"):
        VerifiedLiveRoundIntake(object(), content=replay["intake"].content(), identity="0" * 64)
    with pytest.raises(LiveRoundOwnershipError, match="may only be minted"):
        VerifiedLiveProfileBinding(object(), owned=None, anchors={}, identity="0" * 64)


# --------------------------------------------------------------------------- #
# §K: TRAIN ownership
# --------------------------------------------------------------------------- #
def test_a_verified_round_profile_authority_cannot_be_edited_after_minting(train) -> None:
    assert is_verified_round_profile_authority(train)
    assert train.corpus_identity == ACCEPTED_TRAIN_CORPUS_IDENTITY
    assert train.partition == "train"

    for field, value in (
        ("partition", "live"),
        ("corpus_identity", "f" * 64),
        ("_seal", object()),
        ("_by_round", {}),
        ("_anchors", {}),
    ):
        _refuses(train, field, value, RoundProfileAuthorityError)
    with pytest.raises(RoundProfileAuthorityError):
        del train.partition

    # nested: the internal mappings are read-only views, not the dicts they were built from
    with pytest.raises(TypeError):
        train._by_round["forged-round"] = None
    with pytest.raises(TypeError):
        train._anchors["forged"] = "x"

    assert train.corpus_identity == ACCEPTED_TRAIN_CORPUS_IDENTITY
    assert train.partition == "train"
    assert len(train) == 50
    assert is_verified_round_profile_authority(train)


def test_the_anchors_a_caller_is_handed_are_detached(train) -> None:
    handed = train.anchors
    handed["forged"] = "x"
    handed.pop("registry_snapshot_hash", None)
    assert "forged" not in train.anchors
    assert train.registry_snapshot_hash


def test_an_owned_round_profile_cannot_be_edited(train) -> None:
    """Even with a read-only ``_by_round``, a writable member was a writable authority."""
    owned = train.owned(train.rounds()[0])
    before = owned.content()
    for field in OWNED_PROFILE_FIELDS:
        _refuses(owned, field, "0" * 64, RoundProfileAuthorityError)
    assert owned.content() == before
    assert train.owned(train.rounds()[0]).content() == before


def test_the_owned_profile_content_excludes_the_runtime_marker(train) -> None:
    """``content()`` is a scientific identity and must not learn about ``_frozen``."""
    owned = train.owned(train.rounds()[0])
    content = owned.content()
    assert set(content) == set(OWNED_PROFILE_FIELDS)
    assert "_frozen" not in content
    assert "_frozen" in type(owned).__slots__


def test_the_fixture_terminus_is_frozen_too(replay) -> None:
    """It carries no seal, so freezing cannot be keyed off one."""
    fixture = replay["ownership"]
    assert not is_verified_round_profile_authority(fixture)
    for field, value in (("partition", "train"), ("corpus_identity", "0" * 64)):
        _refuses(fixture, field, value, RoundProfileAuthorityError)


# --------------------------------------------------------------------------- #
# §K: the live profile binding
# --------------------------------------------------------------------------- #
def test_a_live_profile_binding_cannot_be_edited_between_proof_and_use(replay) -> None:
    binding = observe_fixture_live_profile_binding(
        intake=replay["intake"],
        profile_bytes=replay["profile_bytes"],
        manifest_bytes=replay["manifest_bytes"],
        attestation_bytes=replay["attestation_bytes"],
        windows_bytes=replay["windows_bytes"],
    )
    identity, owned = binding.identity, binding.owned
    for field, value in (
        ("owned", None),
        ("identity", "0" * 64),
        ("_seal", object()),
        ("_anchors", {}),
    ):
        _refuses(binding, field, value, LiveRoundOwnershipError)
    with pytest.raises(TypeError):
        binding._anchors["forged"] = "x"
    handed = binding.anchors
    handed["forged"] = "x"

    assert binding.identity == identity
    assert binding.owned is owned
    assert "forged" not in binding.anchors
    # and the profile it holds is frozen in its own right
    _refuses(binding.owned, "bam_sha256", "0" * 64, RoundProfileAuthorityError)


# --------------------------------------------------------------------------- #
# §K: the safe-baseline authority
# --------------------------------------------------------------------------- #
def test_a_safe_baseline_authority_cannot_be_edited_after_minting(safe_authority) -> None:
    assert is_verified_safe_baseline_authority(safe_authority)
    before = safe_authority.baseline_config_hash

    for field, value in (
        ("baseline_config_hash", "0" * 64),
        ("baseline_uri", "file:///evil"),
        ("baseline_payload_sha256", "0" * 64),
        ("parameter_space_hash", "1" * 64),
        ("policy_hash", "2" * 64),
        ("source_commit", "dead"),
        ("source_tree", "dead"),
        ("_seal", object()),
        ("_policy", {}),
        ("_entry_gate_checks", {}),
    ):
        _refuses(safe_authority, field, value, SafeControllerAuthorityError)

    with pytest.raises(TypeError):
        safe_authority._policy["allowed_modes"] = ("EVIL",)
    with pytest.raises(TypeError):
        safe_authority._entry_gate_checks["forged"] = False

    assert safe_authority.baseline_config_hash == before
    assert safe_authority.allowed_modes == ("SAFE_BASELINE",)
    assert is_verified_safe_baseline_authority(safe_authority)


def test_the_policy_a_caller_is_handed_is_a_detached_deep_copy(safe_authority) -> None:
    """Shallow was not enough: the nested containers were shared with the authority."""
    handed = safe_authority.policy
    handed["allowed_modes"] = ["EVIL"]
    handed["injected"] = True
    for value in handed.values():
        if isinstance(value, list):
            value.append("injected")
        elif isinstance(value, dict):
            value["injected"] = True

    assert "injected" not in safe_authority.policy
    assert safe_authority.allowed_modes == ("SAFE_BASELINE",)
    assert safe_authority.policy["allowed_modes"] == ["SAFE_BASELINE"]

    checks = safe_authority.entry_gate_checks
    checks["forged"] = False
    assert "forged" not in safe_authority.entry_gate_checks


def test_the_policy_a_caller_is_handed_is_plain_json_shaped(safe_authority) -> None:
    """It is canonicalized into decision identities, so it must be dict/list, never proxy/tuple."""
    from minos_engine.common.canonical_json import canonical_json_bytes

    handed = safe_authority.policy
    assert type(handed) is dict
    assert type(handed["allowed_modes"]) is list
    canonical_json_bytes(handed)  # would raise on a MappingProxyType/tuple
    assert type(safe_authority.entry_gate_checks) is dict


def test_a_frozen_authority_still_reaches_the_controller(train, safe_authority) -> None:
    """Freezing must not break the supported path."""
    from minos_engine.layer2.safe_controller_qualification import _request_for

    owned = train.owned(train.rounds()[0])
    request = _request_for(owned, authority=safe_authority, mode=ControlMode.SAFE_BASELINE)
    result = select_safe_baseline(request=request, authority=safe_authority, ownership=train)
    assert result.decision.decision_manifest_hash
    assert SafeBaselineController(safe_authority, train).authority is safe_authority


# --------------------------------------------------------------------------- #
# §G / §H: intake, receipt and downloads
# --------------------------------------------------------------------------- #
def test_a_verified_intake_has_no_mutable_internal_state(replay) -> None:
    intake = replay["intake"]
    content = intake.content()
    for field, value in (
        ("scope", "production"),
        ("identity", "0" * 64),
        ("receipt_identity", "0" * 64),
        ("dataset_id", "forged"),
        ("_seal", object()),
        ("_content", {}),
    ):
        _refuses(intake, field, value, LiveRoundIntakeError)
    with pytest.raises(TypeError):
        intake._content["round_id"] = "forged"

    handed = intake.content()
    handed["round_id"] = "forged"
    assert intake.content() == content
    assert intake.scope == "fixture"


def test_a_receipt_cannot_be_edited_after_minting(replay) -> None:
    receipt = replay["receipt"]
    identity, round_id = receipt.identity, receipt.round_id
    for field, value in (
        ("identity", "0" * 64),
        ("scope", "production"),
        ("_seal", object()),
        ("_parsed", None),
        ("_operational_binding", object()),
    ):
        _refuses(receipt, field, value, PlatformRoundStatusError)

    # and through the parsed payload it wraps
    for field, value in (
        ("round_id", "forged"),
        ("region_source", "chr1:1-2"),
        ("endpoint_path", "/v2/demo/round-status"),
        ("expected_bam_sha256", "0" * 64),
        ("_urls", {}),
    ):
        _refuses(receipt._parsed, field, value, PlatformRoundStatusError)
    with pytest.raises(TypeError):
        receipt._parsed._urls["bam_presigned_url"] = "https://evil.example/x"

    handed = receipt._parsed.urls
    handed["bam_presigned_url"] = "https://evil.example/x"
    assert receipt.identity == identity
    assert receipt.round_id == round_id
    assert receipt._parsed.urls != handed


def test_downloads_cannot_be_edited_after_minting(replay) -> None:
    downloads = replay["downloads"]
    bam, bai = downloads.bam_sha256, downloads.bai_sha256
    for field, value in (
        ("bam_sha256", "0" * 64),
        ("bai_sha256", "0" * 64),
        ("receipt_identity", "0" * 64),
        ("bam_source_slot", "forged"),
        ("bai_source_slot", "forged"),
        ("scope", "production"),
        ("_seal", object()),
        ("_operational_binding", object()),
        ("_byte_counts", {}),
    ):
        _refuses(downloads, field, value, PlatformRoundStatusError)
    with pytest.raises(TypeError):
        downloads._byte_counts["bam"] = 0

    handed = downloads.byte_counts
    handed["bam"] = 0
    assert downloads.bam_sha256 == bam
    assert downloads.bai_sha256 == bai
    assert downloads.byte_counts != handed
    assert not is_verified_production_downloads(downloads)
    assert not is_verified_production_receipt(replay["receipt"])


def test_the_external_client_wrapper_snapshots_what_was_verified() -> None:
    """§E: the engine cannot freeze a ``minos_subnet`` object, so it records what it checked."""
    assert "_binding" in VerifiedProductionPlatformClient.__slots__
    assert "_frozen" in VerifiedProductionPlatformClient.__slots__
    # the snapshot is exposed read-only, never as a writable attribute
    for name in (
        "verified_base_url",
        "verified_hotkey_ss58",
        "verified_round_status_path",
        "verified_demo",
    ):
        accessor = getattr(VerifiedProductionPlatformClient, name)
        assert isinstance(accessor, property) and accessor.fset is None
    assert "_frozen" in VerifiedOfficialMiner.__slots__
    # the live object is still reachable for its behaviour, and only through a read accessor
    assert isinstance(VerifiedProductionPlatformClient.client, property)
    assert VerifiedProductionPlatformClient.client.fset is None
    assert isinstance(VerifiedOfficialMiner.miner, property)
    assert VerifiedOfficialMiner.miner.fset is None


@pytest.mark.parametrize("capability", SEALED_CAPABILITIES, ids=lambda c: c.__name__)
def test_every_sealed_capability_is_frozen_after_mint(capability: type) -> None:
    """§E as an executable audit: no sealed capability may be left writable."""
    from minos_engine.common.frozen_state import FrozenAfterMint

    assert issubclass(capability, FrozenAfterMint), f"{capability.__name__} is not frozen"
    slots = {name for klass in capability.__mro__ for name in getattr(klass, "__slots__", ())}
    assert "_frozen" in slots, f"{capability.__name__} declares no _frozen slot"


# --------------------------------------------------------------------------- #
# §L: runtime immutability must not contaminate scientific canonical content
# --------------------------------------------------------------------------- #
def test_the_train_corpus_identity_did_not_move(train) -> None:
    assert train.corpus_identity == ACCEPTED_TRAIN_CORPUS_IDENTITY
    assert len(train) == 50
    assert train.scope == "train"


def test_no_runtime_marker_reaches_any_canonical_content(train, safe_authority, replay) -> None:
    """``_frozen`` and ``_seal`` are runtime capability state and are not scientific facts."""
    import json

    from minos_engine.layer2.safe_controller import safe_decision_manifest_content
    from minos_engine.layer2.safe_controller_qualification import _request_for

    owned = train.owned(train.rounds()[0])
    request = _request_for(owned, authority=safe_authority, mode=ControlMode.SAFE_BASELINE)
    documents = (
        train.identity_content(),
        owned.content(),
        replay["intake"].content(),
        replay["ownership"].identity_content(),
        replay["downloads"].observation(),
        safe_decision_manifest_content(request=request, authority=safe_authority, ownership=train),
    )
    for document in documents:
        serialized = json.dumps(document, sort_keys=True, default=str)
        assert "_frozen" not in serialized
        assert "_seal" not in serialized
        assert "frozen" not in serialized


def test_every_canonical_document_is_plain_json_shaped(train, safe_authority, replay) -> None:
    """A ``MappingProxyType`` or ``tuple`` leaking into a document would silently move a hash."""
    from minos_engine.common.canonical_json import canonical_json_bytes

    for document in (
        train.identity_content(),
        train.anchors,
        train.owned(train.rounds()[0]).content(),
        replay["intake"].content(),
        replay["ownership"].anchors,
        replay["downloads"].observation(),
        safe_authority.policy,
        safe_authority.entry_gate_checks,
    ):
        assert type(document) is dict
        canonical_json_bytes(document)


def test_the_v1_decision_manifest_is_byte_identical_for_unchanged_train_inputs(
    train, safe_authority
) -> None:
    """Freezing changes runtime behaviour only; the decision content must be untouched.

    The two checkout-provenance fields are excluded because they name the *current* commit by
    design and therefore change on every commit, this one included. Everything a decision actually
    asserts is compared byte for byte against the recorded values.
    """
    from minos_engine.common.canonical_json import canonical_json_bytes
    from minos_engine.common.hashing import sha256_hex
    from minos_engine.layer2.safe_controller import safe_decision_manifest_content
    from minos_engine.layer2.safe_controller_qualification import _request_for

    #: Recorded at entry HEAD 98e1a3dcae22fb7c7d055f95254b64757ddbd854, over the manifest with
    #: `execution_source_commit`/`execution_source_tree` removed.
    accepted = {
        "0279a3b8042f848b": "b92af1cbc1c569af968440010b511d524ff9279829096c32b62fdaab4d7e8b8b",
        "028662fb934529d7": "d1df629acaa642cb7bd268890a01225407745e7e14e293f12ed88090963dc0ff",
        "031b9ed22a572248": "7cb2283a59a09413da933157a97c7a3152bce89439bde0577690e9e1c6e49a2a",
    }
    for round_id, expected in accepted.items():
        owned = train.owned(round_id)
        request = _request_for(owned, authority=safe_authority, mode=ControlMode.SAFE_BASELINE)
        manifest = safe_decision_manifest_content(
            request=request, authority=safe_authority, ownership=train
        )
        del manifest["execution_source_commit"]
        del manifest["execution_source_tree"]
        assert sha256_hex(canonical_json_bytes(manifest)) == expected, round_id
