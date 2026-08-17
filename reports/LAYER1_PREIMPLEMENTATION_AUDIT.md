# LAYER 1 — Truth-free BAM/reference profiling: Pre-Implementation Audit

**Engine:** MINOS_ENGINE (Minos Bittensor Subnet 107)
**Scope:** Layer 1 — deterministic, immutable, **truth-free** BAM + reference profiling only.
NOT Layer 2 (candidate generation / optimization / ML / PostgreSQL / controller).
**Date:** 2026-08-17
**Author agent:** Claude (Opus 4.8)
**Supported/qualified runtime:** CPython **3.12.x** only.

## Accepted baseline (externally accepted Stage 1)
- Baseline HEAD: `0469b3b8cb326ff32c0412ebec258871c4c39467`
- Accepted **TWIN-READY** gate hash: `3464fb7604fd6b18d9008bafa9e3cf1ad18d7d440d22806bbe043a11d3e9b22a`
  - qualified_source_git_sha `e9263ef78ce30c4d7c03497d906dd31a159f7156`
  - qualified_source_tree_sha `f84c06617a4adf79cdb8305dc698eaf97c1441ed`, status **PASS**
- Accepted **PROTOCOL-READY** prerequisite: `b9cda0bab329b36a0a62b4b7e9ba9b797fc22b46c1055f76db26b591311a1675`
  (source `4a5a14d…`, tree `f248480…`).

Stage 0 and Stage 1 contracts are **not** changed or reinterpreted. The accepted
gates are pinned and re-verified by re-hashing their committed source; this stage
never rebuilds them. Their frozen `layer1_not_implemented=true` remains a true
historical fact of those commits. Implementing Layer 1 legitimately advances the
engine past that stage marker (documented in §Discrepancies).

## Specifications read (hashes verified against SPECIFICATION_MANIFEST.json)
- Overall v2 `843e6c3a…` ✓ — §1 evaluation path, §2 LIVE/OFFLINE/FORBID, §7 twin, stage chain.
- Layer1 v2 `84c9056b…` ✓ — **authoritative for this stage** (§1–§20, Appendix A).
- Layer2 v2 `66bc10c1…` ✓ — read **only** to keep the Layer 1 output contract
  forward-compatible with future Layer 2 inputs; no Layer 2 behavior implemented.
Also read: architecture docs, contracts, ADR-0001..0004, Stage 0/Stage 1 reports,
`docs/qualification/QUALIFICATION.md`, all schemas, the Layer 1 stub, intake
contracts, the config system, the gate verifier, truth-isolation tests, the
TWIN-READY gate + qualifier, and the Layer 2 entry gate.

## Authoritative Layer 1 controlling text (paraphrased, Layer1 spec)
> **BOUNDARY.** Layer 1 reads only BAM, BAI, exact region, matching FASTA/FAI,
> budget, and operational metadata. It emits descriptive measurements. It never
> reads truth, scores, historical winners, or emits GATK parameters.
> **Artifacts:** `bam-profile-v1.json` (canonical JSON), `window-profile-v1.parquet`
> (fixed Arrow schema), `profile-manifest-v1.json` (written last, hashes the first
> two). Atomic writes (`.tmp`, fsync, validate, rename). **Orchestrator** (§4):
> resolve_region → validate_inputs → profile_header → one-pass stream_alignments →
> finalize_windows_coverage → profile_reference → cost_model → FULL or ADAPTIVE
> pileup → derive features → confidence/completion → crosscheck → atomic write.
> One shared monotonic Deadline; hard limit 300 s; soft 180 s; pileup soft 90 s.

## Exact required inputs (Layer1 §1)
`ProfileRequest = {round_id, bam_path, bai_path, reference_path, fai_path,
region_source, region_coordinate_convention, budget_seconds, expected_hashes?,
cpu_limit, memory_limit_bytes, profiler_config_version, profiler_config_hash}`.
`ProfileResult = {status: COMPLETE|PARTIAL|FAILED, profile_path, windows_path,
manifest_path, failure_code?, fallback_required, warnings[]}`.

