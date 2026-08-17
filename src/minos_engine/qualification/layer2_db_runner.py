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
from minos_engine.layer2 import prerequisites as PRE
from minos_engine.layer2.entry_gate import EntryGateRequest, verify_l2_entry_gate
from minos_engine.storage.fingerprint import storage_schema_hash
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
    MIGRATION_FILE,
    "migrations/env.py",
    "alembic.ini",
    "docs/layer2/STORAGE_ARCHITECTURE.md",
    "docs/layer2/DATABASE_ROLES.md",
    "docs/layer2/MIGRATIONS.md",
)

# gate check -> the DB-suite nodeids (file.py or file.py::test) whose pass proves it.
_CHECK_NODES: dict[str, tuple[str, ...]] = {
    "postgres_16_verified": ("test_schema_inventory.py::test_postgres_major_version_is_16",),
    "seven_schemas_created": ("test_schema_inventory.py::test_exactly_seven_application_schemas",),
    "five_roles_created": ("test_roles_isolation.py::test_all_five_roles_exist",),
    "migration_upgrade_passed": ("test_migration_lifecycle.py::test_full_migration_lifecycle",),
    "migration_downgrade_passed": ("test_migration_lifecycle.py::test_full_migration_lifecycle",),
    "migration_reupgrade_passed": ("test_migration_lifecycle.py::test_full_migration_lifecycle",),
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

    mandatory: dict[str, bool] = {
        "l2a_entry_passed": entry_ok,
        **{check: _check_from_suite(passmap, nodes) for check, nodes in _CHECK_NODES.items()},
        "service_still_blocked": _service_still_blocked(),
        "split_manifest_absent": _split_manifest_absent(root),
        "full_tests_passed": suite_passes(accounting),
        "coverage_passed": coverage.meets(STAGE0_COVERAGE_THRESHOLD),
        "ruff_passed": tools["ruff"],
        "format_passed": tools["format"],
        "mypy_passed": tools["mypy"],
        "accepted_prerequisites_unchanged": _accepted_prerequisites_unchanged(root),
    }

    status = GateStatus.PASS if all(mandatory.values()) else GateStatus.HOLD
    created_at = created_at or datetime.now(UTC).isoformat()

    input_hashes = {
        "alembic_head_revision": alembic_head(),
        "migration_file_hash": si_evidence_hash(si, MIGRATION_FILE),
        "storage_schema_hash": storage_schema_hash(),
        "role_policy_hash": role_policy_hash(),
        "postgres_major_version": "16" if mandatory["postgres_16_verified"] else "unverified",
        "accepted_protocol_ready_gate_hash": PRE.PROTOCOL_READY_GATE_HASH,
        "accepted_twin_ready_gate_hash": PRE.TWIN_READY_GATE_HASH,
        "accepted_l1_ready_gate_hash": PRE.L1_READY_GATE_HASH,
        "accepted_l2a_owner_commit": PRE.OWNER_ACCEPTANCE_COMMIT,
        "accepted_l1_artifact_commit": PRE.ARTIFACT_COMMIT,
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
        "alembic_head_bound": gate.input_hashes.get("alembic_head_revision") == alembic_head(),
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
    }
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

    report_path = root / "reports" / "LAYER2_L2B_DB_QUALIFICATION_REPORT.md"
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
    return f"""# LAYER 2 — L2-B DB-READY Qualification Report

**Gate:** {GATE_NAME} — **{gate.status.value}**
**Qualification tool:** {DB_QUALIFIER_VERSION}
**Qualified source git sha:** `{gate.qualified_source_git_sha}`
**Qualified source tree sha:** `{gate.qualified_source_tree_sha}`

> Generated by `minos-engine layer2 db qualify`. Not hand-authored. A PASS gate is
> not constructible with any failing mandatory check; the DB-READY required set is
> enforced. Evidence is hashed from the qualified commit's blobs. All schema/role/
> constraint/append-only/least-privilege/worker-claim checks come from the real
> PostgreSQL 16 integration suite. `Layer2Service.select_config` remains blocked and
> no dataset split manifest exists.

## Mandatory checks
| Check | Status | Kind |
|---|---|---|
{rows}

## Test execution
| Metric | Value |
|---|---|
| Collected | {accounting.collected} |
| Passed | {accounting.passed} |
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
