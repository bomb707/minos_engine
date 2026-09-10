"""``l2h-safe-decision-manifest-v2``: one SAFE decision against one authenticated LIVE round.

v1 is the TRAIN contract and stays exactly what it was. Four of its fields describe the frozen
research campaign and have no live counterpart -- ``dataset_id`` (a registered research dataset),
``registry_snapshot_hash`` (the frozen registry snapshot), ``profile_corpus_identity`` (the
fifty-member corpus) and ``profile_ownership_anchors`` (Phase-A / schedule / split / protocol) --
so emitting live values under those names would be a reinterpretation without a rename. v2 drops
all four and publishes the live authorities under their own names.

**The seam these tests use.** ``build_production_live_round`` drives the real production verifiers
end to end and monkeypatches only the two official type resolvers plus the accepted reference
table. No private mint token is imported and no capability is hand-built, so the ownership under
test is a genuine sealed ``VerifiedRoundProfileAuthority(scope="live")``. It is still a test seam
and it is **not** qualification evidence -- this task is source only.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from minos_engine.layer2.contracts import ControlMode, FallbackReason
from minos_engine.layer2.round_profile_authority import (
    is_verified_round_profile_authority,
    load_verified_round_profile_corpus,
)
from minos_engine.layer2.safe_controller import (
    SAFE_DECISION_MANIFEST_DOMAIN,
    SAFE_DECISION_MANIFEST_SCHEMA,
    SAFE_DECISION_MANIFEST_V2_DOMAIN,
    SAFE_DECISION_MANIFEST_V2_SCHEMA,
    SafeBaselineController,
    SafeControllerAuthorityError,
    live_safe_decision_manifest_content,
    live_safe_decision_manifest_identity,
    load_verified_safe_baseline_authority,
    safe_decision_identity_for,
    safe_decision_manifest_content,
    safe_decision_manifest_for,
    safe_decision_manifest_identity,
    select_safe_baseline,
)
from minos_engine.layer2.safe_controller_qualification import _request_for
from tests.layer2_live_replay import (
    FRESH_LIVE_ROUND_ID,
    build_live_dataset,
    build_live_replay,
    build_production_live_round,
    live_ownership,
)

REPO_ROOT = Path(__file__).resolve().parents[3]

ACCEPTED_SAFE_CONFIG = "157d88d1587c13be395c62d60e27d1becdada78fad45e65d883bc1190e51acea"
ACCEPTED_TRAIN_CORPUS_IDENTITY = "9cc53b5d28c8a8da34c25095362c09d8cb1fb57533ff0a0b3e1fdf7000970b03"

#: v1 fields that must NOT reappear in v2 under the same name.
TRAIN_ONLY_V1_FIELDS = (
    "dataset_id",
    "registry_snapshot_hash",
    "profile_corpus_identity",
    "profile_ownership_anchors",
)

#: The LIVE authority block v2 adds. Every one comes from the sealed ownership capability.
LIVE_AUTHORITY_FIELDS = (
    "live_input_set_id",
    "live_intake_identity",
    "live_intake_schema",
    "live_profile_ownership_anchors",
    "live_profile_ownership_identity",
    "ownership_scope",
    "platform_receipt_identity",
)


@pytest.fixture(scope="module")
def dataset(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    return build_live_dataset(tmp_path_factory.mktemp("v2_live"))


@pytest.fixture
def live(monkeypatch: pytest.MonkeyPatch, dataset: dict[str, Any]) -> dict[str, Any]:
    """A genuine production-equivalent LIVE ownership authority."""
    return build_production_live_round(monkeypatch, dataset)


@pytest.fixture(scope="module")
def authority() -> Any:
    return load_verified_safe_baseline_authority(repo_root=REPO_ROOT)


@pytest.fixture(scope="module")
def train() -> Any:
    return load_verified_round_profile_corpus(root=REPO_ROOT)


def _live_request(live: dict[str, Any], authority: Any, mode: ControlMode) -> Any:
    ownership = live["ownership"]
    owned = ownership.owned(ownership.rounds()[0])
    return _request_for(owned, authority=authority, mode=mode)


def _train_request(train: Any, authority: Any) -> Any:
    return _request_for(
        train.owned(train.rounds()[0]), authority=authority, mode=ControlMode.SAFE_BASELINE
    )


# --------------------------------------------------------------------------- #
# §O: the whole production chain, ending in a v2 decision
# --------------------------------------------------------------------------- #
def test_the_production_chain_reaches_a_live_ownership_authority(live) -> None:
    """No private token anywhere: every seal was earned from the verifier that owns it."""
    from minos_engine.protocol.round_status import (
        ProductionRoundDownloads,
        ProductionRoundStatusReceipt,
        is_verified_production_downloads,
        is_verified_production_receipt,
    )

    assert type(live["receipt"]) is ProductionRoundStatusReceipt
    assert is_verified_production_receipt(live["receipt"])
    assert type(live["downloads"]) is ProductionRoundDownloads
    assert is_verified_production_downloads(live["downloads"])
    assert live["intake"].scope == "production"

    ownership = live["ownership"]
    assert is_verified_round_profile_authority(ownership)
    assert ownership.scope == "live"
    assert ownership.rounds() == (FRESH_LIVE_ROUND_ID,)
    assert len(ownership) == 1


def test_a_fresh_live_round_is_not_in_the_train_schedule(live, train) -> None:
    assert len(train) == 50
    assert FRESH_LIVE_ROUND_ID not in set(train.rounds())
    assert live["ownership"].rounds() == (FRESH_LIVE_ROUND_ID,)


def test_a_live_round_produces_a_v2_decision_for_the_accepted_safe_config(live, authority) -> None:
    """§J: the decision itself is unchanged. LIVE only changes how the round is described."""
    request = _live_request(live, authority, ControlMode.SAFE_BASELINE)
    result = select_safe_baseline(request=request, authority=authority, ownership=live["ownership"])

    assert result.selected_config.sha256 == ACCEPTED_SAFE_CONFIG
    assert result.mode is ControlMode.SAFE_BASELINE
    assert result.fallback_reason is FallbackReason.NONE
    assert result.decision.config_hash == ACCEPTED_SAFE_CONFIG

    manifest = safe_decision_manifest_for(
        request=request, authority=authority, ownership=live["ownership"]
    )
    assert manifest["schema_version"] == SAFE_DECISION_MANIFEST_V2_SCHEMA
    assert manifest["selected_config_hash"] == ACCEPTED_SAFE_CONFIG
    # the decision the controller returned IS the manifest that was hashed
    assert result.decision.decision_manifest_hash == live_safe_decision_manifest_identity(manifest)
    assert result.decision.decision_id == result.decision.decision_manifest_hash


@pytest.mark.parametrize(
    "requested,expected_fallback",
    [
        (ControlMode.SAFE_BASELINE, FallbackReason.NONE),
        (ControlMode.BOUNDED, FallbackReason.SAFE_BASELINE_FORCED),
        (ControlMode.FULL_CONTEXTUAL, FallbackReason.SAFE_BASELINE_FORCED),
        (ControlMode.REFINEMENT, FallbackReason.SAFE_BASELINE_FORCED),
    ],
)
def test_every_requested_mode_collapses_exactly_as_the_safe_policy_says(
    live, authority, requested: ControlMode, expected_fallback: FallbackReason
) -> None:
    """§J: the accepted collapse, unchanged, on the live path."""
    request = _live_request(live, authority, requested)
    result = select_safe_baseline(request=request, authority=authority, ownership=live["ownership"])
    assert result.mode is ControlMode.SAFE_BASELINE
    assert result.fallback_reason is expected_fallback
    assert result.selected_config.sha256 == ACCEPTED_SAFE_CONFIG

    manifest = safe_decision_manifest_for(
        request=request, authority=authority, ownership=live["ownership"]
    )
    assert manifest["requested_mode"] == requested.value
    assert manifest["actual_mode"] == ControlMode.SAFE_BASELINE.value
    assert manifest["fallback_reason"] == expected_fallback.value


def test_the_controller_object_dispatches_the_same_way(live, authority) -> None:
    controller = SafeBaselineController(authority, live["ownership"])
    request = _live_request(live, authority, ControlMode.SAFE_BASELINE)
    assert controller.manifest(request)["schema_version"] == SAFE_DECISION_MANIFEST_V2_SCHEMA
    assert controller.decide(request).selected_config.sha256 == ACCEPTED_SAFE_CONFIG


# --------------------------------------------------------------------------- #
# §D / §E / §G / §H: the v2 contract itself
# --------------------------------------------------------------------------- #
def test_the_v2_schema_and_domain_are_distinct_from_v1() -> None:
    assert SAFE_DECISION_MANIFEST_V2_SCHEMA == "l2h-safe-decision-manifest-v2"
    assert SAFE_DECISION_MANIFEST_V2_DOMAIN == "minos:l2h-safe-decision-manifest:v2\n"
    assert SAFE_DECISION_MANIFEST_V2_SCHEMA != SAFE_DECISION_MANIFEST_SCHEMA
    assert SAFE_DECISION_MANIFEST_V2_DOMAIN != SAFE_DECISION_MANIFEST_DOMAIN


def test_v2_carries_no_train_only_field(live, authority) -> None:
    """§E: not present under the same name with a new meaning -- not present at all."""
    manifest = safe_decision_manifest_for(
        request=_live_request(live, authority, ControlMode.SAFE_BASELINE),
        authority=authority,
        ownership=live["ownership"],
    )
    for field in TRAIN_ONLY_V1_FIELDS:
        assert field not in manifest, f"{field} is TRAIN-specific and must not appear in v2"


def test_v2_publishes_the_live_authorities_under_their_own_names(live, authority) -> None:
    ownership = live["ownership"]
    manifest = safe_decision_manifest_for(
        request=_live_request(live, authority, ControlMode.SAFE_BASELINE),
        authority=authority,
        ownership=ownership,
    )
    for field in LIVE_AUTHORITY_FIELDS:
        assert field in manifest

    assert manifest["ownership_scope"] == "live"
    assert manifest["live_intake_identity"] == live["intake"].identity
    assert manifest["live_intake_schema"] == "l2h-live-round-intake-v2"
    assert manifest["platform_receipt_identity"] == live["receipt"].identity
    assert manifest["live_profile_ownership_identity"] == ownership.corpus_identity
    assert manifest["live_profile_ownership_anchors"] == dict(sorted(ownership.anchors.items()))


def test_the_live_input_set_id_is_content_addressed_not_a_registered_dataset(
    live, authority
) -> None:
    """§G: it is derived from the authenticated input set, and it is not a research dataset."""
    ownership = live["ownership"]
    owned = ownership.owned(ownership.rounds()[0])
    manifest = safe_decision_manifest_for(
        request=_live_request(live, authority, ControlMode.SAFE_BASELINE),
        authority=authority,
        ownership=ownership,
    )
    value = manifest["live_input_set_id"]
    assert value == owned.dataset_id
    assert value.startswith(f"live-{owned.chromosome}-")
    # content-addressed from the intake's own identity tuple, not from any allocation
    assert value.endswith(owned.identity_tuple_hash[:16])
    assert "dataset_id" not in manifest


def test_the_intake_identity_replaces_the_registry_compatibility_field(live, authority) -> None:
    """§H: the internal L2-D-shaped field is not leaked under its misleading name."""
    ownership = live["ownership"]
    owned = ownership.owned(ownership.rounds()[0])
    manifest = safe_decision_manifest_for(
        request=_live_request(live, authority, ControlMode.SAFE_BASELINE),
        authority=authority,
        ownership=ownership,
    )
    # for a live round the compatibility field holds the intake identity, and the two must agree
    assert owned.registry_snapshot_hash == live["intake"].identity
    assert manifest["live_intake_identity"] == owned.registry_snapshot_hash
    assert "registry_snapshot_hash" not in manifest


def test_v2_keeps_every_common_scientific_field_with_the_same_meaning(live, authority) -> None:
    """§I: the shared science is genuinely shared, from one implementation."""
    ownership = live["ownership"]
    owned = ownership.owned(ownership.rounds()[0])
    manifest = safe_decision_manifest_for(
        request=_live_request(live, authority, ControlMode.SAFE_BASELINE),
        authority=authority,
        ownership=ownership,
    )
    assert manifest["round_id"] == owned.round_id
    assert manifest["chromosome"] == owned.chromosome
    assert manifest["profile_id"] == owned.profile_id
    assert manifest["profile_sha256"] == owned.profile_sha256
    assert manifest["profile_manifest_sha256"] == owned.profile_manifest_sha256
    assert manifest["profile_fingerprint_hash"] == owned.fingerprint_hash
    assert manifest["profile_identity_tuple_hash"] == owned.identity_tuple_hash
    assert manifest["attestation_hash"] == owned.attestation_hash
    assert manifest["region_hash"] == owned.region_hash

    assert manifest["parameter_space_hash"] == authority.parameter_space_hash
    assert manifest["baseline_config_hash"] == authority.baseline_config_hash
    assert manifest["baseline_payload_sha256"] == authority.baseline_payload_sha256
    assert manifest["controller_policy_hash"] == authority.policy_hash
    assert manifest["execution_source_commit"] == authority.source_commit
    assert manifest["execution_source_tree"] == authority.source_tree

    assert manifest["model_bundle_loaded"] is False
    assert manifest["contextual_research_closed"] is True
    assert manifest["guards"]["candidate_generation"] is False
    assert manifest["guards"]["parameter_mutation"] is False
    assert manifest["guards"]["round_profile_ownership_proven"] is True


def test_the_two_manifests_share_exactly_the_common_science(live, authority, train) -> None:
    """The only differences between a v1 and a v2 document are the two authority blocks."""
    v1 = safe_decision_manifest_content(
        request=_train_request(train, authority), authority=authority, ownership=train
    )
    v2 = safe_decision_manifest_for(
        request=_live_request(live, authority, ControlMode.SAFE_BASELINE),
        authority=authority,
        ownership=live["ownership"],
    )
    only_v1 = set(v1) - set(v2)
    only_v2 = set(v2) - set(v1)
    assert only_v1 == set(TRAIN_ONLY_V1_FIELDS)
    assert only_v2 == set(LIVE_AUTHORITY_FIELDS)
    assert set(v1) & set(v2) == set(v1) - only_v1


# --------------------------------------------------------------------------- #
# §L: strict schema separation in both directions
# --------------------------------------------------------------------------- #
def test_the_v1_builder_still_refuses_live_ownership(live, authority) -> None:
    request = _live_request(live, authority, ControlMode.SAFE_BASELINE)
    with pytest.raises(SafeControllerAuthorityError, match="decision-manifest v2"):
        safe_decision_manifest_content(
            request=request, authority=authority, ownership=live["ownership"]
        )


def test_the_v2_builder_refuses_train_ownership(train, authority) -> None:
    request = _train_request(train, authority)
    with pytest.raises(SafeControllerAuthorityError, match="authenticated live round"):
        live_safe_decision_manifest_content(request=request, authority=authority, ownership=train)


def test_the_train_path_still_produces_v1(train, authority) -> None:
    request = _train_request(train, authority)
    manifest = safe_decision_manifest_for(request=request, authority=authority, ownership=train)
    assert manifest["schema_version"] == SAFE_DECISION_MANIFEST_SCHEMA
    assert manifest == safe_decision_manifest_content(
        request=request, authority=authority, ownership=train
    )
    result = select_safe_baseline(request=request, authority=authority, ownership=train)
    assert result.decision.decision_manifest_hash == safe_decision_manifest_identity(manifest)
    assert result.selected_config.sha256 == ACCEPTED_SAFE_CONFIG


# --------------------------------------------------------------------------- #
# §K: the schema is chosen by the sealed capability, never by the caller
# --------------------------------------------------------------------------- #
def test_the_dispatcher_requires_a_sealed_authority_before_reading_scope(live, authority) -> None:
    ownership = live["ownership"]

    class Lookalike:
        scope = "train"
        partition = "train"
        corpus_identity = "0" * 64
        anchors: dict[str, str] = {}

        def owned(self, round_id: str) -> Any:
            return ownership.owned(ownership.rounds()[0])

        def require_owned_request(self, request: Any) -> Any:
            return ownership.owned(ownership.rounds()[0])

    # a REAL baseline authority, so the refusal can only be about the ownership capability
    with pytest.raises(SafeControllerAuthorityError, match="SEALED verified round/profile corpus"):
        safe_decision_manifest_for(request=None, authority=authority, ownership=Lookalike())


def test_the_request_cannot_choose_the_schema(live, authority) -> None:
    """No request field selects the contract; only the verified ownership domain does."""
    request = _live_request(live, authority, ControlMode.SAFE_BASELINE)
    for attribute in ("partition", "scope", "schema_version", "ownership_scope"):
        assert not hasattr(request, attribute)
    manifest = safe_decision_manifest_for(
        request=request, authority=authority, ownership=live["ownership"]
    )
    assert manifest["schema_version"] == SAFE_DECISION_MANIFEST_V2_SCHEMA


def test_an_unknown_ownership_scope_fails_closed(live, authority, monkeypatch) -> None:
    ownership = live["ownership"]
    monkeypatch.setattr(type(ownership), "scope", property(lambda self: "somewhere-else"))
    with pytest.raises(SafeControllerAuthorityError, match="no decision-manifest contract"):
        safe_decision_manifest_for(request=None, authority=authority, ownership=ownership)


def test_the_identity_helper_dispatches_on_the_declared_schema(live, authority, train) -> None:
    v2 = safe_decision_manifest_for(
        request=_live_request(live, authority, ControlMode.SAFE_BASELINE),
        authority=authority,
        ownership=live["ownership"],
    )
    v1 = safe_decision_manifest_for(
        request=_train_request(train, authority), authority=authority, ownership=train
    )
    assert safe_decision_identity_for(v2) == live_safe_decision_manifest_identity(v2)
    assert safe_decision_identity_for(v1) == safe_decision_manifest_identity(v1)
    with pytest.raises(SafeControllerAuthorityError, match="not a safe-controller decision"):
        safe_decision_identity_for({"schema_version": "something-else"})


def test_the_two_domains_cannot_collide(live, authority) -> None:
    """Even byte-equal documents get different identities, because the domains differ."""
    manifest = safe_decision_manifest_for(
        request=_live_request(live, authority, ControlMode.SAFE_BASELINE),
        authority=authority,
        ownership=live["ownership"],
    )
    assert live_safe_decision_manifest_identity(manifest) != safe_decision_manifest_identity(
        manifest
    )


# --------------------------------------------------------------------------- #
# §M: determinism and the semantic mutation matrix
# --------------------------------------------------------------------------- #
def test_the_v2_identity_is_deterministic(live, authority) -> None:
    request = _live_request(live, authority, ControlMode.SAFE_BASELINE)
    first = safe_decision_manifest_for(
        request=request, authority=authority, ownership=live["ownership"]
    )
    second = safe_decision_manifest_for(
        request=request, authority=authority, ownership=live["ownership"]
    )
    assert first == second
    assert live_safe_decision_manifest_identity(first) == live_safe_decision_manifest_identity(
        second
    )


def test_two_independent_runs_of_the_same_round_agree(
    monkeypatch: pytest.MonkeyPatch, dataset, authority
) -> None:
    """The whole production chain re-run must land on the same decision identity."""
    again = build_production_live_round(monkeypatch, dataset)
    manifest = safe_decision_manifest_for(
        request=_live_request(again, authority, ControlMode.SAFE_BASELINE),
        authority=authority,
        ownership=again["ownership"],
    )
    once_more = build_production_live_round(monkeypatch, dataset)
    repeated = safe_decision_manifest_for(
        request=_live_request(once_more, authority, ControlMode.SAFE_BASELINE),
        authority=authority,
        ownership=once_more["ownership"],
    )
    assert manifest == repeated


@pytest.mark.parametrize(
    "field",
    [
        "round_id",
        "live_input_set_id",
        "live_intake_identity",
        "platform_receipt_identity",
        "profile_id",
        "profile_sha256",
        "profile_manifest_sha256",
        "profile_fingerprint_hash",
        "profile_identity_tuple_hash",
        "attestation_hash",
        "region_hash",
        "parameter_space_hash",
        "requested_mode",
        "live_profile_ownership_identity",
        "ownership_scope",
    ],
)
def test_changing_any_semantic_field_moves_the_v2_identity(live, authority, field: str) -> None:
    """Every field carries weight: if one could change silently it would not be an identity."""
    manifest = safe_decision_manifest_for(
        request=_live_request(live, authority, ControlMode.SAFE_BASELINE),
        authority=authority,
        ownership=live["ownership"],
    )
    baseline = live_safe_decision_manifest_identity(manifest)
    mutated = {**manifest, field: f"mutated-{manifest[field]}"}
    assert live_safe_decision_manifest_identity(mutated) != baseline


def test_a_different_requested_mode_moves_the_identity(live, authority) -> None:
    identities = {
        mode: live_safe_decision_manifest_identity(
            safe_decision_manifest_for(
                request=_live_request(live, authority, mode),
                authority=authority,
                ownership=live["ownership"],
            )
        )
        for mode in (
            ControlMode.SAFE_BASELINE,
            ControlMode.BOUNDED,
            ControlMode.FULL_CONTEXTUAL,
            ControlMode.REFINEMENT,
        )
    }
    # SAFE_BASELINE differs from the forced modes; the forced modes differ from each other by
    # `requested_mode` alone, which is itself a semantic difference
    assert len(set(identities.values())) == len(identities)


def test_a_different_live_round_produces_a_different_identity(
    monkeypatch: pytest.MonkeyPatch, dataset, authority
) -> None:
    first = build_production_live_round(monkeypatch, dataset)
    second = build_production_live_round(
        monkeypatch, dataset, live_round_id="2026-10-01T09:30:00+00:00"
    )
    identities = {
        live_safe_decision_manifest_identity(
            safe_decision_manifest_for(
                request=_live_request(chain, authority, ControlMode.SAFE_BASELINE),
                authority=authority,
                ownership=chain["ownership"],
            )
        )
        for chain in (first, second)
    }
    assert len(identities) == 2


# --------------------------------------------------------------------------- #
# §M: no operational fact may enter the identity
# --------------------------------------------------------------------------- #
def test_no_operational_value_enters_the_v2_manifest(live, authority) -> None:
    """Timestamps, URLs, hotkeys, paths and sentinels describe HOW, not WHAT."""
    manifest = safe_decision_manifest_for(
        request=_live_request(live, authority, ControlMode.SAFE_BASELINE),
        authority=authority,
        ownership=live["ownership"],
    )
    serialized = json.dumps(manifest, sort_keys=True)

    receipt = live["receipt"]
    for url in receipt._parsed.urls.values():
        assert url not in serialized
    for fragment in (
        "https://",
        "http://",
        "?sig=",
        "5FakeHotkeyAddressForTests",
        "platform.example",
        "/tmp",
        str(Path(live["dataset"]["bam"])),
        ".minos-provenance",
        "input.bam",
    ):
        assert fragment not in serialized, f"{fragment!r} is operational and must not be published"

    for forbidden in (
        "timestamp",
        "created_at",
        "decided_at",
        "duration",
        "elapsed",
        "pid",
        "hostname",
        "path",
        "url",
        "hotkey",
        "base_url",
        "cache",
        "sidecar",
        "operational_binding",
    ):
        assert forbidden not in serialized.lower().replace("_sha256", "")

    # the per-instance operational sentinel is not serializable at all, and is not reachable here
    assert "operational" not in {key.split("_")[0] for key in manifest}


def test_the_manifest_is_canonical_json_shaped(live, authority) -> None:
    from minos_engine.common.canonical_json import canonical_json_bytes

    manifest = safe_decision_manifest_for(
        request=_live_request(live, authority, ControlMode.SAFE_BASELINE),
        authority=authority,
        ownership=live["ownership"],
    )
    assert type(manifest) is dict
    assert type(manifest["live_profile_ownership_anchors"]) is dict
    canonical_json_bytes(manifest)


# --------------------------------------------------------------------------- #
# §N: the fixture chain still stops outside the controller
# --------------------------------------------------------------------------- #
def test_the_fixture_chain_still_cannot_reach_the_controller(
    monkeypatch: pytest.MonkeyPatch, dataset, authority
) -> None:
    """v2 must not have opened a door for the offline domain."""
    from minos_engine.layer2.live_round_authority import FixtureRoundProfileAuthority
    from tests.layer2_live_replay import accept_synthetic_reference

    accept_synthetic_reference(monkeypatch, dataset)
    replay = build_live_replay(dataset)
    fixture = live_ownership(replay)
    assert type(fixture) is FixtureRoundProfileAuthority
    assert not is_verified_round_profile_authority(fixture)

    request = _request_for(
        fixture.owned(FRESH_LIVE_ROUND_ID), authority=authority, mode=ControlMode.SAFE_BASELINE
    )
    for call in (
        lambda: safe_decision_manifest_for(request=request, authority=authority, ownership=fixture),
        lambda: live_safe_decision_manifest_content(
            request=request, authority=authority, ownership=fixture
        ),
        lambda: select_safe_baseline(request=request, authority=authority, ownership=fixture),
        lambda: SafeBaselineController(authority, fixture),
    ):
        with pytest.raises(SafeControllerAuthorityError, match="fixture authority"):
            call()


def test_v2_requires_a_production_scoped_intake_anchor(live, authority, monkeypatch) -> None:
    """Defense in depth: a live manifest describes a PRODUCTION chain, and says so."""
    ownership = live["ownership"]
    request = _live_request(live, authority, ControlMode.SAFE_BASELINE)
    fixture_anchors = {**ownership.anchors, "live_intake_scope": "fixture"}
    monkeypatch.setattr(type(ownership), "anchors", property(lambda self: dict(fixture_anchors)))
    with pytest.raises(SafeControllerAuthorityError, match="production-scoped live intake"):
        live_safe_decision_manifest_content(
            request=request, authority=authority, ownership=ownership
        )


# --------------------------------------------------------------------------- #
# §P: TRAIN is untouched
# --------------------------------------------------------------------------- #
def test_the_train_corpus_identity_has_not_moved(train) -> None:
    assert train.corpus_identity == ACCEPTED_TRAIN_CORPUS_IDENTITY
    assert len(train) == 50
    assert train.scope == "train"


def test_every_train_round_still_produces_the_accepted_v1_decision(train, authority) -> None:
    """All 50 rounds x 4 modes, through the dispatcher, still v1 and still the SAFE config."""
    seen: set[str] = set()
    for round_id in train.rounds():
        owned = train.owned(round_id)
        for mode in ControlMode:
            request = _request_for(owned, authority=authority, mode=mode)
            result = select_safe_baseline(request=request, authority=authority, ownership=train)
            manifest = safe_decision_manifest_for(
                request=request, authority=authority, ownership=train
            )
            assert manifest["schema_version"] == SAFE_DECISION_MANIFEST_SCHEMA
            assert result.selected_config.sha256 == ACCEPTED_SAFE_CONFIG
            assert result.mode is ControlMode.SAFE_BASELINE
            assert result.decision.decision_manifest_hash == safe_decision_manifest_identity(
                manifest
            )
            seen.add(result.decision.decision_manifest_hash)
    assert len(seen) == 50 * len(ControlMode)


def test_all_two_hundred_train_v1_manifests_are_byte_identical_to_entry_head(
    train, authority
) -> None:
    """§C: v1's canonical bytes did not move when the shared science was factored out.

    A rolling digest over all 50 rounds x 4 modes, with the two checkout-provenance fields
    excluded because they name the current commit by design and therefore change on every commit.
    Recorded at entry HEAD 72a5e5a0ff379fdb98400de33cb43f5b7ea896b6.
    """
    import hashlib

    from minos_engine.common.canonical_json import canonical_json_bytes

    rolling = hashlib.sha256()
    count = 0
    for round_id in train.rounds():
        owned = train.owned(round_id)
        for mode in ControlMode:
            manifest = safe_decision_manifest_content(
                request=_request_for(owned, authority=authority, mode=mode),
                authority=authority,
                ownership=train,
            )
            stable = {
                key: value
                for key, value in manifest.items()
                if key not in ("execution_source_commit", "execution_source_tree")
            }
            rolling.update(canonical_json_bytes(stable))
            count += 1
    assert count == 200
    assert rolling.hexdigest() == (
        "993632d0da7df0abbe7e4391a2972f3ccb9d137e064ae97695f83fd0798e09cd"
    )


# --------------------------------------------------------------------------- #
# §Q: persistence and publication are still v1-only, and fail closed for LIVE
# --------------------------------------------------------------------------- #
def test_the_publication_path_fails_closed_for_a_live_decision(live, authority, tmp_path) -> None:
    """r0003 is required before a live decision can be recorded anywhere.

    ``decide_and_publish`` still builds a v1 manifest, so a live authority is refused rather than
    published under the wrong contract. That refusal is the correct pre-r0003 behaviour: the
    runtime overlay's ``ck_decisions_manifest_schema`` CHECK pins
    ``l2h-safe-decision-manifest-v1``, so nothing v2 could be stored today either.
    """
    from minos_engine.layer2.decision_publication import decide_and_publish

    request = _live_request(live, authority, ControlMode.SAFE_BASELINE)
    with pytest.raises(SafeControllerAuthorityError, match="decision-manifest v2"):
        decide_and_publish(
            request=request,
            authority=authority,
            ownership=live["ownership"],
            output_root=tmp_path / "decisions",
        )


def test_a_v2_manifest_cannot_be_published_under_a_v1_identity(live, authority, tmp_path) -> None:
    """The publisher derives the identity itself, in the v1 domain, and refuses a mismatch."""
    from minos_engine.layer2.decision_publication import (
        DecisionPublicationError,
        publish_safe_decision,
    )

    manifest = safe_decision_manifest_for(
        request=_live_request(live, authority, ControlMode.SAFE_BASELINE),
        authority=authority,
        ownership=live["ownership"],
    )
    with pytest.raises(DecisionPublicationError, match="may not be published as"):
        publish_safe_decision(
            manifest=manifest,
            identity=live_safe_decision_manifest_identity(manifest),
            output_root=tmp_path / "decisions",
        )


def test_the_train_publication_path_is_unaffected(train, authority, tmp_path) -> None:
    from minos_engine.layer2.decision_publication import decide_and_publish

    request = _train_request(train, authority)
    result, published = decide_and_publish(
        request=request,
        authority=authority,
        ownership=train,
        output_root=tmp_path / "decisions",
    )
    assert result.selected_config.sha256 == ACCEPTED_SAFE_CONFIG
    assert published.identity == result.decision.decision_manifest_hash


def test_the_runtime_overlay_still_pins_v1_so_r0003_is_required() -> None:
    """The DB CHECK that makes r0003 the next step, asserted rather than asserted in prose."""
    source = (REPO_ROOT / "migrations_runtime/versions/r0001_l2h_runtime_decisions.py").read_text()
    assert "ck_decisions_manifest_schema" in source
    assert SAFE_DECISION_MANIFEST_SCHEMA in source
    assert SAFE_DECISION_MANIFEST_V2_SCHEMA not in source
    # v2 keeps the four manifest keys the overlay's other CHECKs read, so an r0003 that widens
    # the schema constraint will not have to rewrite them
    for key in ("round_id", "actual_mode", "requested_mode", "fallback_reason"):
        assert f"'{key}'" in source


# --------------------------------------------------------------------------- #
# §D: `owned=` is a redundancy check, never an authority
#
# `OwnedRoundProfile` is deliberately a public, constructible value object. It is immutable once
# built -- and immutability is not provenance. The builders used to read
# `owned if owned is not None else ownership.require_owned_request(request)`, so supplying `owned`
# skipped the proof outright: a fabricated member could be handed in beside a GENUINE sealed live
# authority, and the resulting manifest combined real ownership anchors and a real intake identity
# with attacker-chosen `profile_id`, `profile_sha256` and `round_id`.
# --------------------------------------------------------------------------- #
def _fabricated(owned: Any, **overrides: Any) -> Any:
    from minos_engine.layer2.round_profile_authority import OwnedRoundProfile

    return OwnedRoundProfile(**{**owned.content(), **overrides})


def test_a_fabricated_owned_profile_cannot_ride_a_genuine_live_authority(live, authority) -> None:
    """The sharpest case: real ownership anchors, attacker-chosen profile identity."""
    ownership = live["ownership"]
    real = ownership.owned(ownership.rounds()[0])
    request = _live_request(live, authority, ControlMode.SAFE_BASELINE)
    forged = _fabricated(real, profile_id="f" * 32, profile_sha256="a" * 64)

    for call in (
        lambda: safe_decision_manifest_for(
            request=request, authority=authority, ownership=ownership, owned=forged
        ),
        lambda: live_safe_decision_manifest_content(
            request=request, authority=authority, ownership=ownership, owned=forged
        ),
    ):
        with pytest.raises(
            SafeControllerAuthorityError, match="not the member this authority owns"
        ):
            call()


def test_a_fabricated_owned_profile_that_keeps_the_live_shape_is_still_refused(
    live, authority
) -> None:
    """§D case 4: right ``registry_snapshot_hash``, right partition, changed identity."""
    ownership = live["ownership"]
    real = ownership.owned(ownership.rounds()[0])
    copycat = _fabricated(real, profile_id="d" * 32)
    assert copycat.registry_snapshot_hash == real.registry_snapshot_hash
    assert copycat.partition == real.partition
    with pytest.raises(SafeControllerAuthorityError, match="not the member this authority owns"):
        live_safe_decision_manifest_content(
            request=_live_request(live, authority, ControlMode.SAFE_BASELINE),
            authority=authority,
            ownership=ownership,
            owned=copycat,
        )


def test_an_owned_member_from_another_live_authority_is_refused(
    monkeypatch: pytest.MonkeyPatch, dataset, live, authority
) -> None:
    """§D case 2: a REAL owned profile, from a different authority for a different round."""
    other = build_production_live_round(
        monkeypatch, dataset, live_round_id="2026-10-02T08:15:00+00:00"
    )
    foreign = other["ownership"].owned(other["ownership"].rounds()[0])
    with pytest.raises(SafeControllerAuthorityError, match="not the member this authority owns"):
        safe_decision_manifest_for(
            request=_live_request(live, authority, ControlMode.SAFE_BASELINE),
            authority=authority,
            ownership=live["ownership"],
            owned=foreign,
        )


def test_a_cross_round_owned_member_is_refused_on_the_train_path(train, authority) -> None:
    """§D cases 3 and 5: the same attacks against v1."""
    request = _train_request(train, authority)
    other_round = train.owned(train.rounds()[1])
    assert other_round.round_id != request.round.round_id
    with pytest.raises(SafeControllerAuthorityError, match="not the member this authority owns"):
        safe_decision_manifest_content(
            request=request, authority=authority, ownership=train, owned=other_round
        )
    with pytest.raises(SafeControllerAuthorityError, match="not the member this authority owns"):
        safe_decision_manifest_content(
            request=request,
            authority=authority,
            ownership=train,
            owned=_fabricated(train.owned(train.rounds()[0]), profile_id="e" * 32),
        )


def test_an_equal_looking_copy_of_the_owned_member_is_not_the_owned_member(live, authority) -> None:
    """Field equality is not provenance: only the corpus's own frozen object is accepted."""
    ownership = live["ownership"]
    real = ownership.owned(ownership.rounds()[0])
    twin = _fabricated(real)
    assert twin.content() == real.content()
    assert twin is not real
    with pytest.raises(SafeControllerAuthorityError, match="not the member this authority owns"):
        live_safe_decision_manifest_content(
            request=_live_request(live, authority, ControlMode.SAFE_BASELINE),
            authority=authority,
            ownership=ownership,
            owned=twin,
        )


