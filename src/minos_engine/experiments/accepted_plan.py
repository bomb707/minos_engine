"""L2-F F3-B accepted no-override ExperimentPlan constructor + E5 prerequisite closure.

``build_accepted_experiment_plan()`` is the sole production boundary: it takes **no**
caller-selected identities, roots, partitions, gates, reports or manifests. It verifies the
pinned E5 gate closure, consumes the committed epoch-1 member manifest and E4 train metadata
verbatim, reconstructs and pins the train feature view, generates and independently verifies
the accepted candidate set, and assembles a deterministic ``ExperimentPlan``.

It performs NO PostgreSQL writes, artifact publication, job enqueueing, claiming, execution,
scoring, training, optimization or configuration selection. Prerequisite verification only
reads committed files + git history (read-only), exactly like the L2-E qualification.

The private ``_assemble_experiment_plan`` (in ``plan``) is a pure structural builder used by
tests with synthetic inventories; it is not exported and is not an accepted/trust boundary.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, ValidationError, model_validator

from minos_engine.common.errors import MinosEngineError
from minos_engine.experiments.candidates import (
    generate_accepted_candidate_set,
    verify_accepted_candidate_set,
)
from minos_engine.experiments.plan import (
    ExperimentPlan,
    ExperimentPlanMember,
    _assemble_experiment_plan,
)
from minos_engine.gates.verifier import load_gate
from minos_engine.layer2 import prerequisites as PRE
from minos_engine.layer2.features.extraction import load_accepted_epoch1_member_manifest
from minos_engine.layer2.features.feature_view import (
    FeatureViewMember,
    build_feature_view_manifest,
)
from minos_engine.qualification import git_tree as G
from minos_engine.qualification import layer2_feature_view_runner as R

if TYPE_CHECKING:
    from minos_engine.experiments.candidates import CandidateSet
    from minos_engine.layer2.features.extraction import FrozenSnapshot
    from minos_engine.layer2.features.feature_view import FeatureViewManifest

__all__ = [
    "E5ClosureError",
    "AcceptedExperimentPlanError",
    "verify_e5_prerequisite_closure",
    "build_accepted_experiment_plan",
]

_REPO_ROOT = Path(__file__).resolve().parents[3]
_MEMBER_MANIFEST = _REPO_ROOT / "manifests" / "profile_snapshot_epoch1_members.json"
_E4_TRAIN_REPORT = _REPO_ROOT / "reports" / "e4" / "L2E_E4_TRAIN_MATRIX.json"
_ACCEPTED_EPOCH = 1

_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_HEX40 = re.compile(r"^[0-9a-f]{40}$")


def _hex64(v: str) -> str:
    if not _HEX64.fullmatch(v):
        raise ValueError("must be a lowercase 64-character hex string")
    return v


def _hex40(v: str) -> str:
    if not _HEX40.fullmatch(v):
        raise ValueError("must be a lowercase 40-character hex string")
    return v


_Hex64 = Annotated[str, AfterValidator(_hex64)]
_Hex40 = Annotated[str, AfterValidator(_hex40)]
_STRICT_MODEL = ConfigDict(frozen=True, extra="forbid", strict=True)


class E5ClosureError(MinosEngineError):
    """The pinned E5 prerequisite closure did not verify."""


class AcceptedExperimentPlanError(MinosEngineError):
    """The accepted E4/snapshot inputs did not match the accepted trust anchors."""


def _require(cond: bool, msg: str) -> None:
    if not cond:
        raise AcceptedExperimentPlanError(msg)


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in pairs:
        if key in out:
            raise AcceptedExperimentPlanError(f"duplicate JSON key in E4 report: {key}")
        out[key] = value
    return out


# --------------------------------------------------------------------------- #
# E5 prerequisite closure
# --------------------------------------------------------------------------- #
def verify_e5_prerequisite_closure(root: Path | str | None = None) -> dict[str, bool]:
    """Verify the committed E5 gates against the pinned identities (fail-closed).

    Binds, for both FEATURE-VIEW-READY and FEATURE-MATRIX-FROZEN-1: exact accepted gate hash;
    PASS status + full contract checks (via the qualifier runner); pinned qualified source
    commit and tree; the accepted train feature-view hash; and the git ancestry chain
    E4-evidence -> E5-source -> E5-evidence -> HEAD with exact source/evidence trees. A locally
    regenerated PASS gate cannot satisfy the pinned hashes/ancestry, so it can never authorize
    F3-B. Returns the check map on success; raises :class:`E5ClosureError` otherwise.
    """
    base = Path(root) if root is not None else _REPO_ROOT
    fv_path = base / R.FEATURE_VIEW_READY_GATE_PATH
    fm_path = base / R.FEATURE_MATRIX_FROZEN_1_GATE_PATH

    fv = R.verify_feature_view_ready_gate(base, fv_path)
    fm = R.verify_feature_matrix_frozen_1_gate(base, fm_path)
    fv_gate = load_gate(fv_path)
    fm_gate = load_gate(fm_path)

    checks: dict[str, bool] = {
        # full contract checks (canonical integrity, PASS, tool version, mandatory checks,
        # source present/tree, descends-E4, HEAD-descends-source) from the qualifier runner.
        "feature_view_ready_contract_ok": fv.ok,
        "feature_matrix_frozen_1_contract_ok": fm.ok,
        # exact accepted gate hashes (not merely PASS).
        "feature_view_ready_gate_hash_pinned": fv.gate_hash == PRE.FEATURE_VIEW_READY_GATE_HASH,
        "feature_matrix_frozen_1_gate_hash_pinned": fm.gate_hash
        == PRE.FEATURE_MATRIX_FROZEN_1_GATE_HASH,
        # pinned qualified source commit + tree on both gates.
        "fv_source_commit_pinned": fv_gate.qualified_source_git_sha == PRE.E5_SOURCE_COMMIT,
        "fv_source_tree_pinned": fv_gate.qualified_source_tree_sha == PRE.E5_SOURCE_TREE,
        "fm_source_commit_pinned": fm_gate.qualified_source_git_sha == PRE.E5_SOURCE_COMMIT,
        "fm_source_tree_pinned": fm_gate.qualified_source_tree_sha == PRE.E5_SOURCE_TREE,
        # accepted train feature-view hash carried by the matrix gate.
        "train_feature_view_hash_pinned": fm_gate.input_hashes.get("train_feature_view_hash")
        == PRE.ACCEPTED_EPOCH1_TRAIN_FEATURE_VIEW_HASH,
        # git ancestry + tree chain: E4-evidence -> E5-source -> E5-evidence -> HEAD.
        "e5_source_is_commit": G.is_commit(base, PRE.E5_SOURCE_COMMIT),
        "e5_source_tree_matches": G.commit_tree_sha(base, PRE.E5_SOURCE_COMMIT)
        == PRE.E5_SOURCE_TREE,
        "e5_source_descends_e4_evidence": G.is_ancestor(
            base, PRE.E4_FEATURE_MATRIX_EVIDENCE_COMMIT, PRE.E5_SOURCE_COMMIT
        ),
        "e5_evidence_is_commit": G.is_commit(base, PRE.E5_EVIDENCE_COMMIT),
        "e5_evidence_tree_matches": G.commit_tree_sha(base, PRE.E5_EVIDENCE_COMMIT)
        == PRE.E5_EVIDENCE_TREE,
        "e5_evidence_descends_source": G.is_ancestor(
            base, PRE.E5_SOURCE_COMMIT, PRE.E5_EVIDENCE_COMMIT
        ),
        "head_descends_e5_evidence": G.is_ancestor(base, PRE.E5_EVIDENCE_COMMIT, "HEAD"),
    }
    if not all(checks.values()):
        failed = ", ".join(k for k, v in checks.items() if not v)
        raise E5ClosureError(f"E5 prerequisite closure failed: {failed}")
    return checks


# --------------------------------------------------------------------------- #
# strict E4 train feature-matrix metadata contract
# --------------------------------------------------------------------------- #
class _E4TrainMember(BaseModel):
    """One strict E4 train matrix member (verbatim, no coercion)."""

    model_config = _STRICT_MODEL

    dataset_id: str = Field(min_length=1)
    member_index: int = Field(ge=0)
    vector_hash: _Hex64
    feature_values_hash: _Hex64


class _E4TrainMatrixReport(BaseModel):
    """The committed E4 train feature-matrix metadata report, modelled with real types.

    Every field is present with its committed type; ``extra=forbid`` + strict reject unknown
    fields, bool-as-int, numeric strings, floats-for-ints, nulls and non-hex hashes. The
    fixed-value fields are ``Literal``-bound; the identity/count fields are typed here and
    cross-bound to the accepted pins by the constructor. Members must be contiguous in their
    committed order (no silent re-sort), with unique dataset ids and vector hashes.
    """

    # alias-only input: ONLY the canonical external JSON key ``schema`` is accepted (the internal
    # field name ``report_schema`` is not); ``serialize_by_alias`` re-emits the canonical key.
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True, serialize_by_alias=True)

    report_schema: Literal["l2e-e4-matrix-metadata-v1"] = Field(alias="schema")
    epoch: Literal[1]
    partition: Literal["train"]
    snapshot_hash: _Hex64
    split_manifest_hash: _Hex64
    registry_snapshot_hash: _Hex64
    matrix_hash: _Hex64
    artifact_sha256: _Hex64
    artifact_kind: Literal["l2e:feature-matrix-parquet"]
    artifact_media_type: Literal["application/vnd.apache.parquet"]
    artifact_mode: Literal["0o640"]
    parquet_schema_version: Literal["feature-matrix-parquet-v2"]
    root_mode: Literal["0o2750"]
    partition_gid: int = Field(ge=0)
    feature_set_hash: _Hex64
    registry_hash: _Hex64
    column_count: int = Field(ge=1)
    row_count: int = Field(ge=1)
    snapshot_member_count: int = Field(ge=1)
    git_head: _Hex40
    git_tree: _Hex40
    idempotent_replay: bool
    members: tuple[_E4TrainMember, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _members_contiguous_unique(self) -> _E4TrainMatrixReport:
        for i, m in enumerate(self.members):
            if m.member_index != i:
                raise ValueError(
                    f"E4 member indices must be contiguous in committed order: position {i} "
                    f"has member_index {m.member_index}"
                )
        if len({m.dataset_id for m in self.members}) != len(self.members):
            raise ValueError("duplicate E4 member dataset_id")
        if len({m.vector_hash for m in self.members}) != len(self.members):
            raise ValueError("duplicate E4 member vector_hash")
        return self


def _parse_e4_train_report() -> _E4TrainMatrixReport:
    """Strictly parse the committed E4 train report (duplicate-key + schema rejection).

    All JSON / schema / Pydantic failures surface as :class:`AcceptedExperimentPlanError`.
    """
    try:
        raw = _E4_TRAIN_REPORT.read_text(encoding="utf-8")
    except OSError as exc:  # pragma: no cover - committed file is always present
        raise AcceptedExperimentPlanError(f"cannot read E4 report: {exc}") from exc
    try:
        data = json.loads(raw, object_pairs_hook=_reject_duplicate_keys)
    except json.JSONDecodeError as exc:
        raise AcceptedExperimentPlanError(f"E4 report is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise AcceptedExperimentPlanError("E4 report is not a JSON object")
    # JSON arrays decode to lists; the strict tuple field wants a tuple (this preserves the
    # committed member order — it never re-sorts). Missing/other-typed members still fail.
    members = data.get("members")
    if isinstance(members, list):
        data = {**data, "members": tuple(members)}
    try:
        return _E4TrainMatrixReport.model_validate(data)
    except ValidationError as exc:
        raise AcceptedExperimentPlanError(f"E4 report failed strict validation: {exc}") from exc


# --------------------------------------------------------------------------- #
# verified-input assembly (private; shared by the accepted constructor and tests)
# --------------------------------------------------------------------------- #
def _build_plan_from_verified_inputs(
    snapshot: FrozenSnapshot,
    feature_view: FeatureViewManifest,
    candidate_set: CandidateSet,
) -> ExperimentPlan:
    """Assemble a plan from a verified snapshot + train feature view + candidate set.

    Selects the snapshot's TRAIN membership **verbatim** (no percentages, no split), cross-binds
    it against the supplied train feature view, and assembles the deterministic plan. It checks
    input CONSISTENCY only and establishes no trust anchors of its own — it is **private and
    never exported**; the accepted constructor and tests supply their own verified inputs.
    """
    _require(feature_view.partition == "train", "feature view is not the train partition")
    _require(feature_view.epoch == snapshot.epoch, "feature view epoch != snapshot epoch")
    _require(
        feature_view.snapshot_hash == snapshot.snapshot_hash, "feature view snapshot_hash mismatch"
    )
    _require(
        feature_view.split_manifest_hash == snapshot.split_manifest_hash,
        "feature view split_manifest_hash mismatch",
    )
    _require(
        feature_view.registry_snapshot_hash == snapshot.registry_snapshot_hash,
        "feature view registry_snapshot_hash mismatch",
    )

    train_by_dataset = {m.dataset_id: m for m in snapshot.members_for("train")}
    validation_datasets = {m.dataset_id for m in snapshot.members_for("validation")}
    fv_members = feature_view.members

    indices = [fvm.member_index for fvm in fv_members]
    _require(indices == list(range(len(fv_members))), "feature-view indices not contiguous 0..n-1")
    _require(
        len(fv_members) == len(train_by_dataset),
        "feature-view member count != accepted train membership",
    )
    fv_datasets = {fvm.dataset_id for fvm in fv_members}
    _require(
        fv_datasets == set(train_by_dataset), "feature-view datasets != accepted train members"
    )
    _require(not (fv_datasets & validation_datasets), "feature view contains validation members")
    if len({fvm.vector_hash for fvm in fv_members}) != len(fv_members):
        raise AcceptedExperimentPlanError("duplicate feature-view vector_hash")

    plan_members: list[ExperimentPlanMember] = []
    for fvm in fv_members:
        sm = train_by_dataset[fvm.dataset_id]
        _require(
            fvm.feature_values_hash == sm.feature_values_hash,
            f"feature_values_hash mismatch for {fvm.dataset_id}",
        )
        plan_members.append(
            ExperimentPlanMember(
                dataset_id=sm.dataset_id,
                profile_id=sm.profile_id,
                content_hash=sm.content_hash,
                feature_values_hash=sm.feature_values_hash,
                vector_hash=fvm.vector_hash,
                member_index=fvm.member_index,
            )
        )

    return _assemble_experiment_plan(
        epoch=snapshot.epoch,
        snapshot_hash=snapshot.snapshot_hash,
        split_manifest_hash=snapshot.split_manifest_hash,
        registry_snapshot_hash=snapshot.registry_snapshot_hash,
        train_matrix_hash=feature_view.matrix_hash,
        train_feature_view_hash=feature_view.feature_view_hash,
        feature_set_hash=feature_view.feature_set_hash,
        feature_registry_hash=feature_view.feature_registry_hash,
        candidate_set=candidate_set,
        ordered_members=plan_members,
    )


def build_accepted_experiment_plan() -> ExperimentPlan:
    """THE accepted offline experiment plan — no caller-selected identities/roots/gates.

    Deterministic and repeatable: repeated construction yields an identical plan and
    ``plan_hash``. Pure of side effects (verification reads committed files + git only).
    """
    # 1) pinned E5 gate closure (fail-closed; a regenerated PASS gate cannot pass).
    verify_e5_prerequisite_closure(_REPO_ROOT)

    # 2) accepted epoch-1 member manifest (strict, trust-anchored) + 3) strict E4 report.
    snapshot = load_accepted_epoch1_member_manifest(_MEMBER_MANIFEST.read_bytes())
    e4 = _parse_e4_train_report()

    # bind the E4 report fields to the accepted snapshot + pinned identities/contracts.
    _require(
        snapshot.snapshot_hash == PRE.PROFILE_SNAPSHOT_1_HASH, "snapshot_hash not the accepted pin"
    )
    _require(e4.snapshot_hash == snapshot.snapshot_hash, "E4 snapshot_hash mismatch")
    _require(
        e4.split_manifest_hash
        == snapshot.split_manifest_hash
        == PRE.PROFILE_SNAPSHOT_1_SPLIT_MANIFEST_HASH,
        "split_manifest_hash mismatch",
    )
    _require(
        e4.registry_snapshot_hash
        == snapshot.registry_snapshot_hash
        == PRE.PROFILE_SNAPSHOT_1_REGISTRY_SNAPSHOT_HASH,
        "registry_snapshot_hash mismatch",
    )
    _require(e4.matrix_hash == PRE.E4_TRAIN_MATRIX_HASH, "matrix_hash not the accepted pin")
    _require(
        e4.artifact_sha256 == PRE.E4_TRAIN_ARTIFACT_SHA256, "artifact_sha256 not the accepted pin"
    )
    _require(
        e4.registry_hash == PRE.ACCEPTED_FEATURE_REGISTRY_HASH,
        "registry_hash not the accepted feature registry",
    )
    _require(
        e4.git_head == PRE.E4_FEATURE_MATRIX_SOURCE_COMMIT, "E4 git_head not the accepted E4 source"
    )
    _require(
        e4.git_tree == PRE.E4_FEATURE_MATRIX_SOURCE_TREE, "E4 git_tree not the accepted E4 tree"
    )
    _require(
        e4.snapshot_member_count == len(snapshot.members),
        "snapshot_member_count != total accepted snapshot membership",
    )
    _require(e4.row_count == len(e4.members), "row_count != member count")

    # 4/5) reconstruct the train feature view; require its hash == the pinned accepted hash.
    feature_view = build_feature_view_manifest(
        epoch=e4.epoch,
        partition="train",
        snapshot_hash=snapshot.snapshot_hash,
        split_manifest_hash=snapshot.split_manifest_hash,
        registry_snapshot_hash=snapshot.registry_snapshot_hash,
        matrix_hash=e4.matrix_hash,
        artifact_sha256=e4.artifact_sha256,
        row_count=e4.row_count,
        members=tuple(
            FeatureViewMember(
                dataset_id=m.dataset_id,
                member_index=m.member_index,
                vector_hash=m.vector_hash,
                feature_values_hash=m.feature_values_hash,
            )
            for m in e4.members
        ),
        feature_set=None,
    )
    _require(
        feature_view.feature_view_hash == PRE.ACCEPTED_EPOCH1_TRAIN_FEATURE_VIEW_HASH,
        "reconstructed train feature_view_hash != accepted pin",
    )
    # 7) feature-set / feature-registry / column-count derived from the verified train view.
    _require(
        feature_view.feature_registry_hash == PRE.ACCEPTED_FEATURE_REGISTRY_HASH,
        "feature_registry_hash != accepted pin",
    )
    _require(
        e4.feature_set_hash == feature_view.feature_set_hash,
        "E4 feature_set_hash != reconstructed feature-set identity",
    )
    _require(
        e4.column_count == feature_view.column_count,
        "E4 column_count != reconstructed feature-column count",
    )

    # 8/9) accepted candidate set + independent verification (no override).
    candidate_set = generate_accepted_candidate_set()
    verify_accepted_candidate_set(candidate_set)

    # 10/11) assemble via the shared verified-input path (train membership consumed verbatim).
    return _build_plan_from_verified_inputs(snapshot, feature_view, candidate_set)
