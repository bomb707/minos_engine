# L2-F2 — Baseline Discovery and Baseline Qualification

**Status: the search protocol is FROZEN; the search itself has not run.**

`l2f2-baseline-search-protocol-v1` is committed, hashed and pre-registered in
`manifests/l2f2_baseline_protocol_v1.json`, with freeze evidence in
`reports/layer2/l2f2-b-protocol-freeze-result.json`. **D1–D8 are resolved** (§16) and no rule
below may be altered on the basis of an observed score — that is the entire point of freezing
them before the first score exists.

Sections that present *alternatives and recommendations* are retained as the design record that
led to the frozen choice. Where an option was considered and rejected it is marked as such; the
frozen answer is always the one stated in §16 and in the committed manifest.

**Not yet done:** no Phase-A/B/C execution, no observed score, no validation access, and
`BASELINE-QUALIFIED` is not issued. Current stage state lives in `docs/DEVELOPMENT_STATUS.md`;
the historical pre-implementation evidence lives in
`reports/LAYER2_BASELINE_PREIMPLEMENTATION_AUDIT.md`.

## L2-F2-A prerequisites — CLOSED

| Prerequisite | State |
|---|---|
| Evaluation persistence contract | **CLOSED** — migrations `0009` + `0010` are applied to the baseline store |
| TRAIN truth identities | **REGISTERED** — exactly 50 real TRAIN identities in `minos_l2f2_baseline` |
| Evaluator service principal | **PROVISIONED** — `minos_evaluator_svc`, `LOGIN`, member of `minos_evaluator` only |
| Baseline workspace and database | **ENVIRONMENT_READY** — `/home/hr/bittensor/minos_l2f2_baseline`, database at `0010_l2f2_evaluation_corrective` |
| Database connection isolation | **CLOSED** — `PUBLIC` `CONNECT` revoked on both databases; the evaluator credential cannot reach `minos_engine_db` |
| Real-GATK canary boundary | **SOURCE_READY_PENDING_ENVIRONMENT** — migration `0011` and `execute_next_l2f2_phase_a_job` implement a least-privilege real-GATK boundary for the baseline database. The environment is not yet provisioned and the canary has not run |

---

## 1. Purpose

Discover a **robust baseline GATK CONFIG** for the Minos SN107 practice/validator
objective, and prove — with immutable evidence — that it was selected by a
pre-registered protocol rather than by post-hoc score inspection.

The output is a `BASELINE-QUALIFIED` gate binding one `config_hash`, the exact
protocol that chose it, and every evaluation that informed the choice.

## 2. Prerequisites

| Prerequisite | State |
|---|---|
| HARNESS-READY gate PASS (40/40) | met — gate `0e8411eb…`, qualification `b1d1cc5d…` |
| Frozen split 50/10/15 | met — verified 50 train / 10 validation / 15 test |
| Frozen feature matrix / profile snapshot | met |
| Pinned GATK bundle | met — `2707ad20…` |
| Scoring authority pinned | met — see §3 |
| Evaluation persistence contract | **implemented in source** — migrations `0009` + `0010`, §11 |
| Evaluation execution path | **implemented in source** — `minos_engine.evaluation.orchestrator.evaluate_execution`; proven end to end on synthetic TRAIN data under the evaluator service principal, never on real practice truth |
| Truth identities registered | **mechanism implemented; no real registration yet** — §5 |
| External evaluator service login principal | **contract + runbook exist; not provisioned** — §4 |

The last three are the L2-F2-A deliverables. Their **source** side is complete; the
environment side (baseline workspace, baseline database, service principal, real truth
registration) is the next task. See `docs/DEVELOPMENT_STATUS.md`.

## 3. Scoring authority

The offline objective must reproduce validator semantics exactly.

| | |
|---|---|
| Source | `minos-protocol/minos_subnet` @ `649bb92c6abccebde58a736a2b2af7fd77a701c1` (upstream `main`, independently reconfirmed) |
| `utils/scoring.py` | `7b5aa187adda5978adc029abcd4c96b7b78eafeb9c5641153955175cd0b7b658` |
| `neurons/validator.py` | `2ac0841231a58794097ba40d245f27eaa44e1bd1b66134a17dece96a1a37f33e` |
| hap.py | `genonet/hap-py@sha256:03acabe84bbfba35f5a7234129d524c563f5657e1f21150a2ea2797f8e6d05f2` |
| bcftools | `quay.io/biocontainers/bcftools@sha256:badc3a0c7af72a83e5761ab0e881aa84204694bdead003b47552cb283958f78d` — resolved and confirmed against both the local image and the quay.io registry |

