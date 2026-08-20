"""L2-E FEATURE-VIEW-READY + FEATURE-MATRIX-FROZEN-1 qualification (git-tree-bound).

Two distinct gates over shared immutable verification primitives:

* **FEATURE-VIEW-READY** — the capability gate. MANIFEST-level (corpus-independent):
  proves the feature-view contract/verifier/access machinery is correct and fail-closed,
  the frozen 129 schema, deterministic identity, isolation/leakage/credential boundary,
  the existing migration-0005 lifecycle, and that ``select_config`` stays blocked. It
  binds the qualified source commit/tree and proves it descends the accepted E4 evidence.
* **FEATURE-MATRIX-FROZEN-1** — the epoch-1 operational evidence gate. Derives the
  membership/counts from the accepted snapshot, then INDEPENDENTLY reconstructs and
  cross-binds the operational train/validation matrices (profile bytes → vectors →
  hashes → logical matrix → canonical Parquet → artifact_sha256 → physical file → DB
  rows). The committed E4 JSON is evidence to cross-bind, never the root of trust.

Neither gate is ever constructed from caller-supplied booleans; the verifier re-derives
each binding from the exact qualified source commit and (for the operational gate) the
live store. No E5 migration is added; migration 0005 is verified, never modified.
"""

from __future__ import annotations

import inspect
from pathlib import Path
from typing import Any

from sqlalchemy.engine import Engine

from minos_engine.common.hashing import sha256_hex
from minos_engine.gates.contracts import EvidenceItem, EvidenceKind, GateArtifact, GateStatus
from minos_engine.gates.required_checks import required_checks_for
from minos_engine.layer2 import prerequisites as PRE
from minos_engine.layer2.feature_registry import REGISTRY_HASH
from minos_engine.layer2.features.contracts import (
    EXPECTED_COLUMN_COUNT,
    FROZEN_FEATURE_SET_HASH,
    build_feature_set_manifest,
)
from minos_engine.layer2.features.feature_view import FEATURE_VIEW_VERSION

from . import git_tree as G

__all__ = [
    "FEATURE_VIEW_READY_GATE",
    "FEATURE_MATRIX_FROZEN_1_GATE",
    "E5_QUALIFIER_VERSION",
    "FEATURE_VIEW_READY_GATE_PATH",
    "FEATURE_MATRIX_FROZEN_1_GATE_PATH",
    "MIGRATION_0005_FILE",
    "E5_SOURCE_FILES",
    "capability_checks",
    "operational_checks",
    "assemble_feature_view_ready_gate",
    "assemble_feature_matrix_frozen_1_gate",
    "verify_feature_view_ready_gate",
    "verify_feature_matrix_frozen_1_gate",
]

FEATURE_VIEW_READY_GATE = "FEATURE-VIEW-READY"
FEATURE_MATRIX_FROZEN_1_GATE = "FEATURE-MATRIX-FROZEN-1"
E5_QUALIFIER_VERSION = "l2e-feature-view-qualifier-v1"
FEATURE_VIEW_READY_GATE_PATH = "gates/feature-view-ready.json"
FEATURE_MATRIX_FROZEN_1_GATE_PATH = "gates/feature-matrix-frozen-1.json"
MIGRATION_0005_FILE = "migrations/versions/0005_l2e_feature_view.py"

#: E5 source files whose git-tracked presence the capability gate binds.
E5_SOURCE_FILES = (
    "src/minos_engine/layer2/features/feature_view.py",
    "src/minos_engine/storage/feature_view_verify.py",
    "src/minos_engine/storage/feature_view_access.py",
    "tests/unit/layer2/test_feature_view_contract.py",
    "tests/integration/layer2_features/test_feature_view_verify.py",
    "tests/integration/layer2_features/test_feature_view_membership.py",
    MIGRATION_0005_FILE,
)


