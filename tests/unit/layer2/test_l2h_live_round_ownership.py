"""LIVE round/profile ownership: a production round the TRAIN schedule has never heard of."""

from __future__ import annotations

import builtins
import io
import json
from pathlib import Path
from typing import Any

import pytest

from minos_engine.common.canonical_json import canonical_json_bytes
from minos_engine.common.hashing import canonical_hash
from minos_engine.layer2.contracts import ControlMode
from minos_engine.layer2.live_round_authority import (
    LIVE_OWNERSHIP_DOMAIN,
    LIVE_OWNERSHIP_SCHEMA,
    LiveRoundOwnershipError,
    load_verified_live_round_ownership,
)
from minos_engine.layer2.live_round_intake import (
    LIVE_INTAKE_DOMAIN,
    LIVE_INTAKE_FIELDS,
    LIVE_INTAKE_SCHEMA,
    LiveRoundIntakeError,
    VerifiedLiveRoundIntake,
    build_live_round_intake,
    live_round_intake_identity,
    verify_live_round_intake,
)
from minos_engine.layer2.round_profile_authority import (
    LIVE_PARTITION,
    TRAIN_SCHEDULE_PATH,
    VerifiedRoundProfileAuthority,
    load_verified_round_profile_corpus,
)
from tests.conftest import REPO_ROOT
from tests.layer2_live_replay import (
    FRESH_LIVE_ROUND_ID,
    build_live_replay,
    corpus_available,
    live_ownership,
)

SOURCE_ROUND = "0279a3b8042f848b"
#: The frozen TRAIN corpus identity the accepted controller qualification binds. It must not move.
ACCEPTED_TRAIN_CORPUS_IDENTITY = "9cc53b5d28c8a8da34c25095362c09d8cb1fb57533ff0a0b3e1fdf7000970b03"

pytestmark = pytest.mark.skipif(
    not corpus_available(SOURCE_ROUND), reason="the local L2-D profile corpus is not present"
)


@pytest.fixture(scope="module")
def replay() -> dict[str, Any]:
    built = build_live_replay(SOURCE_ROUND)
    built["ownership"] = live_ownership(built)
    return built


# --------------------------------------------------------------------------- #
# the intake identity
# --------------------------------------------------------------------------- #
def test_the_round_id_is_the_platforms_not_ours(replay):
    """`/v2/round-status` issues an ISO-8601 timestamp; the engine takes it verbatim."""
    from datetime import datetime

    round_id = str(replay["intake"].round_id)
    assert round_id == FRESH_LIVE_ROUND_ID
    assert datetime.fromisoformat(round_id)
    assert len(round_id) <= 40


def test_the_intake_identity_is_domain_separated_and_canonical(replay):
    content = replay["intake_content"]
    identity = replay["intake"].identity
    assert identity == live_round_intake_identity(content)
    assert identity != canonical_hash(content), "a plain content hash is not the identity"
    assert LIVE_INTAKE_DOMAIN.startswith("minos:")
    assert content["schema_version"] == LIVE_INTAKE_SCHEMA


def test_changing_any_single_input_changes_the_intake_identity(replay):
    base = replay["intake_content"]
    seen = {replay["intake"].identity}
    for field, value in (
        ("round_id", "2026-09-08T13:00:00+00:00"),
        ("bam_sha256", "0" * 64),
        ("bai_sha256", "1" * 64),
        ("reference_sha256", "2" * 64),
        ("fai_sha256", "3" * 64),
    ):
        mutated = verify_live_round_intake(
            build_live_round_intake(
                round_id=value if field == "round_id" else base["round_id"],
                region_source=base["region_source"],
                bam_sha256=value if field == "bam_sha256" else base["bam_sha256"],
                bai_sha256=value if field == "bai_sha256" else base["bai_sha256"],
                reference_sha256=(
                    value if field == "reference_sha256" else base["reference_sha256"]
                ),
                fai_sha256=value if field == "fai_sha256" else base["fai_sha256"],
            )
        )
        assert mutated.identity not in seen, field
        seen.add(mutated.identity)
    # ... and a different region too
    region = build_live_round_intake(
        round_id=base["round_id"],
        region_source=f"{base['chromosome']}:2-{base['region_end0_exclusive']}",
        bam_sha256=base["bam_sha256"],
        bai_sha256=base["bai_sha256"],
        reference_sha256=base["reference_sha256"],
        fai_sha256=base["fai_sha256"],
    )
    assert verify_live_round_intake(region).identity not in seen


