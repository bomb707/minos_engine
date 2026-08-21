"""F3-D pure logical attack matrix (no database).

The verifier's comparison core is pure: it consumes an immutable ``PersistedGraph`` snapshot and
the accepted contracts. These tests build a **controlled immutable representation** of a valid
persisted graph for the accepted plan, corrupt exactly one field, and assert the specific named
check that must fail — including corruptions that production database constraints make
unconstructable (so no constraint is ever weakened to manufacture the state).

Relational attacks that genuinely require PostgreSQL (forged job rows, tampered artifact bytes,
non-train membership in the live upstream, and the non-mutation proof) live in
``tests/integration/layer2_db/test_l2f_harness_verifier.py``.
"""

from __future__ import annotations

import dataclasses
import hashlib
from typing import Any

import pytest

from minos_engine.common.canonical_json import canonical_json_bytes
from minos_engine.experiments.accepted_plan import build_accepted_experiment_plan
from minos_engine.experiments.candidates import generate_accepted_candidate_set
from minos_engine.storage import l2f_harness_verifier as HV
from minos_engine.storage.l2f_config_publisher import (
    CONFIG_ARTIFACT_KIND,
    CONFIG_ARTIFACT_MEDIA_TYPE,
)
from minos_engine.storage.l2f_harness_verifier import (
    CHECK_NAMES,
    STATUS_FAIL,
    STATUS_PASS,
    PersistedConfig,
    PersistedGraph,
    PersistedJob,
    PersistedMember,
    PersistedPayload,
    UpstreamMember,
    _build_result,
    verify_accepted_experiment_harness,
)
from minos_engine.storage.l2f_migration_contract import L2F_CONFIG_PAYLOAD_SCHEMA

_PLAN = build_accepted_experiment_plan()
_CS = generate_accepted_candidate_set()

_SNAP_ID = "00000000-0000-0000-0000-0000000000s1"
_MAT_ID = "00000000-0000-0000-0000-0000000000m1"
_FS_ID = "00000000-0000-0000-0000-0000000000f1"


def _member_id(i: int) -> str:
    return f"11111111-0000-0000-0000-{i:012d}"


def _config_id(i: int) -> str:
    return f"22222222-0000-0000-0000-{i:012d}"


def _payload_id(i: int) -> str:
    return f"33333333-0000-0000-0000-{i:012d}"


