# L2-F2 Baseline — Pre-Implementation Audit

**Status of this document:** audit performed before any baseline implementation
or baseline compute. No source, migration, gate or schema was modified while
producing it. No GATK run, no hap.py run, no model training, no test-truth
access.

| | |
|---|---|
| Audit HEAD | `107a745f2530b841ee3b399c81f3b2385cb6de2b` |
| Qualified F7 source | `488af0a8e8d49574fc301d9d5ea2ba2707704428` / tree `71a6ae05d32835383405f26c378e3ca0787b062b` |
| Evidence-commit CI | run `32716388794` — push / completed / success / CI |
| Current stage | L2-F2 (see `docs/DEVELOPMENT_STATUS.md`) |

Verdict vocabulary: **VERIFIED**, **PARTIAL**, **MISSING**, **INCOMPATIBLE**,
**OWNER DECISION REQUIRED**. Severity vocabulary: **BLOCKER**, **DEFECT**,
**HARDENING**, **COSMETIC**.

---

## 0. Executive summary

| # | Finding | Verdict | Severity |
|---|---|---|---|
| 1 | Minos scoring authority is fully traceable end-to-end, including the post-`AdvancedScorer` transformation | **VERIFIED** | — |
| 2 | Reward is **rank-based**, not proportional to score | **VERIFIED** | OWNER DECISION REQUIRED |
| 3 | `evaluation.evaluations` targets the legacy `experiments.results`, not `experiments.l2f_execution_results` | **INCOMPATIBLE** | **BLOCKER** for L2-F2 persistence |
| 4 | Truth/mutation material exists on disk for all 75 rounds but is registered nowhere | **PARTIAL** | **BLOCKER** for evaluation |
| 5 | `minos_evaluator` role exists but is NOLOGIN; no evaluator login principal | **PARTIAL** | **BLOCKER** for evaluator isolation |
| 6 | The accepted 39 candidates are pure one-at-a-time; zero interactions tested | **VERIFIED** | search-design input |
| 7 | Split is exactly 50/10/15 and perfectly chromosome-balanced | **VERIFIED** | enables cheap screening |
| 8 | Robust objective J(c) is not mandated in full by the specification | **OWNER DECISION REQUIRED** | must freeze before compute |

**Three blockers** must clear before baseline compute begins. None requires
reopening L2-F1; all are additive L2-F2 work.

---

## 1. Scoring authority — **VERIFIED**

The baseline is meaningless unless our offline objective reproduces the real
validator semantics, so the score was traced all the way to the reward.

### 1.1 Source identity

| | |
|---|---|
| Repository | `minos-protocol/minos_subnet` |
| Local clone | `/home/hr/bittensor/minos_subnet`, branch `main` |
| Local HEAD | `649bb92c6abccebde58a736a2b2af7fd77a701c1` |
| Upstream `refs/heads/main` | `649bb92c6abccebde58a736a2b2af7fd77a701c1` — **independently reconfirmed** via `git ls-remote`, not assumed |
| Commit date / subject | 2026-08-10 — *Merge PR #24 from feat/chr18-practice-support* |
| Local dirt | `ecosystem.miner.config.js` modified (miner process config; **not** scoring code) |

File digests at that commit:

| File | SHA-256 |
|---|---|
| `utils/scoring.py` | `7b5aa187adda5978adc029abcd4c96b7b78eafeb9c5641153955175cd0b7b658` |
| `neurons/validator.py` | `2ac0841231a58794097ba40d245f27eaa44e1bd1b66134a17dece96a1a37f33e` |
| `templates/tool_params.py` | `6e9648fb6d6bda1ed5411eff01c38596cc869e2f7ae9e5de855e8413f10e0765` |

### 1.2 Container identities

| Tool | Image |
|---|---|
| hap.py | `genonet/hap-py@sha256:03acabe84bbfba35f5a7234129d524c563f5657e1f21150a2ea2797f8e6d05f2` (digest-pinned) |
| bcftools | `quay.io/biocontainers/bcftools:1.20--h8b25389_0` (tag-pinned, **not** digest-pinned) |

