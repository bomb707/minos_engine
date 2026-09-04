# L2-G — expected-score model: training contract and dataset authority

Status: **source contract frozen; no training campaign run, no `MODELS-QUALIFIED` gate issued.**

Entry gate: `BASELINE-QUALIFIED` PASS,
`b9436bf3263925ebe187ed5550c7214cfa92bc75a0dd2607a7766103bfa6befa`, qualified source
`9395c116e22c52777441d76200acd96a738417bf`.

Training contract hash: `c29e089ece40b29e7d998814d9e1be175ac711cce2c53d1e98dc1fe740703c18`.

## 1. The learning problem

Given one BAM's production-eligible features and one canonical GATK configuration, predict what
the engine should expect if it chooses that configuration:

```
(X_BAM, theta_GATK)  ->  expected utility
```

At inference the controller has a BAM and a candidate config. It does **not** have truth,
mutations, hap.py output, a MINOS score, an admission outcome, an evaluation status, or knowledge
of what won previously. All of these are listed in `FORBIDDEN_AT_INFERENCE` and asserted against
the predictor matrix.

## 2. Target formulation — decided, not defaulted

The frozen objective treats a candidate failure as utility `0.0` **at the aggregation layer**. It
does not follow that a crashed GATK run should be handed to a regressor as a biological score of
zero. A run that failed produced *no score at all*; training on a fabricated `0.0` would teach the
model that such configurations produce genomically terrible calls, when in fact they produced
none. The 35 `GATK_NONZERO_EXIT` rows are execution evidence, not biological evidence.

**v1 got the conditioning variable wrong, and v2 corrects it before any model was fitted.**
v1 factorised over *GATK success*, which let all 1140 evaluated rows train the score regressor.
But 154 of those evaluations were **NOT ADMITTED** — 150 `ZERO_INPUT_FINGERPRINT`, 4
`NONPOSITIVE_SCORE` — and the frozen objective does not treat a non-admitted evaluation as
utility. Roughly one in eight score-training rows would have carried a label the scoring authority
refuses to call utility. GATK exiting zero is not the same event as the subnet admitting the
result.

v2 therefore freezes **formulation B, joint expected utility over ADMISSION**:

```
P(admitted | X, theta)                -- admission model, every decided outcome
E[score | admitted, X, theta]         -- score model, ADMITTED examples only
E[utility] = P(admitted) * E[score | admitted] + (1 - P(admitted)) * 0.0
```

Both a GATK crash and a non-admitted evaluation are admission-negatives contributing utility
`0.0`; neither is ever a score-regression label. They keep distinct codes
(`execution_failure_code` vs `admission_code`) because they are different physical events, and a
non-admission may not be recorded as a crash.

The `0.0` is taken from the frozen aggregation semantics, not invented here. Formulation A
(score-only with a separate failure model) is B without the combination step, so B subsumes it and
is what a controller actually needs. An `INFRASTRUCTURE_INCIDENT` is never a label in either
component — it is our defect, and a model that learns from it learns about our infrastructure.

`admitted` is therefore the conditioning event itself, not a diagnostic. An
`INFRASTRUCTURE_INCIDENT` remains a label in neither component — it is our defect, and a model
that learns from it learns about our infrastructure.

## 3. Data reality — and why it constrains model capacity

| quantity | value |
|---|---|
| TRAIN BAMs | 50 (10 per chromosome, chr18–chr22) |
| terminal job rows | 1175 |
| **unique (BAM, config) cells** | **1040** |
| repeated cells | 115 (95 twice, 20 three times) — 135 surplus rows |
| unique configs | 80 |
| full BAM×config matrix | 4000 cells |
| **true observed sparsity** | **74.0%** (1040 / 4000) |
| examples per BAM | 10 – 80 |
| **admission-model examples** | **1040** = 861 ADMITTED + 149 NON_ADMISSION + 30 EXEC_FAILURE |
| **score-model examples** | **861 ADMITTED only** |
| BAMs with no ADMITTED example | **0** |
| non-admissions (row level) | 154 = 150 `ZERO_INPUT_FINGERPRINT` + 4 `NONPOSITIVE_SCORE` |
| execution failures (row level) | 35 `GATK_NONZERO_EXIT` |
| between-BAM SD of mean score | **0.1909** |
| between-config SD of mean score | **0.1663** |

