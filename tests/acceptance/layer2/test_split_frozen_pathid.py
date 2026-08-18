"""Real-Git committed-blob attack on inventory operational paths.

Unlike the temporary-gate tamper tests (which mutate a throwaway gate while the verifier
keeps reading the unchanged inventory blob from HEAD), these clone the repository and
commit a *tampered descendant* in which the committed inventory blob itself is changed to
a still-safe relative path, with every dependent hash recomputed consistently: embedded
inventory hash, committed inventory SHA, gate ``local_input_inventory_hash``,
``evidence_payload_hash``, and the canonical gate hash. Dataset id, round id, chromosome,
manifest, source evidence, and ancestry all remain valid.

The verifier must reject the descendant SPECIFICALLY because
``inventory_paths_identity_bound`` is false — the stored path no longer equals the path
derived from the entry's manifest-bound identity — while every other inventory binding
still passes.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from minos_engine.common.hashing import sha256_hex
from minos_engine.gates.contracts import GateArtifact
from minos_engine.gates.verifier import write_gate
from minos_engine.layer2.split.contracts import LocalInputEntry, LocalInputInventory
from minos_engine.qualification.layer2_split_runner import (
    FINAL_REPORT_PATH,
    INVENTORY_PATH,
    MANIFEST_PATH,
    evidence_payload_hash,
    inventory_bytes,
    verify_split_frozen_gate,
)
from tests.conftest import REPO_ROOT

_GATE = REPO_ROOT / "gates" / "split-frozen.json"
needs_gate = pytest.mark.skipif(not _GATE.exists(), reason="gate produced in the evidence commit")

# Still-safe, still-relative, leakage-free substitutions that nonetheless differ from the
# path derived from the entry's manifest-bound identity.
_SUBSTITUTIONS = {
    "bam_relpath": "practice/round_alt/input.bam",
    "bai_relpath": "practice/round_alt/input.bam.bai",
    "reference_relpath": "reference/chrOther/chrOther.fa",
    "fai_relpath": "reference/chrOther/chrOther.fa.fai",
}


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(root), *args], capture_output=True, text=True, check=True
    ).stdout.strip()


def _clone(tmp_path: Path) -> Path:
    dst = tmp_path / "clone"
    subprocess.run(["git", "clone", "--quiet", "--local", str(REPO_ROOT), str(dst)], check=True)
    _git(dst, "config", "user.email", "t@e.com")
    _git(dst, "config", "user.name", "t")
    return dst


def _commit_blob_tamper(dst: Path, field: str, value: str) -> None:
    # 1-3. change one committed inventory path; recompute embedded hash + committed bytes.
    inv_path = dst / INVENTORY_PATH
    inv_raw = json.loads(inv_path.read_text(encoding="utf-8"))
    inv_raw["entries"][0][field] = value
    entries = tuple(LocalInputEntry(**e) for e in inv_raw["entries"])
    model = LocalInputInventory(entries=entries)  # recomputes embedded inventory_hash
    new_bytes = inventory_bytes(model)
    inv_path.write_bytes(new_bytes)
    new_inv_sha = sha256_hex(new_bytes)

    # 4-6. update gate inventory hash + committed SHA + payload + canonical gate hash.
    gate_path = dst / "gates" / "split-frozen.json"
    g = json.loads(gate_path.read_text(encoding="utf-8"))
    ih = g["input_hashes"]
    ih["local_input_inventory_hash"] = model.inventory_hash
    ih["committed_inventory_sha256"] = new_inv_sha
    ih["evidence_payload_hash"] = evidence_payload_hash(
        [
            (MANIFEST_PATH, "file", ih["committed_manifest_sha256"]),
            (INVENTORY_PATH, "file", new_inv_sha),
            (FINAL_REPORT_PATH, "file", ih["qualification_report_hash"]),
        ]
    )
    g["gate_hash"] = ""  # recompute the canonical gate hash on validation
    write_gate(GateArtifact.model_validate(g), gate_path)

    # 7. commit the tampered descendant (ancestry to Commit Y stays valid).
    _git(dst, "add", "-A")
    _git(dst, "commit", "--quiet", "-m", f"tamper {field}")


@needs_gate
@pytest.mark.parametrize("field,value", sorted(_SUBSTITUTIONS.items()))
def test_committed_inventory_path_substitution_rejected(tmp_path, field, value):
    dst = _clone(tmp_path)
    _commit_blob_tamper(dst, field, value)
    result = verify_split_frozen_gate(
        dst, dst / "gates" / "split-frozen.json", require_descends=True
    )

    assert not result.ok, "consistently re-hashed committed-blob substitution must be rejected"
    # rejected SPECIFICALLY by the manifest-derived path identity...
    assert result.checks["inventory_paths_identity_bound"] is False
    # ...while every other inventory + payload binding still passes (fully consistent).
    assert result.checks["inventory_contract_valid"] is True
    assert result.checks["inventory_hash_recomputed"] is True
    assert result.checks["committed_inventory_bytes_bound"] is True
    assert result.checks["inventory_manifest_identity_bound"] is True
    assert result.checks["inventory_paths_safe"] is True
    assert result.checks["evidence_payload_hash_bound"] is True
    assert result.checks["canonical_integrity"] is True
    assert any("inventory_paths_identity_bound" in r for r in result.reasons)


@needs_gate
def test_untampered_clone_still_verifies(tmp_path):
    # Control: the same clone/verify path accepts the genuine committed inventory.
    dst = _clone(tmp_path)
    result = verify_split_frozen_gate(
        dst, dst / "gates" / "split-frozen.json", require_descends=True
    )
    assert result.ok, result.reasons
    assert result.checks["inventory_paths_identity_bound"] is True
