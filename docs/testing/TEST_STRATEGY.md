# MINOS Test Strategy — tiers, commands and removal policy

Machine-readable companion:
[`reports/testing/MINOS_TEST_INVENTORY.json`](../../reports/testing/MINOS_TEST_INVENTORY.json),
one record per test file. `python scripts/test_inventory.py verify` re-derives it from the working
tree and fails if the report has drifted, so the inventory cannot silently go stale.

---

## 1. Why two tiers

The previous single workflow ran the whole suite twice — once for JUnit (5m25s) and again for
coverage (6m57s) — then re-ran `tests/integration/layer2_db` (3m02s) and four more integration
directories that the full suite had already covered. Of a 17m25s workflow, roughly 9 minutes was
repetition.

The rule now: **a test runs at most once per workflow**, unless a genuinely different environment
or database revision is being exercised.

| Tier | When it runs | Target | What it proves |
|---|---|---|---|
| **fast** | every internal push; fork pull requests; manual dispatch | ≤ 4 min warm | Static checks + everything that needs no database |
| **full** | PRs targeting `main`/`master`/`integration`, `v*` tags, nightly on the **default branch**, manual dispatch | ≤ 10 min | One full-suite invocation with coverage, one lifecycle preflight, every gate once |
| **manual_privileged** | never automatic | — | Behaviour needing host privileges a hosted runner lacks |
| **retire_after_dbv2** | — | — | Reserved: tests that only exist to protect a V1 structure DB-V2 will remove |

### What a push actually triggers

A push to a feature branch runs **the fast tier only**. It does **not** trigger full
qualification. Full qualification runs for pull requests targeting `main`/`master`/`integration`,
for `v*` tags, on manual dispatch, and on the nightly schedule — and a GitHub `schedule` event
always runs the repository **default branch** (`main`), never a feature or integration branch.

Before a DB-V2 stage acceptance, full qualification must therefore be **dispatched manually on
the exact stage SHA**, unless an applicable pull request already ran it on that commit. See
[BRANCH_PROTECTION.md](BRANCH_PROTECTION.md).

### One commit, one fast run

GitHub delivers both a `push` and a `pull_request` event when a branch inside this repository has
an open pull request. The fast job carries a condition that skips the same-repository
`pull_request`, because its `push` run already covered the identical commit:

| Event | Result |
|---|---|
| internal branch push, no open PR | runs |
| internal branch push, open PR | runs **once** (the push; the pull_request is skipped) |
| same-repository `pull_request` | skipped |
| `pull_request` from a fork | runs — a fork's push never reaches this repository |
| `workflow_dispatch` | runs |

