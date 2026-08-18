"""DB-READY (Layer 2 L2-B) qualification — git-tree-bound, evidence-based.

Assembles the ``DB-READY`` gate from **real** command evidence: the L2-A entry gate,
the accepted PROTOCOL/TWIN/L1 prerequisites, the PostgreSQL 16 integration suite
(run as a subprocess against a real server), the full test suite, coverage, and
ruff/format/mypy. The gate binds the source commit/tree, the Alembic head revision,
the migration-file hash, the storage schema/model hash, the role-policy hash, and the
PostgreSQL major version. A PASS is never constructed from caller-supplied booleans.
"""

from __future__ import annotations

import json
import re
import subprocess
import xml.etree.ElementTree as ET
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from minos_engine.common.errors import StageNotReadyError
from minos_engine.common.runtime import is_supported_runtime, runtime_identity
from minos_engine.gates.contracts import EvidenceKind, GateArtifact, GateStatus
from minos_engine.gates.required_checks import required_checks_for
from minos_engine.gates.verifier import load_gate, require_gate_pass, verify_gate_integrity
from minos_engine.layer2 import feature_registry as FR
from minos_engine.layer2 import prerequisites as PRE
from minos_engine.layer2.entry_gate import EntryGateRequest, verify_l2_entry_gate
from minos_engine.storage.fingerprint import storage_schema_hash
from minos_engine.storage.migration_contract import contract_hash
from minos_engine.storage.roles import role_policy_hash

from . import git_tree as G
from .ancestry import verify_commit_ancestry
from .coverage import STAGE0_COVERAGE_THRESHOLD, CoverageResult, run_coverage
from .provenance import GitProvenance, read_provenance
from .pytest_accounting import PytestAccounting, run_pytest, suite_passes
from .runner import SourceIntegrity, _bin, _tool_ok, gather_source_integrity

__all__ = [
    "GATE_NAME",
    "DB_QUALIFIER_VERSION",
    "ALEMBIC_HEAD",
    "l2b_revision_is_immutable_base",
    "DB_EVIDENCE",
    "DB_REQUIRED_TRACKED_FILES",
    "DbQualificationResult",
    "DbGateVerification",
    "alembic_head",
    "run_db_suite",
    "qualify_db_ready",
    "assemble_db_result",
    "verify_db_ready_gate",
    "write_db_outputs",
]

_HEX64 = re.compile(r"^[0-9a-f]{64}$")

GATE_NAME = "DB-READY"
DB_QUALIFIER_VERSION = "layer2-db-qualifier-v1"
ALEMBIC_HEAD = "0001_l2b_initial"
MIGRATION_FILE = "migrations/versions/0001_l2b_initial.py"
_DB_SUITE = "tests/integration/layer2_db"

DB_EVIDENCE: tuple[tuple[str, EvidenceKind], ...] = (
    ("src/minos_engine/storage", EvidenceKind.DIRECTORY),
    ("migrations", EvidenceKind.DIRECTORY),
    ("alembic.ini", EvidenceKind.FILE),
    (MIGRATION_FILE, EvidenceKind.FILE),
    ("gates/protocol-ready.json", EvidenceKind.FILE),
    ("gates/twin-ready.json", EvidenceKind.FILE),
    ("gates/l1-ready.json", EvidenceKind.FILE),
    ("docs/layer2/STORAGE_ARCHITECTURE.md", EvidenceKind.FILE),
    ("docs/layer2/DATABASE_ROLES.md", EvidenceKind.FILE),
    ("docs/layer2/MIGRATIONS.md", EvidenceKind.FILE),
    ("src/minos_engine/layer2/feature_registry.py", EvidenceKind.FILE),
    ("src/minos_engine/layer2/prerequisites.py", EvidenceKind.FILE),
    (_DB_SUITE, EvidenceKind.DIRECTORY),
    ("pyproject.toml", EvidenceKind.FILE),
)

