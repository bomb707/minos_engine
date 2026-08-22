# MINOS Database V2 — Canonical Architecture

**Stage:** DB-V2 **D1.3** — design only. No migration exists; `0009` has not been created. No
production source has changed.
**Designed against:** `feature/L2-F` at `695d9227ed83c595e3ed03375a935fbe801aadbd`.
**Operational database:** `minos_engine_db`, live at `0005_l2e_feature_view`, **untouched by this
stage**.
**Contract hash:** `20f8b6eaa19622c2fff7bcc67c9e58b1f4667dc90795c9c2f4fa18efcb6020ba`
(over [`MINOS_DATABASE_V2_CONTRACT.json`](../../reports/database/MINOS_DATABASE_V2_CONTRACT.json),
excluding its own `contract_sha256` field).

**Physical deployment contract:**
[`MINOS_DATABASE_V2_PHYSICAL_DEPLOYMENT.json`](../../reports/database/MINOS_DATABASE_V2_PHYSICAL_DEPLOYMENT.json),
hash `9611245a6bd9a4fd2bad7f73c44e6ec2cdc4b62974b6faa3f8ff40620854d61b`.

Companion documents: [ERD](MINOS_DATABASE_V2_ERD.md) ·
[Migration plan](MINOS_DATABASE_V2_MIGRATION_PLAN.md) ·
[Operations](MINOS_DATABASE_V2_OPERATIONS.md).

---

## 1. What the audit found

The audit introspected the live database directly and statically scanned every `src/` module.
Everything below is a measured fact, not an inference; the complete object list is in
[`MINOS_DATABASE_V1_INVENTORY.json`](../../reports/database/MINOS_DATABASE_V1_INVENTORY.json).

| Measure | Live value |
|---|---|
| Database / cluster | `minos_engine_db` on `127.0.0.1:5433`, PostgreSQL 16.2 |
| Alembic revision (live) | `0005_l2e_feature_view` |
| Alembic head (source) | `0008_l2f_execution_results` |
| Schemas | 8 (`audit`, `catalog`, `evaluation`, `experiments`, `models`, `profiling`, `public`, `runtime`) |
| Tables / views | 23 / 10 |
| Columns | 353 |
| Constraints | 195 (23 PK, 34 unique, 28 FK, 110 check) |
| Indexes | 67 |
| Triggers / functions | 19 / 2 |
| Sequences / row-level policies | 0 / 0 |
| Roles | 5 (`minos_admin`, `minos_evaluator`, `minos_live`, `minos_runner`, `minos_trainer`) |
| Table grants to MINOS roles | 262 |
| Tables holding rows | 13 |
| **Tables holding zero rows** | **10** |

Row counts, in full: `catalog.artifacts` 227, `catalog.dataset_registry` 75,
`catalog.split_allocations` 75, `catalog.split_epoch_allocations` 75, `catalog.split_snapshots` 1,
`profiling.bam_profiles` 75, `profiling.profile_ingest_attempts` 75,
`profiling.profile_snapshot_members` 75, `profiling.feature_matrix_members` 60,
`profiling.feature_matrices` 2, `profiling.feature_sets` 1, `profiling.profile_snapshots` 1,
`public.alembic_version` 1. Every other table is empty.

The static scan found 9 engine-creation sites, 13 DSN-selection sites, 14 direct `INSERT`
statements, 1 direct `UPDATE`, 1 direct `DELETE`, 6 `SECURITY DEFINER` call sites, 4
`SET LOCAL ROLE` and 1 `SET ROLE`, 4 `SKIP LOCKED`, 3 advisory-lock sites, 9 `file://` sites, 71
path reads, 20 path writes and 11 raw `os.open` sites.

### 1.1 Confirmed defects

Twelve defects are recorded with evidence in the contract's `confirmed_v1_defects`. The six that
drive the redesign:

**D01 — artifacts are capped at 2 GiB.** `catalog.artifacts.size_bytes` is `integer`. The largest
payload today is 55 KB, so this has not bitten, but BAM inputs exceed 2 GiB routinely. V2 uses
`bigint`.

