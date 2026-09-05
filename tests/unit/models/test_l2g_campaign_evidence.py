"""Campaign evidence: retained records, trusted publication, and offline re-verification.

A campaign that hashes its records and then discards them leaves a hash with nothing behind it.
These tests are about the other half of that: the evidence has to survive, reach disk, and be
re-derivable from the bytes by someone who trusts nobody.
"""

from __future__ import annotations

import copy
import inspect
import json
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from minos_engine.models.campaign import (
    _MINT_TOKEN,
    ACCEPTED_CANDIDATE_FAMILIES,
    ACCEPTED_CANDIDATE_SPEC_HASHES,
    ACCEPTED_REFERENCE_SPECS,
    REQUIRED_THREAD_POLICY,
    STATUS_COMPLETE,
    CampaignError,
    TrustedL2GTrainCampaign,
    _run_l2g_train_oof_core,
    run_real_l2g_train_oof_campaign,
)
from minos_engine.models.campaign_evidence import (
    METRIC_ARTIFACT_SCHEMA,
    OOF_ARTIFACT_SCHEMA,
    OUTPUT_LAYOUT,
    CampaignEvidenceError,
    load_and_verify_metric_artifact,
    load_and_verify_oof_artifact,
    oof_artifact_content,
    oof_artifact_identity,
    write_l2g_train_campaign_outputs,
)
from minos_engine.models.contract import CV_FOLD_CHROMOSOMES, SAFE_BASELINE_CONFIG_HASH
from minos_engine.models.design_matrix import DesignMatrix
from minos_engine.models.fit_driver import fit_fold_estimators, fit_reference_fold
from minos_engine.models.shortlist import (
    ACCEPTED_AUTHORITIES,
    ShortlistError,
    build_campaign_result,
    campaign_result_identity,
    verify_campaign_result,
    verify_campaign_result_source,
)

_PREFIT_SHA = "61d8b33432202c1813a3d64d37bb727f8f1b8012ef1af23c7bf7af0ef8356000"


# ------------------------------------------------------------------------------------------ #
# a synthetic campaign at the REAL shape: 1040 cells, 50 BAMs, 80 configs
# ------------------------------------------------------------------------------------------ #
def _pairs(bams: list[str], configs: list[str]) -> list[tuple[str, str]]:
    pairs: list[tuple[str, str]] = []
    for index, bam in enumerate(bams):
        # every BAM must observe the safe baseline or its reference has no measurable utility
        pairs.append((bam, SAFE_BASELINE_CONFIG_HASH))
        seen = {SAFE_BASELINE_CONFIG_HASH}
        offset = 0
        while len(seen) < 20:
            config = configs[(index * 7 + offset) % 80]
            if config not in seen:
                seen.add(config)
                pairs.append((bam, config))
            offset += 1
    added, step = 0, 0
    while added < 40:
        bam, config = bams[added], configs[(added * 13 + step) % 80]
        if (bam, config) not in pairs:
            pairs.append((bam, config))
            added, step = added + 1, 0
        else:
            step += 1
    assert len(pairs) == 1040 and len(set(pairs)) == 1040 and len({p[0] for p in pairs}) == 50
    return pairs


