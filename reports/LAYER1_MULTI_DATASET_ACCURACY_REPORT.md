# Layer 1 Multi-Dataset Numerical & Truth-Relevance Acceptance Report

**Executive verdict: PASS** — Layer 2 recommendation: **APPROVED_WITH_EXCLUSIONS**

Generated: 2026-08-17T17:02:19.026024+00:00 · Python 3.12.3 · pysam 0.22.1 / pyarrow 17.0.0 / numpy 2.5.2

Accepted gate chain (unchanged during measurement): PROTOCOL-READY `b9cda0bab329b36a…`, TWIN-READY `3464fb7604fd6b18…`, L1-READY `aeabfea898edd09f…`.

## 1. Executive summary (hard gates)

| Gate | Result |
|---|---|
| datasets (CHR18–22, ≥1 each) | chr18,chr19,chr20,chr21,chr22 |
| exact field pass rate | 100.0000% (110 fields, 0 mismatch) |
| approximation pass rate | 100.0000% (75 fields, 0 fail; max abs float err 3.15e-12) |
| coordinate agreement | 100.0% |
| schema validity | 100.0% |
| three-run determinism | 100.0% |
| truth-isolation equality | 100.0% |
| robustness scenarios | 100.0% |
| hard-deadline violations | 0 |

## 2–3. Dataset inventory & selection

Inventory: **75 practice rounds** (chr18=15, chr19=15, chr20=15, chr21=15, chr22=15).
Selection algorithm: Per chromosome, inventory all 15 practice rounds; select the round whose mean-coverage proxy is closest to the chromosome median (most representative). Selection performed before examining Layer 1 accuracy results.

| chr | dataset id | region | length bp |
|---|---|---|---|
| chr18 | `49a0587df4584484` | chr18:26992552-36992840 | 10000288 |
| chr19 | `ff150fd013413702` | chr19:30146589-40146876 | 10000287 |
| chr20 | `f589ad1a3e9343c0` | chr20:45790363-50790633 | 5000270 |
| chr21 | `5177fadfb13d5aa6` | chr21:31233150-36233433 | 5000283 |
| chr22 | `1ed494cb935cf7b2` | chr22:29647644-34647937 | 5000293 |

## 4. Input hashes (sanitized)

- **chr18** `49a0587df4584484`: bam `9b68d395342cba5b…` (169897725 B), bai `c72b5d7119505f76…`, ref `4c37db9609b3e865…`, fai `1e9dac505c1b48f7…`, truth `3d1a72ffd44205d4…`, mut `260b83b5349ee367…`
- **chr19** `ff150fd013413702`: bam `a8a4cf012e3e84a6…` (160845738 B), bai `35f38e17f318899d…`, ref `e00b74f7cd48f6c9…`, fai `ce0ee961cd23439f…`, truth `fdb7d2e182135826…`, mut `b370bfad1279a648…`
- **chr20** `f589ad1a3e9343c0`: bam `69aa68e3b21d764f…` (82722023 B), bai `b9f4b02c91166e2c…`, ref `61eba5b05ef7d9ae…`, fai `295950bb320e5f27…`, truth `e4f49cb14f4a6db0…`, mut `319d46b4fbc7db49…`
- **chr21** `5177fadfb13d5aa6`: bam `968816341a654247…` (93187286 B), bai `8424e1f99fa4a451…`, ref `c218d98e3bf58fa3…`, fai `838e3d562353d9a9…`, truth `b7da89e6e96551ff…`, mut `c430a739dd119f49…`
- **chr22** `1ed494cb935cf7b2`: bam `52c7288133551483…` (81532916 B), bai `337bbef838cba7fe…`, ref `8d440d7b863c6d0a…`, fai `7623e1c5091eda09…`, truth `393fe5b6e7ac0cd5…`, mut `3e1f28a5d4904497…`

## 8–10. Per-dataset numerical comparison (observed vs independent oracle)

| chr | exact fields | exact mismatch | float fields | float fail | max abs float err |
|---|---|---|---|---|---|
| chr18 | 22 | 0 | 15 | 0 | 1.63e-12 |
| chr19 | 22 | 0 | 15 | 0 | 2.76e-12 |
| chr20 | 22 | 0 | 15 | 0 | 1.85e-13 |
| chr21 | 22 | 0 | 15 | 0 | 3.15e-12 |
| chr22 | 22 | 0 | 15 | 0 | 1.34e-12 |

## 10–12. Per-chromosome numerical verdict & bias

- **chr18**: PASS (0 exact mismatch, 0 float fail). No systematic bias (all signed float errors within ±1e-9/1e-6).
- **chr19**: PASS (0 exact mismatch, 0 float fail). No systematic bias (all signed float errors within ±1e-9/1e-6).
- **chr20**: PASS (0 exact mismatch, 0 float fail). No systematic bias (all signed float errors within ±1e-9/1e-6).
- **chr21**: PASS (0 exact mismatch, 0 float fail). No systematic bias (all signed float errors within ±1e-9/1e-6).
- **chr22**: PASS (0 exact mismatch, 0 float fail). No systematic bias (all signed float errors within ±1e-9/1e-6).

## 13–17. Truth relevance (feature relevance — offline validation only)

