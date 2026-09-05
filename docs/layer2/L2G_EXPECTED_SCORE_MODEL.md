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

## 12. Pre-fit authority closure

`TrainingDataset` is a strict type, and a strict type is not an authority: a caller could hand it
an internally consistent set of hashes, fifty plausible BAM ids and 1040 well-formed rows and get
back a scientifically foreign table that validated perfectly. Four things closed that.

**One accepted builder.** `models/training_data_authority.py` derives the science; the caller
supplies only an authenticated TRAIN connection and an operational engine. It nominates no
outcome, score, weight, column, member, plan or config payload.

**The exact columns, not the count.** A dataset must present `AUTHORITATIVE_COLUMNS` in its
qualified order. 129 invented names carrying the correct `feature_set_hash` are refused — that
hole was real and is now a regression test.

**The frozen fifty, not a valid shape.** The CV manifest is bound to `build_train_schedule()`.
Synthetic BAM ids in a perfect 10/10/10/10/10 shape are refused, and every row's chromosome must
agree with the manifest.

**Dedup derived, not audited.** The builder starts from all 1175 terminal evidence rows, groups by
`(dataset_id, config_hash)` and requires repeats to agree on outcome, admission code, admitted
score, execution environment and parameter space before collapsing. There is no newest-row rule,
no phase preference and no averaging: a genuine conflict means two runs of one cell disagree, and
the honest response is to stop.

### The real freeze

| | |
|---|---|
| dataset schema / hash | `l2g-training-dataset-v3` / `d031758c58358270843b9b417ea034d1181a6aaafc1c94af000279c26dc62fcc` |
| CV manifest hash | `b441b15fdc185e62e243b93322d6c30d8787f49f9fafbb3dab6ac9371728d92f` |
| scientific cells | **1040** from 1175 terminal jobs (925×1, 95×2, 20×3 → 135 surplus) |
| outcome classes | 861 ADMITTED, 149 NON_ADMISSION, 30 EXEC_FAILURE |
| configs / BAMs | 80 / 50 (10 per chromosome), 0 BAMs without an admitted example |
| conflicts | **0** — every repeated cell agreed on all five fields |

TRAIN was read once through an ephemeral SECURITY DEFINER surface, which was then dropped; the
scientific state and the privilege set were proven identical before and after.

### Versioning, honestly

`l2g-model-spec-v1` and `l2g-model-bundle-v1` carried materially different semantics (the bundle's
identity once included the filesystem path). No artifact was ever produced under them, so both are
`SUPERSEDED_BEFORE_FIRST_MODEL_FIT` rather than migrated; the same applies to the training dataset
schema, which is now v3. Two different meanings must not share one schema string.

### Runtime

`scikit-learn==1.9.0` exactly, with numpy 2.5.2, scipy 1.18.1, joblib 1.6.0 on Python 3.12.3. A
range is not a scientific runtime — a model fitted under 1.5.0 and one fitted under 1.9.0 are not
the same experiment — and `models/runtime.py` refuses to fit under anything else.

### Calibration

`ISOTONIC_ON_TRAIN_OOF_ONLY` was under-specified in a way that leaks. Fitting isotonic on the
outer OOF pairs and then reporting calibration and regret on those same pairs uses each held-out
chromosome's own labels to build the mapping it is scored against. Calibration is therefore
**nested**: within each outer fold, inner BAM-grouped out-of-fold pairs are drawn from the 40
training BAMs, the mapping is fitted on those only, and it is applied to the untouched held-out
chromosome. Frozen before any OOF number exists, so it cannot be chosen after seeing which variant
scores better.

## 13. The TRAIN OOF runner

### ModelSpec v3 — the confirmed misdescription

v2 recorded `failure_risk_formulation = "LOGISTIC_P_ADMISSION"` for all six candidates, but the
frozen grid names a classifier per family: `LogisticRegression` for the linear candidates,
`HistGradientBoostingClassifier` for the trees, `MLPClassifier` for the neural one. Four of the
six specs described an estimator they would never fit. v3 gives the two heads separate fields —
`score_model_implementation` / `admission_model_implementation`, with their own hyperparameters
and losses — so a spec can no longer hide two estimators inside one string. The scientific event
is unchanged: still `P(ADMITTED | X_BAM, theta)`. Nothing was fitted under v1 or v2; both are
`SUPERSEDED_BEFORE_FIRST_MODEL_FIT`.

