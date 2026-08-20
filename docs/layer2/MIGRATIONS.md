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

The audited operational store is on the cluster at **host `127.0.0.1`, port `5433`**
(data directory `/home/hr/bittensor/minos_l2d_db`, Alembic `0004_l2d_profile_ingestion`,
75-profile corpus, accepted epoch-1 snapshot). It is carried onto the canonical database
`minos_engine_db` **without data loss**.

> **Completed operation (record).** The owner performed a direct, in-place rename
> `ALTER DATABASE postgres RENAME TO minos_engine_db;` on the `127.0.0.1:5433` cluster.
> No dump/restore or database copy was performed and the data directory is unchanged.
> Post-rename verification confirmed `current_database() = minos_engine_db`, Alembic still
> `0004_l2d_profile_ingestion`, the three accepted epoch-1 snapshot hashes and
> `member_count = 75` unchanged, corpus counts `bam_profiles=75 / catalog.dataset_registry=75
> / profile_snapshot_members=75 / catalog.artifacts=225`, roles/ownership/grants intact,
> and the operational identity guard passing. The schema was then advanced with
> `alembic upgrade 0005_l2e_feature_view` (Phase B below).

**On renaming `postgres`.** An in-place `ALTER DATABASE postgres RENAME TO minos_engine_db`
is a valid, data-preserving PostgreSQL operation and was the method used here. Renaming
the default maintenance database is *not* prohibited by PostgreSQL — it only requires that
no session is connected to it at the time (use another maintenance database such as
`template1` for the rename), and that other tooling not rely on a database literally named
`postgres`. The dump/restore procedure documented below as an **alternative** is a
conservative option for cross-cluster moves or when an explicit copy is preferred; it is
**not** a PostgreSQL requirement for a `postgres` source.

Rules for this runbook:

- **Creating a new, empty `minos_engine_db` is NOT a migration.** The corpus, Alembic
  revision, accepted snapshot, `catalog.artifacts`, roles, grants, and evidence lineage
  must all be carried over and verified. (The in-place rename preserves all of these by
  construction — nothing is copied or recreated.)
- A database name is a database *inside* a cluster; the cluster / data-directory path
  (`/home/hr/bittensor/minos_l2d_db`) is unchanged. The engine never creates, renames, or
  drops a database — every rename/restore step here is a manual DBA action.
- **Every command is fully cluster-qualified** with `-h 127.0.0.1 -p 5433`. Never run an
  unqualified `createdb minos_engine_db`, `pg_dump postgres`, or
  `pg_restore -d minos_engine_db` — those can silently target a different default cluster
  (e.g. a local socket cluster on 5432).
- **Do not update the operational `MINOS_DATABASE_URL` until the rename (or restore) and
  the full Phase A identity/count/hash verification succeed.** Phase A and Phase B are
  separate, each independently verified — never combine "successful rename/restore" and
  "successful 0005 migration" into one unchecked step.

### Phase A — adopt `minos_engine_db` and preserve the current store

**Method used — in-place rename** (data-preserving; the data directory is unchanged):

```bash
# From a maintenance connection that is NOT the database being renamed (e.g. template1),
# with all writers stopped and no sessions on the source database:
psql -h 127.0.0.1 -p 5433 -d template1 -c 'ALTER DATABASE postgres RENAME TO minos_engine_db;'
```

Then run the Phase-A verification (step "Phase-A verification" below) and only afterwards
update `MINOS_DATABASE_URL`.

**Alternative — verified dump/restore** (for cross-cluster moves or when an explicit copy
is preferred; keeps the source untouched as a rollback source):

1. **Verified logical backup of the source `postgres` (objects + DB-level grants):**
   ```bash
   pg_dump  -h 127.0.0.1 -p 5433 -d postgres -Fc -f minos_postgres_0004.dump
   pg_restore -l minos_postgres_0004.dump >/dev/null   # dump is readable/intact
   ```
2. **Roles-only backup (cluster-wide, separate from `pg_dump`).** `pg_dump` preserves
   database objects and database-level grants, but PostgreSQL roles are cluster-wide and
   live outside any one database:
   ```bash
   pg_dumpall -h 127.0.0.1 -p 5433 --roles-only -f minos_roles.sql
   ```
3. **Record the source database owner / encoding / collation / ctype** (to reproduce
   exactly on the target):
   ```bash
   psql -h 127.0.0.1 -p 5433 -d postgres -tAF'|' -c \
     "SELECT pg_catalog.pg_get_userbyid(datdba), pg_encoding_to_char(encoding), \
             datcollate, datctype FROM pg_database WHERE datname='postgres'"
   ```
