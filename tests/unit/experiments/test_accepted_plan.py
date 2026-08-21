"""F3-B accepted-constructor behavioral tests (no source-string matching).

Proves the no-override accepted boundary: deterministic repeatable construction and
``plan_hash``; the E5 gates + identities are PINNED (not merely PASS) so corrupting any pin
fails construction; every plan identity binds; validation/test members never appear; the
production API takes no arguments; a member mutation with consistently-recomputed lower hashes
still fails against the accepted snapshot anchor; a payload-forged candidate set with
recomputed hashes fails the accepted candidate authority; and ``select_config`` stays blocked.
"""

from __future__ import annotations

import inspect
import json
from pathlib import Path

import pytest

from minos_engine.common.canonical_json import canonical_json_bytes
from minos_engine.common.errors import StageNotReadyError
from minos_engine.common.hashing import sha256_hex
from minos_engine.experiments import accepted_plan as AP
from minos_engine.experiments import plan as PLAN
from minos_engine.experiments.accepted_plan import (
    AcceptedExperimentPlanError,
    E5ClosureError,
    build_accepted_experiment_plan,
    verify_e5_prerequisite_closure,
)
from minos_engine.experiments.candidates import (
    CandidateSet,
    CandidateSetVerificationError,
    generate_accepted_candidate_set,
    verify_accepted_candidate_set,
)
from minos_engine.experiments.plan import iter_logical_jobs, logical_job_keys
from minos_engine.layer2 import prerequisites as PRE

_ACCEPTED_PLAN_HASH = "1e6c4a5e70f370d800af91fc02ce6e312ebff29e39d0b8d554afa938f89959d8"


def test_accepted_construction_is_repeatable_and_pinned_plan_hash() -> None:
    a = build_accepted_experiment_plan()
    b = build_accepted_experiment_plan()
    assert a == b
    assert a.plan_hash == b.plan_hash == _ACCEPTED_PLAN_HASH
    assert (a.train_member_count, a.candidate_count, a.logical_job_count) == (50, 41, 2050)


def test_e5_closure_pins_source_tree_ancestry_not_merely_pass() -> None:
    checks = verify_e5_prerequisite_closure()
    for key in (
        "feature_view_ready_gate_hash_pinned",
        "feature_matrix_frozen_1_gate_hash_pinned",
        "fv_source_commit_pinned",
        "fv_source_tree_pinned",
        "fm_source_commit_pinned",
        "fm_source_tree_pinned",
        "train_feature_view_hash_pinned",
        "e5_source_tree_matches",
        "e5_source_descends_e4_evidence",
        "e5_evidence_tree_matches",
        "e5_evidence_descends_source",
        "head_descends_e5_evidence",
    ):
        assert checks[key] is True, key


@pytest.mark.parametrize(
    "pin",
    [
        "FEATURE_VIEW_READY_GATE_HASH",
        "FEATURE_MATRIX_FROZEN_1_GATE_HASH",
        "E5_SOURCE_COMMIT",
        "E5_SOURCE_TREE",
        "E5_EVIDENCE_COMMIT",
        "E5_EVIDENCE_TREE",
        "ACCEPTED_EPOCH1_TRAIN_FEATURE_VIEW_HASH",
        "E4_TRAIN_MATRIX_HASH",
        "PROFILE_SNAPSHOT_1_HASH",
        "ACCEPTED_FEATURE_REGISTRY_HASH",
    ],
)
def test_corrupting_any_pin_fails_construction(monkeypatch: pytest.MonkeyPatch, pin: str) -> None:
    # a locally regenerated PASS gate / wrong identity can never authorize F3-B.
    monkeypatch.setattr(PRE, pin, sha256_hex(f"corrupt:{pin}".encode())[: len(getattr(PRE, pin))])
    with pytest.raises((E5ClosureError, AcceptedExperimentPlanError)):
        build_accepted_experiment_plan()


def test_every_plan_identity_binds_exactly() -> None:
    plan = build_accepted_experiment_plan()
    cs = generate_accepted_candidate_set()
    assert plan.snapshot_hash == PRE.PROFILE_SNAPSHOT_1_HASH
    assert plan.split_manifest_hash == PRE.PROFILE_SNAPSHOT_1_SPLIT_MANIFEST_HASH
    assert plan.registry_snapshot_hash == PRE.PROFILE_SNAPSHOT_1_REGISTRY_SNAPSHOT_HASH
    assert plan.train_matrix_hash == PRE.E4_TRAIN_MATRIX_HASH
    assert plan.train_feature_view_hash == PRE.ACCEPTED_EPOCH1_TRAIN_FEATURE_VIEW_HASH
    assert plan.feature_registry_hash == PRE.ACCEPTED_FEATURE_REGISTRY_HASH
    assert plan.gatk_registry_hash == cs.policy.registry_hash
    assert plan.parameter_space_hash == cs.policy.parameter_space_hash
    assert plan.experiment_parameter_policy_hash == cs.policy.experiment_parameter_policy_hash
    assert plan.candidate_set_hash == cs.candidate_set_hash
    # candidate order exactly equals the accepted candidate set
    assert tuple(c.config_hash for c in plan.configs) == cs.ordered_config_hashes


