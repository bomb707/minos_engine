# MINOS Database V2 — Migration and Cutover Plan

Companion to [the architecture](MINOS_DATABASE_V2_ARCHITECTURE.md) and
[the ERD](MINOS_DATABASE_V2_ERD.md). The complete object-by-object mapping is in
[`MINOS_DATABASE_V2_CURRENT_TO_TARGET.json`](../../reports/database/MINOS_DATABASE_V2_CURRENT_TO_TARGET.json).

**Nothing in this document has been executed.** D1 is design only.

---

## 1. Current-to-target summary

All 33 live objects (23 tables + 10 views) and the 7 planned L2-F tables are mapped — 40 entries,
each with column mapping, transformation, data-loss risk, rollback, a validation query and the
application modules affected.

| Action | Count | Objects |
|---|---:|---|
| `KEEP` | 7 | `audit.events`, `profiling.profile_snapshots`, `profile_snapshot_members`, `feature_sets`, `feature_matrices`, `feature_matrix_members`, `public.alembic_version` |
| `REPLACE` | 15 | 10 views, `catalog.gatk_configs`, `evaluation.dataset_evaluation_identity`, `evaluation.evaluations`, `runtime.decisions`, `(planned) l2f_config_payloads` |
| `SPLIT` | 6 | `catalog.artifacts`, `profiling.bam_profiles`, `models.model_bundles`, `(planned) l2f_experiment_jobs`, `l2f_execution_results`, `l2f_execution_failures` |
| `MERGE` | 5 | `catalog.dataset_registry`, `catalog.datasets`, `catalog.split_snapshots`, `split_allocations`, `split_epoch_allocations` |
| `RENAME` | 3 | `(planned) l2f_experiment_plans`, `l2f_experiment_plan_members`, `l2f_experiment_plan_configs` |
| `DROP AFTER VERIFICATION` | 3 | `profiling.profiles`, `experiments.jobs`, `experiments.results` |
| `ARCHIVE` | 1 | `profiling.profile_ingest_attempts` |

### 1.1 The six explicitly required resolutions

**`profiling.profiles` vs `profiling.bam_profiles`.** `profiles` holds 0 rows; `bam_profiles`
holds all 75 and is what `profile_ingest.py` writes. `bam_profiles` is authoritative and is
retained (narrowed from 29 to 15 columns); `profiles` is dropped after verification.

**`experiments.jobs`/`results` vs the L2-F job/result model.** Both legacy tables hold 0 rows and
neither is written by any production path. They are dropped after verification. The L2-F design
becomes `experiment_jobs` + `execution_attempts` + `execution_results` + `execution_failures` +
`job_events`.

**`catalog.gatk_configs` vs accepted candidate/config payloads.** 0 rows, and it stores only
`config_hash` + `parameter_space_hash` with no payload binding. Replaced by
`experiments.candidate_configs`, where a configuration *is* its canonical payload artifact.

**Direct artifact URI usage.** All 227 URIs are decomposed into one `storage_backends` row (the
common corpus root) plus a relative `object_key` per location. The nine `file://` sites and the
publisher/verifier modules stop handling paths: they receive an artifact ID and get bytes from a
resolver.

**Repeated scientific hash storage.** 141 `char(64)` columns become one aggregate identity hash
per frozen entity plus artifact foreign keys. `catalog.dataset_registry`'s four component digests
become four artifact FKs; `bam_profiles`' three `*_sha256` columns are dropped because each already
equals the `content_sha256` of an artifact the row references. **No hash whose formula is part of
an accepted scientific identity is removed** — only the copies.

**Application role elevation.** The 4 `SET LOCAL ROLE` and 1 `SET ROLE` sites disappear.
`minos_owner` becomes a `NOLOGIN` definer principal; each runtime role gets exactly the
`EXECUTE` grants its stage needs.

No redundant production table is retained to keep old code working. Compatibility exists only as
the time-bounded qualification window in §3.

---

## 2. Strategy selection

| # | Strategy | Data safety | Rollback | Downtime | Complexity | Reproducible | Verifies all 0005 data |
|---|---|---|---|---|---|---|---|
| 1 | In-place transformation | **Low** — DDL and DML against the only copy | Restore-from-backup only | Hours | Medium | No — one-shot | Only after the fact |
| 2 | **Shadow DB-V2 tables in the same database, verified cutover** | **High** — V1 objects untouched until retirement | Point the application back; drop the shadow | **Minutes** | Medium | Yes — rerunnable | Yes, before cutover |
| 3 | Restore/copy into a separate qualification database, then replace | High | Swap back | Hours | **High** — the database must end up named `minos_engine_db`, forcing a rename or a full reload | Yes | Yes |

**Selected: strategy 2 — shadow tables in `minos_engine_db`, followed by verified cutover.**

The reasoning is specific to this system's current state, not a generic preference:

- The operational database is at `0005` and **migrations 0006–0008 were never applied**, so there
  is no L2-F production data to preserve. The entire transformable dataset is 13 tables and 668
  rows.
