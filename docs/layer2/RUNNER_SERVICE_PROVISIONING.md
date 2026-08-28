# Runner service principal — provisioning runbook

**Nothing in this document is executed by the repository, and no credential belongs in Git.**
It specifies how the *external* login identity for the L2-F2 baseline GATK runner is created.
Nothing here has been provisioned yet: this is the contract the next environment task follows.

## The distinction that matters

| | |
|---|---|
| `minos_runner` | **built-in group role, intentionally `NOLOGIN`** — carries authority, never credentials |
| `minos_runner_svc` | **external service principal, `LOGIN`** — carries the credential, inherits only `minos_runner` |

`minos_live`, `minos_runner`, `minos_evaluator`, `minos_trainer` and `minos_admin` are all
deliberately `NOLOGIN` group roles. That is the architecture and it stays.

> **Never** `ALTER ROLE minos_runner LOGIN`, and never give it a password.

Migration `0011` grants the runner boundary to the **group role**. The service principal gets
that authority purely by membership.

## Required properties

* `LOGIN`
* **NO** `SUPERUSER`, `CREATEDB`, `CREATEROLE`, `BYPASSRLS`
* `GRANT minos_runner TO minos_runner_svc` — and **no other** role membership
* specifically **not** `minos_admin`, `minos_evaluator`, `minos_trainer` or `minos_live`

The runner boundary verifies all of this on **every connection it opens**, reading
`session_user` rather than `current_user` so an already-issued `SET ROLE` cannot disguise which
principal actually logged in.

## Provisioning (run by the operator, outside Git)

```sql
-- password supplied interactively or from a secret store; never committed, never logged
CREATE ROLE minos_runner_svc LOGIN PASSWORD :'runner_password'
    NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS INHERIT;

GRANT CONNECT ON DATABASE minos_l2f2_baseline TO minos_runner_svc;
GRANT minos_runner TO minos_runner_svc;
```

Verify afterwards:

```sql
SELECT rolsuper, rolcreatedb, rolcreaterole, rolbypassrls, rolcanlogin
  FROM pg_roles WHERE rolname = 'minos_runner_svc';
-- expect: f, f, f, f, t

SELECT r.rolname FROM pg_auth_members m
  JOIN pg_roles r ON r.oid = m.roleid
  JOIN pg_roles g ON g.oid = m.member
 WHERE g.rolname = 'minos_runner_svc';
-- expect exactly one row: minos_runner

SELECT rolcanlogin FROM pg_roles WHERE rolname = 'minos_runner';
-- expect: f  (the group role stays NOLOGIN)
```

## Database connection isolation

PostgreSQL role memberships are **cluster-global**, so `CONNECT` must be an explicit allowlist.
`PUBLIC` `CONNECT` is already revoked on both databases; relying on it is forbidden, and a
per-role revoke is ineffective while `PUBLIC` holds the privilege.

The runner service principal may connect to **`minos_l2f2_baseline`** and must **not** be able to
connect to `minos_engine_db`. The next environment task issues exactly one grant:

```sql
GRANT CONNECT ON DATABASE minos_l2f2_baseline TO minos_runner_svc;
-- and deliberately NOTHING for minos_engine_db
```

Verify with the real credential, changing **only** the target database: the baseline store must
connect, and the operational store must fail with
`FATAL: permission denied for database "minos_engine_db"` — at `CONNECT` authorization, not
authentication.

## What the principal can and cannot do

Granted by `0011` through `minos_runner`, on top of the existing `0007`/`0008` grants:

* `EXECUTE` on `experiments.l2f2_resolve_claimed_execution` — the truth-free scientific identity
  of a job this worker already owns. Truth digests, mutation digests and evaluation rows are
  structurally absent from its result type, and a non-TRAIN member cannot be resolved at all
* `EXECUTE` on `experiments.l2f2_resolve_phase_b_runner_bootstrap` — which Phase-B plan this
  worker may claim within, and the execution environment the completed Phase-A campaign ran under.
  Two strings, no arguments, no science: the Phase-B design itself is derived by the control plane
  long beforehand and is already durable as a persisted plan and a recorded authority
* `EXECUTE` on `experiments.l2f2_resolve_claimed_phase_b_execution` — the same truth-free
  resolution for Phase B, with its own fixed `phase = 'PHASE_B'` predicate. Which function is
  called is decided by the authority being executed, never by an argument, and no resolver
  falls back to another phase's authorities
* `EXECUTE` on `experiments.l2f2_resolve_phase_c_runner_bootstrap` and
  `experiments.l2f2_resolve_claimed_phase_c_execution` — the identical pair for Phase C, added by
  `0020` with a fixed `phase = 'PHASE_C'` predicate. Three phases now have three resolvers and two
  bootstraps, and each is fixed internally to its own phase
