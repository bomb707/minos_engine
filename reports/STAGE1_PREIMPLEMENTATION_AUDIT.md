# STAGE 1 — Validator Twin: Pre-Implementation Audit

**Engine:** MINOS_ENGINE (Minos Bittensor Subnet 107)
**Scope:** Stage 1 — Validator Twin *foundation and parity* only. NOT Layer 1, NOT Layer 2.
**Date:** 2026-08-17
**Author agent:** Claude (Opus 4.8)

## Baseline (accepted Stage 0)
- HEAD / Stage 0 artifact commit: `8340c3aa34eaa56dabedbfdd6e90d486d855e628`
- Qualified Stage 0 source commit: `4a5a14d47f198a214e4dff8da2c4466e5fc7a425`
- Qualified source tree: `f248480a5dd43c277e493155a31a9a289a2ce31e`
- PROTOCOL-READY gate hash: `b9cda0bab329b36a0a62b4b7e9ba9b797fc22b46c1055f76db26b591311a1675`
- Stage 0: 215 passed, coverage 94.26%. (Runtime standardized to Python 3.12 only
  during Stage 1 remediation; the engine is tested/qualified on CPython 3.12.x.)
- Specs verified against `docs/specifications/SPECIFICATION_MANIFEST.json` (all three sha256 match). Worktree clean at start.

Stage 0 contracts are **not** changed or reinterpreted by this stage.

## Authoritative material read
- Overall spec **§2** (formal system contract: LIVE/OFFLINE/DELAYED/FORBID), **§1** (evaluation: "execute miner CONFIG, generate VCF, compare with private truth through hap.py, and compute AdvancedScorer"), **§7** (Validator Twin exact workflow), stage chain (S1 → `twin-ready.json`).
- Layer 1 spec (read only to keep interfaces forward-compatible; not implemented).
- Layer 2 spec **§6** (25-param registry — already in Stage 0), **§12/§17** (references the "official scorer objective" but does not define AdvancedScorer weights).
- All Stage 0 source, schemas, tests, ADRs, gate, reports.

### Overall spec §7 (controlling text, paraphrased)
> Reproduce the official evaluation path so every experiment has a trustworthy label. Inputs: BAM/BAI, reference, exact region, canonical GATK CONFIG, pinned images, practice truth and confident regions. Build the GATK command from the same official template; execute in a resource-capped container; validate/index VCF; normalize exactly as the official path; execute hap.py; parse TP/FP/FN and SNP/INDEL metrics; call the **pinned AdvancedScorer**; record requested and effective CONFIG plus all digests. Repeat golden cases and compare byte-level or declared-semantic outputs. Artifacts: TwinRun, VCF artifact, hap.py artifacts, score components, final score, runtime, failure class and manifest. STOP/PROMOTION: do not start Layer 1 qualification or HPO until Twin parity and reproducibility pass.

## Critical determination — AdvancedScorer is UNAVAILABLE in-repo
The spec **references** a "pinned AdvancedScorer" and its component structure (core/completeness/FP/quality with an overcall penalty is alluded to in Layer 2 §12) but **does not define the exact formula, weights, chromosome weighting, clipping, or normalization** in any of the three authoritative specifications or in this repository. Per the assignment's explicit instruction ("If the exact current Minos AdvancedScorer formula … is unavailable, do not invent it. Return a typed unavailable result"), the composite/final Minos score is modeled as:

```
status: UNAVAILABLE
reason_code: AUTHORITATIVE_SCORER_NOT_AVAILABLE
```

What **is** authoritative and therefore implemented numerically: standard hap.py-style comparison metrics — `precision = TP/(TP+FP)`, `recall = TP/(TP+FN)`, `F1 = 2·P·R/(P+R)` — which are standard information-retrieval / hap.py definitions with deterministic zero-denominator handling. These are recomputed and consistency-checked against supplied values, but they are **inputs to** the scorer, not the scorer.

