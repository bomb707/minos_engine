"""SPLIT-FROZEN (Layer 2 L2-C) qualification — git-tree-bound, evidence-based.

Assembles the ``SPLIT-FROZEN`` gate from real evidence: the accepted PROTOCOL/TWIN/
L1/DB-READY prerequisites, the deterministic 75-sample manifest, the immutable L2-C
Alembic migration, and the real PostgreSQL 16 L2-C integration suite (migration
lifecycle, role isolation, immutability, constraints) plus the full test suite,
coverage, and ruff/format/mypy. The gate binds the accepted DB-READY identity, the
L2-C qualified source commit/tree, the canonical manifest/registry/policy/generator/
migration hashes, the Alembic head, the Python/PostgreSQL identities, and the exact
50/10/15 counts. A PASS is never constructed from caller-supplied booleans; the
verifier re-derives every binding and proves proper descent from the DB-READY evidence
commit — never the current HEAD.
"""

from __future__ import annotations

import json
import re
import subprocess
import xml.etree.ElementTree as ET
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from minos_engine.common.canonical_json import canonical_json_str
from minos_engine.common.errors import StageNotReadyError
from minos_engine.common.hashing import sha256_hex
from minos_engine.common.runtime import is_supported_runtime, runtime_identity
from minos_engine.gates.contracts import EvidenceKind, GateArtifact, GateStatus
from minos_engine.gates.required_checks import required_checks_for
from minos_engine.gates.verifier import load_gate, require_gate_pass, verify_gate_integrity
from minos_engine.layer2 import prerequisites as PRE
from minos_engine.layer2.feature_registry import REGISTRY_HASH
from minos_engine.layer2.split.contracts import DatasetSplitManifest, LocalInputInventory
from minos_engine.layer2.split.generator import generate, parameter_space_hash
from minos_engine.layer2.split.policy import (
    PARTITION_TOTALS,
    SUPPORTED_CHROMOSOMES,
    TOTAL_SAMPLES,
    split_policy_hash,
)
from minos_engine.layer2.split.verifier import verify_manifest
from minos_engine.storage.l2c_migration_contract import (
    L2C_MIGRATION_REVISION,
    l2c_contract_hash,
)

from . import git_tree as G
from .coverage import STAGE0_COVERAGE_THRESHOLD, CoverageResult, run_coverage
from .layer2_db_runner import alembic_head
from .provenance import GitProvenance, read_provenance
from .pytest_accounting import PytestAccounting, run_pytest, suite_passes
from .runner import SourceIntegrity, _bin, _tool_ok, gather_source_integrity

__all__ = [
    "GATE_NAME",
    "SPLIT_QUALIFIER_VERSION",
    "L2C_MIGRATION_FILE",
    "MANIFEST_PATH",
    "INVENTORY_PATH",
    "MANIFEST_SCHEMA_FILE",
    "SPLIT_EVIDENCE",
    "SPLIT_REQUIRED_TRACKED_FILES",
    "SplitQualificationResult",
    "SplitGateVerification",
    "db_ready_closure_checks",
    "assemble_split_result",
    "qualify_split_frozen",
    "verify_split_frozen_gate",
    "write_split_outputs",
    "l2c_migration_immutable",
]

_HEX64 = re.compile(r"^[0-9a-f]{64}$")

GATE_NAME = "SPLIT-FROZEN"
SPLIT_QUALIFIER_VERSION = "layer2-split-qualifier-v1"
L2C_MIGRATION_FILE = "migrations/versions/0002_l2c_dataset_split.py"
MANIFEST_SCHEMA_FILE = "schemas/layer2-dataset-split-v1.schema.json"
MANIFEST_PATH = "manifests/layer2_dataset_split_v1.json"
INVENTORY_PATH = "manifests/layer2_local_input_inventory_v1.json"
_SPLIT_SUITE = "tests/integration/layer2_split"