**The score used for ranking is not `AdvancedScorer`'s output.** The validator
computes `combined_final = advanced_score / 100.0` and admits it only when
`0.0 < combined_final ≤ 1.0`; an all-zero-metrics fingerprint at ≈ 0.25 is
rejected outright. Reward then follows a **rank-based** platform policy
(`winner_weight`, `dust_top_n`, `dust_decay`, `burn_rate`, `burn_uid`).

L2-F2 therefore defines:

* `minos_score_100` — `AdvancedScorer.compute_advanced_score(metrics)`, in [0, 100];
* `minos_score` — `minos_score_100 / 100`, in [0, 1], **the** unit of the objective;
* `admitted` — whether the validator would have accepted the submission at all.

A non-admitted evaluation is **not** a zero score. It is a distinct outcome and
is treated as a failure (§8).

## 4. Evaluation isolation

```
GATK execution  (truth-free)          OFFLINE evaluation  (truth-aware)
role: minos_runner                    role: minos_evaluator
writes: experiments.l2f_execution_results     reads: immutable result identity
                                              writes: evaluation.*
        │                                             │
        └── immutable result identity ────────────────┘
                                                      ↓
                                        baseline statistics / training labels
```

The GATK runner **stays truth-free**; nothing in the execution path may resolve a
truth or scoring path. This is already enforced and gate-bound
(`no_truth_or_scoring_access`, `truth_paths_resolved = 0`).

`minos_evaluator` is an **intentionally NOLOGIN group role**, like `minos_live`,
`minos_runner`, `minos_trainer` and `minos_admin`. That is the architecture and it
stays: group roles carry authority, not credentials. Do **not** grant it LOGIN and
do **not** give it a password.

The external service login principal `minos_evaluator_svc` **is now provisioned** (L2-F2-A):
`LOGIN`, granted `minos_evaluator` and nothing more, with no write privilege on `experiments.*`
and no direct `INSERT` on `catalog.artifacts`. It reaches the catalog only through the narrow
`0010` metrics registrar, and an explicit per-database `CONNECT` allowlist prevents it from
opening the operational store at all.

## 5. Partition policy

| Partition | Count | L2-F2 use |
|---|---|---|
| TRAIN | 50 | search, screening, racing, HPO — unrestricted |
| VALIDATION | 10 | **only after** finalists, objective and ranking rule are frozen; small finalist set only |
| TEST | 15 | **LOCKED** until L2-I — not opened, not path-resolved, not read |

Truth material for all 75 rounds exists at
`/home/hr/bittensor/minos_subnet/datasets/practice/round_<id>/`
(`truth.vcf.gz`, `truth.vcf.gz.tbi`, `mutations.vcf.gz`, `mutations.vcf.gz.tbi`)
but **is registered nowhere** — `evaluation.dataset_evaluation_identity` has 0
rows. L2-F2-A registers TRAIN identities by content hash; VALIDATION identities
are registered only when §5 permits their use. TEST identities are **not**
registered at this stage.

Truth must be bound **by content hash**, never by path, so an upstream
`minos_subnet` change cannot silently alter evaluation inputs.

## 6. Candidate-design policy

The accepted 39 candidates (`50d5f369…`) are a pure **one-at-a-time** design:
1 seed + 38 single-parameter deviations, **maximum 1 deviation per candidate**,
covering 22 of 25 live dimensions. `dont_use_soft_clipped_bases`,
`emit_ref_confidence` and `sample_ploidy` are never varied.

Therefore: **zero interactions are tested**. The 39 are a valid *sensitivity
screen* and an invalid *optimisation space*.

The accepted 39, the policy `a40321a6…` and the plan `eb8de84d…` are bound into
HARNESS-READY **forever** and are never mutated. L2-F2 introduces a new,
separately versioned baseline-search candidate design
(`l2f2-baseline-candidate-design-v1`) with its own hash. Historical HARNESS
evidence is untouched.

## 7. Robust objective — FROZEN as Option B (design record below)

Notation: candidate `c`; TRAIN BAMs `i ∈ B`; chromosomes `k ∈ K` (chr18–chr22);
`s_i(c) ∈ [0, 1]` the admitted Minos score; `F(c)` the set of failed or
non-admitted evaluations.

### Option A — mean with failure penalty (simplest)

```
J_A(c) = mean_i s_i(c) − λ · |F(c)| / |B|
```

Cheap and low-variance. **Rejected as the default:** it lets a CONFIG with an
excellent mean but catastrophic behaviour on one BAM or chromosome win.