**Consequence for parity:** Stage 1 can honestly claim **FIXTURE_REPLAY** parity (structural pipeline + deterministic fixture replay + comparison-metric recomputation). It **cannot** claim `VALIDATOR_CONFIRMED` numerical parity, and it does not run real GATK/hap.py (so not `TOOL_EXECUTION`). This is a designed, spec-authorized outcome, not a defect — recorded here and surfaced to review rather than silently approximated.

## Parity levels (Stage 1 vocabulary)
The specs define gate states (PASS/HOLD/PATCH/REJECT) and "byte-level or declared-semantic" output comparison, but no named parity ladder. We introduce a non-conflicting ladder (assignment §3 suggested names):

| Level | Meaning | Stage 1 status |
|---|---|---|
| `STRUCTURAL` | contracts/plan/identity reproduced deterministically | achieved |
| `FIXTURE_REPLAY` | + deterministic replay of comparison results and recomputed metrics via injected fake adapters | **achieved (declared)** |
| `TOOL_EXECUTION` | + real GATK + hap.py executed in a resource-capped container | not achieved (out of Stage 1 scope) |
| `VALIDATOR_CONFIRMED` | + pinned AdvancedScorer numerical parity confirmed against the live validator | not achieved (scorer unavailable) |

A lower level is never reported as a higher level; `TwinParityReport.declared_level` is enforced by tests.

## Architecture placement decisions (existing-architecture-mandated deviations)
- The assignment's suggested `src/minos_engine/gates/twin_ready.py` and `cli/twin_commands.py` are honored for the CLI; the **qualification orchestration** for TWIN-READY is placed in `src/minos_engine/qualification/twin_runner.py` to match the Stage-0 architecture (all gate qualification lives in `qualification/`, reusing `gather_source_integrity`, `run_pytest`, `run_coverage`, git-tree-bound evidence). Documented deviation; responsibilities preserved.
- Twin source lives under `src/minos_engine/twin/` and `src/minos_engine/tools/`; both are already covered by the Stage-0 evidence directory `src/minos_engine`, so evidence hashing needs no new directory — only new `REQUIRED_TRACKED_FILES` entries.

## Conformance matrix

