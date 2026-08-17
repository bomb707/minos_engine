"""Build a deterministic, side-effect-free GATK execution plan.

Uses the Stage 0 25-parameter registry + canonicalizer to validate and
canonicalize the CONFIG, then renders a placeholder-tokenized argv. Concrete
paths are represented as symbolic tokens (``{reference}``/``{bam}``/``{output}``)
so the plan hash is reproducible across machines; identities are recorded in
``declared_inputs``/``declared_outputs``. Nothing is executed here.
"""

from __future__ import annotations

from minos_engine.callers.contracts import ParameterSpaceSnapshot
from minos_engine.callers.gatk.config import canonicalize_config
from minos_engine.common.errors import PolicyViolationError
from minos_engine.tools.gatk import build_gatk_argv

from .contracts import GatkExecutionPlan, ToolInvocation, TwinExecutionRequest

__all__ = ["build_execution_plan", "REFERENCE_TOKEN", "BAM_TOKEN", "OUTPUT_TOKEN"]

REFERENCE_TOKEN = "{reference}"
BAM_TOKEN = "{bam}"
OUTPUT_TOKEN = "{output}"


def build_execution_plan(
    request: TwinExecutionRequest,
    *,
    parameter_space: ParameterSpaceSnapshot | None = None,
) -> GatkExecutionPlan:
    """Validate the CONFIG and build a reproducible GATK execution plan."""
    if request.gatk_tool.name != "gatk":
        raise PolicyViolationError(
            f"Twin execution plan supports only the GATK caller, got {request.gatk_tool.name!r}"
        )
    if (
        parameter_space is not None
        and parameter_space.parameter_space_hash != request.parameter_space_hash
    ):
        raise PolicyViolationError(
            "parameter_space hash does not match the request's parameter_space_hash"
        )

    canonical = canonicalize_config(request.requested_config, parameter_space=parameter_space)

    argv = build_gatk_argv(
        effective_config=canonical.effective_config,
        region=request.region,
        reference_path=REFERENCE_TOKEN,
        bam_path=BAM_TOKEN,
        output_path=OUTPUT_TOKEN,
    )
    invocation = ToolInvocation(
        tool=request.gatk_tool,
        argv=argv,
        declared_inputs={
            "reference": request.reference_sha256,
            "bam": request.bam_sha256 or "unavailable",
        },
        declared_outputs={"vcf": request.output_uri},
    )
    return GatkExecutionPlan(
        round_id=request.round_id,
        caller="gatk",
        region=request.region,
        effective_config=canonical.effective_config,
        config_hash=canonical.config_hash,
        parameter_space_hash=request.parameter_space_hash,
        invocation=invocation,
    )
