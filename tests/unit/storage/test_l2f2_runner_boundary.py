"""Source-level controls on the L2-F2 runner boundary: signature, trust, and truth-freedom.

Pure static/structural checks. No database, no GATK, no filesystem execution.
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

import pytest

from minos_engine.storage import l2f2_runner


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def _module_source(module_name: str) -> str:
    return (_repo_root() / "src" / "minos_engine" / "storage" / f"{module_name}.py").read_text(
        encoding="utf-8"
    )


def _executable_source(module_name: str) -> str:
    """The module's CODE with comments and docstrings removed.

    A docstring that states "this never issues SET ROLE" would otherwise fail a naive substring
    check, and weakening the check to accommodate it would gut the control. Only genuine
    docstrings are removed — via the AST, so real string literals such as SQL are untouched.
    """
    import ast as _ast

    source = _module_source(module_name)
    lines = source.splitlines()
    tree = _ast.parse(source)
    drop: set[int] = set()
    for node in _ast.walk(tree):
        if not isinstance(
            node, _ast.Module | _ast.ClassDef | _ast.FunctionDef | _ast.AsyncFunctionDef
        ):
            continue
        body = getattr(node, "body", [])
        if not body:
            continue
        first = body[0]
        if (
            isinstance(first, _ast.Expr)
            and isinstance(first.value, _ast.Constant)
            and isinstance(first.value.value, str)
            and first.end_lineno is not None
        ):
            drop.update(range(first.lineno, first.end_lineno + 1))
    kept = [
        line
        for number, line in enumerate(lines, start=1)
        if number not in drop and not line.lstrip().startswith("#")
    ]
    return "\n".join(kept)


# --------------------------------------------------------------------------- #
# the PUBLIC entry accepts nothing that could weaken it
# --------------------------------------------------------------------------- #
def test_the_public_entry_accepts_only_a_worker_identity() -> None:
    signature = inspect.signature(l2f2_runner.execute_next_l2f2_phase_a_job)
    assert list(signature.parameters) == ["worker_id"]
    assert signature.parameters["worker_id"].kind is inspect.Parameter.KEYWORD_ONLY


@pytest.mark.parametrize(
    "forbidden",
    [
        "runner",
        "engine",
        "database_url",
        "plan",
        "plan_hash",
        "protocol_hash",
        "candidate_set",
        "member",
        "config",
        "dataset_root",
        "publisher",
        "work_root",
        "artifact_root",
        "gatk_runtime_bundle_sha256",
        "require_operational_identity",
        "allow_baseline",
        "trust",
    ],
)
def test_the_public_entry_has_no_injection_parameter(forbidden: str) -> None:
    """A caller must not be able to supply trust, a plan, a path or a runner."""
    parameters = inspect.signature(l2f2_runner.execute_next_l2f2_phase_a_job).parameters
    assert forbidden not in parameters


def test_the_public_entry_constructs_the_real_runner_itself() -> None:
    source = inspect.getsource(l2f2_runner.execute_next_l2f2_phase_a_job)
    assert "SubprocessGatkRunner.from_env()" in source
    assert "FakeGatkRunner" not in source


def test_no_fake_runner_can_reach_the_l2f2_production_module() -> None:
    assert "FakeGatkRunner" not in _executable_source("l2f2_runner")


def test_the_private_test_seam_is_not_exported_and_says_so() -> None:
    assert "_execute_l2f2_job" not in l2f2_runner.__all__
    doc = inspect.getdoc(l2f2_runner._execute_l2f2_job) or ""
    assert "TEST-ONLY" in doc
    assert "never exported" in doc


def test_the_boundary_requires_the_baseline_database_and_revision() -> None:
    """Each store's required revision is EXACT — never a floor, never a stale pin.

    It tracks the STORE's revision, not merely the migrations the runner itself needs: the runner
    and the evaluator share a database, so a revision the evaluator requires is one this boundary
    must still recognise.

    Until ``0021`` there was one store, so the required revision and the repository head were the
    same string and this test asserted that. ``0021`` ends that coincidence on purpose: L2-F2-F
    runs in a SEPARATE validation database, and the TRAIN baseline is scientifically closed at
    ``0020``. Migrating a completed 500-observation ledger's database forward to keep one constant
    tidy would be changing evidence for the convenience of a test, so the head now belongs to the
    validation store and the TRAIN pin stays where the science left it."""
    from minos_engine.qualification.l2f_accepted_identities import recompute_alembic_head

    assert l2f2_runner.BASELINE_DATABASE_NAME == "minos_l2f2_baseline"
    assert l2f2_runner.BASELINE_REVISION == "0020_l2f2_phase_c_execution"
    assert l2f2_runner.VALIDATION_DATABASE_NAME == "minos_l2f2_validation"
    assert l2f2_runner.VALIDATION_REVISION == "0024_l2f2_phase_d_anchor"
    # the head is the latest store's pin, and the two stores are distinct
    assert recompute_alembic_head() == l2f2_runner.VALIDATION_REVISION
    assert l2f2_runner.BASELINE_REVISION != l2f2_runner.VALIDATION_REVISION
    assert l2f2_runner.BASELINE_DATABASE_NAME != l2f2_runner.VALIDATION_DATABASE_NAME
    # both public entries delegate to ONE body, so neither store can drift from the other's checks
    for entry in (
        l2f2_runner.authorize_baseline_runner_connection,
        l2f2_runner.authorize_validation_runner_connection,
    ):
        assert "_authorize_runner_connection(" in inspect.getsource(entry)
    source = inspect.getsource(l2f2_runner._authorize_runner_connection)
    assert "current_database()" in source
    assert "alembic_version" in source
    # the SESSION principal is checked, so an already-issued SET ROLE cannot disguise it
    assert "session_user" in source
    # the principal and membership checks live in the SHARED body, so the validation store cannot
    # be authorized by a weaker boundary than the TRAIN one
    assert "rolsuper" in source
    assert "pg_auth_members" in source
    assert "_REQUIRED_MEMBERSHIP" in source
    assert "current_user" not in _executable_source("l2f2_runner")


def test_the_boundary_never_escalates_to_an_administrative_role() -> None:
    code = _executable_source("l2f2_runner")
    assert "SET LOCAL ROLE" not in code
    assert "SET ROLE" not in code
    # minos_admin appears in the runner ONLY as a forbidden membership it refuses to hold
    assert "_FORBIDDEN_MEMBERSHIPS" in code
    assert "minos_admin" in code


def test_the_boundary_never_writes_a_table_directly() -> None:
    """Every mutation goes through a SECURITY DEFINER function, never raw DML."""
    code = _executable_source("l2f2_runner").upper()
    for statement in ("INSERT INTO", "UPDATE ", "DELETE FROM"):
        assert statement not in code, f"raw {statement.strip()} in the runner boundary"


def test_the_boundary_registers_artifacts_only_through_the_narrow_registrar() -> None:
    code = _executable_source("l2f2_runner")
    assert "l2f2_register_execution_artifact" in code
    assert "catalog.artifacts" not in code


# --------------------------------------------------------------------------- #
# the runner is TRUTH-FREE
# --------------------------------------------------------------------------- #
_TRUTH_TOKENS = (
    "truth.vcf",
    "mutations.vcf",
    "dataset_evaluation_identity",
    "l2f_evaluation_results",
    "l2f_evaluation_failures",
    "HappyRunner",
    "hap.py",
    "AdvancedScorer",
    "minos_score",
)


@pytest.mark.parametrize("module_name", ["l2f2_runner", "l2f2_canary_prepare"])
@pytest.mark.parametrize("token", _TRUTH_TOKENS)
def test_the_l2f2_execution_modules_are_truth_free(module_name: str, token: str) -> None:
    assert token not in _executable_source(module_name)


@pytest.mark.parametrize("module_name", ["l2f2_runner", "l2f2_canary_prepare"])
def test_the_l2f2_execution_modules_never_import_the_evaluation_package(
    module_name: str,
) -> None:
    tree = ast.parse(_module_source(module_name))
    for node in ast.walk(tree):
        names: list[str] = []
        if isinstance(node, ast.Import):
            names = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module:
            names = [node.module]
        for name in names:
            assert "evaluation" not in name, f"{module_name} imports {name}"
            assert "happy" not in name.lower(), f"{module_name} imports {name}"


# --------------------------------------------------------------------------- #
# the historical F5 entry is untouched
# --------------------------------------------------------------------------- #
def test_the_historical_f5_entry_still_requires_the_operational_database() -> None:
    from minos_engine.storage import l2f_execution

    source = inspect.getsource(l2f_execution.execute_next_accepted_job)
    assert "require_operational_identity=True" in source
    assert "SubprocessGatkRunner.from_env()" in source
    parameters = inspect.signature(l2f_execution.execute_next_accepted_job).parameters
    assert list(parameters) == ["worker_id"]


def test_the_historical_f5_entry_gained_no_bypass_flag() -> None:
    from minos_engine.storage import l2f_execution

    parameters = inspect.signature(l2f_execution.execute_next_accepted_job).parameters
    for bypass in ("allow_baseline", "require_operational_identity", "revision", "engine"):
        assert bypass not in parameters


def test_the_historical_private_trust_seam_is_still_fake_typed() -> None:
    """L2-F2 must not have widened the historical test-only helper to a real runner."""
    from minos_engine.storage import l2f_execution

    signature = inspect.signature(l2f_execution._execute_next_job_with_trust)
    assert signature.parameters["runner"].annotation == "FakeGatkRunner"
    assert "_execute_next_job_with_trust" not in l2f_execution.__all__


def test_the_l2f2_boundary_does_not_call_the_historical_private_seam() -> None:
    assert "_execute_next_job_with_trust" not in _executable_source("l2f2_runner")


# --------------------------------------------------------------------------- #
# the dispatch result carries what the evaluator needs
# --------------------------------------------------------------------------- #
def test_the_dispatch_result_exposes_the_execution_result_id() -> None:
    fields = {f.name for f in l2f2_runner.L2F2DispatchResult.__dataclass_fields__.values()}
    assert "execution_result_id" in fields
    for required in ("job_id", "job_key", "plan_hash", "result_hash", "vcf_sha256", "runtime_ms"):
        assert required in fields
