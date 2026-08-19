# PROFILE-SNAPSHOT-FROZEN-1 — Qualification Report

**Tool:** layer2-profile-snapshot-qualifier-v2

> Two verification levels: OFFLINE (committed artifacts only — gate, member manifest,
> selections, inventory, accepted split + INGEST-READY identities; embedded hashes never
> trusted, everything recomputed) and OPERATIONAL (live store + exact artifact bytes:
> attestations re-parsed and re-hashed per member, inventory rebuilt from the corpus and
> compared hash-for-hash, zero ingestion failures, view counts, sealed-test denial,
> append-only). m5 semantics: SAM `@SQ:M5` is a BAM-header tag (FASTA files never carry
> it); in epoch 1 all 75 BAM headers lack `@SQ:M5` while the computed
> reference-contig MD5 was available for every sample, so all 75 attestations are
> ABSENT/integrity-degraded (0 MATCH, 0 MISMATCH). Raw BAMs are not committed; the
> content-addressed artifact inventory permits later integrity verification without
> trusting the database.

## Bound identities
```
{
  "accepted_ingest_ready_gate_hash": "91f55da0bfe4df8620508ddb9566a0fd9ed838ca1beb2d2522bcb655d8061599",
  "accepted_profiler_config_hash": "d01b8e7a9da8e31adad1b9cba17230506771a885256a67ec2e96c74a13c07670",
  "accepted_profiler_version": "layer1-profiler-v1",
  "artifact_inventory_hash": "d00f40cd5096def8a7f379390736ec07ef09523d28b62f0893d8c5aded469815",
  "degraded_integrity_count": "75",
  "ingest_ready_evidence_commit": "5ed620a6371f771be2cfead8caeb712bf4701121",
  "ingest_ready_source_commit": "87835a99918812172343eabb7a1e8037e317eaec",
  "m5_absent_count": "75",
  "m5_match_count": "0",
  "m5_mismatch_count": "0",
  "member_manifest_hash": "2461751f2de4114fbf29114a4cff76b81e394c790e58e2788dd2b7c28b8e6c9b",
  "registry_snapshot_hash": "3e60aa65aeed8969e29ebeef83024f6fa2285a13c155d7d6dc0c601d1e94f675",
  "rejected_attempt_count": "0",
  "selection_manifest_hash": "8077ed3851ecb706f9dcde44ef47aecd2669e0683b06f8d7cc402dcaa64a6ddb",
  "snapshot_hash": "cf717ebb44e76a3408e975e027b51139df28d643dd1616c5edbce3643182c4c7",
  "split_manifest_hash": "b23cd5716ab46033f7ea0bf123cc9b2a5f401fa37dbffddba8d4201f5ea76145"
}
```

## Mandatory checks
| Check | Result |
|---|---|
| `accepted_ingest_ready_bound` | PASS |
| `all_profiles_complete` | PASS |
| `artifact_bindings_complete` | PASS |
| `attestation_bound` | PASS |
| `attestation_files_exactly_bound` | PASS |
| `ci_verifies_snapshot_gate` | PASS |
| `degraded_integrity_count_75` | PASS |
| `epoch_binding_exact` | PASS |
| `identities_match_registry` | PASS |
| `inventory_canonical_integrity` | PASS |
| `inventory_four_artifacts_each` | PASS |
| `m5_counts_recorded` | PASS |
| `m5_mismatch_count_zero` | PASS |
| `member_count_75` | PASS |
| `member_manifest_canonical_integrity` | PASS |
| `members_unique_identities` | PASS |
| `operational_artifact_bytes_reverified` | PASS |
| `partitions_50_10_15` | PASS |
| `partitions_match_split_allocations` | PASS |
| `per_chromosome_15` | PASS |
| `profiler_identity_exact` | PASS |
| `registry_binding_exact` | PASS |
| `sealed_test_denied` | PASS |
| `selected_versions_unique_and_explicit` | PASS |
| `snapshot_hash_recomputed` | PASS |
| `snapshot_tables_append_only` | PASS |
| `trainer_view_count_50` | PASS |
| `validation_view_count_10` | PASS |
| `zero_ingestion_failures` | PASS |