def _trusted_campaign() -> TrustedL2GTrainCampaign:
    rng = np.random.default_rng(5)
    bams = {f"bam-{c}-{i}": c for c in CV_FOLD_CHROMOSOMES for i in range(10)}
    configs = [SAFE_BASELINE_CONFIG_HASH, *[f"{i:064x}" for i in range(79)]]
    pairs = _pairs(sorted(bams), configs)

    class _Row:
        def __init__(self, bam: str, config: str, admitted: bool) -> None:
            self.dataset_id, self.config_hash = bam, config
            self.chromosome = bams[bam]
            self.admission_label = 1 if admitted else 0
            self.outcome = "ADMITTED" if admitted else "CANDIDATE_NON_ADMISSION"
            self.admitted_score = float(np.clip(rng.normal(0.7, 0.1), 0, 1)) if admitted else None

        def identity(self) -> str:
            return f"{self.dataset_id}|{self.config_hash}"

    rows = [_Row(b, c, bool(rng.random() > 0.25)) for b, c in pairs]
    meta = tuple(
        {
            "dataset_id": r.dataset_id,
            "chromosome": r.chromosome,
            "config_hash": r.config_hash,
            "outcome": r.outcome,
            "admitted_score": r.admitted_score,
            "admission_label": r.admission_label,
            "identity": r.identity(),
        }
        for r in rows
    )
    design = DesignMatrix(
        x_bam=rng.normal(size=(len(rows), 129)),
        x_config=rng.normal(size=(len(rows), 28)),
        bam_columns=tuple(f"b{i}" for i in range(129)),
        config_columns=tuple(f"c{i}" for i in range(28)),
        meta=meta,
    )

    class _CV:
        bam_chromosome = bams

        def identity(self) -> str:
            return ACCEPTED_AUTHORITIES["cv_manifest_hash"]

    class _DS:
        cv_manifest = _CV()

        def __init__(self) -> None:
            self.rows = tuple(rows)

        def admission_weights(self) -> dict[str, float]:
            per: dict[str, int] = {}
            for r in rows:
                per[r.dataset_id] = per.get(r.dataset_id, 0) + 1
            return {r.identity(): 1 / per[r.dataset_id] for r in rows}

        def score_weights(self) -> dict[str, float]:
            admitted = [r for r in rows if r.admitted_score is not None]
            per: dict[str, int] = {}
            for r in admitted:
                per[r.dataset_id] = per.get(r.dataset_id, 0) + 1
            return {r.identity(): 1 / per[r.dataset_id] for r in admitted}

    def _spec(family: str, spec_hash: str) -> Any:
        class _S:
            def __init__(self) -> None:
                self.family = family
                self.score_model_implementation = "sklearn.linear_model.Ridge"
                self.admission_model_implementation = "sklearn.linear_model.LogisticRegression"
                self.score_hyperparameters = {"alpha": 1.0}
                self.admission_hyperparameters = {"C": 1.0, "max_iter": 1000}
                self.random_seed = 20260904

            def identity(self) -> str:
                return spec_hash

        return _S()

    candidates = tuple(
        _spec(f, h)
        for f, h in zip(ACCEPTED_CANDIDATE_FAMILIES, ACCEPTED_CANDIDATE_SPEC_HASHES, strict=True)
    )
    references = tuple(_spec(f, h) for f, h in ACCEPTED_REFERENCE_SPECS)

    closure = _run_l2g_train_oof_core(
        dataset=_DS(),
        design=design,
        candidate_specs=candidates,
        reference_specs=references,
        fit_estimators=fit_fold_estimators,
        fit_reference=fit_reference_fold,
        thread_report=(
            {
                "user_api": "blas",
                "internal_api": "openblas",
                "num_threads": 1,
                "prefix": "libscipy_openblas",
            },
        ),
    )
    records = closure.pop("_records")
    failures = closure.pop("_failures")
    closure["authority"] = {**ACCEPTED_AUTHORITIES, "prefit_authority_sha256": _PREFIT_SHA}
    closure["thread_policy"] = REQUIRED_THREAD_POLICY
    closure["candidate_spec_hashes"] = list(ACCEPTED_CANDIDATE_SPEC_HASHES)
    closure["reference_spec_hashes"] = [h for _, h in ACCEPTED_REFERENCE_SPECS]
    metrics = {
        h: e["metrics"] for h, e in closure["per_spec"].items() if e["status"] == STATUS_COMPLETE
    }
    return TrustedL2GTrainCampaign(
        _MINT_TOKEN, closure=closure, records=records, metrics=metrics, failures=failures
    )


@pytest.fixture(scope="module")
def trusted() -> TrustedL2GTrainCampaign:
    return _trusted_campaign()