`utils/scoring.py:22–23`. The bcftools image is tag-pinned only — **HARDENING**:
resolve and record its digest at evaluation time so the evidence is reproducible.

### 1.3 The complete score flow

```
candidate VCF
  → HappyScorer.score_vcf()                utils/scoring.py:565
      truth slicing                        slice_truth_vcf()            :69
      confident/synthetic region BED       generate_synthetic_regions_bed() :161
                                           generate_challenge_region_bed()  :228
      hap.py in Docker (digest-pinned)     :745–747
  → parsed metrics
      assessed-only corrections            parse_happy_vcf_assessed_metrics() :427
      mutation-only metrics                compute_synthetic_only_metrics()   :254
      region overcall guardrail            parse_region_overcall_metrics()    :365
  → AdvancedScorer.compute_advanced_score(metrics)   :945   →  float in [0, 100]
  → neurons/validator.py:890–892
        combined_final = _valid_round_score(advanced_score / 100.0)     →  (0, 1]
  → zero-input fingerprint rejection        validator.py:86–92
  → ScoreTracker ranking
  → _set_weights_after_round()              validator.py:1295
        weights from platform network config: burn_rate, burn_uid,
        winner_weight, dust_top_n, dust_decay
  → chain set_weights()
```

### 1.4 `AdvancedScorer` formula (frozen at the audited commit)

```
core         = emphasis(Σ(f1_v · truth_total_v) / Σ truth_total_v,  γ = 0.5)
completeness = [ emphasis(mean(recall_snp, recall_indel), γ = 3.0)
               + emphasis(1 − max(frac_na_snp, frac_na_indel), γ = 2.0) ] / 2
fp_rate      = [ exp(−max(0, fp_rate − target_fp) / target_fp)
               + exp(−|size_ratio − 1| / 0.10) ] / 2
                 target_fp  = max(0.002, 1 / total_truth)
                 size_ratio = total_calls / total_truth
quality      = [ titv_component + hethom_component ] / 2
                 ratio_penalty(δ, tol) = exp(−|δ| / tol)
                 Ti/Tv tol = 0.10 ; Het/Hom tol = 0.15

score_100 = 100 · (0.60·core + 0.15·completeness + 0.15·fp_rate + 0.10·quality)
final     = max(0, score_100 − overcall_penalty)

emphasis(m, γ) = 1 − (1 − clamp(m, 0, 0.999999))^γ
```

`total_truth ≤ 0` returns **0.0** and logs an error (`utils/scoring.py:986–988`).

### 1.5 Post-scorer transformation — the part that must not be skipped

`AdvancedScorer` is **not** the validator-visible score.

* **Normalization:** `combined_final = advanced_score / 100.0` →
  subnet-visible range **(0.0, 1.0]**.
* **Strict admission:** `_valid_round_score` (`validator.py:72–84`) requires the
  value to be finite and `0.0 < score ≤ 1.0`. Anything else — including exactly
  `0.0` — is **skipped entirely**, not recorded as zero.
* **Zero-input fingerprint:** `_is_zero_input_advanced_fingerprint`
  (`validator.py:86–92`) rejects a submission when `f1_snp == 0` **and**
  `f1_indel == 0` **and** `combined_final ≈ 0.25`. An all-zero metrics dict
  scores ≈ 25/100 because the three non-core components default toward 1.0; the
  guard exists to stop that artefact from ranking.
* **Reward:** `_set_weights_after_round` takes `burn_rate`, `burn_uid`,
  `winner_weight`, `dust_top_n`, `dust_decay` from the platform network config
  and **fails closed** if the policy is unavailable. Reward is therefore
  **winner-take-most plus decaying dust to the top N, with burn** — a function of
  **rank**, not of score magnitude.

