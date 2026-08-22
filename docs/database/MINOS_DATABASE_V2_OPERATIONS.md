# MINOS Database V2 — Operations, Stability and Recovery

Companion to [the architecture](MINOS_DATABASE_V2_ARCHITECTURE.md),
[the ERD](MINOS_DATABASE_V2_ERD.md) and
[the migration plan](MINOS_DATABASE_V2_MIGRATION_PLAN.md).

**The governing rule of this document:** a PostgreSQL dump *without* its corresponding artifact
snapshot is an **incomplete MINOS recovery set**. Restoring one alone produces a database whose
artifact rows reference bytes that do not exist. The design fails closed rather than reading wrong
bytes, but the restore is still incomplete, and `catalog.backup_sets.completeness` records exactly
which kind of set you are holding.

---

## 1. Connection and session policy

| Setting | Value | Why |
|---|---|---|
| Pool size per process | 8 | 4 worker processes × 8 stays under 32 connections, well inside PostgreSQL's default 100 |
| `statement_timeout` | 30 s | No production query is designed to exceed 5 s; 30 s is the runaway guard |
| `lock_timeout` | 3 s | The claim path never waits (`SKIP LOCKED`); anything blocking 3 s is a defect |
| `idle_in_transaction_session_timeout` | 15 s | GATK runs outside transactions, so no legitimate transaction is idle this long |
| Isolation | `READ COMMITTED` | Every invariant is enforced by a constraint or a row lock; no path needs `SERIALIZABLE` |
| Verifier sessions | `default_transaction_read_only = on` | Verification cannot mutate even by mistake |
| Role elevation | prohibited | No `SET ROLE` / `SET LOCAL ROLE` on any runtime path |

Every production connection verifies `current_database()` and the exact Alembic revision as its
first statements, before any other query, file access or mutation.

---

## 2. Autovacuum and bloat

Three tables receive tuned settings; everything else uses cluster defaults, because at 668 rows
the defaults are already correct.

| Table | Setting | Value | Reason |
|---|---|---|---|
| `experiments.experiment_jobs` | `autovacuum_vacuum_scale_factor` | `0.02` | Every job is updated 3–5 times; dead tuples accumulate on the hot claim path |
| `experiments.experiment_jobs` | `autovacuum_analyze_scale_factor` | `0.02` | The partial-index claim plan depends on current `status` statistics |
| `experiments.job_events` | `autovacuum_vacuum_scale_factor` | `0.10` | Append-only; needs freezing, not dead-tuple collection |
| `experiments.execution_attempts` | `autovacuum_vacuum_scale_factor` | `0.10` | As above |
| `catalog.artifacts` | `fillfactor` | `90` | `last_verified_at` is updated in place by reconciliation |

Monitoring thresholds: alert when any table's dead-tuple ratio exceeds 20 % for over an hour, when
an index exceeds 2× its expected size, or when `experiment_jobs` exceeds 4× its live row count on
disk. Bloat is checked weekly with `pgstattuple` on those tables only.

---

## 3. Backups and recovery

### 3.0 Before and after the migration

Until `0009` runs there is nowhere in the database to record a recovery set: V1 has no
`catalog.backup_sets` and the V2 table does not exist yet. So the pre-migration record is an
immutable **file** beneath `MINOS_DB_RECOVERY_ROOT` (phase **R1**), and it is registered into
`dbv2_catalog.backup_sets` only after `0009` **and after the B0 artifact-catalog bootstrap**
(phase **R2**), where it must re-read equal and carry `completeness = 'complete'`. R2 cannot come
straight after `0009`: `0009` creates the shadow artifact catalog empty, so a complete snapshot has
nothing to be exact against. Nothing beyond B0 may be transformed until R2 has passed. The R1 file is retained afterwards:
downgrading `0009` destroys the row but never the file.

R1 runs inside a **write quiesce**: stop writes and artifact publication, drain write transactions
(record `quiesce_started_at`), take the backup and WAL position, enumerate and verify the
operational artifact snapshot, publish all three files, verify every published byte, then record
`quiesce_ended_at` and resume writes. Both timestamps are stored in the manifest and in the
registered row. Two shapes exist: a `complete` set carries the artifact snapshot and its counts and
may authorize a migration; a `database_only` set carries neither and may not. `completeness` is
immutable — a `database_only` set is never upgraded, only replaced by a new capture.

