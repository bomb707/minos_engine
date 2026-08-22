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
| Full qualification | `.github/workflows/ci.yml` | `full qualification (python-3.12)` |

The name that existed before TEST-CI-1 was **`quality (python-3.12)`**. That job no longer
exists. It must not remain as a required status context on any branch.

## 2. What should be required

- **`full qualification (python-3.12)`** — required to merge a pull request into the default
  branch, and required before a DB-V2 stage acceptance.
- **`fast (python-3.12)`** — useful as an additional required check. It is not a substitute:
  it starts no database and runs no committed-gate verifier.
- **`quality (python-3.12)`** — must be **removed** from every required-checks list.

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
2. If a rule exists, remove `quality (python-3.12)` from *Require status checks to pass*.
3. Add `full qualification (python-3.12)` as a required check.
4. Optionally add `fast (python-3.12)` as well.
5. If no rule exists, decide whether the default branch should be protected at all — until it
   is, neither tier gates a merge.

## 4. Triggering full qualification for a stage acceptance

Full qualification does **not** run on an ordinary feature-branch push. A user push to
`feature/L2-F` runs the fast tier only.

It runs for: pull requests targeting `main`/`master`/`integration`, version tags (`v*`), manual
dispatch, and the nightly schedule — and a GitHub `schedule` event always runs the repository
**default branch** (`main`), never a feature or integration branch.

So before a DB-V2 stage acceptance, unless an applicable pull request has already run it on that
exact commit, full qualification must be **dispatched manually on the exact stage SHA**:

```bash
gh workflow run ci.yml --ref feature/L2-F
```

`--ref` accepts a branch; to qualify one specific commit, ensure the branch tip *is* that commit,
then confirm the run's `head_sha` matches:

```bash
gh run list --workflow=ci.yml --limit 1 --json headSha,status,conclusion
```

Treat a stage as qualified only when a `full qualification (python-3.12)` run reports success
against the exact SHA being accepted.
