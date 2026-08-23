"""Additive, versioned F7 GATK/Twin invocation-parity adapter (pure; no I/O, no execution).

The accepted Stage-1 Twin boundary and the accepted L2-F F5 execution boundary build the SAME
GATK HaplotypeCaller invocation by two independent code paths:

* Twin   — ``twin.execution_plan.build_execution_plan`` -> ``tools.gatk.build_gatk_argv``
* L2-F   — ``storage.l2f_gatk_runner.build_logical_invocation`` -> ``render_execution_argv``

This adapter builds the Twin plan from the SAME effective CONFIG, parameter-space identity,
region, BAM identity, reference identity and output role that the F5 invocation used, then
compares the two argv token streams **semantically**.

Exactly two normalizations are permitted, and both are documented symbolic spellings rather than
semantic changes:

1. **caller token** — the Twin argv leads with the literal ``"gatk"`` caller token; the F5 argv
   omits it because the pinned absolute executable is prepended at exec time. Presence of the
   GATK caller is asserted separately (GATK-only policy) rather than compared as a token.
2. **symbolic path tokens** — Twin spells the three placeholders ``{reference}``/``{bam}``/
   ``{output}``; F5 spells them ``<reference.fa>``/``<input.bam>``/``<output.vcf>``. Each pair is
   mapped onto a single canonical role name (``reference``/``bam``/``output``).

Nothing else is normalized. A parameter is never dropped, renamed, clamped, coerced or defaulted
to obtain parity, and a mismatch is reported as a structured first-difference — never converted
to PASS.
"""

from __future__ import annotations

from typing import Any

from minos_engine.common.errors import PolicyViolationError
from minos_engine.experiments.execution_contract import (
    ARGV_BAM_PLACEHOLDER,
    ARGV_OUTPUT_PLACEHOLDER,
    ARGV_REFERENCE_PLACEHOLDER,
    ExecutionInput,
    LogicalGatkInvocation,
)
from minos_engine.intake.contracts import Region
from minos_engine.qualification.l2f_harness_ready_contract import (
    ParityDifference,
    TwinParityResult,
)
from minos_engine.twin.contracts import GatkExecutionPlan, TwinExecutionRequest
from minos_engine.twin.execution_plan import (
    BAM_TOKEN,
    OUTPUT_TOKEN,
    REFERENCE_TOKEN,
    build_execution_plan,
)
from minos_engine.twin.identities import ToolIdentity

__all__ = [
    "F7_PARITY_ADAPTER_VERSION",
    "CALLER_TOKEN",
    "SUBCOMMAND",
    "PATH_ROLE_BY_TOKEN",
    "build_twin_plan_for_execution",
    "compare_invocation_parity",
]

#: bumping this version changes every qualification hash that embeds a parity result.
F7_PARITY_ADAPTER_VERSION = "l2f-gatk-twin-parity-v1"

CALLER_TOKEN = "gatk"
SUBCOMMAND = "HaplotypeCaller"

#: the ONLY permitted token normalization: two symbolic spellings of the same path role.
PATH_ROLE_BY_TOKEN: dict[str, str] = {
    REFERENCE_TOKEN: "reference",
    BAM_TOKEN: "bam",
    OUTPUT_TOKEN: "output",
    ARGV_REFERENCE_PLACEHOLDER: "reference",
    ARGV_BAM_PLACEHOLDER: "bam",
    ARGV_OUTPUT_PLACEHOLDER: "output",
}


def _normalize(token: str) -> str:
    """Map a symbolic path token onto its canonical role; leave every other token untouched."""
    return PATH_ROLE_BY_TOKEN.get(token, token)


def build_twin_plan_for_execution(
    *,
    effective_config: dict[str, Any],
    parameter_space_hash: str,
    inputs: ExecutionInput,
    output_uri: str,
    gatk_executable_sha256: str,
    gatk_version: str,
    engine_git_sha: str,
    budget_seconds: float,
) -> GatkExecutionPlan:
    """Build the accepted Stage-1 Twin plan from the SAME identities the F5 execution used.

    The effective CONFIG is passed through unchanged, so the Twin canonicalizer must reproduce it
    value-for-value; if the historical Twin parameter boundary could not represent an accepted
    live-GATK value, that surfaces here as a rejection rather than as a silent default.
    """
    request = TwinExecutionRequest(
        round_id=inputs.round_id,
        # the SAME pinned binary the official execution used (never a placeholder identity).
        gatk_tool=ToolIdentity(
            name=CALLER_TOKEN, version=gatk_version, digest=gatk_executable_sha256
        ),
        # the SAME half-open interval the F5 execution used; the 1-based inclusive source string
        # is exactly what GATK -L renders, so both sides share one region convention.
        region=Region(
            source=(
                f"{inputs.chromosome}:{inputs.region_start0 + 1}-{inputs.region_end0_exclusive}"
            ),
            source_coordinate_system="one_based_inclusive",
            contig=inputs.chromosome,
            start0=inputs.region_start0,
            end0_exclusive=inputs.region_end0_exclusive,
            length_bp=inputs.region_end0_exclusive - inputs.region_start0,
            verified=True,
        ),
        requested_config=dict(effective_config),
        parameter_space_hash=parameter_space_hash,
        # the CONFIG was canonicalized against exactly this committed parameter-space snapshot.
        protocol_snapshot_hash=parameter_space_hash,
        reference_sha256=inputs.reference_sha256,
        bam_sha256=inputs.bam_sha256,
        output_uri=output_uri,
        budget_seconds=budget_seconds,
        engine_git_sha=engine_git_sha,
    )
    plan = build_execution_plan(request)
    if plan.effective_config != dict(effective_config):
        # the accepted live-GATK CONFIG must survive the Twin boundary value-for-value.
        raise PolicyViolationError(
            "the Twin boundary did not preserve the accepted live-GATK effective CONFIG"
        )
    return plan


