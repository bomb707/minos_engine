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
from pathlib import Path
from typing import Any

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

__all__ = [
    "E5ClosureError",
    "AcceptedExperimentPlanError",
    "verify_e5_prerequisite_closure",
    "build_accepted_experiment_plan",
]

_REPO_ROOT = Path(__file__).resolve().parents[3]
_MEMBER_MANIFEST = _REPO_ROOT / "manifests" / "profile_snapshot_epoch1_members.json"
_E4_TRAIN_REPORT = _REPO_ROOT / "reports" / "e4" / "L2E_E4_TRAIN_MATRIX.json"
_E4_MATRIX_METADATA_SCHEMA = "l2e-e4-matrix-metadata-v1"
_ACCEPTED_EPOCH = 1


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
# accepted inputs
# --------------------------------------------------------------------------- #
def _parse_e4_train_report() -> dict[str, Any]:
    text = _E4_TRAIN_REPORT.read_text(encoding="utf-8")
    data = json.loads(text, object_pairs_hook=_reject_duplicate_keys)
    if not isinstance(data, dict):
        raise AcceptedExperimentPlanError("E4 report is not a JSON object")
    _require(data.get("schema") == _E4_MATRIX_METADATA_SCHEMA, "unexpected E4 report schema")
    _require(data.get("partition") == "train", "E4 report partition is not train")
    _require(data.get("epoch") == _ACCEPTED_EPOCH, "E4 report epoch is not 1")
    return data


def build_accepted_experiment_plan() -> ExperimentPlan:
    """THE accepted offline experiment plan — no caller-selected identities/roots/gates.

    Deterministic and repeatable: repeated construction yields an identical plan and
    ``plan_hash``. Pure of side effects (verification reads committed files + git only).
    """
    # 1) pinned E5 gate closure (fail-closed; a regenerated PASS gate cannot pass).
    verify_e5_prerequisite_closure(_REPO_ROOT)

    # 2) accepted epoch-1 member manifest (strict, trust-anchored).
    snapshot = load_accepted_epoch1_member_manifest(_MEMBER_MANIFEST.read_bytes())
    train_by_dataset = {m.dataset_id: m for m in snapshot.members_for("train")}
    validation_datasets = {m.dataset_id for m in snapshot.members_for("validation")}

    # 3) strict E4 train metadata report (duplicate-key rejection).
    e4 = _parse_e4_train_report()

    # snapshot/matrix identity binding.
    _require(e4["snapshot_hash"] == snapshot.snapshot_hash, "E4 snapshot_hash mismatch")
    _require(
        snapshot.snapshot_hash == PRE.PROFILE_SNAPSHOT_1_HASH, "snapshot_hash not the accepted pin"
    )
    _require(
        e4["split_manifest_hash"]
        == snapshot.split_manifest_hash
        == PRE.PROFILE_SNAPSHOT_1_SPLIT_MANIFEST_HASH,
        "split_manifest_hash mismatch",
    )
    _require(
        e4["registry_snapshot_hash"]
        == snapshot.registry_snapshot_hash
        == PRE.PROFILE_SNAPSHOT_1_REGISTRY_SNAPSHOT_HASH,
        "registry_snapshot_hash mismatch",
    )
    _require(
        e4["matrix_hash"] == PRE.E4_TRAIN_MATRIX_HASH, "train matrix_hash not the accepted pin"
    )

    # 6) E4 train inventory must equal the accepted snapshot's train membership exactly.
    e4_members = sorted(e4["members"], key=lambda m: int(m["member_index"]))
    indices = [int(m["member_index"]) for m in e4_members]
    _require(indices == list(range(len(e4_members))), "E4 member indices are not contiguous 0..n-1")
    _require(len(e4_members) == int(e4["row_count"]), "E4 member count != row_count")
    e4_datasets = {m["dataset_id"] for m in e4_members}
    _require(e4_datasets == set(train_by_dataset), "E4 train dataset set != accepted train members")
    _require(not (e4_datasets & validation_datasets), "E4 inventory contains validation members")

    plan_members: list[ExperimentPlanMember] = []
    fv_members: list[FeatureViewMember] = []
    for m in e4_members:
        sm = train_by_dataset[m["dataset_id"]]
        _require(
            m["feature_values_hash"] == sm.feature_values_hash,
            f"feature_values_hash mismatch for {m['dataset_id']}",
        )
        fv_members.append(
            FeatureViewMember(
                dataset_id=m["dataset_id"],
                member_index=int(m["member_index"]),
                vector_hash=m["vector_hash"],
                feature_values_hash=m["feature_values_hash"],
            )
        )
        plan_members.append(
            ExperimentPlanMember(
                dataset_id=sm.dataset_id,
                profile_id=sm.profile_id,
                content_hash=sm.content_hash,
                feature_values_hash=sm.feature_values_hash,
                vector_hash=m["vector_hash"],
                member_index=int(m["member_index"]),
            )
        )

    # 4/5) reconstruct the train feature view; require its hash == the pinned accepted hash.
    feature_view = build_feature_view_manifest(
        epoch=_ACCEPTED_EPOCH,
        partition="train",
        snapshot_hash=snapshot.snapshot_hash,
        split_manifest_hash=snapshot.split_manifest_hash,
        registry_snapshot_hash=snapshot.registry_snapshot_hash,
        matrix_hash=e4["matrix_hash"],
        artifact_sha256=e4["artifact_sha256"],
        row_count=int(e4["row_count"]),
        members=tuple(fv_members),
        feature_set=None,
    )
    _require(
        feature_view.feature_view_hash == PRE.ACCEPTED_EPOCH1_TRAIN_FEATURE_VIEW_HASH,
        "reconstructed train feature_view_hash != accepted pin",
    )
    # 7) derive feature-set / feature-registry identities from the verified train view.
    _require(
        feature_view.feature_registry_hash == PRE.ACCEPTED_FEATURE_REGISTRY_HASH,
        "feature_registry_hash != accepted pin",
    )
    _require(
        e4["feature_set_hash"] == feature_view.feature_set_hash,
        "E4 feature_set_hash != reconstructed feature-set identity",
    )

    # 8/9) accepted candidate set + independent verification (no override).
    candidate_set = generate_accepted_candidate_set()
    verify_accepted_candidate_set(candidate_set)

    # 10/11) assemble the deterministic plan (identities derived; no replacement anchors).
    return _assemble_experiment_plan(
        epoch=_ACCEPTED_EPOCH,
        snapshot_hash=snapshot.snapshot_hash,
        split_manifest_hash=snapshot.split_manifest_hash,
        registry_snapshot_hash=snapshot.registry_snapshot_hash,
        train_matrix_hash=e4["matrix_hash"],
        train_feature_view_hash=feature_view.feature_view_hash,
        feature_set_hash=feature_view.feature_set_hash,
        feature_registry_hash=feature_view.feature_registry_hash,
        candidate_set=candidate_set,
        ordered_members=plan_members,
    )
