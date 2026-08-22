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
2. Restore the artifact snapshot to a scratch backend root.
3. Register the scratch backend and repoint locations to it.
4. Verify all artifact payloads against `catalog.artifacts`.
5. Re-run gate G4 (identity preservation).
6. Record the outcome in `audit.admin_operations` and stamp `backup_sets.restore_tested_at`.

A backup set that has never passed a drill is treated as unproven.

---

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