### Bounded output

The admitted score lives in [0, 1]; Ridge and MLP regression do not. `score_output_postprocess =
CLIP_TO_0_1` is frozen on every spec **before** any out-of-fold number exists, so it cannot be
chosen after seeing which variant scores better. Utility is then `p * s` with both factors in
[0, 1], because `FAILURE_UTILITY` is 0.

### Nested calibration, concretely

Per outer fold: hold one chromosome (10 BAMs) out; within the remaining 40, run four inner
BAM-grouped folds to produce inner out-of-fold admission probabilities; fit isotonic on **those**
pairs only; fit the base admission estimator on all 40; predict raw `P(A)` on the held-out
chromosome; apply the inner-fitted mapping. The held-out chromosome's labels never reach the
calibrator applied to them, and every emitted record carries the calibration BAM-set identity so
the claim is checkable rather than asserted. Score regression is not calibrated at all.

### Failure policy

A convergence warning, a numerical exception, a non-finite prediction, a single-class admission
fold or a degenerate calibration are each `TRAINING_FAILURE` for that spec and fold, recorded as
campaign evidence. They are never reinterpreted as genomic candidate failures, and a candidate
must not win a comparison by having fewer folds counted against it.

### Enforcement, not declaration

The runtime content has always said `SINGLE_THREADED_DETERMINISTIC`. Setting `OMP_NUM_THREADS`
after numpy and scikit-learn are imported changes nothing — on this machine both report 16 threads
at import. Fits now run inside a `threadpoolctl` context that re-limits the loaded pools to 1 and
observes the result; outside it the verifier refuses. The runtime hash is unchanged because the
claim was already part of its content; only the implementation caught up.

### What the bundle must earn

Before an estimator sees a number: the four bundle files are hashed from their own bytes, the
`TrainingDataset` is reconstructed and required to hash to `d031758c…` (the hash written inside
the manifest is never the authority), the qualified matrix parquet is re-hashed and its 129
columns checked in their qualified order, and all 80 config payloads must hash to the names they
are stored under before being encoded through the accepted encoder.

### Selection

Regret is `oracle − selected` over configs **actually observed for that BAM**, lower better, with
the BAM as the unit of selection. Ties are broken by the lowest config hash lexicographically,
never by dict, database, phase or runtime order. The shortlist rule is the already-frozen one: a
promotable spec enters iff its OOF mean regret **and** CVaR-0.25 regret are both no worse than the
best reference's. If nothing clears both bars the shortlist is empty, MODELS-QUALIFIED holds, and
SAFE_BASELINE remains the fallback — promoting the least-bad contextual model would be choosing a
threshold after the fact.

## 14. Execution semantics — four defects that could have moved the shortlist

Each of these left every individual component looking correct while changing which candidates
could be promoted. All ten ModelSpec hashes and the four protected identities are unchanged: this
was implementation catching up with semantics that were already frozen.

**CVaR took the wrong tail.** The metric used `round(alpha * N)`. Python's banker's rounding turns
`0.25 * 50 = 12.5` into **12**, so the "CVaR-0.25 regret" averaged the 12 worst BAMs while the
frozen baseline objective's CVaR takes `ceil(alpha * N)` — **13**. A robustness measure that
quietly takes a smaller tail is a different measure wearing the same name.

**The safe baseline selected the wrong config.** Selection was "highest predicted utility, ties to
the lowest config hash". `CONSTANT_SAFE_BASELINE` predicts the same value for every config, so it
tied everywhere and selected the *lexicographically lowest* config — not `157d88d1…`. That is a
different model, and it was the promotion bar. Selection is now bound per family:
`CONSTANT_SAFE_BASELINE` always returns its own config (and the campaign holds if that config was
never observed for a held-out BAM); `GLOBAL_MEAN` and `BAM_FEATURES_ONLY`, which genuinely cannot
distinguish configs, keep the lexicographic tie-break; everything else selects on predicted
utility. Policy is never inferred from prediction equality.

**Two references contradicted their own specs.** `CONFIG_ONLY` and `BAM_FEATURES_ONLY` are hashed
under `admission_probability_calibration = NESTED_CROSS_FITTED_WITHIN_EACH_OUTER_FOLD` but
returned raw `predict_proba`. The implementation now performs the nested calibration its spec
declares — the alternative, editing the specs to say "no calibration", would have moved a frozen
hash to match a convenient implementation. They also hard-coded `random_state=0` against a spec
carrying the frozen campaign seed; they now use `RANDOM_SEED`.