def _valid_graph(*, job_count: int = 0) -> PersistedGraph:
    """A controlled immutable representation of a fully valid persisted graph for _PLAN."""
    plan_row: dict[str, Any] = {
        "id": "44444444-0000-0000-0000-000000000001",
        "profile_snapshot_id": _SNAP_ID,
        "train_feature_matrix_id": _MAT_ID,
        "feature_set_id": _FS_ID,
        "partition": "train",
        "train_member_count": _PLAN.train_member_count,
        "candidate_count": _PLAN.candidate_count,
        "logical_job_count": _PLAN.logical_job_count,
        "plan_hash": _PLAN.plan_hash,
    }
    for col in HV._PLAN_IDENTITY_COLUMNS:
        plan_row[col] = getattr(_PLAN, col)

    members = tuple(
        PersistedMember(
            member_index=m.member_index,
            plan_member_id=_member_id(m.member_index),
            profile_snapshot_member_id=f"psm-{m.dataset_id}",
            feature_matrix_member_id=f"fmm-{m.dataset_id}",
            bam_profile_id=f"bam-{m.dataset_id}",
            dataset_registry_id=f"dsr-{m.dataset_id}",
            partition="train",
            feature_values_hash=m.feature_values_hash,
            dataset_id=m.dataset_id,
            profile_id=m.profile_id,
            content_hash=m.content_hash,
            vector_hash=m.vector_hash,
        )
        for m in _PLAN.members
    )
    configs = tuple(
        PersistedConfig(
            config_index=c.config_index,
            plan_config_id=_config_id(c.config_index),
            config_payload_id=_payload_id(c.config_index),
            config_hash=c.config_hash,
            parameter_space_hash=c.parameter_space_hash,
        )
        for c in _PLAN.configs
    )
    payloads = []
    for i, cand in enumerate(_CS.configs):
        canonical = canonical_json_bytes(cand.effective_config)
        payloads.append(
            PersistedPayload(
                config_payload_id=_payload_id(i),
                config_hash=cand.config_hash,
                parameter_space_hash=_PLAN.parameter_space_hash,
                schema_version=L2F_CONFIG_PAYLOAD_SCHEMA,
                media_type=CONFIG_ARTIFACT_MEDIA_TYPE,
                artifact_id=f"art-{i}",
                artifact_sha256=cand.config_hash,
                artifact_uri=f"file:///cfgroot/{cand.config_hash}.json",
                artifact_size_bytes=len(canonical),
                artifact_provenance=CONFIG_ARTIFACT_KIND,
                file_sha256=hashlib.sha256(canonical).hexdigest(),
                file_size_bytes=len(canonical),
            )
        )
    upstream = tuple(
        UpstreamMember(
            dataset_id=m.dataset_id,
            profile_id=m.profile_id,
            content_hash=m.content_hash,
            snapshot_feature_values_hash=m.feature_values_hash,
            matrix_feature_values_hash=m.feature_values_hash,
            vector_hash=m.vector_hash,
            member_index=m.member_index,
        )
        for m in _PLAN.members
    )
    logical = list(HV.iter_logical_jobs(_PLAN))
    jobs = tuple(
        PersistedJob(
            job_id=f"job-{k}",
            job_key=logical[k].job_key,
            status="PENDING",
            claimed_by=None,
            claimed_at_is_null=True,
            plan_member_id=_member_id(logical[k].member_index),
            plan_config_id=_config_id(logical[k].config_index),
            member_index=logical[k].member_index,
            config_index=logical[k].config_index,
        )
        for k in range(job_count)
    )
    return PersistedGraph(
        plan_id=str(plan_row["id"]),
        plan_row=plan_row,
        upstream_ids={
            "profile_snapshot_id": _SNAP_ID,
            "train_feature_matrix_id": _MAT_ID,
            "feature_set_id": _FS_ID,
        },
        members=members,
        configs=configs,
        payloads=tuple(payloads),
        jobs=jobs,
        upstream_train=upstream,
        upstream_nontrain_dataset_ids=("held-validation-1", "held-test-1"),
        matrix_row_count=_PLAN.train_member_count,
        legacy_profile_overlap=0,
        legacy_gatk_config_overlap=0,
        fingerprint_before="fp",
        fingerprint_after="fp",
    )


def _result(graph: PersistedGraph, plan: Any = _PLAN) -> Any:
    return _build_result(plan, _CS, graph)


def _replace_member(graph: PersistedGraph, index: int, **changes: Any) -> PersistedGraph:
    members = list(graph.members)
    members[index] = dataclasses.replace(members[index], **changes)
    return dataclasses.replace(graph, members=tuple(members))


def _replace_config(graph: PersistedGraph, index: int, **changes: Any) -> PersistedGraph:
    configs = list(graph.configs)
    configs[index] = dataclasses.replace(configs[index], **changes)
    return dataclasses.replace(graph, configs=tuple(configs))


# --------------------------------------------------------------------------- #
# baseline: a valid graph verifies (with and without jobs)
# --------------------------------------------------------------------------- #
def test_valid_graph_with_no_jobs_passes_and_reports_all_missing() -> None:
    r = _result(_valid_graph(job_count=0))
    assert r.status == STATUS_PASS and r.failures == ()
    assert r.persisted_job_count == 0
    # partial (here: zero) enqueue is VALID — missing jobs are reported, never a failure.
    assert r.missing_job_count == _PLAN.logical_job_count
    assert set(r.checks) == set(CHECK_NAMES)
    assert tuple(r.checks) == CHECK_NAMES  # deterministic order
    assert all(r.checks.values())


def test_valid_graph_with_partial_jobs_passes() -> None:
    r = _result(_valid_graph(job_count=7))
    assert r.status == STATUS_PASS and r.failures == ()
    assert r.persisted_job_count == 7
    assert r.missing_job_count == _PLAN.logical_job_count - 7


def test_valid_graph_with_complete_jobs_passes() -> None:
    r = _result(_valid_graph(job_count=_PLAN.logical_job_count))
    assert r.status == STATUS_PASS
    assert r.persisted_job_count == _PLAN.logical_job_count
    assert r.missing_job_count == 0


