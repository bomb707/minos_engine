"""Feature-eligibility registry: scalar-exhaustive, deterministic, restrictive."""

from __future__ import annotations

import hashlib
import json
import math

import pytest

from minos_engine.layer2 import feature_registry as FR
from minos_engine.layer2.contracts import (
    CanonicalFeatureVector,
    FeatureEligibilityState,
    FeatureValueKind,
)
from tests.conftest import REPO_ROOT

_V2 = REPO_ROOT / "reports" / "LAYER1_MULTI_DATASET_ACCURACY_RESULTS_V2.json"
_CONFIG = REPO_ROOT / "configs" / "layer1" / "default.yaml"
_ANALYTIC = {"exact", "float_strict", "approximate", "derived", "sampled"}
_DATA_DEPENDENT = (
    "reference_context.homopolymer_length_histogram",
    "spatial.stratum_window_counts",
)


def _v2_analytical_by_dataset() -> list[set[str]]:
    d = json.loads(_V2.read_text(encoding="utf-8"))
    return [
        {r["path"] for r in ds["field_records"] if r["classification"] in _ANALYTIC}
        for ds in d["datasets"]
    ]


# --------------------------------------------------------------------------- #
# Exhaustiveness / reconciliation
# --------------------------------------------------------------------------- #
def test_every_v2_analytical_field_maps_exactly_once():
    paths = {r.field_path for r in FR.FEATURE_REGISTRY}
    missing: set[str] = set()
    for ds in _v2_analytical_by_dataset():
        for p in ds:
            if any(p.startswith(cp + ".") for cp in _DATA_DEPENDENT):
                # data-dependent bins map to their (single) container record
                if p.rsplit(".", 1)[0] not in paths:
                    missing.add(p)
            elif p not in paths:
                missing.add(p)
    assert missing == set()


def test_no_duplicate_paths():
    paths = [r.field_path for r in FR.FEATURE_REGISTRY]
    assert len(paths) == len(set(paths))


def test_containers_are_never_model_features():
    for r in FR.FEATURE_REGISTRY:
        if r.value_kind is FeatureValueKind.CONTAINER:
            assert r.model_feature is False


def test_scalar_leaves_expanded_not_containers():
    # The concrete quantile/threshold leaves exist as scalar model features.
    for leaf in (
        "mapping_quality.quantiles.P50",
        "coverage.fragment_primary.depth_quantiles.P95",
        "base_quality.quantiles_phred.P01",
        "variant_evidence.support_threshold_site_counts.support_ge_2",
    ):
        rec = FR.record_for(leaf)
        assert rec is not None and rec.model_feature is True


def test_deterministic_order_and_stable_hash():
    paths = [r.field_path for r in FR.FEATURE_REGISTRY]
    assert paths == sorted(paths)
    assert FR.registry_hash() == FR.REGISTRY_HASH == FR.registry_hash()


def test_counts_by_state_and_value_kind():
    st = FR.counts_by_state()
    assert st == {"ELIGIBLE": 147, "CONDITIONAL": 60, "RESEARCH_ONLY": 2, "FORBIDDEN": 76}
    assert sum(st.values()) == len(FR.FEATURE_REGISTRY) == 285
    vk = FR.counts_by_value_kind()
    assert vk["CONTAINER"] == 21
    assert sum(vk.values()) == 285


def test_snp_density_research_only_both_namespaces():
    assert FR.state_for("variant_evidence.candidate_snp_density_per_base") is (
        FeatureEligibilityState.RESEARCH_ONLY
    )
    assert FR.state_for("window.candidate_snp_density_per_base") is (
        FeatureEligibilityState.RESEARCH_ONLY
    )


def test_no_eligible_or_conditional_is_truth_derived():
    for r in FR.FEATURE_REGISTRY:
        if r.state is not FeatureEligibilityState.FORBIDDEN:
            assert r.truth_derived is False


def test_coordinates_and_identities_forbidden():
    for p in ("region.start0", "region.end0_exclusive", "region.length_bp", "identity.bam_sha256"):
        assert FR.state_for(p) is FeatureEligibilityState.FORBIDDEN


# --------------------------------------------------------------------------- #
# Config-bound dynamic maps
# --------------------------------------------------------------------------- #
def test_config_bound_keys_match_accepted_config():
    cfg = _CONFIG.read_text(encoding="utf-8")
    assert "0.01, 0.05, 0.10, 0.25, 0.50, 0.75, 0.90, 0.95, 0.99" in cfg
    assert "[2, 3, 5, 8, 10]" in cfg
    assert "[0.05, 0.10, 0.20, 0.30, 0.40]" in cfg
    assert FR.CONFIG_BOUND_KEYS["mapping_quality.quantiles"] == (
        "P01",
        "P05",
        "P10",
        "P25",
        "P50",
        "P75",
        "P90",
        "P95",
        "P99",
    )
    assert FR.CONFIG_BOUND_KEYS["variant_evidence.support_threshold_site_counts"] == (
        "support_ge_2",
        "support_ge_3",
        "support_ge_5",
        "support_ge_8",
        "support_ge_10",
    )


