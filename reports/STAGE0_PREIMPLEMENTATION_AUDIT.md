# STAGE 0 — Pre-Implementation Audit

**Engine:** MINOS_ENGINE (Minos Bittensor Subnet 107)
**Scope of this document:** Stage 0 only — Basic architecture and protocol foundation.
**Date:** 2026-08-17
**Author agent:** Claude (Opus 4.8)
**Authoritative inputs read in full before writing this audit:**

- `MINOS_ENGINE_Overall_Exact_Build_Specification_v2.docx`
- `MINOS_ENGINE_Layer1_Exact_Build_Specification_v2.docx`
- `MINOS_ENGINE_Layer2_Exact_Build_Specification_v2.docx`
- Read-only inspection of the reference subnet at `../minos_subnet` (never modified).

> This audit is the mandatory gate. No engine implementation was written before it.

---

## 1. Current repository state

### 1.1 `minos_engine/` (the target repository)

```
minos_engine/
└── .git/            # fresh `git init`, branch `main`, NO commits, NO tracked files
```

- `git status`: *"No commits yet / nothing to commit"*.
- `git log`: fatal — no commits.
- `git branch -a`: empty (unborn `main`).
- **There is no existing source, no CLI, no config, no schema, no tests, no Docker, no CI, and no documentation.** The repository is a clean slate.
- A local `.venv/` was created by this session for tooling; it is untracked and will be git-ignored (never committed).

**Consequence:** There is *no existing engine code to retain, deprecate, or isolate inside `minos_engine/`.* Every Stage-0 file is net-new. The "retain / isolate / deprecate" analysis below therefore concerns the *reference* subnet only, which is a separate repository we do not modify.

### 1.2 `../minos_subnet/` (read-only reference — DO NOT CHANGE)

This is the live Subnet-107 miner/validator codebase. It is **not** imported or vendored by Stage 0. It is used only as the factual source for protocol/CONFIG/scoring contracts. Key facts extracted (paths relative to `minos_subnet/`):

| Concern | Location | Fact |
|---|---|---|
| Transport | `utils/platform_client.py` | Central HTTP API `api.theminos.ai`; **no Bittensor `Synapse`/`protocol.py`**. Miners *pull* round status; submit a tool CONFIG (JSON), not a VCF. |
| Miner submission | `neurons/miner.py:_get_tool_config` (≈672) | `{"tool":"gatk","version":"4.5.0.0","gatk_options":{...}}`. Infra keys `{threads,memory_gb,timeout,ref_build,num_threads}` stripped before submit. |
| Round status | `MinerPlatformClient.get_round_status` `/v2/round-status` | Returns `round_id, status(pending/open/scoring/completed), start_time, submission_end_time, scoring_end_time, region, bam_presigned_url, bam_index_presigned_url, num_mutations, downsampled_coverage, time_remaining_seconds`. |
| GATK params (25) | `templates/tool_params.py:GATK_QUALITY_PARAMS` (≈141–337) | Whitelist with type/min/max/enum/default and CLI flag. **Live ranges differ from spec §6** (see §6 below). |
| Region format | `templates/tool_params.py:REGION_PATTERN` | `^chr([1-9]\|1[0-9]\|2[0-2]\|X\|Y\|M):\d+-\d+$`, 1-based inclusive, ≤1e9 bp. |
| round_id format | `templates/tool_params.py:validate_round_id` | ISO-8601 with tzinfo, ≤40 chars (legacy `Z` tolerated). |
| Scoring | `utils/scoring.py` | `HappyScorer` (hap.py, vcfeval) + `AdvancedScorer.compute_advanced_score` → 0..100. Components core 0.60 / completeness 0.15 / fp 0.15 / quality 0.10, minus `overcall_penalty`. |
| hap.py image | `utils/scoring.py`, `base/genomics_config.py` | `genonet/hap-py@sha256:03acabe84bbfba35f5a7234129d524c563f5657e1f21150a2ea2797f8e6d05f2` (digest-pinned). |
| GATK image | `templates/gatk.py` (≈71) | `broadinstitute/gatk:4.5.0.0` (tag, not digest). |
| Reference | `datasets/reference/<chrom>/<chrom>.fa(.fai/.dict/.sdf)` | GRCh38; provisioned by `setup.py` from `api.theminos.ai/reference`. |
| Other callers | `templates/{deepvariant,bcftools,freebayes}.py` | deepvariant & bcftools active in subnet; freebayes deprecated. Engine policy: **GATK-only** (others neither executable nor selectable). |
| Commit-reveal | grep | Owner-reports commit-reveal is enabled with score visibility delayed ~2 epochs; the accessible protocol representation is **not yet verified through the integrated source**. Modeled as typed-unavailable (fail closed). |