def test_the_intake_carries_no_operational_value(replay):
    blob = json.dumps(replay["intake_content"]).lower()
    for pattern in ("http", "://", "/home/", "/tmp/", "presigned", "retrieved_at", "localhost"):
        assert pattern not in blob, pattern


def test_the_intake_schema_is_closed(replay):
    base = replay["intake_content"]
    assert tuple(sorted(base)) == LIVE_INTAKE_FIELDS
    with pytest.raises(LiveRoundIntakeError, match="not exactly"):
        verify_live_round_intake({**base, "num_mutations": 12})
    with pytest.raises(LiveRoundIntakeError, match="not exactly"):
        verify_live_round_intake({k: v for k, v in base.items() if k != "fai_sha256"})


@pytest.mark.parametrize(
    "field,value",
    [
        ("region_hash", "0" * 64),
        ("identity_tuple_hash", "0" * 64),
        ("region_start0", 5),
        ("region_end0_exclusive", 99),
        ("region_length_bp", 7),
        ("chromosome", "chr18"),
        ("schema_version", "l2h-live-round-intake-v2"),
    ],
)
def test_a_declared_field_that_disagrees_with_the_inputs_is_refused(replay, field, value):
    with pytest.raises(LiveRoundIntakeError):
        verify_live_round_intake({**replay["intake_content"], field: value})


@pytest.mark.parametrize(
    "round_id",
    ["", "  ", "../escape", "a/b", "not-a-timestamp", "x" * 41, "2026-09-08T12:00:00+00:00 "],
)
def test_an_unusable_round_id_is_refused(replay, round_id):
    base = replay["intake_content"]
    with pytest.raises(LiveRoundIntakeError):
        build_live_round_intake(
            round_id=round_id,
            region_source=base["region_source"],
            bam_sha256=base["bam_sha256"],
            bai_sha256=base["bai_sha256"],
            reference_sha256=base["reference_sha256"],
            fai_sha256=base["fai_sha256"],
        )


def test_an_unsupported_contig_is_refused(replay):
    base = replay["intake_content"]
    with pytest.raises(LiveRoundIntakeError, match="not one this engine profiles"):
        build_live_round_intake(
            round_id=base["round_id"],
            region_source="chr1:1-1000",
            bam_sha256=base["bam_sha256"],
            bai_sha256=base["bai_sha256"],
            reference_sha256=base["reference_sha256"],
            fai_sha256=base["fai_sha256"],
        )


def test_an_intake_capability_cannot_be_minted_from_a_dictionary(replay):
    with pytest.raises(LiveRoundIntakeError):
        VerifiedLiveRoundIntake(object(), content=replay["intake_content"], identity="x" * 64)


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
    assert len(ownership) == 1, "a live authority owns exactly the round being serviced"


def test_the_live_authority_binds_the_whole_identity_chain(replay):
    intake = replay["intake"]
    owned = replay["ownership"].owned(FRESH_LIVE_ROUND_ID)
    manifest = json.loads(replay["manifest_bytes"])
    profile = json.loads(replay["profile_bytes"])

    assert owned.bam_sha256 == str(intake.bam_sha256)
    assert owned.bai_sha256 == str(intake.bai_sha256)
    assert owned.reference_sha256 == str(intake.reference_sha256)
    assert owned.fai_sha256 == str(intake.fai_sha256)
    assert owned.region_hash == str(intake.region_hash)
    assert owned.identity_tuple_hash == str(intake.identity_tuple_hash)
    assert owned.registry_snapshot_hash == intake.identity
    assert owned.dataset_id == replay["dataset_id"]
    assert owned.profile_id == str(profile["profile_id"]) == str(manifest["profile_id"])
    assert owned.fingerprint_hash == str(manifest["fingerprint_hash"])
    assert owned.attestation_hash == str(replay["attestation"]["attestation_hash"])


def test_the_live_ownership_identity_is_domain_separated_and_round_scoped(replay):
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
    from minos_engine.common.hashing import sha256_hex

    assert ownership.corpus_identity == sha256_hex(LIVE_OWNERSHIP_DOMAIN.encode("utf-8") + expected)
    assert set(ownership.anchors) == {
        "live_intake_identity",
        "live_intake_schema",
        "profile_manifest_sha256",
        "profile_sha256",
        "windows_sha256",
    }
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


