# MINOS_ENGINE

A protocol-faithful engine that produces GATK HaplotypeCaller CONFIGs for the
**Minos Bittensor Subnet 107**.

## Implementation stage

**Layer 1 — truth-free BAM/reference profiling** (on top of Stage 0 + Stage 1).
Layer 1 converts a BAM, its index, the exact region, and the matching reference
into a deterministic, immutable, **truth-free** `ProfileResult` plus three
artifacts (`bam-profile-v1.json`, `window-profile-v1.parquet`,
`profile-manifest-v1.json`). It never selects a GATK CONFIG, runs GATK/hap.py,
reads truth/hidden/winner data, optimizes, or imports Layer 2. Qualified through an
`L1-READY` gate (git-tree-bound, two-commit) with synthetic **and** real-BAM tiers.

Stage 0 established the protocol foundation; Stage 1 added the deterministic,
fixture-backed Validator Twin (`src/minos_engine/twin/`, declared parity
**FIXTURE_REPLAY**; composite score typed-unavailable — see
`docs/twin/SCORING_CONTRACT.md`).

### Layer 1 (truth-free profiling) — implemented
- Frozen contracts + 7 schemas (request, result, fingerprint, integration report,
  bam-profile, window-profile, manifest); typed unavailable measurements.
- One-pass, bounded-memory scan (Welford, fixed histograms, deterministic
  quantiles/MAD); one shared read-filter policy; difference-array coverage (two
  views, fragment-aware, overlap-corrected); reference-context profiler
  (GC/N/entropy/homopolymer/dinucleotide); bounded pileup + truth-free evidence
  proxies; cost model + deterministic adaptive window sampling; difficulty vector,
  confidence, completion; deterministic `ContextFingerprint`.
- Deadline/degradation state machine (soft 180 s, hard 300 s, pileup soft 90 s);
  atomic canonical-JSON + fixed-schema Parquet serialization.
- `pysam` is the single I/O boundary (opens only explicit paths). CLI:
  `layer1 validate/profile/qualify-real/qualify/gate` + public `profile`.
- `L1-READY` gate: 34 mandatory checks incl. both accepted prerequisites
  (PROTOCOL-READY, TWIN-READY), real-BAM qualification, determinism, truth
  isolation, hard-limit, and identity binding. Real-BAM run: chr19 ~9.9 Mbp,
  1.57M reads, ~105 s, ~630 MB, identical fingerprints across runs.

### Stage 1 (Validator Twin) — implemented
- Frozen contracts + 4 schemas (execution request, comparison result, score
  result, parity report); parity levels ladder; typed-unavailable results.
- Side-effect-free GATK **execution-plan** builder (tokenized argv, never a shell
  string; runner ports + deterministic fakes; **no execution**).
- hap.py comparison **parser** + normalized metrics (precision/recall/F1
  recomputed, deterministic zero-denominator); scoring **inputs** authoritative,
  composite score UNAVAILABLE (no invented fallback).
- `TwinService` (requires the Stage 0 PROTOCOL-READY gate; no network); parity
  assessment; immutable run manifest; offline truth-loader namespace.
- CLI: `twin plan / replay / parity / qualify`. Truth isolation enforced
  (production never imports the Twin; sentinel non-leak test).
- `TWIN-READY` gate (git-tree-bound, two-commit) with a required-check registry.

### Stage 0 (foundation) — implemented
- Canonical JSON + SHA-256 identity; monotonic `Deadline` time budgets.
- Immutable contracts: `RoundProtocolSnapshot`, `RoundContext`,
  `ArtifactIdentity`, `Region`, `ParameterSpaceSnapshot`, `GateArtifact`,
  `ReleaseManifest` (required identities fail closed).
- Fixture-backed `ProtocolClient` + snapshot builder + staleness; the live
  client fails closed (`UnavailableError`).
- Canonical **25-parameter** GATK registry (documented ranges) + CONFIG
  validation/canonicalization with a runtime parameter-space override seam.
- GATK-only caller policy; submission envelope (generation separate from
  submission, no side effects).
- CLI: `doctor`, `protocol snapshot`, `config validate`, `manifest build`,
  `gate verify-integrity` / `gate require-pass` (integrity vs promotion),
  `qualify` (human + `--json`).
- Qualification engine (`src/minos_engine/qualification/`): JUnit-based test
  accounting, Cobertura coverage enforcement (≥90%), deterministic evidence
  hashing, required-check registry, and two-commit source provenance.
- GitHub Actions CI on **Python 3.12** (the only supported runtime); authoritative
  specs in `docs/specifications/`.
- Full test suite incl. architecture import-boundary and truth-isolation guards.

### What is intentionally NOT implemented
- Layer 2 candidate generation / optimization / ML (`Layer2Service.select_config`
  is **blocked until L1-READY** verifies through the entry gate).
- PostgreSQL, Optuna, CatBoost, hap.py, and GATK **execution**.
- The composite Minos AdvancedScorer (Layer 1 is descriptive only; no TP/FP/FN,
  scores, predicted reward, or GATK parameters).
- Live Minos API integration (fixture-backed only in Stage 0).
- Any access to truth / locked-test data (see ADR-0003).