**`MINOS_DB_RECOVERY_ROOT`** has no default and no repository-relative fallback; if it is unset,
recovery publication fails closed. It must already exist as an absolute, non-symlink directory the
application never creates or repairs, mode `0o2750`, files `0o640`, on durable storage **separate
from** the Git checkout, the PostgreSQL data directory and the active artifact payload root — so
losing any one of those does not lose the recovery evidence. Layout is content-addressed:
`recovery/<sha>.recovery.json`, `backups/<sha>.dump`, `snapshots/<sha>.snapshot.json`. Recovery
files are never committed to Git.

Once DB-V2 is live under canonical names, the ordinary schedule below applies to
`catalog.backup_sets`.

### 3.1 A recovery set is two things

| Component | What it is | Recorded in |
|---|---|---|
| Database backup | `pg_dump -Fc` (or a base backup) + WAL range | `backup_sets.database_backup_sha256`, `wal_start_lsn`, `wal_end_lsn` |
| Artifact snapshot | Digest over the sorted `(content_sha256, size_bytes)` manifest of all **active** artifacts, plus the payloads themselves | `backup_sets.artifact_snapshot_sha256`, `artifact_count`, `artifact_total_bytes` |

`completeness` is `'complete'` only when both exist and the artifact payloads have been verified.
Otherwise it is `'database_only'`.

### 3.2 Schedule

| Activity | Frequency | Retention |
|---|---|---|
| WAL archiving (PITR) | continuous | 30 days |
| Full database backup | daily | 14 daily, 8 weekly, 12 monthly |
| Artifact snapshot | daily, paired with the database backup | matches the database backup |
| Artifact payload verification sweep | weekly (full) + continuous sampling | — |
| **Restore drill** | **monthly** | last 3 drills recorded in `audit.admin_operations` |
| Reconciliation (Q13) | hourly, bounded batch | — |
| Retention/archive sweep | weekly | — |

Recovery objectives: **RPO ≤ 5 minutes** (WAL archiving), **RTO ≤ 60 minutes** (restore ≤ 30 min +
artifact restore + verification).

### 3.3 Restore drill

A drill is only a pass if it exercises the whole set:

1. Restore the database backup into a **scratch** cluster — never over `minos_engine_db`.
   Before `0009`, the set being drilled is described by the R1 manifest file rather than a
   database row.
2. Restore the artifact snapshot to a scratch backend root.
3. Register the scratch backend and repoint locations to it.
4. Verify all artifact payloads against `catalog.artifacts`.
5. Re-run gate G4 (identity preservation).
6. Record the outcome in `audit.admin_operations` and stamp `backup_sets.restore_tested_at`.

A backup set that has never passed a drill is treated as unproven.

---

## 3a. D3-A: preparing a recovery set

*(Implemented and exercised on scratch PostgreSQL only. No operational R1 has been published and
no migration has been applied.)*

```bash
python scripts/dbv2_prepare_recovery.py build-r1
python scripts/dbv2_prepare_recovery.py bootstrap-artifacts --recovery-manifest-sha256 <sha>
python scripts/dbv2_prepare_recovery.py register-r2 --recovery-manifest-sha256 <sha>
python scripts/dbv2_prepare_recovery.py verify --recovery-manifest-sha256 <sha>
```

Four separate invocations, in that order, with the Alembic upgrade performed by an operator
between `build-r1` and `bootstrap-artifacts`. **No subcommand runs Alembic**, and there is no
combined command: each phase verifies the previous one against the database.

`build-r1` requires `0005_l2e_feature_view`; `bootstrap-artifacts` and `register-r2` require
`0009_dbv2_shadow_schema`. Environment: `MINOS_DATABASE_URL`, `MINOS_DB_RECOVERY_ROOT`,
`MINOS_DBV2_ARTIFACT_ROOTS` (`key=/absolute/path`, comma-separated) and `MINOS_DBV2_PG_DUMP`
(absolute). None of them has a default.

B1 — every transformation beyond the artifact catalog — is **not implemented**.

## 3a. Roles

*(D2: the preflight below is implemented and exercised.)* The nine cluster roles are **not**
created, altered or dropped by migration `0009`. Provision them (or verify them)
outside the migration, before it runs; `0009` preflights and aborts before creating any object if a
role is absent or incompatible. No password or credential belongs in a migration, a report or a
test — authentication is a cluster concern.

`minos_migrate` is the only DDL-capable login and must be a member of the NOLOGIN definer principal
`minos_owner`; the migration session issues `SET ROLE minos_owner` as its first statement so every
created object is owned by the definer. No runtime path ever issues `SET ROLE`.