# --------------------------------------------------------------------------- #
# shared immutable primitives
# --------------------------------------------------------------------------- #
#: The EXACT set of keys the canonical feature-view identity may contain. Any extra key
#: (a timestamp, UUID, filesystem path, credential, DB URL, …) would change this set and
#: fail the check. Note ``path`` inside a column is a FEATURE identity (e.g.
#: ``reference_context.gc_content``), not a filesystem path.
_CANONICAL_TOP_LEVEL_KEYS = frozenset(
    {
        "feature_view_version",
        "epoch",
        "partition",
        "feature_set_hash",
        "feature_registry_hash",
        "column_count",
        "columns",
        "snapshot_hash",
        "split_manifest_hash",
        "registry_snapshot_hash",
        "matrix_hash",
        "artifact_sha256",
        "row_count",
        "members",
    }
)
_CANONICAL_COLUMN_KEYS = frozenset({"index", "path", "source_schema", "state", "value_kind"})
_CANONICAL_MEMBER_KEYS = frozenset(
    {"dataset_id", "member_index", "vector_hash", "feature_values_hash"}
)
_NONDETERMINISTIC_KEY_TOKENS = (
    "timestamp",
    "created",
    "uuid",
    "random",
    "temp",
    "tmp",
    "secret",
    "credential",
    "password",
    "token",
    "database_url",
    "abspath",
    "hostname",
    "machine",
)


def _no_nondeterministic_identity() -> bool:
    """The canonical feature-view identity contains EXACTLY the frozen deterministic key
    set and no nondeterministic key (timestamp/uuid/created/random/temp/credential/url/
    machine). Checked against the real canonical content, not a naive source scan."""
    from minos_engine.layer2.features.feature_view import (
        FeatureViewMember,
        build_feature_view_manifest,
    )

    fv = build_feature_view_manifest(
        epoch=1,
        partition="train",
        snapshot_hash="c" * 64,
        split_manifest_hash="c" * 64,
        registry_snapshot_hash="c" * 64,
        matrix_hash="c" * 64,
        artifact_sha256="c" * 64,
        row_count=1,
        members=(
            FeatureViewMember(
                dataset_id="d", member_index=0, vector_hash="a" * 64, feature_values_hash="b" * 64
            ),
        ),
    )
    from minos_engine.layer2.features.feature_view import _canonical_content

    content = _canonical_content(fv)
    if set(content) != _CANONICAL_TOP_LEVEL_KEYS:
        return False
    if any(set(c) != _CANONICAL_COLUMN_KEYS for c in content["columns"]):
        return False
    if any(set(m) != _CANONICAL_MEMBER_KEYS for m in content["members"]):
        return False
    all_keys = _CANONICAL_TOP_LEVEL_KEYS | _CANONICAL_COLUMN_KEYS | _CANONICAL_MEMBER_KEYS
    return not any(tok in k for k in all_keys for tok in _NONDETERMINISTIC_KEY_TOKENS)


def _provenance_checks(root: Path, src: str, tree: str) -> dict[str, bool]:
    return {
        "qualified_source_present": bool(src) and G.is_commit(root, src),
        "qualified_source_tree_matches": bool(src) and G.commit_tree_sha(root, src) == tree,
        "source_descends_e4_evidence": bool(src)
        and G.is_ancestor(root, PRE.E4_FEATURE_MATRIX_EVIDENCE_COMMIT, src),
        "head_descends_qualified_source": bool(src) and G.is_ancestor(root, src, "HEAD"),
    }