`tests/unit/tools/test_workflow_policy.py` proves this matrix by *evaluating* the job condition
against synthetic event payloads, not by matching workflow source text. Concurrency is keyed on
the effective branch (a fork PR's head label, otherwise the pushed ref), so a newer push still
cancels an obsolete in-progress run.

The fast tier checks out **full history**. Two fast-tier modules —
`tests/unit/storage/test_l2f_harness_verifier_attacks.py` and `test_l2f_job_enqueue_unit.py` —
build the accepted plan, which runs the E5 prerequisite closure over git ancestry. A shallow
clone lacks those objects and the closure correctly fails closed; the first run of the fast
workflow failed for exactly this reason.

---

## 2. Commands

**Fast** — no PostgreSQL, no `MINOS_DATABASE_URL`, no coverage:

```bash
make test-fast
```

```bash
pytest tests/unit tests/leakage tests/determinism tests/protocol_contract
```

Measured at **27 s** locally. It covers unit tests, protocol contracts, leakage and architecture
boundaries, determinism, the DB-V2 report validator and the security tests that need no database.

**Full** — one full-suite invocation producing both JUnit and coverage, plus one
non-overlapping lifecycle preflight; every test executes at most once per workflow. The preflight
(`test_stepwise_migration_chain.py`) is deselected from the full-suite run and is kept separate
so a broken migration chain fails in seconds with its own diagnostics rather than being buried
inside a five-minute suite run:

```bash
make test-full
```

```bash
pytest \
  --junitxml=reports/ci-junit.xml \
  --cov=src/minos_engine \
  --cov-fail-under=90 \
  --cov-report=term-missing \
  --cov-report=xml:reports/ci-coverage.xml
```

Never run a second *full-suite* `pytest` for coverage — the single invocation emits both. The
lifecycle preflight is not a second pass over the suite: it runs one module that the full-suite
invocation explicitly deselects.

**Manual privileged** — not run on ordinary hosted runners:

```bash
sudo -E pytest -m privileged
```

Host prerequisites: a Linux host where the invoking user can change file ownership (`chown`) and
create device or FIFO nodes outside `tmpfs` restrictions, plus a local PostgreSQL 16 the user may
create and drop databases in. These tests are **not weaker** than the others — they are excluded
from automation because a GitHub-hosted runner cannot grant what they need, not because they are
optional.

**Housekeeping:**

```bash
make clean-test-artifacts
```

It removes only named tool caches (`.pytest_cache`, `.mypy_cache`, `.ruff_cache`, `.coverage`,
`__pycache__` under `src`/`tests`/`scripts`) and the transient CI XML files. It never touches
`reports/`, `gates/`, `manifests/`, evidence or runtime state, and it never recursively removes a
directory it was not given by name.

---

## 3. Markers

Registered in `pyproject.toml`: `integration`, `postgres`, `acceptance`, `security`, `migration`,
`slow`, `privileged`, `gate`.

Markers are applied at **module level** (`pytestmark = [...]`) or by directory convention, not
mechanically to every function. A marker exists to make a selection expressible; if the directory
already expresses it, the marker is noise.

---

## 4. Removal policy

A test is removed only when one of these is **proven**, and the proof is recorded in the inventory
record's `reason` and `replacement` fields:

1. **Exact duplicate** — the normalized AST of the test body is byte-identical to another test's,
   and both reach the same production boundary.
2. **Obsolete** — the code it covers no longer exists.
3. **Replaced by stronger coverage** — a named replacement asserts everything the removed test did,
   and more.

Filename similarity is never proof. Detection is AST and reference based
(`python scripts/test_inventory.py duplicates` / `unused`).

**Never removed**, regardless of size or runtime:

- attack cases exercising different threat models;
- separate pre-commit, ambiguous-commit and post-commit behaviour;
- real PostgreSQL foreign-key, trigger and function tests;
- concurrency races;
- descriptor-bound filesystem tests;
- truth-leakage tests;
- migration upgrade/downgrade behaviour;
- privileged tests — those move to `manual_privileged`, they are not deleted.

Coverage percentage alone is never a reason to keep a duplicate, and never a reason to delete a
test that protects a behaviour nothing else protects.

### What this stage removed

Nine deletions, each proven:

| Removed | Proof | Replacement |
|---|---|---|
| 6 × identical `select_config` blocked assertion | byte-identical normalized AST; same public boundary | `tests/acceptance/test_stage_gates.py::test_layer2_blocked` |
| 1 × identical `database_url()` missing-env assertion | byte-identical; same boundary | `tests/unit/layer2/test_storage_config.py::test_missing_env_fails_closed` |
| 2 × `tests/conftest.py` fixtures | no test or conftest requests either name | — |

**Deliberately kept** despite identical bodies:

- `test_qualification_runner.py::test_assemble_reject_when_a_tool_fails` and
  `twin/test_twin_qualification.py::test_reject_when_tool_fails` — each calls a *different* local
  `_assemble` against a different production runner. Distinct boundaries need separate proof.
- The two E4 attack tests in `test_accepted_plan.py` — identical bodies, different
  `@pytest.mark.parametrize` lists. The body-level detector cannot see decorators; the cases differ.

### What replaced the inline workflow steps

Eight workflow steps ran `alembic downgrade` followed by a Python heredoc embedded in YAML. They
are now `tests/integration/layer2_db/test_stepwise_migration_chain.py`, which walks
`head → 0005 → 0004 → 0003 → 0002 → 0001 → base → head` and asserts, at every stop, both that the
stage's objects are gone and that the previous stage's objects survive. That is strictly more than
the YAML asserted, it is version-controlled and reviewable, and it runs once in the full tier.

Six CLI smoke steps were removed because `tests/component/test_cli.py` already exercises `doctor`,
`protocol snapshot`, `config validate` and `gate verify` behaviourally, and `tests/component/twin/`
covers twin plan, replay and parity.


---

## 5. Verifying the inventory

`python scripts/test_inventory.py verify` re-derives the whole report from the working tree and
requires **semantic equality, record by record** — path, counts, line count, classification
inputs, tier, decision, reason and replacement, plus the redundancy and tier sections. Totals
alone are not enough: swapping two files' counts, retiering one module or editing a decision all
leave the totals identical.

Parsing is strict (a duplicate JSON key is an error), the build is deterministic (duplicate-body
digests are SHA-256, not Python's per-process randomized `hash()`), and no field is exempt from
equality — there is no timestamp or absolute path in the report to exempt. `verify` never
rewrites the document it checks.

Adding or removing a test therefore requires regenerating the report:

```bash
python scripts/test_inventory.py inventory
```