### 1.3 Local auto-memory context

Session memory references prior Minos work (`per-round config optimization`, `Minos scorer failure signature`, `V2 gated migration` on a *different* repo). None of it lives in `minos_engine/`; it does not constrain Stage 0 and is not consumed here. No truth/locked-test data was read (see §11).

---

## 2. Existing execution / CLI / live-prediction / duplicate-engine paths

- **Existing execution paths in `minos_engine/`:** none.
- **Existing CLI paths:** none.
- **Existing live-prediction path:** none.
- **Duplicate engines or controllers:** none present. (The Overall spec §Architecture-correction forbids two CONFIG-emission engines; Stage 0 establishes a *single* canonical path from the start — see ADR-0001.)
- **Existing protocol integrations:** none in-repo. The reference subnet's `PlatformClient` is the external contract we model behind our own `ProtocolClient` interface; we do not import it.
- **Existing Layer 1 / Layer 2 code:** none.
- **Existing database/storage code:** none. (No PostgreSQL dependency is added in Stage 0, per assignment §16.)
- **Existing tests and their status:** none.

---

## 3. Architectural conflicts with the three specifications

Because the target repo is empty, there are **no code-level conflicts**. The only reconciliations required are between the specs and the *observed live subnet*, and among the specs themselves:

1. **Parameter ranges: spec §6 (documented) vs live subnet (runtime).** Several parameters differ (§6 table below). The Layer 2 spec's **DYNAMIC RANGE RULE** resolves this: the static registry encodes the documented 2026-08-09 ranges; a versioned runtime *ParameterSpaceSnapshot* may override them, creating a new compatibility domain. Stage 0 implements both layers and the override seam. **We do not silently adopt live ranges into the static registry.**
2. **Commit-reveal.** Overall spec §1 says "commit-reveal enabled" (score visibility delayed ~2 epochs, owner-reported). The precise accessible protocol representation is not yet verified through the integrated protocol source. Stage 0 models `commit_reveal_state` as an explicit typed value that is `available:false` (typed-unavailable) until the authoritative runtime source, fields, and timing semantics are confirmed; a *required* identity that is unknown fails closed. We do **not** fabricate the enabled state, phase, block/epoch timing, or reveal timestamp.
3. **Repository tree: assignment §3 vs Overall spec §5.** The Overall spec's tree includes later-stage packages (`twin/`, `storage/`, `experiments/`, `models/`, `feedback/`, `observability/`). The assignment §3 gives the *Stage-0* subset. Stage 0 follows the assignment §3 tree and leaves later-stage packages unbuilt. Documented in ADR-0001.
4. **CONFIG envelope.** Live CONFIG is `{tool,version,gatk_options}`. The specs' canonicalization operates on the GATK parameter mapping. Stage 0 canonicalizes the GATK parameter mapping and models the submission envelope (`{tool:"gatk",version,gatk_options}`, infra keys stripped) in `protocol/submission_contract.py`. CONFIG generation and submission are kept as separate operations (assignment rule 10).

No conflict requires deleting or overwriting anything (nothing exists to delete).

---

## 4. Code retained / isolated / deprecated

