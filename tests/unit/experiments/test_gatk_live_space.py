"""L2-F committed live-GATK parameter-space contract — pure behavioral tests (no network).

Nothing here performs I/O beyond reading the COMMITTED snapshot through the module's own
repo-rooted constant; the endpoint is never contacted.
"""

from __future__ import annotations

import json
import math
from typing import Any

import pytest

from minos_engine.callers.contracts import ParameterState
from minos_engine.callers.gatk.parameter_registry import REGISTRY
from minos_engine.experiments.candidates import (
    generate_accepted_candidate_set,
    verify_candidates_against_live_domain,
)
from minos_engine.experiments.gatk_live_space import (
    LIVE_OPTIONS_KEY,
    LIVE_SOURCE_ENDPOINT,
    LOCAL_EXECUTION_PARAMETERS,
    GatkLiveParameterSpace,
    LiveSpaceError,
    check_endpoint_drift,
    live_gatk_parameter_space,
    load_live_gatk_parameter_space,
)

_SPACE = live_gatk_parameter_space()


def _doc() -> dict[str, Any]:
    """A fresh mutable copy of the committed document (round-tripped through its own content)."""
    doc = dict(_SPACE.scientific_content())
    doc["parameter_space_hash"] = _SPACE.parameter_space_hash
    doc["source_gatk_object_sha256"] = _SPACE.source_gatk_object_sha256
    doc["retrieved_at"] = _SPACE.retrieved_at
    return json.loads(json.dumps(doc))


def _reload(doc: dict[str, Any]) -> GatkLiveParameterSpace:
    """Re-hash then load, so a test can alter CONTENT without tripping the hash binding first."""
    scientific = {
        k: doc[k]
        for k in ("schema_version", "source_endpoint", "options_key", "caller", "parameters")
    }
    from minos_engine.common.canonical_json import canonical_json_bytes
    from minos_engine.common.hashing import sha256_hex

    doc["parameter_space_hash"] = sha256_hex(canonical_json_bytes(scientific))
    return load_live_gatk_parameter_space(doc)


# --------------------------------------------------------------------------- #
# 1-5: the committed snapshot self-verifies and matches the registry
# --------------------------------------------------------------------------- #
def test_committed_snapshot_parses_and_self_verifies() -> None:
    assert _SPACE.recompute_hash() == _SPACE.parameter_space_hash
    assert _SPACE.source_endpoint == LIVE_SOURCE_ENDPOINT
    assert _SPACE.source_gatk_object_sha256 and _SPACE.retrieved_at
    # retrieval time is provenance only: it is NOT in the scientific preimage.
    assert "retrieved_at" not in _SPACE.scientific_content()
    assert "source_gatk_object_sha256" not in _SPACE.scientific_content()


def test_exactly_25_gatk_parameters_with_the_declared_type_inventory() -> None:
    assert len(_SPACE.parameters) == 25
    assert len(set(_SPACE.names())) == 25
    assert _SPACE.type_counts() == {"int": 14, "float": 7, "bool": 2, "enum": 2}


def test_deepvariant_and_bcftools_are_excluded() -> None:
    names = set(_SPACE.names())
    for foreign in (
        "model_type",
        "vsc_min_fraction_indels",
        "min_MQ",
        "min_BQ",
        "no_BAQ",
        "ploidy",
    ):
        assert foreign not in names
    assert _SPACE.caller == "gatk"
    # local execution controls are declared but are never GATK options.
    assert not (names & set(LOCAL_EXECUTION_PARAMETERS))


def test_options_key_is_gatk_options() -> None:
    assert _SPACE.options_key == LIVE_OPTIONS_KEY == "gatk_options"


def test_names_types_and_defaults_match_the_registry() -> None:
    assert set(_SPACE.names()) == set(REGISTRY.names())
    for p in _SPACE.parameters:
        r = REGISTRY.get(p.name)
        assert r.type.value == p.type, p.name
        if p.type in ("int", "float"):
            assert float(r.official_default) == float(p.default), p.name
        else:
            assert r.official_default == p.default, p.name


def test_known_live_differences_are_present() -> None:
    smc = _SPACE.get("standard_min_confidence_threshold_for_calling")
    assert (smc.type, smc.minimum, smc.maximum, smc.default) == ("float", 30.0, 100.0, 30.0)
    cff = _SPACE.get("contamination_fraction_to_filter")
    assert (cff.type, cff.minimum, cff.maximum, cff.default) == ("float", 0.0, 0.05, 0.0)
    ploidy = _SPACE.get("sample_ploidy")
    assert (ploidy.type, ploidy.allowed_values, ploidy.default) == ("int", (2,), 2)
    soft = _SPACE.get("dont_use_soft_clipped_bases")
    assert (soft.type, soft.allowed_values, soft.default) == ("bool", (False,), False)


