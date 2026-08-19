"""INGEST-READY gate: required checks, SPLIT-FROZEN-V2 closure ancestry, verify skeleton."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from minos_engine.gates.required_checks import required_checks_for
from minos_engine.layer2 import prerequisites as PRE
from minos_engine.qualification.layer2_ingest_runner import (
    GATE_NAME,
    ci_asserts_head_0004,
    l2d_migration_immutable,
    profile_snapshot_gate_name,
    split_frozen_v2_closure_checks,
    verify_ingest_ready_gate,
)
from tests.conftest import REPO_ROOT

_GATE = REPO_ROOT / "gates" / "ingest-ready.json"


def test_ingest_ready_required_checks_registered() -> None:
    required = required_checks_for(GATE_NAME)
    for check in (
        "accepted_split_frozen_v2_unchanged",
        "split_frozen_v2_source_present",
        "split_frozen_v2_evidence_present",
        "l2d_source_descends_split_frozen_v2",
        "head_descends_l2d_source",
        "l2d_migration_immutable",
        "l2d_migration_file_evidence_bound",
        "l2d_migration_contract_bound",
        "alembic_single_head_is_l2d",
        "ci_asserts_head_0004",
        "sealed_test_profile_access_denied",
        "legacy_profiles_reads_revoked",
        "profile_snapshot_freeze_passed",
        "service_still_blocked",
    ):
        assert check in required, check


def test_profile_snapshot_gate_names_are_per_epoch() -> None:
    assert profile_snapshot_gate_name(1) == "PROFILE-SNAPSHOT-FROZEN-1"
    assert profile_snapshot_gate_name(7) == "PROFILE-SNAPSHOT-FROZEN-7"


def test_pinned_v2_identities_are_accepted() -> None:
    assert PRE.SPLIT_FROZEN_V2_GATE_HASH.startswith("6bd9f472")
    assert PRE.SPLIT_FROZEN_V2_SOURCE_COMMIT == "8c641dd1363573ab685df49540561cfe818de17c"
    assert PRE.SPLIT_FROZEN_V2_EVIDENCE_COMMIT == "a8940ac44eef72cbcbdc8f943a163e33f3a3b742"


def test_repo_state_capability_checks() -> None:
    assert l2d_migration_immutable(REPO_ROOT) is True
    assert ci_asserts_head_0004(REPO_ROOT) is True


# --------------------------------------------------------------------------- #
# SPLIT-FROZEN-V2 closure ancestry (synthetic git graphs; pinned ids patched)
# --------------------------------------------------------------------------- #
def _g(root: Path, *a: str) -> str:
    return subprocess.run(
        ["git", "-C", str(root), *a], capture_output=True, text=True, check=True
    ).stdout.strip()


def _commit(root: Path, name: str) -> str:
    (root / name).write_text(name, encoding="utf-8")
    _g(root, "add", "-A")
    _g(root, "commit", "-q", "-m", name)
    return _g(root, "rev-parse", "HEAD")


def _linear(root: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[str, str, str]:
    root.mkdir(parents=True, exist_ok=True)
    _g(root, "init", "-q")
    _g(root, "config", "user.email", "t@e.com")
    _g(root, "config", "user.name", "t")
    src = _commit(root, "v2_src")
    evi = _commit(root, "v2_evi")
    l2d = _commit(root, "l2d_src")
    monkeypatch.setattr(PRE, "SPLIT_FROZEN_V2_SOURCE_COMMIT", src)
    monkeypatch.setattr(
        PRE, "SPLIT_FROZEN_V2_SOURCE_TREE", _g(root, "rev-parse", f"{src}^{{tree}}")
    )
    monkeypatch.setattr(PRE, "SPLIT_FROZEN_V2_EVIDENCE_COMMIT", evi)
    return src, evi, l2d


def test_closure_valid_chain_passes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "r"
    _src, _evi, l2d = _linear(root, monkeypatch)
    head = _commit(root, "later")
    c = split_frozen_v2_closure_checks(root, l2d_source_ref=l2d, head_ref=head)
    assert all(c.values()), c


def test_sibling_source_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "r"
    src, _evi, _l2d = _linear(root, monkeypatch)
    _g(root, "checkout", "-q", "-b", "sib", src)
    sib = _commit(root, "sibling")
    c = split_frozen_v2_closure_checks(root, l2d_source_ref=sib)
    assert c["l2d_source_descends_split_frozen_v2"] is False


def test_evidence_itself_fails_as_source(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "r"
    _src, evi, _l2d = _linear(root, monkeypatch)
    c = split_frozen_v2_closure_checks(root, l2d_source_ref=evi)
    assert c["l2d_source_descends_split_frozen_v2"] is False  # proper descent required


@pytest.mark.skipif(not _GATE.exists(), reason="INGEST-READY gate produced in evidence commit")
def test_committed_ingest_gate_verifies() -> None:
    result = verify_ingest_ready_gate(REPO_ROOT, _GATE, require_descends=True)
    assert result.ok, result.reasons