@pytest.fixture(scope="module")
def published(
    trusted: TrustedL2GTrainCampaign, tmp_path_factory: pytest.TempPathFactory
) -> dict[str, Any]:
    out = tmp_path_factory.mktemp("campaign") / OUTPUT_LAYOUT["root"]
    manifest = write_l2g_train_campaign_outputs(trusted, output_dir=out)
    return {"manifest": manifest, "dir": out}


@pytest.fixture(scope="module")
def result(published: dict[str, Any]) -> dict[str, Any]:
    path = Path(published["manifest"]["campaign_result_path"])
    return dict(json.loads(path.read_bytes()))


# ------------------------------------------------------------------------------------------ #
# §2 / §3 -- retained evidence and the trusted capability
# ------------------------------------------------------------------------------------------ #
def test_the_campaign_retains_the_actual_records_for_every_complete_spec(
    trusted: TrustedL2GTrainCampaign,
) -> None:
    complete = trusted.complete_spec_hashes()
    assert len(complete) == 10
    for spec_hash in complete:
        records = trusted.records_for(spec_hash)
        assert len(records) == 1040
        assert {r.model_spec_hash for r in records} == {spec_hash}


def test_a_caller_cannot_mint_a_trusted_campaign() -> None:
    with pytest.raises(CampaignError, match="may only be minted"):
        TrustedL2GTrainCampaign(object(), closure={}, records={}, metrics={}, failures={})
    with pytest.raises(CampaignError, match="may only be minted"):
        TrustedL2GTrainCampaign(None, closure={}, records={}, metrics={}, failures={})


def test_no_helper_converts_a_dict_into_a_trusted_campaign() -> None:
    import minos_engine.models.campaign as module

    source = inspect.getsource(module)
    assert source.count("_MINT_TOKEN") == 3, "the mint token is used in more places than expected"
    minting = [
        line
        for line in source.splitlines()
        if "TrustedL2GTrainCampaign(" in line and "isinstance" not in line and "class " not in line
    ]
    assert len(minting) == 1, f"more than one construction site: {minting}"


def test_the_trusted_closure_is_a_defensive_copy(trusted: TrustedL2GTrainCampaign) -> None:
    first = trusted.closure
    first["shortlist"] = ["tampered"]
    assert trusted.closure["shortlist"] != ["tampered"]


def test_the_production_entry_returns_a_trusted_campaign() -> None:
    signature = inspect.signature(run_real_l2g_train_oof_campaign)
    assert signature.return_annotation == "TrustedL2GTrainCampaign"


def test_incomplete_specs_expose_failure_evidence_not_records() -> None:
    campaign = _trusted_campaign()
    missing = "f" * 64
    with pytest.raises(CampaignError, match="no OOF evidence"):
        campaign.records_for(missing)
    with pytest.raises(CampaignError, match="no metrics"):
        campaign.metrics_for(missing)
    assert campaign.failures_for(missing) == []


# ------------------------------------------------------------------------------------------ #
# §11 -- the builder requires trust
# ------------------------------------------------------------------------------------------ #
def test_build_campaign_result_refuses_a_plain_dictionary() -> None:
    with pytest.raises(ShortlistError, match="whatever its author typed"):
        build_campaign_result(trusted={"per_spec": {}}, published={})


def test_the_publisher_refuses_a_plain_dictionary(tmp_path: Path) -> None:
    with pytest.raises(CampaignEvidenceError, match="only a trusted campaign"):
        write_l2g_train_campaign_outputs({"per_spec": {}}, output_dir=tmp_path)


def test_the_public_builder_takes_no_scientific_argument() -> None:
    parameters = set(inspect.signature(build_campaign_result).parameters)
    assert parameters == {"trusted", "published", "root"}
    assert not (parameters & {"shortlist", "metrics", "per_spec", "source_commit"})


