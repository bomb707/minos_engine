"""DB-READY gate: required checks, assembly status logic, and tamper verification."""

from __future__ import annotations

import json
import subprocess

import pytest

from minos_engine.gates.contracts import EvidenceItem, EvidenceKind, GateArtifact, GateStatus
from minos_engine.gates.required_checks import required_checks_for
from minos_engine.gates.verifier import write_gate
from minos_engine.qualification.coverage import CoverageResult
from minos_engine.qualification.layer2_db_runner import (
    _CHECK_NODES,
    ALEMBIC_HEAD,
    GATE_NAME,
    MIGRATION_FILE,
    alembic_head,
    assemble_db_result,
    l2b_revision_is_immutable_base,
    verify_db_ready_gate,
)
from minos_engine.qualification.provenance import read_provenance
from minos_engine.qualification.pytest_accounting import PytestAccounting
from minos_engine.qualification.runner import SourceIntegrity
from tests.conftest import REPO_ROOT

_GATE = REPO_ROOT / "gates" / "db-ready.json"
_H = "a" * 64


def _passmap(all_pass: bool = True) -> dict[str, bool]:
    out: dict[str, bool] = {}
    for nodes in _CHECK_NODES.values():
        for n in nodes:
            key = n if "::" in n else f"{n}::case1"
            out[key] = all_pass
    return out


def _synthetic_source_integrity() -> SourceIntegrity:
    return SourceIntegrity(
        evidence=(
            EvidenceItem(
                description="migration", path=MIGRATION_FILE, kind=EvidenceKind.FILE, sha256=_H
            ),
        ),
        evidence_hashes_complete=True,
        required_source_tracked=True,
        worktree_matches_head=True,
        spec_manifest_hash=_H,
    )


def _assemble(passmap: dict[str, bool]):
    return assemble_db_result(
        REPO_ROOT,
        passmap=passmap,
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
        source_integrity=_synthetic_source_integrity(),
        created_at="2026-08-17T00:00:00+00:00",
    )


# --------------------------------------------------------------------------- #
# Static structure
# --------------------------------------------------------------------------- #
def test_db_ready_required_checks_registered():
    required = required_checks_for("DB-READY")
    assert required, "DB-READY must have a registered required-check set"
    for check in (
        "l2a_entry_passed",
        "seven_schemas_created",
        "five_roles_created",
        "append_only_passed",
        "least_privilege_passed",
        "live_evaluation_denied",
        "worker_claim_concurrency_passed",
        "service_still_blocked",
        "split_manifest_absent",
        "accepted_prerequisites_unchanged",
        "migration_immutable",
        "migration_contract_frozen",
        "migration_file_evidence_bound",
        "migration_contract_bound",
        "admin_ownership_passed",
        "l2a_closure_source_bound",
        "l2a_closure_source_tree_bound",
        "l2a_closure_evidence_bound",
        "l2a_closure_evidence_tree_bound",
        "l2a_closure_chain_valid",
        "l2b_qualified_source_present",
        "l2b_qualified_source_tree_bound",
        "l2b_qualified_source_descends_l2a",
        "head_descends_l2b_qualified_source",
        "accepted_feature_registry_hash_bound",
    ):
        assert check in required


def test_l2b_revision_is_immutable_base():
    # After L2-C adds migration 0002, the live head advances past 0001_l2b_initial, but
    # the DB-READY-bound L2-B revision must remain the immutable base of the lineage and
    # an ancestor of the current head (forward-migration compatibility, fail-closed).
    assert ALEMBIC_HEAD == "0001_l2b_initial"
    assert l2b_revision_is_immutable_base(ALEMBIC_HEAD) is True
    assert l2b_revision_is_immutable_base("deadbeef_not_a_revision") is False
    assert alembic_head() != ""  # a single concrete head exists