## Exact output fields, types, units (classification A/I/U = AUTHORITATIVE / INFERRED / UNAVAILABLE)
`bam-profile-v1` top-level sections (§2): `schema_version, status, identity,
region, header, reads, coverage, mapping_quality, base_quality, pairing,
alignment, variant_evidence, reference_context, spatial, difficulty,
runtime_complexity, confidence, completion, stage_timings, warnings` — **A**.
`window-profile-v1` row (§2): `profile_id, window_id, contig, start0, end0,
length_bp, stratum, read_count, depth_*, mq_*, bq_*, duplicate_fraction,
clipping_*, nm_per_aligned_base, cigar_ins_del_burden, gc, entropy,
homopolymer_burden, candidate_snp_density, candidate_indel_density,
difficult_flags, sampled, selection_probability, analysis_weight` — **A**.
Units are explicit per field: **count** (int ≥0), **fraction** (∈[0,1]),
**rate/density** (per-base or per-bp), **Phred** (int), **base-pairs** (int).
Ambiguous names (`quality`, `coverage`, `ratio`, `score`, `value`) are banned.

## Exact metric definitions (Layer1 §9–§11) — AUTHORITATIVE
| Metric | Numerator / denominator or rule | Unit |
|---|---|---|
| `duplicate_fraction` | duplicate overlapping alignments / all mapped primary overlapping alignments | fraction |
| `proper_pair_fraction` | properly-paired valid primary reads / paired valid primary reads | fraction |
| `soft_clipped_read_fraction` | eligible reads with any `S` op / eligible reads | fraction |
| `soft_clipped_base_fraction` | Σ `S` bases / Σ query-consuming CIGAR bases | fraction |
| `nm_per_aligned_base` | Σ NM / Σ aligned query bases (reads with NM); + availability fraction | rate |
| `mq0_fraction` | eligible reads with MQ=0 / eligible reads | fraction |
| `bq_lt20_fraction` | query bases with BQ<20 / query bases with quality available | fraction |
| `insert_size_mad` | median(\|abs(TLEN)−median(abs(TLEN))\|), one mate per valid proper pair | bp |
| `overlapping_mate_fraction` | fragments whose aligned mates overlap / eligible paired fragments | fraction |
| `gc` (reference) | (G+C)/(A+C+G+T) over the interval sequence | fraction |
| `n_fraction` | non-ACGT / length | fraction |
| `entropy` | −Σ p_b·log2 p_b over b∈{A,C,G,T} | bits |
| coverage report | zero, <5, <10, <20, >50, >100, >200, max, mean, median, SD, CV, MAD, P01/05/10/25/75/90/95/99 | mixed |

## Coordinate conventions (§5) — AUTHORITATIVE
One unambiguous **zero-based half-open** `[start0, end0)`. Convert exactly once
from the declared API convention; preserve source string + convention. Require
`0 ≤ start0 < end0 ≤ BAM length` **and** `≤ FASTA length`, with **equal contig
lengths**. No silent ±1. Any ambiguity/mismatch is a **hard failure** (no partial).

## Window / sampling policy (§3, §15) — AUTHORITATIVE
`window.primary_bp=100000` (partition exact interval; last window may be shorter),
`window.refinement_bp=10000` (only full/selected primary windows). Windows are
deterministic from normalized region + BAM identity + config identity + algorithm
version. Adaptive sampling assigns each window to all applicable strata
(uniform, low/high coverage, low MQ/BQ, clipping, NM, CIGAR-INDEL, low entropy,
homopolymer, boundary), reserves deterministic per-stratum quotas, dedupes while
retaining reasons, tie-breaks by `H(BAM hash, region, profiler version, config
hash, window id)` (**never** process RNG), stores inclusion prob `πᵢ` and weight
`1/πᵢ`; weighted estimates `Σwᵢxᵢ/Σwᵢ`.