def test_the_authoritys_own_member_is_accepted_and_changes_nothing(live, authority) -> None:
    """§D case 6, and the reason the parameter can stay: the real object still passes."""
    ownership = live["ownership"]
    real = ownership.owned(ownership.rounds()[0])
    request = _live_request(live, authority, ControlMode.SAFE_BASELINE)
    with_owned = safe_decision_manifest_for(
        request=request, authority=authority, ownership=ownership, owned=real
    )
    without = safe_decision_manifest_for(request=request, authority=authority, ownership=ownership)
    assert with_owned == without


def test_the_train_path_still_accepts_the_locked_persistence_call_shape(train, authority) -> None:
    """``storage/decision_persistence.py`` is byte-locked and passes ``owned=``; it must work."""
    request = _train_request(train, authority)
    owned = train.require_owned_request(request)
    manifest = safe_decision_manifest_content(
        request=request, authority=authority, ownership=train, owned=owned
    )
    assert manifest == safe_decision_manifest_content(
        request=request, authority=authority, ownership=train
    )


def test_no_builder_still_carries_the_owned_bypass() -> None:
    """The exact expression that made `owned=` an authority, asserted absent from the code."""
    import ast
    import textwrap

    from minos_engine.layer2 import safe_controller

    source = (REPO_ROOT / "src/minos_engine/layer2/safe_controller.py").read_text()
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef) and (
            node.body
            and isinstance(node.body[0], ast.Expr)
            and isinstance(node.body[0].value, ast.Constant)
            and isinstance(node.body[0].value.value, str)
        ):
            node.body.pop(0)
    code = ast.unparse(tree)
    assert "owned if owned is not None" not in code
    # every public builder proves the request through the one helper
    assert code.count("_proven_member(") == 3
    assert hasattr(safe_controller, "_proven_member")
    assert textwrap.dedent  # keep the import meaningful


