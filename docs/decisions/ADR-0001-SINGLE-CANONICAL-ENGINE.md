# ADR-0001 — Single canonical engine and package root

## Status
Accepted (Stage 0).

## Context
The Overall spec's Architecture-correction is explicit: *one canonical emission
path*. The pairwise ranker and contextual engine must be invoked by production
`cmd_predict`, and `BaselineEngine`-only live emission is prohibited. A recurring
failure mode in similar systems is two competing CONFIG-emission engines (a
"baseline" path and an "adaptive" path) diverging in production.

The repository was an empty `git init`, so there was no existing package root to
reconcile. The specs offer two trees: the Overall spec §5 (full, multi-stage)
and the Stage-0 assignment §3 (the subset to build now).

## Decision
1. Use a single Python package rooted at **`src/minos_engine/`** (src-layout).
   There is exactly one package root; no parallel `engine/` tree is created.
2. There is exactly one production CONFIG-emission interface:
   `Layer2Service.select_config`. `BaselineEngine` is defined (in later stages)
   only as a *fallback mode inside* that service (mode `SAFE_BASELINE`), never a
   separate production engine. An architecture test asserts a single
   `select_config` definer.
3. Stage 0 follows the assignment §3 module tree; later-stage packages
   (`twin/`, `storage/`, `experiments/`, `models/`, `feedback/`,
   `observability/`) from Overall spec §5 are deferred to their stages.

## Consequences
- No possibility of a second live emission engine slipping in unnoticed.
- The deferred packages are documented, not silently dropped; they arrive with
  their stage.