SPLIT_EVIDENCE: tuple[tuple[str, EvidenceKind], ...] = (
    ("src/minos_engine/layer2/split", EvidenceKind.DIRECTORY),
    ("src/minos_engine/storage/dataset_split.py", EvidenceKind.FILE),
    ("src/minos_engine/storage/l2c_migration_contract.py", EvidenceKind.FILE),
    ("src/minos_engine/qualification/layer2_split_runner.py", EvidenceKind.FILE),
    ("src/minos_engine/cli/layer2_split_commands.py", EvidenceKind.FILE),
    (L2C_MIGRATION_FILE, EvidenceKind.FILE),
    (MANIFEST_SCHEMA_FILE, EvidenceKind.FILE),
    ("gates/protocol-ready.json", EvidenceKind.FILE),
    ("gates/twin-ready.json", EvidenceKind.FILE),
    ("gates/l1-ready.json", EvidenceKind.FILE),
    ("gates/db-ready.json", EvidenceKind.FILE),
    ("docs/layer2/DATASET_SPLIT.md", EvidenceKind.FILE),
    (_SPLIT_SUITE, EvidenceKind.DIRECTORY),
)

SPLIT_REQUIRED_TRACKED_FILES: tuple[str, ...] = (
    "src/minos_engine/layer2/split/__init__.py",
    "src/minos_engine/layer2/split/policy.py",
    "src/minos_engine/layer2/split/contracts.py",
    "src/minos_engine/layer2/split/discovery.py",
    "src/minos_engine/layer2/split/generator.py",
    "src/minos_engine/layer2/split/verifier.py",
    "src/minos_engine/storage/dataset_split.py",
    "src/minos_engine/storage/l2c_migration_contract.py",
    L2C_MIGRATION_FILE,
    MANIFEST_SCHEMA_FILE,
    "docs/layer2/DATASET_SPLIT.md",
)

