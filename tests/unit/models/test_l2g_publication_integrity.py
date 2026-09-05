"""Pre-execution publication integrity: exact authority, execution provenance, readback, staging.

The synthetic campaign here uses the REAL frozen (dataset_id, config_hash) identifiers so the
whole-tree verifier's dataset reconstruction applies, but every label and predictor is synthetic.
No real TRAIN label is consumed and no model is fitted on real data.
"""

from __future__ import annotations

import copy
import inspect
import json
from pathlib import Path
from typing import Any

import pytest

from minos_engine.models.campaign import (
    _MINT_TOKEN,
    STATUS_COMPLETE,
    TrustedL2GTrainCampaign,
    run_real_l2g_train_oof_campaign,
)
from minos_engine.models.campaign_evidence import (
    _EVIDENCE_MINT_TOKEN,
    OOF_WRAPPER_DOMAIN,
    OUTPUT_LAYOUT,
    CampaignEvidenceError,
    TrustedL2GPublishedEvidence,
    oof_wrapper_identity,
    verify_published_l2g_train_campaign,
    write_l2g_train_campaign_outputs,
)
from minos_engine.models.contract import SAFE_BASELINE_CONFIG_HASH
from minos_engine.models.oof_runner import oof_artifact_identity
from minos_engine.models.prefit_loader import load_verified_training_dataset
from minos_engine.models.shortlist import (
    ACCEPTED_PREFIT_AUTHORITY_SHA256,
    ShortlistError,
    build_campaign_result,
    verify_campaign_result,
    verify_prefit_authority_bytes,
)
from minos_engine.qualification.l2f_accepted_identities import repository_root
from minos_engine.qualification.provenance import GitProvenance, read_provenance


def _clean_provenance() -> GitProvenance:
    """The production path requires a clean worktree; this task's own tree is mid-edit."""
    real = read_provenance(repository_root())
    return GitProvenance(
        head_sha=real.head_sha,
        tree_sha=real.tree_sha,
        worktree_clean=True,
        parent_sha=real.parent_sha,
    )


@pytest.fixture(scope="module")
def trusted(trusted_l2g_campaign: TrustedL2GTrainCampaign) -> TrustedL2GTrainCampaign:
    return trusted_l2g_campaign


@pytest.fixture
def clean_source(monkeypatch: Any) -> GitProvenance:
    import minos_engine.models.campaign_evidence as module

    provenance = _clean_provenance()
    monkeypatch.setattr(module, "read_provenance", lambda root: provenance)
    return provenance


@pytest.fixture(scope="module")
def published(published_l2g_campaign: dict[str, Any]) -> dict[str, Any]:
    return published_l2g_campaign


@pytest.fixture(scope="module")
def result(published_l2g_result: dict[str, Any]) -> dict[str, Any]:
    return published_l2g_result


# ------------------------------------------------------------------------------------------ #
# DEFECT A -- exact prefit authority
# ------------------------------------------------------------------------------------------ #
def test_the_committed_prefit_authority_still_hashes_to_the_accepted_value() -> None:
    assert verify_prefit_authority_bytes() == ACCEPTED_PREFIT_AUTHORITY_SHA256
    assert ACCEPTED_PREFIT_AUTHORITY_SHA256 == (
        "61d8b33432202c1813a3d64d37bb727f8f1b8012ef1af23c7bf7af0ef8356000"
    )


def test_another_valid_hex_prefit_sha_is_refused(result: dict[str, Any]) -> None:
    """Well-formed is not the same as accepted."""
    tampered = copy.deepcopy(result)
    tampered["prefit_authority_sha256"] = "a" * 64
    with pytest.raises(ShortlistError, match="accepted\n?\\s*frozen authority|accepted"):
        verify_campaign_result(tampered)


def test_a_moved_authority_file_is_refused(tmp_path: Path) -> None:
    fake = tmp_path / "reports" / "layer2"
    fake.mkdir(parents=True)
    (fake / "l2g-prefit-authority.json").write_text("{}")
    with pytest.raises(ShortlistError, match="now hashes to"):
        verify_prefit_authority_bytes(tmp_path)


# ------------------------------------------------------------------------------------------ #
# DEFECT B -- execution provenance
# ------------------------------------------------------------------------------------------ #
def test_provenance_is_captured_before_the_first_fit() -> None:
    source = inspect.getsource(run_real_l2g_train_oof_campaign)
    capture = source.index("read_provenance(source_root)")
    first_fit = source.index("_run_l2g_train_oof_core(")
    assert capture < first_fit, "provenance is read after fitting"
    assert "worktree is dirty" in source


def test_the_campaign_carries_its_execution_source(trusted: TrustedL2GTrainCampaign) -> None:
    assert trusted.execution_source_commit == _clean_provenance().head_sha
    assert trusted.execution_source_tree == _clean_provenance().tree_sha


