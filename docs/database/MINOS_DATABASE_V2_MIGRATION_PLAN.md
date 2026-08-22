# MINOS Database V2 — Migration and Cutover Plan

Companion to [the architecture](MINOS_DATABASE_V2_ARCHITECTURE.md) and
[the ERD](MINOS_DATABASE_V2_ERD.md). The complete object-by-object mapping is in
[`MINOS_DATABASE_V2_CURRENT_TO_TARGET.json`](../../reports/database/MINOS_DATABASE_V2_CURRENT_TO_TARGET.json).

**Migration `0009` now exists** and has been exercised on scratch PostgreSQL only. Nothing in this
document has been executed against the operational database, which remains at
`0005_l2e_feature_view`. Steps 5 onwards — transformation, verification, cutover, retirement —
remain unexecuted and unauthorized. The physical deployment names and the revision path are frozen in
[`MINOS_DATABASE_V2_PHYSICAL_DEPLOYMENT.json`](../../reports/database/MINOS_DATABASE_V2_PHYSICAL_DEPLOYMENT.json).

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
| 2 | **Shadow DB-V2 tables in the same database, verified cutover** | **High** — V1 objects untouched until retirement | Boundary-aware: pre-cutover `downgrade 0009 → 0008`; post-cutover transactional inverse schema rename | **Minutes** | Medium | Yes — rerunnable | Yes, before cutover |
| 3 | Restore/copy into a separate qualification database, then replace | High | Swap back | Hours | **High** — the database must end up named `minos_engine_db`, forcing a rename or a full reload | Yes | Yes |

**Selected: strategy 2 — shadow tables in `minos_engine_db`, followed by verified cutover.**

### 2.0 What D1 got wrong, and how D1.1 fixed it

D1 described "shadow DB-V2 tables in the same database" as if the canonical names were free. They
are not: **9 of the 38 logical identities are already occupied by live V1 relations**
(`catalog.artifacts`, `catalog.datasets`, `profiling.bam_profiles`, `profiling.feature_matrices`,
`profiling.feature_matrix_members`, `profiling.feature_sets`, `profiling.profile_snapshots`,
`profiling.profile_snapshot_members`, `audit.events`), and `public.alembic_version` is shared.
Two tables cannot hold the same schema-qualified name.

D1.1 freezes a temporary physical schema namespace — `catalog` → `dbv2_catalog`, and so on for
all seven canonical schemas — so D2 creates **37 shadow tables** with no collision, and the 38th
logical table is the existing shared `public.alembic_version`. The logical contract is unchanged.
`dbv2_*` are deployment names; a later cutover renames the schemas so the canonical names are
real again.

The reasoning is specific to this system's current state, not a generic preference:

- The operational database is at `0005` and migrations 0006–0008 **have not been applied yet**, so
  there is no L2-F production data to preserve. The entire transformable dataset is 13 tables and
  668 rows. They are *structural predecessors that will be executed* during the controlled
  operational preparation described in §2.2 — not revisions that stay unapplied forever.
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
stage) that create the V2 shadow schema alongside V1 and later retire V1. 0006–0008 remain in the
lineage as accepted, CI-verified history whose bytes never change.

### 2.2 The Alembic deployment sequence, stated truthfully

A `0009` with `down_revision = 0008_l2f_execution_results` **cannot be applied to a database at
`0005`**. Alembic will execute `0006`, `0007` and `0008` first — that is not optional, and it is
not something to work around. D1 implied those revisions would remain unapplied after DB-V2
deployment; that state is unreachable and the claim is withdrawn.

The frozen sequence:

| Context | Path |
|---|---|
| Development / lifecycle testing (**scratch PostgreSQL only**) | `0008 → 0009 → 0008 → 0009` |
| Operational state **today** | `0005` |
| Future **controlled** operational preparation | `0005 → 0006 → 0007 → 0008 → 0009` |

Invariants for the intermediate revisions:

1. `0006`, `0007` and `0008` remain **byte-identical** — `1eb3a12b…`, `bc247e0a…`, `95614d67…`.
2. They are unapplied operationally **today**, and will execute as structural predecessors during
   the controlled preparation.
3. After **each** intermediate revision, every L2-F table must hold **zero business rows**.
4. No artifact publication, job enqueue or execution may occur during the intermediate revisions.
5. No application path runs Alembic automatically; migration is an explicit operator action.
6. **No operational migration is authorized in D2.** `0009` runs on scratch clusters only.

None of the following is used, now or later: `alembic stamp`, a skipped revision, a rewrite of
`0001`–`0008`, a manual edit of `alembic_version`, a permanent multiple-head graph, a destructive
in-place conversion, or an undocumented table-name suffix.

---

## 3. Execution sequence

Each step is separately reversible until step 8. The rollback that applies depends on **where you
are**, which is why §3.1 enumerates three distinct boundaries rather than one procedure.

### 3.0 The recovery set is two-phase

D1 said "record a `catalog.backup_sets` row" as step 1. That is unexecutable: V1 has **no**
`catalog.backup_sets` relation, and the V2 one is created by the very migration step 1 precedes.
The recovery set is therefore captured in two phases.

**R1 — before any operational migration, inside one write quiesce.** The seven steps are:
stop application writes and artifact publication; drain active write transactions and record
`quiesce_started_at`; take the `pg_dump -Fc` and the WAL position inside the quiesced window;
enumerate and verify the operational artifact snapshot using the exact predicate
`lifecycle_state = 'active' AND backup_scope = 'operational'`; publish the backup, the snapshot
manifest and the recovery manifest beneath `MINOS_DB_RECOVERY_ROOT`; re-read and verify every
published byte; record `quiesce_ended_at` and resume writes only once the complete set is durable.
An independently timed `pg_dump` and filesystem scan do **not** form one recovery point — an
artifact published between them lands in one half only — so the quiesce window is what makes the two
halves one state, and it is recorded in the manifest so the claim is auditable. If any step fails,
`0009` is not authorized. Write a strict, immutable **file** beneath the
externally provisioned recovery root, at
`<MINOS_DB_RECOVERY_ROOT>/recovery/<recovery_manifest_sha256>.recovery.json`, binding all sixteen
R1 fields: schema version, `recovery_set_id`, database name, source Alembic revision, backup kind,
backup SHA-256 and size, WAL start/end LSN, artifact snapshot SHA-256, artifact count, artifact
total bytes, creation time, PostgreSQL version, backup tool version and artifact verification tool
version. This is a file, **not** a database row, and must not be described as one. A dump without
its matching artifact snapshot is **incomplete** and may not authorize a migration.

`MINOS_DB_RECOVERY_ROOT` has **no default and no repository-relative fallback** — a default would
silently select the source checkout. The root must already exist as an absolute, non-symlink
directory that the application never creates or repairs, on durable storage **separate from** the
Git checkout, the PostgreSQL data directory and the active artifact payload root. Publication is
atomic: temp file → fsync → credential verify → no-clobber hard link → directory fsync → re-read
and verify the digest. Recovery files are never committed to Git.

**R2 — after `0009` creates the shadow schema, before any transformation.** Read the R1 bytes,
strictly parse them with duplicate-key rejection, recompute
`recovery_manifest_sha256 = sha256(canonical_json_bytes(manifest))`, publish and get-or-verify
**three** artifacts — the recovery manifest, the database backup and the artifact-snapshot
manifest — then insert `dbv2_catalog.backup_sets` with their exact artifact ids, digests and media
types. Re-read the row *and* the three artifact rows and require field-for-field equality with the
R1 manifest. `completeness` becomes `'complete'` only once all three artifacts are `verified`.

The three composite foreign keys resolve through the existing
`uq_artifacts_id_sha_media (id, content_sha256, media_type)` target, so a digest column can never
name a different artifact than its id column. Re-running R2 is idempotent; the same
`recovery_set_id` or `backup_key` with any differing immutable value **fails closed**.