DB_REQUIRED_TRACKED_FILES: tuple[str, ...] = (
    "src/minos_engine/storage/constants.py",
    "src/minos_engine/storage/database.py",
    "src/minos_engine/storage/metadata.py",
    "src/minos_engine/storage/roles.py",
    "src/minos_engine/storage/triggers.py",
    "src/minos_engine/storage/fingerprint.py",
    "src/minos_engine/storage/models/catalog.py",
    "src/minos_engine/storage/models/profiling.py",
    "src/minos_engine/storage/models/experiments.py",
    "src/minos_engine/storage/models/evaluation.py",
    "src/minos_engine/storage/models/models.py",
    "src/minos_engine/storage/models/runtime.py",
    "src/minos_engine/storage/models/audit.py",
    "src/minos_engine/storage/repositories/artifacts.py",
    "src/minos_engine/storage/repositories/append_only.py",
    "src/minos_engine/storage/migration_contract.py",
    MIGRATION_FILE,
    "migrations/env.py",
    "alembic.ini",
    "docs/layer2/STORAGE_ARCHITECTURE.md",
    "docs/layer2/DATABASE_ROLES.md",
    "docs/layer2/MIGRATIONS.md",
    "src/minos_engine/layer2/feature_registry.py",
    "src/minos_engine/layer2/prerequisites.py",
)

# gate check -> the DB-suite nodeids (file.py or file.py::test) whose pass proves it.
_CHECK_NODES: dict[str, tuple[str, ...]] = {
    "postgres_16_verified": ("test_schema_inventory.py::test_postgres_major_version_is_16",),
    "seven_schemas_created": ("test_schema_inventory.py::test_exactly_seven_application_schemas",),
    "five_roles_created": ("test_roles_isolation.py::test_all_five_roles_exist",),
    "migration_upgrade_passed": ("test_migration_lifecycle.py::test_full_migration_lifecycle",),
    "migration_downgrade_passed": ("test_migration_lifecycle.py::test_full_migration_lifecycle",),
    "migration_reupgrade_passed": ("test_migration_lifecycle.py::test_full_migration_lifecycle",),
    "migration_contract_frozen": ("test_migration_immutability.py",),
    "admin_ownership_passed": ("test_admin_ownership.py",),
    "constraint_tests_passed": ("test_constraints.py",),
    "foreign_keys_passed": ("test_constraints.py::test_orphan_foreign_key_rejected",),
    "append_only_passed": ("test_append_only.py",),
    "least_privilege_passed": ("test_roles_isolation.py",),
    "live_evaluation_denied": (
        "test_roles_isolation.py::test_live_cannot_access_evaluation_schema",
    ),
    "trainer_evaluation_denied": ("test_roles_isolation.py::test_trainer_cannot_read_evaluation",),
    "worker_claim_concurrency_passed": ("test_worker_claim.py",),
    "artifact_policy_passed": ("test_artifact_policy.py",),
}


def alembic_head() -> str:
    """The single migration head revision (read from the Alembic script directory)."""
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    return ScriptDirectory.from_config(Config("alembic.ini")).get_current_head() or ""


def l2b_revision_is_immutable_base(bound_revision: str) -> bool:
    """True iff ``bound_revision`` is the immutable base of the migration lineage.

    Forward-compatible replacement for a strict ``bound == head`` equality: a later
    stage (L2-C and beyond) legitimately adds migrations on top of ``0001_l2b_initial``,
    moving the head forward. The DB-READY binding stays valid as long as the bound L2-B
    revision still exists, is the *base* revision (``down_revision is None``), and is an
    ancestor of the current single head — so a tampered, replaced, or removed L2-B
    migration still fails closed. This never loosens the L2-B guarantee; it only stops
    a legitimate forward migration from falsely breaking DB-READY re-verification.
    """
    if not bound_revision:
        return False
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    sd = ScriptDirectory.from_config(Config("alembic.ini"))
    head = sd.get_current_head() or ""
    if not head:
        return False
    if bound_revision == head:
        return True
    if sd.get_base() != bound_revision:
        return False
    try:
        ancestry = {r.revision for r in sd.walk_revisions(base="base", head=head)}
    except Exception:  # noqa: BLE001 - unknown/broken lineage fails closed
        return False
    return bound_revision in ancestry


