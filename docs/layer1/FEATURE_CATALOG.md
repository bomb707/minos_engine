# Layer 1 Feature Catalog (formula + unit)

Every metric is descriptive. Names carry units; ambiguous names (`quality`,
`coverage`, `ratio`, `score`, `value`) are avoided.

## Alignment (raw over all observed alignments) — fraction
`duplicate_fraction = duplicate / observed`, `secondary_fraction`,
`supplementary_fraction`, `qcfail_fraction`, `mapped_fraction`, `unmapped_fraction`,
`paired_fraction`, `proper_pair_fraction = proper-paired / paired`,
`reverse_strand_fraction`, `mate_unmapped_fraction`.

## Filter accounting (mutually exclusive)
`observed = included + Σ excluded_by_reason` (unmapped, secondary, supplementary,
duplicate, qcfail, below_mapq). Both raw and analysis views are reported.

## Mapping quality (eligible reads) — Phred / fraction
`mean`, `stddev`, `min`, `max`, deterministic quantiles `P01..P99`,
`mq0_fraction = MQ==0 / eligible`, `mq_lt20_fraction`.

## Base quality (eligible reads) — Phred / fraction
`bases_observed`, `bases_with_quality`, `mean_base_quality_phred`, `stddev`,
quantiles, `bq_lt20_fraction = BQ<20 / bases_with_quality`,
`missing_quality_fraction = 1 - bases_with_quality / bases_observed`.

## Read length — bp
`count, mean, stddev, min, max`, quantiles, `variable_read_length`.

## Fragment / insert size (one mate per proper pair) — bp / fraction
`eligible_pair_count`, `mean_insert_size_bp`, `stddev`,
`insert_size_mad_bp = median(|abs(TLEN) − median(abs(TLEN))|)`, quantiles,
`overlapping_mate_fraction = overlapping completed pairs / completed pairs`,
`abnormal_pair_fraction = improper paired / paired`.

## CIGAR / clipping — count / fraction / rate
`aligned_query_bases (M/=/X)`, `soft_clipped_bases (S)`, `hard_clipped_bases (H)`,
`inserted_bases (I)`, `deleted_bases (D)`, `skipped_bases (N)`, `query_consuming_bases (M/I/S/=/X)`,
`soft_clipped_read_fraction = reads with any S / eligible`,
`soft_clipped_base_fraction = ΣS / Σ query-consuming`,
`indel_bearing_read_fraction`, `nm_per_aligned_base = ΣNM / Σ aligned (reads with NM)`,
`nm_availability_fraction`, `cigar_ins_del_burden = (I+D) / aligned`.

## Coverage (two views) — reads/base / fraction
Views: `fragment_primary` (duplicate-excluding, fragment-aware, overlap-corrected)
and `duplicate_including`. Each: `mean/median/stddev/cv/mad/max depth`,
`zero`, `<5`, `<10`, `<20`, `>50`, `>100`, `>200` fractions,
`callable_base_fraction (depth ≥ 10)`, quantiles `P01..P99`. Deletions do not add
base depth (blocks from `read.get_blocks()` split at D/N).

## Variant evidence (truth-free proxies) — count / density / fraction
`mismatch_fraction`, `candidate_snp_density_per_base`,
`candidate_insertion/deletion_density_per_base`, support-threshold site counts
`[2,3,5,8,10]`, allele-fraction site counts `[.05,.10,.20,.30,.40]`,
`forward/reverse_alt_fraction`, `low_quality_alt_fraction`,
`columns_reaching_max_depth`, `max_depth_capped_fraction`. A candidate SNP site is a
callable column whose top alternate allele reaches support ≥ 2 and AF ≥ 0.05, with a
non-ambiguous reference base. **Not** a variant call, TP/FP/FN, score, or CONFIG.

## Reference context — fraction / bits
`gc_fraction = (G+C)/(A+C+G+T)`, `n_fraction = non-ACGT/length`,
`entropy_bits = −Σ p_b log2 p_b`, `homopolymer_base_fraction` + length histogram,
`dinucleotide_repeat_fraction`. Not repeat-mask/mappability annotations.
