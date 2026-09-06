"""The REAL producer -> publisher boundary, with nothing injected in between.

The earlier fixtures built ``per_spec`` by hand, which quietly supplied ``family`` and
``diagnostics`` that the real runner never produced. Publication would therefore have raised
``KeyError: 'family'`` on the first real campaign, after the expensive part had already run. Every
test here takes whatever ``run_relative_outer_oof`` actually returns and passes it straight
through.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from minos_engine.models.relative_finalist_authority import (
    ACCEPTED_FEATURE_SET_HASH,
    ACCEPTED_RELATIVE_CONTRACT_HASH,
    ACCEPTED_RELATIVE_DATASET_IDENTITY,
    ACCEPTED_V2_PREFIT_AUTHORITY_SHA256,
    RelativeAuthorityError,
    build_trusted_final_train_bundle,
    verify_v2_prefit_authority,
)
from minos_engine.models.relative_finalist_contract import (
    FINALIST_DOMAIN,
    SAFE_BASELINE_CONFIG_HASH,
)
from minos_engine.models.relative_finalist_dataset import (
    expected_bam_chromosome_set_hash,
    expected_relative_cell_set_hash,
)
from minos_engine.models.relative_finalist_evidence import (
    _CAMPAIGN_TOKEN,
    _VERIFIED_TOKEN,
    V2_OUTPUT_LAYOUT,
    V2EvidenceError,
    VerifiedPublishedL2GV2TrainCampaign,
    assess_v2_completeness,
    load_verified_published_l2g_v2_campaign,
    mint_trusted_v2_campaign,
    verify_published_l2g_v2_train_campaign,
)
from minos_engine.models.relative_finalist_protocol import (
    V2_CANDIDATE_GRID,
    build_v2_spec_hashes,
    compute_relative_protocol_hash,
)
from minos_engine.models.relative_finalist_reconstruction import (
    build_frozen_scientific_reference,
)
from minos_engine.models.relative_finalist_runner import (
    delta_diagnostics,
)
from minos_engine.qualification.l2f_accepted_identities import repository_root
from tests.unit.models.conftest import v2_campaign_authority, v2_reference_bundle


# ---------------------------------------------------------------------------------------- #
# DEFECT: the producer did not supply what the publisher requires
# ---------------------------------------------------------------------------------------- #
def test_the_real_runner_supplies_family_and_implementation(v2_produced: dict[str, Any]) -> None:
    """This assertion fails on 019b3792: run_relative_outer_oof emitted no family."""
    hashes = build_v2_spec_hashes(ACCEPTED_RELATIVE_DATASET_IDENTITY)
    for recipe, spec_hash in zip(V2_CANDIDATE_GRID, hashes, strict=True):
        entry = v2_produced[spec_hash]
        assert entry["family"] == recipe["family"]
        assert entry["implementation"] == recipe["implementation"]
        assert entry["spec_hash"] == spec_hash


def test_the_real_runner_supplies_non_empty_diagnostics(v2_produced: dict[str, Any]) -> None:
    for entry in v2_produced.values():
        diagnostics = entry["diagnostics"]
        assert diagnostics, "a COMPLETE spec published empty diagnostics"
        for name in ("delta_mae", "delta_rmse", "delta_r2", "delta_spearman"):
            assert name in diagnostics
        assert math.isfinite(diagnostics["delta_mae"])
        assert math.isfinite(diagnostics["delta_rmse"])


def test_the_diagnostics_are_computed_from_the_exact_150_records(
    v2_produced: dict[str, Any],
) -> None:
    entry = next(iter(v2_produced.values()))
    assert len(entry["records"]) == 150
    actual = np.asarray([r["actual_delta"] for r in entry["records"]])
    predicted = np.asarray([r["predicted_delta"] for r in entry["records"]])
    diagnostics = entry["diagnostics"]
    assert diagnostics["delta_mae"] == pytest.approx(float(np.mean(np.abs(predicted - actual))))
    assert diagnostics["delta_rmse"] == pytest.approx(
        float(np.sqrt(np.mean((predicted - actual) ** 2)))
    )


def test_the_published_actual_deltas_are_the_frozen_labels(v2_produced: dict[str, Any]) -> None:
    """The producer's labels come from the frozen dataset, not from anything this test invented."""
    frozen = build_frozen_scientific_reference().delta
    for entry in v2_produced.values():
        for record in entry["records"]:
            cell = (record["dataset_id"], record["alternative_config"])
            assert record["actual_delta"] == frozen[cell]


