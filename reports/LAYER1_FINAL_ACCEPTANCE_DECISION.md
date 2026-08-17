# Layer 1 — Final Acceptance Decision (owner record)

**Date:** 2026-08-17 · **Runtime:** CPython 3.12.x · **Decision authority:** repository owner.

This record documents the owner acceptance decision on Layer 1 and authorizes the
Layer 2 pre-implementation audit. It does **not** modify, delete, or relabel the
Layer 1 v2 evidence, whose honest verdict remains **INCOMPLETE**.

## Decision
```
Layer 1 measurement correctness : ACCEPTED
Layer 2 progression             : APPROVED_WITH_EXCLUSIONS
```

## Accepted Layer 1 identities (immutable)
| Identity | Value |
|---|---|
| PROTOCOL-READY | `b9cda0bab329b36a0a62b4b7e9ba9b797fc22b46c1055f76db26b591311a1675` |
| TWIN-READY | `3464fb7604fd6b18d9008bafa9e3cf1ad18d7d440d22806bbe043a11d3e9b22a` |
| L1-READY | `aeabfea898edd09f68dbe5662b9aebe9dc87d69c97a10b7c8fb3e9d913b5ef5b` |
| Layer 1 qualified source commit | `743c9d9f203c485010db2fa683b5767187fe62b0` |
| Layer 1 qualified source tree | `0d1f827d53e61d66b055d2259ee89134b721344f` |
| Layer 1 accepted implementation artifact | `ceadf70ba16c044a62585e7fa88bbf47fbfefae1` |
| Layer 1 corrected accuracy framework (v2) | `fe0c2d116e4e4771dbe51dbc3193b7626fa39e89` |
| Layer 1 v2 evidence | `fa2a7696a497254fd38251072eb39a278ff24d4d` |

The v2 evidence (`reports/LAYER1_MULTI_DATASET_ACCURACY_REPORT_V2.md`,
`…_RESULTS_V2.json`) and v1 evidence are preserved unchanged.

## What was ACCEPTED (v2, corrected methodology)
- **Complete numerical validation passed.** 229 analytical fields per dataset;
  every category 100% (exact / strict-float / sampled / approximate / derived);
  **0 mismatches**; **0 Layer 2-consumable fields numerically unvalidated**.
- Determinism (3-run), truth isolation, robustness, and deadline behavior passed;
  0 hard-deadline violations across CHR18–CHR22.
- Independent oracle imports no Layer 1 calculation module (genuine cross-check).

## The single exclusion (why v2 is INCOMPLETE)
The emitted 100-kbp window feature `candidate_snp_density_per_base` did not meet a
site-localization relevance threshold:
```
pooled Spearman     = 0.236   (bar >= 0.30)
median-split AUROC  = 0.598   (bar >= 0.60)
```
This is a **feature-utility limitation, not a measurement-correctness defect**:
- the field is computed correctly (validated numerically against the independent
  oracle over the exact sampled windows; 0 mismatches);
- weak *local* SNP-density correlation at 100 kbp is expected (near-uniform truth
  SNP density + error-dominated candidate density);
- the emitted window-level **indel** density IS strongly truth-relevant
  (Spearman 0.916, AUROC 0.958);
- the BAM-intrinsic site-level signal is highly informative (enrichment 189–284×),
  confirming the limitation is one of window-level feature utility, not data.

## Why Layer 2 may proceed under this exclusion
- **Layer 2 performs global GATK configuration selection, not site-level SNP
  localization** (Layer 2 spec §1, §15–§16). The weak field measures a capability
  Layer 2 does not require.
- Numerical measurement correctness — the prerequisite the ENTRY GATE checks
  (profile schema hash / profiler version / config hash) — is fully accepted.
- `candidate_snp_density_per_base` is placed in state **RESEARCH_ONLY** (see
  `LAYER2_DATASET_SPLIT_POLICY.md` feature registry). It must **not** be consumed
  by the production Layer 2 controller as a local SNP-truth predictor. It may be
  evaluated only as a *global configuration-selection* feature, on training samples
  only, and promoted solely through the seven-step protocol (train CV benefit →
  cross-chromosome direction → validation benefit → bootstrap stability → no
  truth dependence → no calibration/downside regression → final locked-test
  confirmation after the pipeline is frozen). Test-set results may never decide
  retention.

## Conditions carried into Layer 2 (non-waivable)
1. Accepted PROTOCOL/TWIN/L1 gate identities remain unchanged; any breaking Layer 1
   schema/semantic change invalidates L1-READY and re-blocks Layer 2.
2. Truth/mutation/scoring data remain offline-only; the production controller never
   consumes them (see FORBIDDEN list in the split-policy document).
3. The 50/10/15 split is frozen before optimization; test set untouched until the
   final locked evaluation.

**Authorization:** proceed to the Layer 2 pre-implementation audit and the staged
plan L2-A … L2-J. No Layer 2 implementation is authorized by this record.
