# Layer 2 — Storage Architecture (L2-B, DB-READY)

PostgreSQL 16 storage foundation for Layer 2. SQLAlchemy 2 typed declarative models,
Alembic migrations, seven application schemas, five least-privilege roles, database
constraints, and append-only evidence protection. L2-B creates **empty** structures
and their security rules only — no scientific or practice-dataset records are
populated. `Layer2Service.select_config` remains blocked (`StageNotReadyError`).

## Technology
CPython 3.12 · PostgreSQL 16 · SQLAlchemy 2.x (synchronous) · Alembic · psycopg 3.
Configuration comes only from `MINOS_DATABASE_URL` (no committed credentials, no
hard-coded host, no SQLite fallback). Importing `minos_engine` opens no connection;
engine/session creation is explicit (`storage/database.py`) and fails closed.

## Schema ownership (seven schemas)
| Schema | Purpose |
|---|---|
| `catalog` | Frozen identities: artifacts (URI+SHA-256), GATK configs, dataset identity shell. |
| `profiling` | Layer 1 profile identity + all mandatory input hashes (append-only). |
| `experiments` | Experiment jobs (worker-claim state) and append-only result shells. |
| `evaluation` | Isolated offline evaluation evidence (inaccessible to `minos_live`). |
| `models` | Model-bundle identity + future binding columns (append-only). |
| `runtime` | Decision identity/manifest shell (append-only). |
| `audit` | Append-only audit events. |

Schema and role names are centralized in `storage/constants.py`.

## Tables and relationships
| Table | Key columns | Relationships |
|---|---|---|
| `catalog.artifacts` | `id` PK, `sha256` UNIQUE (hex64), `uri`, `size_bytes≥0` | referenced by models |
| `catalog.gatk_configs` | `id` PK, `config_hash` UNIQUE, `parameter_space_hash` | referenced by jobs, decisions |
| `catalog.datasets` | `id` PK, `dataset_id` UNIQUE | (identity shell; no samples, no partition) |
| `profiling.profiles` | `id` PK, `profile_id` UNIQUE, 8 input hashes, `identity_tuple_hash` UNIQUE, `(bam,bai,reference,fai,region_hash)` UNIQUE | → `catalog.datasets` |
| `experiments.jobs` | `id` PK, `job_key` UNIQUE, `status` CHECK, `ix_jobs_status_created_at` | → `profiling.profiles`, `catalog.gatk_configs` |
| `experiments.results` | `id` PK, `job_id` UNIQUE, `result_hash` UNIQUE | → `experiments.jobs` |
| `evaluation.evaluations` | `id` PK, `evaluation_hash` UNIQUE | → `experiments.results` |
| `models.model_bundles` | `id` PK, `bundle_key` UNIQUE, future binding hashes | → `catalog.artifacts` |
| `runtime.decisions` | `id` PK, `(round_id, decision_hash)` UNIQUE | → gatk_configs, model_bundles, profiles (nullable) |
| `audit.events` | `id` PK, `actor_role`, `action`, `payload_hash` | — |

All ids are server-generated UUIDs; all timestamps are `timestamptz` (UTC). Every
SHA-256 column is `CHAR(64)` with a `~ '^[0-9a-f]{64}$'` CHECK. Constraint and index
names are deterministic (see `storage/metadata.py` naming convention) so the gate can
inspect them by name.

## Artifact policy
Binary/large payloads are never stored in PostgreSQL — only URI, SHA-256 (unique,
lowercase-hex validated), media type, and byte size (non-negative). No `bytea` and no
large objects.

## Append-only enforcement
Six evidence tables (`profiling.profiles`, `experiments.results`,
`evaluation.evaluations`, `models.model_bundles`, `runtime.decisions`, `audit.events`)
carry a `BEFORE UPDATE OR DELETE` trigger (`audit.minos_reject_mutation`) that raises,
in addition to roles not being granted UPDATE/DELETE. `experiments.jobs` allows status
transitions but a `BEFORE UPDATE` trigger (`experiments.minos_reject_identity_change`)
rejects changes to identity columns. `DROP TABLE` during a downgrade does not fire row
triggers, so the admin migration path remains possible. See `DATABASE_ROLES.md`.

## Transaction boundaries
`session_scope` commits on success, rolls back on any exception, and always closes the
session. Repository methods `flush` but never commit — the caller owns the boundary.

## Worker-claim behavior
`experiments.jobs` supports `SELECT ... FOR UPDATE SKIP LOCKED`. The
`claim_next_job(session, worker_id)` primitive (in `repositories/append_only.py`) claims
the oldest `PENDING` job inside the caller's transaction: concurrent workers never
duplicate a claim (locked rows are skipped), and rollback releases an uncommitted claim.
This is storage concurrency only — the experiment harness (L2-F) is not implemented.

## DB-READY verification
`minos-engine layer2 db qualify` runs the real PostgreSQL 16 integration suite and
assembles the `gates/db-ready.json` gate (bound to source commit/tree, Alembic head,
migration-file hash, storage schema hash, role-policy hash, PostgreSQL major version,
and the accepted PROTOCOL/TWIN/L1 hashes). `minos-engine layer2 db qualify --check`
verifies a committed gate non-mutatingly; `minos-engine layer2 db gate require-pass`
requires PASS. See `MIGRATIONS.md` and `DATABASE_ROLES.md`.

## Known limitations (deferred)
- No 50/10/15 split manifest, no sample allocation, no dataset population (L2-C).
- Partition-based row isolation for the trainer/locked-test path is a future L2-C/L2-E
  control — L2-B does not implement it (documented in `DATABASE_ROLES.md`).
- No profile/BAM/truth ingestion, feature views, experiment execution, models, or
  controller logic (later stages).