The exact grants are the 830-record logical ACL matrix in
[`MINOS_DATABASE_V2_DATABASE_API.json`](../../reports/database/MINOS_DATABASE_V2_DATABASE_API.json),
not the prose in the role table. `PUBLIC` holds nothing; no runtime role holds any DDL privilege;
`evaluation.truth_bindings` is readable by `minos_evaluator` alone.

A downgrade drops DB-V2 objects and never a cluster role.

## 4. Recurring operational jobs

| Job | Cadence | Action |
|---|---|---|
| Stale-claim recovery | every 5 min | `RUNNING`/`CLAIMED` jobs whose `lease_expires_at` has passed return to `PENDING`; the open attempt is closed as `ABANDONED`. History is preserved — this is precisely why attempts are append-only. |
| Artifact reconciliation | hourly | Bounded batch from Q13; each payload re-verified; `verification_state` set to `verified`, `missing` or `corrupt`. Any non-`verified` result raises an alert and blocks release promotion. |
| Retention sweep | weekly | Artifacts past their `retention_class` window move `active → archived`. **Rows are never deleted**; only lifecycle state changes. |
| Backup + snapshot | daily | §3.2 |
| Restore drill | monthly | §3.3 |
| Bloat + slow-query review | weekly | §2, §5 |

---

## 5. Slow-query and capacity monitoring

`pg_stat_statements` is enabled. Alert when any of the 16 critical queries exceeds **2×** its
target in `performance_targets` at p95 over an hour, when mean claim latency (Q7) exceeds 25 ms,
or when any lock wait appears on the claim path at all — `SKIP LOCKED` means the correct value is
zero.

| Capacity signal | Threshold | Action |
|---|---|---|
| Connections | > 60 % of `max_connections` | Reduce pool size; investigate leaks |
| `experiment_jobs` rows | > 5,000,000 | Review claim-index selectivity |
| `job_events` rows | > 50,000,000 | **Enable monthly range partitioning** (contract `partitioning_policy`) |
| `execution_attempts` rows | > 50,000,000 | As above |
| `audit.events` rows | > 50,000,000 | As above |
| Database size | > 100 GB | Investigate — payload bytes must never be in PostgreSQL |
| Artifact count | > 5,000,000 | Review reconciliation batch sizing |

The database-size threshold is a correctness alarm as much as a capacity one: the design stores
**zero** payload bytes in PostgreSQL, so metadata alone reaching 100 GB means something is being
stored that should be an artifact.

---

## 5a. The deployment namespace and revision path

D2 creates its tables in the temporary `dbv2_*` schema namespace (37 shadow tables; the 38th
logical table is the shared `public.alembic_version`), because 9 canonical identities are already
occupied by live V1 relations. A later cutover renames the schemas so the canonical names become
real. `dbv2_*` never appears in application code.

The operational revision path is `0005 → 0006 → 0007 → 0008 → 0009`. Migrations `0006`–`0008` are
unapplied **today** and will execute as structural predecessors during a controlled preparation
window; after each of them, every L2-F table must hold zero business rows, and no artifact
publication, enqueue or execution may occur. No operational migration is authorized in D2: `0009` exists but runs on scratch clusters only.

## 6. Migration windows

Migrations run only through `minos_migrate`, never from a service process. Each migration must:

- run inside a maintenance window with services stopped or in read-only mode;
- record a `audit.admin_operations` row with the from/to revision and outcome;
- be preceded by a complete recovery set (§3.1);
- state its expected lock footprint in advance — any migration requiring `ACCESS EXCLUSIVE` on
  `experiment_jobs` must run with the queue drained.

Index creation on populated tables uses `CREATE INDEX CONCURRENTLY` outside a transaction.

---

## 7. Security posture

- `minos_owner` owns every object and every `SECURITY DEFINER` function and **cannot log in**.
  There is no elevation path from a runtime credential.
- No runtime path issues `SET ROLE` or `SET LOCAL ROLE`.
- Application roles hold no direct `INSERT`/`UPDATE`/`DELETE` on `experiments` tables; every
  mutation goes through a narrow function.
- Only `minos_evaluator` may read `evaluation.truth_bindings`. The execution path cannot reach
  truth data even by foreign-key traversal.
- No credential, password, DSN or endpoint secret is stored in PostgreSQL or in any committed
  report. `catalog.storage_backends` holds a logical root only.
- Backend credentials come from the process environment at connection time.
