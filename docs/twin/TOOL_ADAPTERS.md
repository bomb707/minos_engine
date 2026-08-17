# Validator Twin — Tool Adapters

Adapters live in `src/minos_engine/tools/`. They are **side-effect-free** in
Stage 1: no process is spawned, no container opened, no filesystem touched, and
no network used. Runner *ports* are defined so a later stage can supply a
resource-capped executor without changing the contracts.

## GATK (`tools/gatk.py`)
- `build_gatk_argv(...)` renders a deterministic **tokenized argv list**
  (never a shell string; never `shell=True`). Paths are symbolic placeholders
  (`{reference}`, `{bam}`, `{output}`) so the plan hash is reproducible; concrete
  paths are substituted at execution time (later stage) as separate argv tokens,
  which handles spaces/special characters safely without shell quoting.
- Parameter flags follow the Stage 0 25-parameter registry ordering; the CONFIG
  is validated and canonicalized by the Stage 0 canonicalizer.
- `GATK_only` is enforced: `build_execution_plan` rejects a non-GATK caller.
- `GatkRunner` (port): `DisabledGatkRunner` fails closed
  (`TOOL_EXECUTION_NOT_ENABLED`); `FakeGatkRunner` is deterministic and labels
  itself as not having executed GATK.

## hap.py (`tools/happy.py`)
- `parse_raw_result(...)` parses a small synthetic JSON raw result into
  `RawComparison`, failing closed on malformed/incomplete/negative input.
  Parsing is isolated from normalization (`twin/comparison.py`) and scoring.
- `HappyRunner` (port): `DisabledHappyRunner` fails closed; `FakeHappyRunner`
  returns a pre-supplied raw result (fixture replay) without running hap.py.

## Redaction
`ToolInvocation.redacted_command()` renders a human-readable command with
credential-like tokens (`token=`, `sig=`, signed URLs) redacted. Adapters never
log secrets or signed URLs.

## Not implemented in Stage 1
Real GATK / hap.py execution. A future `TOOL_EXECUTION` stage supplies concrete
runners (resource-capped container) behind the existing ports; this does not
change the Stage 1 contracts.