**Incomplete models were metricised.** A failed fold was recorded and the run continued, so a
four-fold candidate could be compared against five-fold references. A spec is now COMPLETE only
when all five outer folds succeeded and every one of the 1040 cells was predicted exactly once
across all 50 BAMs. An incomplete candidate is recorded as evidence and is INELIGIBLE; an
incomplete *reference* means the promotion bar was never fully observed, which raises
`ReferenceThresholdUnavailable` and holds the campaign. Dropping the failed reference and taking
the best of the rest would silently lower the bar — the one direction a threshold must never move
by accident.

### The campaign boundary

`run_l2g_train_oof_campaign` is the single production entry point: it verifies the authority, the
bundle, the matrix bytes, the config payloads and the runtime; derives the ten frozen specs;
proves completeness; and derives the shortlist only from complete promotable candidates against a
fully observed reference set. The caller nominates no spec subset, fold subset, metric, threshold,
exclusion or shortlist. Inner single-class folds and empty inner folds are now `TRAINING_FAILURE`
rather than skipped, and prediction — not just fitting — runs inside the single-threaded context.

The campaign result schema is `l2g-train-oof-campaign-result-v2`; v1 is
`SUPERSEDED_BEFORE_FIRST_CAMPAIGN`, since binding per-spec completeness materially changed what an
accepted campaign asserts.

## 15. The sealed campaign authority

`run_l2g_train_oof_campaign` was a configurable core wearing a production name. It accepted the
dataset, the design matrix, both spec sets and both fit callables, so it could not promise which
experiment had run — a caller with those parameters can supply a different one.

The injectable function is now `_run_l2g_train_oof_core`, kept private because dependency
injection is what makes the structural properties testable on a small synthetic grid. The
production entry is `run_real_l2g_train_oof_campaign`, whose entire signature is four operational
handles: `feature_matrix_artifact_path`, `workspace`, `config_payload_root`, `root`. Each is
verified against an identity the caller does not control, so pointing at the wrong file fails
rather than substitutes. It derives the authority, dataset, matrix values, config vectors, design
matrix, both spec sets and the runtime itself, and uses the committed `fit_fold_estimators` and
`fit_reference_fold`.

**Exact identities, not counts.** The ten spec hashes are required to be exactly the accepted
ones, in order, each under its expected family, and to match what the committed authority records.
Six foreign specs, a correct count with one wrong hash, a duplicate, a reordering, or a correct
hash under the wrong family are all refused.

**The exact cell set.** Counting 1040 records cannot detect one frozen cell missing and one
foreign cell substituted — the total is unchanged. Completeness now compares the predicted
`(dataset_id, config_hash)` set against the frozen one, and `exact_cell_set_verified` is part of
what COMPLETE means.

**Thread evidence is observed, not accepted.** The boundary takes its own reading from the loaded
pools under the enforced context and refuses if nothing is observable or any pool is unlimited. A
caller-supplied `thread_report` is not evidence and is no longer accepted.

### Campaign result v2, which now binds what it claims

The schema said it bound per-spec completeness; the canonical content did not serialize it. It now
carries, for each of the ten specs: hash, family, role, status, expected and successful fold
counts, failed folds, expected and observed record counts, unique BAM count, duplicate cell count,
`exact_cell_set_verified`, training failures, and — only when COMPLETE — the OOF and metric
artifact hashes. Campaign-level it binds the authority identities, source commit and tree read
from Git, reference completeness, threshold availability, the eligible/ineligible partition, both
reference thresholds, the shortlist, the thread report, and `validation_read` / `test_accessed`.

`build_campaign_result` derives every field from the verified closure — the operator supplies no
shortlist, threshold, eligibility or status — and `verify_campaign_result` checks the document
against itself: COMPLETE implies five folds, 1040 records, 50 BAMs, no duplicates, a verified cell
set and both artifact hashes; a failed spec cannot carry a scientific artifact or be shortlisted;
a shortlist cannot exist unless all four references completed.

On swaps, precisely: duplicated artifact hashes are caught by the verifier, and an exchanged
*metric* hash is caught at build time because the identity binds the spec and is recomputed from
that spec's own metrics. An OOF hash exchanged inside an already-built result cannot be re-derived
from content alone — the records are not in the document — so what protects it is the
domain-separated result identity, which moves on any edit.