## Read-filter policy (§8) — AUTHORITATIVE (one shared filter)
`primary_analysis_eligible` excludes: unmapped, secondary, supplementary, QC-fail,
duplicate, MQ below configured floor (default 0 → keep, but MQ0 tracked), and
reads outside `[start0,end0)`. **Raw** and **analysis** views are both reported;
exclusions are explicit and mutually-exclusive counted:
`observed = included + Σ excluded-by-reason`. Missing base-qualities handled
explicitly (denominator = bases with quality available). Overlapping mates handled
in the fragment-primary coverage view (merge before events).

## Coverage methodology (§10) — AUTHORITATIVE
Two views: duplicate-including and duplicate-excluding; primary view is
**fragment-aware** (merge overlapping mate blocks before ±1 difference events);
deletions do **not** contribute to base depth (deletion coverage recorded
separately). Difference-array + prefix-sum per window; fixed depth bins +
deterministic quantiles. Never substitute read-count/region-length for exact depth.

## Pileup methodology (§12–§14) — AUTHORITATIVE (bounded)
`bam.pileup(truncate=True, min_base_quality=0, min_mapping_quality=0,
ignore_overlaps=True, compute_baq=False, max_depth=config, stepper=config)`.
Diagnostic + callable views; deletions/refskips separate; BAQ disabled; reference
allele from FASTA (ambiguous ref excluded from SNP-like density denominator).
Record columns reaching `max_depth` and reduce completeness/confidence. `PositionEvidence`
= {depth, ref_count, alt_base_counts, insertion_alleles, deletion_lengths,
ref/alt bq & mq hists, forward/reverse alt, read-position bins, clipped support}.
Aggregate multi-dimensional curves over support thresholds `[2,3,5,8,10]`, AF
thresholds `[.05,.10,.20,.30,.40]`, median alt BQ/MQ thresholds `[10,20,30]`.
**Cost model** `pred_seconds = exp(b0 + b1·log(region_bp) + b2·log(reads+1) +
b3·mean_depth + b4·max_depth_proxy + b5·clipping_rate + b6·cigar_complexity)`;
choose FULL only when `pred ≤ min(pileup_soft, remaining − serialization_reserve)`.
Conservative rule-based coefficients initially (calibration deferred — **INFERRED**).

## Reference profiling (§11) — AUTHORITATIVE
Reference **validation** (identity/compat) is separate from reference **profiling**
(GC, N-fraction, Shannon entropy, homopolymer density + length histogram,
dinucleotide-repeat indicator over the interval sequence). FASTA-derived indicators
are **not** repeat-mask/mappability tracks; unavailable annotations are marked
`unavailable` and never inferred from reads. If no FASTA: fail/degrade per policy,
never substitute BAM consensus.

## Missing-data / unavailable rules (§16–§17) — AUTHORITATIVE
`Unavailable = {value:null, status:'unavailable', reason:<typed code>}`. Reject
NaN/Inf, negative counts, unordered percentiles, fractions ∉[0,1], inconsistent
regions, missing units/hashes, and COMPLETE with incomplete required sections.

## Deadlines / degradation (§3–§4) — AUTHORITATIVE (values), stage set INFERRED
Monotonic `Deadline` (reuse Stage 0 `common/clock.py`). soft 180 s, hard 300 s,
pileup soft 90 s. Degradation stages (INFERRED, spec-compatible): `FULL →
REDUCED_PILEUP (adaptive) → REDUCED_WINDOWS → CORE_ONLY`; each records reason,
trigger, omitted/completed features, elapsed, remaining, usable, status. Never a
normal-looking COMPLETE with silently missing features. At hard limit: stop safely,
close handles, return typed degraded PARTIAL or typed timeout FAILED.