# --------------------------------------------------------------------------- #
# Assembly status logic (no PostgreSQL required)
# --------------------------------------------------------------------------- #
def test_assemble_pass_when_all_checks_true(monkeypatch):
    # L2-C legitimately adds the split schema/manifest to the working tree, so a fresh
    # DB-READY assembly would now see split_manifest_absent=False. Isolate that
    # working-tree-dependent check to exercise the pure assemble PASS logic. (The
    # committed DB-READY gate froze split_manifest_absent=True and is unaffected.)
    monkeypatch.setattr(
        "minos_engine.qualification.layer2_db_runner._split_manifest_absent", lambda root: True
    )
    result = _assemble(_passmap(all_pass=True))
    assert result.gate.status is GateStatus.PASS
    assert required_checks_for(GATE_NAME) <= set(result.gate.mandatory_checks)
    # the gate binds the storage/role/alembic + final L2-A closure identities
    ih = result.gate.input_hashes
    # a freshly-assembled gate binds the live Alembic head (0002+ once L2-C is present);
    # the committed DB-READY gate keeps its frozen 0001 binding, still the lineage base.
    assert ih["alembic_head_revision"] == alembic_head()
    assert ih["postgres_major_version"] == "16"
    assert ih["accepted_l1_ready_gate_hash"].startswith("aeabfea8")
    assert ih["accepted_l2a_closure_source_commit"] == "c2ceed0cd8566442ca229eaa41d9a096c0b4ccea"
    assert ih["accepted_l2a_closure_source_tree"] == "e581ff76223210895ceff1521dabba751de72f9a"
    assert ih["accepted_l2a_closure_evidence_commit"] == "70d08daa7a5fce76ca347e1635507757ef792c88"
    assert ih["accepted_l2a_closure_evidence_tree"] == "eadb6d6f19d99a1f20de7bfb231771d832dc6114"
    assert (
        ih["accepted_feature_registry_hash"]
        == "0d8612707c6673060546511d8f5e8d1ba47048ef440e6c2dcf238fdc297f6e0c"
    )
    assert result.gate.mandatory_checks["migration_immutable"] is True
    assert result.gate.mandatory_checks["l2a_closure_chain_valid"] is True
    assert result.gate.mandatory_checks["l2b_qualified_source_present"] is True
    assert result.gate.mandatory_checks["l2b_qualified_source_descends_l2a"] is True
    assert result.gate.mandatory_checks["head_descends_l2b_qualified_source"] is True
    assert result.gate.mandatory_checks["migration_file_evidence_bound"] is True
    assert result.gate.mandatory_checks["migration_contract_bound"] is True
    assert result.gate.mandatory_checks["accepted_feature_registry_hash_bound"] is True


def test_assemble_holds_when_a_behavior_check_fails(monkeypatch):
    monkeypatch.setattr(
        "minos_engine.qualification.layer2_db_runner._split_manifest_absent", lambda root: True
    )
    pm = _passmap(all_pass=True)
    pm["test_worker_claim.py::case1"] = False
    result = _assemble(pm)
    assert result.gate.status is GateStatus.HOLD
    assert result.gate.mandatory_checks["worker_claim_concurrency_passed"] is False


# --------------------------------------------------------------------------- #
# Committed-gate verification + tamper (skips until Commit P adds the gate)
# --------------------------------------------------------------------------- #
@pytest.mark.skipif(not _GATE.exists(), reason="DB-READY gate produced in Commit P")
def test_committed_db_gate_verifies():
    result = verify_db_ready_gate(REPO_ROOT, _GATE, require_descends=True)
    assert result.ok, result.reasons


def _tamper(tmp_path, mutate) -> str:
    raw = json.loads(_GATE.read_text(encoding="utf-8"))
    mutate(raw)
    raw["gate_hash"] = ""
    gate = GateArtifact.model_validate(raw)
    path = tmp_path / "gates" / "db-ready.json"
    write_gate(gate, path)
    return str(path)


