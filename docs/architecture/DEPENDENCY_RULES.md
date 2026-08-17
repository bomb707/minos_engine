# Dependency & Import Rules

These rules are enforced by tests in `tests/leakage/` (static AST scans) and by
the typed contracts. CI must run them.

## Ownership
1. `protocol` owns Minos state retrieval and the official submission contract.
   Layer 2 returns a decision; it holds no HTTP credentials and performs no
   submission side effects.
2. `intake` owns round-artifact identities and reference resolution.
3. CONFIG **generation** and CONFIG **submission** are separate operations.
4. CLI modules are thin composition roots: dependency injection only. Domain
   modules contain no `argparse`, no environment lookups, no ad-hoc SQL.

## Import boundaries
| Package | May import | May NOT import |
|---|---|---|
| `common` | stdlib, pydantic | any domain package |
| `intake` | `common` | `protocol`, `layer2`, evaluation/truth |
| `callers` | `common`, `intake` | evaluation/truth, `layer2` |
| `protocol` | `common`, `intake`, `callers` | `layer2`, evaluation/truth |
| `layer1` (future) | `common`, contracts, pysam/pyarrow | `layer2`, `twin`, evaluation, score, truth, mutations, retrieval, models, hap.py |
| `layer2` (future) | Layer 1 *contract types*, registries, model/storage interfaces | BAM/BAI readers (`pysam`), `intake` file opening, evaluation tables |
| live image | engine packages | evaluator, hap.py, scoring, twin, truth-mount paths |

## Enforced invariants (tests)
- `test_layer1_cannot_import_eval_truth_or_layer2`
- `test_layer2_cannot_import_bam_readers_or_intake`
- `test_live_package_cannot_import_evaluator_happy_or_scoring`
- `test_domain_modules_do_not_parse_cli_args`
- `test_single_config_emission_interface` (exactly one `select_config`)
- `test_disabled_callers_cannot_be_active` (GATK-only)
- `test_no_truth_data_access_in_source` (truth isolation)

## Stage gating
Layer 2 remains blocked until `l1-ready.json` verifies (schema hash + profiler
config hash + PASS + evidence). A breaking Layer 1 schema change invalidates
L1-READY and re-blocks Layer 2. See ADR-0004.