## 16. Campaign evidence: retained, published, re-derivable

The campaign hashed its OOF records and then discarded them, leaving an `oof_artifact_hash` with
nothing behind it — a claim about evidence that no longer existed. And `build_campaign_result`
took a plain dictionary, so an operator-authored campaign was publishable.

**The evidence survives.** `TrustedL2GTrainCampaign` holds the actual `OofRecord` collection and
metrics for every COMPLETE spec until publication succeeds. It is minted with a module-private
token, so only `run_real_l2g_train_oof_campaign` can create one; a dictionary is whatever its
author typed and can never become publishable. The production entry now returns this object rather
than a naked mutable dict, and `closure` hands out a defensive copy.

**Publication derives everything.** `write_l2g_train_campaign_outputs(trusted, *, output_dir)`
writes one `l2g-oof-artifact-v1` and one `l2g-metric-artifact-v1` per COMPLETE spec, atomically
through temp files at `0640` under a `0750` directory, then computes every recorded hash from the
bytes that were actually written. A failed publish deletes what it wrote: a half-published
campaign is worse than none, because it looks like evidence. Failed specs get failure evidence and
no scientific artifact.

Two identities are kept distinct: the domain-separated SCIENTIFIC identity is what makes an
artifact *the* artifact; the FILE SHA-256 is what makes those particular bytes the published ones.

**The result can be checked by someone who trusts nobody.** It binds per-spec `promotion_metrics`,
so `verify_campaign_result` re-runs the frozen two-bar rule from the document itself rather than
believing a recorded shortlist — a shortlist edit fails even after the identity is recomputed. It
requires the *exact* accepted authorities and the *exact* ten spec identities and families, not
merely well-formed 64-hex strings. `verify_campaign_result_source` asks Git whether the recorded
commit exists and whether its real tree is the recorded one. `load_and_verify_oof_artifact` and
`load_and_verify_metric_artifact` re-derive each artifact's identity from its bytes, which is what
makes a file swap detectable offline.

### Frozen output layout

```
<minos root>/minos_l2g_train_oof/      0750
  campaign-result.json                 0640
  oof/<spec_hash>.json                 0640
  metrics/<spec_hash>.json             0640
  failures/<spec_hash>.json            0640   (only when a spec failed)
```

## 17. Pre-execution publication integrity

Four things could still have gone wrong between fitting the models and having evidence anyone
could check.

**The pre-fit authority was only checked for shape.** `prefit_authority_sha256` had to be
lowercase 64-hex, which any other authority also is. It is now required to equal
`61d8b33432202c1813a3d64d37bb727f8f1b8012ef1af23c7bf7af0ef8356000` exactly, and
`verify_prefit_authority_bytes` re-hashes the committed file so a campaign cannot cite an
authority whose bytes have since moved.

**Provenance was read too late.** `build_campaign_result` called Git at publication time, so a
later checkout could be relabelled as the one that fitted the models. `run_real_l2g_train_oof_campaign`
now captures HEAD and its tree *before the first fit* — requiring the commit to exist, its tree to
match, and the worktree to be clean — and stores them in the trusted campaign. Publication re-reads
Git and refuses if the checkout moved.

**Published evidence was still a plain dictionary.** A caller holding a real trusted campaign could
hand the builder any file hashes it liked. `TrustedL2GPublishedEvidence` is now minted only by the
write/readback boundary, with its own module-private token, and `build_campaign_result` refuses
anything else.

**Two functions shared one identity domain.** `oof_runner.oof_artifact_identity` (the record set)
and the file-wrapper hash both used `minos:l2g-oof-artifact:v1`, so "the OOF identity" meant
whichever function you happened to be reading. The record-set function keeps the frozen domain and
remains THE scientific identity; the wrapper moved to `minos:l2g-oof-evidence-wrapper:v1` and
carries the scientific hash as `scientific_oof_hash`. The offline verifier replays the published
records through the same one definition, so core, file and reload agree by construction.

### Write, then read back

`_write_atomic` now re-opens the final path and hashes what is actually there. Hashing the
in-memory buffer proved only what was intended to be written; everything downstream treats that
SHA as a fact about the file.

### Staged publication

