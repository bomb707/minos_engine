"""Workflow trigger policy — evaluated, not grepped.

The fast tier must run exactly once for a given commit. The mechanism is a job-level `if`
condition, so these tests parse the workflow YAML and *evaluate* that condition against synthetic
GitHub event payloads. A source-substring assertion would pass even if the condition were
logically wrong; evaluating it cannot.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
FAST = REPO_ROOT / ".github" / "workflows" / "ci-fast.yml"
FULL = REPO_ROOT / ".github" / "workflows" / "ci.yml"

REPOSITORY = "bomb707/minos_engine"


# --------------------------------------------------------------------------- #
# a minimal evaluator for the GitHub expression subset the condition uses
# --------------------------------------------------------------------------- #
def _lookup(context: dict[str, Any], dotted: str) -> Any:
    """Resolve `github.a.b.c` against a nested payload; a missing branch yields None."""
    node: Any = context
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node


def _operand(token: str, context: dict[str, Any]) -> Any:
    token = token.strip()
    if token.startswith("'") and token.endswith("'"):
        return token[1:-1]
    if token == "true":
        return True
    if token == "false":
        return False
    return _lookup(context, token)


def evaluate(expression: str, context: dict[str, Any]) -> bool:
    """Evaluate `a || b`, `a && b` and `x == y` / `x != y` over a github context.

    Deliberately tiny: it supports exactly the grammar the workflow uses. Anything richer would
    be a sign the workflow condition had grown too clever to reason about.
    """
    text = " ".join(expression.split())
    text = re.sub(r"^\$\{\{(.*)\}\}$", r"\1", text).strip()

    def _atom(part: str) -> bool:
        part = part.strip()
        for op, fn in (("!=", lambda a, b: a != b), ("==", lambda a, b: a == b)):
            if op in part:
                left, right = part.split(op, 1)
                return bool(fn(_operand(left, context), _operand(right, context)))
        return bool(_operand(part, context))

    for splitter, combine in (("||", any), ("&&", all)):
        if splitter in text:
            return bool(combine(evaluate(p, context) for p in text.split(splitter)))
    return _atom(text)


def _github(event_name: str, *, pr_head_repo: str | None = None, ref: str = "refs/heads/x"):
    payload: dict[str, Any] = {"event_name": event_name, "repository": REPOSITORY, "ref": ref}
    if pr_head_repo is not None:
        payload["event"] = {"pull_request": {"head": {"repo": {"full_name": pr_head_repo}}}}
    else:
        payload["event"] = {}
    return {"github": payload}


# --------------------------------------------------------------------------- #
# the evaluator itself must be trustworthy before it proves anything
# --------------------------------------------------------------------------- #
def test_evaluator_handles_equality_inequality_and_or() -> None:
    ctx = {"github": {"event_name": "push", "repository": REPOSITORY}}
    assert evaluate("github.event_name == 'push'", ctx) is True
    assert evaluate("github.event_name != 'push'", ctx) is False
    assert evaluate("github.event_name == 'x' || github.repository == 'bomb707/minos_engine'", ctx)
    assert not evaluate("github.event_name == 'x' && github.repository == REPOSITORY", ctx)


def test_evaluator_treats_a_missing_branch_as_none() -> None:
    ctx = _github("push")
    assert _lookup(ctx, "github.event.pull_request.head.repo.full_name") is None


# --------------------------------------------------------------------------- #
# B — the fast-tier event matrix
# --------------------------------------------------------------------------- #
@pytest.fixture(scope="module")
def fast_workflow() -> dict[str, Any]:
    return yaml.safe_load(FAST.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def fast_condition(fast_workflow: dict[str, Any]) -> str:
    condition = fast_workflow["jobs"]["fast"]["if"]
    assert isinstance(condition, str) and condition.strip()
    return condition


@pytest.mark.parametrize(
    ("case", "context", "expected"),
    [
        ("internal branch push, no open PR", _github("push"), True),
        ("internal branch push, open PR", _github("push"), True),
        (
            "same-repository pull_request",
            _github("pull_request", pr_head_repo=REPOSITORY),
            False,
        ),
        (
            "fork pull_request",
            _github("pull_request", pr_head_repo="someone-else/minos_engine"),
            True,
        ),
        ("workflow_dispatch", _github("workflow_dispatch"), True),
    ],
)
def test_fast_tier_event_matrix(
    fast_condition: str, case: str, context: dict[str, Any], expected: bool
) -> None:
    assert evaluate(fast_condition, context) is expected, case


def test_an_internal_branch_with_an_open_pr_runs_the_fast_job_exactly_once(
    fast_condition: str,
) -> None:
    """The whole point: one commit on an internal branch with an open PR yields ONE fast run.

    GitHub delivers both a push and a pull_request event for that commit. The push runs; the
    same-repository pull_request is skipped.
    """
    delivered = [_github("push"), _github("pull_request", pr_head_repo=REPOSITORY)]
    assert sum(evaluate(fast_condition, ctx) for ctx in delivered) == 1


def test_a_fork_pull_request_still_gets_validation(fast_condition: str) -> None:
    """A fork's push never reaches this repository, so its pull_request must not be skipped."""
    delivered = [_github("pull_request", pr_head_repo="contributor/minos_engine")]
    assert sum(evaluate(fast_condition, ctx) for ctx in delivered) == 1