@pytest.mark.skipif(not _GATE.exists(), reason="DB-READY gate produced in Commit P")
def test_tampered_storage_hash_rejected(tmp_path):
    def mut(raw):
        raw["input_hashes"] = dict(raw["input_hashes"], storage_schema_hash="9" * 64)

    from pathlib import Path

    result = verify_db_ready_gate(REPO_ROOT, Path(_tamper(tmp_path, mut)), require_descends=False)
    assert not result.ok
    assert any("storage_schema_bound" in r for r in result.reasons)


@pytest.mark.skipif(not _GATE.exists(), reason="DB-READY gate produced in Commit P")
def test_tampered_accepted_prereq_rejected(tmp_path):
    def mut(raw):
        raw["input_hashes"] = dict(raw["input_hashes"], accepted_l1_ready_gate_hash="9" * 64)

    from pathlib import Path

    result = verify_db_ready_gate(REPO_ROOT, Path(_tamper(tmp_path, mut)), require_descends=False)
    assert not result.ok
    assert any("accepted_l1_unchanged" in r for r in result.reasons)


@pytest.mark.skipif(not _GATE.exists(), reason="DB-READY gate produced in Commit P")
def test_wrong_qualification_tool_rejected(tmp_path):
    def mut(raw):
        raw["qualification_tool_version"] = "bogus"

    from pathlib import Path

    result = verify_db_ready_gate(REPO_ROOT, Path(_tamper(tmp_path, mut)), require_descends=False)
    assert not result.ok
    assert any("qualification_tool_identity" in r for r in result.reasons)


# --------------------------------------------------------------------------- #
# Defect 1 — qualified-source ancestry (real git graphs, injected identities)
# --------------------------------------------------------------------------- #
from minos_engine.layer2 import prerequisites as PRE  # noqa: E402
from minos_engine.qualification.layer2_db_runner import l2a_closure_checks  # noqa: E402


def _g(root, *a):
    return subprocess.run(
        ["git", "-C", str(root), *a], capture_output=True, text=True, check=True
    ).stdout.strip()


def _init(root):
    root.mkdir(parents=True, exist_ok=True)
    _g(root, "init", "-q")
    _g(root, "config", "user.email", "t@e.com")
    _g(root, "config", "user.name", "t")


def _commit(root, name):
    (root / name).write_text(name, encoding="utf-8")
    _g(root, "add", "-A")
    _g(root, "commit", "-q", "-m", name)
    return _g(root, "rev-parse", "HEAD")


def _tree(root, sha):
    return _g(root, "rev-parse", f"{sha}^{{tree}}")


def _set_pre(mp, *, src, src_tree, evi, evi_tree):
    mp.setattr(PRE, "L2A_CLOSURE_SOURCE_COMMIT", src)
    mp.setattr(PRE, "L2A_CLOSURE_SOURCE_TREE", src_tree)
    mp.setattr(PRE, "L2A_CLOSURE_EVIDENCE_COMMIT", evi)
    mp.setattr(PRE, "L2A_CLOSURE_EVIDENCE_TREE", evi_tree)


def _linear(root, monkeypatch):
    """L2A_source -> L2A_evidence -> L2B_source (returns the three shas)."""
    _init(root)
    src = _commit(root, "l2a_src")
    evi = _commit(root, "l2a_evi")
    l2b = _commit(root, "l2b_src")
    _set_pre(monkeypatch, src=src, src_tree=_tree(root, src), evi=evi, evi_tree=_tree(root, evi))
    return src, evi, l2b


def test_qualified_source_valid_chain_passes(tmp_path, monkeypatch):
    root = tmp_path / "r"
    src, evi, l2b = _linear(root, monkeypatch)
    head = _commit(root, "later_head")
    c = l2a_closure_checks(
        root, l2b_source_ref=l2b, l2b_source_tree=_tree(root, l2b), head_ref=head
    )
    for k in (
        "l2a_closure_chain_valid",
        "l2b_qualified_source_present",
        "l2b_qualified_source_tree_bound",
        "l2b_qualified_source_descends_l2a",
        "head_descends_l2b_qualified_source",
    ):
        assert c[k] is True, k