Evidence is written to `minos_l2g_train_oof.tmp.<pid>.<nonce>`, every file is read back and
semantically verified, the result is built, re-read and re-verified, the whole tree is checked by
`verify_published_l2g_train_campaign`, and only then is the staging directory renamed into place.
An existing final target is refused rather than overwritten, and any failure removes the staging
tree — a half-published campaign is worse than none, because it looks like evidence.

### Immutable retained evidence

Records are snapshotted by value at mint and handed out as deep copies, so mutating what an
accessor returned cannot reach the trusted state. Identity continuity is then checked explicitly:
the retained records must still hash to what the campaign core earned, before anything is written.

## 18. Campaign v1 — the first real TRAIN OOF result

The first real campaign ran at commit `c9618bcda752cb2e1c7faa4d5fced92c62db326f`, tree
`30ef183f7a8a0ecc435d189d6bf6f9b745bb1648`. Fitting took 21 seconds; publication 2 seconds. All
ten specs COMPLETE: 5/5 folds, 1040/1040 cells, 50 BAMs, no duplicates, exact cell set verified,
zero training failures. Campaign result `eddc30a1…` (file `4ac6500f…`, 12031 bytes), whole-tree
verifier PASS.

### The bar, and who set it

| reference | mean regret | CVaR-0.25 regret |
|---|---|---|
| **CONSTANT_SAFE_BASELINE** | **0.022133686444521378** | **0.07825312618460009** |
| CONFIG_ONLY | 0.11766491271393287 | 0.2566942527744037 |
| GLOBAL_MEAN | 0.18289165873748922 | 0.485555374714231 |
| BAM_FEATURES_ONLY | 0.18289165873748922 | 0.485555374714231 |

Both bars were set by `CONSTANT_SAFE_BASELINE` — the fallback the campaign existed to beat turned
out to be the hardest thing in the field. `GLOBAL_MEAN` and `BAM_FEATURES_ONLY` tie exactly,
which is what the frozen semantics predict: neither can distinguish configs, so both fall back to
the same lexicographic tie-break and select the same config for every BAM.

### The candidates

| family | spec | mean regret | CVaR regret | shortlisted |
|---|---|---|---|---|
| TREE_ENSEMBLE | `5e8f905c…` | 0.03060170417930549 | 0.10655015138878801 | no |
| TREE_ENSEMBLE | `32539b32…` | 0.03465081169910364 | 0.12073428266941989 | no |
| LINEAR_REGULARIZED | `2328e0c1…` | 0.1018574215044056 | 0.23280747766230175 | no |
| LINEAR_REGULARIZED | `c8ff4aa8…` | 0.11729779935954486 | 0.28416294949094595 | no |
| LINEAR_REGULARIZED | `e962fb55…` | 0.11756632226573807 | 0.28416294949094595 | no |
| COMPACT_MLP | `4e7f488e…` | 0.172966229632268 | 0.4796503886113579 | no |

**Shortlist: empty.** All six were eligible; none cleared either bar.

The gap is not a modelling accident worth explaining away. The best candidate,
`5e8f905c…`, has genuinely the strongest diagnostics in the field — R² 0.499, Spearman 0.693,
Brier 0.0039, 56% zero-regret BAMs, 2 catastrophic regressions against the safe baseline's 0 — and
still selects a worse config than "always use the qualified baseline" often enough to lose on both
bars. Predicting the score well is not the same as choosing the right config, and this campaign is
the first evidence of how far apart those two things are here.

### What this is, and what it is not

The frozen outcome is `NO_CONTEXTUAL_MODEL_QUALIFIED_ON_TRAIN`, deliberately not
`MODEL_TRAINING_FAILED`. Training did not fail; every model fitted, every fold completed, every
cell was predicted exactly once. The promotion *hypothesis* failed under the criterion frozen
before any number existed.

Because the shortlist is empty there is no frozen contextual candidate for VALIDATION to select
among, so `validation_authorized_for_campaign_v1 = false`: opening VALIDATION now could only serve
to rescue a model the TRAIN criterion rejected. SAFE_BASELINE remains the fallback and
MODELS-QUALIFIED holds at `HOLD_NO_TRAIN_PROMOTABLE_MODEL`.

The record is frozen at `reports/layer2/l2g-train-oof-campaign-freeze-v1.json`, identity
`1c2039dec2f3fbb51a8058c947bbf8de9f9c6d235a133b5948aa6b33ac516673`.