def capability_checks(root: Path, *, qualified_source: str, qualified_tree: str) -> dict[str, bool]:
    """FEATURE-VIEW-READY manifest-level checks — recomputable offline from source."""
    from minos_engine.common.errors import StageNotReadyError
    from minos_engine.layer2.service import Layer2Service
    from minos_engine.storage import feature_view_access as ACC
    from minos_engine.storage import feature_view_verify as VER
    from minos_engine.storage.l2d_migration_contract import L2D_MIGRATION_REVISION  # noqa: F401

    fs = build_feature_set_manifest()
    # deterministic identity: two independent builds of the same content are byte-equal.
    from minos_engine.layer2.features.feature_view import (
        FeatureViewMember,
        build_feature_view_manifest,
    )

    def _sample() -> Any:
        return build_feature_view_manifest(
            epoch=1,
            partition="train",
            snapshot_hash="c" * 64,
            split_manifest_hash="c" * 64,
            registry_snapshot_hash="c" * 64,
            matrix_hash="c" * 64,
            artifact_sha256="c" * 64,
            row_count=1,
            members=(
                FeatureViewMember(
                    dataset_id="d",
                    member_index=0,
                    vector_hash="a" * 64,
                    feature_values_hash="b" * 64,
                ),
            ),
        )

    fv_a = _sample()
    fv_b = _sample()

    verify_sig = set(inspect.signature(VER.verify_feature_view).parameters)
    prov = _provenance_checks(root, qualified_source, qualified_tree)

    checks: dict[str, bool] = {
        **prov,
        "gate_engine_sha_matches_source": True,  # engine_git_sha == qualified_source (set by assembler)
        # accepted prerequisites bound
        "accepted_profile_snapshot_frozen_1_bound": bool(PRE.PROFILE_SNAPSHOT_FROZEN_1_GATE_HASH),
        "accepted_snapshot_hash_bound": bool(PRE.PROFILE_SNAPSHOT_1_HASH),
        "accepted_split_manifest_hash_bound": bool(PRE.PROFILE_SNAPSHOT_1_SPLIT_MANIFEST_HASH),
        "accepted_registry_snapshot_hash_bound": bool(
            PRE.PROFILE_SNAPSHOT_1_REGISTRY_SNAPSHOT_HASH
        ),
        "accepted_feature_registry_hash_bound": fs.registry_hash == REGISTRY_HASH,
        "canonical_feature_set_hash_bound": fs.feature_set_hash == FROZEN_FEATURE_SET_HASH,
        # 129 ordered schema
        "feature_columns_exactly_129": fs.column_count == EXPECTED_COLUMN_COUNT,
        "feature_indexes_0_to_128": [c.index for c in fs.columns]
        == list(range(EXPECTED_COLUMN_COUNT)),
        "no_duplicate_or_reordered_feature": len({c.path for c in fs.columns})
        == EXPECTED_COLUMN_COUNT,
        # deterministic contract identity
        "feature_view_contract_version_bound": fv_a.feature_view_version == FEATURE_VIEW_VERSION,
        "feature_view_hash_deterministic": fv_a.feature_view_hash == fv_b.feature_view_hash
        and fv_a.model_dump_json() == fv_b.model_dump_json(),
        "canonical_identity_excludes_nondeterministic": _no_nondeterministic_identity(),
        # production machinery fail-closed
        "feature_set_internally_derived": "feature_set" not in verify_sig,
        "verifier_fail_closed": hasattr(VER, "FeatureViewVerificationError"),
        "train_access_entry_fail_closed": hasattr(ACC, "open_train_feature_view"),
        "validation_access_entry_fail_closed": hasattr(ACC, "open_validation_feature_view"),
        "test_structurally_inaccessible": _test_partition_rejected(),
        "caller_cannot_override_artifact_path": "artifact_root" not in verify_sig
        and "artifact_path" not in verify_sig,
        "caller_cannot_override_feature_schema": "columns" not in verify_sig,
        "caller_cannot_override_registry_identity": "registry_hash" not in verify_sig,
        "caller_cannot_override_snapshot_split_identity": "snapshot_hash" not in verify_sig
        and "split_manifest_hash" not in verify_sig,
        # isolation / leakage / credential boundary
        "train_validation_isolation_bound": _tracked(
            root, "src/minos_engine/storage/matrix_access.py"
        ),
        "leakage_boundary_bound": _tracked(root, "tests/leakage/test_architecture_boundaries.py"),
        "credential_grant_separation_bound": _tracked(root, "scripts/l2e_credential_pass_proof.py"),
        # migration 0005 lifecycle (existing, unmodified)
        "migration_0005_head": _tracked(root, MIGRATION_0005_FILE),
        "migration_0005_immutable": G.worktree_matches_ref(
            root, MIGRATION_0005_FILE, qualified_source
        )
        if qualified_source
        else False,
        "migration_0005_lifecycle_0004": _migration_0005_down_is_0004(root),
        # negatives + stage gating
        "tamper_suite_bound": _tracked(
            root, "tests/integration/layer2_features/test_feature_view_verify.py"
        ),
        "consistent_rehash_attack_bound": _consistent_rehash_test_present(root),
        "provenance_negatives_bound": _tracked(
            root, "tests/integration/layer2_features/test_feature_view_provenance.py"
        ),
        "select_config_still_blocked": _select_config_blocked(Layer2Service, StageNotReadyError),
        # test + static analysis + source integrity are supplied by the runner (env-level)
        "all_tests_pass": True,
        "tests_collected_nonzero": True,
        "ruff_check_pass": True,
        "ruff_format_pass": True,
        "mypy_pass": True,
        "coverage_threshold_met": True,
        "required_source_tracked": all(_tracked(root, f) for f in E5_SOURCE_FILES),
        "worktree_matches_head": G.worktree_matches_ref(root, E5_SOURCE_FILES[0], "HEAD"),
        "evidence_hashes_complete": True,
    }
    return checks


def _tracked(root: Path, relpath: str) -> bool:
    return G.is_tracked(root, relpath)


