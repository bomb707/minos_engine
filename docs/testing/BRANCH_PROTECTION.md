# Required checks and branch protection

TEST-CI-1 renamed the CI job. Any branch-protection rule that still requires the **old** job name
will either block every pull request forever (waiting for a context that no longer reports) or,
if it was the only required check, silently stop gating anything.

**No repository setting was changed by this work.** Everything below is either a verified
observation or an action that requires a user with admin rights to perform and confirm.

---

## 1. Check names

| Tier | Workflow file | Job name reported to GitHub |
|---|---|---|
| Fast | `.github/workflows/ci-fast.yml` | `fast (python-3.12)` |

**`fast (python-3.12)` is the only status context GitHub reports for this repository.**

Two job names existed previously and no longer exist. Neither may remain as a required status
context on any branch — a required context that never reports blocks every pull request forever:

- `quality (python-3.12)` — removed by TEST-CI-1;
- `full qualification (python-3.12)` — removed by TEST-CI-3, which deleted the remote full
  workflow. Full qualification is now local (`make qualify-local`).

## 2. What may be required

- **`fast (python-3.12)`** — the only check that may be configured as a GitHub-required status
  context. It starts no database and runs no committed-gate verifier, so it is a smoke gate, not
  a qualification gate.
- **`quality (python-3.12)`** and **`full qualification (python-3.12)`** — must be **absent**
  from every required-checks list. Neither job exists.

Full qualification is not enforceable by GitHub any more. It is a manual local step
(`make qualify-local`) required at major stage boundaries and before operational changes, and its
result is reported by the person who ran it.

## 3. Verification status — requires user action

Branch-protection configuration **could not be read** from this environment: the
`/repos/bomb707/minos_engine/branches/{branch}/protection` endpoint returns
**HTTP 401 "Requires authentication"** for `main`, `master` and `integration`, and no credential
with admin scope is available here.

What *was* readable, from the public API:

- the repository default branch is **`main`**;
- the existing branches are `dev`, `feature/L2-D`, `feature/L2-E`, `feature/L2-F`, `fix/L2-C`
  and `main` — there is **no `master` and no `integration` branch**;
- every one of those branches reports **`protected: false`**.

Taken together this indicates that **no branch protection is currently configured**, and
therefore that no stale `quality (python-3.12)` required context is currently blocking anything.
That is an inference from the public `protected` flag, not a reading of the protection rules
themselves, so it **requires user verification**. It is not claimed here as configured.

`.github/workflows/ci.yml` triggers full qualification for pull requests targeting
`main`, `master` or `integration`. Two of those branches do not exist today; the entries are
harmless (they simply never match) and are kept so the trigger keeps working if either branch is
created later.

### Checklist for a user with admin rights

1. Open **Settings → Branches → Branch protection rules** for `main`.
2. If a rule exists, remove both `quality (python-3.12)` and
   `full qualification (python-3.12)` from *Require status checks to pass* — neither job exists,
   so either one would block every pull request indefinitely.
3. Optionally add `fast (python-3.12)`; it is the only context GitHub still reports.
4. If no rule exists, decide whether the default branch should be protected at all.

## 4. Full qualification is local

There is no remote full-qualification workflow. A push runs the fast tier only, and GitHub does
not run PostgreSQL, the whole suite, coverage, the migration lifecycle or the committed-gate
suite for this repository.

Before a major stage boundary or any operational change, run locally:

```bash
make qualify-local
```

It runs ruff, format check and mypy, then the full suite once with JUnit and coverage at >= 90%,
then the committed gate verifiers, then the DB-V2 contract and test-inventory checks, and returns
nonzero on any failure. It refuses to start if `MINOS_DATABASE_URL` resolves to the operational
store (`127.0.0.1:5433/minos_engine_db`), decided by parsing the DSN rather than matching a
string, and it never pushes, commits or migrates.

Historical L2-C and L2-D CI evidence is unaffected: it is verified from the frozen qualified
source commits that produced it, not from the current working tree.