| Requirement ID | Specification source | Required behavior | Planned implementation | Test evidence | Status |
|---|---|---|---|---|---|
| S1-R01 | Overall §7 | Reproduce evaluation path deterministically & auditably (Twin) | `twin/service.py` orchestration + `TwinRunManifest` | integration/twin, determinism/twin | authoritative |
| S1-R02 | Overall §2 LIVE; Stage 0 | Represent round protocol state | reuse `protocol.RoundProtocolSnapshot`; `twin` consumes snapshot hash | component/twin | authoritative |
| S1-R03 | Overall §7 | Miner GATK CONFIG validation | reuse Stage 0 `callers.gatk.config.canonicalize_config` | unit/twin (plan) | authoritative |
| S1-R04 | Overall §7 | Build GATK command from official template (plan only) | `twin/execution_plan.py` + `tools/gatk.py` (tokenized argv, no exec) | unit/twin plan (group C) | authoritative (structure) |
| S1-R05 | Overall §7 | Execution request construction w/ inputs/outputs/deadline | `TwinExecutionRequest`, `GatkExecutionPlan` | contracts (group A) | authoritative |
| S1-R06 | Overall §7 | hap.py comparison request + parse TP/FP/FN & SNP/INDEL | `twin/comparison.py` + `tools/happy.py` (parse+replay) | comparison (group D) | authoritative (parse) |
| S1-R07 | Overall §1/§7 | Comparison metrics normalization (precision/recall/F1, TiTv/HetHom when present) | `twin/comparison.py` recompute + consistency | group D | authoritative (standard defs) |
| S1-R08 | Overall §7 | Minos scoring input construction | `twin/scoring.py` `ScoreInputs` | group E | authoritative |
| S1-R09 | Overall §7 | Component-score & final-score when formula known | `twin/scoring.py` | group E | **unavailable** (AdvancedScorer not in specs) |
| S1-R10 | Assignment §9; Overall §7 | Typed unavailable when scorer unknown | `twin/unavailable.py`, `TwinScoreResult(status=UNAVAILABLE, reason_code=AUTHORITATIVE_SCORER_NOT_AVAILABLE)` | group E | authoritative (unavailable path) |
| S1-R11 | Overall §3; Stage 0 | Complete provenance & content identity | `twin/identities.py`, `TwinRunManifest`, canonical hashing (Stage 0) | group B | authoritative |
| S1-R12 | Overall §7 | Reproducible parity-test artifacts | `twin/parity.py` + fixtures + golden hashes | group F | authoritative |
| S1-R13 | Overall §2 FORBID | Truth isolation across production/L1/L2 | architecture + leakage tests; truth loaders in offline twin namespace only | group G | authoritative |
| S1-R14 | Assignment §7 | GATK-only; no shell string; no exec on plan build | `tools/gatk.py` argv list; policy check | group C | authoritative |
| S1-R15 | Assignment §11 | Service requires valid PROTOCOL-READY gate | `twin/service.py` calls `require_gate_pass` on Stage 0 gate | integration (group H) | authoritative |
| S1-R16 | Assignment §13 | Twin CLI (plan/replay/parity/qualify), `--json`, no live implication | `cli/twin_commands.py` | CLI (group H) | authoritative |
| S1-R17 | Assignment §14 | TWIN-READY gate w/ required-check registry, integrity vs promotion | `gates/required_checks` TWIN-READY set + `qualification/twin_runner.py` | qualification (group I) | authoritative |
| S1-R18 | Assignment §15 | Two-commit git-tree-bound qualification; records prerequisite gate hash + parity level | `qualification/twin_runner.py` | group I, group J | authoritative |
| S1-R19 | Assignment §6 | One canonical JSON/sha256; golden vectors; timestamp excluded | reuse Stage 0 `common.canonical_json`/`hashing`; golden tests | group B | authoritative |
| S1-R20 | Assignment §17 | CI Python 3.12 only, full history, clean checkout, no network/GATK/hap.py deps | `.github/workflows/ci.yml` | group J | authoritative |
| S1-R21 | Assignment §12 | Layer 1 unimplemented, Layer 2 blocked | unchanged Stage 0 services; TWIN-READY checks assert both | group G/I | authoritative |

## Explicit exclusions (not implemented in Stage 1)
Layer 1 BAM profiling, pysam, BAM sampling; Layer 2 candidate generation, Optuna, Bayesian optimization, CatBoost, ranking/retrieval models; PostgreSQL; production submission; live mining; hidden/locked-test data; invented scorer formula; invented owner/API facts; DeepVariant/BCFtools/FreeBayes; real GATK or hap.py execution (only plan construction, parsing, and fixture replay).

## Risks & unresolved protocol questions
1. **AdvancedScorer formula unknown in-repo** → composite score typed-unavailable; declared parity capped at FIXTURE_REPLAY. Resolution needs the owner/spec to provide the pinned scorer (a later stage).
2. **hap.py exact normalization** (left-align, decompose, RTG vcfeval semantics) is not fully specified → the parser handles a documented normalized fixture shape and records `raw_result_hash`; it does not claim to reproduce hap.py's own normalization. Marked authoritative for *parsing*, unavailable for *tool execution*.
3. **GATK image/version pinning** comes from the protocol snapshot (`gatk_image_digest`); when absent the plan records tool identity as unavailable and fails closed where a real run would require it.
4. **Commit-reveal / live endpoints** remain typed-unavailable (unchanged from Stage 0).

## Implementation plan (two commits)
- **Commit A** (qualified source): twin/tools modules, 4 schemas, `configs/twin/default.yaml`, tests (groups A–J), fixtures, docs (`docs/twin/*`, runbook), `qualification/twin_runner.py`, TWIN-READY required-check registry, CI update, this audit.
- **Commit B** (artifacts): regenerate `gates/twin-ready.json` + `reports/STAGE1_QUALIFICATION_REPORT.md` from a clean Commit A.

Gate to proceed: this audit + matrix complete ✅. Implementation begins at the Twin contracts.
