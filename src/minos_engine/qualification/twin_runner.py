"""TWIN-READY (Stage 1) qualification — git-tree-bound, two-commit.

Reuses the Stage 0 qualification building blocks (pytest via JUnit, coverage via
XML, ruff/mypy, git-tree-bound evidence) and adds the Twin-specific mandatory
checks plus the PROTOCOL-READY prerequisite. The gate records the declared
parity level and the prerequisite gate hash. ``qualify_twin`` orchestrates
subprocess tools; ``assemble_twin_result`` (pure) is unit-tested with synthetic
inputs.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from minos_engine import __version__
from minos_engine.gates.contracts import GateArtifact, GateStatus
from minos_engine.gates.required_checks import required_checks_for
from minos_engine.settings import Settings
from minos_engine.twin.contracts import ParityLevel

from . import QUALIFICATION_TOOL_VERSION
from . import checks as C
from . import git_tree as G
from . import twin_checks as TC
from .coverage import STAGE0_COVERAGE_THRESHOLD, CoverageResult, run_coverage
from .provenance import GitProvenance, read_provenance
from .pytest_accounting import PytestAccounting, run_pytest, suite_passes
from .runner import SourceIntegrity, _bin, _tool_ok, gather_source_integrity

__all__ = [
    "TwinQualificationResult",
    "qualify_twin",
    "assemble_twin_result",
    "write_twin_outputs",
]

GATE_NAME = "TWIN-READY"


class TwinQualificationResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    gate: GateArtifact
    report_markdown: str
    accounting: PytestAccounting
    coverage: CoverageResult
    tools: dict[str, bool]
    provenance: GitProvenance
    source_integrity: SourceIntegrity
    declared_parity_level: ParityLevel
    prerequisite_gate_hash: str


def _twin_required_tracked(root: Path) -> bool:
    if not G.is_git_repo(root):
        return False
    return all(G.is_tracked(root, f) for f in TC.TWIN_REQUIRED_TRACKED_FILES)


def qualify_twin(
    root: Path,
) -> TwinQualificationResult:  # pragma: no cover - subprocess orchestration
    root = root.resolve()
    provenance = read_provenance(root)
    ref = provenance.head_sha or "HEAD"
    source_integrity = gather_source_integrity(root, ref)
    twin_required = _twin_required_tracked(root)
    accounting = run_pytest(root)
    coverage = run_coverage(root)[0]
    tools = {
        "ruff_check": _tool_ok([_bin("ruff"), "check", "."], root),
        "ruff_format": _tool_ok([_bin("ruff"), "format", "--check", "."], root),
        "mypy": _tool_ok([_bin("mypy"), "src"], root),
    }
    return assemble_twin_result(
        root,
        accounting=accounting,
        coverage=coverage,
        tools=tools,
        provenance=provenance,
        source_integrity=source_integrity,
        twin_required_tracked=twin_required,
    )


def assemble_twin_result(
    root: Path,
    *,
    accounting: PytestAccounting,
    coverage: CoverageResult,
    tools: dict[str, bool],
    provenance: GitProvenance,
    source_integrity: SourceIntegrity,
    twin_required_tracked: bool,
    created_at: str | None = None,
) -> TwinQualificationResult:
    from minos_engine.twin.contracts import DECLARED_PARITY_LEVEL

    root = root.resolve()
    src_dir = root / "src" / "minos_engine"
    settings = Settings.load()
    si = source_integrity

    integrity_ok, promotion_ok, prereq_hash = TC.protocol_ready_prerequisite(root)

    mandatory = {
        "protocol_ready_prerequisite_valid": integrity_ok,
        "protocol_ready_promotion_authorized": promotion_ok,
        "twin_contracts_schema_valid": TC.twin_contracts_schema_valid(),
        "twin_canonical_hashing_deterministic": TC.twin_canonical_hashing_deterministic(),
        "gatk_execution_plan_ok": TC.gatk_execution_plan_ok(),
        "gatk_only_policy": C.gatk_only_policy(settings),
        "comparison_parser_ok": TC.comparison_parser_ok(),
        "scoring_matches_declared_level": TC.scoring_matches_declared_level(),
        "unavailable_scoring_honest": TC.unavailable_scoring_honest(),
        "fixture_replay_deterministic": TC.fixture_replay_deterministic(),
        "parity_mismatch_detection_works": TC.parity_mismatch_detection_works(),
        "truth_isolation_ok": TC.truth_isolation_ok(src_dir),
        "architecture_boundaries_ok": TC.architecture_boundaries_ok(src_dir),
        "no_hidden_network_dependency": TC.no_hidden_network_dependency(src_dir),
        "tests_collected_nonzero": accounting.collected > 0,
        "all_tests_pass": suite_passes(accounting),
        "ruff_check_pass": tools["ruff_check"],
        "ruff_format_pass": tools["ruff_format"],
        "mypy_pass": tools["mypy"],
        "coverage_threshold_met": coverage.meets(STAGE0_COVERAGE_THRESHOLD),
        "required_source_tracked": si.required_source_tracked and twin_required_tracked,
        "worktree_matches_head": si.worktree_matches_head,
        "evidence_hashes_complete": si.evidence_hashes_complete,
        "qualified_source_clean": provenance.worktree_clean,
        "stage1_documentation_complete": TC.stage1_documentation_complete(root),
        "layer1_not_implemented": C.layer1_not_implemented(),
        "layer2_blocked": C.layer2_blocked(),
    }

    status = GateStatus.PASS if all(mandatory.values()) else GateStatus.REJECT
    created_at = created_at or datetime.now(UTC).isoformat()

    input_hashes = {
        "prerequisite_protocol_ready_gate_hash": prereq_hash or "unavailable",
        "declared_parity_level": DECLARED_PARITY_LEVEL.value,
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
        qualification_tool_version=QUALIFICATION_TOOL_VERSION,
        created_at=created_at,
    )
    report = _render_report(
        gate=gate,
        accounting=accounting,
        coverage=coverage,
        tools=tools,
        mandatory=mandatory,
        provenance=provenance,
        prereq_hash=prereq_hash,
        declared_level=DECLARED_PARITY_LEVEL.value,
        created_at=created_at,
    )
    return TwinQualificationResult(
        gate=gate,
        report_markdown=report,
        accounting=accounting,
        coverage=coverage,
        tools=tools,
        provenance=provenance,
        source_integrity=si,
        declared_parity_level=DECLARED_PARITY_LEVEL,
        prerequisite_gate_hash=prereq_hash or "unavailable",
    )


def write_twin_outputs(result: TwinQualificationResult, root: Path) -> tuple[Path, Path]:
    from minos_engine.gates.verifier import write_gate

    report_path = root / "reports" / "STAGE1_QUALIFICATION_REPORT.md"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(result.report_markdown, encoding="utf-8")
    gate_path = write_gate(result.gate, root / "gates" / "twin-ready.json")
    return gate_path, report_path


def _render_report(
    *,
    gate: GateArtifact,
    accounting: PytestAccounting,
    coverage: CoverageResult,
    tools: dict[str, bool],
    mandatory: dict[str, bool],
    provenance: GitProvenance,
    prereq_hash: str | None,
    declared_level: str,
    created_at: str,
) -> str:
    required = required_checks_for(GATE_NAME)
    check_rows = "\n".join(
        f"| `{k}` | {'PASS' if v else 'FAIL'} | {'required' if k in required else 'supplemental'} |"
        for k, v in sorted(mandatory.items())
    )
    ev_rows = "\n".join(f"| `{e.path}` | {e.kind.value} | `{e.sha256}` |" for e in gate.evidence)
    return f"""# STAGE 1 — Validator Twin Qualification Report