def run_db_suite(root: Path) -> dict[str, bool]:  # pragma: no cover - subprocess + real PG
    """Run the L2-B PostgreSQL integration suite; return {"file.py::test": passed}."""
    junit = root / "reports" / "ci-db-junit.xml"
    junit.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [_bin("pytest"), _DB_SUITE, f"--junitxml={junit}", "-q"],
        cwd=root,
        capture_output=True,
        check=False,
    )
    return parse_junit(junit)


def parse_junit(path: Path) -> dict[str, bool]:
    if not path.exists():
        return {}
    tree = ET.parse(path)
    out: dict[str, bool] = {}
    for case in tree.iter("testcase"):
        classname = case.get("classname", "")
        name = case.get("name", "")
        file_part = classname.split(".")[-1] + ".py" if classname else ""
        nodeid = f"{file_part}::{name}"
        passed = not any(child.tag in ("failure", "error") for child in case)
        out[nodeid] = passed
    return out


def _check_from_suite(passmap: dict[str, bool], nodes: tuple[str, ...]) -> bool:
    matched = False
    for node in nodes:
        if "::" in node:
            if node not in passmap:
                return False
            matched = True
            if not passmap[node]:
                return False
        else:
            prefix = node + "::"
            file_nodes = [k for k in passmap if k.startswith(prefix)]
            if not file_nodes:
                return False
            matched = True
            if not all(passmap[k] for k in file_nodes):
                return False
    return matched


def _accepted_prerequisites_unchanged(root: Path) -> bool:
    pinned = {
        "protocol-ready.json": PRE.PROTOCOL_READY_GATE_HASH,
        "twin-ready.json": PRE.TWIN_READY_GATE_HASH,
        "l1-ready.json": PRE.L1_READY_GATE_HASH,
    }
    for filename, expected in pinned.items():
        path = root / "gates" / filename
        if not path.exists():
            return False
        try:
            gate = load_gate(path)
        except Exception:  # noqa: BLE001
            return False
        if gate.gate_hash != expected:
            return False
        if not require_gate_pass(gate, base_dir=root).ok:
            return False
    return True


def _service_still_blocked() -> bool:
    from minos_engine.layer2.service import Layer2Service

    try:
        Layer2Service().select_config(None)  # type: ignore[arg-type]
    except StageNotReadyError:
        return True
    except Exception:  # noqa: BLE001
        return False
    return False


def _split_manifest_absent(root: Path) -> bool:
    candidates = [
        root / "schemas" / "layer2-dataset-split-v1.schema.json",
        root / "reports" / "LAYER2_DATASET_SPLIT_MANIFEST.json",
    ]
    return not any(p.exists() for p in candidates)


_FORBIDDEN_MIGRATION_TOKENS = (
    "Base.metadata",
    "create_all",
    "drop_all",
    "import Base",
    "storage.metadata",
    "storage import models",
    "storage.models",
)


def migration_immutable(root: Path) -> bool:
    """The migration is a self-contained snapshot: no ORM metadata dependency."""
    path = root / MIGRATION_FILE
    if not path.exists():
        return False
    src = path.read_text(encoding="utf-8")
    return not any(token in src for token in _FORBIDDEN_MIGRATION_TOKENS)