### Option B — lower-tail (CVaR) with per-chromosome floor — **RECOMMENDED**

```
CVaR_α(c) = mean of the ⌈α·|B|⌉ smallest s_i(c)          (α = 0.25)
chr_k(c)  = mean_{i ∈ k} s_i(c)
floor(c)  = min_k chr_k(c)

J_B(c) = 0.50 · CVaR_α(c)
       + 0.30 · floor(c)
       + 0.20 · mean_i s_i(c)
       − λ · |F(c)| / |B|                                 (λ = 1.0)
```

Directly encodes the requirement that a CONFIG cannot win on mean alone: half the
weight sits on the worst quartile and 30 % on the worst chromosome. Chromosome
balance (10 TRAIN BAMs per chromosome) makes `chr_k` well-estimated.

### Option C — rank-oriented (matches how reward is actually paid)

```
For each BAM i, rank candidates; let w_i(c) = 1 if c is best on i, else 0.
J_C(c) = mean_i w_i(c) − λ · |F(c)| / |B|      (optionally softened by
                                                top-n dust weights)
```

Mirrors the winner-take-most reward. **Caveat:** it is a within-our-candidate-set
proxy for a competition against *other miners*, whose configs we cannot observe;
it is also higher-variance and needs more BAMs per candidate.

### Recommendation

**Option B**, with **Option C reported alongside** as a diagnostic. Option B
gives a defensible, low-variance, robustness-first baseline; Option C reveals
whether the B-winner would also *win rounds*, which is what the rank-based reward
actually pays for. If B's winner is consistently rank-mediocre, that is an
explicit protocol decision point — not something to silently re-optimise.

Open: **D2** (form), **D3** (α and floor weight), **D4** (runtime), **D1**
(rank vs level emphasis), **D7** (modelling the platform reward policy).

## 8. Deterministic rules — frozen before execution

| Situation | Rule |
|---|---|
| Tie in `J` | lower mean runtime → lower `config_index` → lexicographically smaller `config_hash`. Fully deterministic. |
| GATK failure | evaluation counts toward `F(c)`; score contributes nothing. Never silently dropped. |
| GATK timeout | same as failure; the timeout bound is part of the protocol hash. |
| Non-admitted score (`≤ 0`, `> 1`, or zero-input fingerprint) | counted in `F(c)`; **not** recorded as 0.0. |
| Missing evaluation | protocol violation → the candidate is **not rankable**; it may not be promoted. |
| Promotion to VALIDATION | only the top `N_f` finalists by `J` on full TRAIN, with `N_f` fixed **before** any validation read. |

**No score-dependent rule changes after execution starts.** The objective,
weights, α, λ, tie-breaks and `N_f` are hashed into the protocol hash and bound
by the gate.

## 9. Racing protocol and cost budget

Selection of screening BAMs is **score-independent**: one deterministic TRAIN BAM
per chromosome, chosen by lowest `sort_order` within each chromosome.

### Phases

* **Phase A — sensitivity screen.** The existing 39 OAT candidates × 5
  representative TRAIN BAMs (one per chromosome). Purpose: rank dimensions by
  effect size. Not an optimisation.
* **Phase B — compact interaction search.** A pre-registered deterministic rule
  selects the influential dimensions from Phase A; a compact design (Sobol / LHS
  / fractional-factorial — **D8**) covers only those, evaluated on 10 TRAIN BAMs
  (two per chromosome).
* **Phase C — survivor confirmation.** Survivors run across all 50 TRAIN BAMs.
* **Phase D — frozen validation.** Freeze candidate set, objective, ranking and
  tie-breaks, *then* read the 10 validation scores for a very small finalist set.
  **No TEST.**

### Budgets

Each GATK execution is paired with one hap.py evaluation. `~69 s` is one observed
GATK reference point (chr18 TRAIN, seed CONFIG); real runtimes vary by sample and
CONFIG. hap.py is assumed comparable in order of magnitude; wall-clock benefits
from parallelism.

| Tier | Phase A | Phase B | Phase C | Phase D | **GATK runs** | **hap.py runs** | GATK ref. time |
|---|---|---|---|---|---|---|---|
| **LEAN** | 39 × 5 = 195 | 24 × 10 = 240 | 6 × 50 = 300 | 3 × 10 = 30 | **765** | 765 | ≈ 14.7 h |
| **STANDARD** | 39 × 5 = 195 | 48 × 10 = 480 | 10 × 50 = 500 | 4 × 10 = 40 | **1 215** | 1 215 | ≈ 23.3 h |
| **HIGH-CONFIDENCE** | 39 × 10 = 390 | 96 × 10 = 960 | 16 × 50 = 800 | 5 × 10 = 50 | **2 200** | 2 200 | ≈ 42.2 h |