def test_diagnostics_never_change_the_shortlist(v2_produced: dict[str, Any]) -> None:
    """A model that predicts advantage beautifully and switches badly is still not qualified."""
    import inspect

    from minos_engine.models import relative_finalist_evidence as module

    source = inspect.getsource(module.verify_v2_campaign_result)
    assert "diagnostics" not in source.split("rederived")[0].split("qualifies_against_bar")[0]


# ---------------------------------------------------------------------------------------- #
# real producer -> trusted -> publish -> verify
# ---------------------------------------------------------------------------------------- #
def test_the_real_produced_campaign_publishes_and_verifies(v2_published: dict[str, Any]) -> None:
    """End to end from the real runner's own output. No hand-built per_spec."""
    report = verify_published_l2g_v2_train_campaign(v2_published["dir"])
    assert report["ok"] is True
    assert report["complete_spec_count"] == 4


def test_every_published_spec_is_complete_under_the_real_producer(
    v2_produced: dict[str, Any], v2_world: dict[str, Any]
) -> None:
    for entry in copy.deepcopy(v2_produced).values():
        report = assess_v2_completeness(entry)
        assert report["status"] == "COMPLETE", report["reasons"]
        assert report["observed_oof_record_count"] == 150
        assert report["observed_decision_count"] == 50
        assert report["observed_margin_count"] == 5
        assert report["exact_cell_set_verified"] is True


def test_publication_refuses_a_producer_result_missing_family(
    v2_produced: dict[str, Any], v2_world: dict[str, Any], tmp_path: Path
) -> None:
    """The exact failure mode 019b3792 would have hit on the real campaign."""
    from minos_engine.models.relative_finalist_evidence import build_v2_campaign_result

    per_spec = copy.deepcopy(v2_produced)
    for entry in per_spec.values():
        entry.pop("family")
    broken = mint_trusted_v2_campaign(
        _CAMPAIGN_TOKEN,
        authority=v2_campaign_authority(),
        per_spec=per_spec,
        references=v2_reference_bundle(v2_world),
        shortlist=(),
    )
    with pytest.raises(KeyError):
        build_v2_campaign_result(trusted=broken, published={})


# ---------------------------------------------------------------------------------------- #
# finiteness
# ---------------------------------------------------------------------------------------- #
@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_a_non_finite_prediction_or_margin_is_refused(
    v2_produced: dict[str, Any], v2_world: dict[str, Any], bad: float
) -> None:
    per_spec = copy.deepcopy(v2_produced)
    entry = copy.deepcopy(next(iter(per_spec.values())))
    entry["margins"]["chr18"] = bad
    assert assess_v2_completeness(entry)["status"] == "TRAINING_FAILURE"


def test_an_undefined_diagnostic_is_null_and_still_publishable() -> None:
    """A constant predictor has no rank correlation.

    ``None`` says that. 0.0 would claim a measurement, and NaN would be refused by the canonical
    encoder -- after the fitting -- which is exactly the failure class this task exists to close.
    """
    from minos_engine.common.canonical_json import canonical_json_str

    class _R:
        def __init__(self, a: float, p: float) -> None:
            self.actual_delta, self.predicted_delta = a, p

    out = delta_diagnostics([_R(0.1, 0.5), _R(0.2, 0.5), _R(0.3, 0.5)])
    assert out["delta_spearman"] is None
    assert math.isfinite(out["delta_mae"]) and math.isfinite(out["delta_rmse"])
    canonical_json_str(out)  # a NaN here would raise, and would raise at publication too