def test_live_ownership_verifies_while_the_train_authorities_are_unreadable(replay, monkeypatch):
    """Every research authority is made to raise; the live round still becomes authoritative."""
    opened: list[str] = []
    original_open = builtins.open
    original_io = io.open
    original_read_bytes = Path.read_bytes

    def _guard(name: str) -> None:
        if name in FORBIDDEN_FOR_LIVE:
            opened.append(name)
            raise AssertionError(f"the live path opened a research authority: {name}")

    def guarded_open(file: Any, *args: Any, **kwargs: Any) -> Any:
        _guard(Path(str(file)).name)
        return original_open(file, *args, **kwargs)

    def guarded_io(file: Any, *args: Any, **kwargs: Any) -> Any:
        _guard(Path(str(file)).name)
        return original_io(file, *args, **kwargs)

    def guarded_read_bytes(self: Path) -> bytes:
        _guard(self.name)
        return original_read_bytes(self)

    monkeypatch.setattr(builtins, "open", guarded_open)
    monkeypatch.setattr(io, "open", guarded_io)
    monkeypatch.setattr(Path, "read_bytes", guarded_read_bytes)

    fresh = build_live_replay(SOURCE_ROUND, live_round_id="2026-10-01T09:30:00+00:00")
    ownership = live_ownership(fresh)

    assert opened == []
    assert ownership.rounds() == ("2026-10-01T09:30:00+00:00",)
    assert ownership.scope == "live"


def test_the_live_modules_never_name_a_research_authority():
    """Checked against real string literals, not prose: the docstrings explain what is avoided."""
    import ast

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
# G: the capability still cannot be forged
# --------------------------------------------------------------------------- #
def test_a_dictionary_is_still_not_an_ownership_authority():
    from minos_engine.layer2.round_profile_authority import RoundProfileAuthorityError

    with pytest.raises(RoundProfileAuthorityError, match="verifying loader"):
        VerifiedRoundProfileAuthority(object(), by_round={}, anchors={}, corpus_identity="x" * 64)


def test_a_lookalike_object_is_refused_by_the_controller(replay):
    """Duck typing must not reach the controller: the isinstance check is unchanged."""
    from minos_engine.layer2.safe_controller import (
        SafeControllerAuthorityError,
        load_verified_safe_baseline_authority,
        select_safe_baseline,
    )
    from minos_engine.layer2.safe_controller_qualification import _request_for

    authority = load_verified_safe_baseline_authority(repo_root=REPO_ROOT)
    real = replay["ownership"]
    owned = real.owned(FRESH_LIVE_ROUND_ID)
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

        def rounds(self) -> tuple[str, ...]:
            return (FRESH_LIVE_ROUND_ID,)

    with pytest.raises(SafeControllerAuthorityError, match="verified round/profile corpus"):
        select_safe_baseline(request=request, authority=authority, ownership=Lookalike())


def test_the_live_factory_is_the_only_other_door(replay):
    """`mint_verified_ownership` is the whole of the widening, and it is a verifying factory."""
    source = (REPO_ROOT / "src/minos_engine/layer2/round_profile_authority.py").read_text()
    assert source.count("_CORPUS_TOKEN,") == 2, "exactly two mint sites"
    assert "def mint_verified_ownership(" in source
    with pytest.raises(LiveRoundOwnershipError, match="verified live intake"):
        load_verified_live_round_ownership(
            intake={"round_id": FRESH_LIVE_ROUND_ID},  # type: ignore[arg-type]
            profile_bytes=replay["profile_bytes"],
            manifest_bytes=replay["manifest_bytes"],
            attestation_bytes=replay["attestation_bytes"],
            windows_bytes=replay["windows_bytes"],
        )


# --------------------------------------------------------------------------- #
# L: the negative matrix
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "field,value",
    [
        ("round_id", "2026-01-01T00:00:00+00:00"),
        ("dataset_id", "minos-chr21-0279a3b8042f848b"),
        ("chromosome", "chr18"),
        ("bam_sha256", "0" * 64),
        ("bai_sha256", "1" * 64),
        ("reference_sha256", "2" * 64),
        ("fai_sha256", "3" * 64),
        ("region_hash", "4" * 64),
        ("identity_tuple_hash", "5" * 64),
        ("registry_snapshot_hash", "6" * 64),
    ],
)
def test_an_attestation_that_disagrees_with_the_intake_is_refused(field, value):
    replay = build_live_replay(SOURCE_ROUND, attestation_override={field: value})
    with pytest.raises(LiveRoundOwnershipError):
        live_ownership(replay)


def test_an_attestation_that_does_not_hash_to_itself_is_refused():
    replay = build_live_replay(SOURCE_ROUND, attestation_override={"attestation_hash": "0" * 64})
    with pytest.raises(LiveRoundOwnershipError, match="own recorded identity"):
        live_ownership(replay)