**Consequence — OWNER DECISION REQUIRED.** Maximising mean score and maximising
reward are different objectives. A CONFIG that is reliably second is worth far
less than one that wins sometimes. Section 6 offers objectives for both readings.

### 1.6 Gaps

| Item | Verdict |
|---|---|
| Scorer source pinned and hashed | VERIFIED |
| hap.py digest-pinned | VERIFIED |
| bcftools digest | **PARTIAL** — tag only (HARDENING) |
| Truth slicing / region semantics | VERIFIED, located |
| Mutation-only filtering | VERIFIED, located |
| Overcall guardrail | VERIFIED, located |
| Final score range and normalisation | VERIFIED — `/100`, `(0, 1]` |
| Validator weighting | VERIFIED — rank-based, platform-supplied policy |
| Chromosome weighting inside the scorer | **none found** — chromosome effects enter only through the data |
| Reward policy values (`winner_weight`, `dust_*`, `burn_rate`) | **MISSING locally** — served by the platform at runtime; cannot be frozen offline (OWNER DECISION on how to model them) |

**No scorer needs to be invented.** Baseline implementation is not blocked on
scoring authority.

---

## 2. Current evaluation schema — **INCOMPATIBLE (BLOCKER)**

The independent finding is **confirmed** by direct inspection of
`migrations/versions/0001_l2b_initial.py:285–300`:

```python
op.create_table(
    "evaluations",
    _uuid_pk(),
    sa.Column("experiment_result_id", postgresql.UUID(as_uuid=False), nullable=False),
    _sha("evaluation_hash"),
    _ts("created_at"),
    sa.ForeignKeyConstraint(["experiment_result_id"],
                            ["experiments.results.id"],      # ← LEGACY target
                            name="fk_evaluations_experiment_result_id_results"),
    sa.UniqueConstraint("evaluation_hash", ...),
    schema="evaluation",
)
```

> ### CURRENT EVALUATION TABLE IS NOT THE L2-F2 AUTHORITY.

Four columns total. Its foreign key targets `experiments.results` — the L2-B
placeholder path — while the accepted harness writes
`experiments.l2f_execution_results`. Live row counts: `evaluation.evaluations`
**0**, `experiments.results` **0**.

**Question-by-question:**

1. *Can it represent evaluation of an L2-F execution result?* **No.** The FK
   cannot reference an `l2f_execution_results` row.
2. *Can it bind execution result / dataset / CONFIG / scorer identity / truth
   identity / hap.py identity / parsed metrics / final score / evaluation
   artifact / partition?* **No** — it binds only an opaque `evaluation_hash` and
   a legacy result id. None of the other ten bindings exist.
3. *Can current role grants support the baseline evaluator?* **Not yet.**
   `minos_evaluator` exists as a role but is **NOLOGIN**; the only login
   principal today is `minos_f7_observer`, whose grants are deliberately narrow
   (it cannot even `SELECT catalog.split_allocations` — confirmed by
   `InsufficientPrivilege` during this audit). That narrowness is correct for
   F7 and simply means L2-F2 needs its own principal.
4. *Is a new additive migration required?* **Yes.**

Do **not** force L2-F evaluations into the legacy row shape. No migration was
created in this task.

`evaluation.dataset_evaluation_identity` (migration `0002_l2c_dataset_split.py:190`)
is the one genuinely reusable piece: it already binds
`dataset_registry_id → (truth_vcf_sha256, truth_tbi_sha256, mutations_vcf_sha256,
mutations_tbi_sha256)` with a uniqueness constraint per dataset. All four columns
are nullable, and **all rows are absent** (see §3).

---

## 3. Truth / split security — **PARTIAL (BLOCKER for evaluation)**

### 3.1 Split contract — VERIFIED

Read from `catalog.split_allocations` (counts only):

| Partition | Datasets | chr18 | chr19 | chr20 | chr21 | chr22 |
|---|---|---|---|---|---|---|
| train | **50** | 10 | 10 | 10 | 10 | 10 |
| validation | **10** | 2 | 2 | 2 | 2 | 2 |
| test | **15** | 3 | 3 | 3 | 3 | 3 |

