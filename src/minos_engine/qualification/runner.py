"""Stage 0 qualification runner.

Orchestrates the real gates (pytest via JUnit, coverage via XML, ruff, ruff
format, mypy) plus in-process checks, hashes the evidence tree, and builds the
PROTOCOL-READY ``GateArtifact`` + qualification report. It does not write files
(the caller writes the two-commit artifacts).

Non-cyclic hashing: evidence covers source/tests/schemas/configs/docs/specs/
build files (Commit A). It never includes the report or the gate itself, so the
report may reference the gate hash without a cycle.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from minos_engine import __version__
from minos_engine.callers.gatk.config import canonicalize_config
from minos_engine.callers.gatk.parameter_registry import REGISTRY
from minos_engine.common.hashing import canonical_hash
from minos_engine.gates.contracts import EvidenceItem, EvidenceKind, GateArtifact, GateStatus
from minos_engine.gates.required_checks import required_checks_for
from minos_engine.manifests.builder import engine_config_hash, protocol_contract_hash
from minos_engine.settings import Settings

from . import QUALIFICATION_TOOL_VERSION
from . import checks as C
from .coverage import STAGE0_COVERAGE_THRESHOLD, CoverageResult, run_coverage
from .evidence import hash_path, sha256_directory, sha256_file
from .provenance import GitProvenance, read_provenance
from .pytest_accounting import PytestAccounting, run_pytest, suite_passes

__all__ = ["QualificationResult", "qualify", "write_outputs"]

GATE_NAME = "PROTOCOL-READY"

# (path, kind) evidence set — Commit A source tree. NOT the report or gate.
_EVIDENCE = (
    ("reports/STAGE0_PREIMPLEMENTATION_AUDIT.md", EvidenceKind.FILE),
    ("schemas", EvidenceKind.DIRECTORY),
    ("configs", EvidenceKind.DIRECTORY),
    ("src/minos_engine", EvidenceKind.DIRECTORY),
    ("tests", EvidenceKind.DIRECTORY),
    ("docs", EvidenceKind.DIRECTORY),
    ("docs/specifications/SPECIFICATION_MANIFEST.json", EvidenceKind.FILE),
    ("pyproject.toml", EvidenceKind.FILE),
    ("Makefile", EvidenceKind.FILE),
    (".github/workflows/ci.yml", EvidenceKind.FILE),
)


class QualificationResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    gate: GateArtifact
    report_markdown: str
    accounting: PytestAccounting
    coverage: CoverageResult
    tools: dict[str, bool]
    provenance: GitProvenance


def _bin(name: str) -> str:  # pragma: no cover - environment path resolution
    candidate = Path(sys.executable).parent / name
    if candidate.exists():
        return str(candidate)
    found = shutil.which(name)
    return found or name


def _tool_ok(cmd: list[str], root: Path) -> bool:  # pragma: no cover - subprocess glue
    proc = subprocess.run(cmd, cwd=str(root), capture_output=True, text=True, check=False)
    return proc.returncode == 0


def _build_evidence(root: Path) -> tuple[list[EvidenceItem], bool]:
    items: list[EvidenceItem] = []
    complete = True
    for rel, kind in _EVIDENCE:
        target = root / rel
        if not target.exists():
            complete = False
            items.append(EvidenceItem(description=rel, path=rel, kind=kind, sha256=None))
            continue
        if kind is EvidenceKind.DIRECTORY:
            digest, _ = sha256_directory(target)
            items.append(EvidenceItem(description=rel, path=rel, kind=kind, sha256=digest))
        else:
            data = target.read_bytes()
            items.append(
                EvidenceItem(
                    description=rel,
                    path=rel,
                    kind=kind,
                    sha256=sha256_file(target),
                    size_bytes=len(data),
                )
            )
    return items, complete


def qualify(root: Path) -> QualificationResult:  # pragma: no cover - subprocess orchestration
    """Gather real tool results (subprocess) then assemble the gate.

    Not unit-tested because it spawns pytest/coverage as subprocesses (which
    would recurse if run inside the test suite). The pure assembly it delegates
    to — :func:`assemble_result` — is fully unit-tested with synthetic inputs.
    """
    root = root.resolve()
    provenance = read_provenance(root)
    accounting = run_pytest(root)
    coverage = run_coverage(root)[0]
    tools = {
        "ruff_check": _tool_ok([_bin("ruff"), "check", "."], root),
        "ruff_format": _tool_ok([_bin("ruff"), "format", "--check", "."], root),
        "mypy": _tool_ok([_bin("mypy"), "src"], root),
    }
    return assemble_result(
        root, accounting=accounting, coverage=coverage, tools=tools, provenance=provenance
    )


def assemble_result(
    root: Path,
    *,
    accounting: PytestAccounting,
    coverage: CoverageResult,
    tools: dict[str, bool],
    provenance: GitProvenance,
    created_at: str | None = None,
) -> QualificationResult:
    """Build the gate + report from already-gathered tool results (no subprocess)."""
    root = root.resolve()
    src_dir = root / "src" / "minos_engine"
    settings = Settings.load()

    evidence, evidence_complete = _build_evidence(root)

    mandatory = {
        "all_tests_pass": suite_passes(accounting),
        "tests_collected_nonzero": accounting.collected > 0,
        "ruff_check_pass": tools["ruff_check"],
        "ruff_format_pass": tools["ruff_format"],
        "mypy_pass": tools["mypy"],
        "canonical_determinism": C.canonical_determinism(),
        "gatk_only_policy": C.gatk_only_policy(settings),
        "gatk_registry_has_25_params": C.gatk_registry_has_25_params(),
        "required_identity_fails_closed": C.required_identity_fails_closed(),
        "layer1_not_implemented": C.layer1_not_implemented(),
        "layer2_blocked": C.layer2_blocked(),
        "no_truth_or_locked_test_access": C.no_truth_or_locked_test_access(src_dir),
        "six_schemas_present": C.six_schemas_present(root),
        "documentation_complete": C.documentation_complete(root),
        "ci_workflow_present": C.ci_workflow_present(root),
        "evidence_hashes_complete": evidence_complete,
        "qualified_source_clean": provenance.worktree_clean,
        "coverage_threshold_met": coverage.meets(STAGE0_COVERAGE_THRESHOLD),
        "specifications_present": C.specifications_present(root),
    }

    status = GateStatus.PASS if all(mandatory.values()) else GateStatus.REJECT
    created_at = created_at or datetime.now(UTC).isoformat()

    input_hashes = {
        "gatk_registry_hash": REGISTRY.registry_hash(),
        "engine_config_hash": engine_config_hash(settings),
        "protocol_contract_hash": protocol_contract_hash(),
        "default_config_hash": canonicalize_config({}).config_hash,
        "specification_manifest_hash": _spec_manifest_hash(root),
    }

    gate = GateArtifact(
        gate_name=GATE_NAME,
        status=status,
        engine_git_sha=provenance.head_sha or "unavailable",
        input_hashes=input_hashes,
        evidence=tuple(evidence),
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
        created_at=created_at,
    )
    return QualificationResult(
        gate=gate,
        report_markdown=report,
        accounting=accounting,
        coverage=coverage,
        tools=tools,
        provenance=provenance,
    )


def _spec_manifest_hash(root: Path) -> str:
    manifest = root / "docs" / "specifications" / "SPECIFICATION_MANIFEST.json"
    if not manifest.exists():
        return canonical_hash({"status": "missing"})
    return hash_path(manifest)


def write_outputs(result: QualificationResult, root: Path) -> tuple[Path, Path]:
    """Write the qualification report and gate (the Commit B artifacts)."""
    from minos_engine.gates.verifier import write_gate

    report_path = root / "reports" / "STAGE0_QUALIFICATION_REPORT.md"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(result.report_markdown, encoding="utf-8")
    gate_path = write_gate(result.gate, root / "gates" / "protocol-ready.json")
    return gate_path, report_path


def _render_report(
    *,
    gate: GateArtifact,
    accounting: PytestAccounting,
    coverage: CoverageResult,
    tools: dict[str, bool],
    mandatory: dict[str, bool],
    provenance: GitProvenance,
    created_at: str,
) -> str:
    required = required_checks_for(GATE_NAME)
    check_rows = "\n".join(
        f"| `{name}` | {'PASS' if ok else 'FAIL'} | {'required' if name in required else 'supplemental'} |"
        for name, ok in sorted(mandatory.items())
    )
    hash_rows = "\n".join(f"| `{k}` | `{v}` |" for k, v in sorted(gate.input_hashes.items()))
    ev_rows = "\n".join(f"| `{e.path}` | {e.kind.value} | `{e.sha256}` |" for e in gate.evidence)
    return f"""# STAGE 0 — Qualification Report

