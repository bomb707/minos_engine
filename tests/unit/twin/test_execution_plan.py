"""Test group C — GATK execution plan (no execution side effects)."""

from __future__ import annotations

import pytest

from minos_engine.callers.gatk.parameter_registry import REGISTRY
from minos_engine.common.errors import ConfigValidationError, PolicyViolationError
from minos_engine.qualification.twin_checks import make_request
from minos_engine.twin.execution_plan import (
    BAM_TOKEN,
    OUTPUT_TOKEN,
    REFERENCE_TOKEN,
    build_execution_plan,
)


def test_default_config_plan_has_all_params():
    plan = build_execution_plan(make_request({}))
    assert len(plan.effective_config) == 25
    assert plan.invocation.argv[0] == "gatk"
    assert plan.invocation.argv[1] == "HaplotypeCaller"


def test_placeholder_tokens_used_not_paths():
    plan = build_execution_plan(make_request())
    assert REFERENCE_TOKEN in plan.invocation.argv
    assert BAM_TOKEN in plan.invocation.argv
    assert OUTPUT_TOKEN in plan.invocation.argv


def test_argv_is_token_list_never_shell_string():
    plan = build_execution_plan(make_request())
    assert isinstance(plan.invocation.argv, tuple)
    assert all(isinstance(t, str) for t in plan.invocation.argv)
    # region -L uses 1-based inclusive coordinates
    li = plan.invocation.argv.index("-L")
    assert plan.invocation.argv[li + 1] == "chr19:13000000-23000000"


def test_deterministic_flag_ordering():
    a = build_execution_plan(make_request()).invocation.argv
    b = build_execution_plan(make_request()).invocation.argv
    assert a == b


def test_unsupported_caller_rejected():
    with pytest.raises(PolicyViolationError):
        build_execution_plan(make_request(tool="deepvariant"))


def test_unknown_parameter_rejected():
    with pytest.raises(ConfigValidationError):
        build_execution_plan(make_request({"totally_unknown": 1}))


def test_out_of_range_parameter_rejected():
    with pytest.raises(ConfigValidationError):
        build_execution_plan(make_request({"min_pruning": 999}))


@pytest.mark.parametrize(
    "name",
    [
        p.name
        for p in REGISTRY.all()
        if p.name not in {"min_assembly_region_size", "max_assembly_region_size"}
    ][:6],
)
def test_boundary_values_accepted(name):
    p = REGISTRY.get(name)
    if p.documented_min is None:
        return
    build_execution_plan(make_request({name: p.documented_min}))


def test_no_execution_side_effect(tmp_path, monkeypatch):
    # build_gatk_argv / build_execution_plan must not spawn a subprocess.
    import subprocess

    def _boom(*a, **k):  # pragma: no cover - must never be called
        raise AssertionError("plan construction must not execute a subprocess")

    monkeypatch.setattr(subprocess, "run", _boom)
    monkeypatch.setattr(subprocess, "Popen", _boom)
    build_execution_plan(make_request())
