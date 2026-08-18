# Layer 2 — Database Roles & Privileges (L2-B)

Five least-privilege PostgreSQL roles (NOLOGIN group roles, no committed passwords).
The policy is code-owned in `storage/roles.py`; the Alembic migration emits exactly
these grants, and the integration tests prove each denial by executing it as the role
and asserting a PostgreSQL permission error (not by inspecting a Python allowlist).
No role is granted membership in another role, so nothing is inherited
(`minos_evaluator` is never inherited by `minos_live`).

## Schema USAGE matrix
A role without `USAGE` cannot reference the schema at all — this is how `minos_live`
is denied `evaluation`, and `minos_trainer` is denied `evaluation`.

| Schema | live | runner | evaluator | trainer | admin |
|---|:--:|:--:|:--:|:--:|:--:|
| catalog | ✓ | ✓ | | ✓ | ✓ |
| profiling | ✓ | ✓ | | ✓ | ✓ |
| experiments | | ✓ | ✓ | ✓ | ✓ |
| evaluation | | | ✓ | | ✓ |
| models | ✓ | | | ✓ | ✓ |
| runtime | ✓ | | | | ✓ |
| audit | ✓ | ✓ | ✓ | ✓ | ✓ |

## Table privilege matrix
| Table | live | runner | evaluator | trainer | admin |
|---|---|---|---|---|---|
| catalog.artifacts | SELECT | SELECT | — | SELECT | ALL |
| catalog.gatk_configs | SELECT | SELECT | — | SELECT | ALL |
| catalog.datasets | SELECT | SELECT | — | SELECT | ALL |
| profiling.profiles | SELECT | SELECT,INSERT | — | SELECT | ALL |
| experiments.jobs | — | SELECT,INSERT,UPDATE | — | — | ALL |
| experiments.results | — | SELECT,INSERT | SELECT | SELECT | ALL |
| evaluation.evaluations | — | — | SELECT,INSERT | — | ALL |
| models.model_bundles | SELECT | — | — | SELECT,INSERT | ALL |
| runtime.decisions | SELECT,INSERT | — | — | — | ALL |
| audit.events | INSERT | INSERT | INSERT | INSERT | ALL |

`UPDATE`/`DELETE` are granted to no application role on any append-only evidence
table; `experiments.jobs` grants `UPDATE` to `minos_runner` only (status/claim), and
identity columns are further protected by a trigger.

## Role intent
- **minos_admin** — owns/administers all seven schemas; migrations and administration
  only. `ALL` on all tables.
- **minos_live** — production runtime: reads runtime-required catalog/model/profile
  identities, appends `runtime.decisions` and `audit.events`. **No** access to
  `evaluation` (no schema USAGE), no truth/hidden-labels/TP-FP-FN/scores/offline
  evaluation evidence, no update/delete of scientific evidence, no catalog/model
  mutation.
- **minos_runner** — reads required catalog/profiling records; creates and appends
  experiment jobs/results; cannot alter frozen catalog identities; cannot obtain admin.
- **minos_evaluator** — writes isolated offline evaluation evidence; reads experiment
  result identities; cannot mutate runtime/catalog/model records; not inherited by live.
- **minos_trainer** — reads approved profiling/experiment/catalog inputs; appends model
  artifacts/metadata; **no** access to `evaluation`.

## PUBLIC hardening & default privileges
The migration `REVOKE`s all privileges on the seven schemas, all their tables, and all
their sequences from `PUBLIC`, and sets default privileges so future tables are not
granted to `PUBLIC` and are owned/administered by `minos_admin`. UUID primary keys mean
no sequences are used, so there are no sequence-privilege leaks.

## Deferred control (L2-C/L2-E)
`minos_trainer` must ultimately never read the future **locked-test** data path. Because
L2-C (the immutable 50/10/15 split manifest and sample allocation) does not exist yet,
**partition-based row isolation is not implemented in L2-B** and is documented here as a
future L2-C/L2-E control. L2-B denies `minos_trainer` access to the `evaluation` schema
(where truth-derived evidence lives) at the schema-USAGE level today.

## Ownership model & administrative login
`minos_admin` is a **NOLOGIN** group/ownership role and is **never** a superuser (it
has none of `SUPERUSER`, `CREATEDB`, `CREATEROLE`, `REPLICATION`, `BYPASSRLS`). The
migration creates the seven schemas `AUTHORIZATION minos_admin` and creates all ten
tables, both trigger functions, and every index/trigger while executing under
`SET ROLE minos_admin`, so `minos_admin` **owns** them and can `ALTER`/`DROP`/index
them. Application roles receive only least-privilege grants and are **never** granted
membership in `minos_admin`, so no application role can `SET ROLE minos_admin`.

An external administrative **login** role (not committed here, not part of the five
MINOS roles) performs migrations by becoming/`SET ROLE minos_admin`. That login must
be a member of `minos_admin` (or a superuser). Separately — and documented distinctly
from `minos_admin`'s own privileges — the bootstrap of the five roles requires the
migration login to hold `CREATEROLE` (or be superuser) **only** to run the initial
`CREATE ROLE` statements; `minos_admin` itself never holds `CREATEROLE`.

Unsafe default `PUBLIC EXECUTE` on the stage functions is revoked, and safe default
privileges are set (as `minos_admin`) so future objects it creates do not restore
`PUBLIC` access. On downgrade those default-privilege entries are cleared before the
roles are dropped.

## Role lifecycle
Roles are created idempotently (NOLOGIN, no password) during `upgrade`. `downgrade`
revokes all grants and default privileges in this database, then drops exactly the five
MINOS roles — and, because roles are cluster-global, safely **retains** a role (with a
NOTICE) if it is still referenced by another database on the same cluster, rather than
using any destructive database-wide cleanup (never `DROP OWNED BY` / `DROP DATABASE`).
