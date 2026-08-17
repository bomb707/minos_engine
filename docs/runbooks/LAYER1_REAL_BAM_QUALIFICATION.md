# Runbook — Layer 1 real-BAM qualification and L1-READY

```bash
# 1) Real-BAM two-run qualification -> committed integration report (Commit A).
minos-engine layer1 qualify-real \
  --bam "$MINOS_DATASET_ROOT/practice/round_.../input.bam" \
  --reference "$MINOS_DATASET_ROOT/reference/chr19/chr19.fa" \
  --region chr19:36800001-46700000 --dataset-id "<id>" \
  --output reports/LAYER1_REAL_BAM_REPORT.json

# 2) Commit A = qualified Layer 1 source (incl. the integration report + audit).

# 3) Run the L1-READY qualification (writes gate + report = Commit B).
minos-engine layer1 qualify --json

# 4) Verify a committed gate without regenerating it (CI / clean checkout):
minos-engine layer1 qualify --check --gate gates/l1-ready.json --base-dir . --json
minos-engine layer1 gate require-pass --gate gates/l1-ready.json --base-dir . --json
```
The L1-READY gate is git-tree-bound (evidence hashed from the qualified commit),
carries both prerequisite gate hashes (PROTOCOL-READY, TWIN-READY), the profile
schema hash, profiler config hash, profiler version, and the real-BAM integration
report hash, and cannot PASS with `layer1_real_bam_qualified=false`.