- **Retained:** nothing (empty repo). The reference subnet remains untouched and un-vendored.
- **Isolated/deprecated:** N/A for Stage 0. DeepVariant/BCFtools/FreeBayes are *out of scope by policy*: the engine ships GATK-only; no adapter for them is created, so there is nothing to isolate. Historical data policy (keep, don't select) is documented in ADR-0002 for future stages.

---

## 5. Exact Stage 0 implementation plan

Package root: **`src/minos_engine/`** (src-layout; single canonical package — ADR-0001). Modules follow assignment §3.

**Commit 1 `stage0/audit`** — this file.

**Commit 2 `stage0/package-and-contracts`**
- `pyproject.toml` (py3.11+, pydantic2, ruff, mypy, pytest, pytest-cov, pyyaml, httpx, jsonschema), `Makefile`, `.gitignore`, `alembic.ini` (placeholder, no runtime DB dep).
- `src/minos_engine/common/`: `errors.py`, `canonical_json.py`, `hashing.py`, `clock.py` (monotonic deadline), `versions.py`.
- Foundational contracts: `RoundProtocolSnapshot`, `RoundContext`, `ArtifactIdentity`, `ParameterSpaceSnapshot`, gate artifact — as frozen pydantic v2 models.
- `schemas/*.schema.json` (6 schemas).
- `configs/{engine,layer1,layer2,runtime}/*.yaml`.

**Commit 3 `stage0/protocol-foundation`**
- `protocol/`: `contracts.py`, `client.py` (interface + `FixtureProtocolClient`; live client returns typed `UnavailableError`), `snapshot.py`, `state_sync.py` (staleness), `parameter_ranges.py`, `network_config.py`, `submission_contract.py`, `upstream_adapter.py`.
- `intake/`: `contracts.py`, `artifact_identity.py`, `reference_registry.py`.
- Deadline/time-budget already in `common/clock.py`; wired here.

**Commit 4 `stage0/gatk-registry-and-config-validation`**
- `callers/contracts.py`, `callers/gatk/config.py`, `callers/gatk/parameter_registry.py` (25 params), `callers/gatk/command.py` (flag mapping metadata only — no execution).
- CONFIG canonicalizer/validator with requested vs effective CONFIG and `config_hash`.

**Commit 5 `stage0/cli-and-manifests`**
- `manifests/release.py`, `manifests/builder.py`.
- `layer1/{contracts,service}.py`, `layer2/{contracts,entry_gate,service}.py` (StageNotReady / blocked).
- `gates/{contracts,verifier}.py`.
- `cli/{main,snapshot,doctor}.py` + `config validate`, `manifest build`, `gate verify`.

**Commit 6 `stage0/tests-and-architecture-guards`**
- `tests/{unit,component,protocol_contract,integration,leakage,acceptance}` + `tests/fixtures/api`, `tests/fixtures/gatk`.
- Architecture import-boundary guards; determinism; failure tests.
- Run ruff/mypy/pytest/coverage.

**Commit 7 `stage0/docs-and-protocol-ready-gate`**
- `docs/architecture/{OVERVIEW,DEPENDENCY_RULES}.md`, `docs/contracts/{PROTOCOL_CONTRACTS,GATK_CONFIG_CONTRACT}.md`, `docs/runbooks/PROTOCOL_SNAPSHOT.md`, `docs/decisions/ADR-000{1..4}-*.md`.
- `README.md`, `reports/STAGE0_QUALIFICATION_REPORT.md`, `gates/protocol-ready.json`.

### 5.1 GATK static registry — documented ranges (spec §6, reviewed 2026-08-09)

| # | name | type | range/enum | default | group | state |
|---|------|------|-----------|---------|-------|-------|
| 1 | min_base_quality_score | int | 10..50 | 10 | evidence_filters | EXPERIMENTAL |
| 2 | min_mapping_quality_score | int | 0..60 | 20 | evidence_filters | EXPERIMENTAL |
| 3 | base_quality_score_threshold | int | 0..50 | 18 | evidence_filters | EXPERIMENTAL |
| 4 | standard_min_confidence_threshold_for_calling | float | 10..100 | 30 | evidence_filters | EXPERIMENTAL |
| 5 | emit_ref_confidence | enum | NONE\|GVCF\|BP_RESOLUTION | NONE | protocol | FIXED |
| 6 | pcr_indel_model | enum | NONE\|HOSTILE\|AGGRESSIVE\|CONSERVATIVE | CONSERVATIVE | library | EXPERIMENTAL |
| 7 | min_pruning | int | 2..10 | 2 | assembly_graph | EXPERIMENTAL |
| 8 | max_alternate_alleles | int | 1..20 | 6 | assembly_graph | EXPERIMENTAL |
| 9 | min_dangling_branch_length | int | 2..20 | 4 | assembly_graph | EXPERIMENTAL |
| 10 | recover_all_dangling_branches | bool | false\|true | false | assembly_graph | EXPERIMENTAL |
| 11 | max_num_haplotypes_in_population | int | 8..128 | 128 | assembly_graph | EXPERIMENTAL |
| 12 | adaptive_pruning_initial_error_rate | float | 0.0001..0.1 | 0.001 | assembly_graph | EXPERIMENTAL |
| 13 | pruning_lod_threshold | float | 0.5..10 | 2.302585 | assembly_graph | EXPERIMENTAL |
| 14 | active_probability_threshold | float | 0.001..0.05 | 0.002 | active_region | EXPERIMENTAL |
| 15 | min_assembly_region_size | int | 1..300 | 50 | active_region | EXPERIMENTAL |
| 16 | max_assembly_region_size | int | 100..700 | 300 | active_region | EXPERIMENTAL |
| 17 | assembly_region_padding | int | 0..500 | 100 | active_region | EXPERIMENTAL |
| 18 | pair_hmm_gap_continuation_penalty | int | 1..30 | 10 | likelihood | EXPERIMENTAL |
| 19 | phred_scaled_global_read_mismapping_rate | int | 10..60 | 45 | likelihood | EXPERIMENTAL |
| 20 | heterozygosity | float | 0.0001..0.01 | 0.001 | prior | EXPERIMENTAL |
| 21 | indel_heterozygosity | float | 0.00001..0.001 | 0.000125 | prior | EXPERIMENTAL |
| 22 | sample_ploidy | int | 1..10 | 2 | protocol | FIXED |
| 23 | contamination_fraction_to_filter | float | 0..0.5 | 0 | evidence | EXPERIMENTAL |
| 24 | max_reads_per_alignment_start | int | 25..300 | 50 | read_retention | EXPERIMENTAL |
| 25 | dont_use_soft_clipped_bases | bool | false\|true | false | read_retention | EXPERIMENTAL |

Cross-parameter rule: `min_assembly_region_size < max_assembly_region_size`.
Initial states: protocol-determined (`emit_ref_confidence`, `sample_ploidy`) = `FIXED`; all others = `EXPERIMENTAL` (no live `ACTIVE` from legal range alone).

### 5.2 Documented-vs-runtime range drift (evidence for the override seam)

| param | documented (spec §6) | live subnet runtime |
|---|---|---|
| min_base_quality_score | 10..50 | 0..50 |
| min_pruning | 2..10 | 1..10 |
| min_dangling_branch_length | 2..20 | 1..20 |
| max_num_haplotypes_in_population | 8..128 | 8..512 |
| active_probability_threshold | 0.001..0.05 | 0.0001..0.05 |
| max_assembly_region_size | 100..700 | 100..1000 |
| max_reads_per_alignment_start | 25..300 | 0..1000 |

This drift is *why* the runtime ParameterSpaceSnapshot must be fetched, versioned, hashed, and allowed to override the static documented snapshot.

---

## 6. Files expected to change

All net-new (empty repo). No existing file is modified or deleted. Full manifest is in §5. Nothing outside `minos_engine/` is touched; `../minos_subnet` is read-only reference.

---

## 7. Risks and open questions

1. **Live protocol endpoints/schemas are not fully specified.** The live `ProtocolClient` cannot be completed without the real API schema. **Mitigation:** ship the interface + a deterministic `FixtureProtocolClient`; the live client raises a typed `UnavailableError`; tests use saved fixtures only (assignment §9).
2. **Commit-reveal semantics unknown.** Modeled as typed-optional; unknown *required* identity fails closed (§3.2).
3. **Range drift** between documented and runtime (§5.2). Handled by the override seam; a changed range → new compatibility domain, never silent mutation.
4. **hap.py/GATK/scorer digests** are known from the reference but must be treated as *runtime snapshot data*, not hard-coded truth. Stage 0 stores them in the snapshot/manifest as versioned identities; `doctor` reports their availability status.
5. **Float canonicalization** must be deterministic. Decision: reject NaN/Inf; serialize floats via `repr()`-stable shortest round-trip (Python `float.__repr__`), integers preserved as ints, bools preserved. Documented in the GATK CONFIG contract.
6. **No truth data** is available or needed for Stage 0; the truth-isolation boundary is established structurally (leakage tests) even though the Validator Twin is a later stage.

---

## 8. Decision log for Stage 0 (implemented as ADRs)

- ADR-0001 Single canonical engine / `src/minos_engine` package root.
- ADR-0002 GATK-only policy.
- ADR-0003 Truth isolation boundary.
- ADR-0004 Stage-gated development (L1-READY blocks Layer 2).

**Gate to proceed:** this audit written ✅. Implementation begins at commit `stage0/package-and-contracts`.