def test_no_validation_or_test_members_in_plan_or_jobs() -> None:
    from minos_engine.layer2.features.extraction import load_accepted_epoch1_member_manifest

    snapshot = load_accepted_epoch1_member_manifest(AP._MEMBER_MANIFEST.read_bytes())
    train_ds = {m.dataset_id for m in snapshot.members_for("train")}
    validation_ds = {m.dataset_id for m in snapshot.members_for("validation")}

    plan = build_accepted_experiment_plan()
    plan_ds = {m.dataset_id for m in plan.members}
    assert plan_ds == train_ds
    assert not (plan_ds & validation_ds)
    # the logical-job iterator never references a non-train member either
    job_ds = {j.dataset_id for j in iter_logical_jobs(plan)}
    assert job_ds <= train_ds
    assert not (job_ds & validation_ds)


def test_production_api_takes_no_arguments() -> None:
    assert list(inspect.signature(build_accepted_experiment_plan).parameters) == []
    with pytest.raises(TypeError):
        build_accepted_experiment_plan("train")  # type: ignore[call-arg]


def test_logical_jobs_count_product_and_unique_ordered() -> None:
    plan = build_accepted_experiment_plan()
    assert plan.logical_job_count == plan.train_member_count * plan.candidate_count == 2050
    keys = logical_job_keys(plan)
    assert len(keys) == 2050
    assert len(set(keys)) == 2050
    order = [(j.member_index, j.config_index) for j in iter_logical_jobs(plan)]
    assert order == [(mi, ci) for mi in range(50) for ci in range(41)]


def test_member_mutation_with_recomputed_hashes_fails_against_snapshot_anchor(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    report = json.loads(AP._E4_TRAIN_REPORT.read_text())
    # mutate member 0 and "consistently" recompute its lower-level hashes.
    members = sorted(report["members"], key=lambda m: m["member_index"])
    members[0]["feature_values_hash"] = sha256_hex(b"mutated-feature-values")
    members[0]["vector_hash"] = sha256_hex(b"mutated-vector")
    report["members"] = members
    tampered = tmp_path / "L2E_E4_TRAIN_MATRIX.json"
    tampered.write_text(json.dumps(report))
    monkeypatch.setattr(AP, "_E4_TRAIN_REPORT", tampered)
    # the accepted snapshot membership (feature_values_hash) is the trust anchor -> rejected.
    with pytest.raises(AcceptedExperimentPlanError):
        build_accepted_experiment_plan()


def test_forged_candidate_payload_with_recomputed_hashes_fails_authority() -> None:
    cs = generate_accepted_candidate_set()
    # forge candidate #1's payload (mutate an EXISTING parameter value), then CONSISTENTLY
    # recompute its config_hash and the candidate_set_hash — the accepted authority still
    # rejects it because the payload does not match the code-owned regenerated set.
    cfg1 = cs.configs[1]
    key = next(iter(cfg1.effective_config))
    old = cfg1.effective_config[key]
    new_val: object = (
        (not old) if isinstance(old, bool) else (old + 1 if isinstance(old, int) else f"{old}x")
    )
    forged_effective = {**cfg1.effective_config, key: new_val}
    new_hash = sha256_hex(canonical_json_bytes(forged_effective))
    forged_cfg = cfg1.model_copy(
        update={"effective_config": forged_effective, "config_hash": new_hash}
    )
    ordered = list(cs.ordered_config_hashes)
    ordered[1] = new_hash
    from minos_engine.experiments.candidates import candidate_set_hash as _csh

    forged = CandidateSet(
        policy=cs.policy,
        configs=(cs.configs[0], forged_cfg, *cs.configs[2:]),
        ordered_config_hashes=tuple(ordered),
        candidate_count=cs.candidate_count,
        candidate_set_hash=_csh(policy=cs.policy, ordered_config_hashes=tuple(ordered)),
        skipped=cs.skipped,
    )
    with pytest.raises(CandidateSetVerificationError):
        verify_accepted_candidate_set(forged)


def test_no_public_override_constructor_or_trust_anchor_injection() -> None:
    # the only exported accepted boundary is the no-arg constructor.
    assert "build_accepted_experiment_plan" in AP.__all__
    # the pure structural assembler is private and NOT exported.
    assert "_assemble_experiment_plan" not in PLAN.__all__
    assert not hasattr(PLAN, "assemble_experiment_plan")
    # no exported accepted/generic builder accepts identity/trust overrides.
    for name in AP.__all__:
        obj = getattr(AP, name)
        if callable(obj) and name.startswith("build"):
            assert list(inspect.signature(obj).parameters) == [], name


def test_select_config_remains_blocked() -> None:
    from minos_engine.layer2.service import Layer2Service

    with pytest.raises(StageNotReadyError):
        Layer2Service().select_config(None)
