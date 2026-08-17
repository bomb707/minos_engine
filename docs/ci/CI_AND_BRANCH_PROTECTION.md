# CI and Branch-Protection Recommendations

## CI workflow
`.github/workflows/ci.yml` runs on `push`, `pull_request`, and
`workflow_dispatch`, on Python **3.11** and **3.12**:

```
python -m pip install -e ".[dev]"
ruff check .
ruff format --check .
mypy src
pytest --junitxml=reports/ci-junit.xml
pytest --cov=src/minos_engine --cov-fail-under=90 --cov-report=term-missing --cov-report=xml:reports/ci-coverage.xml
minos-engine doctor --json
minos-engine protocol snapshot --fixture tests/fixtures/api/valid_round.json
minos-engine config validate --config tests/fixtures/gatk/default_config.json --parameter-space tests/fixtures/api/gatk_parameter_space.json
```

Transient CI output (`reports/ci-*.xml`) is git-ignored and uploaded as a build
artifact, not committed.

## Branch-protection recommendations (not applied here)

These are recommendations only; repository settings are not changed by this
work. On the default branch (`main`):

1. **Require the `CI` status checks to pass** before merging — both
   `quality (py3.11)` and `quality (py3.12)`.
2. **Require a pull request** before merging; require at least one review.
3. **Require branches to be up to date** before merging (linear, no stale
   merges).
4. **Dismiss stale approvals** on new commits.
5. **Do not allow force-pushes or deletions** on `main`.
6. Optionally require signed commits and a linear history.
7. Treat `gates/protocol-ready.json` as **generated evidence**: it is produced
   by qualification (Commit B), never hand-edited; reviewers verify it with
   `minos-engine gate require-pass --gate gates/protocol-ready.json --base-dir .`.