def test_counts_derive_from_plan_not_constants() -> None:
    r = _result(_valid_graph())
    assert r.logical_job_count == _PLAN.train_member_count * _PLAN.candidate_count
    assert r.plan_hash == _PLAN.plan_hash
    assert r.candidate_set_hash == _PLAN.candidate_set_hash


# --------------------------------------------------------------------------- #
# attacks 1-9, 13, 15, 17 (pure; controlled immutable representation)
# --------------------------------------------------------------------------- #
class _ForgedPlan:
    """A controlled immutable representation of an ExperimentPlan with one forged field.

    ``ExperimentPlan`` structurally forbids a non-binding ``plan_hash``, so attack 1 can only be
    expressed through such a representation — never by weakening the model or a DB constraint.
    """

    def __init__(self, plan: Any, **overrides: Any) -> None:
        self._plan = plan
        self._overrides = overrides

    def __getattr__(self, name: str) -> Any:
        if name in self._overrides:
            return self._overrides[name]
        return getattr(self._plan, name)


def test_attack01_wrong_accepted_plan_hash() -> None:
    forged = _ForgedPlan(_PLAN, plan_hash="a" * 64)
    graph = _valid_graph()
    r = _result(graph, plan=forged)
    assert r.status == STATUS_FAIL
    assert "plan_identity_self_binding" in r.failures


def test_attack02_wrong_candidate_set_hash_in_persisted_row() -> None:
    graph = _valid_graph()
    row = dict(graph.plan_row)
    row["candidate_set_hash"] = "b" * 64
    r = _result(dataclasses.replace(graph, plan_row=row))
    assert r.status == STATUS_FAIL
    assert "plan_row_identity_hashes" in r.failures


@pytest.mark.parametrize("field", ["dataset_id", "profile_id", "content_hash"])
def test_attack03_wrong_train_member_identity(field: str) -> None:
    graph = _replace_member(_valid_graph(), 0, **{field: f"WRONG-{field}"})
    r = _result(graph)
    assert r.status == STATUS_FAIL
    assert "member_inventory_exact" in r.failures


def test_attack04_wrong_feature_values_hash() -> None:
    r = _result(_replace_member(_valid_graph(), 1, feature_values_hash="c" * 64))
    assert r.status == STATUS_FAIL
    assert "member_inventory_exact" in r.failures


def test_attack05_wrong_vector_hash() -> None:
    r = _result(_replace_member(_valid_graph(), 2, vector_hash="d" * 64))
    assert r.status == STATUS_FAIL
    assert "member_inventory_exact" in r.failures


def test_attack06_wrong_member_index() -> None:
    r = _result(_replace_member(_valid_graph(), 3, member_index=99))
    assert r.status == STATUS_FAIL
    assert "member_inventory_exact" in r.failures


def test_attack07_wrong_config_hash() -> None:
    r = _result(_replace_config(_valid_graph(), 0, config_hash="e" * 64))
    assert r.status == STATUS_FAIL
    assert "config_inventory_exact" in r.failures


def test_attack08_wrong_parameter_space_hash() -> None:
    r = _result(_replace_config(_valid_graph(), 1, parameter_space_hash="f" * 64))
    assert r.status == STATUS_FAIL
    assert "config_inventory_exact" in r.failures


def test_attack09_reordered_plan_configs() -> None:
    graph = _valid_graph()
    configs = list(graph.configs)
    # swap two configs' persisted identities while keeping their config_index values in place:
    # the stored order no longer matches the accepted candidate order.
    a, b = configs[0], configs[1]
    configs[0] = dataclasses.replace(a, config_hash=b.config_hash)
    configs[1] = dataclasses.replace(b, config_hash=a.config_hash)
    r = _result(dataclasses.replace(graph, configs=tuple(configs)))
    assert r.status == STATUS_FAIL
    assert "config_inventory_exact" in r.failures