**D02 — artifact identity is welded to one host.** All 227 rows carry an absolute
`file:///home/hr/bittensor/minos_l2d_corpus/…` URI in a single `text` column. Moving the corpus,
adding a replica, or migrating to object storage would require rewriting artifact rows — that is,
changing records that other tables treat as immutable. V2 separates *what* an artifact is
(`catalog.artifacts`) from *where its bytes are* (`catalog.artifact_locations` →
`catalog.storage_backends`), so a local-to-S3 migration changes no artifact identity at all.

**D03 — artifacts have no lifecycle.** The table has seven columns and no notion of retention,
archival, verification time or corruption state. Reconciliation cannot be expressed as a query.

**D04/D05/D06 — three entities are modelled twice each.** `catalog.datasets` (0 rows, 4 columns)
and `catalog.dataset_registry` (75 rows, 24 columns) both model a dataset. `profiling.profiles`
(0 rows) and `profiling.bam_profiles` (75 rows) both model a profile. `experiments.jobs`/`results`
(0 rows) coexist with the L2-F job/result design in migrations 0006–0008. In each pair the
unused table is the one production abandoned.

**D08 — there is no attempt history.** Nothing in 0001–0008 records an execution *attempt*. A
retried job would overwrite the only record of the previous run. V2 introduces
`experiments.execution_attempts` as an append-only table and binds results and failures to an
attempt, not directly to a job.

**D10 — scientific hashes are copied everywhere.** There are 141 `char(64)` columns across 46
distinct names, and 21 of those names appear in 2–9 different tables:
`registry_snapshot_hash` in 9, and `region_hash`, `bam_sha256`, `bai_sha256`, `reference_sha256`,
`fai_sha256`, `manifest_hash` and `feature_values_hash` in 8 each. Every copy is a place the value
can diverge, and every copy has to be verified.

---

## 2. The design in one page

Four rules generate almost everything else.

**One authoritative database.** `minos_engine_db` is the only operational PostgreSQL database, and
it is authoritative for datasets, artifacts, profiles, snapshots, features, parameter spaces,
candidates, plans, jobs, attempts, results, failures, evaluation, models, runtime state, audit,
releases, backups and retention. No layer gets its own database. Ephemeral clusters remain
permitted for tests only.

**Bytes live outside; the database governs them completely.** BAM/BAI, FASTA/FAI/dict, Parquet,
VCF and large evidence payloads are never stored in PostgreSQL. Each is an artifact row carrying
its UUID, content SHA-256, exact size, media type, schema version, storage mode, lifecycle state,
retention class, provenance and verification state, plus one or more location rows naming a
backend and a **relative** object key. A payload without an active verified record is unusable; a
record whose payload is absent or corrupt fails closed. Small canonical JSON may be stored inline
only under an explicit bounded policy (≤ 64 KiB, stored as `bytea` holding the exact canonical
bytes so hashing stays byte-deterministic).

**IDs for business logic, hashes for integrity.** Every externally referenced entity has a UUID
primary key, and relationships are foreign keys. Production APIs pass `dataset_id`, `snapshot_id`,
`matrix_id`, `plan_id`, `job_id`, `attempt_id`, `result_id`, `artifact_id`. Each frozen scientific
entity keeps exactly **one** aggregate identity hash; each artifact keeps exactly **one** content
SHA-256. Component digests are reached by traversal, not by duplication. No scientifically required
hash is removed — `plan_hash`, `job_key`, `result_hash`, `config_hash`, `parameter_space_hash`,
`candidate_set_hash`, `input_identity_hash` and `logical_argv_hash` all survive with their formulas
untouched. What goes is the *copying*.

**Current state is a row; history is a log; the log is never replayed.** `experiments.
experiment_jobs` holds one narrow mutable row per logical job — the claim path reads nothing else.
`execution_attempts`, `execution_results`, `execution_failures` and `job_events` are append-only.
Current state is never reconstructed from the event log.

### 2.1 Target inventory

38 tables across 8 schemas. These are the **final application contract** — the names the code
will use after cutover. They are *not* the names D2 creates; see §2.2.



