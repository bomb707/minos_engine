"""In-process TWIN-READY mandatory checks (fast, no subprocess).

Each returns a bool; the twin runner assembles them into the gate. These check
concrete Twin behaviors beyond 'all tests pass': schema validity, deterministic
hashing, plan construction, comparison parsing, honest-unavailable scoring,
deterministic replay, parity mismatch detection, truth isolation, architecture
boundaries, and the absence of any network dependency in the Twin/tools code.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any

from minos_engine.common.errors import ComparisonError
from minos_engine.intake.contracts import Region
from minos_engine.schema_registry import validate_against
from minos_engine.tools.happy import parse_raw_result
from minos_engine.twin.comparison import build_comparison_metrics
from minos_engine.twin.contracts import (
    DECLARED_PARITY_LEVEL,
    ComparisonMetrics,
    ParityExpectation,
    ParityLevel,
    ParityObservation,
    TwinExecutionRequest,
)
from minos_engine.twin.execution_plan import build_execution_plan
from minos_engine.twin.identities import ToolIdentity
from minos_engine.twin.parity import assess_parity
from minos_engine.twin.scoring import build_score_inputs, compute_score
from minos_engine.twin.service import TwinService
from minos_engine.twin.unavailable import AvailabilityStatus, ReasonCode

__all__ = [
    "TWIN_REQUIRED_TRACKED_FILES",
    "TWIN_DOCS",
    "make_request",
    "twin_contracts_schema_valid",
    "twin_canonical_hashing_deterministic",
    "gatk_execution_plan_ok",
    "comparison_parser_ok",
    "scoring_matches_declared_level",
    "unavailable_scoring_honest",
    "fixture_replay_deterministic",
    "parity_mismatch_detection_works",
    "truth_isolation_ok",
    "architecture_boundaries_ok",
    "no_hidden_network_dependency",
    "stage1_documentation_complete",
]

_H = "a" * 64
_TS = "2026-08-17T12:00:00+00:00"
_REGION = Region.from_source("chr19:13000000-23000000", "one_based_inclusive")

# Production packages that must never import the Twin/tools/offline code.
_PRODUCTION_PACKAGES = ("protocol", "callers", "layer1", "layer2", "intake", "manifests", "common")
_NETWORK_TOKENS = (
    "socket",
    "http.client",
    "urllib.request",
    "requests",
    "httpx",
    "ftplib",
    "asyncio",
)

TWIN_REQUIRED_TRACKED_FILES = (
    "configs/twin/default.yaml",
    "schemas/twin-execution-request-v1.schema.json",
    "schemas/twin-comparison-result-v1.schema.json",
    "schemas/twin-score-result-v1.schema.json",
    "schemas/twin-parity-report-v1.schema.json",
    "src/minos_engine/twin/contracts.py",
    "src/minos_engine/twin/service.py",
    "src/minos_engine/tools/gatk.py",
    "src/minos_engine/tools/happy.py",
    "tests/fixtures/twin/FIXTURE_MANIFEST.json",
    "tests/fixtures/twin/replay/valid.json",
    "reports/STAGE1_PREIMPLEMENTATION_AUDIT.md",
)

TWIN_DOCS = (
    "docs/twin/ARCHITECTURE.md",
    "docs/twin/PARITY_LEVELS.md",
    "docs/twin/SCORING_CONTRACT.md",
    "docs/twin/TOOL_ADAPTERS.md",
    "docs/twin/FIXTURE_PROVENANCE.md",
    "docs/twin/LIMITATIONS.md",
    "docs/runbooks/VALIDATOR_TWIN_REPLAY.md",
)


def make_request(config: dict[str, Any] | None = None, tool: str = "gatk") -> TwinExecutionRequest:
    return TwinExecutionRequest(
        round_id="R1",
        region=_REGION,
        requested_config=config if config is not None else {"min_pruning": 3},
        parameter_space_hash=_H,
        protocol_snapshot_hash="b" * 64,
        reference_sha256="c" * 64,
        bam_sha256="d" * 64,
        output_uri="s3://x/out.vcf.gz",
        budget_seconds=300.0,
        gatk_tool=ToolIdentity(name=tool, version="4.5.0.0"),
        engine_git_sha="stage1",
    )


def _comparison() -> ComparisonMetrics:
    raw = {"snp": {"tp": 90, "fp": 5, "fn": 10}, "indel": {"tp": 40, "fp": 8, "fn": 12}}
    return build_comparison_metrics(
        round_id="R1",
        region=_REGION,
        reference_sha256="c" * 64,
        raw=parse_raw_result(raw),
        truth_vcf_sha256="e" * 64,
        query_vcf_sha256="f" * 64,
        tool=ToolIdentity(name="hap.py", version="0.3.14"),
        raw_payload=raw,
    )


def twin_contracts_schema_valid() -> bool:
    req = make_request()
    validate_against("twin-execution-request-v1", req.model_dump(mode="json"))
    cm = _comparison()
    validate_against("twin-comparison-result-v1", cm.model_dump(mode="json"))
    score = compute_score(build_score_inputs(cm))
    validate_against("twin-score-result-v1", score.model_dump(mode="json"))
    report = assess_parity(
        name="x",
        expectation=ParityExpectation(name="x", expected_hash=_H),
        observation=ParityObservation(name="x", observed_hash=_H),
        declared_level=DECLARED_PARITY_LEVEL,
        created_at=_TS,
    )
    validate_against("twin-parity-report-v1", report.model_dump(mode="json"))
    return True


def twin_canonical_hashing_deterministic() -> bool:
    req = make_request()
    return build_execution_plan(req).plan_hash == build_execution_plan(req).plan_hash


def gatk_execution_plan_ok() -> bool:
    plan = build_execution_plan(make_request())
    return plan.caller == "gatk" and plan.invocation.argv[0] == "gatk" and bool(plan.plan_hash)


def comparison_parser_ok() -> bool:
    _comparison()  # valid parses + recomputes
    for bad in (
        {"snp": {"tp": 1, "fp": 0, "fn": 0}},
        {"snp": {"tp": -1, "fp": 0, "fn": 0}, "indel": {"tp": 0, "fp": 0, "fn": 0}},
    ):
        try:
            parse_raw_result(bad)
            return False
        except ComparisonError:
            pass
    return True


def scoring_matches_declared_level() -> bool:
    # Declared FIXTURE_REPLAY makes no numerical-scorer claim; score is unavailable.
    score = compute_score(build_score_inputs(_comparison()))
    return (
        DECLARED_PARITY_LEVEL is ParityLevel.FIXTURE_REPLAY
        and score.status is AvailabilityStatus.UNAVAILABLE
    )


def unavailable_scoring_honest() -> bool:
    score = compute_score(build_score_inputs(_comparison()))
    return (
        score.status is AvailabilityStatus.UNAVAILABLE
        and score.reason_code is ReasonCode.AUTHORITATIVE_SCORER_NOT_AVAILABLE
        and score.final_score is None
        and score.components is None
    )


def fixture_replay_deterministic() -> bool:
    service = TwinService(protocol_ready_check=lambda: _H)
    req = make_request()
    raw = {"snp": {"tp": 90, "fp": 5, "fn": 10}, "indel": {"tp": 40, "fp": 8, "fn": 12}}
    tool = ToolIdentity(name="hap.py", version="0.3.14")
    a = service.replay(
        req,
        raw,
        truth_vcf_sha256="e" * 64,
        query_vcf_sha256="f" * 64,
        comparison_tool=tool,
        fixture_hash=_H,
        now_iso=_TS,
    )
    b = service.replay(
        req,
        raw,
        truth_vcf_sha256="e" * 64,
        query_vcf_sha256="f" * 64,
        comparison_tool=tool,
        fixture_hash=_H,
        now_iso="2030-01-01T00:00:00+00:00",
    )
    return a.manifest.manifest_hash == b.manifest.manifest_hash


def parity_mismatch_detection_works() -> bool:
    exp = ParityExpectation(name="p", expected_hash=_H, fields={"caller": "gatk"})
    match = assess_parity(
        name="p",
        expectation=exp,
        observation=ParityObservation(name="p", observed_hash=_H, fields={"caller": "gatk"}),
        declared_level=DECLARED_PARITY_LEVEL,
        created_at=_TS,
    )
    mismatch = assess_parity(
        name="p",
        expectation=exp,
        observation=ParityObservation(
            name="p", observed_hash="b" * 64, fields={"caller": "bcftools"}
        ),
        declared_level=DECLARED_PARITY_LEVEL,
        created_at=_TS,
    )
    return match.matched and (not mismatch.matched) and len(mismatch.differences) >= 1


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    mods: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            mods.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            mods.add(node.module)
    return mods


def architecture_boundaries_ok(src_dir: Path) -> bool:
    forbidden = ("minos_engine.twin", "minos_engine.tools")
    for pkg in _PRODUCTION_PACKAGES:
        for f in (src_dir / pkg).rglob("*.py"):
            for m in _imports(f):
                if any(m == t or m.startswith(t + ".") for t in forbidden):
                    return False
    return True


def truth_isolation_ok(src_dir: Path) -> bool:
    # No production package may import the offline truth namespace.
    for pkg in _PRODUCTION_PACKAGES:
        for f in (src_dir / pkg).rglob("*.py"):
            for m in _imports(f):
                if "twin.offline" in m:
                    return False
    return True


def no_hidden_network_dependency(src_dir: Path) -> bool:
    for sub in ("twin", "tools"):
        for f in (src_dir / sub).rglob("*.py"):
            for m in _imports(f):
                if any(m == t or m.startswith(t.split(".")[0] + ".") for t in _NETWORK_TOKENS):
                    return False
    return True


def stage1_documentation_complete(root: Path) -> bool:
    return all((root / d).exists() for d in TWIN_DOCS)