* `EXECUTE` on `experiments.l2f2_register_execution_artifact` — the ONLY path into
  `catalog.artifacts`. It accepts `vcf` or `result_manifest` and fixes media type and provenance
  itself, so it cannot register any other kind of artifact
* `SELECT` on `public.alembic_version` — so the boundary can refuse a wrong-revision database

### Required baseline schema revision

The runner authority **functions and grants originate in `0011`**; `0016` adds one more (the
Phase-B resolver), `0019` a second (the Phase-B bootstrap), and `0020` two more (the Phase-C pair).
The revision the boundary requires on every connection is
**`0020_l2f2_phase_c_execution`**, exactly — never a floor, never `head`.

The reason it tracks a later revision than its own functions is that the runner and the evaluator
**share one database**. Neither later migration grants the runner anything:

* `0012` adds `experiments.l2f_experiment_plan_members.source_matrix_member_index`, so a plan
  member's **plan-local** ordinal and the **source feature-matrix** ordinal it references are
  stored separately. A database still at `0011` carries one column for both and cannot represent
  the Phase-A plan at all (local `0..4` over source `0/10/20/30/40`).
* `0013` makes the four AdvancedScorer component columns nullable, because the pinned upstream
  scorer exposes only the combined score. It touches no table the runner reads.
* `0014` adds `experiments.l2f_execution_failures.runtime_ms` and widens the failure writer
  `experiments.minos_l2f_fail_job` by one argument, so a failed execution records how long the
  attempt actually took. The runner **executes** that function, as it always did, and still holds
  no DML on the failure ledger.
* `0015` adds `execution_environment_hash` to BOTH outcome ledgers and widens both writers by one
  argument, so every durable outcome records which runtime produced it. It grants nothing.
* `0016` admits `PHASE_B` to `experiments.l2f2_execution_authorities`, makes `canary_job_key`
  nullable under a phase-semantic rule (Phase A must carry a canary, Phase B must not), and adds
  `experiments.l2f2_resolve_claimed_phase_b_execution`. The runner gains `EXECUTE` on that one
  function and nothing else — no table privilege anywhere, and the Phase-A resolver is untouched.
* `0019` adds `experiments.l2f2_resolve_phase_b_runner_bootstrap()` — the ONLY way this service
  learns which Phase-B plan it may consume and which runtime that plan's science was chosen under.
  It takes no arguments, reads nothing in the `evaluation` schema, and grants the runner one more
  `EXECUTE` and no table access. Without it a Phase-B worker cannot start at all, which is why the
  required revision moves.
* `0018` re-owns the four `0009`/`0010` evaluator definers and their two outcome ledgers to
  `minos_admin`. It grants the runner nothing and touches nothing the runner reads; the revision
  moves because the runner and the evaluator share one store.
* `0017` re-owns the two `0011` `SECURITY DEFINER` functions to `minos_admin`. A definer executes
  with its owner's authority, and `0011` created them as the migration principal — a SUPERUSER —
  so the runner's two privileged calls were running far wider than the control plane itself. This
  grants nothing and revokes nothing: every MINOS role's effective `EXECUTE` is unchanged, the
  function bodies and OIDs are untouched, and the definer simply stops being a superuser.

* `0020` admits `PHASE_C` to `experiments.l2f2_execution_authorities`, extends the canary rule to
  forbid a Phase-C canary, and adds `experiments.l2f2_resolve_claimed_phase_c_execution` and
  `experiments.l2f2_resolve_phase_c_runner_bootstrap()`. The runner gains `EXECUTE` on those two
  functions and nothing else; the Phase-A and Phase-B resolvers are untouched, and the Phase-C
  bootstrap reads nothing in the `evaluation` schema, exactly as `0019`'s does.

Several of these are migrations the runner's own code depends on: `0014` and `0015` drop the
narrower writer signatures, so a runner at `0015` cannot record an outcome against an older
database; without `0016` a claimed Phase-B job cannot be resolved at all; and without `0020` the
same is true of a Phase-C job. That is precisely why the revision check is exact.

The service principal's authority grows by exactly one `EXECUTE` at `0016`, one at `0019` and two
at `0020`, and is otherwise identical under all nine revisions. What changes at `0017` is not the
runner's authority but the **definer's**: every `SECURITY DEFINER` function this service can reach
is owned by `minos_admin`, and none is owned by a superuser. That is asserted through
`pg_roles.rolsuper` rather than by owner name.
* the existing `0007`/`0008` claim, start, release, complete-success and fail functions

Deliberately **not** granted:

* any direct privilege on `experiments.l2f2_execution_authorities` — the execution authority is
  created and read by the control plane only, and is append-only even for it
* any direct `SELECT`/`INSERT`/`UPDATE`/`DELETE` on the L2-F plan, job, result or failure tables
* generic `INSERT` on `catalog.artifacts`
* anything in the `evaluation` schema — the runner is truth-free by construction