The 50/10/15 contract holds and the split is **perfectly chromosome-balanced**.
This is directly exploitable: "one deterministic TRAIN BAM per chromosome" is a
clean, score-independent 5-BAM screening set.

### 3.2 Truth material — PARTIAL

| Observation | Value |
|---|---|
| Practice round directories on disk | 75 |
| With `truth.vcf.gz` | 75 |
| With `truth.vcf.gz.tbi` | 75 |
| With `mutations.vcf.gz` | 75 |
| With `mutations.vcf.gz.tbi` | 75 |
| Registry rounds mapping to an on-disk directory | 75 / 75 (train 50, validation 10, test 15; **0 missing**) |
| Rows in `evaluation.dataset_evaluation_identity` | **0** |

Location: `/home/hr/bittensor/minos_subnet/datasets/practice/round_<round_id>/`.

So the material **exists and is complete**, but **no truth identity is registered
in the database for any partition**. Registering TRAIN (and later VALIDATION)
truth identities is a required L2-F2-A deliverable.

Two consequences:

* **BLOCKER**: evaluation cannot run until truth identities are ingested.
* **HARDENING**: the truth corpus currently lives under `minos_subnet`, i.e.
  outside a MINOS-owned data root. It is already beneath `/home/hr/bittensor`, so
  the canonical-workspace rule is satisfied, but L2-F2 should bind truth by
  content hash rather than by path so a `minos_subnet` update cannot silently
  change evaluation inputs.

### 3.3 Test isolation — held

During this audit **no test truth was opened, resolved to a payload path, or
read**. Only directory names were listed and files counted; split membership was
read as counts from the database. Test truth remains locked until L2-I.

`reference` (FASTA/FAI/DICT) is available under the harness dataset root.
Whether the real scorer additionally requires a confident BED or an SDF is
handled inside `HappyScorer` via `generate_synthetic_regions_bed()` /
`generate_challenge_region_bed()`, which construct the BED from the truth VCF —
so no external confident-region file needs provisioning. **VERIFIED.**

---

## 4. Candidate design — **VERIFIED (screening only)**

Audited `experiments/candidates.py`, `gatk_live_space.py`, `policy.py`,
`accepted_plan.py` by generating the accepted set and diffing every candidate
against the seed CONFIG.

| Property | Value |
|---|---|
| Accepted candidate count | 39 (`50d5f369…`) |
| Structure | 1 seed + 38 single-parameter deviations |
| **Maximum deviations from seed in any candidate** | **1** |
| Live dimensions in the seed CONFIG | 25 |
| Dimensions varied by the 39 | 22 |
| Dimensions never varied | 3 — `dont_use_soft_clipped_bases` (False), `emit_ref_confidence` ("NONE"), `sample_ploidy` (2) |
| Policy | `l2f-experiment-parameter-policy-v2`, registry `e4000cf7…`, seed CONFIG `4251cb85…b751b8` |

Per-dimension coverage: `pcr_indel_model` has 3 levels; fifteen dimensions have
2 levels each; six have 1 level.

**Interpretation.** The accepted 39 are a textbook **one-at-a-time (OAT)
sensitivity design**. Because no candidate deviates in more than one dimension,
**zero parameter interactions have been tested** — and GATK's assembly
parameters interact strongly (e.g. `min_pruning` × `pruning_lod_threshold`,
`max_assembly_region_size` × `assembly_region_padding`,
`active_probability_threshold` × `standard_min_confidence_threshold_for_calling`).

**Verdict:** suitable as a **Phase-A sensitivity screen**, *not* sufficient as a
baseline optimisation space.

The accepted 39 and the plan hash `eb8de84d…` are bound into HARNESS-READY
forever and **must not be mutated**. L2-F2 defines a *new, separately versioned*
baseline-search candidate design; HARNESS evidence is untouched.

---

## 5. Compute-efficiency finding

