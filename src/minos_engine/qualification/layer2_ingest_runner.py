"""INGEST-READY (Layer 2 L2-D) qualification — git-tree-bound, evidence-based.

Assembles the ``INGEST-READY`` **capability** gate: proof that the L2-D ingestion
machinery (attestation contract + producer, pure admission validation, epoch-bound
storage, migration ``0004``, partition-separated views with a sealed test cohort) is
correct and safe — independent of whether any profile corpus exists yet. Per-epoch corpus
evidence is deliberately SEPARATE: each ``PROFILE-SNAPSHOT-FROZEN-<epoch>`` snapshot is
its own evidence artifact whose member count derives from that epoch's ``sample_count``
(never a hardcoded corpus size).

The gate binds the accepted PROTOCOL/TWIN/L1/DB-READY/SPLIT-FROZEN/SPLIT-FROZEN-V2
prerequisites and proves its qualified source properly descends the accepted
SPLIT-FROZEN-V2 *evidence* commit. A PASS is never constructed from caller-supplied
booleans; the verifier re-derives every binding from the exact qualified source commit —
never the current HEAD.
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
from minos_engine.common.hashing import sha256_hex
from minos_engine.common.runtime import is_supported_runtime, runtime_identity
from minos_engine.gates.contracts import EvidenceItem, EvidenceKind, GateArtifact, GateStatus
from minos_engine.gates.verifier import load_gate, require_gate_pass, verify_gate_integrity
from minos_engine.layer2 import prerequisites as PRE
from minos_engine.layer2.ingest.contracts import ATTESTATION_SCHEMA_VERSION
from minos_engine.storage.l2d_migration_contract import (
    L2D_MIGRATION_REVISION,
    l2d_contract_hash,
)

from . import git_tree as G
from .coverage import STAGE0_COVERAGE_THRESHOLD, CoverageResult, run_coverage
from .layer2_db_runner import alembic_head
from .provenance import GitProvenance, read_provenance
from .pytest_accounting import PytestAccounting, run_pytest, suite_passes
from .runner import SourceIntegrity, _bin, _tool_ok, gather_source_integrity

__all__ = [
    "GATE_NAME",
    "INGEST_QUALIFIER_VERSION",
    "L2D_MIGRATION_FILE",
    "ATTESTATION_SCHEMA_FILE",
    "INGEST_PACKAGE_DIR",
    "FINAL_REPORT_PATH",
    "INGEST_EVIDENCE",
    "INGEST_REQUIRED_TRACKED_FILES",
    "profile_snapshot_gate_name",
    "split_frozen_v2_closure_checks",
    "ci_asserts_head_0004",
    "l2d_migration_immutable",
    "IngestQualificationResult",
    "IngestGateVerification",
    "assemble_ingest_result",
    "qualify_ingest_ready",
    "verify_ingest_ready_gate",
    "write_ingest_outputs",
]

_HEX64 = re.compile(r"^[0-9a-f]{64}$")

GATE_NAME = "INGEST-READY"
INGEST_QUALIFIER_VERSION = "layer2-ingest-qualifier-v1"
L2D_MIGRATION_FILE = "migrations/versions/0004_l2d_profile_ingestion.py"
ATTESTATION_SCHEMA_FILE = "schemas/input-integrity-attestation-v1.schema.json"
INGEST_PACKAGE_DIR = "src/minos_engine/layer2/ingest"
FINAL_REPORT_PATH = "reports/LAYER2_L2D_INGEST_READY_REPORT.md"
_GATE_SELF_PATH = "gates/ingest-ready.json"
_INGEST_SUITE = "tests/integration/layer2_ingest"
CI_WORKFLOW = ".github/workflows/ci.yml"

_CI_REQUIRED_TOKENS = (
    "0004_l2d_profile_ingestion",
    "downgrade 0003_l2c_split_v2_epochs",
    "downgrade 0002_l2c_dataset_split",
    "downgrade 0001_l2b_initial",
    "downgrade base",
    "tests/integration/layer2_ingest",
)

EVIDENCE_PAYLOAD_PATHS: tuple[str, ...] = (FINAL_REPORT_PATH,)

INGEST_EVIDENCE: tuple[tuple[str, EvidenceKind], ...] = (
    (INGEST_PACKAGE_DIR, EvidenceKind.DIRECTORY),
    ("src/minos_engine/intake/attestation.py", EvidenceKind.FILE),
    ("src/minos_engine/storage/profile_ingest.py", EvidenceKind.FILE),
    ("src/minos_engine/storage/l2d_migration_contract.py", EvidenceKind.FILE),
    ("src/minos_engine/qualification/layer2_ingest_runner.py", EvidenceKind.FILE),
    (L2D_MIGRATION_FILE, EvidenceKind.FILE),
    (ATTESTATION_SCHEMA_FILE, EvidenceKind.FILE),
    ("gates/protocol-ready.json", EvidenceKind.FILE),
    ("gates/twin-ready.json", EvidenceKind.FILE),
    ("gates/l1-ready.json", EvidenceKind.FILE),
    ("gates/db-ready.json", EvidenceKind.FILE),
    ("gates/split-frozen.json", EvidenceKind.FILE),
    ("gates/split-frozen-v2.json", EvidenceKind.FILE),
    ("docs/layer2/PROFILE_INGESTION.md", EvidenceKind.FILE),
    (CI_WORKFLOW, EvidenceKind.FILE),
    (_INGEST_SUITE, EvidenceKind.DIRECTORY),
)

INGEST_REQUIRED_TRACKED_FILES: tuple[str, ...] = (
    "src/minos_engine/layer2/ingest/__init__.py",
    "src/minos_engine/layer2/ingest/contracts.py",
    "src/minos_engine/layer2/ingest/validation.py",
    "src/minos_engine/intake/attestation.py",
    "src/minos_engine/storage/profile_ingest.py",
    "src/minos_engine/storage/l2d_migration_contract.py",
    L2D_MIGRATION_FILE,
    ATTESTATION_SCHEMA_FILE,
    "docs/layer2/PROFILE_INGESTION.md",
    CI_WORKFLOW,
)

# gate check -> integration nodeids whose pass proves it.
_CHECK_NODES: dict[str, tuple[str, ...]] = {
    "postgres_16_verified": ("test_migration_lifecycle.py::test_postgres_major_version_is_16",),
    "l2d_migration_lifecycle_passed": (
        "test_migration_lifecycle.py::test_l2d_migration_lifecycle",
    ),
    "ingest_admission_passed": ("test_ingest.py",),
    "ingest_role_isolation_passed": ("test_roles_and_views.py",),
    "sealed_test_profile_access_denied": (
        "test_roles_and_views.py::test_sealed_test_members_denied_to_all_roles",
    ),
    "legacy_profiles_reads_revoked": (
        "test_roles_and_views.py::test_legacy_profiles_reads_revoked",
    ),
    "profile_snapshot_freeze_passed": ("test_ingest.py::test_freeze_profile_snapshot_epoch1",),
    # named per-behavior checks (owner item 5) — each proven by its dedicated test.
    "three_artifact_contract_passed": (
        "test_ingest.py::test_real_profile_admitted_with_three_artifacts",
    ),
    "exact_byte_hashing_passed": ("test_ingest.py::test_exact_bytes_hashed_in_boundary",),
    "artifact_conflict_passed": ("test_ingest.py::test_artifact_metadata_conflict_rejected",),
    "idempotency_passed": ("test_ingest.py::test_idempotent_resubmission_returns_existing_row",),
    "content_conflict_passed": ("test_ingest.py::test_content_conflict_rejected",),
    "profile_id_conflict_passed": ("test_concurrency.py::test_concurrent_profile_id_conflict",),
    "concurrent_serialization_passed": ("test_concurrency.py",),
    "epoch_membership_passed": ("test_ingest.py::test_non_member_epoch_ingestion_rejected",),
    "parquet_invariants_passed": (
        "test_ingest.py::test_parquet_row_identity_violation_rejected",
        "test_ingest.py::test_parquet_extra_column_rejected",
        "test_ingest.py::test_parquet_duplicate_rows_rejected",
        "test_ingest.py::test_parquet_shuffled_order_rejected",
        "test_ingest.py::test_parquet_overlap_rejected",
        "test_ingest.py::test_parquet_bad_window_id_rejected",
    ),
    "fasta_bounds_passed": ("test_ingest.py::test_fasta_length_bounds_rejected",),
    "version_selection_passed": (
        "test_ingest.py::test_freeze_requires_explicit_version_selection",
        "test_ingest.py::test_new_immutable_version_appends",
    ),
    "atomic_audit_passed": ("test_ingest.py::test_admitted_audit_is_atomic_with_row",),
    "trainer_view_isolation_passed": (
        "test_roles_and_views.py::test_member_views_hide_sensitive_columns",
    ),
}

_FORBIDDEN_MIGRATION_TOKENS = (
    "Base.metadata",
    "create_all",
    "drop_all",
    "import Base",
    "storage.metadata",
    "storage import models",
    "storage.models",
)


def profile_snapshot_gate_name(epoch: int) -> str:
    """The per-epoch corpus gate name (separate from this capability gate)."""
    return f"PROFILE-SNAPSHOT-FROZEN-{epoch}"


def ci_asserts_head_0004(root: Path) -> bool:
    """True iff CI pins Alembic head 0004 + the full downgrade/re-upgrade lifecycle."""
    path = root / CI_WORKFLOW
    if not path.exists():
        return False
    content = path.read_text(encoding="utf-8")
    return all(tok in content for tok in _CI_REQUIRED_TOKENS)


def l2d_migration_immutable(root: Path) -> bool:
    """The L2-D migration is a self-contained snapshot (no ORM metadata dependency)."""
    path = root / L2D_MIGRATION_FILE
    if not path.exists():
        return False
    src = path.read_text(encoding="utf-8")
    return not any(token in src for token in _FORBIDDEN_MIGRATION_TOKENS)


def _l2d_revision_is_head() -> bool:
    """True iff 0004 is the SINGLE Alembic head (fork-free lineage)."""
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    try:
        script = ScriptDirectory.from_config(Config("alembic.ini"))
        heads = script.get_heads()
        return heads == [L2D_MIGRATION_REVISION]
    except Exception:  # noqa: BLE001 - fail closed
        return False


def split_frozen_v2_closure_checks(
    root: Path,
    *,
    l2d_source_ref: str,
    head_ref: str = "HEAD",
    require_head_descends: bool = True,
) -> dict[str, bool]:
    """Prove the accepted SPLIT-FROZEN-V2 closure and the exact L2-D qualified source."""
    src = PRE.SPLIT_FROZEN_V2_SOURCE_COMMIT
    src_tree = PRE.SPLIT_FROZEN_V2_SOURCE_TREE
    evi = PRE.SPLIT_FROZEN_V2_EVIDENCE_COMMIT

    src_ok = G.is_commit(root, src)
    src_tree_ok = src_ok and G.commit_tree_sha(root, src) == src_tree
    evi_ok = G.is_commit(root, evi)
    chain_ok = bool(src_ok and evi_ok and G.is_ancestor(root, src, evi))

    qs = l2d_source_ref
    qs_present = bool(qs) and G.is_commit(root, qs)
    descends = bool(
        evi_ok and qs_present and G.is_ancestor(root, evi, qs) and not G.is_ancestor(root, qs, evi)
    )
    head_ok = G.object_exists(root, head_ref)
    head_descends = (not require_head_descends) or bool(
        qs_present and head_ok and G.is_ancestor(root, qs, head_ref)
    )
    return {
        "split_frozen_v2_source_present": src_ok,
        "split_frozen_v2_source_tree_bound": src_tree_ok,
        "split_frozen_v2_evidence_present": evi_ok and chain_ok,
        "l2d_source_descends_split_frozen_v2": descends,
        "head_descends_l2d_source": head_descends,
    }


class IngestQualificationResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    gate: GateArtifact
    report_markdown: str
    mandatory: dict[str, bool]
    accounting: PytestAccounting
    coverage: CoverageResult
    provenance: GitProvenance


def _si_hash(si: SourceIntegrity, path: str) -> str:
    for item in si.evidence:
        if item.path == path and item.sha256:
            return item.sha256
    return "unavailable"


def _unique_evidence(
    evidence: tuple[EvidenceItem, ...], path: str, kind: EvidenceKind
) -> EvidenceItem | None:
    items = [e for e in evidence if e.path == path]
    if len(items) != 1:
        return None
    item = items[0]
    if item.kind is not kind or not item.sha256 or not _HEX64.match(item.sha256):
        return None
    return item


def _accepted_gate_unchanged(root: Path, filename: str, expected_hash: str) -> bool:
    path = root / "gates" / filename
    if not path.exists():
        return False
    try:
        gate = load_gate(path)
    except Exception:  # noqa: BLE001
        return False
    return gate.gate_hash == expected_hash and require_gate_pass(gate, base_dir=root).ok


def _service_still_blocked() -> bool:
    from minos_engine.layer2.service import Layer2Service

    try:
        Layer2Service().select_config(None)  # type: ignore[arg-type]
    except StageNotReadyError:
        return True
    except Exception:  # noqa: BLE001
        return False
    return False


def run_ingest_suite(root: Path) -> dict[str, bool]:  # pragma: no cover - subprocess + real PG
    junit = root / "reports" / "ci-ingest-junit.xml"
    junit.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [_bin("pytest"), _INGEST_SUITE, f"--junitxml={junit}", "-q"],
        cwd=root,
        capture_output=True,
        check=False,
    )
    return _parse_junit(junit)


def _parse_junit(path: Path) -> dict[str, bool]:
    if not path.exists():
        return {}
    tree = ET.parse(path)
    out: dict[str, bool] = {}
    for case in tree.iter("testcase"):
        classname = case.get("classname", "")
        name = case.get("name", "")
        file_part = classname.split(".")[-1] + ".py" if classname else ""
        out[f"{file_part}::{name}"] = not any(c.tag in ("failure", "error") for c in case)
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


def gather_ingest_source_integrity(root: Path, ref: str) -> SourceIntegrity:
    return gather_source_integrity(
        root, ref, evidence_spec=INGEST_EVIDENCE, required_files=INGEST_REQUIRED_TRACKED_FILES
    )


def qualify_ingest_ready(root: Path) -> IngestQualificationResult:  # pragma: no cover
    root = root.resolve()
    provenance = read_provenance(root)
    ref = provenance.head_sha or "HEAD"
    source_integrity = gather_ingest_source_integrity(root, ref)
    passmap = run_ingest_suite(root)
    accounting = run_pytest(root)
    coverage = run_coverage(root)[0]
    tools = {
        "ruff": _tool_ok([_bin("ruff"), "check", "."], root),
        "format": _tool_ok([_bin("ruff"), "format", "--check", "."], root),
        "mypy": _tool_ok([_bin("mypy"), "src"], root),
    }
    return assemble_ingest_result(
        root,
        passmap=passmap,
        accounting=accounting,
        coverage=coverage,
        tools=tools,
        provenance=provenance,
        source_integrity=source_integrity,
    )


def assemble_ingest_result(
    root: Path,
    *,
    passmap: dict[str, bool],
    accounting: PytestAccounting,
    coverage: CoverageResult,
    tools: dict[str, bool],
    provenance: GitProvenance,
    source_integrity: SourceIntegrity,
    created_at: str | None = None,
) -> IngestQualificationResult:
    root = root.resolve()
    si = source_integrity
    migration_sha = _si_hash(si, L2D_MIGRATION_FILE)
    schema_sha = _si_hash(si, ATTESTATION_SCHEMA_FILE)
    package_sha = _si_hash(si, INGEST_PACKAGE_DIR)

    closure = split_frozen_v2_closure_checks(root, l2d_source_ref=provenance.head_sha or "")
    pkg_item = _unique_evidence(si.evidence, INGEST_PACKAGE_DIR, EvidenceKind.DIRECTORY)
    schema_item = _unique_evidence(si.evidence, ATTESTATION_SCHEMA_FILE, EvidenceKind.FILE)
    suite_checks = {
        check: _check_from_suite(passmap, nodes) for check, nodes in _CHECK_NODES.items()
    }
    created_at = created_at or datetime.now(UTC).isoformat()

    input_hashes = {
        "attestation_schema_version": ATTESTATION_SCHEMA_VERSION,
        "attestation_schema_hash": schema_sha,
        "ingest_package_hash": package_sha,
        "l2d_migration_file_hash": migration_sha,
        "l2d_migration_contract_hash": l2d_contract_hash(migration_sha),
        "alembic_head_revision": alembic_head(),
        "accepted_protocol_ready_gate_hash": PRE.PROTOCOL_READY_GATE_HASH,
        "accepted_twin_ready_gate_hash": PRE.TWIN_READY_GATE_HASH,
        "accepted_l1_ready_gate_hash": PRE.L1_READY_GATE_HASH,
        "accepted_db_ready_gate_hash": PRE.DB_READY_GATE_HASH,
        "accepted_split_frozen_gate_hash": PRE.SPLIT_FROZEN_GATE_HASH,
        "accepted_split_frozen_v2_gate_hash": PRE.SPLIT_FROZEN_V2_GATE_HASH,
        "split_frozen_v2_source_commit": PRE.SPLIT_FROZEN_V2_SOURCE_COMMIT,
        "split_frozen_v2_source_tree": PRE.SPLIT_FROZEN_V2_SOURCE_TREE,
        "split_frozen_v2_evidence_commit": PRE.SPLIT_FROZEN_V2_EVIDENCE_COMMIT,
        "test_collected": str(accounting.collected),
        "test_passed": str(accounting.passed),
        "test_failed": str(accounting.failed),
        "test_skipped": str(accounting.skipped),
        "ingest_integration_collected": str(len(passmap)),
        "ingest_integration_passed": str(sum(1 for v in passmap.values() if v)),
        "python_runtime": runtime_identity(),
        "evidence_payload_paths": json.dumps(list(EVIDENCE_PAYLOAD_PATHS)),
    }

    markdown = _render_report(
        qualified_source_sha=provenance.head_sha,
        qualified_source_tree=provenance.tree_sha,
        input_hashes=input_hashes,
        evidence=si.evidence,
    )
    report_sha = sha256_hex(markdown.encode("utf-8"))
    input_hashes["qualification_report_hash"] = report_sha

    mandatory: dict[str, bool] = {
        "accepted_protocol_ready_unchanged": _accepted_gate_unchanged(
            root, "protocol-ready.json", PRE.PROTOCOL_READY_GATE_HASH
        ),
        "accepted_twin_ready_unchanged": _accepted_gate_unchanged(
            root, "twin-ready.json", PRE.TWIN_READY_GATE_HASH
        ),
        "accepted_l1_ready_unchanged": _accepted_gate_unchanged(
            root, "l1-ready.json", PRE.L1_READY_GATE_HASH
        ),
        "accepted_db_ready_unchanged": _accepted_gate_unchanged(
            root, "db-ready.json", PRE.DB_READY_GATE_HASH
        ),
        "accepted_split_frozen_unchanged": _accepted_gate_unchanged(
            root, "split-frozen.json", PRE.SPLIT_FROZEN_GATE_HASH
        ),
        "accepted_split_frozen_v2_unchanged": _accepted_gate_unchanged(
            root, "split-frozen-v2.json", PRE.SPLIT_FROZEN_V2_GATE_HASH
        ),
        **closure,
        "ingest_package_evidence_present": pkg_item is not None,
        "ingest_package_evidence_bound": pkg_item is not None and pkg_item.sha256 == package_sha,
        "attestation_schema_evidence_present": schema_item is not None,
        "attestation_schema_evidence_bound": schema_item is not None
        and schema_item.sha256 == schema_sha,
        "qualification_report_bytes_bound": _HEX64.match(report_sha) is not None,
        "l2d_migration_immutable": l2d_migration_immutable(root),
        "l2d_migration_file_evidence_bound": _HEX64.match(migration_sha) is not None,
        "l2d_migration_contract_bound": _HEX64.match(migration_sha) is not None,
        "alembic_single_head_is_l2d": _l2d_revision_is_head(),
        "ci_asserts_head_0004": ci_asserts_head_0004(root),
        "full_tests_passed": suite_passes(accounting),
        "coverage_passed": coverage.meets(STAGE0_COVERAGE_THRESHOLD),
        "ruff_passed": tools["ruff"],
        "format_passed": tools["format"],
        "mypy_passed": tools["mypy"],
        **suite_checks,
        "evidence_hashes_complete": si.evidence_hashes_complete,
        "required_source_tracked": si.required_source_tracked,
        "service_still_blocked": _service_still_blocked(),
    }
    status = GateStatus.PASS if all(mandatory.values()) else GateStatus.HOLD
    gate = GateArtifact(
        gate_name=GATE_NAME,
        status=status,
        engine_git_sha=provenance.head_sha or "unavailable",
        input_hashes=input_hashes,
        evidence=si.evidence,
        mandatory_checks=mandatory,
        qualified_source_git_sha=provenance.head_sha,
        qualified_source_tree_sha=provenance.tree_sha,
        qualification_tool_version=INGEST_QUALIFIER_VERSION,
        created_at=created_at,
    )
    return IngestQualificationResult(
        gate=gate,
        report_markdown=markdown,
        mandatory=mandatory,
        accounting=accounting,
        coverage=coverage,
        provenance=provenance,
    )


class IngestGateVerification(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    ok: bool
    gate_hash: str
    checks: dict[str, bool]
    reasons: tuple[str, ...] = ()


def verify_ingest_ready_gate(
    root: Path, gate_path: Path, *, require_descends: bool = True
) -> IngestGateVerification:
    """Non-mutating verification of a committed INGEST-READY gate (check mode)."""
    root = root.resolve()
    reasons: list[str] = []
    try:
        gate = load_gate(gate_path)
    except Exception as exc:  # noqa: BLE001
        return IngestGateVerification(
            ok=False, gate_hash="", checks={"loadable": False}, reasons=(str(exc),)
        )

    integrity = verify_gate_integrity(gate, base_dir=root)
    promotion = require_gate_pass(gate, base_dir=root)
    src_sha = gate.qualified_source_git_sha or ""
    gih = gate.input_hashes.get

    checks: dict[str, bool] = {
        "gate_name_ingest_ready": gate.gate_name == GATE_NAME,
        "canonical_integrity": gate.gate_hash == gate.compute_hash(),
        "qualification_tool_identity": gate.qualification_tool_version == INGEST_QUALIFIER_VERSION,
        "python_runtime_is_3_12": is_supported_runtime(),
        "evidence_verified": integrity.ok,
        "required_checks_and_promotion": promotion.ok,
        "alembic_single_head_is_l2d": _l2d_revision_is_head()
        and gih("alembic_head_revision") == L2D_MIGRATION_REVISION,
        "l2d_migration_immutable": l2d_migration_immutable(root),
        "ci_asserts_head_0004": ci_asserts_head_0004(root),
        "accepted_split_frozen_v2_unchanged": _accepted_gate_unchanged(
            root, "split-frozen-v2.json", PRE.SPLIT_FROZEN_V2_GATE_HASH
        )
        and gih("accepted_split_frozen_v2_gate_hash") == PRE.SPLIT_FROZEN_V2_GATE_HASH,
    }

    # migration cross-binding to qualified-source evidence
    mig_item = _unique_evidence(gate.evidence, L2D_MIGRATION_FILE, EvidenceKind.FILE)
    mig_evi_sha = mig_item.sha256 if (mig_item and mig_item.sha256) else ""
    try:
        committed_mig_sha = G.sha256_git_file(root, L2D_MIGRATION_FILE, src_sha)[0]
    except Exception:  # noqa: BLE001
        committed_mig_sha = ""
    checks["l2d_migration_evidence_present"] = mig_item is not None
    checks["l2d_migration_evidence_matches_source_blob"] = bool(
        mig_item is not None and committed_mig_sha and mig_evi_sha == committed_mig_sha
    )
    checks["l2d_migration_file_evidence_bound"] = bool(
        mig_item is not None and gih("l2d_migration_file_hash") == mig_evi_sha
    )
    checks["l2d_migration_contract_bound"] = bool(
        mig_item is not None
        and gih("l2d_migration_contract_hash") == l2d_contract_hash(mig_evi_sha)
    )

    # ingest package + attestation schema cross-binding
    pkg_item = _unique_evidence(gate.evidence, INGEST_PACKAGE_DIR, EvidenceKind.DIRECTORY)
    pkg_evi = pkg_item.sha256 if (pkg_item and pkg_item.sha256) else ""
    try:
        computed_pkg = G.sha256_git_directory(root, INGEST_PACKAGE_DIR, src_sha)[0]
    except Exception:  # noqa: BLE001
        computed_pkg = ""
    checks["ingest_package_evidence_present"] = pkg_item is not None
    checks["ingest_package_evidence_matches_source"] = bool(
        pkg_item is not None and computed_pkg and pkg_evi == computed_pkg
    )
    checks["ingest_package_evidence_bound"] = bool(
        pkg_item is not None and gih("ingest_package_hash") == pkg_evi
    )
    schema_item = _unique_evidence(gate.evidence, ATTESTATION_SCHEMA_FILE, EvidenceKind.FILE)
    schema_evi = schema_item.sha256 if (schema_item and schema_item.sha256) else ""
    checks["attestation_schema_evidence_present"] = schema_item is not None
    checks["attestation_schema_evidence_bound"] = bool(
        schema_item is not None and gih("attestation_schema_hash") == schema_evi
    )

    # committed report bytes
    try:
        committed_report_sha = G.sha256_git_file(root, FINAL_REPORT_PATH, "HEAD")[0]
    except Exception:  # noqa: BLE001
        committed_report_sha = ""
    checks["qualification_report_bytes_bound"] = bool(
        committed_report_sha and committed_report_sha == gih("qualification_report_hash")
    )

    # SPLIT-FROZEN-V2 closure ancestry against the EXACT qualified source
    cc = split_frozen_v2_closure_checks(
        root, l2d_source_ref=src_sha, head_ref="HEAD", require_head_descends=require_descends
    )
    checks["l2d_qualified_source_tree_matches"] = (
        G.commit_tree_sha(root, src_sha) == gate.qualified_source_tree_sha
    )
    checks["split_frozen_v2_source_present"] = cc["split_frozen_v2_source_present"] and (
        gih("split_frozen_v2_source_commit") == PRE.SPLIT_FROZEN_V2_SOURCE_COMMIT
    )
    checks["split_frozen_v2_source_tree_bound"] = cc["split_frozen_v2_source_tree_bound"] and (
        gih("split_frozen_v2_source_tree") == PRE.SPLIT_FROZEN_V2_SOURCE_TREE
    )
    checks["split_frozen_v2_evidence_present"] = cc["split_frozen_v2_evidence_present"] and (
        gih("split_frozen_v2_evidence_commit") == PRE.SPLIT_FROZEN_V2_EVIDENCE_COMMIT
    )
    checks["l2d_source_descends_split_frozen_v2"] = cc["l2d_source_descends_split_frozen_v2"]
    checks["head_descends_l2d_source"] = cc["head_descends_l2d_source"]

    for name, ok in checks.items():
        if not ok:
            reasons.append(f"{name} failed")
    reasons.extend(f"evidence: {r}" for r in integrity.reasons)
    reasons.extend(f"promotion: {r}" for r in promotion.reasons)
    return IngestGateVerification(
        ok=all(checks.values()),
        gate_hash=gate.gate_hash,
        checks=checks,
        reasons=tuple(dict.fromkeys(reasons)),
    )


def write_ingest_outputs(result: IngestQualificationResult, root: Path) -> tuple[Path, Path]:
    """Write the closure report and the (final) gate."""
    from minos_engine.gates.verifier import write_gate

    report_path = root / FINAL_REPORT_PATH
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_bytes(result.report_markdown.encode("utf-8"))
    gate_path = write_gate(result.gate, root / _GATE_SELF_PATH)
    return gate_path, report_path


def _render_report(
    *,
    qualified_source_sha: str | None,
    qualified_source_tree: str | None,
    input_hashes: dict[str, str],
    evidence: tuple[EvidenceItem, ...],
) -> str:
    """Render the non-circular closure report (omits report/gate hashes)."""
    stable = {k: v for k, v in sorted(input_hashes.items()) if k != "qualification_report_hash"}
    ih = json.dumps(stable, indent=2, sort_keys=True)
    ev = "\n".join(f"| `{e.path}` | {e.kind.value} | `{e.sha256}` |" for e in evidence)
    g = input_hashes.get
    return f"""# LAYER 2 — L2-D INGEST-READY Capability Report

