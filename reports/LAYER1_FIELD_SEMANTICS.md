# Layer 1 Field Semantics (acceptance oracle specification)

Enumerates the Layer 1 `bam-profile-v1` output fields, their exact definitions,
the validation oracle used, the predeclared acceptance tolerance, and whether the
field can influence Layer 2. Tolerances are declared **before** any comparison.

Conventions (Layer 1 declared policy, mirrored by `scripts/qualification/oracle.py`):
- **Region:** input converted exactly once to a zero-based half-open `[start0,end0)`.
  `observed` = reads returned by `fetch(contig,start0,end0)` (overlap semantics).
- **Exclusion priority (single reason):** unmapped → secondary → supplementary →
  duplicate → qcfail → below_mapq (MAPQ floor 0 ⇒ none excluded here).
- **Raw** alignment counts are over ALL observed; **analysis** stats (MAPQ, BQ,
  read length, CIGAR) are over **included** reads.
- **Coverage duplicate-including view:** +1 over each M/=/X reference block of every
  coverage-eligible read (mapped, non-secondary/supplementary/qcfail; duplicates
  included). Deletions/skips do not add depth. **Fragment-primary view** additionally
  excludes duplicates and clips overlapping mates (declared-approximate fragment depth).
- **Quantiles:** fixed integer-histogram cumulative estimate (deterministic).
- **Empty input:** counts 0, fractions 0.0, no NaN/Inf ever serialized.

Kinds: **exact** (integer identity, `==` required), **float_strict** (deterministic
float, abs ≤ 1e-9 or rel ≤ 1e-6), **approx** (sampled/estimated, family tolerance),
**derived** (computed from exact fields).