def test_the_low_level_serializer_is_private_and_non_authoritative() -> None:
    import minos_engine.models.shortlist as module

    assert not hasattr(module, "train_campaign_result_content")
    assert "NON-AUTHORITATIVE" in module._train_campaign_result_content.__doc__


# ------------------------------------------------------------------------------------------ #
# §5-§7, §9, §21 -- publication
# ------------------------------------------------------------------------------------------ #
def test_the_output_layout_is_frozen() -> None:
    assert OUTPUT_LAYOUT["root"] == "minos_l2g_train_oof"
    assert OUTPUT_LAYOUT["campaign_result"] == "campaign-result.json"
    assert OUTPUT_LAYOUT["dir_mode"] == "0o750"
    assert OUTPUT_LAYOUT["file_mode"] == "0o640"


def test_publication_writes_one_oof_and_one_metric_artifact_per_complete_spec(
    published: dict[str, Any],
) -> None:
    out = published["dir"]
    assert len(list((out / "oof").glob("*.json"))) == 10
    assert len(list((out / "metrics").glob("*.json"))) == 10
    assert (out / "campaign-result.json").is_file()


def test_published_files_carry_the_frozen_modes(published: dict[str, Any]) -> None:
    out = published["dir"]
    assert oct(out.stat().st_mode & 0o777) == "0o750"
    for path in (out / "oof").glob("*.json"):
        assert oct(path.stat().st_mode & 0o777) == "0o640"


def test_the_recorded_file_hashes_are_the_hashes_of_the_written_bytes(
    published: dict[str, Any], result: dict[str, Any]
) -> None:
    import hashlib

    out = published["dir"]
    for entry in result["per_spec"]:
        if "oof_file_sha256" not in entry:
            continue
        data = (out / "oof" / f"{entry['spec_hash']}.json").read_bytes()
        assert hashlib.sha256(data).hexdigest() == entry["oof_file_sha256"]
        assert len(data) == entry["oof_size_bytes"]


def test_the_scientific_identity_and_the_file_sha_are_distinct(result: dict[str, Any]) -> None:
    for entry in result["per_spec"]:
        if "oof_scientific_hash" in entry:
            assert entry["oof_scientific_hash"] != entry["oof_file_sha256"]


def test_the_oof_artifact_identity_is_row_order_independent(
    trusted: TrustedL2GTrainCampaign,
) -> None:
    spec_hash = trusted.complete_spec_hashes()[0]
    entry = trusted.spec_entry(spec_hash)
    records = trusted.records_for(spec_hash)
    cells = frozenset((r.dataset_id, r.config_hash) for r in records)
    kwargs = {
        "spec_hash": spec_hash,
        "family": entry["family"],
        "training_dataset_hash": ACCEPTED_AUTHORITIES["training_dataset_hash"],
        "cv_manifest_hash": ACCEPTED_AUTHORITIES["cv_manifest_hash"],
        "expected_cell_set": cells,
    }
    forward = oof_artifact_content(records=records, **kwargs)
    backward = oof_artifact_content(records=list(reversed(records)), **kwargs)
    assert oof_artifact_identity(forward) == oof_artifact_identity(backward)


def test_publication_cleans_up_on_failure(
    trusted: TrustedL2GTrainCampaign, tmp_path: Path, monkeypatch: Any
) -> None:
    """A half-published campaign is worse than none: it looks like evidence."""
    import minos_engine.models.campaign_evidence as module

    calls = {"n": 0}
    real = module._write_atomic

    def flaky(path: Path, payload: dict[str, Any]) -> dict[str, Any]:
        calls["n"] += 1
        if calls["n"] > 4:
            raise OSError("disk full")
        return real(path, payload)

    monkeypatch.setattr(module, "_write_atomic", flaky)
    out = tmp_path / "campaign"
    with pytest.raises(OSError, match="disk full"):
        write_l2g_train_campaign_outputs(trusted, output_dir=out)
    assert not list(out.rglob("*.json")), "a partial publication was left behind"