4. **Stop writers for the final cutover.** Quiesce every service holding
   `MINOS_DATABASE_URL`; confirm zero application sessions on the source:
   ```bash
   psql -h 127.0.0.1 -p 5433 -d postgres -c \
     "SELECT count(*) FROM pg_stat_activity WHERE datname='postgres' AND application_name NOT LIKE 'psql%'"
   ```
5. **Create `minos_engine_db` on the SAME `127.0.0.1:5433` cluster**, reproducing the
   recorded owner/encoding/collation/ctype (use `-T template0` so collation/ctype can be
   set explicitly). Substitute the values captured in step 3:
   ```bash
   createdb -h 127.0.0.1 -p 5433 -O <owner> -E <encoding> \
            --lc-collate='<collate>' --lc-ctype='<ctype>' -T template0 minos_engine_db
   ```
   (On a *different* cluster you would first `psql -h <newhost> -p <newport> -d postgres
   -f minos_roles.sql` to recreate the cluster-wide roles; on `127.0.0.1:5433` they
   already exist.)
6. **Restore the backup into `minos_engine_db` (source `postgres` left untouched):**
   ```bash
   pg_restore -h 127.0.0.1 -p 5433 -d minos_engine_db --exit-on-error minos_postgres_0004.dump
   ```
7. **Phase-A verification — ALL must pass before touching the DSN:**
   ```bash
   psql -h 127.0.0.1 -p 5433 -d minos_engine_db -c "SELECT current_database()"     # minos_engine_db
   psql -h 127.0.0.1 -p 5433 -d minos_engine_db -c "SELECT version_num FROM alembic_version"  # 0004_l2d_profile_ingestion
   ```
   * accepted epoch-1 `profiling.profile_snapshots` row unchanged — `snapshot_hash` =
     `cf717ebb44e76a3408e975e027b51139df28d643dd1616c5edbce3643182c4c7`,
     `split_manifest_hash` = `b23cd5716ab46033f7ea0bf123cc9b2a5f401fa37dbffddba8d4201f5ea76145`,
     `registry_snapshot_hash` = `3e60aa65aeed8969e29ebeef83024f6fa2285a13c155d7d6dc0c601d1e94f675`,
     `member_count` = `75`;
   * corpus counts preserved: `profiling.bam_profiles` = 75, `catalog.dataset_registry`
     = 75, `profiling.profile_snapshot_members` = 75, `catalog.artifacts` = 225;
   * the five MINOS roles exist cluster-wide and grants/ownership are intact
     (`\du`; `has_table_privilege('minos_trainer','catalog.artifacts','SELECT')`; objects
     owned by `minos_admin`).
8. **Only now update `MINOS_DATABASE_URL`** to the `minos_engine_db` database on
   `127.0.0.1:5433` (name only; host/port unchanged; no default DSN or hard-coded host).
   The source `postgres` database remains as the rollback source.

### Phase B — advance the L2-E schema (only after Phase A passes)

With the restored `0004` database verified and the DSN pointing at `minos_engine_db`:

```bash
alembic upgrade 0005_l2e_feature_view      # MINOS_DATABASE_URL -> minos_engine_db @127.0.0.1:5433
alembic current | grep 0005_l2e_feature_view
```

Phase-B verification — ALL must pass:

* `alembic current` is exactly `0005_l2e_feature_view`;
* the three L2-E tables exist: `profiling.feature_sets`, `profiling.feature_matrices`,
  `profiling.feature_matrix_members`;
* the accepted L2-D snapshot hashes, members, `profiling.bam_profiles` and
  `catalog.artifacts` counts are **unchanged** from Phase A (re-run the step-7 checks);
* migration 0005 privilege delta holds: `minos_trainer` no longer has
  `catalog.artifacts` `SELECT`, and `minos_evaluator` still has none;
* the fail-closed operational identity guard passes (a connection to `minos_engine_db`
  satisfies `verify_operational_database_identity`);
* no E4 output exists yet: `profiling.feature_matrices` and
  `profiling.feature_matrix_members` are empty and no matrix-kind `catalog.artifacts`
  rows are present.

Only after this runbook completes **and** the source-contract commit is accepted does
the real operational identity satisfy the guard. E4 stays unauthorized until both hold.

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
