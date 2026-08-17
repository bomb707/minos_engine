# Layer 2 — Dataset Split, Feature Eligibility & Input-Integrity Policy

**Runtime:** CPython 3.12.x. Authoritative source: Layer 2 spec §9 (partition &
leakage), §6–§8 (registry/canonicalization), Overall §2 (FORBID). This policy is
**design-only**; the immutable split manifest is produced in stage **L2-B** (not in
this audit), because it must freeze all 75 verified dataset IDs + input hashes.

## 1. Fixed split (frozen before any optimization)
Training **50** / Validation **10** / Testing **15**, by **complete sample/round**:

| Chromosome | Train | Validation | Test |
|---|---:|---:|---:|
| CHR18 | 10 | 2 | 3 |
| CHR19 | 10 | 2 | 3 |
| CHR20 | 10 | 2 | 3 |
| CHR21 | 10 | 2 | 3 |
| CHR22 | 10 | 2 | 3 |

Mandatory rules (spec §9): split by complete sample; never by window/locus/read/
variant; no sample in two partitions; deterministic generation before training;
freeze IDs + input hashes in an immutable manifest; fit all transformations on the
50 training samples only; validation used only for model selection / hyperparameters
/ thresholds / early stopping / calibration; the 15 test samples untouched until the
final locked evaluation; never choose samples by performance; stratify each
chromosome by deterministic hash after confirming its contig; truth/mutation/scoring
remain offline-only.

## 2. Deterministic generation algorithm (verified)
```
SALT = "minos-l2-split-v1"            # fixed; changing it defines a NEW split identity
for each round dir in datasets/practice/round_*:
    confirm contig via BAM @SQ (single-contig practice BAMs); group by contig
for each chromosome group (exactly 15 rounds):
    order = sort(round_ids, key = int(sha256(f"{SALT}:{round_id}").hexdigest(), 16))
    train      = order[0:10]
    validation = order[10:12]
    test       = order[12:15]
```
Properties: pure function of `{SALT, round_id, contig}`; independent of file order,
wall-clock, and any model/score; reproducible across machines. **Dry-run confirmed**
exactly 50/10/15 (10/2/3 per chromosome), 75 unique disjoint samples. Assignment is
made **only after** the contig is confirmed; no score/label influences it.

## 3. Immutable split-manifest schema (`schemas/layer2-dataset-split-v1.schema.json`, built in L2-B)
```
{
  schema_version: "layer2-dataset-split-v1",
  split_algorithm: "hash-stratified", salt: "minos-l2-split-v1",
  counts: {train:50, validation:10, test:15},
  per_chromosome: {chr18:{train:10,validation:2,test:3}, ...},
  samples: [ { dataset_id, chromosome, partition,           # partition ∈ train|validation|test
               bam_sha256, bai_sha256, reference_sha256, fai_sha256,
               truth_vcf_sha256, mutations_sha256,          # recorded for offline eval only
               bam_size_bytes, region_hash, sort_order } ],  # region_hash = sha256(canonical region)
  dataset_registry_hash,      # sha256 over the canonical sorted samples[] (identity excl. truth/mut hashes)
  parameter_space_hash,       # bound compatibility domain
  created_at, engine_git_sha
}
```
The manifest is written once, hashed, and treated as append-only evidence; it is the
authoritative dataset registry (backed in PostgreSQL by `catalog.dataset_splits` with
uniqueness + FK constraints). Regeneration with the same SALT reproduces it byte-for-byte.

## 4. Feature eligibility registry (Layer 1 → Layer 2)
States: `ELIGIBLE` (validated truth-free measurement usable as a training input,
subject to training-only selection + leakage review), `CONDITIONAL`/`RESEARCH_ONLY`
(offline evaluation only until promoted), `FORBIDDEN` (never consumed by the
production controller). All numeric fields are the v2-validated truth-free
measurements (229 analytical fields; 0 unvalidated).