The row is **never** written to `catalog.backup_sets`. Downgrading `0009` removes the rows but
**must not delete any R1 recovery file** — those files outlive every database object and are the
authoritative pre-migration record.

### 3.1 The ten steps

**1. R1 — complete backup.** `pg_dump -Fc` plus the WAL position. Verify it restores into a
scratch cluster.

**2. R1 — artifact snapshot.** Enumerate all active artifacts, verify each payload's SHA-256, and
write the R1 manifest file with the snapshot digest, count and total bytes. `completeness` is
`'complete'` only when both halves exist and verify; a dump alone is `'database_only'` and is
**not** a valid MINOS recovery set.

**3. Create the V2 shadow schema.** *(Implemented in D2, exercised on scratch PostgreSQL.)*
Forward migration `0009` creating the **37 shadow tables** in
the `dbv2_*` namespace, with their constraints, indexes, **34 functions, 89 triggers** and the
800-record ACL matrix. `public.alembic_version` is shared, not duplicated. **No V1 object is
touched** — not renamed, not altered, not deleted, not written.

`0009` begins with a **role preflight**: `SET ROLE minos_owner`, then verify every required cluster
role exists, that `minos_owner` is NOLOGIN and `minos_migrate` is a member of it, and that no
required role is a superuser. Only then is any object created, and only then are grants applied.
Roles are cluster objects: `0009` creates none, and a downgrade drops none.

**4. R2 — register the recovery set.** Publish the recovery artifacts with
`backup_scope = 'recovery'`, insert `dbv2_catalog.backup_sets` bound to them, and let the CONSTRAINT
trigger `trg_backup_sets_shape` verify all fifteen cross-table conditions in the same transaction.
Transformation may not begin until this passes and `completeness = 'complete'`; a `database_only`
row explicitly may not authorize it.

**5. Deterministic transformation.** Copy and transform V1 → the `dbv2_*` shadow tables, one
transaction per source table, driven by the mapping report. Re-runnable: every insert is keyed and
idempotent. V1 remains read-only throughout.

**6. Row-count and identity verification.** Run every `validation_query` in the mapping report,
plus the identity checks: each of the 75 `dataset_registry.identity_tuple_hash` values appears
exactly once as `dbv2_catalog.datasets.identity_hash`; each of the four dataset digests equals the
`content_sha256` of the artifact its new FK points at; each `bam_profiles.profile_sha256` equals
the `content_sha256` of its `profile_artifact_id`; and each of the 227 locations reconstructs its
original V1 URI byte-for-byte.

**7. Artifact verification.** Re-verify all 227 payloads and set `verification_state = 'verified'`.
Any `missing` or `corrupt` row blocks cutover.

**8. Cutover — a schema rename, not a switch.** See §3.2.

**9. Read-only qualification period — 14 days.** V2 serves production under the canonical names.
`v1_retired_*` holds the rollback source and is never written. No V2 object is deleted. Daily
reconciliation and a restore drill must both pass.

**10. Final retirement.** Only after qualification passes, and only within `v1_retired_*`. See
§3.4.

### 3.2 Cutover

With writes stopped and transactions drained, inside one transaction: rename each canonical V1
schema to `v1_retired_*`, then rename each `dbv2_*` schema to its canonical name, then revalidate,
then commit only if every validation passes.

**PostgreSQL does not make this free.** Foreign keys, indexes, sequences and parse-tree-stored
view and default definitions track their objects by OID and follow a schema rename automatically.
These do **not**:

- a **function body**, stored as text and re-parsed at execution time — a schema-qualified
  reference inside a `SECURITY DEFINER` body keeps naming the *old* schema and would silently
  resolve to the retired V1 object;
- `SET search_path` attached to a function, a role or the database, which names schemas as
  strings;
- `regclass` / `regprocedure` values stored as text rather than as the typed value;
- any schema name embedded in application SQL or configuration.

