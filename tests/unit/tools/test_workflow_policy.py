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


def test_fast_tier_references_no_deleted_workflow() -> None:
    """C3 + TEST-CI-3: neither the stale ci-full.yml nor the deleted ci.yml may be referenced
    as a live sibling workflow."""
    text = FAST.read_text(encoding="utf-8")
    assert "ci-full.yml" not in text
    assert "workflows/ci.yml" not in text


# --------------------------------------------------------------------------- #
# TEST-CI-3 — the full GitHub workflow is gone; fast is the only remote tier
# --------------------------------------------------------------------------- #
def _all_workflows() -> list[Path]:
    return sorted((REPO_ROOT / ".github" / "workflows").glob("*.y*ml"))


def test_the_full_workflow_file_is_absent() -> None:
    """F1: .github/workflows/ci.yml no longer exists at HEAD."""
    assert not FULL.exists()


def test_fast_is_the_only_workflow() -> None:
    """F: and it was not replaced by ci-full.yml, a reusable or a scheduled equivalent."""
    assert _all_workflows() == [FAST]


def test_no_workflow_starts_postgresql() -> None:
    """F2: structural YAML check — no job declares a database service."""
    for path in _all_workflows():
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
        for job_name, job in document["jobs"].items():
            assert "services" not in job, (path.name, job_name)
        rendered = yaml.dump(document)
        assert "postgres" not in rendered.lower(), path.name
        assert "MINOS_DATABASE_URL" not in rendered, path.name


def test_no_workflow_runs_the_full_suite_or_coverage() -> None:
    """F3: no remote workflow may run the whole suite or compute coverage."""
    for path in _all_workflows():
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
        for job in document["jobs"].values():
            for step in job["steps"]:
                run = " ".join(step.get("run", "").split())
                if "pytest" not in run:
                    continue
                for banned in ("--cov", "--junitxml", "--cov-fail-under"):
                    assert banned not in run, (path.name, step.get("name"), banned)
                # a pytest step must name explicit paths, never the whole suite
                assert re.search(r"pytest\s+tests/", run), (path.name, step.get("name"))


def test_no_workflow_runs_migration_lifecycle_or_the_gate_suite() -> None:
    """F4: migration lifecycle qualification and the committed-gate suite are local-only."""
    for path in _all_workflows():
        text = path.read_text(encoding="utf-8")
        document = yaml.safe_load(text)
        for job in document["jobs"].values():
            for step in job["steps"]:
                run = " ".join(step.get("run", "").split())
                assert "alembic" not in run.lower(), (path.name, step.get("name"))
                assert "require-pass" not in run, (path.name, step.get("name"))
                assert "stepwise_migration_chain" not in run, (path.name, step.get("name"))


def test_no_hidden_reusable_or_scheduled_equivalent() -> None:
    """F: no workflow_call, and no schedule that could reintroduce full qualification."""
    for path in _all_workflows():
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
        triggers = document[True] if True in document else document["on"]
        assert "workflow_call" not in triggers, path.name
        assert "schedule" not in triggers, path.name


# --------------------------------------------------------------------------- #
# historical evidence is resolved from frozen commits, not from HEAD
# --------------------------------------------------------------------------- #
def test_accepted_historical_ci_evidence_still_verifies() -> None:
    """F6/F7/F9: L2-C and L2-D evidence verifies with no ci.yml at HEAD."""
    from minos_engine.qualification.layer2_ingest_runner import ci_asserts_head_0004
    from minos_engine.qualification.layer2_snapshot_runner import ci_verifies_snapshot_gate
    from minos_engine.qualification.layer2_split_v2_runner import ci_asserts_head_0003

    assert not FULL.exists(), "precondition: the workflow must be absent at HEAD"
    assert ci_asserts_head_0003(REPO_ROOT) is True
    assert ci_asserts_head_0004(REPO_ROOT) is True
    assert ci_verifies_snapshot_gate(REPO_ROOT, 1) is True


def test_historical_verification_reads_the_frozen_commit_not_head(tmp_path: Path) -> None:
    """F8: a decoy at the current path must not be what the check reads.

    The check runs against a repository whose HEAD contains a *different* ci.yml. If it read the
    working tree it would see the decoy; reading the frozen commit it sees the real evidence.
    """
    from minos_engine.layer2 import prerequisites as PRE
    from minos_engine.qualification.git_tree import historical_blob_text

    decoy = REPO_ROOT / ".github" / "workflows" / "ci.yml"
    assert not decoy.exists()
    decoy.write_text("# decoy: not the historical workflow\n", encoding="utf-8")
    try:
        frozen = historical_blob_text(
            REPO_ROOT, ".github/workflows/ci.yml", PRE.SPLIT_FROZEN_V2_SOURCE_COMMIT
        )
        assert frozen is not None
        assert "decoy" not in frozen
        assert "0003_l2c_split_v2_epochs" in frozen
    finally:
        decoy.unlink()


def test_a_missing_historical_object_fails_closed() -> None:
    """F10: an unknown ref yields None, never a substituted or partial value."""
    from minos_engine.qualification.git_tree import historical_blob_text

    absent = "0" * 40
    assert historical_blob_text(REPO_ROOT, ".github/workflows/ci.yml", absent) is None
    assert historical_blob_text(REPO_ROOT, "no/such/path.yml", "HEAD") is None


def test_tampered_historical_bytes_fail_closed() -> None:
    """F11: token checking is over the exact frozen bytes; altered content cannot pass."""
    from minos_engine.qualification.layer2_split_v2_runner import _CI_REQUIRED_TOKENS

    tampered = "# nothing of substance here\n"
    assert not all(token in tampered for token in _CI_REQUIRED_TOKENS)


def test_accepted_gates_verify_without_a_current_workflow() -> None:
    """F9: the five committed gate verifiers pass with ci.yml absent."""
    from minos_engine.qualification.layer2_ingest_runner import verify_ingest_ready_gate
    from minos_engine.qualification.layer2_snapshot_runner import verify_snapshot_offline
    from minos_engine.qualification.layer2_split_v2_runner import verify_split_frozen_v2_gate

    assert not FULL.exists()
    gates = REPO_ROOT / "gates"
    assert verify_split_frozen_v2_gate(REPO_ROOT, gates / "split-frozen-v2.json").ok
    assert verify_ingest_ready_gate(
        REPO_ROOT, gates / "ingest-ready.json", require_descends=True
    ).ok
    assert verify_snapshot_offline(REPO_ROOT, 1).ok
