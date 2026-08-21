"""F3-B pure ExperimentPlan contract + hash-formula + logical-job behavioral tests.

Behavioral proofs only (no source-string / inspect.getsource matching): strict construction
rejections, frozen domain-separated hash formulas, derived counts, deterministic ordered
logical-job enumeration, and per-field hash sensitivity.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from minos_engine.common.canonical_json import canonical_json_bytes
from minos_engine.common.hashing import sha256_hex
from minos_engine.experiments.candidates import generate_accepted_candidate_set
from minos_engine.experiments.plan import (
    JOB_KEY_DOMAIN,
    PLAN_HASH_DOMAIN,
    PLAN_SCHEMA_VERSION,
    ExperimentPlan,
    ExperimentPlanConfig,
    ExperimentPlanMember,
    _assemble_experiment_plan,
    compute_job_key,
    compute_plan_hash,
    iter_logical_jobs,
    logical_job_keys,
)

_CS = generate_accepted_candidate_set()


def _h(label: str) -> str:
    return sha256_hex(label.encode())


def _member(i: int, *, dataset: str | None = None) -> ExperimentPlanMember:
    d = dataset or f"ds-{i}"
    return ExperimentPlanMember(
        dataset_id=d,
        profile_id=f"profile-{d}",
        content_hash=_h(f"content:{d}"),
        feature_values_hash=_h(f"fvh:{d}"),
        vector_hash=_h(f"vec:{d}"),
        member_index=i,
    )


def _synth_plan(n_members: int, *, candidate_set=_CS) -> ExperimentPlan:
    members = [_member(i) for i in range(n_members)]
    return _assemble_experiment_plan(
        epoch=1,
        snapshot_hash=_h("snap"),
        split_manifest_hash=_h("split"),
        registry_snapshot_hash=_h("reg"),
        train_matrix_hash=_h("matrix"),
        train_feature_view_hash=_h("fview"),
        feature_set_hash=_h("fset"),
        feature_registry_hash=_h("freg"),
        candidate_set=candidate_set,
        ordered_members=members,
    )


# --------------------------------------------------------------------------- #
# strict construction rejections (bool/coercion/null/missing/extra)
# --------------------------------------------------------------------------- #
def test_member_rejects_bool_coercion_null_missing_extra() -> None:
    base = {
        "dataset_id": "d",
        "profile_id": "p",
        "content_hash": _h("c"),
        "feature_values_hash": _h("f"),
        "vector_hash": _h("v"),
        "member_index": 0,
    }
    ExperimentPlanMember(**base)  # valid
    for bad in (
        {**base, "member_index": True},  # bool-as-int
        {**base, "member_index": "0"},  # coercible numeric string
        {**base, "member_index": 1.0},  # float-as-int
        {**base, "content_hash": None},  # null
        {**base, "content_hash": "NOTHEX"},  # not hex64
        {**base, "content_hash": _h("c").upper()},  # uppercase hex
        {k: v for k, v in base.items() if k != "profile_id"},  # missing
        {**base, "surprise": 1},  # extra
        {**base, "member_index": -1},  # negative index
    ):
        with pytest.raises(ValidationError):
            ExperimentPlanMember(**bad)


def test_config_rejects_bool_coercion_null_missing_extra() -> None:
    base = {"config_index": 0, "config_hash": _h("c"), "parameter_space_hash": _h("ps")}
    ExperimentPlanConfig(**base)
    for bad in (
        {**base, "config_index": True},
        {**base, "config_index": "0"},
        {**base, "config_hash": None},
        {**base, "config_hash": "xyz"},
        {k: v for k, v in base.items() if k != "config_hash"},
        {**base, "extra": 1},
    ):
        with pytest.raises(ValidationError):
            ExperimentPlanConfig(**bad)


# --------------------------------------------------------------------------- #
# plan_hash formula + determinism + self-binding
# --------------------------------------------------------------------------- #
def test_plan_hash_matches_documented_formula() -> None:
    plan = _synth_plan(3)
    content = {
        "schema_version": plan.schema_version,
        "epoch": plan.epoch,
        "partition": "train",
        "snapshot_hash": plan.snapshot_hash,
        "split_manifest_hash": plan.split_manifest_hash,
        "registry_snapshot_hash": plan.registry_snapshot_hash,
        "train_matrix_hash": plan.train_matrix_hash,
        "train_feature_view_hash": plan.train_feature_view_hash,
        "feature_set_hash": plan.feature_set_hash,
        "feature_registry_hash": plan.feature_registry_hash,
        "gatk_registry_hash": plan.gatk_registry_hash,
        "parameter_space_hash": plan.parameter_space_hash,
        "experiment_parameter_policy_hash": plan.experiment_parameter_policy_hash,
        "candidate_set_hash": plan.candidate_set_hash,
        "ordered_members": [
            {
                "dataset_id": m.dataset_id,
                "profile_id": m.profile_id,
                "content_hash": m.content_hash,
                "feature_values_hash": m.feature_values_hash,
                "vector_hash": m.vector_hash,
                "member_index": m.member_index,
            }
            for m in plan.members
        ],
        "ordered_configs": [
            {
                "config_index": c.config_index,
                "config_hash": c.config_hash,
                "parameter_space_hash": c.parameter_space_hash,
            }
            for c in plan.configs
        ],
        "train_member_count": plan.train_member_count,
        "candidate_count": plan.candidate_count,
        "logical_job_count": plan.logical_job_count,
    }
    expected = sha256_hex(PLAN_HASH_DOMAIN.encode("utf-8") + canonical_json_bytes(content))
    assert plan.plan_hash == expected
    # the plan_hash field itself is excluded from the preimage.
    assert "plan_hash" not in content


def test_plan_hash_is_deterministic_and_self_binding() -> None:
    assert _synth_plan(4).plan_hash == _synth_plan(4).plan_hash
    plan = _synth_plan(3)
    data = plan.model_dump()
    data["plan_hash"] = _h("forged")
    with pytest.raises(ValidationError):  # incorrect self-binding plan_hash rejected
        ExperimentPlan(**data)


# --------------------------------------------------------------------------- #
# counts, ordering, partition
# --------------------------------------------------------------------------- #
def test_counts_are_derived_and_consistent() -> None:
    plan = _synth_plan(5)
    assert plan.train_member_count == 5
    assert plan.candidate_count == len(_CS.configs)
    assert plan.logical_job_count == plan.train_member_count * plan.candidate_count
    # inconsistent logical_job_count rejected
    data = plan.model_dump()
    data["logical_job_count"] = plan.logical_job_count + 1
    with pytest.raises(ValidationError):
        ExperimentPlan(**data)


def test_partition_is_structurally_fixed_to_train() -> None:
    plan = _synth_plan(2)
    assert plan.partition == "train"
    data = plan.model_dump()
    data["partition"] = "validation"
    with pytest.raises(ValidationError):
        ExperimentPlan(**data)
    data["partition"] = "test"
    with pytest.raises(ValidationError):
        ExperimentPlan(**data)


def test_members_must_be_in_matrix_index_order_not_arbitrary() -> None:
    members = [_member(i) for i in range(4)]
    reordered = [members[2], members[0], members[1], members[3]]
    with pytest.raises(ValidationError):  # member_index must equal position
        _assemble_experiment_plan(
            epoch=1,
            snapshot_hash=_h("s"),
            split_manifest_hash=_h("sp"),
            registry_snapshot_hash=_h("r"),
            train_matrix_hash=_h("m"),
            train_feature_view_hash=_h("fv"),
            feature_set_hash=_h("fs"),
            feature_registry_hash=_h("fr"),
            candidate_set=_CS,
            ordered_members=reordered,
        )


def test_candidate_order_equals_accepted_candidate_set() -> None:
    plan = _synth_plan(2)
    assert tuple(c.config_hash for c in plan.configs) == _CS.ordered_config_hashes
    assert all(c.parameter_space_hash == plan.parameter_space_hash for c in plan.configs)
    assert [c.config_index for c in plan.configs] == list(range(len(plan.configs)))


# --------------------------------------------------------------------------- #
# member / config inventory failures (missing/extra/dup/reordered/substituted)
# --------------------------------------------------------------------------- #
def _valid_plan_kwargs(n: int = 4) -> dict:
    return _synth_plan(n).model_dump()


def test_member_inventory_failures() -> None:
    base = _valid_plan_kwargs(4)
    # missing (drop last member -> count mismatch / non-contiguous)
    d = {**base, "members": base["members"][:-1]}
    with pytest.raises(ValidationError):
        ExperimentPlan(**d)
    # extra member appended without fixing counts / index gap
    extra = dict(base["members"][-1])
    extra["member_index"] = 99
    d = {**base, "members": [*base["members"], extra]}
    with pytest.raises(ValidationError):
        ExperimentPlan(**d)
    # duplicated dataset_id (index still contiguous but dataset repeats)
    dup = [dict(m) for m in base["members"]]
    dup[1]["dataset_id"] = dup[0]["dataset_id"]
    with pytest.raises(ValidationError):
        ExperimentPlan(**{**base, "members": dup})
    # reordered (swap two members' positions -> index != position)
    rev = list(reversed([dict(m) for m in base["members"]]))
    with pytest.raises(ValidationError):
        ExperimentPlan(**{**base, "members": rev})
    # substituted member vector_hash -> plan_hash no longer binds
    sub = [dict(m) for m in base["members"]]
    sub[0]["vector_hash"] = _h("substituted")
    with pytest.raises(ValidationError):
        ExperimentPlan(**{**base, "members": sub})


def test_config_inventory_failures() -> None:
    base = _valid_plan_kwargs(2)
    # missing config
    with pytest.raises(ValidationError):
        ExperimentPlan(**{**base, "configs": base["configs"][:-1]})
    # duplicated config_hash
    dup = [dict(c) for c in base["configs"]]
    dup[1]["config_hash"] = dup[0]["config_hash"]
    with pytest.raises(ValidationError):
        ExperimentPlan(**{**base, "configs": dup})
    # reordered configs (index != position)
    rev = list(reversed([dict(c) for c in base["configs"]]))
    with pytest.raises(ValidationError):
        ExperimentPlan(**{**base, "configs": rev})
    # payload-forged config_hash -> plan_hash no longer binds
    forged = [dict(c) for c in base["configs"]]
    forged[0]["config_hash"] = _h("forged-config")
    with pytest.raises(ValidationError):
        ExperimentPlan(**{**base, "configs": forged})
    # config parameter_space_hash mismatch with plan
    mism = [dict(c) for c in base["configs"]]
    mism[0]["parameter_space_hash"] = _h("other-space")
    with pytest.raises(ValidationError):
        ExperimentPlan(**{**base, "configs": mism})


# --------------------------------------------------------------------------- #
# per-field hash sensitivity (identity / member / config / count / order)
# --------------------------------------------------------------------------- #
def test_altering_any_identity_changes_plan_hash() -> None:
    plan = _synth_plan(3)
    base_kwargs = {
        "schema_version": plan.schema_version,
        "epoch": plan.epoch,
        "snapshot_hash": plan.snapshot_hash,
        "split_manifest_hash": plan.split_manifest_hash,
        "registry_snapshot_hash": plan.registry_snapshot_hash,
        "train_matrix_hash": plan.train_matrix_hash,
        "train_feature_view_hash": plan.train_feature_view_hash,
        "feature_set_hash": plan.feature_set_hash,
        "feature_registry_hash": plan.feature_registry_hash,
        "gatk_registry_hash": plan.gatk_registry_hash,
        "parameter_space_hash": plan.parameter_space_hash,
        "experiment_parameter_policy_hash": plan.experiment_parameter_policy_hash,
        "candidate_set_hash": plan.candidate_set_hash,
        "members": plan.members,
        "configs": plan.configs,
        "train_member_count": plan.train_member_count,
        "candidate_count": plan.candidate_count,
        "logical_job_count": plan.logical_job_count,
    }
    for field in (
        "snapshot_hash",
        "split_manifest_hash",
        "registry_snapshot_hash",
        "train_matrix_hash",
        "train_feature_view_hash",
        "feature_set_hash",
        "feature_registry_hash",
        "gatk_registry_hash",
        "parameter_space_hash",
        "experiment_parameter_policy_hash",
        "candidate_set_hash",
    ):
        mutated = {**base_kwargs, field: _h(f"mutated:{field}")}
        assert compute_plan_hash(**mutated) != plan.plan_hash, field
    # epoch and counts also bind
    assert compute_plan_hash(**{**base_kwargs, "epoch": 2}) != plan.plan_hash


def test_reordering_members_changes_plan_hash() -> None:
    plan = _synth_plan(3)
    reordered = (plan.members[1], plan.members[0], plan.members[2])
    got = compute_plan_hash(
        schema_version=plan.schema_version,
        epoch=plan.epoch,
        snapshot_hash=plan.snapshot_hash,
        split_manifest_hash=plan.split_manifest_hash,
        registry_snapshot_hash=plan.registry_snapshot_hash,
        train_matrix_hash=plan.train_matrix_hash,
        train_feature_view_hash=plan.train_feature_view_hash,
        feature_set_hash=plan.feature_set_hash,
        feature_registry_hash=plan.feature_registry_hash,
        gatk_registry_hash=plan.gatk_registry_hash,
        parameter_space_hash=plan.parameter_space_hash,
        experiment_parameter_policy_hash=plan.experiment_parameter_policy_hash,
        candidate_set_hash=plan.candidate_set_hash,
        members=reordered,
        configs=plan.configs,
        train_member_count=plan.train_member_count,
        candidate_count=plan.candidate_count,
        logical_job_count=plan.logical_job_count,
    )
    assert got != plan.plan_hash


# --------------------------------------------------------------------------- #
# job_key formula + logical-job enumeration
# --------------------------------------------------------------------------- #
def test_job_key_matches_documented_formula() -> None:
    plan = _synth_plan(2)
    m, c = plan.members[0], plan.configs[0]
    content = {
        "plan_hash": plan.plan_hash,
        "member_index": m.member_index,
        "dataset_id": m.dataset_id,
        "profile_id": m.profile_id,
        "content_hash": m.content_hash,
        "feature_values_hash": m.feature_values_hash,
        "config_index": c.config_index,
        "config_hash": c.config_hash,
    }
    expected = sha256_hex(JOB_KEY_DOMAIN.encode("utf-8") + canonical_json_bytes(content))
    assert next(iter(iter_logical_jobs(plan))).job_key == expected


def test_logical_jobs_are_ordered_unique_and_count_exact() -> None:
    plan = _synth_plan(4)
    jobs = list(iter_logical_jobs(plan))
    assert len(jobs) == plan.logical_job_count == plan.train_member_count * plan.candidate_count
    keys = [j.job_key for j in jobs]
    assert len(set(keys)) == len(keys)  # unique
    # member-major then config-index order
    order = [(j.member_index, j.config_index) for j in jobs]
    expected_order = [
        (mi, ci) for mi in range(plan.train_member_count) for ci in range(plan.candidate_count)
    ]
    assert order == expected_order
    # repeatable
    assert logical_job_keys(plan) == tuple(keys)
    assert logical_job_keys(_synth_plan(4)) == tuple(keys)


def test_altering_any_job_input_changes_job_key() -> None:
    plan = _synth_plan(2)
    m, c = plan.members[0], plan.configs[0]
    base = {
        "plan_hash": plan.plan_hash,
        "member_index": m.member_index,
        "dataset_id": m.dataset_id,
        "profile_id": m.profile_id,
        "content_hash": m.content_hash,
        "feature_values_hash": m.feature_values_hash,
        "config_index": c.config_index,
        "config_hash": c.config_hash,
    }
    ref = compute_job_key(**base)
    for field, value in (
        ("plan_hash", _h("other-plan")),
        ("member_index", 99),
        ("dataset_id", "other-ds"),
        ("profile_id", "other-profile"),
        ("content_hash", _h("other-content")),
        ("feature_values_hash", _h("other-fvh")),
        ("config_index", 99),
        ("config_hash", _h("other-config")),
    ):
        assert compute_job_key(**{**base, field: value}) != ref, field


# --------------------------------------------------------------------------- #
# no nondeterministic / leakage / execution data in the identity contracts
# --------------------------------------------------------------------------- #
def test_contracts_carry_no_nondeterministic_or_execution_fields() -> None:
    assert set(ExperimentPlan.model_fields) == {
        "schema_version",
        "epoch",
        "partition",
        "snapshot_hash",
        "split_manifest_hash",
        "registry_snapshot_hash",
        "train_matrix_hash",
        "train_feature_view_hash",
        "feature_set_hash",
        "feature_registry_hash",
        "gatk_registry_hash",
        "parameter_space_hash",
        "experiment_parameter_policy_hash",
        "candidate_set_hash",
        "members",
        "configs",
        "train_member_count",
        "candidate_count",
        "logical_job_count",
        "plan_hash",
    }
    assert set(ExperimentPlanMember.model_fields) == {
        "dataset_id",
        "profile_id",
        "content_hash",
        "feature_values_hash",
        "vector_hash",
        "member_index",
    }
    assert set(ExperimentPlanConfig.model_fields) == {
        "config_index",
        "config_hash",
        "parameter_space_hash",
    }
    # extra=forbid means a timestamp/uuid/score/path field can never be attached.
    with pytest.raises(ValidationError):
        ExperimentPlan(**{**_synth_plan(2).model_dump(), "created_at": "now"})


# --------------------------------------------------------------------------- #
# synthetic non-75 snapshots consumed verbatim with derived counts
# --------------------------------------------------------------------------- #
def test_two_synthetic_snapshots_derive_counts_from_actual_membership() -> None:
    # snapshot A: 3 train members (uneven chromosomes); snapshot B: 7 train members.
    plan_a = _synth_plan(3)
    plan_b = _synth_plan(7)
    assert plan_a.train_member_count == 3
    assert plan_b.train_member_count == 7
    cc = len(_CS.configs)
    assert plan_a.logical_job_count == 3 * cc
    assert plan_b.logical_job_count == 7 * cc
    assert plan_a.plan_hash != plan_b.plan_hash
    assert len(logical_job_keys(plan_a)) == 3 * cc
    assert len(logical_job_keys(plan_b)) == 7 * cc
    assert plan_a.schema_version == PLAN_SCHEMA_VERSION