Continuing under campaign v1 is not available: it is complete and frozen. The decision is either
to accept SAFE_BASELINE and stop contextual-model promotion, or to authorize a new versioned
TRAIN-only research protocol — which would be a fresh frozen campaign, not an adaptive
continuation of this one.

## 19. L2-G v2 — relative advantage over the four finalists

Campaign v1 is closed. v2 is a NEW research protocol, not a continuation: nothing here relaxes a
v1 threshold, and v1's best tree model is motivation, not a qualified result.

### The hypothesis

v1 asked each model to predict `U(BAM, config)` for any of 80 configs and pick the best. It
predicted the score creditably and still chose worse configs than the safe baseline. Most of the
signal in `U` is BAM difficulty, which is common to every config and cancels in the decision. So
v2 learns the quantity the decision actually turns on:

```
DELTA(i, theta) = U(i, theta) - U(i, theta_safe)
```

A model that predicts BAM difficulty perfectly and advantage not at all now scores zero.

### Why exactly four configs

The VALIDATION cohort carries outcome labels for exactly the four Phase-D finalists on its ten
BAMs. A policy free to select any of the 80 TRAIN configs could never be checked end-to-end
without running new VALIDATION executions. Restricting the action domain is what makes the later
one-shot VALIDATION evaluation honest. The four were re-derived through the accepted Phase-C
finalist freeze (`540aeca0…`), not copied.

### The feasibility audit — TRAIN only, before any v2 fit

The four-finalist slice of TRAIN is **completely dense**: 200/200 cells, all ADMITTED, no
execution failures and no non-admissions. So there is no missing-data policy to argue about.

| | |
|---|---|
| ORACLE4 mean gain over safe baseline | **0.014976328450755626** |
| median gain | 0.0 |
| max gain | 0.18246926709464156 |
| safe-baseline four-domain CVaR-0.25 regret | 0.05608717452333845 |
| zero-regret fraction | 0.68 |
| safe baseline strictly best | **34 / 50 BAMs** |
| another finalist better | 16 / 50 |

| alternative | win/tie/loss | mean DELTA | mean when winning | mean when losing |
|---|---|---|---|---|
| `0972930f…` | 8/0/42 | −0.1038 | +0.0611 | −0.1352 |
| `22a1f1fd…` | 5/0/45 | −0.0530 | +0.0104 | −0.0601 |
| `4251cb85…` | 6/0/44 | −0.0944 | +0.0353 | −0.1120 |

**This is the number that matters: a perfect oracle over these four configs would gain 0.0150 mean
utility.** That is the entire ceiling. Switching is right on only 19 of 150 opportunities, and the
average loss when wrong is several times the average gain when right. A policy that switches on
prediction noise is strictly worse than never switching.

The audit is descriptive TRAIN evidence. It does not set a promotion threshold, and no threshold
was derived from it.

### What is frozen

The switch rule is one family, chosen before any fit: switch to the highest predicted advantage
**only if it exceeds a margin**, where the margin is the `margin_quantile`-th quantile of the
model's own inner out-of-fold `|residual|`, fitted on outer-training BAMs only and applied
untouched to the held-out chromosome. The safe baseline is always available and no policy is ever
forced to switch. "Switch whenever predicted advantage is positive" was considered and rejected on
the audit above — with this loss asymmetry it would switch on noise.

Four candidate specs: Ridge and HistGB over `[X_BAM(129) | encode(theta) − encode(theta_safe)(28)]`,
each at margin quantiles 0.75 and 0.90. No MLP, no deep model, no HPO library — v1's failure was a
decision failure, not an accuracy failure, and 50 BAMs do not support more capacity.

The promotion bar is `ALWAYS_SAFE_BASELINE`, a deployable reference: mean regret **and** CVaR-0.25
regret must both be no worse. `ORACLE4` is recorded as an upper bound only — it needs the held-out
answer and can never be deployed, so promoting against it would measure against something nobody
can run.

Because v2's design was informed by v1 TRAIN evidence, its own TRAIN OOF is declared
`DEVELOPMENT_EVIDENCE_FOR_THIS_PROTOCOL` — not an untouched estimate of how well the protocol
design generalises. VALIDATION stays unread unless v2 freezes at least one promotable selector.

Contract `dd5aca80…`, dataset `4a8f2777…`, protocol `835d7acf…`, domain `11f71243…`.
