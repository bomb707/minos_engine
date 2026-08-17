# Validator Twin — Architecture (Stage 1)

The Validator Twin is a deterministic, auditable, fixture-backed model of the
Minos validator's **observable** evaluation process (Overall spec §7). It is not
Layer 1 profiling and not Layer 2 optimization.

## Dependency direction (one-way)
```
common  →  intake / callers / protocol (Stage 0, production)
                     ↑
   tools (gatk, happy adapters)  ─────────────┐
                     ↑                          │
   twin (contracts, execution_plan, comparison, scoring, parity, service)
                     ↑
   twin.offline (truth loaders — OFFLINE ONLY)
                     ↑
   qualification.twin_runner / cli.twin_commands
```
- Production packages (`protocol`, `callers`, `layer1`, `layer2`, `intake`,
  `manifests`, `common`) **must not import** `twin`, `tools`, or `twin.offline`.
  Enforced by `qualification.twin_checks.architecture_boundaries_ok` and a
  leakage test.
- `twin`/`tools` **must not import** any network library (enforced by
  `no_hidden_network_dependency`).

## Workflow (service)
```
PROTOCOL-READY verification (require_gate_pass on Stage 0 gate)
  → CONFIG canonicalization + GATK execution-plan construction (no execution)
  → comparison-result ingestion (fixture replay via injected adapters)
  → scoring inputs (authoritative) + score (typed UNAVAILABLE)
  → parity assessment
  → immutable TwinRunManifest
```
`TwinService` requires a valid Stage 0 PROTOCOL-READY gate, injects tool
adapters (disabled by default; deterministic fakes in tests), performs **no
network access**, and returns identical content hashes for identical semantic
inputs (operational timestamps excluded).

## Modules
| Module | Responsibility |
|---|---|
| `twin/contracts.py` | frozen contracts + parity levels + content identity |
| `twin/identities.py` | `ToolIdentity`, `content_hash` (reuses Stage 0 hashing) |
| `twin/unavailable.py` | typed unavailable results + reason codes |
| `twin/execution_plan.py` | side-effect-free GATK plan builder |
| `twin/comparison.py` | normalize hap.py metrics; recompute precision/recall/F1 |
| `twin/scoring.py` | score inputs (authoritative); composite score UNAVAILABLE |
| `twin/parity.py` | deterministic expectation-vs-observation diff |
| `twin/service.py` | orchestration + run manifest |
| `twin/fixtures.py` | replay fixture loader (non-truth) |
| `twin/offline/truth_loader.py` | OFFLINE truth-fixture identity loader |
| `tools/gatk.py` | argv builder (list, never shell) + runner port + fakes |
| `tools/happy.py` | raw-result parser + runner port + fakes |
| `qualification/twin_runner.py` | TWIN-READY gate (git-tree-bound) |
| `cli/twin_commands.py` | `twin plan/replay/parity/qualify` |
