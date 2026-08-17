"""Test group G — truth isolation, prohibited imports, no network, stage gating."""

from __future__ import annotations

import ast

from minos_engine.qualification.twin_checks import (
    architecture_boundaries_ok,
    no_hidden_network_dependency,
    truth_isolation_ok,
)
from minos_engine.twin.offline.truth_loader import load_truth_fixture
from tests.conftest import REPO_ROOT, SRC

_PRODUCTION = ("protocol", "callers", "layer1", "layer2", "intake", "manifests", "common")


def _imports(path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    out = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            out.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            out.add(node.module)
    return out


def test_production_does_not_import_twin_or_tools():
    offenders = {}
    for pkg in _PRODUCTION:
        for f in (SRC / pkg).rglob("*.py"):
            bad = {
                m
                for m in _imports(f)
                if m.startswith("minos_engine.twin") or m.startswith("minos_engine.tools")
            }
            if bad:
                offenders[str(f.relative_to(SRC))] = bad
    assert offenders == {}, offenders


def test_architecture_and_truth_checks_pass():
    assert architecture_boundaries_ok(SRC)
    assert truth_isolation_ok(SRC)


def test_no_network_dependency_in_twin_or_tools():
    assert no_hidden_network_dependency(SRC)


def test_truth_sentinel_cannot_reach_production_contract():
    # The truth fixture carries a sentinel accessible ONLY via the offline loader.
    fixture = load_truth_fixture(
        REPO_ROOT / "tests" / "fixtures" / "twin" / "truth" / "practice_truth.json"
    )
    sentinel = fixture.sentinel
    assert sentinel and "TRUTH_SENTINEL" in sentinel

    # Building a production submission envelope from a GATK config never contains it.
    from minos_engine.callers.gatk.config import canonicalize_config
    from minos_engine.protocol.submission_contract import build_submission_envelope

    effective = canonicalize_config({}).effective_config
    envelope = build_submission_envelope(effective, version="4.5.0.0")
    assert sentinel not in envelope.canonical_bytes().decode("utf-8")

    # And the sentinel appears in no production source file.
    for pkg in _PRODUCTION:
        for f in (SRC / pkg).rglob("*.py"):
            assert sentinel not in f.read_text(encoding="utf-8")


def test_layer1_implemented_and_layer2_blocked():
    import pytest

    from minos_engine.common.errors import StageNotReadyError
    from minos_engine.layer1.service import Layer1Service
    from minos_engine.layer2.service import Layer2Service

    # Layer 1 is implemented (real profiling entry point); Layer 2 remains blocked.
    assert hasattr(Layer1Service, "profile")
    with pytest.raises(StageNotReadyError):
        Layer2Service().select_config(None)  # type: ignore[arg-type]