def test_missing_qualified_source_fails(tmp_path, monkeypatch):
    root = tmp_path / "r"
    _linear(root, monkeypatch)
    c = l2a_closure_checks(root, l2b_source_ref="0" * 40, l2b_source_tree=None)
    assert c["l2b_qualified_source_present"] is False
    assert c["l2b_qualified_source_descends_l2a"] is False


def test_wrong_qualified_source_tree_fails(tmp_path, monkeypatch):
    root = tmp_path / "r"
    _src, _evi, l2b = _linear(root, monkeypatch)
    c = l2a_closure_checks(root, l2b_source_ref=l2b, l2b_source_tree="0" * 40)
    assert c["l2b_qualified_source_tree_bound"] is False


def test_ancestor_qualified_source_fails(tmp_path, monkeypatch):
    root = tmp_path / "r"
    src, _evi, _l2b = _linear(root, monkeypatch)
    # src predates the L2A evidence -> evidence is not an ancestor of it
    c = l2a_closure_checks(root, l2b_source_ref=src, l2b_source_tree=_tree(root, src))
    assert c["l2b_qualified_source_descends_l2a"] is False


def test_unrelated_orphan_qualified_source_fails(tmp_path, monkeypatch):
    root = tmp_path / "r"
    _linear(root, monkeypatch)
    _g(root, "checkout", "-q", "--orphan", "orphan")
    orph = _commit(root, "orphan_root")
    c = l2a_closure_checks(root, l2b_source_ref=orph, l2b_source_tree=_tree(root, orph))
    assert c["l2b_qualified_source_descends_l2a"] is False


def test_sibling_qualified_source_fails(tmp_path, monkeypatch):
    root = tmp_path / "r"
    src, _evi, _l2b = _linear(root, monkeypatch)
    _g(root, "checkout", "-q", "-b", "sib", src)  # branch off BEFORE the evidence
    sib = _commit(root, "sibling_src")
    c = l2a_closure_checks(root, l2b_source_ref=sib, l2b_source_tree=_tree(root, sib))
    assert c["l2b_qualified_source_descends_l2a"] is False


def test_merge_head_does_not_launder_sibling_source(tmp_path, monkeypatch):
    # MANDATORY: a later merge HEAD that descends BOTH the accepted L2-A evidence and a
    # sibling L2-B source must NOT make that sibling source acceptable.
    root = tmp_path / "r"
    src, evi, _l2b = _linear(root, monkeypatch)
    _g(root, "checkout", "-q", "-b", "sib", src)  # sibling lineage from before evidence
    sib = _commit(root, "sibling_src")
    _g(root, "checkout", "-q", evi)  # detached at the accepted evidence
    _g(root, "merge", "-q", "--no-ff", "-m", "merge_sib", "sib")
    merge_head = _g(root, "rev-parse", "HEAD")
    # the merge HEAD genuinely descends both the evidence AND the sibling source
    assert (
        subprocess.run(
            ["git", "-C", str(root), "merge-base", "--is-ancestor", evi, merge_head]
        ).returncode
        == 0
    )
    assert (
        subprocess.run(
            ["git", "-C", str(root), "merge-base", "--is-ancestor", sib, merge_head]
        ).returncode
        == 0
    )
    c = l2a_closure_checks(
        root, l2b_source_ref=sib, l2b_source_tree=_tree(root, sib), head_ref=merge_head
    )
    assert c["head_descends_l2b_qualified_source"] is True  # HEAD descends the sibling source...
    assert c["l2b_qualified_source_descends_l2a"] is False  # ...but the source is not from L2-A


def test_head_not_descending_qualified_source_fails(tmp_path, monkeypatch):
    root = tmp_path / "r"
    _src, _evi, l2b = _linear(root, monkeypatch)
    _later = _commit(root, "later")
    # point head_ref at the qualified source's parent (evidence), which does not descend it
    c = l2a_closure_checks(
        root,
        l2b_source_ref=l2b,
        l2b_source_tree=_tree(root, l2b),
        head_ref=PRE.L2A_CLOSURE_EVIDENCE_COMMIT,
        require_head_descends=True,
    )
    assert c["head_descends_l2b_qualified_source"] is False