| Layer 1 field family | State | Notes |
|---|---|---|
| `reads.*` fractions, `filter_counts.*` | ELIGIBLE | truth-free QC; leakage-reviewed |
| `mapping_quality.*` (mean/sd/min/max/quantiles/mq0/mq_lt20) | ELIGIBLE | mapping-risk driver (spec §6) |
| `base_quality.*` (mean/sd/quantiles/bq_lt20/missing) | ELIGIBLE | BQ evidence-filter driver |
| `read_length.*`, `pairing.*` (insert mean/sd/mad/quantiles, proper/overlap/abnormal) | ELIGIBLE | library/INDEL context |
| `alignment.*` (CIGAR base+event, NM, clipping fractions, ins/del burden) | ELIGIBLE | assembly/clip drivers |
| `coverage.duplicate_including.*` and `coverage.fragment_primary.*` | ELIGIBLE | depth/runtime drivers (fragment view declared-approximate) |
| `reference_context.*` (gc/n/entropy/homopolymer/dinucleotide) | ELIGIBLE | context complexity drivers |
| `variant_evidence.*` EXCEPT candidate_snp_density | ELIGIBLE | indel/support/AF/strand evidence proxies |
| **`variant_evidence.candidate_snp_density_per_base`** | **RESEARCH_ONLY** | owner exclusion; promotion via §5 only; never a live local SNP-truth predictor |
| `difficulty.*`, `confidence.*`, `spatial.*` (derived) | CONDITIONAL | descriptive; use only as global features after leakage/monotonicity review |
| `region.start0/end0/length_bp` (coordinates) | FORBIDDEN | position label proxy (spec §9 allowlist excludes coordinates) |
| `region.contig`, `header.*`, `identity.*` hashes, `provenance.*`, `profile_id`, dataset/round IDs | FORBIDDEN | identity/label encoders; used for join/integrity, never as model features |
| truth VCF, mutations, hap.py results, TP/FP/FN, hidden labels, live-round final scores, leaderboard/operator identity, previous winning CONFIG, evaluation paths/credentials | FORBIDDEN | Overall §2 FORBID; controller never consumes |

Historical **scores** are usable **only** inside the offline training boundary
(training rows in `experiments`/`evaluation` schemas), never at live inference.

## 5. `candidate_snp_density_per_base` promotion protocol (RESEARCH_ONLY → ELIGIBLE)
Promotion requires ALL of: (1) incremental predictive benefit on training
cross-validation (grouped by complete BAM, chromosome-held-out); (2) consistent
effect direction across chromosomes; (3) benefit on the untouched validation
partition; (4) stability under both sample bootstrap (N_eff=50) and chromosome
cluster bootstrap (N_eff=5); (5) no dependence on truth-derived inputs; (6) no
degradation of calibration or downside/CVaR risk; (7) final confirmation on the
locked 15-sample test set **only after the complete pipeline is frozen**. Test-set
results must never be used to decide retention. It is evaluated strictly as a
**global configuration-selection** feature, never as a live local SNP-truth predictor.

## 6. Leakage prevention (spec §9)
- Feature allowlist **excludes** round ID, sample/UUID, chromosome label,
  coordinates, artifact URI, truth-derived metrics, score, leaderboard/operator
  identity, and previous winning CONFIG.
- Training SQL views expose **development rows only**; a separate locked-test role is
  inaccessible until the production candidate is frozen; validation records
  predictions before evaluator access is granted.
- All transformations (scalers, encoders, imputation, calibration, thresholds) are
  fit on the 50 training samples only, then frozen.
- Cross-validation groups by complete BAM; no CONFIG rows from one BAM cross folds;
  chromosome-held-out results are reported. Every model snapshot stores the exact SQL
  query, row hashes, split manifest hash, feature schema, label/scorer compatibility
  domain, and exclusions.
- Validation/locked-test outcomes can never be converted into HPO observations.

## 7. Input-integrity compensating controls (Layer 1 robustness gaps)
The Layer 1 v2 robustness report documents two inherent input-integrity gaps: BAI
files carry no embedded BAM checksum, and a wrong reference with the same contig name
and length is undetectable without `@SQ:M5`. Layer 2 compensates:
- Mandatory content SHA-256 for BAM, BAI, reference, FAI recorded in the immutable
  dataset registry; a `dataset_manifest_hash`, `region_hash`, and
  `parameter_space_hash`.
- Exact association enforced between (BAM, BAI, reference, region): a decision/exec
  may reference only a registered `(bam_sha256, bai_sha256, reference_sha256,
  region_hash)` tuple; unexpected identity combinations are rejected.
- Optional verification against `@SQ:M5` when the header provides it.
- PostgreSQL uniqueness (`UNIQUE artifacts(sha256)`, `UNIQUE gatk_configs(config_hash)`,
  `UNIQUE decisions(round_id, decision_hash)`) and foreign-key constraints across
  catalog→profiling→experiments→runtime; immutable/append-only scientific evidence.
- **Fail-closed** when any required identity is missing or mismatched (never a
  best-effort decision).