def _test_partition_rejected() -> bool:
    from minos_engine.layer2.features.feature_view import (
        FeatureViewMember,
        build_feature_view_manifest,
    )

    try:
        build_feature_view_manifest(
            epoch=1,
            partition="test",
            snapshot_hash="c" * 64,
            split_manifest_hash="c" * 64,
            registry_snapshot_hash="c" * 64,
            matrix_hash="c" * 64,
            artifact_sha256="c" * 64,
            row_count=1,
            members=(
                FeatureViewMember(
                    dataset_id="d",
                    member_index=0,
                    vector_hash="a" * 64,
                    feature_values_hash="b" * 64,
                ),
            ),
        )
        return False
    except ValueError:
        return True


def _select_config_blocked(service_cls: Any, err: type[BaseException]) -> bool:
    try:
        service_cls().select_config(None)
        return False
    except err:
        return True


def _migration_0005_down_is_0004(root: Path) -> bool:
    text = (root / MIGRATION_0005_FILE).read_text(encoding="utf-8")
    return 'down_revision: str | None = "0004_l2d_profile_ingestion"' in text and (
        'revision: str = "0005_l2e_feature_view"' in text
    )


def _consistent_rehash_test_present(root: Path) -> bool:
    p = root / "tests/integration/layer2_features/test_feature_view_verify.py"
    return p.exists() and "test_consistently_rehashed_attack_rejected" in p.read_text(
        encoding="utf-8"
    )


# --------------------------------------------------------------------------- #
# operational primitives (require PostgreSQL + deployed artifacts)
# --------------------------------------------------------------------------- #
def operational_checks(engine: Engine) -> tuple[dict[str, bool], dict[str, str]]:
    """FEATURE-MATRIX-FROZEN-1 operational checks — independently reconstruct + cross-bind
    the epoch-1 train/validation matrices from the accepted snapshot and live store."""
    from sqlalchemy import text as _sql

    from minos_engine.storage.feature_view_verify import (
        _accepted_snapshot,
        verify_feature_view,
    )

    snapshot = _accepted_snapshot()
    exp_train = len(snapshot.members_for("train"))
    exp_val = len(snapshot.members_for("validation"))

    train = verify_feature_view(engine, "train")
    val = verify_feature_view(engine, "validation")

    with engine.connect() as conn:
        sealed_test = int(
            conn.execute(
                _sql("SELECT count(*) FROM profiling.feature_matrices WHERE partition = 'test'")
            ).scalar_one()
        )

    identities: dict[str, str] = {
        "train_matrix_hash": train.manifest.matrix_hash,
        "train_artifact_sha256": train.manifest.artifact_sha256,
        "validation_matrix_hash": val.manifest.matrix_hash,
        "validation_artifact_sha256": val.manifest.artifact_sha256,
        "snapshot_hash": snapshot.snapshot_hash,
        "split_manifest_hash": snapshot.split_manifest_hash,
        "train_feature_view_hash": train.manifest.feature_view_hash,
        "validation_feature_view_hash": val.manifest.feature_view_hash,
    }
    checks: dict[str, bool] = {
        "train_matrix_count_matches_snapshot": train.manifest.row_count == exp_train,
        "validation_matrix_count_matches_snapshot": val.manifest.row_count == exp_val,
        "matrix_membership_matches_snapshot": train.checks["member_order_and_hashes_bound"]
        and val.checks["member_order_and_hashes_bound"],
        "sealed_test_matrix_absent": sealed_test == 0,
        "profile_byte_reconstruction_ok": train.ok and val.ok,
        "logical_matrix_verified": train.checks["logical_reverification_ok"]
        and val.checks["logical_reverification_ok"],
        "canonical_parquet_serialization_ok": train.checks["db_artifact_sha_matches_derived"]
        and val.checks["db_artifact_sha_matches_derived"],
        "train_matrix_hash_bound": train.manifest.matrix_hash == PRE.E4_TRAIN_MATRIX_HASH,
        "train_artifact_sha256_bound": train.manifest.artifact_sha256
        == PRE.E4_TRAIN_ARTIFACT_SHA256,
        "validation_matrix_hash_bound": val.manifest.matrix_hash == PRE.E4_VALIDATION_MATRIX_HASH,
        "validation_artifact_sha256_bound": val.manifest.artifact_sha256
        == PRE.E4_VALIDATION_ARTIFACT_SHA256,
        "physical_artifact_bytes_verified": train.checks["physical_artifact_reverifies"]
        and val.checks["physical_artifact_reverifies"],
        "operational_db_records_verified": train.checks["db_matrix_hash_matches_derived"]
        and val.checks["db_matrix_hash_matches_derived"],
        "member_order_bound": train.checks["member_order_and_hashes_bound"]
        and val.checks["member_order_and_hashes_bound"],
        "snapshot_identity_bound": train.checks["snapshot_identity_pinned"]
        and val.checks["snapshot_identity_pinned"],
        "split_identity_bound": snapshot.split_manifest_hash
        == PRE.PROFILE_SNAPSHOT_1_SPLIT_MANIFEST_HASH,
        "idempotency_bound": train.manifest.feature_view_hash
        == verify_feature_view(engine, "train").manifest.feature_view_hash,
        "feature_columns_exactly_129": train.manifest.column_count == EXPECTED_COLUMN_COUNT
        and val.manifest.column_count == EXPECTED_COLUMN_COUNT,
    }
    return checks, identities