**Gate:** {GATE_NAME} — **{gate.status.value}**
**Engine version:** {__version__}
**Declared parity level:** {declared_level}
**Prerequisite PROTOCOL-READY gate hash:** `{prereq_hash}`
**Qualified source git sha:** `{gate.qualified_source_git_sha}`
**Qualified source tree sha:** `{gate.qualified_source_tree_sha}`
**Generated:** {created_at}
**Gate hash:** `{gate.gate_hash}`

> Generated by `minos-engine twin qualify`. Not hand-authored. A PASS gate is
> not constructible with any failing mandatory check; the TWIN-READY required
> set is enforced. Evidence is hashed from the qualified commit's blobs.

## Parity & scoring honesty
- Declared parity level: **{declared_level}** (structural + deterministic fixture
  replay + recomputed comparison metrics). No real GATK/hap.py execution
  (not `TOOL_EXECUTION`); the composite Minos score is **UNAVAILABLE**
  (`AUTHORITATIVE_SCORER_NOT_AVAILABLE`) because the pinned AdvancedScorer
  formula is not defined in the authoritative specifications — so no
  `VALIDATOR_CONFIRMED` numerical parity is claimed.

## Test execution (JUnit XML)
| Metric | Value |
|---|---|
| Collected | {accounting.collected} |
| Passed | {accounting.passed} |
| Failed | {accounting.failed} |
| Errors | {accounting.errors} |
| Skipped | {accounting.skipped} |
| Exit code | {accounting.exit_code} |

## Coverage (XML) & static analysis
- Line coverage: **{coverage.line_coverage_percent}%** (threshold {STAGE0_COVERAGE_THRESHOLD}%)
- ruff check: {"PASS" if tools["ruff_check"] else "FAIL"} · ruff format: {"PASS" if tools["ruff_format"] else "FAIL"} · mypy: {"PASS" if tools["mypy"] else "FAIL"}

## Mandatory checks
| Check | Status | Kind |
|---|---|---|
{check_rows}

## Evidence (git-tree-bound, hashed from the qualified commit)
| Path | Kind | sha256 |
|---|---|---|
{ev_rows}

## Known limitations
- Composite AdvancedScorer score is typed-unavailable (formula not in specs).
- No real GATK/hap.py execution; fixture replay only.
- Live protocol integration remains fixture-backed; commit-reveal typed-unavailable.
- Layer 1 remains unimplemented; Layer 2 remains blocked.
"""
