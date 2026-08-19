# PROFILE-SNAPSHOT-FROZEN-1 — Qualification Report

**Tool:** layer2-profile-snapshot-qualifier-v1

> Generated from the operational store + committed manifests. The frozen snapshot hash
> is independently reproducible from the committed member manifest (same canonical
> formula as the freeze). m5 semantics: SAM `@SQ:M5` is a BAM-header tag (FASTA files
> never carry it); in epoch 1 all 75 BAM headers lack `@SQ:M5` while the computed
> reference-contig MD5 was available for every sample, so all 75 attestations are
> ABSENT/integrity-degraded (0 MATCH, 0 MISMATCH). Raw BAMs are not committed; the
> content-addressed artifact inventory permits later integrity verification without
> trusting the database.

## Bound identities
```
{
  "accepted_ingest_ready_gate_hash": "91f55da0bfe4df8620508ddb9566a0fd9ed838ca1beb2d2522bcb655d8061599",
  "artifact_inventory_hash": "d00f40cd5096def8a7f379390736ec07ef09523d28b62f0893d8c5aded469815",
  "degraded_integrity_count": "75",
  "ingest_ready_evidence_commit": "5ed620a6371f771be2cfead8caeb712bf4701121",
  "ingest_ready_source_commit": "87835a99918812172343eabb7a1e8037e317eaec",
  "m5_absent_count": "75",
  "m5_match_count": "0",
  "member_manifest_hash": "2461751f2de4114fbf29114a4cff76b81e394c790e58e2788dd2b7c28b8e6c9b",
  "registry_snapshot_hash": "3e60aa65aeed8969e29ebeef83024f6fa2285a13c155d7d6dc0c601d1e94f675",
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
| `artifact_inventory_bound` | PASS |
| `attestation_bound` | PASS |
| `degraded_integrity_count_75` | PASS |
| `epoch_binding_exact` | PASS |
| `m5_counts_recorded` | PASS |
| `member_count_75` | PASS |
| `members_unique_identities` | PASS |
| `partitions_50_10_15` | PASS |
| `partitions_match_split_allocations` | PASS |
| `per_chromosome_15` | PASS |
| `registry_binding_exact` | PASS |
| `sealed_test_denied` | PASS |
| `selected_versions_unique_and_explicit` | PASS |
| `snapshot_hash_recomputed` | PASS |
| `snapshot_tables_append_only` | PASS |
| `trainer_view_count_50` | PASS |
| `validation_view_count_10` | PASS |
