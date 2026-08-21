"""F3-B genuine synthetic-snapshot behavioral tests (real FrozenSnapshot, non-75).

Exercises the same private verified-input assembly path
(``accepted_plan._build_plan_from_verified_inputs``) that ``build_accepted_experiment_plan``
uses, driven by real ``FrozenSnapshot`` objects with uneven chromosome and partition
distributions (neither containing 75 members). Proves the plan consumes snapshot TRAIN
membership verbatim, excludes validation/test, derives counts from actual membership, and
rejects malformed feature-view membership — with no synthetic/partition/trust override exposed
by the production API.
"""

from __future__ import annotations

import inspect

import pytest
from pydantic import ValidationError

from minos_engine.common.hashing import sha256_hex
from minos_engine.experiments import accepted_plan as AP
from minos_engine.experiments.accepted_plan import (
    AcceptedExperimentPlanError,
    _build_plan_from_verified_inputs,
    build_accepted_experiment_plan,
)
from minos_engine.experiments.candidates import generate_accepted_candidate_set
from minos_engine.experiments.plan import iter_logical_jobs
from minos_engine.layer2.features.extraction import FrozenSnapshot, SnapshotMember
from minos_engine.layer2.features.feature_view import (
    FeatureViewMember,
    build_feature_view_manifest,
)

_CS = generate_accepted_candidate_set()


def _h(label: str) -> str:
    return sha256_hex(label.encode())


# (dataset_id, partition, chromosome) — uneven chromosome + partition distributions.
_SNAPSHOT_A = [
    ("dsA1", "train", "chr18"),
    ("dsA2", "train", "chr18"),
    ("dsA3", "train", "chr19"),
    ("dsA4", "train", "chr22"),
    ("dsA5", "validation", "chr20"),
    ("dsA6", "validation", "chr21"),
    ("dsA7", "test", "chr18"),
    ("dsA8", "test", "chr19"),
    ("dsA9", "test", "chr20"),
]  # total 9, train 4, validation 2, test 3
_SNAPSHOT_B = [
    ("dsB1", "train", "chr22"),
    ("dsB2", "train", "chr22"),
    ("dsB3", "validation", "chr18"),
    ("dsB4", "validation", "chr18"),
    ("dsB5", "validation", "chr19"),
    ("dsB6", "validation", "chr20"),
    ("dsB7", "validation", "chr21"),
    ("dsB8", "validation", "chr21"),
    ("dsB9", "test", "chr18"),
    ("dsB10", "test", "chr19"),
    ("dsB11", "test", "chr20"),
]  # total 11, train 2, validation 6, test 3


def _member(ds: str, partition: str, chrom: str) -> SnapshotMember:
    return SnapshotMember(
        dataset_id=ds,
        profile_id=f"profile-{ds}",
        partition=partition,  # type: ignore[arg-type]
        content_hash=_h(f"content:{ds}"),
        feature_values_hash=_h(f"fvh:{ds}"),
        profile_sha256=_h(f"sha:{ds}"),
        chromosome=chrom,  # type: ignore[arg-type]
    )


def _snapshot(spec: list[tuple[str, str, str]]) -> FrozenSnapshot:
    return FrozenSnapshot(
        epoch=1,
        split_manifest_hash=_h("split"),
        registry_snapshot_hash=_h("reg"),
        members=tuple(_member(ds, part, chrom) for ds, part, chrom in spec),
    )


def _train_feature_view(snapshot: FrozenSnapshot):
    train = snapshot.members_for("train")  # ordered by dataset_id
    fv_members = tuple(
        FeatureViewMember(
            dataset_id=m.dataset_id,
            member_index=i,
            vector_hash=_h(f"vec:{m.dataset_id}"),
            feature_values_hash=m.feature_values_hash,
        )
        for i, m in enumerate(train)
    )
    return build_feature_view_manifest(
        epoch=1,
        partition="train",
        snapshot_hash=snapshot.snapshot_hash,
        split_manifest_hash=snapshot.split_manifest_hash,
        registry_snapshot_hash=snapshot.registry_snapshot_hash,
        matrix_hash=_h("matrix"),
        artifact_sha256=_h("artifact"),
        row_count=len(train),
        members=fv_members,
        feature_set=None,
    )