**Gate:** {GATE_NAME} — **{gate.status.value}**
**Engine version:** {__version__}
**Qualification tool:** {gate.qualification_tool_version}
**Generated:** {created_at}
**Gate hash:** `{gate.gate_hash}`

> Generated by `minos-engine qualify` / `scripts/build_protocol_ready_gate.py`.
> Not hand-authored. A PASS gate is not constructible with any failing mandatory
> check, and its required-check set is enforced for the PROTOCOL-READY gate name.

## Source provenance (two-commit qualification)
| Field | Value |
|---|---|
| `qualified_source_git_sha` | `{gate.qualified_source_git_sha}` |
| `qualified_source_tree_sha` | `{gate.qualified_source_tree_sha}` |
| worktree clean at build | {provenance.worktree_clean} |

The gate hashes the Commit-A source tree (evidence below); it never hashes the
report or the gate, so there is no circular dependency. The report may reference
the gate hash because the report is not part of the gate's evidence.

## Test execution (machine-readable JUnit XML)
| Metric | Value |
|---|---|
| Collected | {accounting.collected} |
| Passed | {accounting.passed} |
| Failed | {accounting.failed} |
| Errors | {accounting.errors} |
| Skipped | {accounting.skipped} |
| Duration (s) | {round(accounting.duration_seconds, 3)} |
| Exit code | {accounting.exit_code} |