So every `SECURITY DEFINER` function, every `SET search_path` and every textual object reference
must be re-created or re-verified **inside the cutover transaction**. A rename alone is not a
cutover.

### 3.3 Three rollback boundaries

There is no single "the rollback". Which procedure applies depends on where the deployment is.

| Boundary | State | Procedure | Drops V2? | Touches V1? |
|---|---|---|---|---|
| **B1** before `0009` | no DB-V2 object exists | nothing to undo; if recovery is needed, restore from the verified R1 set | n/a | no |
| **B2** after `0009`, before cutover | `dbv2_*` exists; canonical is still V1 | `alembic downgrade 0009 → 0008`, removing **only** `dbv2_*` objects | yes — correct here, nothing live is lost | **no** |
| **B3** after cutover, in qualification | canonical is V2; `v1_retired_*` is the rollback source | quiesce → rename canonical V2 back to `dbv2_*` → rename `v1_retired_*` back to canonical → revalidate → commit only if all pass | **never** | renamed back only; never altered, deleted or written |

At **B2**, `public.alembic_version` changes only through Alembic's own bookkeeping — never by hand.

At **B3**, the returned `dbv2_*` schema is **not dropped** inside the rollback transaction: it is
the evidence of what was rolled back. Retiring V2 is a separately authorized cleanup, only after
recovery is proven.

**Withdrawn:** the earlier statement that post-cutover rollback is done by "pointing the
application back at the V1 repositories and dropping the shadow tables". After cutover the shadow
tables *are* the live system under canonical names; dropping them would delete the migrated
database, and "V1 repositories" no longer resolve to V1.

### 3.3b Function bodies are re-created inside the cutover transaction

A plpgsql body is stored as TEXT and re-parsed at execution, so a schema-qualified reference does
**not** follow a schema rename. All 34 DB-V2 functions are therefore `CREATE OR REPLACE`d inside the
cutover transaction itself, immediately after the renames and before revalidation, with their bodies
qualified against the canonical names; the rollback transaction restores the `dbv2_*`-qualified
bodies at the same point. Triggers track their table and function by OID and survive the rename
untouched — it is only the text inside the bodies that has to be rewritten.

### 3.4 Final retirement

**Retirement may affect only the `v1_retired_*` namespace.** After cutover, every canonical name
is a live V2 object — `catalog.datasets` is simultaneously a declared logical V2 table, so an
instruction to "drop `catalog.datasets`" would destroy the migrated system.

These must **survive** retirement: `catalog.*`, `profiling.*`, `experiments.*`, `evaluation.*`,
`models.*`, `runtime.*`, `audit.*`.

Eligible targets, all in the retired namespace:

| Target | Why |
|---|---|
| `v1_retired_profiling.profiles` | superseded by `profiling.bam_profiles`; 0 rows |
| `v1_retired_experiments.jobs` | legacy job model; 0 rows |
| `v1_retired_experiments.results` | legacy result model; 0 rows |
| `v1_retired_catalog.datasets` | the unused 4-column duplicate |
| `v1_retired_catalog.gatk_configs` | superseded by `experiments.candidate_configs`; 0 rows |
| the 10 retired V1 views | replaced by one indexed predicate |
| archived source objects | `v1_retired_profiling.profile_ingest_attempts` |

Immediately before each removal, verify: no remaining foreign key, view, function or trigger
depends on the object; its row count matches what was recorded at cutover (or is zero where zero
was expected); every identity it carried is provably present in its V2 successor; and it has not
been written since cutover. Prefer dropping a complete `v1_retired_*` schema, and only once every
object it contains has individually passed.

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
| G8a — recovery order | R1 precedes `0009`; R2 follows `0009` and precedes transformation |
| G8b — rollback | each of B1/B2/B3 has exactly one applicable procedure, and B3 drops no V2 object |
| G9 — qualification | 14 days with no reconciliation failure and no stranded job |
| G10 — retirement | Every target is in `v1_retired_*`, and each is dependency- and identity-verified immediately before the drop |

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
