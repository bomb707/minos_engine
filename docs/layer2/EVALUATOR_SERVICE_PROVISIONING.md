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
* `EXECUTE` on the three `0009` `SECURITY DEFINER` persistence functions
* `EXECUTE` on `evaluation.l2f_register_metrics_artifact` (`0010`) — the ONLY path by which the
  service registers the metrics document it publishes. It accepts a digest, URI and size; media
  type and provenance are fixed inside the function, so it cannot register any other kind of
  artifact

Deliberately **not** granted:

* any direct `INSERT`/`UPDATE`/`DELETE` on `catalog.artifacts` — registration goes through the
  `0010` registrar or not at all
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
