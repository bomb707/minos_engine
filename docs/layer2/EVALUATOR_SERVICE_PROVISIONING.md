# Evaluator service principal — provisioning runbook

**Nothing in this document is executed by the repository, and no credential belongs in Git.**
It specifies how the *external* login identity for the L2-F2 offline evaluator is created.

## The distinction that matters

| | |
|---|---|
| `minos_evaluator` | **built-in group role, intentionally `NOLOGIN`** — carries authority, never credentials |
| `minos_evaluator_svc` | **external service principal, `LOGIN`** — carries the credential, inherits only `minos_evaluator` |

`minos_live`, `minos_runner`, `minos_evaluator`, `minos_trainer` and `minos_admin` are all
deliberately `NOLOGIN` group roles. That is the architecture and it stays.

> **Never** `ALTER ROLE minos_evaluator LOGIN`, and never give it a password. Doing so would
> merge the authority model with the credential model, which is exactly the separation the
> role design exists to maintain.

Migrations `0009` and `0010` grant evaluation authority to the **group role**. The service
principal gets that authority purely by membership.

## The two evaluated stores — TRAIN and Phase-D VALIDATION

The evaluator now has **two** legitimate destinations, and they are different databases holding
different partitions. They are listed separately because the whole point of the partition
boundary is lost if an operator provisions one and assumes the other came with it.

| store | revision | partition | what the evaluator scores there |
|---|---|---|---|
| `minos_l2f2_baseline` | `0020_l2f2_phase_c_execution` | `train` | Phase-A/B/C TRAIN executions |
| `minos_l2f2_validation` | `0025_l2f2_phase_d_eval_auth` | `validation` | the forty frozen Phase-D executions |

`minos_engine_db` is **not** an evaluated store. It is the operational validation-lineage
authority and the evaluator connects to it for nothing.

`0025` adds the one authority the Phase-D evaluator was missing:
`evaluation.l2f_phase_d_execution_authority`, a two-column view — `execution_result_id` and the
persisted `plan_hash` — restricted to `partition = 'validation'` and readable by
`minos_evaluator` alone. It exists because *partition alone is not campaign identity*: a second
validation plan over the same ten frozen members and the same four frozen configurations passes
every partition, member, config and parameter-space check and is still a different campaign. The
service compares that `plan_hash` against the hash **derived** from the frozen finalist artifact
before any truth path is constructed.

`0025` also grants `SELECT ON public.alembic_version` to `minos_evaluator`, exactly as `0011`
already grants it to `minos_runner` and for the same reason: a boundary that pins the schema
revision has to be able to read it, and `alembic_version` carries no scientific data.

> **The Phase-D evaluator must never be pointed at `minos_l2f2_baseline`.** That store holds no
> validation lineage at all, so the failure would not be a clean refusal — there is simply
> nothing there to refuse against.

## Database connection isolation — the rule that was missing

PostgreSQL role memberships are **cluster-global**. `minos_evaluator_svc` inherits
`minos_evaluator` in *every* database of the cluster, including `minos_engine_db`, where that
group role legitimately holds historical object grants over evaluation and closed-partition
projections.

Membership is therefore only safe when database `CONNECT` is an **explicit allowlist**.

> **Never rely on the default `PUBLIC` `CONNECT` grant.** PostgreSQL privileges are additive, so
> `REVOKE CONNECT ON DATABASE minos_engine_db FROM minos_evaluator_svc` is **ineffective** while
> `PUBLIC` still holds `CONNECT` — the role keeps receiving it through `PUBLIC`.

Required end state:

```sql
-- operational store: PUBLIC confers nothing; only real operational logins may connect
REVOKE CONNECT ON DATABASE minos_engine_db FROM PUBLIC;
GRANT  CONNECT ON DATABASE minos_engine_db TO <each verified operational LOGIN principal>;
-- and deliberately NOT to minos_evaluator_svc

-- dedicated evaluation store: the service principal, plus the migration login
REVOKE CONNECT ON DATABASE minos_l2f2_baseline FROM PUBLIC;
GRANT  CONNECT ON DATABASE minos_l2f2_baseline TO minos_evaluator_svc;
GRANT  CONNECT ON DATABASE minos_l2f2_baseline TO <migration/admin LOGIN principal>;

-- the Phase-D validation store. It is created with PUBLIC CONNECT and this is NOT optional:
-- until PUBLIC is revoked, every LOGIN role in the cluster can reach the validation partition.
REVOKE CONNECT ON DATABASE minos_l2f2_validation FROM PUBLIC;
GRANT  CONNECT ON DATABASE minos_l2f2_validation TO minos_evaluator_svc;
GRANT  CONNECT ON DATABASE minos_l2f2_validation TO minos_runner_svc;
GRANT  CONNECT ON DATABASE minos_l2f2_validation TO <migration/admin LOGIN principal>;
```