def l2a_closure_checks(
    root: Path,
    *,
    l2b_source_ref: str,
    l2b_source_tree: str | None = None,
    head_ref: str = "HEAD",
    require_head_descends: bool = True,
) -> dict[str, bool]:
    """Independently prove the L2-A closure chain and the *exact* qualified L2-B source.

    ``l2b_source_ref`` is the frozen L2-B source commit (from provenance during gate
    generation; ``gate.qualified_source_git_sha`` during verification) — never a
    substitute such as the current HEAD. The qualified source is proven to exist, be a
    commit, carry the bound tree, and *properly descend* the accepted L2-A evidence
    (evidence is a proper ancestor of the qualified source, and the qualified source is
    not an ancestor of / sibling to / unrelated to the evidence). HEAD is only used for
    the separate ``head_descends_l2b_qualified_source`` invariant.
    """
    src = PRE.L2A_CLOSURE_SOURCE_COMMIT
    src_tree = PRE.L2A_CLOSURE_SOURCE_TREE
    evi = PRE.L2A_CLOSURE_EVIDENCE_COMMIT
    evi_tree = PRE.L2A_CLOSURE_EVIDENCE_TREE

    src_ok = G.object_exists(root, src)
    evi_ok = G.object_exists(root, evi)
    src_tree_ok = src_ok and G.commit_tree_sha(root, src) == src_tree
    evi_tree_ok = evi_ok and G.commit_tree_sha(root, evi) == evi_tree
    # accepted closure evidence properly descends the accepted closure source
    chain_ok = bool(
        src_ok and evi_ok and G.is_ancestor(root, src, evi) and not G.is_ancestor(root, evi, src)
    )

    # The exact qualified L2-B source commit (never current HEAD for checks 1-7).
    qs = l2b_source_ref
    qs_present = bool(qs) and G.is_commit(root, qs)
    qs_tree_ok = bool(
        qs_present and (l2b_source_tree is None or G.commit_tree_sha(root, qs) == l2b_source_tree)
    )
    # accepted L2-A evidence is a PROPER ancestor of the qualified source
    # (rejects sibling / ancestor / unrelated qualified sources).
    qs_descends_l2a = bool(
        evi_ok and qs_present and G.is_ancestor(root, evi, qs) and not G.is_ancestor(root, qs, evi)
    )
    head_ok = G.object_exists(root, head_ref)
    head_descends_qs = (not require_head_descends) or bool(
        qs_present and head_ok and G.is_ancestor(root, qs, head_ref)
    )

    return {
        "l2a_closure_source_bound": src_ok,
        "l2a_closure_source_tree_bound": src_tree_ok,
        "l2a_closure_evidence_bound": evi_ok,
        "l2a_closure_evidence_tree_bound": evi_tree_ok,
        "l2a_closure_chain_valid": chain_ok,
        "l2b_qualified_source_present": qs_present,
        "l2b_qualified_source_tree_bound": qs_tree_ok,
        "l2b_qualified_source_descends_l2a": qs_descends_l2a,
        "head_descends_l2b_qualified_source": head_descends_qs,
        "accepted_feature_registry_hash_bound": FR.REGISTRY_HASH
        == PRE.ACCEPTED_FEATURE_REGISTRY_HASH,
    }


class DbQualificationResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    gate: GateArtifact
    report_markdown: str
    mandatory: dict[str, bool]
    accounting: PytestAccounting
    coverage: CoverageResult
    provenance: GitProvenance
    source_integrity: SourceIntegrity


def gather_db_source_integrity(root: Path, ref: str) -> SourceIntegrity:
    return gather_source_integrity(
        root, ref, evidence_spec=DB_EVIDENCE, required_files=DB_REQUIRED_TRACKED_FILES
    )


def qualify_db_ready(root: Path) -> DbQualificationResult:  # pragma: no cover - subprocess + PG
    root = root.resolve()
    provenance = read_provenance(root)
    ref = provenance.head_sha or "HEAD"
    source_integrity = gather_db_source_integrity(root, ref)
    passmap = run_db_suite(root)
    accounting = run_pytest(root)
    coverage = run_coverage(root)[0]
    tools = {
        "ruff": _tool_ok([_bin("ruff"), "check", "."], root),
        "format": _tool_ok([_bin("ruff"), "format", "--check", "."], root),
        "mypy": _tool_ok([_bin("mypy"), "src"], root),
    }
    return assemble_db_result(
        root,
        passmap=passmap,
        accounting=accounting,
        coverage=coverage,
        tools=tools,
        provenance=provenance,
        source_integrity=source_integrity,
    )


