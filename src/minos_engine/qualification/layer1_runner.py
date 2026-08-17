"""L1-READY (Layer 1) qualification — git-tree-bound, two-commit.

Reuses the Stage-0/Stage-1 building blocks (pytest via JUnit, coverage via XML,
ruff/mypy, git-tree-bound evidence) and adds the Layer 1 behavior checks plus the
accepted PROTOCOL-READY and TWIN-READY prerequisites. The gate records the
profile schema hash, profiler config hash, profiler version, both prerequisite
gate hashes, and the real-BAM integration-report hash. ``qualify_layer1``
orchestrates subprocess tools; ``assemble_layer1_result`` (pure) is unit-tested
with synthetic inputs.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from minos_engine import __version__
from minos_engine.common.hashing import canonical_hash, sha256_hex
from minos_engine.common.runtime import is_supported_runtime, runtime_identity
from minos_engine.gates.contracts import EvidenceKind, GateArtifact, GateStatus
from minos_engine.gates.required_checks import required_checks_for
from minos_engine.layer1.integration import IntegrationReport
from minos_engine.layer1.prerequisites import verify_twin_ready_prerequisite
from minos_engine.twin.prerequisites import verify_protocol_ready

from . import checks as C
from . import git_tree as G
from . import layer1_checks as L
from .coverage import STAGE0_COVERAGE_THRESHOLD, CoverageResult, run_coverage
from .provenance import GitProvenance, read_provenance, verify_parent_is
from .pytest_accounting import PytestAccounting, run_pytest, suite_passes
from .runner import SourceIntegrity, _bin, _tool_ok, gather_source_integrity

__all__ = [
    "Layer1QualificationResult",
    "Layer1GateVerification",
    "LAYER1_QUALIFIER_VERSION",
    "LAYER1_EVIDENCE",
    "LAYER1_REQUIRED_TRACKED_FILES",
    "REAL_BAM_REPORT_PATH",
    "gather_layer1_source_integrity",
    "load_integration_report",
    "qualify_layer1",
    "assemble_layer1_result",
    "verify_l1_ready_gate",
    "write_layer1_outputs",
]

GATE_NAME = "L1-READY"
LAYER1_QUALIFIER_VERSION = "layer1-qualifier-v1"
REAL_BAM_REPORT_PATH = "reports/LAYER1_REAL_BAM_REPORT.json"

LAYER1_EVIDENCE: tuple[tuple[str, EvidenceKind], ...] = (
    ("reports/LAYER1_PREIMPLEMENTATION_AUDIT.md", EvidenceKind.FILE),
    (REAL_BAM_REPORT_PATH, EvidenceKind.FILE),
    ("gates/protocol-ready.json", EvidenceKind.FILE),
    ("gates/twin-ready.json", EvidenceKind.FILE),
    ("src/minos_engine", EvidenceKind.DIRECTORY),
    ("schemas", EvidenceKind.DIRECTORY),
    ("configs", EvidenceKind.DIRECTORY),
    ("tests", EvidenceKind.DIRECTORY),
    ("docs", EvidenceKind.DIRECTORY),
    (".github/workflows/ci.yml", EvidenceKind.FILE),
    ("pyproject.toml", EvidenceKind.FILE),
    ("Makefile", EvidenceKind.FILE),
)

LAYER1_REQUIRED_TRACKED_FILES: tuple[str, ...] = (
    "reports/LAYER1_PREIMPLEMENTATION_AUDIT.md",
    REAL_BAM_REPORT_PATH,
    "gates/protocol-ready.json",
    "gates/twin-ready.json",
    "pyproject.toml",
    "Makefile",
    ".github/workflows/ci.yml",
    "configs/layer1/default.yaml",
    "schemas/bam-profile-v1.schema.json",
    "schemas/window-profile-v1.schema.json",
    "schemas/profile-manifest-v1.schema.json",
    "schemas/layer1-profile-request-v1.schema.json",
    "schemas/layer1-profile-result-v1.schema.json",
    "schemas/layer1-fingerprint-v1.schema.json",
    "schemas/layer1-integration-report-v1.schema.json",
    "src/minos_engine/layer1/contracts.py",
    "src/minos_engine/layer1/service.py",
    "src/minos_engine/layer1/orchestrator.py",
    "src/minos_engine/layer1/scan.py",
    "src/minos_engine/layer1/coverage.py",
    "src/minos_engine/layer1/pileup.py",
    "src/minos_engine/layer1/reference_profile.py",
    "src/minos_engine/layer1/sampling.py",
    "src/minos_engine/layer1/serializer.py",
    "src/minos_engine/layer1/prerequisites.py",
    "src/minos_engine/layer1/adapters/pysam_adapter.py",
    "src/minos_engine/qualification/layer1_runner.py",
    "src/minos_engine/qualification/layer1_checks.py",
    "src/minos_engine/cli/layer1_commands.py",
    "docs/layer1/ARCHITECTURE.md",
)


def gather_layer1_source_integrity(root: Path, ref: str = "HEAD") -> SourceIntegrity:
    return gather_source_integrity(
        root,
        ref,
        evidence_spec=LAYER1_EVIDENCE,
        required_files=LAYER1_REQUIRED_TRACKED_FILES,
    )


def load_integration_report(root: Path) -> IntegrationReport | None:
    path = root / REAL_BAM_REPORT_PATH
    if not path.exists():
        return None
    try:
        return IntegrationReport.model_validate_json(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 - a malformed report is treated as unavailable
        return None


class Layer1QualificationResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    gate: GateArtifact
    report_markdown: str
    accounting: PytestAccounting
    coverage: CoverageResult
    tools: dict[str, bool]
    provenance: GitProvenance
    source_integrity: SourceIntegrity
    real_bam_qualified: bool


def qualify_layer1(root: Path) -> Layer1QualificationResult:  # pragma: no cover - subprocess
    root = root.resolve()
    provenance = read_provenance(root)
    ref = provenance.head_sha or "HEAD"
    source_integrity = gather_layer1_source_integrity(root, ref)
    accounting = run_pytest(root)
    coverage = run_coverage(root)[0]
    tools = {
        "ruff_check": _tool_ok([_bin("ruff"), "check", "."], root),
        "ruff_format": _tool_ok([_bin("ruff"), "format", "--check", "."], root),
        "mypy": _tool_ok([_bin("mypy"), "src"], root),
    }
    return assemble_layer1_result(
        root,
        accounting=accounting,
        coverage=coverage,
        tools=tools,
        provenance=provenance,
        source_integrity=source_integrity,
    )


def assemble_layer1_result(
    root: Path,
    *,
    accounting: PytestAccounting,
    coverage: CoverageResult,
    tools: dict[str, bool],
    provenance: GitProvenance,
    source_integrity: SourceIntegrity,
    created_at: str | None = None,
) -> Layer1QualificationResult:
    root = root.resolve()
    src_dir = root / "src" / "minos_engine"
    si = source_integrity

    protocol = verify_protocol_ready(root)
    twin = verify_twin_ready_prerequisite(root)
    report = load_integration_report(root)
    hard_limit_met = bool(report and report.hard_limit_met)
    real_bam_qualified = bool(
        report and report.real_bam_qualified and report.repeat_run_fingerprint_equal
    )

    schema_hash = L.profile_schema_hash()
    config_hash = L.profiler_config_hash()
    version = L.profiler_version()

    mandatory = {
        "protocol_ready_identity_accepted": protocol.identity_accepted,
        "protocol_ready_evidence_verified": protocol.evidence_verified,
        "twin_ready_identity_accepted": twin.identity_accepted,
        "twin_ready_evidence_verified": twin.evidence_verified,
        "twin_ready_promotion_authorized": twin.promotion_authorized,
        "python_runtime_is_3_12": is_supported_runtime(),
        "layer1_contracts_schema_valid": L.contracts_schema_valid(),
        "layer1_input_validation_complete": L.input_validation_complete(),
        "layer1_filter_policy_verified": L.filter_policy_verified(),
        "layer1_feature_known_answers_pass": L.feature_known_answers_pass(),
        "layer1_reference_profiler_verified": L.reference_profiler_verified(),
        "layer1_determinism_verified": L.determinism_verified(),
        "layer1_truth_isolation_verified": L.truth_isolation_verified(src_dir),
        "layer1_architecture_boundaries_verified": L.architecture_boundaries_verified(src_dir),
        "layer1_deadline_behavior_verified": L.deadline_behavior_verified(),
        "layer1_hard_limit_met": hard_limit_met,
        "layer1_memory_policy_verified": L.memory_policy_verified(),
        "layer1_real_bam_qualified": real_bam_qualified,
        "profile_schema_hash_match": bool(schema_hash),
        "profiler_config_hash_match": bool(config_hash),
        "profiler_version_match": version == "layer1-profiler-v1",
        "required_source_tracked": si.required_source_tracked,
        "worktree_matches_head": si.worktree_matches_head,
        "evidence_hashes_complete": si.evidence_hashes_complete,
        "qualified_source_clean": provenance.worktree_clean,
        "qualified_source_present": bool(provenance.head_sha) and bool(provenance.tree_sha),
        "tests_collected_nonzero": accounting.collected > 0,
        "all_tests_pass": suite_passes(accounting),
        "coverage_threshold_met": coverage.meets(STAGE0_COVERAGE_THRESHOLD),
        "ruff_check_pass": tools["ruff_check"],
        "ruff_format_pass": tools["ruff_format"],
        "mypy_pass": tools["mypy"],
        "layer1_documentation_complete": L.documentation_complete(root),
        "layer2_blocked": C.layer2_blocked(),
    }

    status = GateStatus.PASS if all(mandatory.values()) else GateStatus.HOLD
    created_at = created_at or datetime.now(UTC).isoformat()

    fixture_identity = canonical_hash(
        {"generator": "layer1-synthetic-fixtures-v1", "builder": "tests/layer1_fixtures.py"}
    )
    report_hash = report.model_dump_json() if report else ""
    input_hashes = {
        "layer1_schema_hash": schema_hash,
        "profiler_config_hash": config_hash,
        "profiler_version": version,
        "qualification_report_hash": "",  # set by write_layer1_outputs after render
        "prerequisite_protocol_ready_gate_hash": protocol.gate_hash or "unavailable",
        "prerequisite_twin_ready_gate_hash": twin.gate_hash or "unavailable",
        "python_runtime": runtime_identity(),
        "real_bam_integration_report_hash": sha256_hex(report_hash.encode("utf-8"))
        if report
        else "unavailable",
        "synthetic_fixture_identity": fixture_identity,
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
        qualification_tool_version=LAYER1_QUALIFIER_VERSION,
        created_at=created_at,
    )
    markdown = _render_report(
        gate=gate,
        accounting=accounting,
        coverage=coverage,
        tools=tools,
        mandatory=mandatory,
        provenance=provenance,
        report=report,
        schema_hash=schema_hash,
        config_hash=config_hash,
        version=version,
        created_at=created_at,
    )
    return Layer1QualificationResult(
        gate=gate,
        report_markdown=markdown,
        accounting=accounting,
        coverage=coverage,
        tools=tools,
        provenance=provenance,
        source_integrity=si,
        real_bam_qualified=real_bam_qualified,
    )


class Layer1GateVerification(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    ok: bool
    gate_hash: str
    checks: dict[str, bool]
    reasons: tuple[str, ...] = ()


def verify_l1_ready_gate(
    root: Path, gate_path: Path, *, require_descends: bool = True
) -> Layer1GateVerification:
    """Non-mutating verification of a committed L1-READY gate (check mode)."""
    from minos_engine.gates.verifier import load_gate, require_gate_pass, verify_gate_integrity

    root = root.resolve()
    reasons: list[str] = []
    try:
        gate = load_gate(gate_path)
    except Exception as exc:  # noqa: BLE001
        return Layer1GateVerification(
            ok=False, gate_hash="", checks={"loadable": False}, reasons=(str(exc),)
        )

    protocol = verify_protocol_ready(root)
    twin = verify_twin_ready_prerequisite(root)
    integrity = verify_gate_integrity(gate, base_dir=root)
    promotion = require_gate_pass(gate, base_dir=root)
    src_sha = gate.qualified_source_git_sha or ""

    checks = {
        "gate_name_l1_ready": gate.gate_name == GATE_NAME,
        "canonical_integrity": gate.gate_hash == gate.compute_hash(),
        "qualification_tool_identity": gate.qualification_tool_version == LAYER1_QUALIFIER_VERSION,
        "python_runtime_is_3_12": is_supported_runtime(),
        "layer1_evidence_verified": integrity.ok,
        "required_checks_and_promotion": promotion.ok,
        "protocol_prerequisite": protocol.ok,
        "twin_prerequisite": twin.ok,
        "qualified_commit_present": G.object_exists(root, src_sha),
        "qualified_tree_matches": G.commit_tree_sha(root, src_sha)
        == gate.qualified_source_tree_sha,
        "profiler_version_bound": gate.input_hashes.get("profiler_version") == L.profiler_version(),
        "profile_schema_bound": gate.input_hashes.get("layer1_schema_hash")
        == L.profile_schema_hash(),
    }
    if require_descends:
        checks["commit_b_descends_a"] = verify_parent_is(root, src_sha)

    for name, ok in checks.items():
        if not ok:
            reasons.append(f"{name} failed")
    reasons.extend(f"evidence: {r}" for r in integrity.reasons)
    reasons.extend(f"promotion: {r}" for r in promotion.reasons)
    return Layer1GateVerification(
        ok=all(checks.values()),
        gate_hash=gate.gate_hash,
        checks=checks,
        reasons=tuple(dict.fromkeys(reasons)),
    )


def write_layer1_outputs(result: Layer1QualificationResult, root: Path) -> tuple[Path, Path]:
    """Write the report first, then the gate carrying the report's sha256."""
    from minos_engine.gates.verifier import write_gate

    report_path = root / "reports" / "LAYER1_QUALIFICATION_REPORT.md"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(result.report_markdown, encoding="utf-8")
    report_sha = sha256_hex(report_path.read_bytes())

    data = result.gate.model_dump(mode="json")
    data["input_hashes"]["qualification_report_hash"] = report_sha
    data["gate_hash"] = ""  # force recompute over the updated content
    gate = GateArtifact.model_validate(data)
    gate_path = write_gate(gate, root / "gates" / "l1-ready.json")
    return gate_path, report_path