Before revoking `PUBLIC` `CONNECT`, enumerate every LOGIN principal that legitimately needs the
database (`SELECT rolname FROM pg_roles WHERE rolcanlogin`, plus `pg_stat_activity`) and grant it
explicitly. `NOLOGIN` group roles need no `CONNECT` grant.

Verify the isolation with the real credential, changing **only** the target database:

```sql
-- must succeed against the evaluation store, and fail against the operational store with
-- FATAL: permission denied for database "minos_engine_db"
```

A failure caused by a wrong password, an unknown role or a network error does **not** prove
isolation — the refusal must occur at database `CONNECT` authorization.

This is a connection-layer control. It does **not** change the group-role architecture, and no
object-level privilege of `minos_evaluator` is altered.

## Required properties

The service principal must be created with:

* `LOGIN`
* **NO** `SUPERUSER`
* **NO** `CREATEDB`
* **NO** `CREATEROLE`
* **NO** `BYPASSRLS`
* `GRANT minos_evaluator TO minos_evaluator_svc` — and **no other** role membership
* specifically **not** `minos_admin`, `minos_runner`, `minos_trainer` or `minos_live`

## Provisioning (run by the operator, outside Git)

```sql
-- password supplied interactively or from a secret store; never committed, never logged
CREATE ROLE minos_evaluator_svc LOGIN PASSWORD :'evaluator_password'
    NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS INHERIT;

GRANT CONNECT ON DATABASE minos_l2f2_baseline   TO minos_evaluator_svc;  -- TRAIN
GRANT CONNECT ON DATABASE minos_l2f2_validation TO minos_evaluator_svc;  -- Phase-D VALIDATION
GRANT minos_evaluator TO minos_evaluator_svc;
```

Verify afterwards:

```sql
SELECT rolsuper, rolcreatedb, rolcreaterole, rolbypassrls, rolcanlogin
  FROM pg_roles WHERE rolname = 'minos_evaluator_svc';
-- expect: f, f, f, f, t

SELECT r.rolname FROM pg_auth_members m
  JOIN pg_roles r ON r.oid = m.roleid
  JOIN pg_roles g ON g.oid = m.member
 WHERE g.rolname = 'minos_evaluator_svc';
-- expect exactly one row: minos_evaluator

SELECT rolcanlogin FROM pg_roles WHERE rolname = 'minos_evaluator';
-- expect: f  (the group role stays NOLOGIN)
```

The `CONNECT` allowlist is a claim about who is *excluded*, so verify it by enumerating rather
than by spot-checking the roles you expect to pass:

```sql
SELECT r.rolname
  FROM pg_roles r
 WHERE r.rolcanlogin
   AND has_database_privilege(r.rolname, 'minos_l2f2_validation', 'CONNECT')
 ORDER BY 1;
-- expect exactly: minos_evaluator_svc, minos_runner_svc, <migration/admin LOGIN principal>

SELECT has_database_privilege('public', 'minos_l2f2_validation', 'CONNECT');
-- expect: f
```

### Phase-D evaluator runtime roots

The service **validates** these and never creates them; a missing root is a refusal, not a
`mkdir`. Provision them under the canonical MINOS root before the first Phase-D evaluation:

| variable | path | mode |
|---|---|---|
| `MINOS_L2F2_FINALIST_FREEZE_PATH` | the frozen finalist artifact | `0640` |
| `MINOS_L2F2_EVALUATION_PRACTICE_ROOT` | validation truth bundles | `0750` |
| `MINOS_L2F2_EVALUATION_REFERENCE_ROOT` | `<chrom>/<chrom>.fa` beside `<chrom>.sdf` | `0750` |
| `MINOS_L2F2_EVALUATION_WORK_ROOT` | `…/minos_l2f2_validation/evaluation_work` | `0750` |
| `MINOS_L2F2_EVALUATION_ARTIFACT_ROOT` | `…/minos_l2f2_validation/evaluation_artifacts` | `02750` |