def test_an_edited_diagnostic_fails_because_it_is_recomputed(
    v2_published: dict[str, Any], tmp_path: Path
) -> None:
    """Diagnostics are recomputed from the 150 records, so neither a number nor a null survives.

    ``null`` is the honest report for an undefined statistic, but only where the recomputation is
    actually undefined; claiming undefined for a statistic that has a value is as false as
    claiming the wrong value.
    """
    from minos_engine.models.relative_finalist_evidence import v2_metric_artifact_identity

    def _edit(value: Any) -> Any:
        def mutate(target: Path, result: dict[str, Any]) -> None:
            entry = next(e for e in result["per_spec"] if e["status"] == "COMPLETE")
            path = target / "metrics" / f"{entry['spec_hash']}.json"
            metric = json.loads(path.read_bytes())
            metric["diagnostics"]["delta_spearman"] = value
            data = json.dumps(metric, sort_keys=True).encode("utf-8")
            path.write_bytes(data)
            entry["metric_file_sha256"] = hashlib.sha256(data).hexdigest()
            entry["metric_scientific_hash"] = v2_metric_artifact_identity(metric)

        return mutate

    with pytest.raises(V2EvidenceError, match="recomputes to"):
        verify_published_l2g_v2_train_campaign(
            _republish(v2_published, tmp_path / "wrong", _edit(0.5))
        )
    with pytest.raises(V2EvidenceError, match="recomputation is undefined|recomputes to"):
        verify_published_l2g_v2_train_campaign(
            _republish(v2_published, tmp_path / "null", _edit(None))
        )


def test_the_diagnostics_helper_refuses_non_finite_input() -> None:
    from minos_engine.models.relative_finalist_runner import RelativeRunnerError

    class _R:
        def __init__(self, a: float, p: float) -> None:
            self.actual_delta, self.predicted_delta = a, p

    with pytest.raises(RelativeRunnerError, match="not finite"):
        delta_diagnostics([_R(0.1, float("inf"))])


# ---------------------------------------------------------------------------------------- #
# the authority gate
# ---------------------------------------------------------------------------------------- #
def test_the_committed_authority_is_v4_and_binds_the_expected_sets() -> None:
    assert verify_v2_prefit_authority(repository_root()) == ACCEPTED_V2_PREFIT_AUTHORITY_SHA256
    document = json.loads(
        (repository_root() / "reports/layer2/l2g-v2-prefit-authority.json").read_bytes()
    )
    assert document["schema_version"] == "l2g-v2-prefit-authority-v4"
    from minos_engine.models.prefit_loader import load_verified_training_dataset
    from minos_engine.models.relative_finalist_dataset import (
        build_relative_finalist_dataset,
    )

    dataset = build_relative_finalist_dataset(load_verified_training_dataset())
    assert document["expected_relative_cell_set_hash"] == expected_relative_cell_set_hash(
        dataset.rows
    )
    assert document["expected_bam_chromosome_set_hash"] == expected_bam_chromosome_set_hash(
        dataset.bam_chromosome
    )


def test_an_edited_authority_is_refused(tmp_path: Path) -> None:
    fake = tmp_path / "reports" / "layer2"
    fake.mkdir(parents=True)
    source = (repository_root() / "reports/layer2/l2g-v2-prefit-authority.json").read_bytes()
    document = json.loads(source)
    document["protocol_hash"] = "f" * 64
    (fake / "l2g-v2-prefit-authority.json").write_bytes(json.dumps(document).encode("utf-8"))
    with pytest.raises(RelativeAuthorityError, match="hashes to"):
        verify_v2_prefit_authority(tmp_path)


# ---------------------------------------------------------------------------------------- #
# adversarial: rehashed tampering must still fail
# ---------------------------------------------------------------------------------------- #
def _republish(v2_published: dict[str, Any], tmp_path: Path, mutate: Any) -> Path:
    """Copy the tree, mutate it, and repair every hash so only recomputation can catch it."""
    import shutil

    target = tmp_path / V2_OUTPUT_LAYOUT["root"]
    shutil.copytree(v2_published["dir"], target)
    result = json.loads((target / "campaign-result.json").read_bytes())
    mutate(target, result)
    (target / "campaign-result.json").write_bytes(
        json.dumps(result, sort_keys=True).encode("utf-8")
    )
    return target