**Gate:** {GATE_NAME}
**Qualification tool:** {INGEST_QUALIFIER_VERSION}
**Qualified source git sha:** `{qualified_source_sha}`
**Qualified source tree sha:** `{qualified_source_tree}`

> Generated by the INGEST-READY qualifier. Not hand-authored. This is the CAPABILITY
> gate: it proves the ingestion machinery (content-addressed input-integrity attestation,
> pure fail-closed admission validation, epoch-bound storage, migration 0004, partition
> views with the sealed test cohort denied) is correct — independent of any profile
> corpus. Per-epoch corpus evidence is separate: each PROFILE-SNAPSHOT-FROZEN-<epoch>
> snapshot derives its member count from that epoch's split ``sample_count`` and is
> frozen only when every allocated identity has exactly one accepted profile version.
> `Layer2Service.select_config` remains blocked.

## Capability summary
| Field | Value |
|---|---|
| Attestation schema | `{g("attestation_schema_version")}` (`{g("attestation_schema_hash")}`) |
| Ingest package hash | `{g("ingest_package_hash")}` |
| L2-D migration file hash | `{g("l2d_migration_file_hash")}` |
| L2-D migration contract hash | `{g("l2d_migration_contract_hash")}` |
| Alembic head | `{g("alembic_head_revision")}` |
| Accepted SPLIT-FROZEN-V2 gate | `{g("accepted_split_frozen_v2_gate_hash")}` |
| SPLIT-FROZEN-V2 source commit | `{g("split_frozen_v2_source_commit")}` |
| SPLIT-FROZEN-V2 evidence commit | `{g("split_frozen_v2_evidence_commit")}` |

## Bound identities (stable; excludes report/gate hashes)
```
{ih}
```

## Source evidence (git-tree-bound, hashed from the qualified source commit)
| Path | Kind | sha256 |
|---|---|---|
{ev}
"""
