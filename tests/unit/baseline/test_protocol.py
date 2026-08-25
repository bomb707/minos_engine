"""The frozen protocol identity: what it binds, what it must NOT bind, and its determinism."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
from pathlib import Path
from typing import Any

import pytest

from minos_engine.baseline.protocol import (
    BASELINE_PROTOCOL_MANIFEST,
    BASELINE_PROTOCOL_VERSION,
    GATK_TIMEOUT_SECONDS,
    HAPPY_TIMEOUT_SECONDS,
    INFRASTRUCTURE_ABORT_THRESHOLD,
    MAX_EVALUATION_BUDGET,
    PARAMETER_SPACE_HASH,
    PHASE_A_CANDIDATE_SET_HASH,
    BaselineProtocolError,
    build_baseline_protocol,
    compute_protocol_hash,
    load_committed_protocol,
)
from minos_engine.baseline.schedule import build_train_schedule


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def _clone(tmp_path: Path, name: str) -> Path:
    root = tmp_path / name
    (root / "manifests").mkdir(parents=True)
    for manifest in (
        "layer2_dataset_split_v2_epoch1.json",
        "l2f_gatk_parameter_space_v1.json",
        "l2f2_baseline_protocol_v1.json",
    ):
        source = _repo_root() / "manifests" / manifest
        if source.is_file():
            shutil.copy2(source, root / "manifests" / manifest)
    return root


# --------------------------------------------------------------------------- #
# D1-D8 are resolved
# --------------------------------------------------------------------------- #
def test_every_protocol_decision_is_resolved() -> None:
    decisions = build_baseline_protocol().decisions()
    assert sorted(decisions) == [
        "D1_primary_optimization_target",
        "D2_objective_form",
        "D3_robustness_parameters",
        "D4_runtime_treatment",
        "D5_compute_budget",
        "D6_validation_timing",
        "D7_platform_reward_modelling",
        "D8_phase_b_design_family",
    ]
    assert decisions["D1_primary_optimization_target"] == "LEVEL_ROBUST_PRIMARY_RANK_DIAGNOSTIC"
    assert decisions["D2_objective_form"] == "OPTION_B_CVAR_FLOOR_MEAN_FAILURE_PENALTY"
    assert decisions["D5_compute_budget"] == "STANDARD"
    assert decisions["D6_validation_timing"] == "L2F2F_AFTER_TRAIN_RANKING_FINAL"
    assert decisions["D7_platform_reward_modelling"] == "NO_SIMULATED_OPPONENT_DISTRIBUTION"
    assert decisions["D8_phase_b_design_family"] == ("DETERMINISTIC_MIXED_DOMAIN_LATIN_HYPERCUBE")
    assert "no owner" not in json.dumps(decisions).lower()


def test_the_frozen_budget_and_timeouts_are_exact() -> None:
    content = build_baseline_protocol().content()
    assert content["budget"]["phase_maximums"] == {
        "phase_a": 195,
        "phase_b": 480,
        "phase_c": 500,
        "phase_d": 40,
    }
    assert MAX_EVALUATION_BUDGET == 1215 == content["budget"]["maximum_evaluation_pairs"]
    assert GATK_TIMEOUT_SECONDS == HAPPY_TIMEOUT_SECONDS == 3600
    assert INFRASTRUCTURE_ABORT_THRESHOLD == 0.05


def test_the_protocol_binds_the_immutable_upstream_authorities() -> None:
    content = build_baseline_protocol().content()
    assert content["phase_a"]["candidate_set_hash"] == PHASE_A_CANDIDATE_SET_HASH
    assert content["phase_a"]["candidate_count"] == 39
    assert content["phase_b"]["parameter_space_hash"] == PARAMETER_SPACE_HASH
    assert content["train_schedule"]["split_manifest_sha256"] == (
        build_train_schedule().split_manifest_sha256
    )


def test_test_partition_is_locked_and_validation_is_deferred() -> None:
    content = build_baseline_protocol().content()
    assert content["test_lock"]["l2f2_usage"] == "ZERO"
    assert content["test_lock"]["sealed_until"] == "L2-I"
    assert content["validation"]["stage"] == "L2-F2-F"
    assert content["validation"]["evaluations"] == 40
    assert "phase_c" in content["validation"]["forbidden_before"]


def test_rank_diagnostics_can_never_influence_selection() -> None:
    diagnostics = build_baseline_protocol().content()["rank_diagnostics"]
    assert diagnostics["policy"] == "DIAGNOSTIC_ONLY"
    assert diagnostics["may_influence"] == []
    assert set(diagnostics["must_never_influence"]) == {
        "objective",
        "racing",
        "promotion",
        "baseline_selection",
    }


# --------------------------------------------------------------------------- #
# what the hash must NOT bind
# --------------------------------------------------------------------------- #
def test_the_protocol_binds_decisions_never_outcomes() -> None:
    """A protocol that bound its own results would be circular and unverifiable.

    The strong form of the claim: every 64-hex identity inside the protocol is one of the three
    committed UPSTREAM authorities. No candidate hash, no evaluation hash and no future Phase-B
    configuration can therefore have leaked into the identity.
    """
    import re

    content = build_baseline_protocol().content()
    body = json.dumps(content, sort_keys=True)
    identities = set(re.findall(r"\b[0-9a-f]{64}\b", body))
    assert identities == {
        PHASE_A_CANDIDATE_SET_HASH,
        PARAMETER_SPACE_HASH,
        build_train_schedule().split_manifest_sha256,
    }

    # no filesystem path, no repository SHA, no future design output is present as a VALUE
    assert "/home/" not in body
    assert "selected_dimensions" not in body
    assert "source_commit" not in body and "git_sha" not in body

    # the objective section describes the RULE; it carries no observed value
    assert content["objective"]["aggregation_utility_rule"].startswith("admitted -> minos_score")
    # and the design section explicitly NAMES the entropy sources it forbids, which is the
    # opposite of leaking them
    assert set(content["phase_b"]["entropy_sources_forbidden"]) == {
        "system_random_seed",
        "current_time",
        "python_hash",
        "hostname",
        "pid",
    }


def test_the_protocol_is_a_separate_identity_from_the_scoring_contract() -> None:
    from minos_engine.evaluation.scoring_contract import (
        compute_scoring_contract_hash,
        load_scoring_authority,
    )

    scoring = compute_scoring_contract_hash(load_scoring_authority(_repo_root()))
    assert build_baseline_protocol().protocol_hash != scoring
    assert scoring not in json.dumps(build_baseline_protocol().content())


# --------------------------------------------------------------------------- #
# determinism and sensitivity
# --------------------------------------------------------------------------- #
def test_the_protocol_hash_is_identical_from_independent_directories(tmp_path: Path) -> None:
    first = build_baseline_protocol(_clone(tmp_path, "one"))
    second = build_baseline_protocol(_clone(tmp_path, "two"))
    assert first.protocol_hash == second.protocol_hash
    assert first.content() == second.content()
    assert json.dumps(first.content(), sort_keys=True) == json.dumps(
        second.content(), sort_keys=True
    )


def test_runtime_environment_cannot_move_the_protocol_hash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    baseline = build_baseline_protocol().protocol_hash
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    monkeypatch.setenv("HOSTNAME", "some-other-host")
    monkeypatch.chdir(tmp_path)
    assert build_baseline_protocol(_repo_root()).protocol_hash == baseline
    assert os.environ["HOSTNAME"] == "some-other-host"


def test_changing_a_robustness_constant_moves_the_protocol_hash() -> None:
    """The mutation control: alpha 0.25 -> 0.20 must be a DIFFERENT protocol."""
    protocol = build_baseline_protocol()
    baseline = protocol.protocol_hash
    mutated = json.loads(json.dumps(protocol.content()))
    mutated["objective"]["cvar_alpha"] = 0.20

    from minos_engine.baseline.protocol import BASELINE_PROTOCOL_DOMAIN
    from minos_engine.common.canonical_json import canonical_json_bytes
    from minos_engine.common.hashing import sha256_hex

    moved = sha256_hex(BASELINE_PROTOCOL_DOMAIN.encode("utf-8") + canonical_json_bytes(mutated))
    assert moved != baseline


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("objective", "weight_cvar"), 0.55),
        (("objective", "failure_penalty_lambda"), 0.5),
        (("promotion", "validation_finalists"), 5),
        (("racing", "optimistic_unseen_utility"), 0.9),
        (("phase_b", "candidate_count"), 47),
    ],
)
def test_any_frozen_rule_change_is_a_different_protocol(path: tuple[str, ...], value: Any) -> None:
    from minos_engine.baseline.protocol import BASELINE_PROTOCOL_DOMAIN
    from minos_engine.common.canonical_json import canonical_json_bytes
    from minos_engine.common.hashing import sha256_hex

    protocol = build_baseline_protocol()
    mutated = json.loads(json.dumps(protocol.content()))
    node = mutated
    for key in path[:-1]:
        node = node[key]
    node[path[-1]] = value
    moved = sha256_hex(BASELINE_PROTOCOL_DOMAIN.encode("utf-8") + canonical_json_bytes(mutated))
    assert moved != protocol.protocol_hash


# --------------------------------------------------------------------------- #
# the committed manifests
# --------------------------------------------------------------------------- #
def test_the_committed_protocol_manifest_matches_the_code() -> None:
    document = load_committed_protocol()
    assert document["protocol_version"] == BASELINE_PROTOCOL_VERSION
    assert document["protocol_hash"] == build_baseline_protocol().protocol_hash


def test_a_tampered_committed_manifest_is_refused(tmp_path: Path) -> None:
    root = _clone(tmp_path, "tampered")
    path = root / BASELINE_PROTOCOL_MANIFEST
    document = json.loads(path.read_text(encoding="utf-8"))
    document["content"]["objective"]["cvar_alpha"] = 0.20
    path.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(BaselineProtocolError, match="does not match"):
        load_committed_protocol(root)


def test_a_missing_committed_manifest_fails_closed(tmp_path: Path) -> None:
    with pytest.raises(BaselineProtocolError, match="missing"):
        load_committed_protocol(tmp_path)


def test_the_committed_train_schedule_manifest_matches_the_code() -> None:
    path = _repo_root() / "manifests" / "l2f2_train_schedule_v1.json"
    document = json.loads(path.read_text(encoding="utf-8"))
    assert document == build_train_schedule().content()
    assert len(hashlib.sha256(path.read_bytes()).hexdigest()) == 64


def test_the_committed_manifests_contain_no_closed_partition_identity() -> None:
    schedule_body = (_repo_root() / "manifests" / "l2f2_train_schedule_v1.json").read_text()
    document = json.loads(
        (_repo_root() / "manifests" / "layer2_dataset_split_v2_epoch1.json").read_text()
    )
    closed = {s["dataset_id"] for s in document["samples"] if s["partition"] != "train"}
    assert closed, "the split manifest does contain closed partitions"
    for dataset_id in closed:
        assert dataset_id not in schedule_body
    protocol_body = (_repo_root() / BASELINE_PROTOCOL_MANIFEST).read_text()
    for dataset_id in closed:
        assert dataset_id not in protocol_body


def test_the_protocol_manifest_carries_no_future_design_output() -> None:
    """The committed manifest may not contain anything Phase A or Phase B will later produce."""
    import re

    document = json.loads((_repo_root() / BASELINE_PROTOCOL_MANIFEST).read_text())
    identities = set(re.findall(r"\b[0-9a-f]{64}\b", json.dumps(document, sort_keys=True)))
    expected = {
        PHASE_A_CANDIDATE_SET_HASH,
        PARAMETER_SPACE_HASH,
        build_train_schedule().split_manifest_sha256,
        document["protocol_hash"],
    }
    assert identities == expected, "an unexpected identity is committed in the protocol manifest"
    for forbidden in ("selected_dimensions", "anchor_config_hashes", "lhs_config_hashes"):
        assert forbidden not in json.dumps(document)


def test_compute_protocol_hash_is_stable_for_an_identical_protocol() -> None:
    assert compute_protocol_hash(build_baseline_protocol()) == compute_protocol_hash(
        build_baseline_protocol()
    )


def test_the_committed_protocol_manifest_validates_against_its_schema() -> None:
    from minos_engine.schema_registry import validate_against

    document = json.loads((_repo_root() / BASELINE_PROTOCOL_MANIFEST).read_text())
    validate_against("l2f2-baseline-protocol-v1", document)


def test_the_schema_refuses_a_protocol_that_moved_a_frozen_count() -> None:
    from minos_engine.common.errors import ContractValidationError
    from minos_engine.schema_registry import validate_against

    document = json.loads((_repo_root() / BASELINE_PROTOCOL_MANIFEST).read_text())
    document["content"]["promotion"]["validation_finalists"] = 5
    with pytest.raises(ContractValidationError):
        validate_against("l2f2-baseline-protocol-v1", document)