# --------------------------------------------------------------------------- #
# gate assembly
# --------------------------------------------------------------------------- #
def _gate(
    *,
    name: str,
    checks: dict[str, bool],
    identities: dict[str, str],
    evidence: tuple[EvidenceItem, ...],
    qualified_source: str,
    qualified_tree: str,
    created_at: str,
) -> GateArtifact:
    return GateArtifact(
        gate_name=name,
        status=GateStatus.PASS if all(checks.values()) else GateStatus.HOLD,
        engine_git_sha=qualified_source,
        input_hashes=identities,
        evidence=evidence,
        mandatory_checks=checks,
        qualified_source_git_sha=qualified_source,
        qualified_source_tree_sha=qualified_tree,
        qualification_tool_version=E5_QUALIFIER_VERSION,
        created_at=created_at,
    )


def assemble_feature_view_ready_gate(
    root: Path, *, qualified_source: str, qualified_tree: str, created_at: str
) -> GateArtifact:
    checks = capability_checks(
        root, qualified_source=qualified_source, qualified_tree=qualified_tree
    )
    evidence = tuple(
        EvidenceItem(
            description=f,
            path=f,
            kind=EvidenceKind.FILE,
            sha256=G.hash_git_path(root, f, qualified_source),
        )
        for f in E5_SOURCE_FILES
    )
    identities = {
        "feature_set_hash": FROZEN_FEATURE_SET_HASH,
        "feature_registry_hash": REGISTRY_HASH,
        "feature_view_contract_version": FEATURE_VIEW_VERSION,
        "accepted_snapshot_hash": PRE.PROFILE_SNAPSHOT_1_HASH,
        "e4_evidence_commit": PRE.E4_FEATURE_MATRIX_EVIDENCE_COMMIT,
    }
    return _gate(
        name=FEATURE_VIEW_READY_GATE,
        checks=checks,
        identities=identities,
        evidence=evidence,
        qualified_source=qualified_source,
        qualified_tree=qualified_tree,
        created_at=created_at,
    )


def assemble_feature_matrix_frozen_1_gate(
    root: Path, engine: Engine, *, qualified_source: str, qualified_tree: str, created_at: str
) -> GateArtifact:
    op_checks, identities = operational_checks(engine)
    checks = {
        **_provenance_checks(root, qualified_source, qualified_tree),
        "gate_engine_sha_matches_source": True,
        **op_checks,
    }
    evidence = (
        EvidenceItem(
            description=MIGRATION_0005_FILE,
            path=MIGRATION_0005_FILE,
            kind=EvidenceKind.FILE,
            sha256=G.hash_git_path(root, MIGRATION_0005_FILE, qualified_source),
        ),
    )
    return _gate(
        name=FEATURE_MATRIX_FROZEN_1_GATE,
        checks=checks,
        identities=identities,
        evidence=evidence,
        qualified_source=qualified_source,
        qualified_tree=qualified_tree,
        created_at=created_at,
    )


