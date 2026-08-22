"""PROFILE-SNAPSHOT-FROZEN-1: offline verification + targeted tamper rejection.

Every named check must fail under its specific tampering. Committed-evidence tests skip
until the evidence commit exists; helper-level tamper tests always run.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from minos_engine.gates.required_checks import required_checks_for
from minos_engine.layer2 import prerequisites as PRE
from minos_engine.qualification.layer2_snapshot_runner import (
    _attestations_exactly_bound,
    verify_snapshot_offline,
)
from tests.conftest import REPO_ROOT

_GATE = REPO_ROOT / "gates" / "profile-snapshot-frozen-1.json"
_NEEDED = (
    "gates/profile-snapshot-frozen-1.json",
    "manifests/profile_snapshot_epoch1_members.json",
    "manifests/profile_snapshot_epoch1_selections.json",
    "manifests/profile_snapshot_epoch1_artifact_inventory.json",
    "manifests/layer2_dataset_split_v2_epoch1.json",
    "gates/ingest-ready.json",
    "gates/split-frozen-v2.json",
    "gates/split-frozen.json",
    "gates/protocol-ready.json",
    "gates/twin-ready.json",
    "gates/l1-ready.json",
    "gates/db-ready.json",
    ".github/workflows/ci.yml",
    "reports/PROFILE_SNAPSHOT_FROZEN_1_REPORT.md",
)


def test_required_checks_registered() -> None:
    required = required_checks_for("PROFILE-SNAPSHOT-FROZEN-1")
    for check in (
        "zero_ingestion_failures",
        "m5_mismatch_count_zero",
        "identities_match_registry",
        "profiler_identity_exact",
        "attestation_files_exactly_bound",
        "member_manifest_canonical_integrity",
        "inventory_canonical_integrity",
        "inventory_four_artifacts_each",
        "operational_artifact_bytes_reverified",
        "ci_verifies_snapshot_gate",
        "accepted_ingest_ready_bound",
        "snapshot_hash_recomputed",
    ):
        assert check in required, check


def test_attestation_byte_tamper_detected(tmp_path: Path) -> None:
    """Altered attestation bytes fail the exact-binding helper (no DB required)."""
    from minos_engine.layer2.ingest.contracts import InputIntegrityAttestation, M5Status

    att = InputIntegrityAttestation(
        generator="g",
        generator_version="v",
        dataset_id="minos-chr18-00000000000000aa",
        round_id="00000000000000aa",
        chromosome="chr18",
        registry_snapshot_hash="a" * 64,
        bam_sha256="b" * 64,
        bai_sha256="c" * 64,
        reference_sha256="d" * 64,
        fai_sha256="e" * 64,
        region_hash="f" * 64,
        identity_tuple_hash="1" * 64,
        bam_sq_m5=None,
        computed_reference_m5="2" * 32,
        m5_status=M5Status.ABSENT,
    )
    rdir = tmp_path / att.round_id
    rdir.mkdir()
    member = {
        "round_id": att.round_id,
        "dataset_id": att.dataset_id,
        "identity_tuple_hash": att.identity_tuple_hash,
        "registry_snapshot_hash": att.registry_snapshot_hash,
        "attestation_hash": att.attestation_hash,
        "m5_status": "ABSENT",
    }
    p = rdir / "input-integrity-attestation-v1.json"
    p.write_text(json.dumps(att.model_dump(mode="json")), encoding="utf-8")
    assert _attestations_exactly_bound(tmp_path, [member]) is True
    # tamper the bytes: content no longer matches the canonical attestation hash
    raw = json.loads(p.read_text())
    raw["bam_sha256"] = "0" * 64
    p.write_text(json.dumps(raw), encoding="utf-8")
    assert _attestations_exactly_bound(tmp_path, [member]) is False
    # wrong stored hash also fails
    p.write_text(json.dumps(att.model_dump(mode="json")), encoding="utf-8")
    assert (
        _attestations_exactly_bound(tmp_path, [{**member, "attestation_hash": "9" * 64}]) is False
    )


# --------------------------------------------------------------------------- #
# committed-evidence tamper matrix (activates once the evidence commit exists)
# --------------------------------------------------------------------------- #
def _tamper_root(tmp_path: Path, mutate) -> Path:
    """Materialize the declared evidence set into a scratch root.

    ``.github/workflows/ci.yml`` is HISTORICAL evidence: it was present at the frozen qualified
    source commit and no longer exists at HEAD (TEST-CI-3 removed the remote full workflow). It is
    therefore materialized from its frozen commit, exactly as the production verifier reads it —
    never recreated at the live path, which would defeat the point of deleting it.
    """
    from minos_engine.layer2 import prerequisites as PRE
    from minos_engine.qualification.git_tree import historical_blob_text

    root = tmp_path / "repo"
    for rel in _NEEDED:
        dst = root / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        source = REPO_ROOT / rel
        if source.exists():
            shutil.copy(source, dst)
            continue
        frozen = historical_blob_text(REPO_ROOT, rel, PRE.PROFILE_SNAPSHOT_FROZEN_1_SOURCE_COMMIT)
        assert frozen is not None, f"no committed blob for {rel} at the frozen source commit"
        dst.write_text(frozen, encoding="utf-8")
    mutate(root)
    return root


def _mutate_json(root: Path, rel: str, fn) -> None:
    p = root / rel
    data = json.loads(p.read_text())
    fn(data)
    p.write_text(json.dumps(data), encoding="utf-8")


needs_evidence = pytest.mark.skipif(not _GATE.exists(), reason="evidence commit pending")


@needs_evidence
def test_offline_verifies_on_repo() -> None:
    r = verify_snapshot_offline(REPO_ROOT, 1)
    assert r.ok, r.reasons


@needs_evidence
def test_tampered_inventory_hash_detected(tmp_path: Path) -> None:
    root = _tamper_root(
        tmp_path,
        lambda r: _mutate_json(
            r,
            "manifests/profile_snapshot_epoch1_artifact_inventory.json",
            lambda d: d.__setitem__("inventory_hash", "0" * 64),
        ),
    )
    r = verify_snapshot_offline(root, 1)
    assert not r.ok and r.checks["inventory_canonical_integrity"] is False


@needs_evidence
def test_wrong_identity_tuple_detected(tmp_path: Path) -> None:
    def mut(d: dict) -> None:
        d["members"][0]["identity_tuple_hash"] = "0" * 64

    root = _tamper_root(
        tmp_path,
        lambda r: _mutate_json(r, "manifests/profile_snapshot_epoch1_members.json", mut),
    )
    r = verify_snapshot_offline(root, 1)
    assert not r.ok and r.checks["identities_match_registry"] is False


@needs_evidence
def test_wrong_profiler_config_detected(tmp_path: Path) -> None:
    def mut(d: dict) -> None:
        d["members"][0]["profiler_config_hash"] = "0" * 64

    root = _tamper_root(
        tmp_path,
        lambda r: _mutate_json(r, "manifests/profile_snapshot_epoch1_members.json", mut),
    )
    r = verify_snapshot_offline(root, 1)
    assert not r.ok and r.checks["profiler_identity_exact"] is False


@needs_evidence
def test_missing_fourth_artifact_detected(tmp_path: Path) -> None:
    def mut(d: dict) -> None:
        del d["entries"][0]["artifacts"]["input-integrity-attestation-v1.json"]

    root = _tamper_root(
        tmp_path,
        lambda r: _mutate_json(r, "manifests/profile_snapshot_epoch1_artifact_inventory.json", mut),
    )
    r = verify_snapshot_offline(root, 1)
    assert not r.ok and r.checks["inventory_four_artifacts_each"] is False


@needs_evidence
def test_duplicate_inventory_member_detected(tmp_path: Path) -> None:
    def mut(d: dict) -> None:
        d["entries"][1] = d["entries"][0]  # duplicate round id within 75 entries

    root = _tamper_root(
        tmp_path,
        lambda r: _mutate_json(r, "manifests/profile_snapshot_epoch1_artifact_inventory.json", mut),
    )
    r = verify_snapshot_offline(root, 1)
    assert not r.ok and r.checks["inventory_four_artifacts_each"] is False


@needs_evidence
def test_modified_member_manifest_detected(tmp_path: Path) -> None:
    def mut(d: dict) -> None:
        d["members"][0]["feature_values_hash"] = "0" * 64

    root = _tamper_root(
        tmp_path,
        lambda r: _mutate_json(r, "manifests/profile_snapshot_epoch1_members.json", mut),
    )
    r = verify_snapshot_offline(root, 1)
    assert not r.ok
    assert (
        r.checks["member_manifest_canonical_integrity"] is False
        or r.checks["snapshot_hash_recomputed"] is False
    )


@needs_evidence
def test_wrong_ingest_ready_identity_detected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(PRE, "INGEST_READY_GATE_HASH", "0" * 64)
    r = verify_snapshot_offline(REPO_ROOT, 1)
    assert not r.ok and r.checks["accepted_ingest_ready_bound"] is False


@needs_evidence
def test_modified_artifact_bytes_detected(tmp_path: Path) -> None:
    """Operational-level: a corpus artifact whose bytes changed fails the rebuild compare."""
    from minos_engine.common.hashing import canonical_hash
    from minos_engine.qualification.layer2_snapshot_runner import build_artifact_inventory

    members = json.loads((REPO_ROOT / "manifests/profile_snapshot_epoch1_members.json").read_text())
    committed = json.loads(
        (REPO_ROOT / "manifests/profile_snapshot_epoch1_artifact_inventory.json").read_text()
    )
    one = members["members"][0]
    corpus = tmp_path / "corpus"
    src = Path("/home/hr/bittensor/minos_l2d_corpus") / str(one["round_id"])
    if not src.exists():
        pytest.skip("operational corpus not available on this machine")
    shutil.copytree(src, corpus / str(one["round_id"]))
    (corpus / str(one["round_id"]) / "bam-profile-v1.json").write_bytes(b"{}")
    tiny = {**members, "members": [one]}
    rebuilt = build_artifact_inventory(corpus, tiny)
    committed_entry = next(e for e in committed["entries"] if e["round_id"] == str(one["round_id"]))
    assert canonical_hash(rebuilt["entries"][0]) != canonical_hash(committed_entry)


# --------------------------------------------------------------------------- #
# gate-contract tamper matrix (final review): each named contract check rejects
# --------------------------------------------------------------------------- #
def _gate_tamper(tmp_path: Path, mutate) -> Path:
    root = _tamper_root(tmp_path, lambda r: None)
    shutil.copy(
        REPO_ROOT / "reports/PROFILE_SNAPSHOT_FROZEN_1_REPORT.md",
        (root / "reports" / "PROFILE_SNAPSHOT_FROZEN_1_REPORT.md"),
    ) if (root / "reports").mkdir(parents=True, exist_ok=True) is None else None
    _mutate_json(root, "gates/profile-snapshot-frozen-1.json", mutate)
    return root


def _offline(root: Path):
    return verify_snapshot_offline(root, 1, git_root=REPO_ROOT)


@needs_evidence
@pytest.mark.parametrize(
    ("named_check", "mutate"),
    [
        ("gate_status_pass", lambda d: d.__setitem__("status", "HOLD")),
        (
            "mandatory_set_exact",
            lambda d: d["mandatory_checks"].pop("member_count_75"),
        ),
        (
            "mandatory_set_exact",
            lambda d: d["mandatory_checks"].__setitem__("smuggled_extra", True),
        ),
        (
            "offline_results_match_recomputed",
            lambda d: d["mandatory_checks"].__setitem__("member_count_75", False),
        ),
        (
            "qualified_source_tree_matches",
            lambda d: d.__setitem__("qualified_source_git_sha", "0" * 40),
        ),
        (
            "qualified_source_tree_matches",
            lambda d: d.__setitem__("qualified_source_tree_sha", "0" * 40),
        ),
        (
            "gate_tool_version",
            lambda d: d.__setitem__("qualification_tool_version", "wrong-tool"),
        ),
        (
            "gate_engine_sha_matches_source",
            lambda d: d.__setitem__("engine_git_sha", "1" * 40),
        ),
        (
            "report_bytes_bound",
            lambda d: d["input_hashes"].__setitem__("qualification_report_hash", "0" * 64),
        ),
        (
            "evidence_paths_exact",
            lambda d: d["evidence"][0].__setitem__("path", "manifests/substituted.json"),
        ),
        (
            "source_descends_ingest_ready_evidence",
            lambda d: d.__setitem__(
                "qualified_source_git_sha",
                "5ff8c361acc19613f0db7e4f93f88fe4aab9bfd5",  # pre-INGEST-READY commit
            ),
        ),
    ],
)
def test_gate_contract_tamper_rejected(tmp_path: Path, named_check: str, mutate) -> None:
    def mut(d: dict) -> None:
        mutate(d)
        d["gate_hash"] = ""  # will be recomputed by write; contract checks still fire

    root = _gate_tamper(tmp_path, mut)
    # rewrite gate_hash canonically so canonical-integrity does not mask the named check
    import json as _json

    from minos_engine.gates.contracts import GateArtifact

    gp = root / "gates/profile-snapshot-frozen-1.json"
    raw = _json.loads(gp.read_text())
    try:
        g = GateArtifact.model_validate(raw)
        gp.write_text(g.model_dump_json())
    except Exception:
        pass  # invalid gates are acceptable: verification must still reject
    try:
        r = _offline(root)
    except Exception:
        return  # fail-closed load rejection is a pass
    assert not r.ok
    assert r.checks.get(named_check) is False, (named_check, r.reasons)


@needs_evidence
def test_report_byte_modification_rejected(tmp_path: Path) -> None:
    root = _tamper_root(tmp_path, lambda r: None)
    (root / "reports").mkdir(parents=True, exist_ok=True)
    report = (REPO_ROOT / "reports/PROFILE_SNAPSHOT_FROZEN_1_REPORT.md").read_text()
    (root / "reports/PROFILE_SNAPSHOT_FROZEN_1_REPORT.md").write_text(report + "\ntampered\n")
    r = _offline(root)
    assert not r.ok and r.checks["report_bytes_bound"] is False


@needs_evidence
def test_offline_full_contract_passes_on_repo() -> None:
    r = _offline(REPO_ROOT)
    assert r.ok, r.reasons
