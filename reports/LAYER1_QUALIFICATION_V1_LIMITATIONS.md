# Layer 1 Multi-Dataset Qualification v1 — Limitations (superseded by v2)

The v1 acceptance artifacts (`reports/LAYER1_MULTI_DATASET_ACCURACY_REPORT.md`,
`reports/LAYER1_MULTI_DATASET_ACCURACY_RESULTS.json`, framework commit
`e965aadc…`, evidence commit `fb400baff…`) reported an overall **PASS** with a
100% "approximation pass rate" and enrichment 189–284× / AUROC 0.996–0.999.
An external review found the PASS did not validate the complete Layer 1 profile.
This document records the v1 defects. **v1 is not deleted or modified**; it is
superseded by v2 for final Layer 2 readiness.

## Confirmed v1 defects

1. **"Approximation pass rate" was unsupported.** v1 derived
   `approximation_pass_rate` from `total_float_fields` / `total_float_failures` —
   which are **strict deterministic floats**, not sampled/approximate fields.
   `observed_from_profile()` emitted **no** sampled/approximate fields, so the
   category had zero tested members yet was reported as 100%. A category with zero
   tested fields must be `NOT_TESTED`, never 100%.

2. **Truth relevance measured the BAM, not Layer 1's output.** v1's
   `truth_relevance.py` computed site-level alt evidence directly from BAM pileups.
   That proves the BAM *contains* variant evidence (BAM-intrinsic observability);
   it does **not** prove Layer 1's **emitted** `variant_evidence` / `spatial` /
   `difficulty` / `confidence` features capture it. The headline enrichment
   189–284× / AUROC 0.999 was BAM-intrinsic, not Layer 1 emitted-feature relevance.

3. **Incomplete field coverage.** v1 compared only **37** fields per BAM (22 exact
   + 15 strict floats) out of ~229 analytical fields in `bam-profile-v1`. Full
   distributions (min/max/stddev/quantiles/histograms/threshold fractions),
   pairing/orientation, CIGAR event counts, both coverage views incl. an
   independent fragment oracle, reference homopolymer histograms, the emitted
   `variant_evidence`, and all derived `spatial`/`difficulty`/`confidence` fields
   were not validated. v1 summarized 37 tested fields as complete-profile accuracy.

## What v1 did validate (retained as correct)
- 22 exact integer identities + 15 strict deterministic floats, per dataset, with
  0 mismatches — a genuine core numerical subset.
- Determinism (3-run), truth isolation, coordinate agreement, and a partial
  robustness set.

## v2 corrections
- Three separate accounting categories (`exact`, `float_strict`,
  `sampled/approximate`) plus `derived`, with `NOT_TESTED` for empty categories.
- Complete field inventory + classification of every serialized leaf; every
  Layer 2-consumable analytical field is validated (oracle / recompute / invariant).
- Two clearly separated labels: **BAM intrinsic truth observability** (direct
  pileup, secondary) and **Layer 1 emitted-feature truth relevance** (consumes the
  actual serialized window output). Substantive mutation↔truth reconciliation.
- Independent fragment-level coverage oracle; full distributions; variant evidence
  validated over the exact sampled windows.
- Completed robustness (synthetic fixtures for duplicates/secondary/supplementary/
  qcfail/overlap/unusual-CIGAR/high-depth/zero-depth/stale+mismatched index/wrong
  same-name reference).

See `reports/LAYER1_MULTI_DATASET_ACCURACY_REPORT_V2.md` for the v2 verdict.