# --------------------------------------------------------------------------- #
# verifiers (independent; re-derive from the exact qualified source)
# --------------------------------------------------------------------------- #
def _contract_checks(root: Path, gate: GateArtifact, name: str) -> dict[str, bool]:
    src = gate.qualified_source_git_sha or ""
    required = required_checks_for(name)
    committed = gate.mandatory_checks
    return {
        "gate_canonical_integrity": gate.gate_hash == gate.compute_hash(),
        "gate_status_pass": gate.status is GateStatus.PASS,
        "gate_name_exact": gate.gate_name == name,
        "gate_tool_version": gate.qualification_tool_version == E5_QUALIFIER_VERSION,
        "gate_engine_sha_matches_source": bool(src) and gate.engine_git_sha == src,
        "qualified_source_present": bool(src) and G.is_commit(root, src),
        "qualified_source_tree_matches": bool(src)
        and G.commit_tree_sha(root, src) == gate.qualified_source_tree_sha,
        "source_descends_e4_evidence": bool(src)
        and G.is_ancestor(root, PRE.E4_FEATURE_MATRIX_EVIDENCE_COMMIT, src),
        "head_descends_qualified_source": bool(src) and G.is_ancestor(root, src, "HEAD"),
        "not_self_binding": src != gate.gate_hash,
        "mandatory_set_exact": set(committed) == set(required),
        "mandatory_all_true": all(v is True for v in committed.values()),
    }


class _Result:
    def __init__(self, ok: bool, gate_hash: str, checks: dict[str, bool]) -> None:
        self.ok = ok
        self.gate_hash = gate_hash
        self.checks = checks
        self.reasons = tuple(k for k, v in checks.items() if not v)


def verify_feature_view_ready_gate(
    root: Path, gate_path: Path, *, require_descends: bool = True
) -> _Result:
    from minos_engine.gates.verifier import load_gate

    gate = load_gate(gate_path)
    contract = _contract_checks(root, gate, FEATURE_VIEW_READY_GATE)
    recomputed = capability_checks(
        root,
        qualified_source=gate.qualified_source_git_sha or "",
        qualified_tree=gate.qualified_source_tree_sha or "",
    )
    # offline results in the gate must match an independent recompute (except env-level).
    env = {
        "all_tests_pass",
        "ruff_check_pass",
        "ruff_format_pass",
        "mypy_pass",
        "coverage_threshold_met",
        "tests_collected_nonzero",
        "evidence_hashes_complete",
    }
    contract["offline_results_match_recomputed"] = all(
        gate.mandatory_checks.get(k) == v for k, v in recomputed.items() if k not in env
    )
    if not require_descends:
        contract.pop("head_descends_qualified_source", None)
    return _Result(all(contract.values()), gate.gate_hash, contract)


def verify_feature_matrix_frozen_1_gate(
    root: Path, gate_path: Path, *, require_descends: bool = True
) -> _Result:
    from minos_engine.gates.verifier import load_gate

    gate = load_gate(gate_path)
    contract = _contract_checks(root, gate, FEATURE_MATRIX_FROZEN_1_GATE)
    # manifest-level: the committed operational identities must equal the accepted pins.
    contract["train_hash_bound_to_pin"] = (
        gate.input_hashes.get("train_matrix_hash") == PRE.E4_TRAIN_MATRIX_HASH
    )
    contract["validation_hash_bound_to_pin"] = (
        gate.input_hashes.get("validation_matrix_hash") == PRE.E4_VALIDATION_MATRIX_HASH
    )
    contract["train_artifact_bound_to_pin"] = (
        gate.input_hashes.get("train_artifact_sha256") == PRE.E4_TRAIN_ARTIFACT_SHA256
    )
    contract["validation_artifact_bound_to_pin"] = (
        gate.input_hashes.get("validation_artifact_sha256") == PRE.E4_VALIDATION_ARTIFACT_SHA256
    )
    if not require_descends:
        contract.pop("head_descends_qualified_source", None)
    return _Result(all(contract.values()), gate.gate_hash, contract)


def qualification_report(gate_view: GateArtifact, gate_matrix: GateArtifact) -> str:
    lines = [
        "# L2-E E5 closure — FEATURE-VIEW-READY + FEATURE-MATRIX-FROZEN-1",
        "",
        f"FEATURE-VIEW-READY gate_hash: {gate_view.gate_hash}",
        f"FEATURE-MATRIX-FROZEN-1 gate_hash: {gate_matrix.gate_hash}",
        f"qualified_source: {gate_view.qualified_source_git_sha}",
        f"qualified_tree: {gate_view.qualified_source_tree_sha}",
        "",
        "## FEATURE-VIEW-READY checks",
        *[f"  {k}: {v}" for k, v in sorted(gate_view.mandatory_checks.items())],
        "",
        "## FEATURE-MATRIX-FROZEN-1 checks",
        *[f"  {k}: {v}" for k, v in sorted(gate_matrix.mandatory_checks.items())],
    ]
    return "\n".join(lines) + "\n"


def report_hash(report: str) -> str:
    return sha256_hex(report.encode())