| Schema | Tables | Names |
|---|---:|---|
| `catalog` | 6 | `storage_backends`, `artifacts`, `artifact_locations`, `datasets`, `releases`, `backup_sets` |
| `profiling` | 6 | `bam_profiles`, `profile_snapshots`, `profile_snapshot_members`, `feature_sets`, `feature_matrices`, `feature_matrix_members` |
| `experiments` | 12 | `parameter_spaces`, `candidate_configs`, `candidate_sets`, `candidate_set_configs`, `experiment_plans`, `experiment_plan_members`, `experiment_plan_configs`, `experiment_jobs`, `execution_attempts`, `execution_results`, `execution_failures`, `job_events` |
| `evaluation` | 4 | `truth_bindings`, `evaluation_runs`, `evaluation_metrics`, `evaluation_scores` |
| `models` | 4 | `model_definitions`, `training_runs`, `model_versions`, `model_activations` |
| `runtime` | 3 | `service_instances`, `leases`, `active_selections` |
| `audit` | 2 | `events`, `admin_operations` |
| `public` | 1 | `alembic_version` |

Ten V1 views disappear. They exist because partition and snapshot identity were spread across
several tables and had to be re-joined; in V2 a single indexed predicate on
`profile_snapshot_members (snapshot_id, partition)` answers all of them.

### 2.2 The physical shadow namespace

D1 said the V2 tables would be created alongside untouched V1 tables. That is not directly
possible: **9 of the 38 logical identities are already occupied by live V1 relations** —
`catalog.artifacts`, `catalog.datasets`, `profiling.bam_profiles`, `profiling.feature_matrices`,
`profiling.feature_matrix_members`, `profiling.feature_sets`, `profiling.profile_snapshots`,
`profiling.profile_snapshot_members` and `audit.events`. A tenth identity,
`public.alembic_version`, is shared rather than colliding.

D1.1 resolved this with a frozen **temporary physical schema namespace**, still in force:

| Canonical (final) | D2 physical (temporary) | After cutover |
|---|---|---|
| `catalog` | `dbv2_catalog` | `catalog` |
| `profiling` | `dbv2_profiling` | `profiling` |
| `experiments` | `dbv2_experiments` | `experiments` |
| `evaluation` | `dbv2_evaluation` | `evaluation` |
| `models` | `dbv2_models` | `models` |
| `runtime` | `dbv2_runtime` | `runtime` |
| `audit` | `dbv2_audit` | `audit` |
| `public.alembic_version` | *shared, not duplicated* | unchanged |

So **D2 creates exactly 37 shadow tables**; the 38th logical table is the existing shared
`public.alembic_version`. Every logical table maps to exactly one physical table, no shadow object
collides with V1, and every DB-V2 foreign key resolves inside the shadow namespace.

`dbv2_*` are **deployment names, not the application contract**. No application code ever
references one. D2 and D3 never rename, alter, delete or write any V1 object.

---

## 3. Artifact subsystem

Three tables replace one.

`catalog.storage_backends` names a backend (`local_fs`, `s3`, `minio`) and its **logical root**.
It holds no credential, key or endpoint secret — those come from the process environment at
connection time and are never written to PostgreSQL.

`catalog.artifacts` is the logical artifact: one row per distinct `content_sha256`, carrying
`artifact_kind`, exact `size_bytes` (`bigint`), `media_type`, `schema_version`, `storage_mode`,
`lifecycle_state` (`active`/`archived`/`quarantined`/`deleted`), `retention_class`
(`permanent`/`long`/`standard`/`ephemeral`), bounded `provenance` JSONB, and
`verification_state` (`unverified`/`verified`/`missing`/`corrupt`) with first/last verification
timestamps.

`catalog.artifact_locations` says where the bytes are: `(artifact_id, backend_id, object_key,
location_state, is_primary)`. `object_key` is relative and is constrained against a leading `/` and
against `..`, so a location can never address outside its backend root.

Two consequences follow directly. Migrating from the local corpus to S3/MinIO inserts a second
location row and flips `is_primary`; **no artifact identity changes**, so no downstream table is
touched. And reconciliation becomes a plain indexed query (Q13) instead of a filesystem crawl.

Domain tables reference artifacts by **explicit foreign key** — `datasets.bam_artifact_id`,
`bam_profiles.profile_artifact_id`, `execution_results.vcf_artifact_id`, and so on. There is no
generic `entity_type`/`entity_id` association table anywhere in the design.