def test_an_edited_policy_metric_fails_even_when_every_hash_is_repaired(
    v2_published: dict[str, Any], tmp_path: Path
) -> None:
    from minos_engine.models.relative_finalist_evidence import v2_metric_artifact_identity

    def mutate(target: Path, result: dict[str, Any]) -> None:
        entry = next(e for e in result["per_spec"] if e["status"] == "COMPLETE")
        path = target / "metrics" / f"{entry['spec_hash']}.json"
        metric = json.loads(path.read_bytes())
        metric["metrics"]["mean_regret"] = 0.0
        data = json.dumps(metric, sort_keys=True).encode("utf-8")
        path.write_bytes(data)
        entry["metric_file_sha256"] = hashlib.sha256(data).hexdigest()
        entry["metric_scientific_hash"] = v2_metric_artifact_identity(metric)
        entry["promotion_metrics"]["mean_regret"] = 0.0

    target = _republish(v2_published, tmp_path, mutate)
    with pytest.raises((V2EvidenceError, Exception)):
        verify_published_l2g_v2_train_campaign(target)


def test_an_edited_safe_bar_fails_because_it_is_recomputed(
    v2_published: dict[str, Any], tmp_path: Path
) -> None:
    def mutate(target: Path, result: dict[str, Any]) -> None:
        result["safe_baseline_mean_regret"] = 0.0

    target = _republish(v2_published, tmp_path, mutate)
    # caught either by the recomputed bar or by the shortlist it would have changed
    with pytest.raises(V2EvidenceError, match="own decisions give|three-part rule"):
        verify_published_l2g_v2_train_campaign(target)


def test_an_edited_source_tree_fails(v2_published: dict[str, Any], tmp_path: Path) -> None:
    def mutate(target: Path, result: dict[str, Any]) -> None:
        result["execution_source_tree"] = "0" * 40

    target = _republish(v2_published, tmp_path, mutate)
    with pytest.raises(V2EvidenceError, match="actual tree"):
        verify_published_l2g_v2_train_campaign(target)


def test_an_edited_feature_authority_fails(v2_published: dict[str, Any], tmp_path: Path) -> None:
    def mutate(target: Path, result: dict[str, Any]) -> None:
        result["feature_set_hash"] = "f" * 64

    target = _republish(v2_published, tmp_path, mutate)
    with pytest.raises(V2EvidenceError, match="feature_set_hash"):
        verify_published_l2g_v2_train_campaign(target)


def test_an_unexpected_non_json_file_fails(v2_published: dict[str, Any], tmp_path: Path) -> None:
    target = _republish(v2_published, tmp_path, lambda t, r: None)
    (target / "notes.txt").write_text("hello")
    with pytest.raises(V2EvidenceError, match="unexpected file"):
        verify_published_l2g_v2_train_campaign(target)


def test_an_unexpected_subdirectory_fails(v2_published: dict[str, Any], tmp_path: Path) -> None:
    target = _republish(v2_published, tmp_path, lambda t, r: None)
    (target / "extra").mkdir()
    with pytest.raises(V2EvidenceError, match="unexpected directory"):
        verify_published_l2g_v2_train_campaign(target)


# ---------------------------------------------------------------------------------------- #
# the verified capability and the bundle
# ---------------------------------------------------------------------------------------- #
def test_a_caller_cannot_mint_a_verified_published_campaign() -> None:
    with pytest.raises(V2EvidenceError, match="may only be minted"):
        VerifiedPublishedL2GV2TrainCampaign(object(), result={}, root=Path("/tmp"), per_spec={})
    assert _VERIFIED_TOKEN is not None