| chr | evaluable truth | SNP/INS/DEL | overall sens | background | enrichment (95% CI) | AUROC | AUPRC |
|---|---|---|---|---|---|---|---|
| chr18 | 4000 | 3369/325/306 | 0.993 | 0.0035 | 283.8 (168.2–478.7) | 0.998 | 0.998 |
| chr19 | 4000 | 3311/335/353 | 0.992 | 0.0050 | 198.4 (128.2–307.3) | 0.998 | 0.998 |
| chr20 | 4000 | 3487/269/244 | 0.995 | 0.0047 | 209.4 (133.7–327.9) | 0.999 | 0.999 |
| chr21 | 4000 | 3343/359/298 | 0.994 | 0.0053 | 189.4 (123.6–290.1) | 0.997 | 0.998 |
| chr22 | 4000 | 3344/323/331 | 0.989 | 0.0050 | 197.8 (127.7–306.2) | 0.996 | 0.997 |

### Sensitivity by variant class
- **chr18**: del=0.964, ins=0.975, snp=0.998
- **chr19**: del=0.975, ins=0.967, mnp=1.000, snp=0.997
- **chr20**: del=0.988, ins=0.974, snp=0.997
- **chr21**: del=0.997, ins=0.958, snp=0.998
- **chr22**: del=0.970, ins=0.957, mnp=1.000, snp=0.994

Minimum enrichment 95% CI lower bound across datasets: **123.61**; minimum AUROC: **0.996**; class feature-collapse: none.
Minimal defensible truth-relevance bars met: **True**. Definitive numeric thresholds: **OWNER_DECISION_REQUIRED (authoritative spec defines none)** — the authoritative specification defines no numeric truth-relevance acceptance threshold, so the exact pass/fail cutoffs for sensitivity per class are an **owner decision**; measured distributions are reported above for ratification.

## 18. Truth-isolation proof

| chr | fingerprint equal | content-hash equal | families equal | warnings equal | sanitized files |
|---|---|---|---|---|---|
| chr18 | True | True | True | True | chr18.fa,chr18.fa.fai,input.bam,input.bam.bai |
| chr19 | True | True | True | True | chr19.fa,chr19.fa.fai,input.bam,input.bam.bai |
| chr20 | True | True | True | True | chr20.fa,chr20.fa.fai,input.bam,input.bam.bai |
| chr21 | True | True | True | True | chr21.fa,chr21.fa.fai,input.bam,input.bam.bai |
| chr22 | True | True | True | True | chr22.fa,chr22.fa.fai,input.bam,input.bam.bai |

## 19. Three-run determinism

| chr | fingerprint | content-hash | families | warnings | elapsed (s) | peak RSS (MB) |
|---|---|---|---|---|---|---|
| chr18 | True | True | True | True | [109.37, 110.26, 110.53] | 641.4 |
| chr19 | True | True | True | True | [100.2, 100.9, 100.05] | 640.6 |
| chr20 | True | True | True | True | [61.33, 61.7, 63.6] | 364.4 |
| chr21 | True | True | True | True | [73.03, 72.69, 72.62] | 362.8 |
| chr22 | True | True | True | True | [58.88, 58.63, 58.53] | 364.6 |

## 20. Robustness & cross-region/boundary

- **chr18**: fail-closed 6/6 error cases; restrictive deadline → PARTIAL (degraded, NaN/Inf-free); cross-region length-match: True.
- **chr19**: fail-closed 6/6 error cases; restrictive deadline → PARTIAL (degraded, NaN/Inf-free); cross-region length-match: True.
- **chr20**: fail-closed 6/6 error cases; restrictive deadline → PARTIAL (degraded, NaN/Inf-free); cross-region length-match: True.
- **chr21**: fail-closed 6/6 error cases; restrictive deadline → PARTIAL (degraded, NaN/Inf-free); cross-region length-match: True.
- **chr22**: fail-closed 6/6 error cases; restrictive deadline → PARTIAL (degraded, NaN/Inf-free); cross-region length-match: True.

## 21. Runtime & memory

- **chr18**: runs [109.37, 110.26, 110.53] s (< 300 s hard limit), peak RSS 644.9 MB, status COMPLETE.
- **chr19**: runs [100.2, 100.9, 100.05] s (< 300 s hard limit), peak RSS 643.6 MB, status COMPLETE.
- **chr20**: runs [61.33, 61.7, 63.6] s (< 300 s hard limit), peak RSS 367.5 MB, status COMPLETE.
- **chr21**: runs [73.03, 72.69, 72.62] s (< 300 s hard limit), peak RSS 366.8 MB, status COMPLETE.
- **chr22**: runs [58.88, 58.63, 58.53] s (< 300 s hard limit), peak RSS 368.0 MB, status COMPLETE.

## 22. Warnings, exclusions & limitations

- Practice BAMs carry no marked duplicates (duplicate_fraction = 0 across the corpus); the duplicate-exclusion path is exercised structurally by CI synthetic fixtures.
- The 'official challenge region' is taken as the full read-covered span of each practice BAM (no BED ships with the corpus); this is protocol-scale (5–10 Mbp).
- Full-corpus SHA-256 + deep metrics were computed for the 5 selected datasets; the other 70 rounds were inventoried by header/index statistics + a medium-depth scan (runtime bound).
- `variant_evidence` fields are validated for truth-relevance (feature lift), not numeric identity, because they are sampled under the ADAPTIVE pileup mode on large regions.
- `fragment_primary` coverage is a declared-approximate fragment-depth view; the duplicate-including view is the exact numeric oracle target.

## 23. Final Layer 2 recommendation: **APPROVED_WITH_EXCLUSIONS**

All hard numerical, coordinate, schema, determinism, truth-isolation, and robustness gates PASS with zero defects, and truth-relevance shows statistically significant enrichment/lift for every evaluated variant class with no feature collapse. The single outstanding item is the **owner decision** on definitive numeric truth-relevance thresholds (per-class sensitivity cutoffs), which the authoritative specification does not define. Recommend proceeding to Layer 2 planning conditional on owner ratification of those thresholds against the measured distributions above.