## The JVM the launcher actually starts

`JAVA_HOME/bin/java` is the **pinned JVM identity**: it is named explicitly, never discovered, and
its bytes are what every runtime hash is taken over. That pin does not, by itself, decide which
JVM runs, because Broad's launcher builds its own command as `["java", ...]` — a bare token — and
hands it to `check_call`. The Java process that runs HaplotypeCaller is therefore whatever the
**child `PATH`** resolves.

The runner now closes that gap. Against the exact environment dictionary it is about to pass as
`env=`, it predicts what the launcher's bare `java` would resolve to and refuses unless that is
the pinned binary by **canonical path and by content** — a same-version impostor is still a
different JVM. The proof runs twice: at `preflight()`, before any launcher process exists, and
again immediately before every scientific launch, because a `PATH` can be re-provisioned between
service startup and job N.

Nothing is discovered by this: the binary is already known, and `PATH` is consulted only to ask
what upstream would pick. The child `PATH` is not rewritten — whatever allowlisted `PATH` is
provisioned must simply resolve `java` to the pinned JVM, which the accepted deployment does
(`JAVA_HOME/bin` is on it).

This is the same shape as the exit-127 interpreter defect, one level down: there, `env` could not
find `python`; here, the launcher could find *a* `java` that was not the provisioned one. The
execution-environment identity is unchanged — policy `l2f-gatk-child-env-v2` already claimed the
JVM was pinned, and this makes the implementation enforce what it claimed.

## GATK runtime provisioning (mandatory)

The GATK 4.5.0.0 launcher is a `#!/usr/bin/env python` script. Under the original policy a worker
started GATK only if its ambient `PATH` happened to contain a command named `python` — and when
one did not, `env` exited **127** before a single argument was parsed and five Phase-A jobs were
recorded as candidate failures for configurations GATK never saw. Production therefore no longer
relies on the shebang, and every executable it starts is named explicitly and pinned by content:

| Variable | Meaning |
|---|---|
| `MINOS_L2F_GATK_EXECUTABLE` | absolute path to the pinned GATK launcher (unchanged) |
| `MINOS_L2F_GATK_EXECUTABLE_SHA256` | its content digest (unchanged) |
| `MINOS_L2F_GATK_VERSION` | the pinned version, `4.5.0.0` (unchanged) |
| **`MINOS_L2F_GATK_PYTHON`** | absolute path to the interpreter that EXECUTES the launcher. Must be a regular, non-symlinked, executable file — a symlink can be re-pointed between the check and the run |
| **`MINOS_L2F_GATK_PYTHON_SHA256`** | that interpreter's content digest, re-verified before every process |
| **`JAVA_HOME`** | absolute path to the provisioned JDK. `JAVA_HOME/bin/java` must exist and be executable; the JVM is never located through `PATH` |

`SubprocessGatkRunner.from_env()` refuses to build unless all of them are provisioned. The child
process is started as `[MINOS_L2F_GATK_PYTHON, MINOS_L2F_GATK_EXECUTABLE, *argv]` with
`shell=False`, so the launcher's own shebang is inert and a `PATH` with no `python` is harmless.

**Runtime preflight happens before a job is claimed.** `execute_next_l2f2_phase_a_job` verifies the
launcher, the local JAR bundle, the interpreter and the JVM by content, then runs the real
`gatk --version` through that interpreter and requires `4.5.0.0` — all before any database call. A
worker that cannot pass leaves the queue untouched: a broken runtime must never consume a candidate
observation. The same identity is re-verified immediately before and immediately after
HaplotypeCaller, and a runtime that moved is recorded as `EXECUTION_ERROR`, never as a candidate
failure.

## Credential handling

The connection secret lives in a `0600` environment file beneath the canonical baseline
workspace:

```
/home/hr/bittensor/minos_l2f2_baseline/l2f2-runner.env
```

It may carry `MINOS_DATABASE_URL` plus the already-accepted GATK, dataset, work-root and
artifact-root variables. Consistent with the canonical-workspace rule it is never created
directly under `/home/hr`, and it is never committed. Rotate with
`ALTER ROLE minos_runner_svc PASSWORD …` and update that file; no repository change is required,
because the repository never holds the credential.

## CI

CI proves the *authority shape* without a production credential: it creates an ephemeral
`minos_runner_ci_svc LOGIN`, grants it `minos_runner` and nothing else, and runs a complete
execution — claim, resolve, start, publish, register, complete — entirely under that principal,
before asserting the denials (no table mutation anywhere, no ledger or authority read, no
`SET ROLE`, and none of `SUPERUSER`/`CREATEDB`/`CREATEROLE`/`BYPASSRLS`). Each denial statement is
separately proven well formed under a role that *is* allowed to run it, so a denial can never
pass by dying in the parser. See `tests/integration/layer2_db/test_l2f2_runner_boundary.py`.