def test_attack13_job_outside_accepted_logical_universe() -> None:
    """A job whose bound member/config indices fall outside the accepted logical-job space is
    unconstructable relationally (the composite FKs pin plan membership), so it is expressed as a
    controlled immutable representation."""
    graph = _valid_graph(job_count=1)
    jobs = (
        dataclasses.replace(
            graph.jobs[0],
            member_index=_PLAN.train_member_count + 5,
            config_index=_PLAN.candidate_count + 5,
        ),
    )
    r = _result(dataclasses.replace(graph, jobs=jobs))
    assert r.status == STATUS_FAIL
    assert "job_indices_valid_subset" in r.failures


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("artifact_uri", "file:///cfgroot/wrong-name.json"),
        ("artifact_size_bytes", 3),
        ("artifact_size_bytes", None),
        ("artifact_provenance", "not-l2f"),
        ("artifact_sha256", "1" * 64),
        ("schema_version", "l2f-config-payload-v0"),
        ("media_type", "application/octet-stream"),
    ],
)
def test_attack15_artifact_metadata_mismatch(field: str, value: Any) -> None:
    graph = _valid_graph()
    payloads = list(graph.payloads)
    payloads[0] = dataclasses.replace(payloads[0], **{field: value})
    r = _result(dataclasses.replace(graph, payloads=tuple(payloads)))
    assert r.status == STATUS_FAIL
    assert "config_payload_bytes_canonical" in r.failures


def test_attack14_noncanonical_config_bytes_pure() -> None:
    graph = _valid_graph()
    payloads = list(graph.payloads)
    payloads[0] = dataclasses.replace(payloads[0], file_sha256="9" * 64)
    r = _result(dataclasses.replace(graph, payloads=tuple(payloads)))
    assert r.status == STATUS_FAIL
    assert "config_payload_bytes_canonical" in r.failures


def test_attack16_nontrain_member_in_train_plan() -> None:
    graph = _replace_member(_valid_graph(), 0, partition="validation")
    r = _result(graph)
    assert r.status == STATUS_FAIL
    assert "no_nontrain_or_truth_data" in r.failures


def test_attack16b_plan_dataset_also_held_as_nontrain() -> None:
    graph = _valid_graph()
    graph = dataclasses.replace(graph, upstream_nontrain_dataset_ids=(_PLAN.members[0].dataset_id,))
    r = _result(graph)
    assert r.status == STATUS_FAIL
    assert "no_nontrain_or_truth_data" in r.failures


@pytest.mark.parametrize(
    "provenance",
    ["minos:truth-labels", "minos:mutation-catalog", "minos:score-report", "happy-vcf-output"],
)
def test_attack17_truth_or_mutation_bearing_artifact(provenance: str) -> None:
    """A truth/mutation/score-bearing artifact cannot be registered through the F3-C1 boundary
    (its get-or-verify pins the CONFIG provenance), so it is expressed purely."""
    graph = _valid_graph()
    payloads = list(graph.payloads)
    payloads[0] = dataclasses.replace(payloads[0], artifact_provenance=provenance)
    r = _result(dataclasses.replace(graph, payloads=tuple(payloads)))
    assert r.status == STATUS_FAIL
    assert "no_nontrain_or_truth_data" in r.failures
    assert "config_payload_bytes_canonical" in r.failures


def test_attack18_mutating_verification_is_detected() -> None:
    """If verification itself changed any row count, timestamp, status, claim field or file, the
    before/after state fingerprints diverge and the named self-check fails."""
    graph = dataclasses.replace(_valid_graph(), fingerprint_after="MUTATED")
    r = _result(graph)
    assert r.status == STATUS_FAIL
    assert "verification_non_mutating" in r.failures


# --------------------------------------------------------------------------- #
# additional pure checks: upstream exactness, legacy exclusion, job bindings
# --------------------------------------------------------------------------- #
def test_extra_upstream_train_member_rejected() -> None:
    graph = _valid_graph()
    extra = UpstreamMember(
        dataset_id="extra-ds",
        profile_id="extra-profile",
        content_hash="7" * 64,
        snapshot_feature_values_hash="7" * 64,
        matrix_feature_values_hash="7" * 64,
        vector_hash="7" * 64,
        member_index=_PLAN.train_member_count,
    )
    r = _result(dataclasses.replace(graph, upstream_train=(*graph.upstream_train, extra)))
    assert "upstream_membership_exact" in r.failures


def test_matrix_row_count_inconsistent_rejected() -> None:
    graph = dataclasses.replace(_valid_graph(), matrix_row_count=_PLAN.train_member_count + 1)
    assert "upstream_membership_exact" in _result(graph).failures