# --------------------------------------------------------------------------- #
# §F: both sealed authorities, checked before either is read
# --------------------------------------------------------------------------- #
class _LookalikeAuthority:
    """Carries every attribute `_common_safe_decision_science` reads. Sealed by nothing."""

    baseline_uri = "file:///evil"
    parameter_space_hash = "0" * 64
    baseline_config_hash = "1" * 64
    baseline_payload_sha256 = "2" * 64
    policy_hash = "3" * 64
    source_commit = "attacker-commit"
    source_tree = "attacker-tree"
    entry_gate_checks: dict[str, bool] = {"entry_gate_ok": True}

    def __init__(self, policy: dict[str, Any]) -> None:
        self.policy = policy

    @property
    def allowed_modes(self) -> tuple[str, ...]:
        return ("SAFE_BASELINE",)


def _forged_authorities(authority: Any) -> list[tuple[str, Any]]:
    from minos_engine.layer2.safe_controller import VerifiedSafeBaselineAuthority

    class SubclassAuthority(VerifiedSafeBaselineAuthority):
        pass

    unsealed = object.__new__(SubclassAuthority)
    object.__setattr__(unsealed, "_policy", authority.policy)
    object.__setattr__(unsealed, "_entry_gate_checks", authority.entry_gate_checks)
    for field in (
        "policy_hash",
        "baseline_config_hash",
        "baseline_payload_sha256",
        "baseline_uri",
        "parameter_space_hash",
        "source_commit",
        "source_tree",
    ):
        object.__setattr__(unsealed, field, getattr(authority, field))

    return [
        ("a dict shaped like the authority", {"policy": {}, "baseline_config_hash": "1" * 64}),
        ("a lookalike object", _LookalikeAuthority(authority.policy)),
        ("an unsealed subclass", unsealed),
    ]