def test_the_verified_capability_carries_real_oof_identities(v2_published: dict[str, Any]) -> None:
    verified = load_verified_published_l2g_v2_campaign(v2_published["dir"])
    result = json.loads((v2_published["dir"] / "campaign-result.json").read_bytes())
    for entry in result["per_spec"]:
        if entry["status"] != "COMPLETE":
            continue
        assert verified.oof_scientific_hash(entry["spec_hash"]) == entry["oof_scientific_hash"]
        assert len(verified.oof_residuals(entry["spec_hash"])) == 150


def test_the_bundle_never_binds_sixty_four_zeroes(v2_published: dict[str, Any]) -> None:
    verified = load_verified_published_l2g_v2_campaign(v2_published["dir"])
    if not verified.shortlist:
        pytest.skip("this synthetic campaign shortlisted nothing")
    spec_hash = verified.shortlist[0]
    bundle = build_trusted_final_train_bundle(
        verified_campaign=verified,
        spec_hash=spec_hash,
        estimator_artifact_sha256="a" * 64,
        transform_artifact_sha256="b" * 64,
    )
    assert bundle["train_oof_evidence_identity"] != "0" * 64
    assert bundle["train_oof_evidence_identity"] == verified.oof_scientific_hash(spec_hash)
    assert bundle["feature_set_hash"] == ACCEPTED_FEATURE_SET_HASH
    assert bundle["feature_set_hash"] != bundle["finalist_domain_hash"]


def test_the_bundle_requires_a_verified_campaign_not_a_dict() -> None:
    with pytest.raises(RelativeAuthorityError, match="VERIFIED published"):
        build_trusted_final_train_bundle(
            verified_campaign={"shortlist": ("a" * 64,)},
            spec_hash="a" * 64,
            estimator_artifact_sha256="a" * 64,
            transform_artifact_sha256=None,
        )


def test_a_non_shortlisted_spec_cannot_be_bundled(v2_published: dict[str, Any]) -> None:
    verified = load_verified_published_l2g_v2_campaign(v2_published["dir"])
    result = verified.result
    rejected = [
        e["spec_hash"] for e in result["per_spec"] if e["spec_hash"] not in verified.shortlist
    ]
    if not rejected:
        pytest.skip("every spec shortlisted in this synthetic campaign")
    with pytest.raises(RelativeAuthorityError, match="not in the verified TRAIN shortlist"):
        build_trusted_final_train_bundle(
            verified_campaign=verified,
            spec_hash=rejected[0],
            estimator_artifact_sha256="a" * 64,
            transform_artifact_sha256=None,
        )


# ---------------------------------------------------------------------------------------- #
# locks
# ---------------------------------------------------------------------------------------- #
def test_no_real_v2_output_exists() -> None:
    from tests.minos_scratch import CANONICAL_MINOS_ROOT

    assert not (CANONICAL_MINOS_ROOT / V2_OUTPUT_LAYOUT["root"]).exists()


def test_the_accepted_science_did_not_move() -> None:
    assert compute_relative_protocol_hash() == (
        "d2b275c0ffde8f12db5a937a0b5b0cceab8b9cdce921779d79995bd74a9a2bd6"
    )
    assert list(build_v2_spec_hashes(ACCEPTED_RELATIVE_DATASET_IDENTITY)) == [
        "b7e68f65a4b28d2370243edaad38f0a8670c04de9fab1faa9383243a1ca69aa4",
        "6be59fbcf340364535ce6efd16faef47376c6ca82a3ad2a4feba681e6058578c",
        "673ecf5b4dca196a0bfff55ca9c933ea4712684b2a3aec6d41217e414c06d377",
        "0ab6a36a250dbe88bff68af88001404f9ac6daad4c6220938d65945c56ea63c6",
    ]
    assert ACCEPTED_RELATIVE_DATASET_IDENTITY == (
        "4a8f2777ebaddffb29dc2ae96a6426eff5e76c1f23b3ee43c1b275f8d97bc5e5"
    )
    assert ACCEPTED_RELATIVE_CONTRACT_HASH == (
        "dd5aca807bf0499411b9b4279c206d7a0e9a6d50c85bf75c5d319cf7ba2d394e"
    )
    assert FINALIST_DOMAIN[0] == SAFE_BASELINE_CONFIG_HASH
