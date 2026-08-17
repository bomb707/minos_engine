# GATK CONFIG Contract

## Registry (`callers/gatk/parameter_registry.py`)
Exactly **25** GATK HaplotypeCaller parameters. Each entry records: `name`,
`type` (int/float/bool/enum), `official_default`, documented range/enum, control
group, `state`, `changeable`, `runtime_adaptive`, coupling rules, and source
(`Layer2 spec §6`, `documented-2026-08-09`).

### States (Layer 2 spec §7)
- `emit_ref_confidence`, `sample_ploidy` → **FIXED** (protocol-determined).
- all others → **EXPERIMENTAL** (offline evidence only; never live from a legal
  range alone). No parameter is `ACTIVE` in Stage 0.
- `changeable = (state != FIXED)`; `runtime_adaptive = false` for all in Stage 0.

### Coupling
`min_assembly_region_size < max_assembly_region_size` (validated on the
registry defaults and on every effective CONFIG).

### Documented vs runtime ranges
The registry holds the **documented** ranges. At runtime a
`ParameterSpaceSnapshot` may override them (versioned, hashed). Known drift
between documented and live subnet ranges (e.g. `min_pruning` 2..10 vs 1..10;
`max_num_haplotypes_in_population` 8..128 vs 8..512;
`max_assembly_region_size` 100..700 vs 100..1000) is exactly why the runtime
snapshot must be fetched and hashed, not baked in.

## Canonicalization (`callers/gatk/config.py`)
`canonicalize_config(requested, *, registry, parameter_space=None)`:
1. reject unknown keys;
2. exact JSON types — **no** implicit coercion (no str→number; `bool` is not
   `int`; an `int` is accepted where a `float` is expected and widened);
3. apply protocol-fixed values and explicit defaults — a request that changes a
   FIXED param to a non-default value is rejected;
4. validate ranges/enums against the runtime parameter space when supplied, else
   the documented ranges;
5. validate `min_assembly_region_size < max_assembly_region_size`;
6. produce a deterministic **effective** CONFIG (all 25 params);
7. canonical-JSON serialize, compute `config_hash`;
8. return both `requested_config` and `effective_config`.

Equivalent key orders produce identical effective bytes and `config_hash`.

## CLI flags (`callers/gatk/command.py`)
CONFIG-key → HaplotypeCaller flag mapping (metadata only; **no execution** in
Stage 0). `render_flag_args(effective)` builds an argument list as pure strings;
booleans render `--flag true|false`.