## Deterministic identity (§15, §17) — AUTHORITATIVE
Canonical JSON + sha256 (reuse `common/canonical_json`, `common/hashing`). Fixed
field order, fixed float precision (shortest round-trip repr), fixed histogram
bins/aggregation order, UTC timestamps. `ContextFingerprint` binds: profile schema
version, profiler algorithm version, profiler config hash, BAM identity, index
identity/status, reference identity/status, normalized region, sampling-plan hash,
read-filter-policy hash, completed feature families, degradation status, canonical
feature values. Excludes machine paths, timestamps, elapsed runtime, hostnames,
temp filenames, PIDs, thread ids.

## Truth-isolation requirements (BOUNDARY, Overall §2 FORBID) — AUTHORITATIVE
Layer 1 must never import/read truth VCF, mutations VCF, hidden score, leaderboard,
previous winning CONFIG, hap.py/scoring/evaluation results, or Layer 2. Enforced by:
(1) static import scan (existing `no_truth_or_locked_test_access` covers `layer1`);
(2) architecture import-boundary tests; (3) runtime filesystem-denial leakage test
(a sentinel truth file beside the BAM is never opened — Layer 1 opens only the
explicit BAM/BAI/FASTA/FAI paths and never globs a round directory).

## Performance requirements (§19) — AUTHORITATIVE
Median < 180 s; every run completes or degrades safely by 300 s (hard). The
qualification measures elapsed monotonic time + peak RSS and **fails** if the hard
limit is exceeded. Bounded memory: integer counters, fixed histograms, Welford,
deterministic quantile sketch, per-window accumulators; never retain all reads/bases.

## Required external tools
`pysam` (0.22.x) — BAM/CRAM/SAM + FASTA; index detection; regional fetch; pileup;
flags; CIGAR; MQ/BQ; read length; TLEN; contig compat. `pyarrow` (17.x) — the
fixed-Arrow-schema Parquet window artifact (spec §2). No `samtools` shell-out for
core behavior (optional diagnostic cross-check only). Stdlib monotonic time.

## Optional external test data (§4 of prompt) — real dataset located
- Root: `minos_subnet/datasets/` (read-only reference repo; **never modified**).
- Rounds: `datasets/practice/round_*/input.bam` (+ `.bai`); references
  `datasets/reference/chr{18..22}/chrN.fa` (+ `.fai`, `.dict`, `.sdf`).
- **Truth-isolation hazard:** each round dir also contains `truth.vcf.gz` and
  `mutations.vcf.gz` beside the BAM. Discovery therefore requires **explicit**
  BAM/reference paths and never enumerates a round directory. `MINOS_DATASET_ROOT`
  overrides the root; the real-BAM qualifier requires an explicit region.
- Sample used for this run: round `11fff0d59c751113` — chr19, coordinate-sorted,
  VN 1.6, single RG (`SM=sample, PL=ILLUMINA`), 1,572,563 mapped reads spanning
  chr19:36,703,000–46,703,290; reference `chr19.fa` LN 58,617,616 = BAM `@SQ:LN`.
  Region for qualification: **chr19:36,800,001-46,700,000** (1-based inclusive;
  ≈9.9 Mbp, protocol-scale, fully inside coverage).
- `data_lake(6).rar` (per the user) is the same BAM dataset; not used/extracted —
  the dataset is already present uncompressed. No archive is committed/extracted
  into a git-tracked directory.

## Unknown / underspecified (classified UNAVAILABLE or INFERRED)
1. `pileup.max_depth`, quantile algorithm identity, cost-model coefficients →
   "versioned after calibration" (§3, §14). **INFERRED**: pin conservative,
   documented, versioned defaults (`layer1-profiler-v1`); calibration deferred.
2. Confidence weights / risk transforms (§16) "validate later". **INFERRED**:
   documented monotonic transforms; descriptive only, never a GATK recommendation.
3. Difficult-window recall / adaptive-accuracy thresholds (§19) require a labeled
   study. **INFERRED** targets recorded; not gated numerically in synthetic CI.
4. Exact samtools parity tolerances (§19 Parity) → optional diagnostic cross-check
   only, never a mandatory CI dependency.