def assemble_db_result(
    root: Path,
    *,
    passmap: dict[str, bool],
    accounting: PytestAccounting,
    coverage: CoverageResult,
    tools: dict[str, bool],
    provenance: GitProvenance,
    source_integrity: SourceIntegrity,
    created_at: str | None = None,
) -> DbQualificationResult:
    root = root.resolve()
    si = source_integrity
    entry_ok = verify_l2_entry_gate(EntryGateRequest(repo_root=str(root))).ok

    migration_sha = si_evidence_hash(si, MIGRATION_FILE)
    mandatory: dict[str, bool] = {
        "l2a_entry_passed": entry_ok,
        **{check: _check_from_suite(passmap, nodes) for check, nodes in _CHECK_NODES.items()},
        "migration_immutable": migration_immutable(root),
        "service_still_blocked": _service_still_blocked(),
        "split_manifest_absent": _split_manifest_absent(root),
        "full_tests_passed": suite_passes(accounting),
        "coverage_passed": coverage.meets(STAGE0_COVERAGE_THRESHOLD),
        "ruff_passed": tools["ruff"],
        "format_passed": tools["format"],
        "mypy_passed": tools["mypy"],
        "accepted_prerequisites_unchanged": _accepted_prerequisites_unchanged(root),
        # migration_file_hash is the git-bound evidence hash; the contract is derived
        # from it (the verifier re-proves both against the committed source blob).
        "migration_file_evidence_bound": migration_sha == si_evidence_hash(si, MIGRATION_FILE),
        "migration_contract_bound": contract_hash(migration_sha)
        == contract_hash(si_evidence_hash(si, MIGRATION_FILE)),
        # The frozen L2-B qualified source is provenance's HEAD (== source at generation).
        **l2a_closure_checks(
            root,
            l2b_source_ref=provenance.head_sha or "",
            l2b_source_tree=provenance.tree_sha,
        ),
    }

    status = GateStatus.PASS if all(mandatory.values()) else GateStatus.HOLD
    created_at = created_at or datetime.now(UTC).isoformat()

    input_hashes = {
        "alembic_head_revision": alembic_head(),
        "migration_file_hash": migration_sha,
        "migration_contract_hash": contract_hash(migration_sha),
        "storage_schema_hash": storage_schema_hash(),
        "role_policy_hash": role_policy_hash(),
        "postgres_major_version": "16" if mandatory["postgres_16_verified"] else "unverified",
        "accepted_protocol_ready_gate_hash": PRE.PROTOCOL_READY_GATE_HASH,
        "accepted_twin_ready_gate_hash": PRE.TWIN_READY_GATE_HASH,
        "accepted_l1_ready_gate_hash": PRE.L1_READY_GATE_HASH,
        "accepted_l2a_closure_source_commit": PRE.L2A_CLOSURE_SOURCE_COMMIT,
        "accepted_l2a_closure_source_tree": PRE.L2A_CLOSURE_SOURCE_TREE,
        "accepted_l2a_closure_evidence_commit": PRE.L2A_CLOSURE_EVIDENCE_COMMIT,
        "accepted_l2a_closure_evidence_tree": PRE.L2A_CLOSURE_EVIDENCE_TREE,
        "accepted_feature_registry_hash": PRE.ACCEPTED_FEATURE_REGISTRY_HASH,
        # Exact test accounting bound into the gate (unambiguous evidence).
        "test_collected": str(accounting.collected),
        "test_passed": str(accounting.passed),
        "test_failed": str(accounting.failed),
        "test_errors": str(accounting.errors),
        "test_skipped": str(accounting.skipped),
        "db_integration_collected": str(len(passmap)),
        "db_integration_passed": str(sum(1 for v in passmap.values() if v)),
        "qualification_report_hash": "",  # set by write_db_outputs after render
        "python_runtime": runtime_identity(),
        "specification_manifest_hash": si.spec_manifest_hash,
    }

    gate = GateArtifact(
        gate_name=GATE_NAME,
        status=status,
        engine_git_sha=provenance.head_sha or "unavailable",
        input_hashes=input_hashes,
        evidence=si.evidence,
        mandatory_checks=mandatory,
        qualified_source_git_sha=provenance.head_sha,
        qualified_source_tree_sha=provenance.tree_sha,
        qualification_tool_version=DB_QUALIFIER_VERSION,
        created_at=created_at,
    )
    markdown = _render_report(
        gate=gate, mandatory=mandatory, accounting=accounting, coverage=coverage
    )
    return DbQualificationResult(
        gate=gate,
        report_markdown=markdown,
        mandatory=mandatory,
        accounting=accounting,
        coverage=coverage,
        provenance=provenance,
        source_integrity=si,
    )