---

## 4. Job and execution model

Five concerns, five tables:

1. **Logical identity** — `experiment_jobs (plan_id, plan_member_id, plan_config_id, job_key)`,
   unique on both the triple and the key.
2. **Current status** — the same row's `status`, `attempt_count`, `claimed_by`, `claimed_at`,
   `lease_expires_at`. Narrow and hot.
3. **Attempts** — `execution_attempts`, append-only, unique on `(job_id, attempt_number)`.
4. **Transitions** — `job_events`, append-only, for audit and diagnosis only.
5. **Outcomes** — `execution_results` (success) and `execution_failures` (bounded failure), each
   unique per **attempt**, so a retry adds a record instead of overwriting one.

State machine — `PENDING → CLAIMED → RUNNING → SUCCEEDED | FAILED`, plus `CLAIMED → PENDING`
(release) and `RUNNING → PENDING` (stale-lease reclamation, new in V2). `CANCELLED` remains
unreachable.

Enforcement is layered deliberately:

| Invariant | Enforced by |
|---|---|
| Job identity uniqueness | unique constraints |
| Claim metadata consistency with status | check constraint |
| Result/failure bound to a real attempt of a real job | composite foreign keys |
| Exactly one outcome per attempt | unique constraints |
| Legal transitions, single-row terminal update | narrow `SECURITY DEFINER` functions |
| Append-only history | trigger (a constraint cannot express "no UPDATE") |

Triggers are used **only** where neither a constraint nor a function can enforce the invariant —
append-only enforcement and outcome mutual exclusion. No business workflow is implemented in a
trigger.

---

## 5. Roles and connections

V1 has five roles, `minos_admin` owns every object with `DELETE`/`TRUNCATE` on all 32 relations,
and runtime code elevates to it via `SET LOCAL ROLE` in four places. V2 removes that dependence
entirely.

| Role | Login | Responsibility |
|---|---|---|
| `minos_owner` | **no** | Owns every object and every `SECURITY DEFINER` function. Unreachable by any runtime credential. |
| `minos_migrate` | yes | Alembic only. The sole holder of DDL rights. |
| `minos_planner` | yes | Plan and config persistence. |
| `minos_enqueue` | yes | Bounded enqueue. Cannot claim or transition. |
| `minos_runner` | yes | Claim, transition, record outcomes — through functions only. |
| `minos_verifier` | yes | Read-only; session runs `default_transaction_read_only = on`. |
| `minos_trainer` | yes | Reads features, writes models. |
| `minos_evaluator` | yes | The **only** role that may read `evaluation.truth_bindings`. |
| `minos_live` | yes | Reads releases and active selection; writes heartbeats. |

No runtime path issues `SET ROLE` or `SET LOCAL ROLE`. Because `minos_owner` cannot log in, an
attacker holding a runtime credential has no elevation path — the object owner is not reachable
by password at all.

Every production connection verifies `current_database() = 'minos_engine_db'` **and** the exact
expected Alembic revision as its first statements, before any other query, file access or
mutation. This preserves the F6 exact-connection rule: a successful check on one connection never
authorizes another.

Connection discipline: pool of 8 per process, `statement_timeout` 30 s, `lock_timeout` 3 s,
`idle_in_transaction_session_timeout` 15 s, `READ COMMITTED` throughout (no path requires
`SERIALIZABLE`). GATK never runs inside a transaction.

---

## 6. Performance

The 16 critical queries, their expected cardinality, index and locking behaviour are enumerated in
the contract's `critical_queries` and reproduced in the [ERD](MINOS_DATABASE_V2_ERD.md#critical-queries).
The shapes that matter:

- **Claim (Q7)** rides a partial index `ix_jobs_claim (plan_id, created_at, id) WHERE status =
  'PENDING'`, with `FOR UPDATE SKIP LOCKED LIMIT 1`. It touches one row and never waits.
- **Terminal transition (Q9)** locks exactly one job row `FOR UPDATE`, which is what serializes a
  racing success and failure, then requires its `UPDATE` to affect exactly one row.
- **Plan persistence (Q5)** takes a `pg_advisory_xact_lock` keyed on `plan_hash` — never a table
  lock.
