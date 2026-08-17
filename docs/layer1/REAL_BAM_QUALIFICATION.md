# Real-BAM Qualification (prompt §4, §19 group L)

Two test tiers:

- **synthetic_ci_qualified** — mandatory pysam-built fixtures that run in GitHub
  Actions (no external dataset required). CI never fails merely because the real
  dataset is absent.
- **real_bam_qualified** — an optional real-BAM run recorded in
  `reports/LAYER1_REAL_BAM_REPORT.json`. The L1-READY gate can **never** PASS with
  `real_bam_qualified=false` — if the external BAM is unavailable the gate is HOLD.

## Running it
```bash
minos-engine layer1 qualify-real \
  --bam "$MINOS_DATASET_ROOT/practice/round_.../input.bam" \
  --reference "$MINOS_DATASET_ROOT/reference/chr19/chr19.fa" \
  --region chr19:36800001-46700000 \
  --coordinate-system one_based_inclusive \
  --dataset-id "<sanitized-id>" \
  --output reports/LAYER1_REAL_BAM_REPORT.json
```
Discovery requires explicit paths (no directory globbing, so truth files beside the
BAM are never seen). The BAM is never modified and no index is written beside the
original. The committed report records content sha256s, byte size, sanitized dataset
id, region, profiler identity, fingerprint, two-run elapsed + peak RSS, repeat-run
fingerprint equality, degradation status, and hard-limit result — **never** private
absolute paths or credentials.

## Recorded qualification run
- Dataset: `minos-practice-chr19-round-11fff0d5` (Minos Subnet 107 practice round).
- Region: `chr19:36800001-46700000` (~9.9 Mbp, protocol-scale, 1.57M reads).
- Two runs completed under the 300 s hard limit with identical fingerprints.
