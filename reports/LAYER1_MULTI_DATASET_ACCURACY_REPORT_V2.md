# Layer 1 Multi-Dataset Accuracy Report v2 (corrected methodology)

**Executive verdict: INCOMPLETE** — Layer 2 recommendation: **BLOCKED**

Generated 2026-08-17T18:29:59.999054+00:00 · Python 3.12.3 · pysam 0.22.1 / pyarrow 17.0.0 / numpy 2.5.2

Supersedes v1 (see `LAYER1_QUALIFICATION_V1_LIMITATIONS.md`). Accepted gates unchanged: PROTOCOL `b9cda0bab329…`, TWIN `3464fb7604fd…`, L1 `aeabfea898ed…`.

## Field inventory (per dataset)

- total serialized fields: 278
- analytical / L2-consumable: 229
- identifier (excluded): 40 · operational (excluded): 9
- **unvalidated L2-consumable fields: 0**

## Field coverage by classification (pooled across 5 datasets)

| category | tested/dataset | pass | fail | not_tested | pass_rate |
|---|---|---|---|---|---|
| exact | 64 | 323 | 0 | 0 | 1.0 |
| float_strict | 55 | 275 | 0 | 0 | 1.0 |
| sampled | 21 | 105 | 0 | 0 | 1.0 |
| approximate | 68 | 340 | 0 | 0 | 1.0 |
| derived | 21 | 106 | 0 | 0 | 1.0 |

## Per-dataset numerical result

| chr | dataset | exact f | float_strict f | sampled f | approx f | derived f | unvalidated L2 |
|---|---|---|---|---|---|---|---|
| chr18 | `49a0587df4584484` | 0 | 0 | 0 | 0 | 0 | 0 |
| chr19 | `ff150fd013413702` | 0 | 0 | 0 | 0 | 0 | 0 |
| chr20 | `f589ad1a3e9343c0` | 0 | 0 | 0 | 0 | 0 | 0 |
| chr21 | `5177fadfb13d5aa6` | 0 | 0 | 0 | 0 | 0 | 0 |
| chr22 | `1ed494cb935cf7b2` | 0 | 0 | 0 | 0 | 0 | 0 |

## Layer 1 EMITTED-FEATURE truth relevance (window-level, actual output; pooled)

- sampled windows pooled: 51
- SNP: Spearman(candidate_snp_density, truth SNP/bp) = **0.236**, median-split AUROC = **0.598**, useful = **False**
- INDEL: Spearman(candidate_indel_density, truth indel/bp) = **0.916**, median-split AUROC = **0.958**, useful = **True**
- usefulness bar: abs(spearman)>=0.30 and median-split AUROC>=0.60

> Finding: Layer 1's emitted window-level candidate INDEL density tracks per-window truth indel content, but the emitted window-level candidate SNP density does NOT usefully separate windows by truth SNP content at 100 kbp granularity (near-uniform truth SNP density + error-dominated candidate density). This is a granularity limitation of the window profile; SNP truth signal lives in the site-level sampled evidence, not the window aggregate.

## BAM-intrinsic truth observability (secondary — NOT Layer 1 emitted features)

| chr | enrichment | AUROC | sensitivity | background |
|---|---|---|---|---|
| chr18 | 283.8 | 0.998 | 0.993 | 0.0035 |
| chr19 | 198.4 | 0.998 | 0.992 | 0.0050 |
| chr20 | 209.4 | 0.999 | 0.995 | 0.0047 |
| chr21 | 189.4 | 0.997 | 0.994 | 0.0053 |
| chr22 | 197.8 | 0.996 | 0.989 | 0.0050 |

(The strong BAM-intrinsic signal confirms the data is informative; it does NOT establish that Layer 1's emitted features capture it — see the emitted-feature section.)

## Mutation ↔ truth reconciliation

| chr | mutation records | truth records | matched | unmatched mut | unmatched truth | complex excl |
|---|---|---|---|---|---|---|
| chr18 | 399 | 12501 | 399 | 0 | 12102 | 167 |
| chr19 | 215 | 12326 | 215 | 0 | 12111 | 159 |
| chr20 | 105 | 6661 | 105 | 0 | 6556 | 68 |
| chr21 | 220 | 6509 | 220 | 0 | 6289 | 68 |
| chr22 | 99 | 7604 | 99 | 0 | 7505 | 98 |

## Determinism / truth isolation / runtime

| chr | det fp | det content | iso fp | iso content | elapsed s | peak RSS MB |
|---|---|---|---|---|---|---|
| chr18 | True | True | True | True | [112.65, 112.95, 111.47] | 646.0 |
| chr19 | True | True | True | True | [98.4, 96.95, 96.85] | 646.1 |
| chr20 | True | True | True | True | [60.01, 59.86, 60.11] | 369.7 |
| chr21 | True | True | True | True | [69.83, 71.02, 71.72] | 368.7 |
| chr22 | True | True | True | True | [61.1, 60.82, 61.11] | 369.5 |

## Robustness (synthetic fixtures — corpus lacks these conditions)

- duplicates: PASS
- secondary_supplementary_qcfail: PASS
- overlapping_mates: PASS
- missing_qualities: PASS
- unusual_cigar: PASS
- zero_depth_region: PASS
- high_depth: PASS
- zero_mapq: PASS
- malformed_read_group: PASS
- truncated_bam: PASS
- bam_bai_identity_mismatch: DOCUMENTED_LIMITATION
- wrong_reference_same_name_len: PASS
- incorrect_readable_bai: PASS
- wrong_reference_same_len_seq: DOCUMENTED_LIMITATION

Robustness failures: none

## Summary gates

- exact_pass_rate: 1.0
- float_strict_pass_rate: 1.0
- sampled_pass_rate: 1.0
- approximate_pass_rate: 1.0
- derived_pass_rate: 1.0
- determinism_pass_rate: 1.0
- truth_isolation_pass_rate: 1.0
- robustness_pass_rate: 1.0
- hard_deadline_violations: 0
- total_exact_mismatches: 0
- total_analytical_failures: 0

## Verdict: **INCOMPLETE** · Layer 2: **BLOCKED**

Complete-profile numerical validation PASSES (every analytical field validated, 0 unvalidated L2-consumable fields, 0 mismatches), and determinism / truth-isolation / robustness pass with 0 hard-deadline violations. However Layer 1's EMITTED window-level SNP feature is NOT demonstrably useful for site-level SNP truth at 100 kbp, so the emitted-feature truth-relevance requirement is not met. Per the v2 rules the verdict is INCOMPLETE and Layer 2 remains BLOCKED pending an owner decision on window-feature fitness for SNP localization (and whether site-level sampled evidence should be the SNP signal Layer 2 consumes).