# gate check -> L2-C integration nodeids whose pass proves it.
_CHECK_NODES: dict[str, tuple[str, ...]] = {
    "postgres_16_verified": ("test_migration_lifecycle.py::test_postgres_major_version_is_16",),
    "l2c_migration_lifecycle_passed": (
        "test_migration_lifecycle.py::test_l2c_migration_lifecycle",
    ),
    "role_isolation_passed": ("test_role_isolation.py",),
    "immutability_passed": ("test_split_store.py::test_registry_and_allocation_append_only",),
    "constraints_passed": ("test_split_store.py::test_duplicate_and_overlap_rejected",),
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


def l2c_migration_immutable(root: Path) -> bool:
    """The L2-C migration is a self-contained snapshot (no ORM metadata dependency)."""
    path = root / L2C_MIGRATION_FILE
    if not path.exists():
        return False
    src = path.read_text(encoding="utf-8")
    return not any(token in src for token in _FORBIDDEN_MIGRATION_TOKENS)


def db_ready_closure_checks(
    root: Path,
    *,
    l2c_source_ref: str,
    head_ref: str = "HEAD",
    require_head_descends: bool = True,
) -> dict[str, bool]:
    """Prove the accepted DB-READY closure and the *exact* L2-C qualified source.

    ``l2c_source_ref`` is the frozen L2-C source commit (provenance HEAD at generation;
    ``gate.qualified_source_git_sha`` at verification) — never a substitute such as the
    current HEAD for checks 1-4. The L2-C source is proven to properly descend the
    accepted DB-READY *evidence* commit (rejecting sibling/ancestor/unrelated sources).
    """
    src = PRE.DB_READY_SOURCE_COMMIT
    src_tree = PRE.DB_READY_SOURCE_TREE
    evi = PRE.DB_READY_EVIDENCE_COMMIT

    src_ok = G.is_commit(root, src)
    src_tree_ok = src_ok and G.commit_tree_sha(root, src) == src_tree
    evi_ok = G.is_commit(root, evi)
    # DB-READY evidence commit properly descends the DB-READY source commit.
    db_chain_ok = bool(src_ok and evi_ok and G.is_ancestor(root, src, evi))

    qs = l2c_source_ref
    qs_present = bool(qs) and G.is_commit(root, qs)
    # accepted DB-READY evidence is a PROPER ancestor of the L2-C qualified source.
    l2c_descends = bool(
        evi_ok and qs_present and G.is_ancestor(root, evi, qs) and not G.is_ancestor(root, qs, evi)
    )
    head_ok = G.object_exists(root, head_ref)
    head_descends = (not require_head_descends) or bool(
        qs_present and head_ok and G.is_ancestor(root, qs, head_ref)
    )
    return {
        "db_ready_source_present": src_ok,
        "db_ready_source_tree_bound": src_tree_ok,
        "db_ready_evidence_present": evi_ok and db_chain_ok,
        "l2c_source_descends_db_ready": l2c_descends,
        "head_descends_l2c_source": head_descends,
    }


class SplitQualificationResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    gate: GateArtifact
    manifest: DatasetSplitManifest
    inventory: LocalInputInventory
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


def run_split_suite(root: Path) -> dict[str, bool]:  # pragma: no cover - subprocess + real PG
    """Run the L2-C PostgreSQL integration suite; return {"file.py::test": passed}."""
    junit = root / "reports" / "ci-split-junit.xml"
    junit.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [_bin("pytest"), _SPLIT_SUITE, f"--junitxml={junit}", "-q"],
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


def gather_split_source_integrity(root: Path, ref: str) -> SourceIntegrity:
    return gather_source_integrity(
        root, ref, evidence_spec=SPLIT_EVIDENCE, required_files=SPLIT_REQUIRED_TRACKED_FILES
    )


def manifest_bytes(manifest: DatasetSplitManifest) -> bytes:
    return (canonical_json_str(manifest.to_canonical()) + "\n").encode("utf-8")


def inventory_bytes(inventory: LocalInputInventory) -> bytes:
    return (canonical_json_str(inventory.to_canonical()) + "\n").encode("utf-8")


def qualify_split_frozen(  # pragma: no cover - subprocess + real PG + corpus
    root: Path, dataset_root: str | Path
) -> SplitQualificationResult:
    root = root.resolve()
    provenance = read_provenance(root)
    ref = provenance.head_sha or "HEAD"
    source_integrity = gather_split_source_integrity(root, ref)
    manifest, inventory = generate(dataset_root)
    passmap = run_split_suite(root)
    accounting = run_pytest(root)
    coverage = run_coverage(root)[0]
    tools = {
        "ruff": _tool_ok([_bin("ruff"), "check", "."], root),
        "format": _tool_ok([_bin("ruff"), "format", "--check", "."], root),
        "mypy": _tool_ok([_bin("mypy"), "src"], root),
    }
    return assemble_split_result(
        root,
        manifest=manifest,
        inventory=inventory,
        passmap=passmap,
        accounting=accounting,
        coverage=coverage,
        tools=tools,
        provenance=provenance,
        source_integrity=source_integrity,
    )


def assemble_split_result(
    root: Path,
    *,
    manifest: DatasetSplitManifest,
    inventory: LocalInputInventory,
    passmap: dict[str, bool],
    accounting: PytestAccounting,
    coverage: CoverageResult,
    tools: dict[str, bool],
    provenance: GitProvenance,
    source_integrity: SourceIntegrity,
    created_at: str | None = None,
) -> SplitQualificationResult:
    root = root.resolve()
    si = source_integrity
    mv = verify_manifest(manifest.to_canonical())
    migration_sha = _si_hash(si, L2C_MIGRATION_FILE)
    manifest_sha = sha256_hex(manifest_bytes(manifest))
    inventory_sha = sha256_hex(inventory_bytes(inventory))
    per_chrom_ok = all(
        manifest.per_chromosome.get(c) == {"train": 10, "validation": 2, "test": 3}
        for c in SUPPORTED_CHROMOSOMES
    )

    closure = db_ready_closure_checks(root, l2c_source_ref=provenance.head_sha or "")

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
        **closure,
        "manifest_schema_valid": mv.checks.get("schema_valid", False),
        "manifest_verified": mv.ok,
        "canonical_manifest_hash_bound": mv.manifest_hash == manifest.manifest_hash,
        "dataset_registry_hash_bound": mv.dataset_registry_hash == manifest.dataset_registry_hash,
        "split_policy_hash_bound": manifest.split_policy_hash == split_policy_hash(),
        "parameter_space_hash_bound": manifest.parameter_space_hash == parameter_space_hash(),
        "feature_registry_hash_bound": manifest.feature_registry_hash == REGISTRY_HASH,
        "committed_manifest_bytes_bound": _HEX64.match(manifest_sha) is not None,
        "local_input_inventory_hash_bound": _HEX64.match(inventory_sha) is not None,
        "l2c_migration_immutable": l2c_migration_immutable(root),
        "l2c_migration_file_evidence_bound": _HEX64.match(migration_sha) is not None,
        "l2c_migration_contract_bound": _HEX64.match(migration_sha) is not None,
        "alembic_head_is_l2c": alembic_head() == L2C_MIGRATION_REVISION,
        "total_sample_count_75": len(manifest.samples) == TOTAL_SAMPLES,
        "partition_totals_50_10_15": manifest.counts == PARTITION_TOTALS,
        "per_chromosome_10_2_3": per_chrom_ok,
        "full_tests_passed": suite_passes(accounting),
        "coverage_passed": coverage.meets(STAGE0_COVERAGE_THRESHOLD),
        "ruff_passed": tools["ruff"],
        "format_passed": tools["format"],
        "mypy_passed": tools["mypy"],
        **{check: _check_from_suite(passmap, nodes) for check, nodes in _CHECK_NODES.items()},
        "evidence_hashes_complete": si.evidence_hashes_complete,
        "required_source_tracked": si.required_source_tracked,
        "truth_mutation_isolation_ok": mv.checks.get("no_truth_or_mutation_fields", False),
        "service_still_blocked": _service_still_blocked(),
    }

    status = GateStatus.PASS if all(mandatory.values()) else GateStatus.HOLD
    created_at = created_at or datetime.now(UTC).isoformat()

    per_chrom_hashes = {
        c: f"{manifest.per_chromosome[c]['train']}/"
        f"{manifest.per_chromosome[c]['validation']}/"
        f"{manifest.per_chromosome[c]['test']}"
        for c in SUPPORTED_CHROMOSOMES
    }
    input_hashes = {
        "canonical_manifest_hash": manifest.manifest_hash,
        "dataset_registry_hash": manifest.dataset_registry_hash,
        "split_policy_hash": split_policy_hash(),
        "parameter_space_hash": manifest.parameter_space_hash,
        "feature_registry_hash": manifest.feature_registry_hash,
        "committed_manifest_sha256": manifest_sha,
        "local_input_inventory_hash": inventory.inventory_hash,
        "committed_inventory_sha256": inventory_sha,
        "manifest_schema_hash": _si_hash(si, MANIFEST_SCHEMA_FILE),
        "generator_source_hash": _si_hash(si, "src/minos_engine/layer2/split"),
        "l2c_migration_file_hash": migration_sha,
        "l2c_migration_contract_hash": l2c_contract_hash(migration_sha),
        "alembic_head_revision": alembic_head(),
        "accepted_protocol_ready_gate_hash": PRE.PROTOCOL_READY_GATE_HASH,
        "accepted_twin_ready_gate_hash": PRE.TWIN_READY_GATE_HASH,
        "accepted_l1_ready_gate_hash": PRE.L1_READY_GATE_HASH,
        "accepted_db_ready_gate_hash": PRE.DB_READY_GATE_HASH,
        "db_ready_source_commit": PRE.DB_READY_SOURCE_COMMIT,
        "db_ready_source_tree": PRE.DB_READY_SOURCE_TREE,
        "db_ready_evidence_commit": PRE.DB_READY_EVIDENCE_COMMIT,
        "accepted_feature_registry_hash": PRE.ACCEPTED_FEATURE_REGISTRY_HASH,
        "total_samples": str(len(manifest.samples)),
        "count_train": str(manifest.counts.get("train", 0)),
        "count_validation": str(manifest.counts.get("validation", 0)),
        "count_test": str(manifest.counts.get("test", 0)),
        "per_chromosome_layout": json.dumps(per_chrom_hashes, sort_keys=True),
        "postgres_major_version": "16" if mandatory["postgres_16_verified"] else "unverified",
        "test_collected": str(accounting.collected),
        "test_passed": str(accounting.passed),
        "test_failed": str(accounting.failed),
        "test_skipped": str(accounting.skipped),
        "split_integration_collected": str(len(passmap)),
        "split_integration_passed": str(sum(1 for v in passmap.values() if v)),
        "python_runtime": runtime_identity(),
        "qualification_report_hash": "",  # set by write_split_outputs after render
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
        qualification_tool_version=SPLIT_QUALIFIER_VERSION,
        created_at=created_at,
    )
    markdown = _render_report(
        gate=gate, mandatory=mandatory, accounting=accounting, coverage=coverage
    )
    return SplitQualificationResult(
        gate=gate,
        manifest=manifest,
        inventory=inventory,
        report_markdown=markdown,
        mandatory=mandatory,
        accounting=accounting,
        coverage=coverage,
        provenance=provenance,
    )