def test_a_profile_belonging_to_another_live_round_is_refused(replay):
    """Same profile bytes, an attestation registered to a DIFFERENT round: refused."""
    other = build_live_replay(SOURCE_ROUND, live_round_id="2026-11-11T11:11:11+00:00")
    with pytest.raises(LiveRoundOwnershipError):
        load_verified_live_round_ownership(
            intake=replay["intake"],
            profile_bytes=other["profile_bytes"],
            manifest_bytes=other["manifest_bytes"],
            attestation_bytes=other["attestation_bytes"],
            windows_bytes=other["windows_bytes"],
        )


def test_a_round_replayed_with_different_input_bytes_is_refused(replay):
    """Same round id, different artifact bytes: the manifest no longer describes them."""
    with pytest.raises(LiveRoundOwnershipError):
        load_verified_live_round_ownership(
            intake=replay["intake"],
            profile_bytes=replay["profile_bytes"] + b"\n",
            manifest_bytes=replay["manifest_bytes"],
            attestation_bytes=replay["attestation_bytes"],
            windows_bytes=replay["windows_bytes"],
        )


def test_tampered_windows_bytes_are_refused(replay):
    with pytest.raises(LiveRoundOwnershipError):
        load_verified_live_round_ownership(
            intake=replay["intake"],
            profile_bytes=replay["profile_bytes"],
            manifest_bytes=replay["manifest_bytes"],
            attestation_bytes=replay["attestation_bytes"],
            windows_bytes=replay["windows_bytes"] + b"\x00",
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


def test_noncanonical_or_unparseable_artifact_bytes_are_refused(replay):
    with pytest.raises(LiveRoundOwnershipError, match="not JSON"):
        load_verified_live_round_ownership(
            intake=replay["intake"],
            profile_bytes=b"{not json",
            manifest_bytes=replay["manifest_bytes"],
            attestation_bytes=replay["attestation_bytes"],
            windows_bytes=replay["windows_bytes"],
        )


@pytest.mark.parametrize("field,value", [("profile_id", "0" * 32)])
def test_a_manifest_that_disagrees_with_the_profile_is_refused(replay, field, value):
    manifest = json.loads(replay["manifest_bytes"])
    manifest[field] = value
    with pytest.raises(LiveRoundOwnershipError):
        load_verified_live_round_ownership(
            intake=replay["intake"],
            profile_bytes=replay["profile_bytes"],
            manifest_bytes=json.dumps(manifest, sort_keys=True).encode(),
            attestation_bytes=replay["attestation_bytes"],
            windows_bytes=replay["windows_bytes"],
        )


@pytest.mark.parametrize("field", ["region_contig", "region_start0", "region_end0"])
def test_a_manifest_describing_a_different_region_is_refused(replay, field):
    manifest = json.loads(replay["manifest_bytes"])
    manifest[field] = "chr18" if field == "region_contig" else 7
    with pytest.raises(LiveRoundOwnershipError, match="different region"):
        load_verified_live_round_ownership(
            intake=replay["intake"],
            profile_bytes=replay["profile_bytes"],
            manifest_bytes=json.dumps(manifest, sort_keys=True).encode(),
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
    from minos_engine.layer2.round_profile_authority import RoundProfileAuthorityError
    from minos_engine.layer2.safe_controller import load_verified_safe_baseline_authority
    from minos_engine.layer2.safe_controller_qualification import _request_for

    authority = load_verified_safe_baseline_authority(repo_root=REPO_ROOT)
    ownership = replay["ownership"]
    owned = ownership.owned(FRESH_LIVE_ROUND_ID)
    override = {field: "0" * (32 if field == "profile_id" else 64)}
    request = _request_for(owned, authority=authority, mode=ControlMode.SAFE_BASELINE, **override)
    with pytest.raises(RoundProfileAuthorityError):
        ownership.require_owned_request(request)


def test_a_request_whose_identity_tuple_disagrees_is_refused_by_the_contract_itself(replay):
    """``identity_tuple_hash`` never reaches ownership: ``Layer1ProfileReference`` recomputes it."""
    from pydantic import ValidationError

    from minos_engine.layer2.safe_controller import load_verified_safe_baseline_authority
    from minos_engine.layer2.safe_controller_qualification import _request_for

    authority = load_verified_safe_baseline_authority(repo_root=REPO_ROOT)
    owned = replay["ownership"].owned(FRESH_LIVE_ROUND_ID)
    with pytest.raises(ValidationError, match="identity tuple"):
        _request_for(
            owned,
            authority=authority,
            mode=ControlMode.SAFE_BASELINE,
            identity_tuple_hash="0" * 64,
        )


def test_a_request_for_a_round_this_authority_does_not_own_is_refused(replay):
    from minos_engine.layer2.round_profile_authority import RoundProfileAuthorityError

    with pytest.raises(RoundProfileAuthorityError, match="not in the accepted profile snapshot"):
        replay["ownership"].owned("2099-01-01T00:00:00+00:00")


# --------------------------------------------------------------------------- #
# H: the manifest version decision, enforced
# --------------------------------------------------------------------------- #
def test_a_live_authority_cannot_produce_a_v1_decision_manifest(replay):
    """v1 fields would change meaning; the engine refuses rather than quietly reinterpreting."""
    from minos_engine.layer2.safe_controller import (
        SafeControllerAuthorityError,
        load_verified_safe_baseline_authority,
        select_safe_baseline,
    )
    from minos_engine.layer2.safe_controller_qualification import _request_for

    authority = load_verified_safe_baseline_authority(repo_root=REPO_ROOT)
    ownership = replay["ownership"]
    owned = ownership.owned(FRESH_LIVE_ROUND_ID)
    request = _request_for(owned, authority=authority, mode=ControlMode.SAFE_BASELINE)
    with pytest.raises(SafeControllerAuthorityError, match="decision-manifest v2"):
        select_safe_baseline(request=request, authority=authority, ownership=ownership)


# --------------------------------------------------------------------------- #
# M: the TRAIN authority is untouched
# --------------------------------------------------------------------------- #
def test_the_train_corpus_identity_has_not_moved():
    train = load_verified_round_profile_corpus(root=REPO_ROOT)
    assert train.corpus_identity == ACCEPTED_TRAIN_CORPUS_IDENTITY
    assert len(train) == 50
    assert train.partition == "train"
    assert train.scope == "train"
    assert "train_schedule_manifest_sha256" in train.anchors


def test_the_train_authority_still_refuses_a_live_round():
    from minos_engine.layer2.round_profile_authority import RoundProfileAuthorityError

    train = load_verified_round_profile_corpus(root=REPO_ROOT)
    with pytest.raises(RoundProfileAuthorityError):
        train.owned(FRESH_LIVE_ROUND_ID)


def test_the_train_route_still_produces_a_v1_manifest():
    """The accepted qualification route is unchanged: same scope, same manifest, same shape."""
    from minos_engine.layer2.safe_controller import (
        SAFE_DECISION_MANIFEST_SCHEMA,
        load_verified_safe_baseline_authority,
        select_safe_baseline,
    )
    from minos_engine.layer2.safe_controller_qualification import _request_for

    authority = load_verified_safe_baseline_authority(repo_root=REPO_ROOT)
    train = load_verified_round_profile_corpus(root=REPO_ROOT)
    owned = train.owned(train.rounds()[0])
    request = _request_for(owned, authority=authority, mode=ControlMode.SAFE_BASELINE)
    result = select_safe_baseline(request=request, authority=authority, ownership=train)
    assert result.selected_config.sha256 == authority.baseline_config_hash

    from minos_engine.layer2.safe_controller import safe_decision_manifest_content

    manifest = safe_decision_manifest_content(request=request, authority=authority, ownership=train)
    assert manifest["schema_version"] == SAFE_DECISION_MANIFEST_SCHEMA
    assert manifest["profile_corpus_identity"] == ACCEPTED_TRAIN_CORPUS_IDENTITY


# --------------------------------------------------------------------------- #
# no gate, no service, no seal breach
# --------------------------------------------------------------------------- #
def test_nothing_here_reaches_truth_scoring_or_a_sealed_partition():
    import ast

    for name in ("live_round_intake.py", "live_round_authority.py"):
        tree = ast.parse((REPO_ROOT / "src/minos_engine/layer2" / name).read_text())
        modules: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                modules.append(node.module)
            elif isinstance(node, ast.Import):
                modules.extend(a.name for a in node.names)
        for module in modules:
            for banned in ("hap", "scoring", "truth", "evaluation", "mutations", "split"):
                if banned == "split":
                    continue
                assert banned not in module.split("."), f"{name}: {module}"


def test_the_service_is_still_blocked_and_no_gate_was_issued():
    from minos_engine.common.errors import StageNotReadyError
    from minos_engine.layer2.service import Layer2Service

    with pytest.raises(StageNotReadyError):
        Layer2Service().select_config(None)  # type: ignore[arg-type]
    assert not (REPO_ROOT / "gates/models-qualified.json").exists()
    assert not (REPO_ROOT / "gates/controller-frozen.json").exists()