def test_legacy_overlap_rejected() -> None:
    assert (
        "legacy_tables_excluded"
        in _result(dataclasses.replace(_valid_graph(), legacy_profile_overlap=1)).failures
    )
    assert (
        "legacy_tables_excluded"
        in _result(dataclasses.replace(_valid_graph(), legacy_gatk_config_overlap=1)).failures
    )


def test_duplicate_job_key_rejected() -> None:
    graph = _valid_graph(job_count=2)
    jobs = (graph.jobs[0], dataclasses.replace(graph.jobs[1], job_key=graph.jobs[0].job_key))
    assert "job_uniqueness" in _result(dataclasses.replace(graph, jobs=jobs)).failures


# --------------------------------------------------------------------------- #
# F4 status/claim consistency (replaces the pre-F4 "every job must be PENDING" rule)
# --------------------------------------------------------------------------- #
def _with_job(graph: PersistedGraph, **changes: Any) -> PersistedGraph:
    return dataclasses.replace(graph, jobs=(dataclasses.replace(graph.jobs[0], **changes),))


@pytest.mark.parametrize("status", ["PENDING", "CLAIMED", "RUNNING"])
def test_valid_f4_status_claim_combinations_accepted(status: str) -> None:
    """A correctly-claimed CLAIMED/RUNNING job and an unclaimed PENDING job are all valid now
    that F4 claiming exists."""
    graph = _valid_graph(job_count=1)
    if status == "PENDING":
        graph = _with_job(graph, status=status, claimed_by=None, claimed_at_is_null=True)
    else:
        graph = _with_job(graph, status=status, claimed_by="worker-1", claimed_at_is_null=False)
    r = _result(graph)
    assert r.checks["job_status_claim_consistency"] is True
    assert r.status == STATUS_PASS


@pytest.mark.parametrize(
    ("status", "claimed_by", "claimed_at_is_null"),
    [
        # PENDING must carry no claim metadata at all
        ("PENDING", "worker-1", True),
        ("PENDING", None, False),
        ("PENDING", "worker-1", False),
        # CLAIMED/RUNNING require BOTH a non-empty worker and a claimed_at
        ("CLAIMED", None, False),
        ("CLAIMED", "worker-1", True),
        ("CLAIMED", "   ", False),
        ("RUNNING", None, False),
        ("RUNNING", "worker-1", True),
        ("RUNNING", "", False),
    ],
)
def test_malformed_status_claim_combinations_rejected(
    status: str, claimed_by: str | None, claimed_at_is_null: bool
) -> None:
    graph = _with_job(
        _valid_graph(job_count=1),
        status=status,
        claimed_by=claimed_by,
        claimed_at_is_null=claimed_at_is_null,
    )
    r = _result(graph)
    assert r.status == STATUS_FAIL
    assert "job_status_claim_consistency" in r.failures


@pytest.mark.parametrize("status", ["SUCCEEDED", "FAILED", "CANCELLED"])
def test_terminal_states_remain_invalid_during_f4(status: str) -> None:
    """Terminal execution states are unreachable until F5; a job in one is invalid now."""
    graph = _with_job(
        _valid_graph(job_count=1), status=status, claimed_by="worker-1", claimed_at_is_null=False
    )
    r = _result(graph)
    assert r.status == STATUS_FAIL
    assert "job_status_claim_consistency" in r.failures


def test_derived_count_mismatch_rejected() -> None:
    graph = _valid_graph()
    row = dict(graph.plan_row)
    row["train_member_count"] = _PLAN.train_member_count + 1
    assert "derived_counts" in _result(dataclasses.replace(graph, plan_row=row)).failures


def test_upstream_uuid_binding_mismatch_rejected() -> None:
    graph = _valid_graph()
    row = dict(graph.plan_row)
    row["train_feature_matrix_id"] = "00000000-0000-0000-0000-00000000dead"
    assert (
        "plan_upstream_uuid_binding" in _result(dataclasses.replace(graph, plan_row=row)).failures
    )


# --------------------------------------------------------------------------- #
# public surface
# --------------------------------------------------------------------------- #
def test_public_entry_point_takes_no_arguments() -> None:
    import inspect

    assert list(inspect.signature(verify_accepted_experiment_harness).parameters) == []
    assert "verify_accepted_experiment_harness" in HV.__all__
    assert "_verify_experiment_harness_with_trust" not in HV.__all__