# --------------------------------------------------------------------------- #
# 6-9: strict rejection behavior
# --------------------------------------------------------------------------- #
def test_forged_hash_is_rejected() -> None:
    doc = _doc()
    doc["parameter_space_hash"] = "0" * 64
    with pytest.raises(LiveSpaceError):
        load_live_gatk_parameter_space(doc)


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("schema_version", "wrong-v9"),
        ("options_key", "deepvariant_options"),
        ("caller", "bcftools"),
    ],
)
def test_wrong_envelope_fields_reject(key: str, value: str) -> None:
    doc = _doc()
    doc[key] = value
    with pytest.raises(LiveSpaceError):
        _reload(doc)


def test_duplicate_and_unknown_and_missing_parameters_reject() -> None:
    doc = _doc()
    doc["parameters"] = [*doc["parameters"], dict(doc["parameters"][0])]  # duplicate name
    with pytest.raises(LiveSpaceError):
        _reload(doc)

    doc = _doc()
    doc["parameters"][0]["surprise"] = 1  # unknown key
    with pytest.raises(LiveSpaceError):
        _reload(doc)

    doc = _doc()
    del doc["parameters"][0]["default"]  # missing default
    with pytest.raises(LiveSpaceError):
        _reload(doc)


def test_unknown_type_and_invalid_bounds_and_bad_defaults_reject() -> None:
    doc = _doc()
    doc["parameters"][0]["type"] = "complex"
    with pytest.raises(LiveSpaceError):
        _reload(doc)

    doc = _doc()
    idx = next(i for i, p in enumerate(doc["parameters"]) if p["name"] == "min_pruning")
    doc["parameters"][idx]["min"], doc["parameters"][idx]["max"] = 10, 2  # min > max
    with pytest.raises(LiveSpaceError):
        _reload(doc)

    doc = _doc()
    doc["parameters"][idx]["default"] = 99  # outside min/max
    with pytest.raises(LiveSpaceError):
        _reload(doc)

    doc = _doc()
    j = next(i for i, p in enumerate(doc["parameters"]) if p["name"] == "sample_ploidy")
    doc["parameters"][j]["default"] = 3  # outside allowed_values
    with pytest.raises(LiveSpaceError):
        _reload(doc)


def test_allowed_values_entries_of_the_wrong_type_reject() -> None:
    doc = _doc()
    j = next(i for i, p in enumerate(doc["parameters"]) if p["name"] == "sample_ploidy")
    doc["parameters"][j]["allowed_values"] = ["2"]  # numeric string, not int
    with pytest.raises(LiveSpaceError):
        _reload(doc)


def test_bool_and_enum_may_not_declare_bounds() -> None:
    doc = _doc()
    j = next(
        i for i, p in enumerate(doc["parameters"]) if p["name"] == "recover_all_dangling_branches"
    )
    doc["parameters"][j]["min"] = 0
    with pytest.raises(LiveSpaceError):
        _reload(doc)


# --------------------------------------------------------------------------- #
# 7-9: allowed_values for int/bool/enum; float normalization; coercion rejection
# --------------------------------------------------------------------------- #
def test_allowed_values_admission_for_int_bool_and_enum() -> None:
    ploidy = _SPACE.get("sample_ploidy")
    assert ploidy.admits(2) and not ploidy.admits(3) and not ploidy.admits(1)
    soft = _SPACE.get("dont_use_soft_clipped_bases")
    assert soft.admits(False) and not soft.admits(True)
    enum = _SPACE.get("pcr_indel_model")
    assert enum.admits("HOSTILE") and not enum.admits("SOMETHING")
    # singleton legal domains yield NO alternative candidate
    assert ploidy.alternative_values() == ()
    assert soft.alternative_values() == ()
    assert enum.alternative_values() == ("NONE", "HOSTILE", "AGGRESSIVE")


def test_float_json_integers_normalize_to_float_semantics() -> None:
    smc = _SPACE.get("standard_min_confidence_threshold_for_calling")
    assert isinstance(smc.minimum, float) and smc.minimum == 30.0
    assert smc.admits(100) and smc.admits(100.0)  # JSON int accepted for a float parameter
    assert smc.normalized(100) == 100.0 and isinstance(smc.normalized(100), float)
    assert not smc.admits(29.9) and not smc.admits(100.1)


@pytest.mark.parametrize("bad", ["30", "abc", None, float("nan"), float("inf"), float("-inf")])
def test_numeric_strings_nan_infinity_and_null_reject(bad: Any) -> None:
    smc = _SPACE.get("standard_min_confidence_threshold_for_calling")
    assert not smc.admits(bad)
    if isinstance(bad, float) and not math.isfinite(bad):
        assert not _SPACE.get("min_pruning").admits(bad)


