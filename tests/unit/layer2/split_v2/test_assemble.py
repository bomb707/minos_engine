"""Cover the SPLIT-FROZEN-V2 assemble path with synthetic inputs (no subprocess/PG).

Drives ``assemble_split_v2_result`` against the real repository root — whose git history
carries the accepted v1 SPLIT-FROZEN source/evidence commits, so the supersede-ancestry
closure resolves — with a synthetic ``SourceIntegrity`` and passmap. Proves the assembled
gate is PASS, contains every registered required check, and binds the epoch-1 identities.
"""

from __future__ import annotations

import json

from minos_engine.gates.contracts import EvidenceItem, EvidenceKind, GateStatus
from minos_engine.gates.required_checks import required_checks_for
from minos_engine.gates.verifier import write_gate
from minos_engine.layer2.split_v2.generator import epoch1_from_v1_manifest
from minos_engine.qualification.coverage import CoverageResult
from minos_engine.qualification.layer2_split_v2_runner import (
    _CHECK_NODES,
    GATE_NAME,
    MANIFEST_SCHEMA_FILE,
    SPLIT_V2_PACKAGE_DIR,
    V2_MIGRATION_FILE,
    assemble_split_v2_result,
    verify_split_frozen_v2_gate,
    write_split_v2_outputs,
)
from minos_engine.qualification.provenance import read_provenance
from minos_engine.qualification.pytest_accounting import PytestAccounting
from minos_engine.qualification.runner import SourceIntegrity
from tests.conftest import REPO_ROOT

_H = "a" * 64


def _passmap() -> dict[str, bool]:
    out: dict[str, bool] = {}
    for nodes in _CHECK_NODES.values():
        for n in nodes:
            out[n if "::" in n else f"{n}::case1"] = True
    return out


def _source_integrity() -> SourceIntegrity:
    ev = (
        EvidenceItem(description="mig", path=V2_MIGRATION_FILE, kind=EvidenceKind.FILE, sha256=_H),
        EvidenceItem(
            description="schema", path=MANIFEST_SCHEMA_FILE, kind=EvidenceKind.FILE, sha256=_H
        ),
        EvidenceItem(
            description="pkg", path=SPLIT_V2_PACKAGE_DIR, kind=EvidenceKind.DIRECTORY, sha256=_H
        ),
    )
    return SourceIntegrity(
        evidence=ev,
        evidence_hashes_complete=True,
        required_source_tracked=True,
        worktree_matches_head=True,
        spec_manifest_hash=_H,
    )


def _epoch_manifest() -> dict:
    v1 = json.loads(
        (REPO_ROOT / "manifests/layer2_dataset_split_v1.json").read_text(encoding="utf-8")
    )
    return epoch1_from_v1_manifest(v1)


def _assemble():
    return assemble_split_v2_result(
        REPO_ROOT,
        epoch_manifest=_epoch_manifest(),
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


def test_assemble_produces_pass_gate_with_all_required_checks() -> None:
    result = _assemble()
    gate = result.gate
    assert gate.gate_name == GATE_NAME
    assert gate.status is GateStatus.PASS, [k for k, v in gate.mandatory_checks.items() if not v]
    # every registered required check is present AND true in the assembled mandatory set.
    assert required_checks_for(GATE_NAME) <= set(gate.mandatory_checks)
    assert all(gate.mandatory_checks[c] for c in required_checks_for(GATE_NAME))


def test_assemble_binds_epoch1_identities() -> None:
    result = _assemble()
    gate = result.gate
    for k in (
        "epoch_manifest_verified",
        "canonical_epoch_manifest_hash_bound",
        "epoch1_inherits_v1_partitions_exactly",
        "epoch1_zero_transitions",
        "epoch1_test_cohort_preserved",
        "epoch1_validation_cohort_preserved",
        "epoch1_parent_fields_null",
        "ancestor_v1_registry_bound",
        "registry_snapshot_hash_bound",
        "split_policy_hash_bound",
        "epoch1_is_first_epoch",
        "total_sample_count_75",
        "partition_totals_50_10_15",
        "per_chromosome_10_2_3",
        "truth_mutation_isolation_ok",
        "v2_source_descends_split_frozen",
        "accepted_split_frozen_unchanged",
        "ci_asserts_head_0003",
    ):
        assert gate.mandatory_checks[k] is True, k
    ih = gate.input_hashes
    assert ih["accepted_split_frozen_gate_hash"].startswith("5520328868")
    assert ih["count_train"] == "50"
    assert ih["count_validation"] == "10"
    assert ih["count_test"] == "15"
    assert ih["epoch"] == "1"
    assert ih["parent_epoch"] == "None"
    assert ih["transition_count"] == "0"
    assert ih["inherited_count"] == "75"
    assert ih["new_count"] == "0"


def test_ci_asserts_head_0003_on_repo_and_fails_closed(tmp_path) -> None:
    from minos_engine.qualification.layer2_split_v2_runner import ci_asserts_head_0003

    assert ci_asserts_head_0003(REPO_ROOT) is True
    # a workflow missing the v2 lifecycle tokens fails closed
    wf = tmp_path / ".github" / "workflows" / "ci.yml"
    wf.parent.mkdir(parents=True)
    wf.write_text("alembic upgrade head\nalembic current | grep 0002\n", encoding="utf-8")
    assert ci_asserts_head_0003(tmp_path) is False
    # and a missing workflow file fails closed
    assert ci_asserts_head_0003(tmp_path / "nowhere") is False


def test_verify_fails_closed_before_source_committed(tmp_path) -> None:
    """verify re-reads committed evidence from git; before the source commit exists it must
    fail closed while still resolving the git-independent bindings correctly."""
    result = _assemble()
    gate = result.gate.model_copy(update={"gate_hash": result.gate.compute_hash()})
    gate_path = write_gate(gate, tmp_path / "gates" / "split-frozen-v2.json")

    v = verify_split_frozen_v2_gate(REPO_ROOT, gate_path, require_descends=False)
    # Source/evidence are not committed at HEAD yet -> overall verification fails closed.
    assert v.ok is False
    assert v.checks  # the verifier produced a full checks map (did not crash)
    # git-independent bindings are nonetheless resolved correctly.
    for k in (
        "gate_name_split_frozen_v2",
        "split_policy_hash_bound",
        "accepted_split_frozen_unchanged",
        "v2_migration_immutable",
        "split_frozen_source_present",
        "split_frozen_evidence_present",
        "v2_source_descends_split_frozen",
    ):
        assert v.checks[k] is True, k


def test_write_outputs_roundtrips(tmp_path) -> None:
    """write_split_v2_outputs writes the manifest, report, and gate to disk."""
    result = _assemble()
    gate_path, manifest_path, report_path = write_split_v2_outputs(result, tmp_path)
    assert gate_path.exists() and manifest_path.exists() and report_path.exists()
    written = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert written["counts"] == {"train": 50, "validation": 10, "test": 15}
    # the report deliberately omits the gate/report/payload hashes (non-circular).
    report = report_path.read_text(encoding="utf-8")
    assert "SPLIT-FROZEN-V2" in report
    assert result.gate.input_hashes["qualification_report_hash"] not in report
