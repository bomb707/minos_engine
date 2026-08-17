# Validator Twin — Scoring Contract

## Authoritative (implemented) — comparison metrics
Standard information-retrieval / hap.py definitions, recomputed from TP/FP/FN
with deterministic zero-denominator behavior (tested):

```
precision = TP / (TP + FP)     # 0.0 when TP+FP == 0
recall    = TP / (TP + FN)     # 0.0 when TP+FN == 0
F1        = 2·P·R / (P + R)    # 0.0 when P+R == 0
```
Computed per variant class (SNP, INDEL). When the raw result supplies its own
metric values, recomputed values must agree within `1e-9` or ingestion fails
closed. `total_calls = (SNP.TP+SNP.FP) + (INDEL.TP+INDEL.FP)` and is
consistency-checked. Ti/Tv and Het/Hom are carried through when present (≥ 0).

## Authoritative (implemented) — score inputs
`ScoreInputs` assembles: `snp_f1`, `indel_f1`, `mean_recall = (snp_recall +
indel_recall)/2`, `total_truth`, `total_calls`, `fp_total`. Domain: rates in
[0,1]; counts ≥ 0. These are the normalized inputs a scorer would consume.

## UNAVAILABLE — composite AdvancedScorer
`compute_score` returns:
```
status:      UNAVAILABLE
reason_code: AUTHORITATIVE_SCORER_NOT_AVAILABLE
final_score: null
components:  null
```
**Authoritative source check:** Overall spec §7 references calling the "pinned
AdvancedScorer" and Layer 2 §12 references "the official scorer objective," but
**no specification in this repository defines the AdvancedScorer formula,
weights, chromosome weighting, clipping, or normalization.** Per the Stage 1
mandate we do not invent it. When an authoritative scorer is provided (a later
stage), populate `components` + `final_score`, set `status = AVAILABLE`, name the
`scorer_identity`, and only then may a `VALIDATOR_CONFIRMED` parity be claimed.

There is **no invented fallback score**. A structural/fixture-replay Twin
qualifies at `FIXTURE_REPLAY` without claiming numerical parity.