Two facts drive every design decision below.

**1175 rows are not 1175 independent samples, and they are not even 1175 distinct cells.** The
phases overlap: 115 (BAM, config) pairs were scheduled more than once, so the learning table
collapses to **1040** cells under `ONE_EXAMPLE_PER_BAM_CONFIG_PAIR`. Collapsing is safe here
because the repeats were checked and agree: 0 conflicting outcome class, 0 conflicting admitted
score, 0 conflicting execution environment. Beyond dedup, the 1040 cells still come from only 50
BAMs, heavily unbalanced — Phase-A's 5 BAMs carry up to 80 examples each, Phase-C's 50 carry ten
— so `EQUAL_BAM_TOTAL` gives every BAM the same total loss weight. Without it the model would fit
whichever BAMs the scheduler favoured, not the population.

**The BAM matters more than the config.** Between-BAM spread (0.191) exceeds between-config
spread (0.166). A model can score well by learning "which BAM is this" and never learning
anything about configuration choice — which is exactly what the controller needs it to learn.
Grouped CV is not a formality here; it is the only thing that distinguishes the two.

## 4. CV protocol

BAM-grouped, chromosome-held-out, five folds — one per chromosome, 10 BAMs held out each time.
The grouping unit is the **BAM, never the row**: one BAM contributes up to 97 rows, and splitting
them puts the same features on both sides of a fold.

Every learned transform (standardisation, imputation, category vocabulary) is fitted **inside the
fold's training side only**. No global fit precedes CV. The final TRAIN model fits transforms on
all 50 TRAIN BAMs and no others.

The config encoder needs no fold-local fitting at all: it scales by the **frozen parameter space's
own bounds**, which are a property of the search space rather than of the sampled rows.

## 5. Representations

**BAM features** — production-eligible only, from feature registry
`0d8612707c6673060546511d8f5e8d1ba47048ef440e6c2dcf238fdc297f6e0c`: 285 records, of which 147
`ELIGIBLE`, 60 `CONDITIONAL`, 2 `RESEARCH_ONLY`, 76 `FORBIDDEN` (including all 6 truth-derived).
The registry's 141 production-eligible fields are **not** the training matrix. The matrix that was
actually built, qualified and persisted is the frozen **129-column** set
`7e867dfa5633044b69869be8a87fac564431a73a183aa0ab0b1b13158a7c176f`
(`EXPECTED_COLUMN_COUNT = 129`), and a dataset presenting 141 columns is refused. Promoting the
extra 12 would be a feature-promotion event requiring the matrix to be re-qualified, not a
training-time choice. `FORBIDDEN` and `RESEARCH_ONLY` fields never enter the predictor.

Feature **values**, not just names, are bound: each of the 50 BAMs contributes a
`BamFeatureBinding`, so a silently changed predictor value moves the dataset identity. `chromosome` stays METADATA for CV grouping — as a feature it is an invitation
to memorise.

**Config features** — encoder identity
`3053fed09a1a7fdc9462a963871564275c88e4eca5fe3a898d2d6821c36b1fe4`. 25 frozen parameters (14 int,
7 float, 2 enum, 2 bool) → **28 variable columns**. Enums are one-hot over their frozen
vocabulary, never ordinal: `CONSERVATIVE` is not "greater than" `AGGRESSIVE`. Two parameters are
**fixed** by the space to a single value (`sample_ploidy` = 2, `dont_use_soft_clipped_bases` =
false); they are recorded in the schema as fixed and excluded from the variable input rather than
contributing constant columns.

## 6. Model families and references

Ordered by capacity, lowest first: `CONSTANT_SAFE_BASELINE`, `GLOBAL_MEAN`, `CONFIG_ONLY`,
`BAM_FEATURES_ONLY`, `LINEAR_REGULARIZED`, `TREE_ENSEMBLE`, `COMPACT_MLP`.

The first four are references a contextual model must **beat**, not formalities. Given 50
independent BAMs and a dominant BAM effect, a high-capacity model can memorise 50 contexts and
report a flattering number. On a scientific tie the simpler qualified model wins.

## 7. Metrics

Accuracy is diagnostic; **decision quality** is the target. `regret = oracle_utility −
selected_utility`, lower is better — orientation frozen before any result exists. Regret considers
only configs actually run for that BAM, because 74.0% of the matrix was never measured.

