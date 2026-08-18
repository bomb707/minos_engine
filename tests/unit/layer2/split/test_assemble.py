"""Cover the SPLIT-FROZEN assemble path with synthetic inputs (no subprocess/PG)."""

from __future__ import annotations

from minos_engine.gates.contracts import EvidenceItem, EvidenceKind
from minos_engine.gates.required_checks import required_checks_for
from minos_engine.qualification.coverage import CoverageResult
from minos_engine.qualification.layer2_split_runner import (
    _CHECK_NODES,
    GATE_NAME,
    L2C_MIGRATION_FILE,
    MANIFEST_SCHEMA_FILE,
    assemble_split_result,
)
from minos_engine.qualification.provenance import read_provenance
from minos_engine.qualification.pytest_accounting import PytestAccounting
from minos_engine.qualification.runner import SourceIntegrity
from tests.conftest import REPO_ROOT
from tests.layer2c_synth import synthetic_inventory, synthetic_manifest

_H = "a" * 64


def _passmap() -> dict[str, bool]:
    out: dict[str, bool] = {}
    for nodes in _CHECK_NODES.values():
        for n in nodes:
            out[n if "::" in n else f"{n}::case1"] = True
    return out


def _source_integrity() -> SourceIntegrity:
    ev = (
        EvidenceItem(description="mig", path=L2C_MIGRATION_FILE, kind=EvidenceKind.FILE, sha256=_H),
        EvidenceItem(
            description="schema", path=MANIFEST_SCHEMA_FILE, kind=EvidenceKind.FILE, sha256=_H
        ),
        EvidenceItem(
            description="split",
            path="src/minos_engine/layer2/split",
            kind=EvidenceKind.DIRECTORY,
            sha256=_H,
        ),
    )
    return SourceIntegrity(
        evidence=ev,
        evidence_hashes_complete=True,
        required_source_tracked=True,
        worktree_matches_head=True,
        spec_manifest_hash=_H,
    )


def test_assemble_produces_gate_with_all_required_checks_and_bindings():
    result = assemble_split_result(
        REPO_ROOT,
        manifest=synthetic_manifest(),
        inventory=synthetic_inventory(),
        passmap=_passmap(),
        accounting=PytestAccounting(
            collected=10,
            passed=10,
            failed=0,
            errors=0,
            skipped=0,
            duration_seconds=1.0,
            exit_code=0,
        ),
        coverage=CoverageResult(
            line_coverage_percent=95.0, covered_lines=95, valid_lines=100, missing_lines=5, tool="c"
        ),
        tools={"ruff": True, "format": True, "mypy": True},
        provenance=read_provenance(REPO_ROOT),
        source_integrity=_source_integrity(),
        created_at="2026-08-18T00:00:00+00:00",
    )
    gate = result.gate
    assert gate.gate_name == GATE_NAME
    # every registered required check is present in the assembled mandatory set.
    assert required_checks_for(GATE_NAME) <= set(gate.mandatory_checks)
    # manifest-derived bindings (git-independent) are true.
    for k in (
        "manifest_verified",
        "canonical_manifest_hash_bound",
        "dataset_registry_hash_bound",
        "split_policy_hash_bound",
        "parameter_space_hash_bound",
        "feature_registry_hash_bound",
        "total_sample_count_75",
        "partition_totals_50_10_15",
        "per_chromosome_10_2_3",
        "truth_mutation_isolation_ok",
    ):
        assert gate.mandatory_checks[k] is True, k
    # the gate binds the accepted DB-READY identity + counts.
    ih = gate.input_hashes
    assert ih["accepted_db_ready_gate_hash"].startswith("259986a0")
    assert ih["count_train"] == "50"
    assert ih["count_validation"] == "10"
    assert ih["count_test"] == "15"
    assert ih["feature_registry_hash"].startswith("0d861270")
