# MINOS_ENGINE — Architecture Overview (Stage 0)

MINOS_ENGINE produces a protocol-faithful GATK HaplotypeCaller CONFIG for Minos
Bittensor Subnet 107. Stage 0 builds the **protocol foundation**: contracts,
canonical identity, the GATK parameter registry + CONFIG validation, a
fixture-backed protocol client, a release manifest, stage gates, and a CLI.
Layer 1 is not implemented; Layer 2 is blocked until L1-READY.

## The three data flows (Overall spec §2)
```
LIVE:    RoundContext + BAM/BAI/FASTA -> Layer1Profile -> Layer2Decision -> CONFIG -> submit
OFFLINE: frozen decision + GATK VCF + truth -> hap.py -> AdvancedScorer -> ExperimentResult
FORBID:  truth / evaluator output -> Layer 1 or live Layer 2 feature path
```
Stage 0 realizes the *left half* of the LIVE path up to (but not including)
profiling and decision-making, plus the identity/provenance spine.

## Packages (`src/minos_engine/`)
| Package | Responsibility (Stage 0) |
|---|---|
| `common` | canonical JSON, sha256 identity, monotonic `Deadline`, timestamps, errors, version identities |
| `intake` | `Region` (+ exact coordinate conversion), `ArtifactIdentity`, reference registry |
| `protocol` | `RoundProtocolSnapshot`/`RoundContext`, fixture+live clients, snapshot builder, staleness, parameter-range/network parsing, submission envelope, upstream provenance |
| `callers/gatk` | 25-parameter registry, CONFIG canonicalizer/validator, CLI-flag metadata (no execution) |
| `layer1` | stable API; `analyze` raises `StageNotReadyError` |
| `layer2` | stable API; `select_config` blocked; L1-READY entry gate |
| `gates` | `GateArtifact` contract + verifier/writer |
| `manifests` | `ReleaseManifest` + builder |
| `cli` | thin composition roots (doctor, protocol snapshot, config validate, manifest build, gate verify) |
| `settings`, `schema_registry` | single typed settings layer; JSON-Schema loading/validation |

## Identity model
Every hash is `sha256(canonical_json_bytes(x))`. Canonical JSON = sorted keys,
compact separators, UTF-8, finite numbers only, deterministic floats, no
timestamps generated at serialization. Snapshot / parameter-space / gate /
manifest identities are computed from canonical content; timestamps are excluded
from gate and manifest content hashes so re-stamping does not change identity.

## Timing
A round is 72 minutes; prediction targets ~5 minutes with a final safety
reserve. `common.clock.Deadline` provides `remaining_seconds`, `expired`,
`require_remaining`, and `child_budget` over a monotonic clock (a `FakeClock` is
used in tests). See `configs/engine/default.yaml`.

## What Stage 0 deliberately excludes
Layer 1 BAM profiling; Layer 2 candidate generation/optimization/ML;
PostgreSQL; Optuna/CatBoost/pysam/hap.py/GATK execution; live API integration
(the live client fails closed). See the README "intentionally not implemented".