## Coverage (machine-readable XML)
| Metric | Value |
|---|---|
| Line coverage | {coverage.line_coverage_percent}% |
| Covered lines | {coverage.covered_lines} |
| Valid lines | {coverage.valid_lines} |
| Missing lines | {coverage.missing_lines} |
| Threshold | {STAGE0_COVERAGE_THRESHOLD}% |
| Tool | {coverage.tool} |

## Static analysis
| Tool | Command | Result |
|---|---|---|
| ruff (lint) | `ruff check .` | {"PASS" if tools["ruff_check"] else "FAIL"} |
| ruff (format) | `ruff format --check .` | {"PASS" if tools["ruff_format"] else "FAIL"} |
| mypy | `mypy src` | {"PASS" if tools["mypy"] else "FAIL"} |

## Mandatory checks
| Check | Status | Kind |
|---|---|---|
{check_rows}

## Input hashes
| Identity | Value |
|---|---|
{hash_rows}

## Evidence (hashed source tree — Commit A)
| Path | Kind | sha256 |
|---|---|---|
{ev_rows}

## Known limitations
- Live protocol integration is not enabled (fixture-backed only); the live
  client fails closed with `UnavailableError`.
- Commit-reveal is owner-reported but not yet verified through the integrated
  protocol source; Stage 0 represents it as typed-unavailable.
- Layer 1 is not implemented; Layer 2 is blocked until L1-READY.
- No GATK/hap.py execution, no PostgreSQL, no ML in Stage 0.
"""