- **Verification (Q11)** runs read-only and rolls back.

Acceptance targets are set before implementation (contract `performance_targets`): dataset
resolution p95 ≤ 5 ms, snapshot membership ≤ 15 ms, plan persistence ≤ 750 ms, bounded enqueue of
64 ≤ 250 ms, claim under 8 concurrent workers ≤ 25 ms, transition ≤ 15 ms, result persistence
(excluding GATK) ≤ 120 ms, artifact metadata lookup ≤ 10 ms, full 1,950-job verification ≤ 5 s,
≤ 32 connections at steady state, zero lock wait on the claim path, backup ≤ 10 min, restore to a
qualification database ≤ 30 min, and **zero** bytes of payload growth attributable to artifacts
(metadata only, ≤ 2 KB per row).

Scale used: 75 datasets, 50 accepted train members, 39 candidate configurations, 1,950 logical
jobs, 227 artifacts / 5.2 MB. One-year projections (≈400k jobs, 2.5M job events, ~900k artifacts)
are recorded, but **nothing is partitioned for them**. Partitioning is enabled per table only when
a measured threshold is crossed — 50M rows or 50 GB on `job_events`, `execution_attempts` or
`audit.events` — at which point those three (and only those three) switch to UUIDv7 keys and
monthly range partitions.

---

## 6a. Recovery, rollback and retirement

Three sequencing defects in the D1 runbook are corrected; each was an execution defect, not a
wording preference.

**The recovery set is two-phase.** D1 required a `catalog.backup_sets` row as step 1, but V1 has
no such relation and the V2 one is created by the migration step 1 precedes. **R1** writes an
immutable manifest *file* before any migration, beneath the externally provisioned
`MINOS_DB_RECOVERY_ROOT` — which has **no default and no repository-relative fallback**, and must
sit on durable storage separate from the Git checkout, the PostgreSQL data directory and the
artifact payload root. **R2** registers that exact manifest into `dbv2_catalog.backup_sets` after
`0009` and before any transformation.

`backup_sets` binds **three** artifacts by composite foreign key — the recovery manifest, the
database backup and the artifact-snapshot manifest — each through
`uq_artifacts_id_sha_media (id, content_sha256, media_type)`, so a digest column can never name a
different artifact than its id column. `recovery_manifest_sha256` is
`sha256(canonical_json_bytes(manifest))` over the whole manifest, making R1↔R2 byte-verifiable.
`completeness` reaches `'complete'` only once all three artifacts are verified. R1 files are
retained — downgrading `0009` removes the rows but never a recovery file.

**There are three rollback boundaries, not one.** Before `0009` there is nothing to undo. After
`0009` but before cutover, `alembic downgrade 0009 → 0008` removes only `dbv2_*` objects and never
touches V1. After cutover, rollback is the **inverse schema rename** — and it never drops V2,
because after cutover the V2 tables *are* the live system. The earlier claim that post-cutover
rollback means "point at V1 and drop the shadow tables" is withdrawn: it would delete the migrated
database.

**Retirement targets only `v1_retired_*`.** After cutover every canonical name is a live V2
object; `catalog.datasets` is simultaneously a declared logical V2 table, so a canonical
retirement target would destroy the migrated system. `catalog.*`, `profiling.*`, `experiments.*`,
`evaluation.*`, `models.*`, `runtime.*` and `audit.*` must survive retirement. A machine check in
the validator refuses any retirement target that begins with a canonical active schema name.

Details, including the qualification-period naming semantics, are in
[the migration plan](MINOS_DATABASE_V2_MIGRATION_PLAN.md#30-the-recovery-set-is-two-phase) and the
physical deployment contract.

## 7. What this stage did not do

No migration was written. No production source, test, gate, manifest, prerequisite or evidence
file was modified. `0009` does not exist. Migrations 0001–0008 remain byte-identical:
`0006` `1eb3a12b…`, `0007` `bc247e0a…`, `0008` `95614d67…`. The operational database was opened
read-only and remains at `0005_l2e_feature_view` with zero L2-F tables and zero L2-F rows.

F7, `HARNESS-READY`, scoring, training, optimization and `select_config` activation all remain
absent; `Layer2Service.select_config` still raises `StageNotReadyError`.