# ------------------------------------------------------------------------------------------ #
# §14 / §15 -- offline artifact verification
# ------------------------------------------------------------------------------------------ #
def _complete_entries(result: dict[str, Any]) -> list[dict[str, Any]]:
    return [e for e in result["per_spec"] if "oof_scientific_hash" in e]


def _cells(trusted: TrustedL2GTrainCampaign) -> frozenset[tuple[str, str]]:
    return frozenset((a, b) for a, b in trusted.closure["expected_cell_set"])


def test_every_published_oof_artifact_verifies_from_its_bytes(
    published: dict[str, Any], result: dict[str, Any], trusted: TrustedL2GTrainCampaign
) -> None:
    out = published["dir"]
    for entry in _complete_entries(result):
        payload = load_and_verify_oof_artifact(
            out / "oof" / f"{entry['spec_hash']}.json",
            spec_hash=entry["spec_hash"],
            family=entry["family"],
            expected_file_sha256=entry["oof_file_sha256"],
            expected_scientific_hash=entry["oof_scientific_hash"],
            expected_cell_set=_cells(trusted),
            training_dataset_hash=result["training_dataset_hash"],
            cv_manifest_hash=result["cv_manifest_hash"],
        )
        assert payload["schema_version"] == OOF_ARTIFACT_SCHEMA
        assert payload["record_count"] == 1040
        assert payload["bam_count"] == 50


def test_every_published_metric_artifact_verifies_from_its_bytes(
    published: dict[str, Any], result: dict[str, Any]
) -> None:
    out = published["dir"]
    for entry in _complete_entries(result):
        payload = load_and_verify_metric_artifact(
            out / "metrics" / f"{entry['spec_hash']}.json",
            spec_hash=entry["spec_hash"],
            family=entry["family"],
            expected_file_sha256=entry["metric_file_sha256"],
            expected_scientific_hash=entry["metric_scientific_hash"],
            expected_promotion_metrics=entry["promotion_metrics"],
            training_dataset_hash=result["training_dataset_hash"],
        )
        assert payload["schema_version"] == METRIC_ARTIFACT_SCHEMA


def test_an_oof_file_swapped_between_specs_is_refused(
    published: dict[str, Any], result: dict[str, Any], trusted: TrustedL2GTrainCampaign
) -> None:
    out = published["dir"]
    first, second = _complete_entries(result)[:2]
    with pytest.raises(CampaignEvidenceError):
        load_and_verify_oof_artifact(
            out / "oof" / f"{second['spec_hash']}.json",
            spec_hash=first["spec_hash"],
            family=first["family"],
            expected_file_sha256=first["oof_file_sha256"],
            expected_scientific_hash=first["oof_scientific_hash"],
            expected_cell_set=_cells(trusted),
            training_dataset_hash=result["training_dataset_hash"],
            cv_manifest_hash=result["cv_manifest_hash"],
        )


def test_a_metric_file_swapped_between_specs_is_refused(
    published: dict[str, Any], result: dict[str, Any]
) -> None:
    out = published["dir"]
    first, second = _complete_entries(result)[:2]
    with pytest.raises(CampaignEvidenceError):
        load_and_verify_metric_artifact(
            out / "metrics" / f"{second['spec_hash']}.json",
            spec_hash=first["spec_hash"],
            family=first["family"],
            expected_file_sha256=first["metric_file_sha256"],
            expected_scientific_hash=first["metric_scientific_hash"],
            expected_promotion_metrics=first["promotion_metrics"],
            training_dataset_hash=result["training_dataset_hash"],
        )


