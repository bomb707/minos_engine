# Layer 2 — Migrations & Rollback (L2-B)

A single deterministic Alembic migration establishes the L2-B storage foundation.
Configuration is credential-free: `alembic.ini` sets no `sqlalchemy.url`; the URL comes
only from `MINOS_DATABASE_URL` via `migrations/env.py` (synchronous psycopg 3 engine).

## Canonical operational database
The persistent MINOS Engine operational store is the single PostgreSQL **database**
named **`minos_engine_db`** (owner decision; the code-owned constant
`CANONICAL_OPERATIONAL_DATABASE_NAME` in `src/minos_engine/storage/constants.py`). All
stages (L2-B → L2-E, and E4 onward) share this one database; each stage's migration
*adds* schema to it. There is no per-stage database.

Precise scope of the name:

- `minos_engine_db` is a **database inside a PostgreSQL cluster** — created once with
  `CREATE DATABASE minos_engine_db`. It is **not** a cluster / data-directory path
  (e.g. `/var/lib/postgresql/16/main`), a host, a port, or a role. One cluster can host
  many databases; the operational store is exactly the one named `minos_engine_db`.
- **Alembic manages the schemas and tables *inside* this database; it never creates,
  renames, or drops the PostgreSQL database itself.** `alembic upgrade head` /
  `downgrade base` only add or remove stage-owned schemas/objects; bringing the database
  into existence (and naming it) is a one-time DBA/deployment step, not a migration.
- Connection details still come **only** from `MINOS_DATABASE_URL` — no hard-coded host,
  port, username, password, or default DSN, no automatic database creation, and no
  SQLite fallback. The URL supplies *where/how* to connect; `minos_engine_db` is *which*
  database must be served once connected.

A typed, fail-closed **operational identity guard**
(`verify_operational_database_identity` in `src/minos_engine/storage/database.py`)
executes `SELECT current_database()` on the live connection and refuses to proceed
unless it equals `minos_engine_db`. Because the decision is read from the connected
server — never parsed from the DSN string — a URL that merely *mentions* the canonical
name but resolves elsewhere is rejected. The guard is applied only at production /
accepted operational mutation boundaries (including the accepted epoch-1 feature-matrix
builder used by E4); generic helpers and synthetic / scratch integration databases
(`minos_l2b_main`, `minos_l2d_main`, `minos_l2e_features`, …) are deliberately left
name-independent so tests keep working.

## Head revision
```
0001_l2b_initial   (down_revision = None)
```

## Immutable revision (frozen snapshot)
Revision `0001_l2b_initial` is a **frozen, self-contained snapshot**. It imports no
ORM declarative base or model metadata and never bulk-emits or reflects the ORM schema
— every schema, role, table, column, constraint, index, function, trigger, and grant
is written with explicit Alembic operations. Consequently, replaying revision 0001
always produces the exact L2-B inventory regardless of any later ORM changes (L2-C+);
adding or removing a runtime ORM table does not change what revision 0001 creates or
drops. The frozen inventory (7 schemas, 10 tables, 10 PK / 9 FK / 12 unique / 29 check
constraints, 23 indexes, 2 functions, 7 triggers, 5 roles) and a deterministic
migration-contract hash over the committed migration bytes + inventory are defined in
`src/minos_engine/storage/migration_contract.py` and bound into the DB-READY gate.
Ownership is established via `AUTHORIZATION minos_admin` + `SET ROLE minos_admin`
(see `DATABASE_ROLES.md`).

## Upgrade order
1. Create the five NOLOGIN roles (idempotent, no passwords).
2. Create the seven application schemas.
3. Create the immutability trigger functions (`audit.minos_reject_mutation`,
   `experiments.minos_reject_identity_change`).
4. Create all tables, constraints, and indexes in dependency order (SQLAlchemy
   metadata; deterministic constraint/index names).
5. Attach append-only / identity-immutability triggers.
6. `REVOKE` unsafe `PUBLIC` privileges on schemas, tables, and sequences.
7. `GRANT` least-privilege access (see `DATABASE_ROLES.md`).
8. Set safe default privileges for objects created later.

## Downgrade order (reverse, stage-owned only)
1. Drop the triggers.
2. Reset default privileges.
3. Revoke all grants from the five roles (while schemas still exist).
4. Drop all tables (reverse dependency order).
5. Drop the trigger functions.
6. Drop the seven schemas (`RESTRICT` — never a broad `CASCADE`).
7. Drop exactly the five MINOS roles, safely: a role still referenced by another
   database on the cluster is retained with a NOTICE (fail-safe), never removed by a
   destructive database-wide operation. Nothing outside stage-owned objects and the
   five MINOS roles is ever touched. There is no `DROP DATABASE` / `DROP OWNED BY`.

## Local PostgreSQL test setup
Set `MINOS_DATABASE_URL` to a PostgreSQL 16 instance. For the canonical operational
store, create the database once and point the URL at it (the database name is
`minos_engine_db`; the `host=…` value is a cluster socket directory, not the database):

