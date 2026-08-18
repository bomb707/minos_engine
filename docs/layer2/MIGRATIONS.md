# Layer 2 — Migrations & Rollback (L2-B)

A single deterministic Alembic migration establishes the L2-B storage foundation.
Configuration is credential-free: `alembic.ini` sets no `sqlalchemy.url`; the URL comes
only from `MINOS_DATABASE_URL` via `migrations/env.py` (synchronous psycopg 3 engine).

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
Set `MINOS_DATABASE_URL` to a PostgreSQL 16 instance, e.g.:

```bash
export MINOS_DATABASE_URL='postgresql://postgres@/postgres?host=/var/run/postgresql'
minos-engine layer2 db qualify
```

If `MINOS_DATABASE_URL` is unset, the integration tests use a bundled, root-free
ephemeral PostgreSQL 16 server via the `pgserver` dev dependency, and skip only when
neither is available. SQLite is never used.

## CI PostgreSQL setup
GitHub Actions runs a `postgres:16` service container; the workflow waits for readiness,
sets `MINOS_DATABASE_URL` to the ephemeral CI database, runs `alembic upgrade head`, the
PostgreSQL integration tests, `alembic downgrade base`, verifies stage-owned schemas are
removed, re-runs `alembic upgrade head`, and confirms the head revision — alongside all
existing Python 3.12 checks and gates. In CI the integration tests must never all skip
(enforced by `tests/integration/layer2_db/test_ci_guard.py`). Credentials are confined
to CI test configuration and never appear in committed files, logs, or reports.

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
