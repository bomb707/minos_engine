"""Validator Twin service — deterministic orchestration, no hidden side effects.

Workflow (Overall spec §7; assignment §11):

    PROTOCOL-READY verification
      -> CONFIG canonicalization + GATK execution-plan construction
      -> comparison-result ingestion (fixture replay; adapters injected)
      -> scoring (typed unavailable) + parity
      -> immutable TwinRunManifest

The service requires a valid Stage 0 PROTOCOL-READY gate, injects tool adapters
(disabled by default; deterministic fakes in tests), performs NO network access,
and returns identical content hashes for identical semantic inputs.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict

from minos_engine.callers.contracts import ParameterSpaceSnapshot
from minos_engine.common.errors import GateError
from minos_engine.tools.happy import parse_raw_result

from . import TWIN_TOOL_VERSION
from .comparison import build_comparison_metrics
from .contracts import (
    DECLARED_PARITY_LEVEL,
    ComparisonMetrics,
    GatkExecutionPlan,
    TwinExecutionRequest,
    TwinRunManifest,
    TwinScoreResult,
)
from .execution_plan import build_execution_plan
from .fixtures import TwinReplayFixture
from .identities import ToolIdentity
from .scoring import build_score_inputs, compute_score

__all__ = [
    "TwinRunResult",
    "TwinService",
    "default_protocol_ready_check",
    "make_protocol_ready_check",
]


class TwinRunResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    plan: GatkExecutionPlan
    comparison: ComparisonMetrics
    score: TwinScoreResult
    manifest: TwinRunManifest


def make_protocol_ready_check(root: Path) -> Callable[[], str | None]:
    """Build a prerequisite check bound to a specific repository root.

    The returned callable verifies the accepted Stage 0 PROTOCOL-READY identity,
    rehashes its Stage 0 evidence from the qualified commit, and requires PASS
    promotion — returning the accepted gate hash only when all hold, else None.
    """
    from minos_engine.twin.prerequisites import protocol_ready_gate_hash

    def _check() -> str | None:
        return protocol_ready_gate_hash(root)

    return _check


def default_protocol_ready_check() -> str | None:
    """Discover the repository root from the current directory and verify.

    Prefer injecting an explicit verifier (``make_protocol_ready_check(root)`` or
    a fake in tests). This default discovers the repo via git and fails closed
    (returns ``None``) when no repository / accepted prerequisite is available —
    it never hard-codes a package-relative path that would break installed use.
    """
    from minos_engine.qualification.git_tree import repo_root

    root = repo_root()
    if root is None:
        return None
    return make_protocol_ready_check(root)()


class TwinService:
    """Deterministic Twin orchestration with injectable adapters/prerequisite."""

    def __init__(
        self,
        *,
        protocol_ready_check: Callable[[], str | None] = default_protocol_ready_check,
    ) -> None:
        self._protocol_ready_check = protocol_ready_check

    def _require_protocol_ready(self) -> str:
        gate_hash = self._protocol_ready_check()
        if not gate_hash:
            raise GateError(
                "Twin requires a valid PROTOCOL-READY gate (Stage 0) to authorize a run"
            )
        return gate_hash

    def replay(
        self,
        request: TwinExecutionRequest,
        comparison_raw: dict[str, Any],
        *,
        truth_vcf_sha256: str,
        query_vcf_sha256: str,
        comparison_tool: ToolIdentity,
        now_iso: str,
        parameter_space: ParameterSpaceSnapshot | None = None,
        fixture_hash: str | None = None,
    ) -> TwinRunResult:
        """Run one deterministic Twin replay from a raw comparison result."""
        prerequisite_gate_hash = self._require_protocol_ready()

        plan = build_execution_plan(request, parameter_space=parameter_space)

        raw = parse_raw_result(comparison_raw)
        comparison = build_comparison_metrics(
            round_id=request.round_id,
            region=request.region,
            reference_sha256=request.reference_sha256,
            raw=raw,
            truth_vcf_sha256=truth_vcf_sha256,
            query_vcf_sha256=query_vcf_sha256,
            tool=comparison_tool,
            raw_payload=comparison_raw,
        )

        score = compute_score(build_score_inputs(comparison))

        manifest = TwinRunManifest(
            round_id=request.round_id,
            region=request.region,
            engine_git_sha=request.engine_git_sha,
            protocol_snapshot_hash=request.protocol_snapshot_hash,
            parameter_space_hash=request.parameter_space_hash,
            config_hash=plan.config_hash,
            plan_hash=plan.plan_hash,
            comparison_hash=comparison.content_hash(),
            score_hash=score.content_hash(),
            parity_hash=None,
            scorer_status=score.status,
            declared_parity_level=DECLARED_PARITY_LEVEL,
            fixture_hash=fixture_hash,
            prerequisite_gate_hash=prerequisite_gate_hash,
            created_at=now_iso,
        )
        return TwinRunResult(plan=plan, comparison=comparison, score=score, manifest=manifest)

    def replay_fixture(
        self, fixture: TwinReplayFixture, *, now_iso: str, fixture_hash: str | None = None
    ) -> TwinRunResult:
        return self.replay(
            fixture.request,
            fixture.comparison_raw,
            truth_vcf_sha256=fixture.truth_vcf_sha256,
            query_vcf_sha256=fixture.query_vcf_sha256,
            comparison_tool=fixture.comparison_tool,
            now_iso=now_iso,
            fixture_hash=fixture_hash,
        )

    def tool_version(self) -> str:
        return TWIN_TOOL_VERSION
