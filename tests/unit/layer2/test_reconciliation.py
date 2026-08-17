"""The feature-registry reconciliation is clean and schema-valid."""

from __future__ import annotations

import importlib.util
from pathlib import Path

from minos_engine.layer2 import feature_registry as FR
from minos_engine.schema_registry import validate_against
from tests.conftest import REPO_ROOT

_GEN = REPO_ROOT / "scripts" / "qualification" / "layer2_feature_reconciliation.py"


def _load_generator():
    spec = importlib.util.spec_from_file_location("_l2_recon", _GEN)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_reconciliation_passes_and_is_schema_valid():
    art = _load_generator().build_reconciliation()
    validate_against("layer2-feature-reconciliation-v1", art)
    assert art["passed"] is True
    assert art["missing_analytical_scalar_paths"] == []
    assert art["duplicate_analytical_scalar_paths"] == []
    assert art["unclassified_analytical_scalar_paths"] == []
    assert art["unknown_registry_scalar_paths"] == []
    assert art["non_scalar_model_feature_paths"] == []
    assert art["registry_hash"] == FR.REGISTRY_HASH


def test_committed_reconciliation_matches_registry():
    committed = REPO_ROOT / "reports" / "LAYER2_FEATURE_REGISTRY_RECONCILIATION.json"
    if not committed.exists():  # produced in the remediation evidence commit
        return
    import json

    art = json.loads(Path(committed).read_text(encoding="utf-8"))
    validate_against("layer2-feature-reconciliation-v1", art)
    assert art["passed"] is True
    assert art["registry_hash"] == FR.REGISTRY_HASH