- Transformation is a pure function of existing rows, so it can be re-run until it is right. In
  strategy 1 the first attempt is also the last.
- V1 objects stay byte-identical throughout the qualification period, so rollback is switching a
  connection target — not a restore.
- Strategy 3 buys nothing here that strategy 2 lacks, and it introduces the one thing to avoid:
  the operational database must remain named `minos_engine_db`, so strategy 3 ends in either a
  rename dance or a full reload, both of which are riskier than the cutover they replace.

### 2.1 Effect on migrations 0001–0008 — explicitly analysed

**They are not rewritten.** Their byte hashes are frozen and cited in accepted evidence
(`0006` `1eb3a12b…`, `0007` `bc247e0a…`, `0008` `95614d67…`, F5 contract `8b7d8e89…`), and the F5/F6
qualification tests recompute them. Rewriting any of them would invalidate that evidence and every
gate that references it.

Instead, DB-V2 is expressed as **new forward migrations** (`0009` onward, not created in this
stage) that create the V2 schema alongside V1 and later retire V1. 0006–0008 remain in the lineage
as accepted, CI-verified history that was deliberately never applied to the operational database.
Their tables are therefore mapped as `(planned)` sources: superseded on paper, with no operational
data to move and no bytes to change.

---

## 3. Execution sequence

Ten steps. Each is separately reversible until step 8.

**1. Complete backup.** `pg_dump -Fc` of `minos_engine_db` plus the WAL position. Record a
`catalog.backup_sets` row.

**2. Artifact snapshot.** Enumerate all 227 active artifacts, verify each payload's SHA-256, and
record the snapshot digest, count and total bytes into the same `backup_sets` row.
`completeness = 'complete'` requires both halves; a dump alone is `'database_only'` and is **not**
a valid MINOS recovery set.

**3. Create the V2 schema.** Forward migration creating all 38 tables, constraints, indexes,
functions and role grants. No V1 object is touched.

**4. Deterministic transformation.** Copy and transform V1 → V2 inside one transaction per source
table, driven by the mapping report. Re-runnable: every insert is keyed and idempotent.

**5. Row-count and identity verification.** Run every `validation_query` in the mapping report.
Beyond counts: each of the 75 `dataset_registry.identity_tuple_hash` values must appear exactly
once as `datasets.identity_hash`; each of the four dataset digests must equal the
`content_sha256` of the artifact its new FK points at; each `bam_profiles.profile_sha256` must
equal the `content_sha256` of its `profile_artifact_id`; and each of the 227 locations must
reconstruct its original V1 URI byte-for-byte.

**6. Artifact verification.** Re-verify all 227 payloads against `catalog.artifacts` and set
`verification_state = 'verified'`. Any `missing` or `corrupt` row blocks cutover.

**7. Application cutover.** Switch connection roles and repository implementations to V2. This is
the only step with downtime, measured in minutes.

**8. Read-only qualification period — 14 days.** V2 serves production; V1 objects remain in place,
untouched and unread. Daily reconciliation (Q13) and a restore drill must both pass.

**9. Rollback procedure.** Before step 8 completes: point the application back at the V1
repositories and drop the shadow tables — V1 data was never modified. After step 10: restore from
the step-1/2 recovery set. The rollback path is exercised in a scratch cluster during D2, not
first attempted in production.

**10. Final retirement.** After qualification passes, drop `profiling.profiles`,
`experiments.jobs`, `experiments.results`, `catalog.datasets`, `catalog.gatk_configs`, the 10
views and the archived source table, each guarded by its `validation_query` returning 0 rows.

---

## 4. Acceptance gates

| Gate | Condition |
|---|---|
| G1 — design frozen | Contract hash stable; validator reports 0 problems |
| G2 — schema created | All 38 tables exist with declared constraints and indexes; V1 unmodified |
| G3 — transformation complete | Every mapping `validation_query` passes; re-run is a no-op |
| G4 — identity preserved | Every aggregate hash and every artifact digest verified equal |
| G5 — artifacts verified | 227/227 `verification_state = 'verified'`; 0 missing, 0 corrupt |
| G6 — performance | Every target in `performance_targets` met at stated scale |
| G7 — security | 0 `SET ROLE` occurrences; `minos_owner` cannot log in; each role holds only its declared grants |
| G8 — recovery | A restore drill reproduces the database **and** its artifact snapshot, and passes G4 |
| G9 — qualification | 14 days with no reconciliation failure and no stranded job |
| G10 — retirement | Every dropped object verified empty immediately before the drop |

---

## 5. Implementation sequence after D1

| Stage | Content |
|---|---|
| **D2** | Forward migration creating the V2 schema in a scratch cluster; no data movement. |
| **D3** | Deterministic transformation + the full validation-query suite, against a restored copy. |
| **D4** | Repository and role rework; the artifact resolver replaces all path handling. |
| **D5** | Performance qualification against `performance_targets`. |
| **D6** | Backup/restore drill proving a complete recovery set. |
| **D7** | Cutover, qualification period, retirement. |

Each stage requires explicit authorization and a user push, exactly as F3–F6 did. D2 does not
begin in this turn.