Blind execution of the full 1950-job harness universe would be ≈ 37.4 h of GATK
and would still test **zero interactions**.

**Recommended default: STANDARD — 1 215 GATK runs, a 37.7 % reduction versus
1950, while adding interaction coverage the 1950 does not have.** LEAN (765,
−60.8 %) is appropriate if compute is tight. HIGH-CONFIDENCE only if Phase A
shows many comparably influential dimensions.

Disk: each result publishes a VCF (~2.2 MB observed) plus a ~2 KB manifest, so
STANDARD ≈ **2.7 GB** of result artifacts (LEAN ≈ 1.7 GB, HIGH-CONFIDENCE ≈ 4.9 GB),
excluding hap.py intermediates. Inputs are shared and not duplicated.

**Stopping criteria.** Stop a phase early when the top-`N` set is stable across
the last two BAM increments; abandon a candidate when its optimistic bound
(`J` plus the remaining-BAM upper bound) cannot reach the current `N`-th best;
abort the phase if failures exceed 5 % of evaluations (indicates an
infrastructure problem, not a scientific result).

## 10. Canary requirement

Before Phase A, exactly **one** end-to-end TRAIN canary must pass: one TRAIN BAM,
one known CONFIG, real GATK, real truth-aware evaluator, real hap.py, real
`AdvancedScorer`, real evaluation persistence, and an **independent score
recomputation** from the published metrics artifact.

It must prove the whole chain: execution result → evaluator → metrics → final
score → immutable evaluation record. Phase A may not begin until it passes.
The canary is L2-F2-C and is **not** run during the audit stage.

## 11. Persistence and evidence model

`evaluation.evaluations` is **not** the L2-F2 authority: it has four columns and
its foreign key targets the legacy `experiments.results`, not
`experiments.l2f_execution_results`. It must not be forced into service.

The minimal additive contract `0009_l2f_evaluation_results` **is implemented and applied**,
together with the `0010_l2f2_evaluation_corrective` corrective (exclusive XOR serialization,
composite metrics-artifact identity, and the narrow metrics registrar). DB-V2 remains abandoned;
this is not permission to revive it.

Minimum bindings for one immutable evaluation record:

| Binding | Purpose |
|---|---|
| `l2f_execution_result_id` + `result_hash` | exactly which execution was scored |
| `dataset_registry_id`, `partition` | which sample, which split |
| `truth_vcf_sha256`, `truth_tbi_sha256` | truth identity by content |
| `mutations_vcf_sha256` | mutation-only filtering input |
| `scorer_source_sha256`, `scorer_commit` | which scorer produced it |
| `happy_image_digest`, `bcftools_image` | container identity |
| `evaluation_contract_version` | semantics version |
| `metrics_artifact_sha256` | canonical content-addressed metrics document. Since `0010` the id, digest and media type are ONE composite foreign key against `catalog.artifacts(id, sha256, media_type)`, so the row cannot cite a document it did not produce |
| `minos_score_100`, `minos_score`, `admitted` | the score and its admissibility |
| `evaluation_hash` | domain-separated identity over all of the above |

Deliberately **not** normalised into SQL: the full hap.py metric set. A single
canonical, content-addressed metrics artifact is cleaner, keeps the schema small,
and preserves reproducibility; only the few fields the baseline and model stages
must *query* are promoted to columns.

Reuse `evaluation.dataset_evaluation_identity` (already present, per-dataset
truth/mutation SHAs, unique on `dataset_registry_id`) rather than duplicating
truth identity per evaluation row.

## 12. Promotion rules

1. A candidate is rankable only if **every** protocol-required evaluation exists.
2. TRAIN ranking uses frozen `J`; ties broken per §8.
3. The top `N_f` finalists promote to VALIDATION — `N_f` fixed before any
   validation read.
4. The baseline is the best finalist on VALIDATION under the **same** frozen `J`.
5. If VALIDATION contradicts TRAIN, that is **recorded**, not re-optimised.
6. TEST is never consulted.

## 13. BASELINE-QUALIFIED gate design (not implemented)

Bindings:

* HARNESS-READY gate hash `0e8411eb…` and qualification hash `b1d1cc5d…`;
* scorer source identity (commit + file SHAs) and hap.py/bcftools image identity;
* evaluation contract hash and evaluation schema revision;
* split snapshot identity; feature matrix identity;
* baseline-search **protocol hash**, **objective hash**, **candidate-design hash**;
* every TRAIN evaluation required by the protocol (by `evaluation_hash`);
* the VALIDATION evidence set;
* the selected baseline `config_hash` and its robust statistics;
* qualified git source and tree.

It must prove: TEST untouched; no post-hoc objective change (objective hash
frozen before the first evaluation timestamp); baseline reproducibility from
committed identities; and that **no failed evaluation was silently ignored**.

Uses the established two-commit pattern: a qualified **source** commit with green
exact-SHA CI, then a separate **evidence** commit containing only the gate and
qualification result, with the gate naming the qualified source rather than the
evidence commit.

## 14. Substages

| Substage | Entry | Deliverables | Tests | Exit |
|---|---|---|---|---|
| **L2-F2-A** | HARNESS-READY | evaluation contract + additive migration; evaluator login role; truth identity ingestion (TRAIN); isolated truth-aware evaluator | Tier 1 + Tier 2 (**positive** happy-path SQL tests mandatory) | evaluator persists a fixture evaluation; runner still truth-free |
| **L2-F2-B** | A complete | candidate design v1; frozen objective + protocol hash; racing engine | Tier 1 | protocol/objective/design hashes frozen and committed |
| **L2-F2-C** | B complete | one real TRAIN canary | Tier 3 (single run) | end-to-end chain proven with independent score recomputation |
| **L2-F2-D** | C passed | Phase A + Phase B TRAIN search | Tier 3 | influential dimensions identified by the pre-registered rule; survivors chosen |
| **L2-F2-E** | D complete | Phase C full-TRAIN confirmation | Tier 3 | finalists ranked on all 50 TRAIN BAMs |
| **L2-F2-F** | E complete, everything frozen | Phase D validation confirmation | Tier 3 (small) | baseline selected |
| **L2-F2-G** | F complete | BASELINE-QUALIFIED gate + evidence | Tier 1 + Tier 2 offline verification | gate issued, two-commit pattern |

**Expensive compute first occurs at L2-F2-C** (the single canary), and at scale
only from L2-F2-D.

## 15. Non-goals

Not in L2-F2: contextual/per-sample model learning (L2-G); the live controller
and `select_config` activation (L2-H); locked-test evaluation (L2-I); production
and delayed feedback (L2-J); any change to HARNESS evidence or the accepted 39
candidates; DeepVariant or bcftools as callers; DB-V2.

## 16. Protocol decisions — RESOLVED and FROZEN

| # | Decision | Frozen answer |
|---|---|---|
| D1 | Objective emphasis | **Absolute robust score is primary**; within-our-set rank is a diagnostic only. Competitor submissions are unavailable, so an internal rank cannot stand for the platform reward distribution. |
| D2 | Objective form | **Option B** — lower-tail CVaR + worst-chromosome floor + mean − failure penalty. |
| D3 | Robustness constants | **α = 0.25**, weights **0.50 / 0.30 / 0.20**, failure penalty **λ = 1.00**. |
| D4 | Runtime | **Not a weighted term.** Bounded 3600 s timeouts; mean GATK runtime is a tie-break only. hap.py runtime is excluded — it is offline evaluation infrastructure, not live inference. |
| D5 | Budget tier | **STANDARD** — a protocol maximum of **1215** evaluation pairs. Reuse and racing may only reduce it. |
| D6 | Validation timing | **L2-F2-F**, after Phase C is complete, TRAIN ranking is final and the four finalist hashes are frozen. Never in the canary or Phases A–C, and never deferred to L2-G. |
| D7 | Platform reward modelling | **No simulated opponent distribution.** Fabricating a competitor score distribution would optimise against an invented adversary. |
| D8 | Phase-B design family | **Deterministic mixed-domain Latin hypercube** over the six most influential dimensions, seeded by a domain-separated hash — no system RNG, clock, `hash()`, hostname or PID. |

The equation, tie-break, racing bounds, seed-control policy, phase sizes, failure-vs-missing
semantics and the TEST lock are all bound into `protocol_hash`
`c548e190571f5e964560cf30021a520ea8aad6674569fa3202af880d7dff77d1`.

These are now frozen into `l2f2-baseline-search-protocol-v1`, whose canonical manifest and
protocol hash are committed. Their authority comes from that committed protocol, its hash, its
tests and its evidence — there is no separate approval step and no approval artifact. Changing
any of them requires a NEW protocol version with a NEW hash; it cannot alter this one.
