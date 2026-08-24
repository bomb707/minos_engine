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

Migration `0009` grants evaluation authority to the **group role**. The service principal gets
that authority purely by membership.

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

GRANT CONNECT ON DATABASE minos_l2f2_baseline TO minos_evaluator_svc;
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

## What the principal can and cannot do

Granted by `0009` through `minos_evaluator`:

* `USAGE` on schema `evaluation`
* `SELECT` on `evaluation.l2f_completed_execution_inputs` — the narrow completed-execution
  projection, **not** the L2-F experiment tables
* `SELECT` on `evaluation.l2f_train_truth_registration_targets` — **TRAIN only**; validation and
  test are structurally absent from that view
* `SELECT` on `evaluation.dataset_evaluation_identity`, `l2f_evaluation_results`,
  `l2f_evaluation_failures`
* `EXECUTE` on the three `SECURITY DEFINER` persistence functions

Deliberately **not** granted:

* any write on `experiments.*` — the execution ledger stays owned by the runner path
* broad `SELECT` on the L2-F experiment tables — `0008` gives application roles no direct table
  privileges there, and `0009` preserves that boundary
* anything reaching validation or test truth

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
`minos_evaluator_ci_svc LOGIN`, grants it `minos_evaluator`, asserts it can read the two
projections and cannot mutate `experiments.l2f_execution_results`, `l2f_experiment_jobs` or
`l2f_experiment_plan_configs`, and drops the role afterwards. See
`tests/integration/layer2_db/test_l2f2_evaluation_ledger.py`.