| JSON path | family | type | kind | numerator / rule | denominator | oracle | tolerance | → L2? |
|---|---|---|---|---|---|---|---|---|
| `filter_counts.observed` | filter | int | exact | reads from `fetch` | — | pysam fetch count | `==` | yes |
| `filter_counts.included` | filter | int | exact | observed − Σ excluded | — | independent flag classify | `==` | yes |
| `filter_counts.excluded_unmapped` | filter | int | exact | FLAG&0x4 (1st reason) | — | independent | `==` | yes |
| `filter_counts.excluded_secondary` | filter | int | exact | FLAG&0x100 (1st) | — | independent | `==` | yes |
| `filter_counts.excluded_supplementary` | filter | int | exact | FLAG&0x800 (1st) | — | independent | `==` | yes |
| `filter_counts.excluded_duplicate` | filter | int | exact | FLAG&0x400 (1st) | — | independent | `==` | yes |
| `filter_counts.excluded_qcfail` | filter | int | exact | FLAG&0x200 (1st) | — | independent | `==` | yes |
| `filter_counts.excluded_below_mapq` | filter | int | exact | MAPQ<floor (1st) | — | independent | `==` | yes |
| `reads.total_observed_alignments` | alignment | int | exact | all observed | — | pysam | `==` | yes |
| `reads.included_primary_alignments` | alignment | int | exact | included | — | independent | `==` | yes |
| `reads.duplicate_fraction` | alignment | float | float_strict | duplicate | observed | independent | 1e-9/1e-6 | yes |
| `reads.mapped_fraction` | alignment | float | float_strict | mapped | observed | independent | 1e-9/1e-6 | yes |
| `reads.reverse_strand_fraction` | alignment | float | float_strict | mapped&reverse | included | independent | 1e-9/1e-6 | yes |
| `reads.{secondary,supplementary,qcfail}_fraction` | alignment | float | float_strict | flag count | observed | independent | 1e-9/1e-6 | yes |
| `reads.{paired,proper_pair,mate_unmapped,unmapped}_fraction` | alignment | float | derived | flag count | observed/paired | independent | 1e-9/1e-6 | yes |
| `mapping_quality.count` | mapq | int | exact | included reads | — | independent | `==` | yes |
| `mapping_quality.mean` | mapq | float | float_strict | Σ MAPQ | included | independent | 1e-9/1e-6 | yes |
| `mapping_quality.mq0_fraction` | mapq | float | float_strict | MAPQ=0 | included | independent | 1e-9/1e-6 | yes |
| `mapping_quality.mq_lt20_fraction` | mapq | float | float_strict | MAPQ<20 | included | independent | 1e-9/1e-6 | yes |
| `mapping_quality.quantiles.P*` | mapq | float | approx | histogram cumulative | — | independent hist | ≤1 pct-pt | yes |
| `base_quality.bases_observed` | bq | int | exact | Σ query_length (incl) | — | independent | `==` | yes |
| `base_quality.bases_with_quality` | bq | int | exact | Σ len(quals) (incl) | — | independent | `==` | yes |
| `base_quality.mean_base_quality_phred` | bq | float | float_strict | Σ BQ | bases_with_quality | independent | 1e-9/1e-6 | yes |
| `base_quality.bq_lt20_fraction` | bq | float | float_strict | BQ<20 | bases_with_quality | independent | 1e-9/1e-6 | yes |
| `read_length.count` | readlen | int | exact | included | — | independent | `==` | yes |
| `read_length.mean` | readlen | float | float_strict | Σ query_length | included | independent | 1e-9/1e-6 | yes |
| `read_length.variable_read_length` | readlen | bool | exact | distinct lengths>1 | — | independent | `==` | maybe |
| `alignment.aligned_query_bases` | cigar | int | exact | Σ M/=/X len | — | direct CIGAR | `==` | yes |
| `alignment.soft_clipped_bases` | cigar | int | exact | Σ S len | — | direct CIGAR | `==` | yes |
| `alignment.hard_clipped_bases` | cigar | int | exact | Σ H len | — | direct CIGAR | `==` | yes |
| `alignment.inserted_bases` | cigar | int | exact | Σ I len | — | direct CIGAR | `==` | yes |
| `alignment.deleted_bases` | cigar | int | exact | Σ D len | — | direct CIGAR | `==` | yes |
| `alignment.skipped_bases` | cigar | int | exact | Σ N len | — | direct CIGAR | `==` | yes |
| `alignment.query_consuming_bases` | cigar | int | exact | Σ M/I/S/=/X len | — | direct CIGAR | `==` | yes |
| `alignment.soft_clipped_base_fraction` | cigar | float | float_strict | ΣS | query_consuming | direct CIGAR | 1e-9/1e-6 | yes |
| `coverage.eligible_region_bases` | coverage | int | exact | end0−start0 | — | arithmetic | `==` | yes |
| `coverage.duplicate_including.max_depth` | coverage | int | exact | max prefix-sum depth | — | diff-array | `==` | yes |
| `coverage.duplicate_including.mean_depth_reads_per_base` | coverage | float | float_strict | Σ depth | region_len | diff-array | 1e-9/1e-6 | yes |
| `coverage.duplicate_including.zero_depth_fraction` | coverage | float | float_strict | depth==0 bases | region_len | diff-array | 1e-9/1e-6 | yes |
| `coverage.duplicate_including.depth_quantiles.P*` | coverage | float | approx | numpy quantile | — | numpy | ≤1 pct-pt | yes |
| `coverage.fragment_primary.*` | coverage | float | approx | fragment-aware, overlap-clipped | region_len | (declared-approx) | means ≤0.01/2%, breadth ≤0.005 | yes |
| `reference_context.gc_fraction` | reference | float | float_strict | (G+C) | A+C+G+T | direct FASTA | 1e-9/1e-6 | yes |
| `reference_context.n_fraction` | reference | float | float_strict | non-ACGT | length | direct FASTA | 1e-9/1e-6 | yes |
| `reference_context.entropy_bits` | reference | float | float_strict | −Σ p·log2 p | — | direct FASTA | 1e-9/1e-6 | yes |
| `reference_context.homopolymer_base_fraction` | reference | float | float_strict | Σ runs≥4 bases | length | direct FASTA | 1e-9/1e-6 | yes |
| `variant_evidence.*` | evidence | mixed | approx | truth-free pileup proxies (sampled under ADAPTIVE) | see Phase 5 | independent pileup | truth-relevance (not exact) | yes |
| `spatial.*`, `difficulty.*`, `confidence.*` | derived | mixed | derived | descriptive transforms | — | — | not numerically gated | yes (descriptive) |
| `stage_timings`, `runtime_complexity`, `degradation` | operational | mixed | n/a | wall-clock | — | — | excluded from identity | no |

**Empty-input behavior:** all counts 0, fractions 0.0; `reference_context` for an
empty interval returns zeros; the serializer rejects NaN/Inf. **Truth influence:**
no field is derived from truth/mutation/score data (enforced by truth isolation).
`variant_evidence` fields are the fields most relevant to Layer 2 and are validated
for truth-relevance (Phase 5), not numeric identity, because they are sampled under
the ADAPTIVE pileup mode on large regions.