Reported: MAE, RMSE, R², Spearman; BAM-grouped top-1 regret, oracle gap, worst-BAM regret,
per-chromosome regret, fraction beating `SAFE_BASELINE`; downside as mean / max / CVaR-0.25
regret and catastrophic-regression count. Failure-risk: prevalence, Brier, log loss, calibration
by predicted-risk bin — reported at fold level, since 35 failures across 10 BAMs and 3 configs
cannot support strong precision claims. Calibration uses **OOF** predictions only.

**A model is not acceptable because its average improved.** One that raises the mean while
introducing severe tail loss must not be promoted.

## 8. VALIDATION policy — and its hard limit

TRAIN-only CV completes first; candidate specs, the selection metric and the tie-break are frozen;
only then are frozen candidates scored on VALIDATION. No feature added because it helped
VALIDATION, no target change, no expanded HPO, no transforms fitted on it, no merging it into
training at this stage.

**Phase D evaluated only the four frozen finalists.** VALIDATION therefore carries labels for four
configs on ten BAMs — not for the L2-G config domain. It can legitimately assess score prediction
and relative ranking *among those four*, calibration under domain shift, and comparison against
`SAFE_BASELINE`. It **cannot** validate arbitrary unseen configurations, and no new VALIDATION
GATK run is authorised to change that.

## 9. Artifacts

`l2g-training-dataset-v1` (identity is **row-order independent**: a sorted digest of per-row
identities), `l2g-cv-manifest-v1` (deterministic, no randomness, no row-level split),
`l2g-model-spec-v1` (hashed **before** fitting, so the candidate set is frozen rather than
discovered), `l2g-model-bundle-v1` (every artifact by SHA-256 — no opaque pickle).

## 10. Exit

`MODELS-QUALIFIED`, 34 required checks across ENTRY / DATA / CV / MODEL / PERFORMANCE /
VALIDATION / ISOLATION / BUNDLE. **Designed here, not issued.**

TEST remains sealed until L2-I. `Layer2Service.select_config` remains blocked until L2-H.

## 11. Training protocol authority — `l2g-model-training-protocol-v1`

`src/minos_engine/models/protocol.py` freezes the decision procedure itself, hash
`607aa46e864808c6c19a0cf7ec2b1e7b5c415f9080ce235c9e62c4b3da8f82d1`. It binds the training
contract hash, the 129-column feature-set hash, the config encoding, the target formulation, the
dedup and weighting policies, the CV rules, the candidate and reference sets, the metric
definitions, the regret orientation (`ORACLE_MINUS_SELECTED_LOWER_IS_BETTER`), CVaR α = 0.25, the
selection order, the VALIDATION limitation and the TEST lock.

**The candidate set is finite and named before fitting.** Six specifications across the three
promotable families, no adaptive search. With 50 independent BAMs, an open-ended hyperparameter
search would find whatever the folds happened to reward.

**Two-stage threshold rule.** Numeric promotion thresholds cannot honestly be fixed before any
out-of-fold number exists, so the protocol freezes the *procedure* instead:

- **Stage 1** — TRAIN OOF may derive the shortlist and thresholds, using only the predeclared
  formula: shortlist = promotable specs whose OOF mean regret ≤ the best reference's mean regret
  **and** whose OOF CVaR-0.25 regret ≤ the best reference's CVaR regret.
- **Stage 2** — a separate source freeze binds the exact shortlisted `ModelSpec`s and the exact
  resulting thresholds **before the first VALIDATION score is read**.

The invariant is `VALIDATION_NEVER_CHOOSES_THE_RULES_USED_TO_JUDGE_IT`. TEST stays sealed
until L2-I.

**Backend.** scikit-learn `>=1.5,<2` (verified 1.9.0, Python 3.12, joblib), CPU only. It was
chosen because `EQUAL_BAM_TOTAL` requires `sample_weight` to be genuinely honoured, which was
verified empirically rather than assumed from the signature. No GPU stack is introduced for 50
independent BAMs.

**Bundle identity is host-independent.** `ArtifactRef.scientific_identity()` excludes the
filesystem `path`: the same artifact bytes stored under two different absolute directories are
the same scientific object.