def test_bool_is_never_accepted_as_int_or_float() -> None:
    assert not _SPACE.get("min_pruning").admits(True)
    assert not _SPACE.get("standard_min_confidence_threshold_for_calling").admits(True)
    # ...and an int is never accepted as a bool
    assert not _SPACE.get("recover_all_dangling_branches").admits(1)
    assert not _SPACE.get("recover_all_dangling_branches").admits(0)


def test_enum_rejects_non_strings() -> None:
    enum = _SPACE.get("pcr_indel_model")
    assert not enum.admits(0) and not enum.admits(True) and not enum.admits(None)


# --------------------------------------------------------------------------- #
# effective-config validation incl. cross-parameter coupling
# --------------------------------------------------------------------------- #
def _seed_config() -> dict[str, Any]:
    return dict(generate_accepted_candidate_set().configs[0].effective_config)


def test_effective_config_validation_requires_the_complete_inventory() -> None:
    cfg = _seed_config()
    _SPACE.validate_effective_config(cfg)  # the seed is admissible

    missing = dict(cfg)
    missing.pop("min_pruning")
    with pytest.raises(LiveSpaceError):
        _SPACE.validate_effective_config(missing)

    extra = dict(cfg)
    extra["timeout"] = 300  # a LOCAL execution control must never appear
    with pytest.raises(LiveSpaceError):
        _SPACE.validate_effective_config(extra)


def test_coupling_min_lt_max_assembly_region_size_enforced() -> None:
    cfg = _seed_config()
    cfg["min_assembly_region_size"] = cfg["max_assembly_region_size"]
    with pytest.raises(LiveSpaceError):
        _SPACE.validate_effective_config(cfg)


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("standard_min_confidence_threshold_for_calling", 10.0),  # below the LIVE min
        ("contamination_fraction_to_filter", 0.5),  # above the LIVE max
        ("dont_use_soft_clipped_bases", True),  # outside the LIVE allowed_values
        ("sample_ploidy", 4),  # outside the LIVE allowed_values
    ],
)
def test_values_the_live_service_would_silently_default_are_rejected(name: str, value: Any) -> None:
    cfg = _seed_config()
    cfg[name] = value
    with pytest.raises(LiveSpaceError):
        _SPACE.validate_effective_config(cfg)


# --------------------------------------------------------------------------- #
# 10: every generated candidate is accepted UNCHANGED
# --------------------------------------------------------------------------- #
def test_all_generated_candidates_are_accepted_unchanged_by_the_live_domain() -> None:
    cs = generate_accepted_candidate_set()
    verify_candidates_against_live_domain(cs.configs)  # fail-closed; raises on any drift
    for c in cs.configs:
        _SPACE.validate_effective_config(dict(c.effective_config))


def test_registry_activation_state_is_separate_from_the_live_legal_domain() -> None:
    assert REGISTRY.get("sample_ploidy").state is ParameterState.FIXED
    assert REGISTRY.get("emit_ref_confidence").state is ParameterState.FIXED
    # EXPERIMENTAL but with a singleton live domain -> no alternative candidate
    assert REGISTRY.get("dont_use_soft_clipped_bases").state is ParameterState.EXPERIMENTAL
    assert _SPACE.get("dont_use_soft_clipped_bases").alternative_values() == ()
    assert REGISTRY.get("pcr_indel_model").state is ParameterState.EXPERIMENTAL


# --------------------------------------------------------------------------- #
# optional drift check (caller-supplied body; never a network call here)
# --------------------------------------------------------------------------- #
def _fresh_body() -> dict[str, Any]:
    params: list[dict[str, Any]] = []
    for p in _SPACE.parameters:
        row: dict[str, Any] = {"name": p.name, "type": p.type}
        if p.minimum is not None:
            row["min"] = p.minimum
        if p.maximum is not None:
            row["max"] = p.maximum
        if p.allowed_values is not None:
            row["allowed_values"] = list(p.allowed_values)
        row["default"] = p.default
        params.append(row)
    return {"tools": {"gatk": {"options_key": "gatk_options", "parameters": params}}}


def test_drift_check_reports_no_drift_for_an_identical_body() -> None:
    report = check_endpoint_drift(_fresh_body())
    assert report["drifted"] is False
    assert report["fresh_parameter_space_hash"] == _SPACE.parameter_space_hash


def test_drift_check_detects_a_changed_bound() -> None:
    body = _fresh_body()
    for p in body["tools"]["gatk"]["parameters"]:
        if p["name"] == "contamination_fraction_to_filter":
            p["max"] = 0.5  # the OLD documented bound
    report = check_endpoint_drift(body)
    assert report["drifted"] is True


def test_drift_check_requires_a_gatk_object() -> None:
    with pytest.raises(LiveSpaceError):
        check_endpoint_drift({"tools": {"deepvariant": {}}})