## What the principal can and cannot do

Granted by `0009` through `minos_evaluator`:

* `USAGE` on schema `evaluation`
* `SELECT` on `evaluation.l2f_completed_execution_inputs` — the narrow completed-execution
  projection, **not** the L2-F experiment tables
* `SELECT` on `evaluation.l2f_train_truth_registration_targets` — **TRAIN only**; validation and
  test are structurally absent from that view
* `SELECT` on `evaluation.dataset_evaluation_identity`, `l2f_evaluation_results`,
  `l2f_evaluation_failures`
* `EXECUTE` on the three `0009` `SECURITY DEFINER` persistence functions
* `EXECUTE` on `evaluation.l2f_register_metrics_artifact` (`0010`) — the ONLY path by which the
  service registers the metrics document it publishes. It accepts a digest, URI and size; media
  type and provenance are fixed inside the function, so it cannot register any other kind of
  artifact

Deliberately **not** granted:

* any direct `INSERT`/`UPDATE`/`DELETE` on `evaluation.l2f_evaluation_results` or
  `l2f_evaluation_failures` — the outcome ledgers are append-only and reachable only through the
  `0009` writers
* any direct `INSERT`/`UPDATE`/`DELETE` on `catalog.artifacts` — registration goes through the
  `0010` registrar or not at all
* any write on `experiments.*` — the execution ledger stays owned by the runner path
* broad `SELECT` on the L2-F experiment tables — `0008` gives application roles no direct table
  privileges there, and `0009` preserves that boundary
* anything reaching validation or test truth

## Who those `SECURITY DEFINER` functions execute as

A `SECURITY DEFINER` function runs with its **owner's** authority, so the owner is the real
privilege boundary — not the grant. `0009` and `0010` created their functions and their two
outcome ledgers without `SET ROLE minos_admin`, so all six inherited the migration principal, in
practice a SUPERUSER. Every evaluation therefore made four privileged calls that ran far wider
than the control plane itself.

`0018_l2f2_eval_owner_fix` corrects that: the four functions and the two ledgers are re-owned to
`minos_admin` — `NOLOGIN`, `NOSUPERUSER` — by `ALTER ... OWNER TO`, never by recreating anything.
Function OIDs, bodies, signatures, `SECURITY DEFINER` and `search_path` are unchanged, the tables'
columns, constraints, indexes, triggers and rows are unchanged, and **no application role gains or
loses a single privilege**. The writers get their `INSERT` authority the same way `0008`'s
execution writers always have: by the control plane owning the ledger they append to.

This service's own authority is exactly what it was. What changed is what stands behind the four
functions it calls.

## The pinned scoring checkout — `MINOS_L2F_MINOS_SUBNET_ROOT`

Production scoring does not use a local reimplementation of the Minos score. It **executes the
exact pinned MINOS_SUBNET implementation**, so the evaluator service needs one more piece of
provisioning: a checkout of that implementation, and an environment variable naming it.

```
export MINOS_L2F_MINOS_SUBNET_ROOT="/home/hr/bittensor/minos_l2f2_baseline/minos_subnet_pinned"
```

Requirements, all enforced at scoring time and all fail-closed:

* **Absolute, existing, not a symlink.** A symlinked root could be repointed between verification
  and use.
* **A git checkout or worktree whose HEAD is exactly the authority commit**
  `649bb92c6abccebde58a736a2b2af7fd77a701c1`. It must be a **detached, pinned** worktree — never
  the mutable development clone, whose HEAD is free to move.
* **The three authority files hash exactly** as `manifests/l2f2_scoring_authority_v2.json`
  records — the production authority; `…_v1.json` is superseded history, kept only so its
  published contract hash stays recomputable — and git reports them clean. A branch name, a
  directory name, an mtime or a caller-supplied hash prove nothing and are never consulted.