def compare_invocation_parity(
    plan: GatkExecutionPlan,
    invocation: LogicalGatkInvocation,
    *,
    execution_config_hash: str,
) -> TwinParityResult:
    """Compare the Twin plan and the F5 logical invocation semantically, token by token.

    Returns a structured result; a mismatch records the FIRST differing semantic token/field and
    never becomes a pass.
    """
    twin_argv = tuple(plan.invocation.argv)
    exec_argv = tuple(invocation.logical_argv)

    def _fail(diff: ParityDifference, compared: int) -> TwinParityResult:
        return TwinParityResult(
            adapter_version=F7_PARITY_ADAPTER_VERSION,
            parity_ok=False,
            compared_token_count=compared,
            twin_plan_hash=plan.plan_hash,
            twin_config_hash=plan.config_hash,
            execution_config_hash=execution_config_hash,
            region_token=invocation.region_token,
            normalized_path_tokens=("reference", "bam", "output"),
            first_difference=diff,
        )

    # 1) caller: GATK only, on both sides.
    if plan.caller != CALLER_TOKEN:
        return _fail(
            ParityDifference(field="caller", twin_value=plan.caller, execution_value=CALLER_TOKEN),
            0,
        )
    if not twin_argv or twin_argv[0] != CALLER_TOKEN:
        return _fail(
            ParityDifference(
                field="caller_token",
                index=0,
                twin_value=twin_argv[0] if twin_argv else None,
                execution_value=CALLER_TOKEN,
            ),
            0,
        )
    # the F5 argv omits the caller token because the pinned executable is prepended at exec time.
    twin_body = twin_argv[1:]

    # 2) subcommand.
    if invocation.tool != SUBCOMMAND:
        return _fail(
            ParityDifference(
                field="subcommand", twin_value=SUBCOMMAND, execution_value=invocation.tool
            ),
            0,
        )

    # 3) token count.
    if len(twin_body) != len(exec_argv):
        return _fail(
            ParityDifference(
                field="argv_length",
                twin_value=str(len(twin_body)),
                execution_value=str(len(exec_argv)),
            ),
            min(len(twin_body), len(exec_argv)),
        )

    # 4) every token, in order (ordering IS contractually significant for GATK argv).
    for index, (twin_token, exec_token) in enumerate(zip(twin_body, exec_argv, strict=True)):
        left, right = _normalize(twin_token), _normalize(exec_token)
        if left != right:
            return _fail(
                ParityDifference(
                    field="argv_token",
                    index=index,
                    twin_value=twin_token,
                    execution_value=exec_token,
                ),
                index,
            )

    # 5) the region convention must agree exactly (1-based inclusive on both sides).
    region_index = twin_body.index("-L") + 1 if "-L" in twin_body else None
    if region_index is None or twin_body[region_index] != invocation.region_token:
        return _fail(
            ParityDifference(
                field="region_token",
                index=region_index,
                twin_value=None if region_index is None else twin_body[region_index],
                execution_value=invocation.region_token,
            ),
            len(twin_body),
        )

    # 6) the CONFIG identity itself must agree: parity of tokens with a different CONFIG identity
    #    would mean one side silently defaulted a parameter.
    if plan.config_hash != execution_config_hash:
        return _fail(
            ParityDifference(
                field="config_hash",
                twin_value=plan.config_hash,
                execution_value=execution_config_hash,
            ),
            len(twin_body),
        )

    return TwinParityResult(
        adapter_version=F7_PARITY_ADAPTER_VERSION,
        parity_ok=True,
        compared_token_count=len(twin_body),
        twin_plan_hash=plan.plan_hash,
        twin_config_hash=plan.config_hash,
        execution_config_hash=execution_config_hash,
        region_token=invocation.region_token,
        normalized_path_tokens=("reference", "bam", "output"),
        first_difference=None,
    )