@pytest.mark.parametrize("index", [0, 1, 2])
def test_no_manifest_entry_point_accepts_a_forged_baseline_authority(
    live, train, authority, index: int
) -> None:
    """The manifest API is exported; it may not rely on ``select_safe_baseline`` checking first."""
    label, forged = _forged_authorities(authority)[index]
    live_request = _live_request(live, authority, ControlMode.SAFE_BASELINE)
    train_request = _train_request(train, authority)

    for call in (
        lambda: safe_decision_manifest_for(
            request=live_request, authority=forged, ownership=live["ownership"]
        ),
        lambda: safe_decision_manifest_for(
            request=train_request, authority=forged, ownership=train
        ),
        lambda: live_safe_decision_manifest_content(
            request=live_request, authority=forged, ownership=live["ownership"]
        ),
        lambda: safe_decision_manifest_content(
            request=train_request, authority=forged, ownership=train
        ),
    ):
        with pytest.raises(SafeControllerAuthorityError, match="SEALED verified safe-baseline"):
            call()


def test_a_forged_authority_never_reaches_a_scientific_field(live, authority) -> None:
    """It is refused BEFORE anything is read, so no attacker value can be published."""
    reads: list[str] = []

    class Tattletale:
        """Records every scientific attribute read. None of them should ever be touched."""

        allowed_modes = ("SAFE_BASELINE",)

        def __getattr__(self, name: str) -> Any:
            reads.append(name)
            return "attacker-value"

    for call in (
        lambda: live_safe_decision_manifest_content(
            request=_live_request(live, authority, ControlMode.SAFE_BASELINE),
            authority=Tattletale(),
            ownership=live["ownership"],
        ),
        lambda: safe_decision_manifest_for(
            request=_live_request(live, authority, ControlMode.SAFE_BASELINE),
            authority=Tattletale(),
            ownership=live["ownership"],
        ),
    ):
        with pytest.raises(SafeControllerAuthorityError, match="SEALED verified safe-baseline"):
            call()
    assert reads == [], f"the authority was read before it was verified: {reads}"


def test_the_identity_hashers_stay_pure(live, authority) -> None:
    """§I: hashing an already-built document performs no authority or repository lookup."""
    import ast

    source = (REPO_ROOT / "src/minos_engine/layer2/safe_controller.py").read_text()
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name in (
            "safe_decision_manifest_identity",
            "live_safe_decision_manifest_identity",
            "safe_decision_identity_for",
        ):
            body = ast.unparse(node)
            for forbidden in (
                "load_verified",
                "_require_manifest_authorities",
                "repo_root",
                "Path(",
            ):
                assert forbidden not in body, f"{node.name} performs a lookup: {forbidden}"

    manifest = safe_decision_manifest_for(
        request=_live_request(live, authority, ControlMode.SAFE_BASELINE),
        authority=authority,
        ownership=live["ownership"],
    )
    assert safe_decision_identity_for(manifest) == live_safe_decision_manifest_identity(manifest)