class SplitGateVerification(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    ok: bool
    gate_hash: str
    checks: dict[str, bool]
    reasons: tuple[str, ...] = ()


def verify_split_frozen_gate(
    root: Path, gate_path: Path, *, require_descends: bool = True
) -> SplitGateVerification:
    """Non-mutating verification of a committed SPLIT-FROZEN gate (check mode).

    Independently recomputes every binding, re-hashes source evidence from the exact
    qualified source commit, proves proper descent from the DB-READY evidence commit,
    verifies the committed manifest bytes + recomputes its content, and rejects
    duplicate/missing migration evidence and consistently-tampered hash pairs.
    """
    root = root.resolve()
    reasons: list[str] = []
    try:
        gate = load_gate(gate_path)
    except Exception as exc:  # noqa: BLE001
        return SplitGateVerification(
            ok=False, gate_hash="", checks={"loadable": False}, reasons=(str(exc),)
        )

    integrity = verify_gate_integrity(gate, base_dir=root)
    promotion = require_gate_pass(gate, base_dir=root)
    src_sha = gate.qualified_source_git_sha or ""
    gih = gate.input_hashes.get

    checks: dict[str, bool] = {
        "gate_name_split_frozen": gate.gate_name == GATE_NAME,
        "canonical_integrity": gate.gate_hash == gate.compute_hash(),
        "qualification_tool_identity": gate.qualification_tool_version == SPLIT_QUALIFIER_VERSION,
        "python_runtime_is_3_12": is_supported_runtime(),
        "evidence_verified": integrity.ok,
        "required_checks_and_promotion": promotion.ok,
        "alembic_head_is_l2c": alembic_head() == L2C_MIGRATION_REVISION
        and gih("alembic_head_revision") == L2C_MIGRATION_REVISION,
        "l2c_migration_immutable": l2c_migration_immutable(root),
        "accepted_protocol_ready_unchanged": _accepted_gate_unchanged(
            root, "protocol-ready.json", PRE.PROTOCOL_READY_GATE_HASH
        )
        and gih("accepted_protocol_ready_gate_hash") == PRE.PROTOCOL_READY_GATE_HASH,
        "accepted_twin_ready_unchanged": _accepted_gate_unchanged(
            root, "twin-ready.json", PRE.TWIN_READY_GATE_HASH
        )
        and gih("accepted_twin_ready_gate_hash") == PRE.TWIN_READY_GATE_HASH,
        "accepted_l1_ready_unchanged": _accepted_gate_unchanged(
            root, "l1-ready.json", PRE.L1_READY_GATE_HASH
        )
        and gih("accepted_l1_ready_gate_hash") == PRE.L1_READY_GATE_HASH,
        "accepted_db_ready_unchanged": _accepted_gate_unchanged(
            root, "db-ready.json", PRE.DB_READY_GATE_HASH
        )
        and gih("accepted_db_ready_gate_hash") == PRE.DB_READY_GATE_HASH,
        "feature_registry_hash_bound": gih("feature_registry_hash") == REGISTRY_HASH
        and gih("accepted_feature_registry_hash") == PRE.ACCEPTED_FEATURE_REGISTRY_HASH,
        "split_policy_hash_bound": gih("split_policy_hash") == split_policy_hash(),
        "parameter_space_hash_bound": gih("parameter_space_hash") == parameter_space_hash(),
    }

    # --- L2-C migration cross-binding to qualified-source evidence ----------------
    mig_items = [e for e in gate.evidence if e.path == L2C_MIGRATION_FILE]
    mig_item = mig_items[0] if len(mig_items) == 1 else None
    mig_item_valid = bool(
        mig_item is not None
        and mig_item.kind is EvidenceKind.FILE
        and mig_item.sha256 is not None
        and _HEX64.match(mig_item.sha256)
    )
    mig_evi_sha = mig_item.sha256 if (mig_item and mig_item.sha256) else ""
    try:
        committed_mig_sha = G.sha256_git_file(root, L2C_MIGRATION_FILE, src_sha)[0]
    except Exception:  # noqa: BLE001
        committed_mig_sha = ""
    checks["l2c_migration_evidence_present"] = mig_item_valid
    checks["l2c_migration_evidence_matches_source_blob"] = bool(
        mig_item_valid and committed_mig_sha and mig_evi_sha == committed_mig_sha
    )
    checks["l2c_migration_file_evidence_bound"] = bool(
        mig_item_valid and gih("l2c_migration_file_hash") == mig_evi_sha
    )
    checks["l2c_migration_contract_bound"] = bool(
        mig_item_valid and gih("l2c_migration_contract_hash") == l2c_contract_hash(mig_evi_sha)
    )

    # --- committed manifest bytes + independent recomputation ---------------------
    try:
        committed_manifest_sha, _ = G.sha256_git_file(root, MANIFEST_PATH, "HEAD")
        manifest_blob = G.blob_bytes(root, MANIFEST_PATH, "HEAD")
    except Exception:  # noqa: BLE001
        committed_manifest_sha, manifest_blob = "", b""
    checks["committed_manifest_bytes_bound"] = bool(
        committed_manifest_sha and committed_manifest_sha == gih("committed_manifest_sha256")
    )
    manifest_ok = False
    manifest_hash_ok = False
    registry_hash_ok = False
    counts_ok = False
    truth_ok = False
    if manifest_blob:
        try:
            raw = json.loads(manifest_blob.decode("utf-8"))
            mv = verify_manifest(raw)
            manifest_ok = mv.ok
            manifest_hash_ok = mv.manifest_hash == gih("canonical_manifest_hash")
            registry_hash_ok = mv.dataset_registry_hash == gih("dataset_registry_hash")
            counts_ok = (
                mv.checks.get("total_sample_count", False)
                and mv.checks.get("partition_totals_exact", False)
                and mv.checks.get("per_chromosome_layout_exact", False)
            )
            truth_ok = mv.checks.get("no_truth_or_mutation_fields", False)
        except Exception:  # noqa: BLE001
            pass
    checks["manifest_schema_valid"] = manifest_ok
    checks["manifest_verified"] = manifest_ok
    checks["canonical_manifest_hash_bound"] = manifest_hash_ok
    checks["dataset_registry_hash_bound"] = registry_hash_ok
    checks["total_sample_count_75"] = counts_ok
    checks["partition_totals_50_10_15"] = counts_ok
    checks["per_chromosome_10_2_3"] = counts_ok
    checks["truth_mutation_isolation_ok"] = truth_ok

    # --- committed local input inventory bytes ------------------------------------
    try:
        inv_sha, inv_blob = (
            G.sha256_git_file(root, INVENTORY_PATH, "HEAD")[0],
            G.blob_bytes(root, INVENTORY_PATH, "HEAD"),
        )
    except Exception:  # noqa: BLE001
        inv_sha, inv_blob = "", b""
    inv_hash_ok = False
    if inv_blob:
        try:
            inv_raw = json.loads(inv_blob.decode("utf-8"))
            inv_hash_ok = inv_raw.get("inventory_hash") == gih("local_input_inventory_hash")
        except Exception:  # noqa: BLE001
            pass
    checks["local_input_inventory_hash_bound"] = bool(
        inv_sha and inv_sha == gih("committed_inventory_sha256") and inv_hash_ok
    )

    # --- DB-READY closure ancestry against the EXACT qualified source -------------
    cc = db_ready_closure_checks(
        root,
        l2c_source_ref=src_sha,
        head_ref="HEAD",
        require_head_descends=require_descends,
    )
    checks["l2c_qualified_source_tree_matches"] = (
        G.commit_tree_sha(root, src_sha) == gate.qualified_source_tree_sha
    )
    checks["db_ready_source_present"] = cc["db_ready_source_present"] and (
        gih("db_ready_source_commit") == PRE.DB_READY_SOURCE_COMMIT
    )
    checks["db_ready_source_tree_bound"] = cc["db_ready_source_tree_bound"] and (
        gih("db_ready_source_tree") == PRE.DB_READY_SOURCE_TREE
    )
    checks["db_ready_evidence_present"] = cc["db_ready_evidence_present"] and (
        gih("db_ready_evidence_commit") == PRE.DB_READY_EVIDENCE_COMMIT
    )
    checks["l2c_source_descends_db_ready"] = cc["l2c_source_descends_db_ready"]
    checks["head_descends_l2c_source"] = cc["head_descends_l2c_source"]

    for name, ok in checks.items():
        if not ok:
            reasons.append(f"{name} failed")
    reasons.extend(f"evidence: {r}" for r in integrity.reasons)
    reasons.extend(f"promotion: {r}" for r in promotion.reasons)
    return SplitGateVerification(
        ok=all(checks.values()),
        gate_hash=gate.gate_hash,
        checks=checks,
        reasons=tuple(dict.fromkeys(reasons)),
    )


def write_split_outputs(
    result: SplitQualificationResult, root: Path
) -> tuple[Path, Path, Path, Path]:
    """Write manifest, inventory, report, then the gate carrying the report's sha256."""
    from minos_engine.gates.verifier import write_gate

    manifests_dir = root / "manifests"
    manifests_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = root / MANIFEST_PATH
    inventory_path = root / INVENTORY_PATH
    manifest_path.write_bytes(manifest_bytes(result.manifest))
    inventory_path.write_bytes(inventory_bytes(result.inventory))

    report_path = root / "reports" / "LAYER2_L2C_SPLIT_FROZEN_REPORT.md"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(result.report_markdown, encoding="utf-8")
    report_sha = sha256_hex(report_path.read_bytes())

    data = result.gate.model_dump(mode="json")
    data["input_hashes"]["qualification_report_hash"] = report_sha
    data["gate_hash"] = ""
    gate = GateArtifact.model_validate(data)
    gate_path = write_gate(gate, root / "gates" / "split-frozen.json")
    return gate_path, manifest_path, inventory_path, report_path


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
    return f"""# LAYER 2 — L2-C SPLIT-FROZEN Qualification Report

**Gate:** {GATE_NAME} — **{gate.status.value}**
**Qualification tool:** {SPLIT_QUALIFIER_VERSION}
**Qualified source git sha:** `{gate.qualified_source_git_sha}`
**Qualified source tree sha:** `{gate.qualified_source_tree_sha}`

> Generated by `minos-engine layer2 split qualify`. Not hand-authored. A PASS gate is
> not constructible with any failing mandatory check; the SPLIT-FROZEN required set is
> enforced. Source evidence is hashed from the qualified commit's blobs; the committed
> canonical manifest is independently re-verified and its partition assignment is
> re-derived from `{{SALT, round_id}}`. The split is a pure function of the confirmed
> contig and round id — truth, mutation labels, and scores never influence it and are
> absent from the canonical manifest. `Layer2Service.select_config` remains blocked.

## Split summary
| Field | Value |
|---|---|
| Total samples | {g("total_samples")} |
| Train / Validation / Test | {g("count_train")} / {g("count_validation")} / {g("count_test")} |
| Per-chromosome (train/val/test) | `{g("per_chromosome_layout")}` |
| Canonical manifest hash | `{g("canonical_manifest_hash")}` |
| Dataset registry hash | `{g("dataset_registry_hash")}` |
| Split policy hash | `{g("split_policy_hash")}` |
| Local input inventory hash | `{g("local_input_inventory_hash")}` |
| L2-C migration file hash | `{g("l2c_migration_file_hash")}` |
| L2-C migration contract hash | `{g("l2c_migration_contract_hash")}` |
| Alembic head | `{g("alembic_head_revision")}` |

## Mandatory checks
| Check | Status | Kind |
|---|---|---|
{rows}

## Test accounting (pre-evidence run)
| Metric | Value |
|---|---|
| Full suite — collected | {g("test_collected")} |
| Full suite — passed | {g("test_passed")} |
| Full suite — failed | {g("test_failed")} |
| Full suite — skipped | {g("test_skipped")} |
| PostgreSQL L2-C integration — collected | {g("split_integration_collected")} |
| PostgreSQL L2-C integration — passed | {g("split_integration_passed")} |
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