def test_an_edited_oof_record_is_refused(
    published: dict[str, Any],
    result: dict[str, Any],
    trusted: TrustedL2GTrainCampaign,
    tmp_path: Path,
) -> None:
    entry = _complete_entries(result)[0]
    source = published["dir"] / "oof" / f"{entry['spec_hash']}.json"
    payload = json.loads(source.read_bytes())
    payload["records"][0]["expected_utility_prediction"] = 0.999999
    target = tmp_path / "edited.json"
    target.write_bytes(json.dumps(payload).encode("utf-8"))
    with pytest.raises(CampaignEvidenceError, match="hashes to"):
        load_and_verify_oof_artifact(
            target,
            spec_hash=entry["spec_hash"],
            family=entry["family"],
            expected_file_sha256=entry["oof_file_sha256"],
            expected_scientific_hash=entry["oof_scientific_hash"],
            expected_cell_set=_cells(trusted),
            training_dataset_hash=result["training_dataset_hash"],
            cv_manifest_hash=result["cv_manifest_hash"],
        )


def test_an_edited_metric_value_is_refused(
    published: dict[str, Any], result: dict[str, Any], tmp_path: Path
) -> None:
    entry = _complete_entries(result)[0]
    payload = json.loads((published["dir"] / "metrics" / f"{entry['spec_hash']}.json").read_bytes())
    payload["metrics"]["mean_regret"] = -1.0
    target = tmp_path / "edited.json"
    data = json.dumps(payload).encode("utf-8")
    target.write_bytes(data)
    import hashlib

    with pytest.raises(CampaignEvidenceError):
        load_and_verify_metric_artifact(
            target,
            spec_hash=entry["spec_hash"],
            family=entry["family"],
            expected_file_sha256=hashlib.sha256(data).hexdigest(),
            expected_scientific_hash=entry["metric_scientific_hash"],
            expected_promotion_metrics=entry["promotion_metrics"],
            training_dataset_hash=result["training_dataset_hash"],
        )


def test_a_symlinked_artifact_is_refused(
    published: dict[str, Any],
    result: dict[str, Any],
    tmp_path: Path,
    trusted: TrustedL2GTrainCampaign,
) -> None:
    entry = _complete_entries(result)[0]
    link = tmp_path / "link.json"
    link.symlink_to(published["dir"] / "oof" / f"{entry['spec_hash']}.json")
    with pytest.raises(CampaignEvidenceError, match="symlink"):
        load_and_verify_oof_artifact(
            link,
            spec_hash=entry["spec_hash"],
            family=entry["family"],
            expected_file_sha256=entry["oof_file_sha256"],
            expected_scientific_hash=entry["oof_scientific_hash"],
            expected_cell_set=_cells(trusted),
            training_dataset_hash=result["training_dataset_hash"],
            cv_manifest_hash=result["cv_manifest_hash"],
        )


# ------------------------------------------------------------------------------------------ #
# §8 / §12 / §16 / §17 -- the result verifies itself
# ------------------------------------------------------------------------------------------ #
def test_the_result_verifies_and_binds_promotion_metrics(result: dict[str, Any]) -> None:
    assert verify_campaign_result(result)["ok"] is True
    for entry in _complete_entries(result):
        assert set(entry["promotion_metrics"]) == {"mean_regret", "cvar_regret"}


def test_the_source_commit_and_tree_are_verified_against_git(result: dict[str, Any]) -> None:
    assert verify_campaign_result_source(result)["ok"] is True


def test_a_source_tree_that_is_not_the_commits_tree_is_refused(result: dict[str, Any]) -> None:
    tampered = copy.deepcopy(result)
    tampered["source_tree"] = "0" * 40
    with pytest.raises(ShortlistError, match="has tree"):
        verify_campaign_result_source(tampered)


def test_a_source_commit_git_does_not_have_is_refused(result: dict[str, Any]) -> None:
    tampered = copy.deepcopy(result)
    tampered["source_commit"] = "0" * 40
    with pytest.raises(ShortlistError, match="not a commit"):
        verify_campaign_result_source(tampered)


@pytest.mark.parametrize("field", sorted(ACCEPTED_AUTHORITIES))
def test_a_foreign_authority_identity_is_refused(result: dict[str, Any], field: str) -> None:
    """A well-formed 64-hex string is not an authority."""
    tampered = copy.deepcopy(result)
    tampered[field] = "f" * 64
    with pytest.raises(ShortlistError, match="accepted authority"):
        verify_campaign_result(tampered)


