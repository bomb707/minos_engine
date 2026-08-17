# Truth Isolation (BOUNDARY; Overall spec §2 FORBID)

Layer 1 must never read or import truth VCFs, mutation VCFs, hidden scores,
leaderboards, previous winning CONFIGs, hap.py/scoring/evaluation results, or Layer
2 / the Twin offline truth loaders, and never derives labels from evaluation results.

## Enforcement
1. **Static import scan** — `tests/leakage/test_truth_isolation.py` and
   `qualification/checks.no_truth_or_locked_test_access` scan the `layer1` package
   (a live/production package) for forbidden import targets and non-docstring string
   tokens (`truth.vcf`, `mutations.vcf`, `.sdf`, `confident_regions`, `hidden_score`,
   `leaderboard`, `final_test`).
2. **Architecture boundaries** — `tests/leakage/test_architecture_boundaries.py`
   proves `layer1` never imports `layer2`, `twin`, `scoring`, `happy`, `evaluation`,
   `truth`, `mutation`, or `retrieval`.
3. **Runtime denial** — `tests/leakage/layer1/test_truth_isolation_layer1.py` places
   truth/mutation sentinels beside the BAM and proves the profile fingerprint is
   unchanged (Layer 1 opens only the explicit BAM/BAI/FASTA/FAI paths and never
   enumerates a round directory).

The `pysam` adapter is the single I/O boundary and opens exactly the paths it is
given. The L1-READY gate carries `layer1_truth_isolation_verified` and
`layer1_architecture_boundaries_verified` as mandatory checks.