## Architecture-mandated deviations (documented)
- The spec's `cli.py` inside `layer1/` is honored via `cli/layer1_commands.py`
  wired into the existing single `minos-engine` CLI (Stage-0 architecture: all CLI
  under `cli/`). The public `minos-engine profile` command (Appendix A) is provided.
- Qualification orchestration for L1-READY lives in
  `qualification/layer1_runner.py` + `qualification/layer1_checks.py`, matching the
  Stage-0/Stage-1 architecture (all gate qualification under `qualification/`,
  reusing git-tree-bound evidence, JUnit accounting, coverage, ruff/mypy).
- Spec module list (region/deadline/hashing/…) is **merged** into a coherent set
  (prompt §6 permits merging while preserving responsibilities); the file→module
  responsibility map is in `docs/layer1/ARCHITECTURE.md`.

## Discrepancies between this prompt and the Layer 1 specification (spec wins)
1. **Window artifact format.** Prompt §6 lists only JSON schemas; the spec §2
   mandates `window-profile-v1.parquet` (fixed Arrow schema). **Follow spec:** emit
   Parquet via `pyarrow`, add the dependency. Both the prompt-named schemas
   (`layer1-profile-*`, `-fingerprint-`, `-integration-report-`) **and** the
   spec-named artifact schemas (`bam-profile-v1`, `window-profile-v1`,
   `profile-manifest-v1`) are produced. Content identity (the fingerprint) is over
   **canonical JSON** of the row values (stable across runs); the manifest records
   the Parquet file's sha256 (byte-stable under the pinned `pyarrow`). Documented in
   `docs/layer1/DETERMINISM.md`.
2. **`layer1_not_implemented` stage marker.** Implementing Layer 1 flips the live
   check to false. The accepted PROTOCOL-READY/TWIN-READY gates are frozen and
   re-verified by re-hash, so their `layer1_not_implemented=true` is untouched. The
   stale Stage-0/Stage-1 *live-tree* qualifier tests are updated to assert (a) the
   accepted committed gates still verify via `verify_protocol_ready` /
   `verify_twin_ready`, and (b) Layer 1 is now implemented while Layer 2 remains
   blocked. No accepted gate identity changes.

## Two-commit plan
- **Commit A** — Layer 1 qualified source: `layer1/*`, adapters, 7 schemas,
  `configs/layer1/default.yaml` (expanded), synthetic fixture generator, tests
  (groups A–L), docs, `qualification/layer1_{checks,runner}.py`, L1 prerequisite
  verifier, L1-READY required-check set, Layer 2 entry-gate wiring, CI update, this
  audit, and the committed `reports/LAYER1_REAL_BAM_REPORT.json` (sanitized, non-
  circular evidence). Clean worktree; passes full battery + real-BAM qualification
  + hard-limit under Python 3.12 from full history.
- **Commit B** — generated artifacts only: `gates/l1-ready.json`,
  `reports/LAYER1_QUALIFICATION_REPORT.md`; parent = Commit A.

