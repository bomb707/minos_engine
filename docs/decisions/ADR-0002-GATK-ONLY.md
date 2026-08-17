# ADR-0002 — GATK-only caller policy

## Status
Accepted (Stage 0).

## Context
The reference subnet supports several callers (`gatk`, `deepvariant`,
`bcftools`, with `freebayes` deprecated). The Overall spec §1 mandates GATK
HaplotypeCaller only across executable, search, candidate, Twin,
retrieval/ranking, and live paths. Canonical runtime policy: `caller.active =
gatk`. Legacy caller *data* may be preserved but must not be selectable through
the active engine.

## Decision
1. The runtime policy (`configs/runtime/gatk_only.yaml`) sets `active: gatk`,
   `allowed: [gatk]`, `disabled: [deepvariant, bcftools, freebayes]`.
2. `RuntimePolicy` (the typed settings model) *rejects* any non-gatk `active`
   value and any `allowed` set other than `("gatk",)` with a
   `PolicyViolationError`.
3. `ParameterSpaceSnapshot` and `SubmissionEnvelope` reject a non-gatk caller.
4. No DeepVariant/BCFtools adapter is created in the engine; there is nothing to
   isolate because nothing selectable exists.
5. Historical DeepVariant/BCFtools/FreeBayes *data* is neither imported nor
   deleted by Stage 0.

## Consequences
- The engine cannot execute or select a non-GATK caller.
- Architecture tests assert disabled callers cannot become active.
- If the subnet later changes caller policy, this ADR is revisited; the change
  is versioned, not silent.