def test_closure_checks_pass_on_real_repo():
    from minos_engine.qualification.provenance import read_provenance

    head = read_provenance(REPO_ROOT).head_sha or "HEAD"
    c = l2a_closure_checks(REPO_ROOT, l2b_source_ref=head)
    assert all(c.values()), c


# --------------------------------------------------------------------------- #
# Defect 3 (retained) — closure binding tamper (committed gate)
# --------------------------------------------------------------------------- #
@pytest.mark.skipif(not _GATE.exists(), reason="DB-READY gate produced in Commit T")
@pytest.mark.parametrize(
    "field,check",
    [
        ("accepted_l2a_closure_source_commit", "l2a_closure_source_bound"),
        ("accepted_l2a_closure_source_tree", "l2a_closure_source_tree_bound"),
        ("accepted_l2a_closure_evidence_commit", "l2a_closure_evidence_bound"),
        ("accepted_l2a_closure_evidence_tree", "l2a_closure_evidence_tree_bound"),
        ("accepted_feature_registry_hash", "accepted_feature_registry_hash_bound"),
    ],
)
def test_tampered_closure_binding_rejected(tmp_path, field, check):
    from pathlib import Path

    def mut(raw):
        raw["input_hashes"] = dict(
            raw["input_hashes"], **{field: "9" * len(raw["input_hashes"][field])}
        )

    result = verify_db_ready_gate(REPO_ROOT, Path(_tamper(tmp_path, mut)), require_descends=False)
    assert not result.ok
    assert any(check in r for r in result.reasons), result.reasons


@pytest.mark.skipif(not _GATE.exists(), reason="DB-READY gate produced in Commit T")
def test_old_owner_commit_substituted_for_closure_rejected(tmp_path):
    from pathlib import Path

    def mut(raw):
        raw["input_hashes"] = dict(
            raw["input_hashes"],
            accepted_l2a_closure_source_commit="f96ea78e0943e33f751afe2eb1709512445e9437",
        )

    result = verify_db_ready_gate(REPO_ROOT, Path(_tamper(tmp_path, mut)), require_descends=False)
    assert not result.ok
    assert any("l2a_closure_source_bound" in r for r in result.reasons)


# --------------------------------------------------------------------------- #
# Defect 2 — migration cross-binding to qualified-source evidence
# --------------------------------------------------------------------------- #
def _find_mig_evidence(raw):
    return next(
        e for e in raw["evidence"] if e["path"] == "migrations/versions/0001_l2b_initial.py"
    )


@pytest.mark.skipif(not _GATE.exists(), reason="DB-READY gate produced in Commit T")
def test_only_migration_file_hash_changed_rejected(tmp_path):
    from pathlib import Path

    def mut(raw):
        raw["input_hashes"] = dict(raw["input_hashes"], migration_file_hash="9" * 64)

    result = verify_db_ready_gate(REPO_ROOT, Path(_tamper(tmp_path, mut)), require_descends=False)
    assert not result.ok
    assert any("migration_file_evidence_bound" in r for r in result.reasons)


@pytest.mark.skipif(not _GATE.exists(), reason="DB-READY gate produced in Commit T")
def test_only_migration_contract_hash_changed_rejected(tmp_path):
    from pathlib import Path

    def mut(raw):
        raw["input_hashes"] = dict(raw["input_hashes"], migration_contract_hash="9" * 64)

    result = verify_db_ready_gate(REPO_ROOT, Path(_tamper(tmp_path, mut)), require_descends=False)
    assert not result.ok
    assert any("migration_contract_bound" in r for r in result.reasons)