def test_all_accepted_dynamic_keys_resolve():
    for parent, keys in FR.CONFIG_BOUND_KEYS.items():
        for k in keys:
            rec = FR.record_for(f"{parent}.{k}")
            assert rec is not None, f"{parent}.{k}"


def test_unknown_dynamic_key_fails_closed():
    with pytest.raises(ValueError):
        FR.state_for("mapping_quality.quantiles.P42")
    with pytest.raises(ValueError):
        FR.assert_production_feature_vector(["mapping_quality.quantiles.P42"])


def test_source_schema_hashes_match_files():
    for schema, name in (
        ("bam-profile-v1", "bam-profile-v1.schema.json"),
        ("window-profile-v1", "window-profile-v1.schema.json"),
    ):
        actual = hashlib.sha256((REPO_ROOT / "schemas" / name).read_bytes()).hexdigest()
        assert FR.SOURCE_SCHEMA_HASHES[schema] == actual


# --------------------------------------------------------------------------- #
# Production feature vector authorization
# --------------------------------------------------------------------------- #
def test_eligible_scalar_accepted():
    FR.assert_production_feature_vector(["reads.mapped_fraction", "mapping_quality.quantiles.P50"])


def test_conditional_rejected():
    with pytest.raises(ValueError):
        FR.assert_production_feature_vector(["filter_counts.observed"])


def test_research_only_rejected():
    with pytest.raises(ValueError):
        FR.assert_production_feature_vector(["variant_evidence.candidate_snp_density_per_base"])


def test_forbidden_rejected():
    with pytest.raises(ValueError):
        FR.assert_production_feature_vector(["region.contig"])


def test_container_rejected():
    with pytest.raises(ValueError):
        FR.assert_production_feature_vector(["mapping_quality.quantiles"])


def test_unknown_rejected():
    with pytest.raises(ValueError):
        FR.assert_production_feature_vector(["does.not.exist"])


def test_duplicate_rejected():
    with pytest.raises(ValueError):
        FR.assert_production_feature_vector(["reads.mapped_fraction", "reads.mapped_fraction"])


def test_no_promotions_parameter():
    import inspect

    sig = inspect.signature(FR.assert_production_feature_vector)
    assert list(sig.parameters) == ["field_paths"]


# --------------------------------------------------------------------------- #
# validate_production_feature_mapping
# --------------------------------------------------------------------------- #
def test_mapping_returns_canonical_vector():
    v = FR.validate_production_feature_mapping(
        {"reads.mapped_fraction": 0.9, "mapping_quality.quantiles.P50": 42.0}
    )
    assert isinstance(v, CanonicalFeatureVector)
    assert v.fields == ("mapping_quality.quantiles.P50", "reads.mapped_fraction")
    assert v.registry_hash == FR.REGISTRY_HASH
    again = FR.validate_production_feature_mapping(
        {"mapping_quality.quantiles.P50": 42.0, "reads.mapped_fraction": 0.9}
    )
    assert again.vector_hash == v.vector_hash  # order-independent, deterministic


def test_mapping_rejects_bool():
    with pytest.raises(ValueError):
        FR.validate_production_feature_mapping({"reads.mapped_fraction": True})


def test_mapping_rejects_nan_inf():
    with pytest.raises(ValueError):
        FR.validate_production_feature_mapping({"reads.mapped_fraction": math.nan})
    with pytest.raises(ValueError):
        FR.validate_production_feature_mapping({"reads.mapped_fraction": math.inf})


def test_mapping_rejects_wrong_type():
    with pytest.raises(ValueError):
        FR.validate_production_feature_mapping({"reads.mapped_fraction": "0.9"})  # type: ignore[dict-item]


def test_mapping_rejects_fraction_out_of_range():
    with pytest.raises(ValueError):
        FR.validate_production_feature_mapping({"reads.mapped_fraction": 1.5})


def test_mapping_rejects_container_and_conditional():
    with pytest.raises(ValueError):
        FR.validate_production_feature_mapping({"mapping_quality.quantiles": 1.0})
    with pytest.raises(ValueError):
        FR.validate_production_feature_mapping({"filter_counts.observed": 10})
