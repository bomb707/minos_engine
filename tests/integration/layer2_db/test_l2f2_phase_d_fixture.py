"""The committed Phase-D campaign fixture must be the campaign, or fail closed.

The Phase-D preparation proof runs on a GitHub runner that has never seen the campaign
workspace, so the evidence it needs is committed. Committed evidence is only worth what its
integrity checks are worth: a bundle nobody re-hashes is a bundle that can drift into agreeing
with whatever the code happens to do.

So this module re-derives rather than re-reads wherever it can:

* the finalist freeze is hashed and then loaded through the ACCEPTED loader — not a second
  parser written for tests, which could disagree with production about what the document means;
* every payload must satisfy three independent identities at once — the raw bytes, the filename
  it is stored under, and the canonical CONFIG hash the parameter space computes from it;
* the adversarial identities are REGENERATED from committed Phase-B design inputs rather than
  taken on the manifest's word. If ``design.py``, the committed seed fixture or the committed
  design report ever stop reproducing them, this fails.

The manifest is test provenance. It is never a scientific authority: every accepted constant
asserted here is written out as a literal, so a manifest edited to describe a different campaign
cannot redefine the campaign — it can only disagree with these literals and fail.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path
from typing import Any

import pytest

from minos_engine.baseline.design import InfluentialDimension, build_phase_b_configs
from minos_engine.baseline.finalist_freeze import load_finalist_freeze
from minos_engine.baseline.phase_d import build_l2f2_phase_d_authority
from minos_engine.experiments.gatk_live_space import (
    canonicalize_live_gatk_config,
    live_gatk_parameter_space,
)
from tests.conftest import REPO_ROOT
from tests.l2f2_phase_d_fixture import (
    FIXTURE_CONFIG_ROOT,
    FIXTURE_FREEZE_PATH,
    FIXTURE_MANIFEST_PATH,
    FIXTURE_ROOT,
    forgery_config_hashes,
    load_fixture_manifest,
)

# --------------------------------------------------------------------------------------------
# THIS campaign, as literals. The manifest may not redefine any of them.
# --------------------------------------------------------------------------------------------
_FREEZE_SHA = "540aeca0640871ca91e3ec771ec66d2df4b96d38210ec3265f944dee3e0433f3"
_CLOSURE_SHA = "5de368eec327b66c868737d1819cc1b1a590eaf185b28e53d1cfecae59b593ca"
_PROTOCOL = "c548e190571f5e964560cf30021a520ea8aad6674569fa3202af880d7dff77d1"
_PARAMETER_SPACE = "b2d401918084d64023305d9262baf5011a89fe517bee4e0bd33af79fb14aee2e"
_ENVIRONMENT = "71e14a49833ac77bb9dc576345fb89c4dd68f4a3ad3673eb098d38593c1ef4d3"
_CONTRACT = "b24a07e208ce8e2fff6672102ae4e61aed93c6f352a5af46ba81c4789adb76d6"
_SUBNET = "649bb92c6abccebde58a736a2b2af7fd77a701c1"
_SPLIT_MANIFEST = "ffdd31955a24147430156aff003248f8acb51c68514ca95c6fdbe75525328773"
_PLAN_HASH = "f6bd1e450c38d789dcfcdafaaf357dad2f7602f53fc8ec779c5be40c71e6d7ce"

_FINALISTS = (
    "157d88d1587c13be395c62d60e27d1becdada78fad45e65d883bc1190e51acea",
    "0972930f8d8c562be15382203e123b2909094e7eac46e84321d36c67abf8345e",
    "22a1f1fd9ddf02a97776d991f11280b3982673693a4f357479098a99fb411a16",
    "4251cb85e5cd58b7eabfe530b9df23ea7d1d14fd882114b488d67cbd81b751b8",
)
_INHERITED = [42, 25, 36, 0]
_SEED = _FINALISTS[3]

#: the adversarial four, by Phase-B design index and exact full identity. Full hashes, never
#: prefixes: a prefix assertion is an assertion that something *starts* like the campaign.
_FORGERY: tuple[tuple[int, str], ...] = (
    (7, "959d1e946a8800220fba79246213d24460b41bbec3c6956c8fca6ee6f457fa6a"),
    (11, "72d0fa71b844fa42b833dbcc97c0949df25c91bca6f9395319aa3443fe6e387e"),
    (31, "d61d7b26fa9f216e0ed44375ca6056d84deb31a87da8976a8e6154abbe796aa8"),
    (43, "b2fc30c077e93d18fb94fa54a89e27f7a3b4021e26af6468b4ce54ed6af7d2ba"),
)

#: the committed public inputs the Phase-B design is regenerated from.
_SEED_FIXTURE = REPO_ROOT / "tests" / "fixtures" / "gatk" / "default_config.json"
_DESIGN_REPORT = REPO_ROOT / "reports" / "layer2" / "l2f2-e-phase-b-readiness.json"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _regenerate_phase_b_design() -> dict[int, str]:
    """The closed Phase-B design, rebuilt from committed public inputs alone.

    Layout is the design's own: index 0 is the seed, 1..6 the Phase-A anchors, 7..47 the LHS
    sequence. The anchors enter only as HASHES — their payloads are not public and are not in
    this repository — so only the seed and the LHS positions carry a regenerated payload here.
    """
    design = json.loads(_DESIGN_REPORT.read_bytes())["phase_b_design"]
    order = list(live_gatk_parameter_space().names())
    dimensions = tuple(
        InfluentialDimension(
            name=d["name"], impact=d["impact"], live_parameter_index=order.index(d["name"])
        )
        for d in design["influential_dimensions"]
    )
    seed = canonicalize_live_gatk_config(json.loads(_SEED_FIXTURE.read_bytes()))
    lhs = build_phase_b_configs(
        dimensions=dimensions,
        seed=seed,
        anchor_config_hashes=tuple(design["anchor_config_hashes"]),
    )
    resolved = {0: seed.config_hash}
    for position, anchor in enumerate(design["anchor_config_hashes"], start=1):
        resolved[position] = str(anchor)
    for position, config in enumerate(lhs, start=7):
        resolved[position] = config.config_hash
    return resolved


# --------------------------------------------------------------------------------------------
# A / B / C — the freeze
# --------------------------------------------------------------------------------------------
def test_the_fixture_freeze_is_this_campaigns_bytes() -> None:
    assert _sha256(FIXTURE_FREEZE_PATH) == _FREEZE_SHA
    assert FIXTURE_FREEZE_PATH.stat().st_size == 10033


def test_the_fixture_freeze_loads_through_the_accepted_loader() -> None:
    """The production loader, not a test parser. A second parser could disagree with production."""
    freeze = load_finalist_freeze(
        FIXTURE_FREEZE_PATH,
        expected_artifact_sha256=_FREEZE_SHA,
        expected_phase_c_closure_sha256=_CLOSURE_SHA,
    )
    authority = build_l2f2_phase_d_authority(freeze)

    assert authority.finalist_freeze_sha256 == _FREEZE_SHA
    assert authority.phase_c_closure_sha256 == _CLOSURE_SHA
    assert authority.baseline_protocol_hash == _PROTOCOL
    assert authority.parameter_space_hash == _PARAMETER_SPACE
    assert authority.execution_environment_hash == _ENVIRONMENT
    assert authority.scoring_contract_hash == _CONTRACT
    assert authority.minos_subnet_sha == _SUBNET
    assert authority.split_manifest_sha256 == _SPLIT_MANIFEST
    assert authority.seed_config_hash == _SEED
    assert authority.plan_hash == _PLAN_HASH
    assert tuple(authority.ordered_config_hashes) == _FINALISTS
    assert [authority.inherited_candidate_index[h] for h in _FINALISTS] == _INHERITED


def test_the_manifest_cannot_redefine_the_campaign() -> None:
    """The manifest must AGREE with the literals above; it is never their source."""
    manifest = load_fixture_manifest()
    assert manifest["schema"] == "l2f2-phase-d-ci-fixture-v1"
    assert manifest["finalist_freeze"]["sha256"] == _FREEZE_SHA
    assert manifest["phase_c_closure_sha256"] == _CLOSURE_SHA
    assert manifest["baseline_protocol_hash"] == _PROTOCOL
    assert manifest["parameter_space_hash"] == _PARAMETER_SPACE
    assert manifest["execution_environment_hash"] == _ENVIRONMENT
    assert manifest["scoring_contract_hash"] == _CONTRACT
    assert manifest["minos_subnet_sha"] == _SUBNET
    assert manifest["seed_config_hash"] == _SEED
    assert tuple(e["config_hash"] for e in manifest["ordered_finalists"]) == _FINALISTS
    assert manifest["inherited_candidate_indices"] == _INHERITED
    assert [e["config_index"] for e in manifest["ordered_finalists"]] == [0, 1, 2, 3]


# --------------------------------------------------------------------------------------------
# D — the four accepted finalist payloads
# --------------------------------------------------------------------------------------------
@pytest.mark.parametrize(("index", "config_hash"), list(enumerate(_FINALISTS)))
def test_each_finalist_payload_satisfies_three_identities(index: int, config_hash: str) -> None:
    """Raw bytes, stored filename and canonical CONFIG hash must all be the same identity."""
    path = FIXTURE_CONFIG_ROOT / f"{config_hash}.json"
    payload = path.read_bytes()

    assert hashlib.sha256(payload).hexdigest() == config_hash
    assert path.stem == config_hash
    recanonical = canonicalize_live_gatk_config(json.loads(payload))
    assert recanonical.config_hash == config_hash
    assert recanonical.parameter_space_hash == _PARAMETER_SPACE

    recorded = load_fixture_manifest()["ordered_finalists"][index]
    assert recorded["config_hash"] == config_hash
    assert recorded["sha256"] == config_hash
    assert recorded["size_bytes"] == len(payload)


# --------------------------------------------------------------------------------------------
# E — idx-7 provenance, and the rest of the adversarial four
# --------------------------------------------------------------------------------------------
def test_the_committed_design_inputs_reproduce_the_whole_campaign() -> None:
    """The regeneration is trustworthy only if it also reproduces identities we already know."""
    resolved = _regenerate_phase_b_design()
    assert resolved[0] == _SEED
    assert resolved[42] == _FINALISTS[0]
    assert resolved[25] == _FINALISTS[1]
    assert resolved[36] == _FINALISTS[2]


@pytest.mark.parametrize(("design_index", "config_hash"), _FORGERY)
def test_each_forgery_identity_regenerates_from_committed_source(
    design_index: int, config_hash: str
) -> None:
    """Committed design inputs → deterministic generation → this exact full identity.

    Design member 7 in particular is here on its provenance, not on a copied file's say-so: it
    was never promoted into the Phase-C ten, so no campaign artifact vouches for it. What
    vouches for it is that ``build_phase_b_configs`` puts it at index 7 when driven by the
    committed seed fixture and the committed design report — and it stops vouching the moment
    any of those three change.
    """
    resolved = _regenerate_phase_b_design()
    assert resolved[design_index] == config_hash

    path = FIXTURE_CONFIG_ROOT / f"{config_hash}.json"
    payload = path.read_bytes()
    assert hashlib.sha256(payload).hexdigest() == config_hash
    assert path.stem == config_hash
    assert canonicalize_live_gatk_config(json.loads(payload)).config_hash == config_hash


def test_no_phase_a_anchor_payload_is_committed() -> None:
    """The three Phase-A anchors are in the Phase-C ten and are deliberately NOT in this bundle.

    Their payload bytes are not public and are not reconstructible from this repository, so
    committing one would be a disclosure this fixture is not authorized to make. The bundle
    holds exactly eight payloads and none of them is an anchor.
    """
    design = json.loads(_DESIGN_REPORT.read_bytes())["phase_b_design"]
    anchors = {str(h) for h in design["anchor_config_hashes"]}
    assert len(anchors) == 6

    stored = {p.stem for p in FIXTURE_CONFIG_ROOT.glob("*.json")}
    assert stored & anchors == set()
    assert stored == set(_FINALISTS) | {h for _, h in _FORGERY}
    assert len(stored) == 8


# --------------------------------------------------------------------------------------------
# F — the adversarial four are genuinely wrong
# --------------------------------------------------------------------------------------------
def test_the_forged_four_are_distinct_and_none_is_a_finalist() -> None:
    forged = forgery_config_hashes()
    assert forged == tuple(h for _, h in _FORGERY)
    assert len(set(forged)) == 4
    assert not set(forged) & set(_FINALISTS)

    manifest = load_fixture_manifest()["forgery_identities"]
    assert [m["phase_b_design_index"] for m in manifest["members"]] == [i for i, _ in _FORGERY]
    assert all(m["is_finalist"] is False for m in manifest["members"])
    # three lost in Phase C; design member 7 was never promoted. Both are genuinely not-the-four.
    assert [m["promoted_to_phase_c"] for m in manifest["members"]] == [False, True, True, True]


def test_no_payload_identity_is_duplicated() -> None:
    stored = sorted(FIXTURE_CONFIG_ROOT.glob("*.json"))
    digests = [_sha256(p) for p in stored]
    assert len(set(digests)) == len(digests) == 8
    assert [p.stem for p in stored] == sorted(digests)


# --------------------------------------------------------------------------------------------
# G — tamper detection
# --------------------------------------------------------------------------------------------
def _verify_bundle(root: Path) -> None:
    """Every byte the manifest describes must still hash to what it says. Fail closed."""
    manifest = json.loads((root / "manifest.json").read_bytes())
    freeze = root / manifest["finalist_freeze"]["filename"]
    if _sha256(freeze) != manifest["finalist_freeze"]["sha256"]:
        raise AssertionError("the finalist freeze does not match the manifest")
    if freeze.stat().st_size != manifest["finalist_freeze"]["size_bytes"]:
        raise AssertionError("the finalist freeze is not the recorded size")
    recorded = list(manifest["ordered_finalists"]) + list(manifest["forgery_identities"]["members"])
    for entry in recorded:
        path = root / "config_artifacts" / f"{entry['config_hash']}.json"
        payload = path.read_bytes()
        if hashlib.sha256(payload).hexdigest() != entry["sha256"]:
            raise AssertionError(f"payload {entry['config_hash']} does not match the manifest")
        if len(payload) != entry["size_bytes"]:
            raise AssertionError(f"payload {entry['config_hash']} is not the recorded size")


def test_the_committed_bundle_verifies_against_its_manifest() -> None:
    _verify_bundle(FIXTURE_ROOT)


@pytest.mark.parametrize(
    "victim",
    ["phase_c_validation_finalists_20260830.json", f"config_artifacts/{_FINALISTS[2]}.json"],
)
def test_a_tampered_byte_is_detected(tmp_path: Path, victim: str) -> None:
    """One appended newline is enough. It has to be — a digest that tolerates drift is not one."""
    copy = tmp_path / "bundle"
    shutil.copytree(FIXTURE_ROOT, copy)
    _verify_bundle(copy)

    target = copy / victim
    target.write_bytes(target.read_bytes() + b"\n")
    with pytest.raises(AssertionError, match="does not match the manifest"):
        _verify_bundle(copy)


def test_the_bundle_carries_no_credentials_truth_or_test_data() -> None:
    """A fixture is a place secrets get committed by accident. Assert they did not."""
    manifest = load_fixture_manifest()
    assert manifest["contains_no_credentials"] is True
    assert manifest["contains_no_truth_data"] is True
    assert manifest["contains_no_test_partition_data"] is True
    assert manifest["contains_no_phase_a_anchor_payloads"] is True

    forbidden = (
        "postgresql://",
        "postgres://",
        "password",
        "PGPASSWORD",
        "MINOS_DATABASE_URL",
        "truth_vcf",
        "truth_tbi",
        "BEGIN PRIVATE KEY",
    )
    for path in sorted(FIXTURE_ROOT.rglob("*")):
        if not path.is_file():
            continue
        text = path.read_bytes().decode("utf-8", errors="replace").lower()
        for token in forbidden:
            assert token.lower() not in text, f"{path.name} contains {token!r}"


def test_the_only_machine_local_path_in_frozen_bytes_is_inert_provenance() -> None:
    """The frozen freeze records where its closure came from. That string cannot be edited out.

    ``phase_c_closure_artifact.path`` is part of the bytes that hash to the accepted
    ``540aeca0…``; rewriting it to something portable would destroy the identity the whole
    campaign is anchored to. So it stays, and what must be proven instead is that it is inert —
    a provenance record, not a dependency. The closure is bound by SHA-256 inside the document,
    and nothing resolves that path.

    No CONFIG payload contains such a string at all.
    """
    raw = FIXTURE_FREEZE_PATH.read_bytes()
    assert raw.count(b"/home/") == 1
    document = json.loads(raw)
    assert document["phase_c_closure_artifact"]["path"].startswith("/home/")
    assert document["phase_c_closure_artifact"]["sha256"] == _CLOSURE_SHA

    for path in sorted(FIXTURE_CONFIG_ROOT.glob("*.json")):
        assert b"/home/" not in path.read_bytes(), path.name

    manifest = load_fixture_manifest()
    assert manifest["source_campaign_root"] == "/home/hr/bittensor/minos_l2f2_baseline"
    body = json.dumps({k: v for k, v in manifest.items() if k != "source_campaign_root"})
    assert "/home/" not in body


def test_the_accepted_loader_never_opens_the_recorded_provenance_path(
    monkeypatch: Any,
) -> None:
    """Decisive for CI: the loader must read the freeze and nothing else.

    If ``load_finalist_freeze`` ever resolved ``phase_c_closure_artifact.path``, this fixture
    would be portable on this machine and broken on every runner — the exact failure this
    corrective exists to remove, hidden one level deeper. So the filesystem is instrumented and
    the opened paths are asserted, rather than the behaviour being inferred from reading the
    source.
    """
    opened: list[str] = []
    real_read_bytes = Path.read_bytes
    real_read_text = Path.read_text
    real_open = Path.open

    def spy_read_bytes(self: Path) -> bytes:
        opened.append(str(self))
        return real_read_bytes(self)

    def spy_read_text(self: Path, *args: Any, **kwargs: Any) -> str:
        opened.append(str(self))
        return real_read_text(self, *args, **kwargs)

    def spy_open(self: Path, *args: Any, **kwargs: Any) -> Any:
        opened.append(str(self))
        return real_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_bytes", spy_read_bytes)
    monkeypatch.setattr(Path, "read_text", spy_read_text)
    monkeypatch.setattr(Path, "open", spy_open)

    freeze = load_finalist_freeze(
        FIXTURE_FREEZE_PATH,
        expected_artifact_sha256=_FREEZE_SHA,
        expected_phase_c_closure_sha256=_CLOSURE_SHA,
    )
    authority = build_l2f2_phase_d_authority(freeze)
    monkeypatch.undo()

    assert authority.plan_hash == _PLAN_HASH
    assert opened, "the spy recorded nothing; it is not instrumenting the loader"
    recorded_provenance = json.loads(FIXTURE_FREEZE_PATH.read_bytes())["phase_c_closure_artifact"][
        "path"
    ]
    assert recorded_provenance not in opened
    outside = [p for p in opened if "l2f2_phase_d_campaign" not in p and "minos_engine" not in p]
    assert outside == [], outside


def test_the_fixture_root_resolves_from_the_source_tree_not_the_cwd(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """The whole point of the corrective: the suite must not care where it is run from."""
    import os

    monkeypatch.chdir(tmp_path)
    assert FIXTURE_ROOT.is_absolute()
    assert FIXTURE_ROOT.is_dir()
    assert FIXTURE_MANIFEST_PATH.is_file()
    assert _sha256(FIXTURE_FREEZE_PATH) == _FREEZE_SHA
    assert Path(os.getcwd()).resolve() == tmp_path.resolve()