def test_the_result_names_the_execution_source_not_the_publication_checkout(
    result: dict[str, Any], trusted: TrustedL2GTrainCampaign
) -> None:
    assert result["source_commit"] == trusted.execution_source_commit
    assert result["source_tree"] == trusted.execution_source_tree
    source = inspect.getsource(build_campaign_result)
    assert "trusted.execution_source_commit" in source
    assert "read_provenance" not in source


def test_publication_refuses_when_the_checkout_moved_after_fitting(
    trusted: TrustedL2GTrainCampaign, tmp_path: Path, clean_source: GitProvenance
) -> None:
    drifted = TrustedL2GTrainCampaign(
        _MINT_TOKEN,
        closure=trusted.closure,
        records={h: trusted.records_for(h) for h in trusted.complete_spec_hashes()},
        metrics={h: trusted.metrics_for(h) for h in trusted.complete_spec_hashes()},
        failures={},
        execution_source_commit="0" * 40,
        execution_source_tree="0" * 40,
    )
    assert clean_source
    with pytest.raises(CampaignEvidenceError, match="was fitted at"):
        write_l2g_train_campaign_outputs(drifted, output_dir=tmp_path / "out")


def test_publication_refuses_a_dirty_worktree(
    trusted: TrustedL2GTrainCampaign, tmp_path: Path, monkeypatch: Any
) -> None:
    import minos_engine.models.campaign_evidence as module

    dirty = GitProvenance(
        head_sha=trusted.execution_source_commit,
        tree_sha=trusted.execution_source_tree,
        worktree_clean=False,
        parent_sha=None,
    )
    monkeypatch.setattr(module, "read_provenance", lambda root: dirty)
    with pytest.raises(CampaignEvidenceError, match="worktree is dirty"):
        write_l2g_train_campaign_outputs(trusted, output_dir=tmp_path / "out")


# ------------------------------------------------------------------------------------------ #
# DEFECT C -- published evidence is a capability
# ------------------------------------------------------------------------------------------ #
def test_a_caller_cannot_mint_published_evidence() -> None:
    with pytest.raises(CampaignEvidenceError, match="may only be minted"):
        TrustedL2GPublishedEvidence(object(), entries={}, output_dir="/tmp")


def test_the_result_builder_refuses_a_published_dictionary(
    trusted: TrustedL2GTrainCampaign,
) -> None:
    with pytest.raises(ShortlistError, match="not proof that anything reached disk"):
        build_campaign_result(trusted=trusted, published={"a": {"oof_file_sha256": "x"}})


def test_no_public_authoritative_path_accepts_a_published_dict() -> None:
    import minos_engine.models.campaign_evidence as module

    source = inspect.getsource(module)
    assert source.count("_EVIDENCE_MINT_TOKEN") == 3
    minting = [
        line
        for line in source.splitlines()
        if "TrustedL2GPublishedEvidence(" in line
        and "isinstance" not in line
        and "class " not in line
    ]
    assert len(minting) == 1, f"more than one mint site: {minting}"


def test_published_evidence_entries_are_copies(published: dict[str, Any]) -> None:
    assert _EVIDENCE_MINT_TOKEN is not None
    entries = published["manifest"]["published"]
    entries[next(iter(entries))]["oof_file_sha256"] = "tampered"
    fresh = json.loads(Path(published["manifest"]["campaign_result_path"]).read_bytes())
    assert all(e.get("oof_file_sha256") != "tampered" for e in fresh["per_spec"])


# ------------------------------------------------------------------------------------------ #
# DEFECT D -- one scientific OOF identity
# ------------------------------------------------------------------------------------------ #
def test_only_one_function_uses_the_frozen_oof_domain() -> None:
    root = repository_root() / "src/minos_engine"
    hits = [
        path.name
        for path in root.rglob("*.py")
        if "minos:l2g-oof-artifact:v1" in path.read_text(encoding="utf-8")
    ]
    assert hits == ["oof_runner.py"], f"the frozen OOF domain is used in {hits}"


def test_the_wrapper_has_its_own_domain() -> None:
    assert OOF_WRAPPER_DOMAIN == "minos:l2g-oof-evidence-wrapper:v1\n"
    assert OOF_WRAPPER_DOMAIN != "minos:l2g-oof-artifact:v1\n"


def test_the_wrapper_carries_the_frozen_scientific_identity(
    published: dict[str, Any], result: dict[str, Any]
) -> None:
    for entry in result["per_spec"]:
        if "oof_scientific_hash" not in entry:
            continue
        payload = json.loads((published["dir"] / "oof" / f"{entry['spec_hash']}.json").read_bytes())
        assert payload["scientific_oof_hash"] == entry["oof_scientific_hash"]
        assert oof_wrapper_identity(payload) != entry["oof_scientific_hash"]