@pytest.mark.parametrize(
    ("spec", "expected_train"),
    [(_SNAPSHOT_A, 4), (_SNAPSHOT_B, 2)],
)
def test_synthetic_snapshot_consumes_train_membership_verbatim(
    spec: list[tuple[str, str, str]], expected_train: int
) -> None:
    snapshot = _snapshot(spec)
    fv = _train_feature_view(snapshot)
    plan = _build_plan_from_verified_inputs(snapshot, fv, _CS)

    train_ds = {ds for ds, part, _ in spec if part == "train"}
    validation_ds = {ds for ds, part, _ in spec if part == "validation"}
    test_ds = {ds for ds, part, _ in spec if part == "test"}
    plan_ds = {m.dataset_id for m in plan.members}

    # 1) only the exact train members; 2/3) validation and test never enter.
    assert plan_ds == train_ds
    assert not (plan_ds & validation_ds)
    assert not (plan_ds & test_ds)
    # 4) verbatim, not a percentage of the total membership.
    assert plan.train_member_count == expected_train == len(train_ds)
    assert plan.train_member_count != round(len(spec) * 0.7)  # not an implicit 70% split
    # 5) matrix order follows the accepted feature-view member indices.
    assert [m.member_index for m in plan.members] == list(range(expected_train))
    assert [m.dataset_id for m in plan.members] == [fvm.dataset_id for fvm in fv.members]
    # 6/7/8) derived counts.
    assert plan.candidate_count == len(_CS.configs)
    assert plan.logical_job_count == expected_train * len(_CS.configs)
    # 9) the job iterator references only train datasets.
    job_ds = {j.dataset_id for j in iter_logical_jobs(plan)}
    assert job_ds == train_ds
    assert not (job_ds & (validation_ds | test_ds))


def test_changing_partition_assignment_changes_snapshot_and_breaks_binding() -> None:
    snapshot = _snapshot(_SNAPSHOT_A)
    fv = _train_feature_view(snapshot)
    plan = _build_plan_from_verified_inputs(snapshot, fv, _CS)

    # flip one train member to validation -> a genuinely different snapshot identity.
    flipped_spec = [
        (ds, "validation" if ds == "dsA1" else part, chrom) for ds, part, chrom in _SNAPSHOT_A
    ]
    flipped = _snapshot(flipped_spec)
    assert flipped.snapshot_hash != snapshot.snapshot_hash

    # the OLD feature view (bound to the old snapshot) cannot bind the flipped snapshot.
    with pytest.raises(AcceptedExperimentPlanError):
        _build_plan_from_verified_inputs(flipped, fv, _CS)

    # a fresh plan for the flipped snapshot has fewer train members and a different plan_hash.
    flipped_plan = _build_plan_from_verified_inputs(flipped, _train_feature_view(flipped), _CS)
    assert flipped_plan.train_member_count == 3
    assert flipped_plan.plan_hash != plan.plan_hash


def test_malformed_feature_view_membership_is_rejected() -> None:
    snapshot = _snapshot(_SNAPSHOT_A)
    train = snapshot.members_for("train")

    def _fv(members: tuple[FeatureViewMember, ...]):
        return build_feature_view_manifest(
            epoch=1,
            partition="train",
            snapshot_hash=snapshot.snapshot_hash,
            split_manifest_hash=snapshot.split_manifest_hash,
            registry_snapshot_hash=snapshot.registry_snapshot_hash,
            matrix_hash=_h("matrix"),
            artifact_sha256=_h("artifact"),
            row_count=len(members),
            members=members,
            feature_set=None,
        )

    def _fvm(i: int, m: SnapshotMember, **over) -> FeatureViewMember:
        base = {
            "dataset_id": m.dataset_id,
            "member_index": i,
            "vector_hash": _h(f"vec:{m.dataset_id}"),
            "feature_values_hash": m.feature_values_hash,
        }
        base.update(over)
        return FeatureViewMember(**base)

    full = tuple(_fvm(i, m) for i, m in enumerate(train))

    # missing a train member
    missing = tuple(_fvm(i, m) for i, m in enumerate(train[:-1]))
    with pytest.raises(AcceptedExperimentPlanError):
        _build_plan_from_verified_inputs(snapshot, _fv(missing), _CS)

    # an extra dataset not in snapshot train membership
    extra = (
        *full,
        FeatureViewMember(
            dataset_id="not-a-train-dataset",
            member_index=len(full),
            vector_hash=_h("vec:extra"),
            feature_values_hash=_h("fvh:extra"),
        ),
    )
    with pytest.raises(AcceptedExperimentPlanError):
        _build_plan_from_verified_inputs(snapshot, _fv(extra), _CS)

    # substituted feature_values_hash for the first member
    substituted = (_fvm(0, train[0], feature_values_hash=_h("substituted")), *full[1:])
    with pytest.raises(AcceptedExperimentPlanError):
        _build_plan_from_verified_inputs(snapshot, _fv(substituted), _CS)

    # reordered members (each keeps its original member_index but positions are swapped) are
    # rejected by the feature-view contract itself (members must be contiguous in order).
    reordered = (full[1], full[0], *full[2:])
    with pytest.raises(ValidationError):
        _fv(reordered)


def test_no_synthetic_or_partition_override_in_production_api() -> None:
    # the verified-input helper is private and unexported.
    assert "_build_plan_from_verified_inputs" not in AP.__all__
    assert not hasattr(AP, "build_plan_from_verified_inputs")
    # the production constructor takes no arguments (no partition/trust override).
    assert list(inspect.signature(build_accepted_experiment_plan).parameters) == []
