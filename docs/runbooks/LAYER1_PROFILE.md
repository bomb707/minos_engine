# Runbook — Layer 1 profiling

```bash
# Validate inputs only (integrity + region resolution):
minos-engine layer1 validate \
  --bam input.bam --reference chr19.fa --region chr19:36800001-46700000 --json

# Profile and write the three artifacts (bam-profile-v1.json,
# window-profile-v1.parquet, profile-manifest-v1.json):
minos-engine profile \
  --bam input.bam --bai input.bam.bai \
  --reference chr19.fa --fai chr19.fa.fai \
  --region chr19:36800001-46700000 \
  --coordinate-system one_based_inclusive \
  --budget-seconds 300 --output-dir out/profile --json
```
Exit codes: `0` COMPLETE/PARTIAL usable, `2` hard input failure, `3`
internal/serialization failure. `--skip-prerequisite` bypasses the accepted
TWIN-READY gate check (development only). The profiler requires CPython 3.12.x.
Outputs are generated runtime files and are git-ignored (except the committed
real-BAM integration report).