## Conformance matrix
| Req ID | Spec § | Required behavior | Existing capability | Planned module | Tool/method | Test | Acceptance criterion |
|---|---|---|---|---|---|---|---|
| L1-01 | §1 | `Layer1Service.analyze(ProfileRequest)->ProfileResult` | stub raises StageNotReady | `layer1/service.py` | DI orchestrator | component/service | COMPLETE/PARTIAL usable; FAILED typed |
| L1-02 | §5 | Zero-based half-open region, convert once, bounds/contig equality, hard-fail ambiguity | region string in intake | `layer1/region.py` | pysam header/FAI | unit/region | boundary/one-base/1-based/chr-mismatch/unknown-conv covered |
| L1-03 | §6 | BAM/BAI/FASTA integrity via pysam; content hash; verification strength | none | `layer1/validation.py`, `adapters/pysam_adapter.py` | pysam open+fetch+idx | component/validation | corrupt/missing/stale/mismatch → typed hard failure |
| L1-04 | §7 | Header profile HD/SQ/RG/PG + compat | none | `layer1/validation.py` (header) | pysam header | component/header | fields extracted; SO/compat derived |
| L1-05 | §8 | One-pass bounded-memory alignment scan; raw+analysis views | none | `layer1/alignment_scan.py`, `aggregators.py` | pysam fetch | unit/aggregators, component | never retains all reads; exclusions explicit |
| L1-06 | §9 | Exact alignment/MQ/BQ/clip/NM/pair metrics | none | `alignment_scan.py`, `filters.py` | counters/Welford/hist | unit/metrics (known-answer) | each formula matches independent calc |
| L1-07 | §10 | Fragment-aware two-view coverage; bins+quantiles; deletions separate | none | `layer1/coverage.py` | diff-array+prefix-sum | unit/coverage | overlap not double-counted; exact vs known |
| L1-08 | §11 | Reference GC/N/entropy/homopolymer/dinuc | none | `layer1/reference_profile.py` | pysam FASTA | unit/reference | known GC/N/entropy/homopolymer vectors |
| L1-09 | §12–13 | Bounded pileup → PositionEvidence + curves | none | `layer1/pileup.py` | pysam pileup | unit+component/pileup | capped columns recorded; thresholds correct |
| L1-10 | §14 | Cost model FULL vs ADAPTIVE | none | `layer1/cost_model.py` | closed-form | unit/cost | FULL only within budget; else ADAPTIVE |
| L1-11 | §15 | Deterministic strata sampling + weights | none | `layer1/sampling.py` | hash tie-break | unit/sampling, determinism | stable selection; πᵢ,1/πᵢ recorded |
| L1-12 | §16 | Difficulty vector + confidence + completion | none | `layer1/difficulty.py` | versioned transforms | unit/difficulty | descriptive; geo-mean confidence bands |
| L1-13 | §15,17 | ContextFingerprint canonical identity | Stage 0 hashing | `layer1/fingerprint.py` | canonical_hash | determinism | identical across runs; semantic change → change |
| L1-14 | §2,17 | Atomic canonical JSON + Parquet + manifest | Stage 0 canonical | `layer1/serializer.py` | tmp+fsync+rename, pyarrow | component/serializer, failure-injection | rename-fail → no complete set; SERIALIZATION_FAILURE |
| L1-15 | §4 | Orchestrator deadline state machine + degradation | Stage 0 Deadline | `layer1/orchestrator.py` | FakeClock | determinism/deadline | soft/hard/pileup-soft degrade; no silent partial |
| L1-16 | §19 | Performance median<180s, all ≤300s, bounded RSS | none | qualification perf | monotonic + RSS | performance/real-BAM | hard-limit success = 1.0 |
| L1-17 | BOUNDARY | Truth isolation (import + runtime denial) | Stage 0 scan | scan + leakage test | ast + fs sentinel | leakage/layer1 | no truth import/read; sentinel untouched |
| L1-18 | §1 | Layer 2 forward-compatible output contract | Layer 2 entry gate | `contracts.py` | schema | acceptance/entry-gate | profile fields cover L2 inputs; L2 blocked |
| L1-19 | §20 | L1-READY issuance (schema/config/version/determinism/perf/isolation) | Stage 0/1 qualifier | `qualification/layer1_runner.py` | git-tree-bound gate | component/qualification | PASS not constructible with any false check |
| L1-20 | §20 lock | Layer 2 blocked until L1-READY verifies | entry_gate | `layer2/entry_gate.py` | gate verify | acceptance/entry-gate | HOLD/REJECT/tamper/wrong-hash block |
| L1-21 | prompt §4 | synthetic_ci_qualified vs real_bam_qualified split | none | qualifier statuses | two-tier | qualification | no PASS with real_bam_qualified=false |

**Gate to proceed:** audit + matrix complete ✅ — implementation begins at the Layer 1 contracts.