def test_fast_tier_triggers_are_declared(fast_workflow: dict[str, Any]) -> None:
    triggers = fast_workflow[True] if True in fast_workflow else fast_workflow["on"]
    assert set(triggers) == {"push", "pull_request", "workflow_dispatch"}


def test_fast_tier_cancels_obsolete_runs_per_effective_branch(
    fast_workflow: dict[str, Any],
) -> None:
    concurrency = fast_workflow["concurrency"]
    assert concurrency["cancel-in-progress"] is True
    # keyed on the fork PR head label when present, otherwise the pushed ref
    assert "pull_request.head.label" in concurrency["group"]
    assert "github.ref" in concurrency["group"]


def test_fast_tier_checks_out_full_history(fast_workflow: dict[str, Any]) -> None:
    """Regression: the first run of this workflow failed because a shallow clone cannot satisfy
    the E5 prerequisite closure that two fast-tier unit modules exercise."""
    checkout = fast_workflow["jobs"]["fast"]["steps"][0]
    assert checkout["uses"].startswith("actions/checkout@")
    assert checkout["with"]["fetch-depth"] == 0


def test_fast_tier_starts_no_database_service(fast_workflow: dict[str, Any]) -> None:
    job = fast_workflow["jobs"]["fast"]
    assert "services" not in job
    assert "MINOS_DATABASE_URL" not in yaml.dump(job)


def test_fast_tier_does_not_reference_a_nonexistent_workflow_file() -> None:
    """C3: the stale `ci-full.yml` reference must be gone; the full tier lives at ci.yml."""
    text = FAST.read_text(encoding="utf-8")
    assert "ci-full.yml" not in text
    assert "ci.yml" in text
    assert FULL.is_file()


# --------------------------------------------------------------------------- #
# C/D — full-tier semantics stated truthfully
# --------------------------------------------------------------------------- #
@pytest.fixture(scope="module")
def full_workflow() -> dict[str, Any]:
    return yaml.safe_load(FULL.read_text(encoding="utf-8"))


def test_full_tier_triggers(full_workflow: dict[str, Any]) -> None:
    triggers = full_workflow[True] if True in full_workflow else full_workflow["on"]
    assert set(triggers) == {"pull_request", "workflow_dispatch", "schedule", "push"}
    assert triggers["pull_request"]["branches"] == ["main", "master", "integration"]
    # tags only: an ordinary feature-branch push must NOT trigger full qualification
    assert triggers["push"] == {"tags": ["v*"]}
    assert "branches" not in triggers["push"]


def test_a_feature_branch_push_does_not_trigger_full_qualification(
    full_workflow: dict[str, Any],
) -> None:
    triggers = full_workflow[True] if True in full_workflow else full_workflow["on"]
    push = triggers["push"]
    assert list(push) == ["tags"]
    assert not any(pattern in ("**", "*", "feature/**") for pattern in push["tags"])


def test_full_tier_runs_each_test_at_most_once(full_workflow: dict[str, Any]) -> None:
    """One full-suite invocation plus one non-overlapping lifecycle preflight."""
    steps = full_workflow["jobs"]["qualification"]["steps"]
    pytest_steps = [s for s in steps if "run" in s and re.search(r"\bpytest\b", s["run"])]
    assert len(pytest_steps) == 2, [s.get("name") for s in pytest_steps]

    preflight = next(s for s in pytest_steps if "stepwise" in s["run"])
    full_suite = next(s for s in pytest_steps if "--junitxml" in s["run"])
    assert preflight is not full_suite

    chain = "tests/integration/layer2_db/test_stepwise_migration_chain.py"
    assert chain in preflight["run"]
    # the preflight module is deselected from the full suite, so it executes once per workflow
    assert f"--deselect {chain}" in " ".join(full_suite["run"].split())


def test_full_tier_emits_junit_and_coverage_from_one_invocation(
    full_workflow: dict[str, Any],
) -> None:
    steps = full_workflow["jobs"]["qualification"]["steps"]
    full_suite = next(
        s for s in steps if "run" in s and "--junitxml" in s["run"] and "pytest" in s["run"]
    )
    run = " ".join(full_suite["run"].split())
    for flag in ("--junitxml=", "--cov=src/minos_engine", "--cov-fail-under=90", "--cov-report="):
        assert flag in run, flag


def test_no_workflow_claims_a_single_pytest_invocation() -> None:
    """D: the wording must not hide the preflight behind an 'exactly one invocation' claim."""
    for path in (FAST, FULL):
        text = path.read_text(encoding="utf-8").lower()
        for banned in (
            "exactly once, in a single invocation",
            "the single full-suite invocation",
            "no second pytest run anywhere",
            "one pytest invocation",
        ):
            assert banned not in text, (path.name, banned)


def test_full_tier_schedule_is_documented_as_default_branch_only() -> None:
    """A GitHub schedule event runs the default branch; the comment must say so."""
    text = FULL.read_text(encoding="utf-8")
    assert "default branch" in text.lower()
    assert "arbitrary feature or integration branch" in text.lower()


def test_full_tier_keeps_the_accepted_workflow_path() -> None:
    """Five qualification runners bind .github/workflows/ci.yml as git-bound evidence."""
    from minos_engine.qualification.layer2_ingest_runner import ci_asserts_head_0004
    from minos_engine.qualification.layer2_split_v2_runner import ci_asserts_head_0003

    assert FULL.is_file()
    assert ci_asserts_head_0003(REPO_ROOT) is True
    assert ci_asserts_head_0004(REPO_ROOT) is True