Those checks run **before** the scoring subprocess starts, which on its own only establishes that
the checkout *was* correct. The identity is therefore established two more times: the scoring
subprocess independently derives the git HEAD and the three digests itself — before its upstream
imports, again after them, and again after scoring — and the parent re-derives them once more
after that process exits. All of it must agree with the committed authority, or no result is
produced. In practice this means **the pinned worktree must not be edited, synced or rebuilt
while an evaluation is running**; if it is, the evaluation fails closed rather than attributing a
real score to source bytes that did not produce it.

Create it with `git worktree add --detach <path> 649bb92c…` from the upstream clone. The upstream
repository is an authority: it is never edited, committed into, rebased, reset or re-branched by
MINOS_ENGINE.

### The interpreter

The bridge that calls upstream runs under a **separate** interpreter, because upstream's package
names (`utils`, `templates`, `neurons`, `base`) are generic enough to collide with anything in
the long-lived evaluator process. That interpreter must already be able to import upstream's own
dependencies — MINOS_ENGINE never installs them at scoring time.

By default it is `<root>/.venv/bin/python`, so the environment that can import upstream is pinned
by the same provisioning step as the source. If the pinned worktree does not carry one, set:

```
export MINOS_L2F_MINOS_SUBNET_PYTHON="/absolute/path/to/python"
```

It must be an absolute path to an existing executable; anything else is refused.

### Docker, and the two images that must already be present

The pinned scorer runs its own containers (hap.py, and bcftools for its internal steps). Those
are **upstream's** commands: MINOS_ENGINE builds none of them and must not. The evaluator host
needs Docker reachable by the service account, and `DOCKER_HOST` is one of the few variables
passed through to the bridge subprocess.

Both images must be **provisioned in advance**. MINOS_ENGINE never pulls during scoring — a
scoring call must not fetch new bytes off the network — so an absent image is a refusal, not a
download:

```
docker pull genonet/hap-py@sha256:03acabe84bbfba35f5a7234129d524c563f5657e1f21150a2ea2797f8e6d05f2
docker pull quay.io/biocontainers/bcftools:1.20--h8b25389_0
```

Note the asymmetry, which is upstream's own and is reproduced rather than corrected: **hap.py is
digest-pinned in the pinned source, bcftools is tag-pinned.** MINOS_ENGINE does not rewrite the
tag — rewriting it would change the command upstream constructs. Instead it verifies, before
every real score, that the tag resolves on this host to the audited immutable content:

```
quay.io/biocontainers/bcftools@sha256:badc3a0c7af72a83e5761ab0e881aa84204694bdead003b47552cb283958f78d
```

If the tag has been moved upstream and a fresh pull brings different bytes, evaluation fails
closed rather than scoring against unaudited content. Re-auditing the new bytes and issuing a new
scoring authority is the deliberate operator action in that case — never a silent acceptance.

Verify a host is ready with:

```
docker image inspect quay.io/biocontainers/bcftools:1.20--h8b25389_0 --format '{{json .RepoDigests}}'
```

## Credential handling

The connection secret lives in a `0600` environment file beneath the canonical baseline
workspace:

```
/home/hr/bittensor/minos_l2f2_baseline/l2f2.env
```

Consistent with the canonical-workspace rule, it is never created directly under `/home/hr`, and
it is never committed. Rotate by `ALTER ROLE minos_evaluator_svc PASSWORD …` and updating that
file; no repository change is required, because the repository never holds the credential.

## CI

CI proves the *authority shape* without a production credential: it creates an ephemeral
`minos_evaluator_ci_svc LOGIN`, grants it `minos_evaluator` and nothing else, and then runs the
**whole production evaluation path** under that login — TRAIN truth registration, metrics
publication, artifact registration, evaluation persistence and read-back — before asserting the
denials (no direct `catalog.artifacts` write, no `experiments.*` mutation, no plan/config
mutation, no role escalation, and none of `SUPERUSER`/`CREATEDB`/`CREATEROLE`/`BYPASSRLS`). The
role is dropped afterwards. See `tests/integration/layer2_db/test_l2f2_evaluation_corrective.py`
and `tests/integration/layer2_db/test_l2f2_evaluation_ledger.py`.

Each denial statement is separately proven to be **well formed** by running it under a role that
is allowed to run it: a denial test whose statement dies on an unknown column would pass while
testing nothing.
