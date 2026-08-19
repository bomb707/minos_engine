# L2-D — Layer 1 Profile Ingestion (INGEST-READY + PROFILE-SNAPSHOT-FROZEN)

Append-only stage definition. This document defines the L2-D authority model, the epoch
binding, the admission rules, and the access-state machine. Nothing here overrides the
frozen v1/v2 split artifacts or any accepted gate.

## Authority model (owner ruling)

| Object | Role |
|---|---|
| `profiling.profiles` (L2-B) | **Compatibility-only.** No downstream reads (SELECT revoked from `minos_trainer`/`minos_live` by migration 0004; `minos_runner`'s legacy write path unchanged; `storage/roles.py` byte-identical). |
| `profiling.bam_profiles` | **Canonical immutable content authority.** One append-only row per accepted COMPLETE profile version; `content_hash` is the location-independent version key. |
| `profiling.profile_snapshots` + `profile_snapshot_members` | **Canonical epoch membership.** A snapshot binds one split epoch (FK to `catalog.split_snapshots` + split manifest hash + registry snapshot hash) and selects the exact accepted `bam_profiles` version per identity. |
| Partition member views | **Sole authorized downstream interface.** L2-E never reads base tables. |

## Epoch binding & growth

* A profile snapshot exists per split epoch; its `member_count` **equals the epoch's
  `sample_count`** — never a hardcoded corpus size. Corpus growth = new split epoch →
  new profile snapshot.
* Freezing fails closed if any allocated identity lacks an accepted profile (incomplete
  corpus) or has more than one accepted version (ambiguous — explicit owner version
  selection required).
* Sample removal is **prohibited**: split epochs are supersets of their parents
  (SPLIT-FROZEN-V2 invariant), and snapshot membership derives from them.
* Model binding: any trained model must record the `profile_snapshots.snapshot_hash`
  (which transitively binds split manifest + registry snapshot + every member
  `content_hash`/`feature_values_hash`).

## Admission (fail-closed; every attempt logged)

1. Intake produces the **content-addressed input-integrity attestation**
   (`minos-engine intake attest-input`): stream SHA-256 of BAM/BAI/FASTA/FAI, canonical
   region hash, identity-tuple recompute, registry-record match, BAM `@SQ` read (stdlib
   BGZF), SAM-compatible reference-contig MD5, m5 status. Deterministic content only.
2. L2-D consumes + independently validates; it **never opens genomic files**.
3. m5 rule: `MATCH` admits; `ABSENT` admits flagged `integrity_degraded`
   (`BAM_SQ_M5_ABSENT`); `MISMATCH` always rejects; any identity/hash mismatch or
   malformed attestation rejects.
4. Only `COMPLETE` profiles are admissible. `feature_values_hash` is NOT NULL, computed by
   the frozen algorithm `SHA256("minos:canonical-feature-values:v1\n" +
   canonical_json(eligible_values))` over the production-ELIGIBLE scalar values; equality
   is enforced between validation-time and write-time recomputation, the typed column,
   and the stored JSONB re-derivation (conflict → typed failure + rollback).
5. Rejected/partial attempts are recorded in `profiling.profile_ingest_attempts`
   (separate transaction) and never weaken the accepted-row constraints.

## Access-state machine (test exposure / consumption)

| State | Meaning | Transition |
|---|---|---|
| `SEALED` | `evaluation.sealed_test_profile_members` + `evaluation.sealed_test_epoch_allocations` carry **no grant**; no role can read test membership. | Initial state (migration 0003/0004). |
| `FINAL_EVAL_AUTHORIZED` | A separate, explicitly owner-authorized migration grants evaluator SELECT for the final-evaluation run. | Owner decision only; new migration + gate. |

Trainer reads train membership only (`profiling.training_profile_members`); the evaluator
reads validation membership only (`evaluation.validation_profile_members`). Views expose
membership + integrity identity — no raw JSONB, artifact ids/URIs, file identity hashes,
or region coordinates. New sealed cohorts (future epochs) inherit `SEALED` automatically
because test membership flows through the same ungranted views.

## Gates

* **INGEST-READY** (capability, corpus-independent): machinery correctness — migration
  0004 single-head lineage, boundaries, sealed access, admission rules — bound to the
  accepted SPLIT-FROZEN-V2 closure via the two-commit model.
* **PROFILE-SNAPSHOT-FROZEN-\<epoch\>** (per-epoch corpus evidence): frozen snapshot +
  membership for one epoch; only PASS snapshots are consumable downstream.