def test_the_offline_verifier_recomputes_the_frozen_record_set_identity(
    published: dict[str, Any], result: dict[str, Any]
) -> None:
    class _Replay:
        def __init__(self, content: dict[str, Any]) -> None:
            self._c = content

        def content(self) -> dict[str, Any]:
            return self._c

    entry = next(e for e in result["per_spec"] if "oof_scientific_hash" in e)
    payload = json.loads((published["dir"] / "oof" / f"{entry['spec_hash']}.json").read_bytes())
    replayed = oof_artifact_identity([_Replay(dict(r)) for r in payload["records"]])
    assert replayed == entry["oof_scientific_hash"]


def test_core_to_published_identity_continuity(
    trusted: TrustedL2GTrainCampaign, result: dict[str, Any]
) -> None:
    """Execution -> trusted memory -> file -> reload must be one identity, not four lookalikes."""
    for entry in result["per_spec"]:
        if "oof_scientific_hash" not in entry:
            continue
        core = trusted.spec_entry(entry["spec_hash"])["oof_artifact_hash"]
        retained = oof_artifact_identity(trusted.records_for(entry["spec_hash"]))
        assert core == retained == entry["oof_scientific_hash"]


# ------------------------------------------------------------------------------------------ #
# §6 / §7 -- readback and staging
# ------------------------------------------------------------------------------------------ #
def test_the_file_sha_is_read_back_from_the_final_path() -> None:
    import minos_engine.models.campaign_evidence as module

    source = inspect.getsource(module._write_atomic)
    assert "path.read_bytes()" in source
    assert "hashlib.sha256(observed)" in source
    assert "hashlib.sha256(expected)" not in source


def test_publication_promotes_a_staging_tree_and_leaves_none_behind(
    published: dict[str, Any],
) -> None:
    out = published["dir"]
    assert out.is_dir()
    assert not list(out.parent.glob("*.tmp.*")), "a staging tree survived"
    assert (out / "campaign-result.json").is_file()
    assert len(list((out / "oof").glob("*.json"))) == 10


def test_an_existing_final_target_is_refused(
    trusted: TrustedL2GTrainCampaign, tmp_path: Path, clean_source: GitProvenance
) -> None:
    existing = tmp_path / OUTPUT_LAYOUT["root"]
    existing.mkdir()
    assert clean_source
    with pytest.raises(CampaignEvidenceError, match="refusing to overwrite"):
        write_l2g_train_campaign_outputs(trusted, output_dir=existing)


def test_a_partial_failure_leaves_no_staging_and_no_final(
    trusted: TrustedL2GTrainCampaign,
    tmp_path: Path,
    monkeypatch: Any,
    clean_source: GitProvenance,
) -> None:
    import minos_engine.models.campaign_evidence as module

    calls = {"n": 0}
    real = module._write_atomic

    def flaky(path: Path, payload: dict[str, Any]) -> dict[str, Any]:
        calls["n"] += 1
        if calls["n"] > 5:
            raise OSError("disk full")
        return real(path, payload)

    monkeypatch.setattr(module, "_write_atomic", flaky)
    out = tmp_path / OUTPUT_LAYOUT["root"]
    assert clean_source
    with pytest.raises(OSError, match="disk full"):
        write_l2g_train_campaign_outputs(trusted, output_dir=out)
    assert not out.exists()
    assert not list(tmp_path.glob("*.tmp.*"))


# ------------------------------------------------------------------------------------------ #
# §12 -- immutability through accessors
# ------------------------------------------------------------------------------------------ #
def test_mutating_a_returned_record_cannot_reach_the_trusted_state(
    trusted: TrustedL2GTrainCampaign,
) -> None:
    spec_hash = trusted.complete_spec_hashes()[0]
    before = oof_artifact_identity(trusted.records_for(spec_hash))
    borrowed = trusted.records_for(spec_hash)
    borrowed[0].content()["expected_utility_prediction"] = 999.0
    borrowed[0].content()["dataset_id"] = "tampered"
    assert oof_artifact_identity(trusted.records_for(spec_hash)) == before


def test_mutating_returned_metrics_or_entries_cannot_reach_the_trusted_state(
    trusted: TrustedL2GTrainCampaign,
) -> None:
    spec_hash = trusted.complete_spec_hashes()[0]
    metrics = trusted.metrics_for(spec_hash)
    metrics["mean_regret"] = -999.0
    assert trusted.metrics_for(spec_hash)["mean_regret"] != -999.0
    entry = trusted.spec_entry(spec_hash)
    entry["status"] = "TAMPERED"
    assert trusted.spec_entry(spec_hash)["status"] == STATUS_COMPLETE


