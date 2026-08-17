# Layer 1 Input Contract

`ProfileRequest` (Layer 1 spec §1): `round_id, bam_path, bai_path, reference_path,
fai_path, region_source, region_coordinate_convention, budget_seconds,
expected_hashes?, cpu_limit, memory_limit_bytes, profiler_config_version,
profiler_config_hash`.

`ProfileResult`: `status (COMPLETE|PARTIAL|FAILED), profile_path, windows_path,
manifest_path, failure_code?, fallback_required, warnings[]`.

## Validation (fail-closed, typed `Layer1InputError`)
Hard failures: corrupt/unreadable BAM; missing/unusable/corrupt BAI; unsorted BAM;
missing contig; region outside contig; BAM/FASTA contig-length mismatch; empty
region; reference open failure. Warnings: missing `@RG`/`SM`; multiple read
groups/libraries. A BAI mtime is never treated as identity — content hashes plus a
successful indexed fetch are used and the verification strength is recorded.

## Coordinate conventions
`one_based_inclusive` and `zero_based_half_open`. Conversion happens exactly once
into a zero-based half-open interval; `0 <= start0 < end0 <= contig length` for both
BAM and FASTA, with equal contig lengths. Abbreviations (`k`, `M`) and commas are
rejected. Any ambiguity/mismatch is a hard failure — no partial profile is emitted.

## Truth isolation
Discovery requires **explicit** BAM/BAI/FASTA/FAI paths. Layer 1 never enumerates a
round directory, so `truth.vcf.gz` / `mutations.vcf.gz` beside a BAM are never
discovered or read (`MINOS_DATASET_ROOT` overrides only the dataset root).