def test_a_foreign_model_spec_hash_is_refused(result: dict[str, Any]) -> None:
    tampered = copy.deepcopy(result)
    tampered["candidate_spec_hashes"] = ["f" * 64, *tampered["candidate_spec_hashes"][1:]]
    with pytest.raises(ShortlistError, match="not the accepted frozen six"):
        verify_campaign_result(tampered)


def test_a_reference_spec_hash_swap_is_refused(result: dict[str, Any]) -> None:
    tampered = copy.deepcopy(result)
    tampered["reference_spec_hashes"] = ["e" * 64, *tampered["reference_spec_hashes"][1:]]
    with pytest.raises(ShortlistError, match="not the accepted frozen four"):
        verify_campaign_result(tampered)


def test_a_shortlist_edit_is_refused_even_though_the_identity_recomputes(
    result: dict[str, Any],
) -> None:
    """Recording a shortlist and asserting it was derived correctly proves nothing."""
    tampered = copy.deepcopy(result)
    tampered["shortlist"] = [tampered["candidate_spec_hashes"][0]]
    tampered["shortlist_empty"] = False
    assert campaign_result_identity(tampered) != campaign_result_identity(result)
    with pytest.raises(ShortlistError, match="not what the frozen two-bar rule"):
        verify_campaign_result(tampered)


def test_an_edited_reference_threshold_is_refused(result: dict[str, Any]) -> None:
    tampered = copy.deepcopy(result)
    tampered["best_reference_mean_regret"] = 0.0
    with pytest.raises(ShortlistError, match="best_reference_mean_regret"):
        verify_campaign_result(tampered)


def test_an_edited_promotion_metric_is_refused(result: dict[str, Any]) -> None:
    tampered = copy.deepcopy(result)
    entry = next(e for e in tampered["per_spec"] if "promotion_metrics" in e)
    entry["promotion_metrics"]["mean_regret"] = -1.0
    with pytest.raises(ShortlistError):
        verify_campaign_result(tampered)


def test_the_shortlist_is_rederived_not_trusted() -> None:
    source = inspect.getsource(verify_campaign_result)
    assert "rederived" in source
    assert "best_mean = min(" in source and "best_cvar = min(" in source


def test_a_thread_report_recording_multiple_threads_is_refused(result: dict[str, Any]) -> None:
    tampered = copy.deepcopy(result)
    tampered["thread_report"] = [{"user_api": "blas", "num_threads": 16}]
    with pytest.raises(ShortlistError, match="16 threads"):
        verify_campaign_result(tampered)


def test_an_empty_thread_report_is_refused(result: dict[str, Any]) -> None:
    tampered = copy.deepcopy(result)
    tampered["thread_report"] = []
    with pytest.raises(ShortlistError, match="thread report is empty"):
        verify_campaign_result(tampered)


# ------------------------------------------------------------------------------------------ #
# stage locks
# ------------------------------------------------------------------------------------------ #
def test_no_real_campaign_output_exists() -> None:
    from tests.minos_scratch import CANONICAL_MINOS_ROOT

    assert not (CANONICAL_MINOS_ROOT / OUTPUT_LAYOUT["root"]).exists()
    workspace = CANONICAL_MINOS_ROOT / "minos_l2g_training"
    for forbidden in ("oof_predictions.json", "campaign-result.json", "metrics.json"):
        assert not (workspace / forbidden).exists()


def test_models_qualified_absent_and_select_config_blocked() -> None:
    from minos_engine.layer2.service import Layer2Service

    root = Path(__file__).resolve().parents[3]
    assert not (root / "gates/models-qualified.json").exists()
    with pytest.raises(Exception) as excinfo:
        Layer2Service().select_config(None)  # type: ignore[arg-type]
    assert "StageNotReady" in type(excinfo.value).__name__
