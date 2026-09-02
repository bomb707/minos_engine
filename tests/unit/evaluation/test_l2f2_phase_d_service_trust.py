"""The Phase-D evaluation service's trust surface: one argument, everything else derived.

``evaluate_validation_execution`` is a component seam whose caller supplies the engine, the
scoring authority, the oracle, the publisher and the provisioning. That is right for composition
and wrong for a production service: a caller who can hand in a ``ScoringAuthority`` can decide
what "score" means. These tests hold the production entry to accepting only which of the already
frozen executions to evaluate.
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

import pytest

from minos_engine.evaluation import phase_d_service
from minos_engine.evaluation.phase_d_service import (
    ACCEPTED_MINOS_SUBNET_COMMIT,
    ACCEPTED_SCORING_CONTRACT_HASH,
    PHASE_D_EVALUATOR_DATABASE,
    PHASE_D_EVALUATOR_REVISION,
    evaluate_l2f2_phase_d_execution,
)

_FORBIDDEN = (
    "engine",
    "database",
    "database_url",
    "url",
    "revision",
    "partition",
    "plan",
    "plan_hash",
    "authority",
    "scoring_contract",
    "oracle",
    "publisher",
    "provisioning",
    "truth",
    "truth_root",
    "reference",
    "reference_root",
    "work_dir",
    "work_root",
    "artifact_root",
    "minos_subnet_root",
    "container",
    "image",
)


def test_the_public_entry_accepts_only_an_execution_result_id() -> None:
    signature = inspect.signature(evaluate_l2f2_phase_d_execution)
    assert list(signature.parameters) == ["execution_result_id"]
    parameter = signature.parameters["execution_result_id"]
    assert parameter.kind is inspect.Parameter.KEYWORD_ONLY
    assert parameter.default is inspect.Parameter.empty


def test_the_public_entry_has_no_kwargs_trust_escape() -> None:
    """``**kwargs`` would reopen every door the explicit signature closes."""
    signature = inspect.signature(evaluate_l2f2_phase_d_execution)
    kinds = {p.kind for p in signature.parameters.values()}
    assert inspect.Parameter.VAR_KEYWORD not in kinds
    assert inspect.Parameter.VAR_POSITIONAL not in kinds


@pytest.mark.parametrize("forbidden", _FORBIDDEN)
def test_no_scientific_authority_is_caller_nominable(forbidden: str) -> None:
    assert forbidden not in inspect.signature(evaluate_l2f2_phase_d_execution).parameters


def test_the_component_seam_still_exists_and_is_still_injected() -> None:
    """The lower seam is kept deliberately — but it is NOT the production trust boundary."""
    from minos_engine.evaluation.validation_orchestrator import evaluate_validation_execution

    injected = set(inspect.signature(evaluate_validation_execution).parameters)
    assert {"authority", "oracle", "publisher", "provisioning"} <= injected
    # both name the execution — that is the operational selector, not a scientific authority.
    # What the production entry must NOT share is the injected DEPENDENCY surface.
    dependencies = injected - {"execution_result_id"}
    public = set(inspect.signature(evaluate_l2f2_phase_d_execution).parameters)
    assert dependencies & public == set(), dependencies & public
    assert public == {"execution_result_id"}


def test_the_frozen_identities_are_pinned_in_source() -> None:
    assert PHASE_D_EVALUATOR_DATABASE == "minos_l2f2_validation"
    assert PHASE_D_EVALUATOR_REVISION == "0025_l2f2_phase_d_eval_auth"
    assert ACCEPTED_SCORING_CONTRACT_HASH == (
        "b24a07e208ce8e2fff6672102ae4e61aed93c6f352a5af46ba81c4789adb76d6"
    )
    assert ACCEPTED_MINOS_SUBNET_COMMIT == "649bb92c6abccebde58a736a2b2af7fd77a701c1"


def test_the_service_module_holds_no_mutable_authority_global() -> None:
    """A module global rewritten to widen a check is a trust surface with no signature."""
    source = Path(inspect.getfile(phase_d_service)).read_text(encoding="utf-8")
    tree = ast.parse(source)
    assert not [n for n in ast.walk(tree) if isinstance(n, ast.Global)]


def test_the_public_entry_builds_its_own_engine_and_oracle() -> None:
    """Derived, not accepted: the entry constructs what a caller must not be able to name."""
    source = inspect.getsource(evaluate_l2f2_phase_d_execution)
    assert "create_db_engine()" in source
    assert "_evaluate_with_trust" in source
    assert "PHASE_D_EVALUATOR_DATABASE" in source
    assert "PHASE_D_EVALUATOR_REVISION" in source


def test_authority_is_established_before_the_component_seam_is_called() -> None:
    """Ordering is the guarantee: truth is opened by the seam, and only after every gate."""
    source = inspect.getsource(phase_d_service._evaluate_with_trust)
    order = [
        source.index("_authorize_evaluator_connection("),
        source.index("_require_exact_phase_d_execution("),
        source.index("_require_scoring_authority()"),
        source.index("return evaluate_validation_execution("),
    ]
    assert order == sorted(order), order


def test_the_service_never_creates_a_provisioned_root() -> None:
    """The evaluator validates roots; it does not provision them."""
    source = Path(inspect.getfile(phase_d_service)).read_text(encoding="utf-8")
    for forbidden in ("mkdir(", "makedirs(", "chmod(", "rmtree("):
        assert forbidden not in source, forbidden


def test_the_public_entry_nominates_neither_the_oracle_nor_the_authority() -> None:
    """The seam accepts both; the production call site supplies neither.

    Widening the private seam for a scratch proof is only safe while the public entry keeps
    deriving both from pinned sources, so that is asserted on the call site itself rather than
    inferred from the signature.
    """
    import ast
    import inspect

    from minos_engine.evaluation import phase_d_service

    tree = ast.parse(inspect.getsource(phase_d_service))
    entry = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "evaluate_l2f2_phase_d_execution"
    )
    calls = [
        node
        for node in ast.walk(entry)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "_evaluate_with_trust"
    ]
    assert len(calls) == 1, calls
    passed = {kw.arg for kw in calls[0].keywords}
    assert "oracle" not in passed and "authority" not in passed, passed
    assert calls[0].args == [], "the seam is keyword-only; a positional would bypass this check"


def test_the_seam_defaults_to_the_pinned_authority_rather_than_a_caller_value() -> None:
    """``authority=None`` must reach ``_require_scoring_authority``, not become a silent no-op."""
    import inspect

    from minos_engine.evaluation import phase_d_service

    source = inspect.getsource(phase_d_service._evaluate_with_trust)
    assert "authority if authority is not None else _require_scoring_authority()" in source, source
    assert (
        inspect.signature(phase_d_service._evaluate_with_trust).parameters["authority"].default
        is None
    )