@pytest.mark.skipif(not _GATE.exists(), reason="DB-READY gate produced in Commit T")
def test_both_migration_hashes_changed_consistently_rejected(tmp_path):
    # MANDATORY: an attacker sets file_hash=X and contract=contract_hash(X). It must
    # fail because the input migration hash != the independently verified source blob.
    from pathlib import Path

    from minos_engine.storage.migration_contract import contract_hash

    x = "1" * 64

    def mut(raw):
        raw["input_hashes"] = dict(
            raw["input_hashes"], migration_file_hash=x, migration_contract_hash=contract_hash(x)
        )

    result = verify_db_ready_gate(REPO_ROOT, Path(_tamper(tmp_path, mut)), require_descends=False)
    assert not result.ok
    assert any("migration_file_evidence_bound" in r for r in result.reasons), result.reasons


@pytest.mark.skipif(not _GATE.exists(), reason="DB-READY gate produced in Commit T")
def test_migration_evidence_missing_rejected(tmp_path):
    from pathlib import Path

    def mut(raw):
        raw["evidence"] = [
            e for e in raw["evidence"] if e["path"] != "migrations/versions/0001_l2b_initial.py"
        ]

    result = verify_db_ready_gate(REPO_ROOT, Path(_tamper(tmp_path, mut)), require_descends=False)
    assert not result.ok
    assert any("migration_evidence_present" in r for r in result.reasons)


@pytest.mark.skipif(not _GATE.exists(), reason="DB-READY gate produced in Commit T")
def test_migration_evidence_duplicated_rejected(tmp_path):
    from pathlib import Path

    def mut(raw):
        raw["evidence"] = [*raw["evidence"], dict(_find_mig_evidence(raw))]

    result = verify_db_ready_gate(REPO_ROOT, Path(_tamper(tmp_path, mut)), require_descends=False)
    assert not result.ok
    assert any("migration_evidence_present" in r for r in result.reasons)


@pytest.mark.skipif(not _GATE.exists(), reason="DB-READY gate produced in Commit T")
def test_migration_evidence_kind_changed_rejected(tmp_path):
    from pathlib import Path

    def mut(raw):
        for e in raw["evidence"]:
            if e["path"] == "migrations/versions/0001_l2b_initial.py":
                e["kind"] = "directory"

    result = verify_db_ready_gate(REPO_ROOT, Path(_tamper(tmp_path, mut)), require_descends=False)
    assert not result.ok
    assert any("migration_evidence_present" in r for r in result.reasons)


@pytest.mark.skipif(not _GATE.exists(), reason="DB-READY gate produced in Commit T")
def test_migration_evidence_hash_differs_from_blob_rejected(tmp_path):
    # Change the evidence sha AND the matching input hash so file_evidence_bound would
    # pass, but it no longer equals the committed source blob.
    from pathlib import Path

    x = "2" * 64

    def mut(raw):
        for e in raw["evidence"]:
            if e["path"] == "migrations/versions/0001_l2b_initial.py":
                e["sha256"] = x
        raw["input_hashes"] = dict(raw["input_hashes"], migration_file_hash=x)

    result = verify_db_ready_gate(REPO_ROOT, Path(_tamper(tmp_path, mut)), require_descends=False)
    assert not result.ok
    assert any("migration_evidence_matches_source_blob" in r for r in result.reasons) or any(
        "evidence" in r for r in result.reasons
    )


@pytest.mark.skipif(not _GATE.exists(), reason="DB-READY gate produced in Commit T")
def test_verification_uses_source_blob_not_working_tree(tmp_path):
    # Modifying the working-tree migration must not affect verification, which reads the
    # blob from the qualified source commit.

    mig = REPO_ROOT / "migrations" / "versions" / "0001_l2b_initial.py"
    original = mig.read_bytes()
    try:
        mig.write_bytes(original + b"\n# working-tree noise\n")
        result = verify_db_ready_gate(REPO_ROOT, _GATE, require_descends=True)
        assert result.ok, result.reasons
    finally:
        mig.write_bytes(original)
    # Working-tree noise must not surface as a failing binding: the verifier hashed the
    # migration blob from the qualified source commit, not the drifted working-tree file.
    assert not any("migration_contract_bound" in r for r in result.reasons)
    assert not any("migration_file_evidence_bound" in r for r in result.reasons)