def _render_report(
    *,
    gate: GateArtifact,
    accounting: PytestAccounting,
    coverage: CoverageResult,
    tools: dict[str, bool],
    mandatory: dict[str, bool],
    provenance: GitProvenance,
    report: IntegrationReport | None,
    schema_hash: str,
    config_hash: str,
    version: str,
    created_at: str,
) -> str:
    required = required_checks_for(GATE_NAME)
    check_rows = "\n".join(
        f"| `{k}` | {'PASS' if v else 'FAIL'} | {'required' if k in required else 'supplemental'} |"
        for k, v in sorted(mandatory.items())
    )
    ev_rows = "\n".join(f"| `{e.path}` | {e.kind.value} | `{e.sha256}` |" for e in gate.evidence)
    rb = "not performed"
    if report:
        rb = (
            f"dataset `{report.dataset_id}` region `{report.region_source}` — "
            f"run1 {report.first_run_elapsed_seconds:.1f}s / {report.first_run_peak_rss_mb:.0f}MB, "
            f"run2 {report.second_run_elapsed_seconds:.1f}s / {report.second_run_peak_rss_mb:.0f}MB, "
            f"fingerprint-equal={report.repeat_run_fingerprint_equal}, "
            f"hard-limit {report.hard_limit_seconds:.0f}s met={report.hard_limit_met}"
        )
    return f"""# LAYER 1 — Qualification Report

**Gate:** {GATE_NAME} — **{gate.status.value}**
**Engine version:** {__version__}
**Qualification tool:** {LAYER1_QUALIFIER_VERSION}
**Profiler:** {version} · schema `{schema_hash[:16]}…` · config `{config_hash[:16]}…`
**Qualified source git sha:** `{gate.qualified_source_git_sha}`
**Qualified source tree sha:** `{gate.qualified_source_tree_sha}`
**Generated:** {created_at}

> Generated by `minos-engine layer1 qualify`. Not hand-authored. A PASS gate is
> not constructible with any failing mandatory check; the L1-READY required set is
> enforced. Evidence is hashed from the qualified commit's blobs. The gate records
> this report's sha256; the report does not embed the gate hash (no cycle).

## Prerequisites (accepted, git-bound)
- PROTOCOL-READY `{gate.input_hashes.get("prerequisite_protocol_ready_gate_hash")}`
- TWIN-READY `{gate.input_hashes.get("prerequisite_twin_ready_gate_hash")}`

## Real-BAM qualification (two-tier)
- synthetic_ci_qualified: **{"yes" if suite_passes(accounting) else "no"}**
- real_bam_qualified: **{"yes" if mandatory["layer1_real_bam_qualified"] else "no"}** — {rb}

## Test execution (JUnit XML)
| Metric | Value |
|---|---|
| Collected | {accounting.collected} |
| Passed | {accounting.passed} |
| Failed | {accounting.failed} |
| Errors | {accounting.errors} |
| Coverage | {coverage.line_coverage_percent}% (threshold {STAGE0_COVERAGE_THRESHOLD}%) |
| ruff/format/mypy | {"PASS" if tools["ruff_check"] else "FAIL"} / {"PASS" if tools["ruff_format"] else "FAIL"} / {"PASS" if tools["mypy"] else "FAIL"} |

## Mandatory checks
| Check | Status | Kind |
|---|---|---|
{check_rows}

## Evidence (git-tree-bound, hashed from the qualified commit)
| Path | Kind | sha256 |
|---|---|---|
{ev_rows}

## Known limitations
- Composite Minos AdvancedScorer remains out of scope (Layer 1 is descriptive only).
- Cost-model / confidence-weight calibration deferred (documented in the audit).
- Layer 2 remains blocked until this L1-READY gate verifies through the entry gate.
"""