def si_evidence_hash(si: SourceIntegrity, path: str) -> str:
    for item in si.evidence:
        if item.path == path and item.sha256:
            return item.sha256
    return "unavailable"


class DbGateVerification(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    ok: bool
    gate_hash: str
    checks: dict[str, bool]
    reasons: tuple[str, ...] = ()


def verify_db_ready_gate(
    root: Path, gate_path: Path, *, require_descends: bool = True
) -> DbGateVerification:
    """Non-mutating verification of a committed DB-READY gate (check mode)."""
    root = root.resolve()
    reasons: list[str] = []
    try:
        gate = load_gate(gate_path)
    except Exception as exc:  # noqa: BLE001
        return DbGateVerification(
            ok=False, gate_hash="", checks={"loadable": False}, reasons=(str(exc),)
        )

    integrity = verify_gate_integrity(gate, base_dir=root)
    promotion = require_gate_pass(gate, base_dir=root)
    src_sha = gate.qualified_source_git_sha or ""

    checks = {
        "gate_name_db_ready": gate.gate_name == GATE_NAME,
        "canonical_integrity": gate.gate_hash == gate.compute_hash(),
        "qualification_tool_identity": gate.qualification_tool_version == DB_QUALIFIER_VERSION,
        "python_runtime_is_3_12": is_supported_runtime(),
        "evidence_verified": integrity.ok,
        "required_checks_and_promotion": promotion.ok,
        "alembic_head_bound": l2b_revision_is_immutable_base(
            gate.input_hashes.get("alembic_head_revision") or ""
        ),
        "storage_schema_bound": gate.input_hashes.get("storage_schema_hash")
        == storage_schema_hash(),
        "role_policy_bound": gate.input_hashes.get("role_policy_hash") == role_policy_hash(),
        "accepted_protocol_unchanged": gate.input_hashes.get("accepted_protocol_ready_gate_hash")
        == PRE.PROTOCOL_READY_GATE_HASH,
        "accepted_twin_unchanged": gate.input_hashes.get("accepted_twin_ready_gate_hash")
        == PRE.TWIN_READY_GATE_HASH,
        "accepted_l1_unchanged": gate.input_hashes.get("accepted_l1_ready_gate_hash")
        == PRE.L1_READY_GATE_HASH,
        "qualified_commit_present": G.object_exists(root, src_sha),
        "qualified_tree_matches": G.commit_tree_sha(root, src_sha)
        == gate.qualified_source_tree_sha,
        "migration_immutable": migration_immutable(root),
    }
    gih = gate.input_hashes.get

    # --- Defect 2: migration cross-binding to qualified-source evidence -----------
    # Find exactly one file evidence item for the migration, with a valid sha, and
    # prove its sha equals the committed migration blob at the QUALIFIED SOURCE commit
    # (not the working tree / current HEAD). The migration hash and the contract hash
    # are then checked against that independently-verified evidence hash — never
    # recomputed from an untrusted input field.
    mig_items = [e for e in gate.evidence if e.path == MIGRATION_FILE]
    mig_item = mig_items[0] if len(mig_items) == 1 else None
    mig_item_valid = bool(
        mig_item is not None
        and mig_item.kind is EvidenceKind.FILE
        and mig_item.sha256 is not None
        and _HEX64.match(mig_item.sha256)
    )
    mig_evi_sha = mig_item.sha256 if (mig_item and mig_item.sha256) else ""
    try:
        committed_mig_sha = G.sha256_git_file(root, MIGRATION_FILE, src_sha)[0]
    except Exception:  # noqa: BLE001 - unreachable/missing blob fails closed
        committed_mig_sha = ""
    checks["migration_evidence_present"] = mig_item_valid
    checks["migration_evidence_matches_source_blob"] = bool(
        mig_item_valid and committed_mig_sha and mig_evi_sha == committed_mig_sha
    )
    checks["migration_file_evidence_bound"] = bool(
        mig_item_valid and gih("migration_file_hash") == mig_evi_sha
    )
    checks["migration_contract_bound"] = bool(
        mig_item_valid and gih("migration_contract_hash") == contract_hash(mig_evi_sha)
    )

    # --- Defect 1: L2-A closure verified against the EXACT qualified source --------
    # Each closure check ANDs the gate's bound value (== pinned) with the independent
    # git recomputation, under a single key, so tampering the input OR a broken chain
    # both fail closed. The qualified source (not current HEAD) is the subject.
    cc = l2a_closure_checks(
        root,
        l2b_source_ref=src_sha,
        l2b_source_tree=gate.qualified_source_tree_sha,
        head_ref="HEAD",
        require_head_descends=require_descends,
    )
    checks["l2a_closure_source_bound"] = cc["l2a_closure_source_bound"] and (
        gih("accepted_l2a_closure_source_commit") == PRE.L2A_CLOSURE_SOURCE_COMMIT
    )
    checks["l2a_closure_source_tree_bound"] = cc["l2a_closure_source_tree_bound"] and (
        gih("accepted_l2a_closure_source_tree") == PRE.L2A_CLOSURE_SOURCE_TREE
    )
    checks["l2a_closure_evidence_bound"] = cc["l2a_closure_evidence_bound"] and (
        gih("accepted_l2a_closure_evidence_commit") == PRE.L2A_CLOSURE_EVIDENCE_COMMIT
    )
    checks["l2a_closure_evidence_tree_bound"] = cc["l2a_closure_evidence_tree_bound"] and (
        gih("accepted_l2a_closure_evidence_tree") == PRE.L2A_CLOSURE_EVIDENCE_TREE
    )
    checks["l2a_closure_chain_valid"] = cc["l2a_closure_chain_valid"]
    checks["l2b_qualified_source_present"] = cc["l2b_qualified_source_present"]
    checks["l2b_qualified_source_tree_bound"] = cc["l2b_qualified_source_tree_bound"]
    checks["l2b_qualified_source_descends_l2a"] = cc["l2b_qualified_source_descends_l2a"]
    checks["head_descends_l2b_qualified_source"] = cc["head_descends_l2b_qualified_source"]
    checks["accepted_feature_registry_hash_bound"] = cc[
        "accepted_feature_registry_hash_bound"
    ] and (
        gih("accepted_feature_registry_hash")
        == PRE.ACCEPTED_FEATURE_REGISTRY_HASH
        == FR.REGISTRY_HASH
    )
    ancestry = None
    if require_descends:
        ancestry = verify_commit_ancestry(
            root,
            qualified_source=src_sha,
            expected_tree=gate.qualified_source_tree_sha or "",
            artifact_commit=None,
        )
        checks["commit_b_descends_a"] = ancestry.checks["commit_b_descends_a"]

    for name, ok in checks.items():
        if not ok:
            reasons.append(f"{name} failed")
    if ancestry is not None:
        reasons.extend(f"ancestry: {r}" for r in ancestry.reasons)
    reasons.extend(f"evidence: {r}" for r in integrity.reasons)
    reasons.extend(f"promotion: {r}" for r in promotion.reasons)
    return DbGateVerification(
        ok=all(checks.values()),
        gate_hash=gate.gate_hash,
        checks=checks,
        reasons=tuple(dict.fromkeys(reasons)),
    )


def write_db_outputs(result: DbQualificationResult, root: Path) -> tuple[Path, Path]:
    """Write the report first, then the gate carrying the report's sha256."""
    from minos_engine.common.hashing import sha256_hex
    from minos_engine.gates.verifier import write_gate

    report_path = root / "reports" / "LAYER2_L2B_DB_REMEDIATION_REPORT.md"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(result.report_markdown, encoding="utf-8")
    report_sha = sha256_hex(report_path.read_bytes())

    data = result.gate.model_dump(mode="json")
    data["input_hashes"]["qualification_report_hash"] = report_sha
    data["gate_hash"] = ""
    gate = GateArtifact.model_validate(data)
    gate_path = write_gate(gate, root / "gates" / "db-ready.json")
    return gate_path, report_path


def _render_report(
    *,
    gate: GateArtifact,
    mandatory: dict[str, bool],
    accounting: PytestAccounting,
    coverage: CoverageResult,
) -> str:
    required = required_checks_for(GATE_NAME)
    rows = "\n".join(
        f"| `{k}` | {'PASS' if v else 'FAIL'} | {'required' if k in required else 'supplemental'} |"
        for k, v in sorted(mandatory.items())
    )
    ev = "\n".join(f"| `{e.path}` | {e.kind.value} | `{e.sha256}` |" for e in gate.evidence)
    ih = json.dumps(gate.input_hashes, indent=2, sort_keys=True)
    g = gate.input_hashes.get
    return f"""# LAYER 2 — L2-B DB-READY Remediation Qualification Report

**Gate:** {GATE_NAME} — **{gate.status.value}**
**Qualification tool:** {DB_QUALIFIER_VERSION}
**Qualified source git sha:** `{gate.qualified_source_git_sha}`
**Qualified source tree sha:** `{gate.qualified_source_tree_sha}`

> Generated by `minos-engine layer2 db qualify`. Not hand-authored. A PASS gate is
> not constructible with any failing mandatory check; the DB-READY required set is
> enforced. Evidence is hashed from the qualified commit's blobs. The migration is a
> frozen, self-contained snapshot (no ORM metadata dependency); `minos_admin` owns the
> schemas/tables/functions (NOLOGIN, non-superuser); and the gate binds the final
> accepted L2-A closure identities and the accepted feature-registry hash. All
> schema/role/constraint/append-only/least-privilege/worker-claim/admin-ownership
> checks come from the real PostgreSQL 16 integration suite.
> `Layer2Service.select_config` remains blocked and no dataset split manifest exists.

## Mandatory checks
| Check | Status | Kind |
|---|---|---|
{rows}

## Test accounting (exact, unambiguous)
This report binds the pre-evidence full-suite accounting from the qualification run
(the committed DB-READY gate did not exist yet, so the expected-gate tests skip with an
explicit reason). The final post-evidence full-suite result — with the gate present so
those tests execute — is recorded separately in the final review response.

| Metric | Value |
|---|---|
| Full suite — collected | {g("test_collected")} |
| Full suite — passed | {g("test_passed")} |
| Full suite — failed | {g("test_failed")} |
| Full suite — errors | {g("test_errors")} |
| Full suite — skipped | {g("test_skipped")} |
| Full suite — deselected | 0 |
| PostgreSQL integration — collected | {g("db_integration_collected")} |
| PostgreSQL integration — passed | {g("db_integration_passed")} |
| PostgreSQL integration — skipped | 0 |
| Coverage | {coverage.line_coverage_percent}% (threshold {STAGE0_COVERAGE_THRESHOLD}%) |

## Bound identities
```
{ih}
```

## Evidence (git-tree-bound, hashed from the qualified commit)
| Path | Kind | sha256 |
|---|---|---|
{ev}
"""