```bash
createdb minos_engine_db   # one-time; Alembic does NOT create the database
export MINOS_DATABASE_URL='postgresql://postgres@/minos_engine_db?host=/var/run/postgresql'
alembic upgrade head       # creates the stage-owned schemas/tables INSIDE minos_engine_db
minos-engine layer2 db qualify
```

If `MINOS_DATABASE_URL` is unset, the integration tests use a bundled, root-free
ephemeral PostgreSQL 16 server via the `pgserver` dev dependency, and skip only when
neither is available. SQLite is never used. Generic integration suites create their own
throwaway scratch databases (e.g. `minos_l2b_main`, `minos_l2e_features`) off the base
URL; these are name-independent and are unaffected by the canonical-name guard.

## CI PostgreSQL setup
GitHub Actions runs a `postgres:16` service container whose database is the canonical
operational name `minos_engine_db` (so CI exercises the operational-identity guard
against the real name); the workflow waits for readiness, sets `MINOS_DATABASE_URL` to
it, runs `alembic upgrade head`, the
PostgreSQL integration tests, `alembic downgrade base`, verifies stage-owned schemas are
removed, re-runs `alembic upgrade head`, and confirms the head revision — alongside all
existing Python 3.12 checks and gates. In CI the integration tests must never all skip
(enforced by `tests/integration/layer2_db/test_ci_guard.py`). Credentials are confined
to CI test configuration and never appear in committed files, logs, or reports.

## DBA runbook — adopt the canonical operational database name

If an existing operational store is on a database that is **not** `minos_engine_db`,
the operator (DBA) performs a one-time, data-preserving migration. **Creating a new,
empty `minos_engine_db` is NOT a migration** — it would abandon the corpus, snapshot,
migrations, roles, grants, and evidence lineage. The existing database, its Alembic
revision, the accepted `profile_snapshots` row, the profile corpus, `catalog.artifacts`,
roles, and grants must all be carried over intact and verified afterward.

The database name is a database *inside* a cluster; the cluster/data-directory path is
unchanged by this procedure. Run every step manually as a DBA — nothing here is
automated by the engine, and the engine never renames or drops a database.

1. **Backup (mandatory first).** Take a verified logical backup of the source database
   (roles/grants included), e.g. `pg_dump -Fc <source_db> -f minos_pre_rename.dump` plus
   `pg_dumpall --roles-only -f minos_roles.sql`. Confirm the dump restores in a scratch
   cluster before proceeding.
2. **Stop writers / drain connections.** Quiesce all services that hold
   `MINOS_DATABASE_URL`; confirm zero application sessions
   (`SELECT * FROM pg_stat_activity WHERE datname = '<source_db>'`).
3. **Use a maintenance connection.** Connect to a *different* maintenance database
   (typically `postgres`) so the source database has no open sessions — you cannot
   rename or drop the database you are connected through.
4. **Adopt the name — choose ONE:**
   * **(a) In-place rename** (preferred when the source has a dedicated operational
     name): `ALTER DATABASE <source_db> RENAME TO minos_engine_db;`. Fast, preserves
     everything, no data copy.
     *If the source database is literally `postgres`, do NOT rename it* — `postgres` is
     the cluster's default maintenance database; keep it and use path (b) instead.
   * **(b) Verified dump/restore** (required when the source is `postgres`, or across
     clusters): `createdb minos_engine_db` on the target, restore the roles then the
     dump into it (`pg_restore -d minos_engine_db minos_pre_rename.dump`), leaving the
     source untouched until verification passes.
5. **Update the DSN.** Point `MINOS_DATABASE_URL` at `.../minos_engine_db` (name only;
   host/port/socket unchanged). No default DSN or hard-coded host is introduced.
6. **Post-migration verification (must all pass before re-enabling writers):**
   * `SELECT current_database()` → `minos_engine_db`;
   * `SELECT version_num FROM alembic_version` equals the pre-migration revision
     (e.g. `0004_l2d_profile_ingestion`) — no accidental upgrade/downgrade;
   * the accepted epoch-1 `profile_snapshots` row is present with the pinned identity
     (`snapshot_hash`, `split_manifest_hash`, `registry_snapshot_hash`, `member_count`);
   * corpus counts preserved (`profiling.bam_profiles`, `profiling.dataset_registry`,
     `profiling.profile_snapshot_members`, `catalog.artifacts`);
   * the five MINOS roles and their grants exist (`\du`, `has_table_privilege(...)`),
     ownership by `minos_admin` intact;
   * the fail-closed identity guard now passes at the accepted write boundary.
7. **Retain the source until verified.** For path (b), keep the source database
   read-only until every check above passes; only then decommission it per policy
   (never a broad `DROP` before verification).

Only after this runbook completes and the source-contract commit is accepted does the
real operational identity satisfy the guard. E4 stays unauthorized until both hold.

## Credential handling
No credentials are committed. `MINOS_DATABASE_URL` is the only source of connection
details; error messages never echo the URL or any password
(`normalize_database_url` fails closed with a generic message).

## DB-READY verification
```bash
minos-engine layer2 db qualify              # run + write gates/db-ready.json + report
minos-engine layer2 db qualify --check --gate gates/db-ready.json --base-dir .
minos-engine layer2 db gate require-pass --gate gates/db-ready.json --base-dir .
```