# ------------------------------------------------------------------------------------------ #
# §13 -- the whole-tree offline verifier
# ------------------------------------------------------------------------------------------ #
def test_the_whole_tree_verifier_passes_on_a_published_campaign(published: dict[str, Any]) -> None:
    report = verify_published_l2g_train_campaign(published["dir"])
    assert report["ok"] is True
    assert report["complete_spec_count"] == 10


def test_the_whole_tree_verifier_needs_no_train_database() -> None:
    source = inspect.getsource(verify_published_l2g_train_campaign)
    for forbidden in ("psycopg", "create_engine", "sqlalchemy", "l2f2_baseline"):
        assert forbidden not in source


def test_a_foreign_oof_file_is_refused(published: dict[str, Any]) -> None:
    intruder = published["dir"] / "oof" / f"{'f' * 64}.json"
    intruder.write_text("{}")
    try:
        with pytest.raises(CampaignEvidenceError, match="not accounted for"):
            verify_published_l2g_train_campaign(published["dir"])
    finally:
        intruder.unlink()


def test_a_foreign_metric_file_is_refused(published: dict[str, Any]) -> None:
    intruder = published["dir"] / "metrics" / f"{'e' * 64}.json"
    intruder.write_text("{}")
    try:
        with pytest.raises(CampaignEvidenceError, match="not accounted for"):
            verify_published_l2g_train_campaign(published["dir"])
    finally:
        intruder.unlink()


def test_an_edited_oof_record_fails_the_whole_tree_verifier(
    published: dict[str, Any], result: dict[str, Any]
) -> None:
    entry = next(e for e in result["per_spec"] if "oof_scientific_hash" in e)
    path = published["dir"] / "oof" / f"{entry['spec_hash']}.json"
    original = path.read_bytes()
    payload = json.loads(original)
    payload["records"][0]["expected_utility_prediction"] = 0.5
    path.write_bytes(json.dumps(payload).encode("utf-8"))
    try:
        with pytest.raises(CampaignEvidenceError):
            verify_published_l2g_train_campaign(published["dir"])
    finally:
        path.write_bytes(original)


def test_an_edited_campaign_result_fails_the_whole_tree_verifier(published: dict[str, Any]) -> None:
    path = published["dir"] / "campaign-result.json"
    original = path.read_bytes()
    payload = json.loads(original)
    payload["shortlist"] = [payload["candidate_spec_hashes"][0]]
    payload["shortlist_empty"] = False
    path.write_bytes(json.dumps(payload).encode("utf-8"))
    try:
        with pytest.raises(ShortlistError, match="frozen two-bar rule"):
            verify_published_l2g_train_campaign(published["dir"])
    finally:
        path.write_bytes(original)


def test_the_result_reports_two_distinct_identities(published: dict[str, Any]) -> None:
    manifest = published["manifest"]
    assert manifest["campaign_result_identity"] != manifest["campaign_result_file_sha256"]
    assert len(manifest["campaign_result_identity"]) == 64
    assert len(manifest["campaign_result_file_sha256"]) == 64


# ------------------------------------------------------------------------------------------ #
# stage locks
# ------------------------------------------------------------------------------------------ #
def test_the_real_safe_baseline_is_observed_for_every_bam() -> None:
    """If it were not, CONSTANT_SAFE_BASELINE would hold the real campaign."""
    dataset = load_verified_training_dataset()
    bams = {r.dataset_id for r in dataset.rows}
    with_baseline = {
        r.dataset_id for r in dataset.rows if r.config_hash == SAFE_BASELINE_CONFIG_HASH
    }
    assert bams == with_baseline
    assert len(bams) == 50


def test_the_published_campaign_tree_is_the_authorized_v1_campaign() -> None:
    """Inverted, not deleted.

    This guard required the campaign tree to be ABSENT, which was correct while the real campaign
    was still forbidden. The authorized v1 campaign has since run, so the honest assertion is that
    whatever is there is that campaign and still verifies -- not that nothing is there.
    """
    from minos_engine.models.campaign_evidence import verify_published_l2g_train_campaign
    from tests.minos_scratch import CANONICAL_MINOS_ROOT

    root = CANONICAL_MINOS_ROOT / OUTPUT_LAYOUT["root"]
    if not root.is_dir():
        pytest.skip("the published campaign tree is not present on this machine")
    report = verify_published_l2g_train_campaign(root, repository_root=repository_root())
    assert report["ok"] is True
    assert report["complete_spec_count"] == 10
    assert report["shortlist_size"] == 0


def test_models_qualified_absent_and_select_config_blocked() -> None:
    from minos_engine.layer2.service import Layer2Service

    assert not (repository_root() / "gates/models-qualified.json").exists()
    with pytest.raises(Exception) as excinfo:
        Layer2Service().select_config(None)  # type: ignore[arg-type]
    assert "StageNotReady" in type(excinfo.value).__name__