`50 × 39 = 1950` logical jobs proved *harness capability*. It is **not** the
baseline-search budget, and executing it blindly would be poor experimental
design as well as expensive: 1950 × ~69 s ≈ **37.4 hours** of GATK alone, plus
1950 hap.py evaluations, to explore an OAT design with no interaction coverage.

The single observed GATK runtime is **69,398 ms** for the accepted chr18 TRAIN
sample at the seed CONFIG (F7-B R3). Runtimes vary materially by sample and by
CONFIG — parameters such as `max_assembly_region_size`,
`max_num_haplotypes_in_population` and `max_reads_per_alignment_start` move
compute cost directly — so all estimates below use ~69 s as **one reference
point**, not a constant.

Budgets are in `docs/layer2/BASELINE_QUALIFICATION.md` §9.

---

## 6. Robust objective — **OWNER DECISION REQUIRED**

The specification requires a robust objective and chromosome-aware analysis but
does not fully mandate the functional form. Three options with equations, and a
recommendation, are in `docs/layer2/BASELINE_QUALIFICATION.md` §7.

Non-negotiable regardless of choice: the objective, ranking rule, tie-breaks,
failure handling and promotion rule are **frozen and hashed before any score is
observed**. No score-dependent rule changes after execution begins.

---

## 7. Reuse without contamination — VERIFIED

Safely reusable (immutable, content-addressed, truth-free):

* the 39 CONFIG payload artifacts (`config_artifacts`, byte-identical, 2750);
* the frozen feature matrix and profile snapshot;
* the pinned GATK bundle `2707ad20…` (launcher + local JAR + version);
* reference FASTA/FAI/DICT and the BAM/BAI inputs;
* content-addressed result artifacts **only** where scientific identity matches
  exactly (same plan hash, job key, CONFIG hash and runtime bundle).

Explicitly **not** reusable as a store: `minos_f7b_qualification`,
`…_r2` and `…_r3`. Those are qualification and forensic state — attempt #1
(2 jobs / 1 result / 1 failure), attempt #2 (1/0/1) and attempt #3 (2/1/1). They
must never become the baseline experiment database.

Proposed dedicated workspace: **`/home/hr/bittensor/minos_l2f2_baseline`**, with
a separate baseline database. Work roots must be `0750` **without setgid** —
setgid propagates to per-attempt directories and breaks the production
`0700`-exact check (this caused F7-B attempt #2 to fail). Artifact roots remain
`2750` with setgid.

---

## 8. Unresolved owner decisions

| # | Decision | Why it cannot be defaulted |
|---|---|---|
| D1 | Rank-oriented vs level-oriented objective | Reward is winner-take-most; mean-score optimisation may be the wrong target |
| D2 | Robust objective form (Option A / B / C) | Not fully mandated by the specification |
| D3 | Lower-tail parameter (CVaR α) and per-chromosome floor | Sets risk appetite |
| D4 | Runtime constraint — hard cap, tie-break, or ignored | Affects feasibility under round deadlines |
| D5 | Search budget tier (LEAN / STANDARD / HIGH-CONFIDENCE) | Cost vs confidence |
| D6 | Whether VALIDATION may be read at L2-F2-F or is deferred to L2-G | Specification permits post-freeze use; exact timing is owner-controlled |
| D7 | Whether to model the platform reward policy offline | `winner_weight` / `dust_*` / `burn_rate` are served at runtime and unavailable locally |
| D8 | Phase-B design family (Sobol / LHS / fractional-factorial) | Any is defensible; must be pre-registered |

---

## 9. Verdict

Three **BLOCKERS** stand between here and baseline compute — the evaluation
schema (§2), truth registration (§3.2) and the evaluator login principal (§2.3).
All are additive L2-F2-A work. None reopens L2-F1, none requires touching
HARNESS evidence, and none requires inventing a scorer.

Eight **owner decisions** (§8) must be resolved and frozen before Phase-A
compute begins.
