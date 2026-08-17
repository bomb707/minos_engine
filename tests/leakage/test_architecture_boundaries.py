"""Architecture / import-boundary guards enforced statically over the source tree."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from tests.conftest import SRC


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    mods: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            mods.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            mods.add(node.module)
    return mods


def _package_files(package: str) -> list[Path]:
    return sorted((SRC / package).rglob("*.py"))


def _all_source_files() -> list[Path]:
    return sorted(SRC.rglob("*.py"))


def _violations(files: list[Path], forbidden: tuple[str, ...]) -> dict[str, set[str]]:
    out: dict[str, set[str]] = {}
    for f in files:
        bad = {m for m in _imports(f) for tok in forbidden if tok in m}
        if bad:
            out[str(f.relative_to(SRC))] = bad
    return out


def test_layer1_cannot_import_eval_truth_or_layer2():
    forbidden = (
        "layer2",
        "twin",
        "evaluation",
        "truth",
        "mutation",
        "happy",
        "scoring",
        "retrieval",
    )
    assert _violations(_package_files("layer1"), forbidden) == {}


def test_layer2_cannot_import_bam_readers_or_intake():
    forbidden = ("pysam", "minos_engine.intake")
    assert _violations(_package_files("layer2"), forbidden) == {}


def test_live_package_cannot_import_evaluator_happy_or_scoring():
    forbidden = ("happy", "hap_py", "scoring", "evaluator", "genomics_config")
    assert _violations(_all_source_files(), forbidden) == {}


def test_production_code_never_imports_tests():
    # Production qualification/engine code must not depend on test helpers.
    offenders: dict[str, set[str]] = {}
    for f in _all_source_files():
        bad = {m for m in _imports(f) if m == "tests" or m.startswith("tests.") or m == "conftest"}
        if bad:
            offenders[str(f.relative_to(SRC))] = bad
    assert offenders == {}, offenders


def test_domain_modules_do_not_parse_cli_args():
    # Only cli/* may import argparse (Overall spec §6: domain has no argparse).
    for f in _all_source_files():
        if f.parent.name == "cli":
            continue
        assert "argparse" not in _imports(f), f"{f} imports argparse outside cli/"


def test_single_config_emission_interface():
    # Exactly one production CONFIG-emission entry point (Layer2Service.select_config).
    definers = []
    for f in _all_source_files():
        tree = ast.parse(f.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "select_config":
                definers.append(str(f.relative_to(SRC)))
    assert definers == ["layer2/service.py"], definers


@pytest.mark.parametrize("caller", ["deepvariant", "bcftools", "freebayes"])
def test_disabled_callers_cannot_be_active(caller):
    from minos_engine.common.errors import PolicyViolationError
    from minos_engine.settings import RuntimePolicy

    with pytest.raises(PolicyViolationError):
        RuntimePolicy(active=caller)


def test_submission_and_parameter_space_reject_non_gatk():
    from pydantic import ValidationError

    from minos_engine.callers.contracts import ParameterSpaceSnapshot
    from minos_engine.protocol.submission_contract import SubmissionEnvelope

    with pytest.raises(ValidationError):
        SubmissionEnvelope(tool="deepvariant", version="1", gatk_options={})
    with pytest.raises(ValidationError):
        ParameterSpaceSnapshot(
            caller="bcftools",
            parameters={},
            source="x",
            retrieved_at="2026-08-17T00:00:00+00:00",
            parameter_space_hash="a" * 64,
            stale=False,
        )