## Supported runtime
**CPython 3.12.x** is the only supported, tested, and qualified runtime (it
matches the Minos subnet). Other Python versions are rejected by the runtime
preflight (`minos-engine doctor` reports the policy; `require_supported_runtime`
fails closed). No Python 3.11 support is claimed.

## Setup
```bash
# Requires Python 3.12.
make bootstrap          # python3 -m venv .venv && pip install -e ".[dev]"
# or manually:
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"
```

## Test & quality commands
```bash
make lint         # ruff check .
make fmt-check    # ruff format --check .
make typecheck    # mypy src
make test         # pytest
make cov          # pytest --cov=src/minos_engine --cov-report=term-missing
make stage0       # lint + fmt-check + typecheck + cov
```

## CLI examples
```bash
minos-engine doctor
minos-engine doctor --json
minos-engine protocol snapshot --fixture tests/fixtures/api/valid_round.json
minos-engine config validate \
  --config tests/fixtures/gatk/default_config.json \
  --parameter-space tests/fixtures/api/gatk_parameter_space.json
minos-engine manifest build --fixture tests/fixtures/api/valid_round.json
minos-engine gate verify-integrity --gate gates/protocol-ready.json --base-dir .
minos-engine gate require-pass --gate gates/protocol-ready.json --base-dir .
minos-engine qualify   # runs the Stage 0 qualification and writes gate + report
# Validator Twin (Stage 1) — fixture-backed, not a live validator:
minos-engine twin plan   --request tests/fixtures/twin/replay/valid.json
minos-engine twin replay --fixture tests/fixtures/twin/replay/valid.json --json
minos-engine twin parity --expected tests/fixtures/twin/parity/expectation.json \
                         --observed tests/fixtures/twin/parity/observation_match.json
minos-engine twin qualify   # runs the TWIN-READY qualification
# Layer 1 (truth-free profiling) — reads only explicit paths, never truth data:
minos-engine layer1 validate --bam input.bam --reference chr19.fa \
  --region chr19:36800001-46700000
minos-engine profile --bam input.bam --bai input.bam.bai \
  --reference chr19.fa --fai chr19.fa.fai \
  --region chr19:36800001-46700000 --output-dir out/profile
minos-engine layer1 gate require-pass --gate gates/l1-ready.json --base-dir .
```

## Stage-gate status
| Gate | Status |
|---|---|
| `protocol-ready.json` (S0) | PASS (accepted; `b9cda0ba…`) |
| `twin-ready.json` (S1) | PASS (accepted; `3464fb76…`, FIXTURE_REPLAY) |
| `l1-ready.json` (Layer 1) | generated by `minos-engine layer1 qualify` (synthetic + real-BAM) |

## Current stage
**L2-F2 — baseline discovery + baseline qualification.**

Layer 2 is well past its entry gate. The PostgreSQL evidence foundation (L2-B), the frozen
50/10/15 split (L2-C), profile ingestion (L2-D), the feature infrastructure (L2-E) and the
offline GATK experiment harness (L2-F1) are all accepted, and **`HARNESS-READY` has been
issued** — `gates/harness-ready.json`, PASS with 40/40 mandatory checks, from a real GATK
4.5.0.0 qualification.

`docs/DEVELOPMENT_STATUS.md` is the single source of truth for current stage, accepted gates
and what is explicitly not started. `docs/layer2/BASELINE_QUALIFICATION.md` holds the proposed
L2-F2 contract. `Layer2Service.select_config` remains deliberately blocked by
`StageNotReadyError` until L2-H.

## Documentation
- `docs/architecture/OVERVIEW.md`, `docs/architecture/DEPENDENCY_RULES.md`
- `docs/contracts/PROTOCOL_CONTRACTS.md`, `docs/contracts/GATK_CONFIG_CONTRACT.md`
- `docs/runbooks/PROTOCOL_SNAPSHOT.md`
- `docs/twin/` (ARCHITECTURE, PARITY_LEVELS, SCORING_CONTRACT, TOOL_ADAPTERS,
  FIXTURE_PROVENANCE, LIMITATIONS) + `docs/runbooks/VALIDATOR_TWIN_REPLAY.md`
- `docs/layer1/` (ARCHITECTURE, INPUT_CONTRACT, FEATURE_CATALOG,
  SAMPLING_AND_WINDOWS, FILTERING_POLICY, REFERENCE_PROFILING, DETERMINISM,
  PERFORMANCE, TRUTH_ISOLATION, REAL_BAM_QUALIFICATION, LIMITATIONS) +
  `docs/runbooks/LAYER1_PROFILE.md`, `docs/runbooks/LAYER1_REAL_BAM_QUALIFICATION.md`
- `docs/qualification/QUALIFICATION.md` (two-commit provenance, evidence hashing,
  required checks, integrity vs promotion)
- `docs/ci/CI_AND_BRANCH_PROTECTION.md`
- `docs/specifications/` (authoritative build specs + `SPECIFICATION_MANIFEST.json`)
- `docs/decisions/ADR-0001..0004`
- `reports/STAGE0_PREIMPLEMENTATION_AUDIT.md`, `reports/STAGE0_QUALIFICATION_REPORT.md`
