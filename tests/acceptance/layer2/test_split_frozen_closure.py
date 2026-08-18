"""SPLIT-FROZEN closure: new required checks + generator/schema/report/payload tamper.

Registration checks run always. The gate tamper cases are guarded by the committed
gate's existence (produced in the evidence commit, Commit X): each mutates one binding
and asserts the verifier — which independently re-derives from the qualified source
(Commit W) and the current tree — rejects it, even when the mutation is internally
self-consistent.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from minos_engine.gates.contracts import GateArtifact
from minos_engine.gates.required_checks import required_checks_for
from minos_engine.gates.verifier import write_gate
from minos_engine.qualification.layer2_split_runner import (
    FINAL_REPORT_PATH,
    GATE_NAME,
    MANIFEST_SCHEMA_FILE,
    SPLIT_PACKAGE_DIR,
    verify_split_frozen_gate,
)
from tests.conftest import REPO_ROOT

_GATE = REPO_ROOT / "gates" / "split-frozen.json"

_NEW_REQUIRED = (
    "inventory_contract_valid",
    "inventory_hash_recomputed",
    "committed_inventory_bytes_bound",
    "inventory_manifest_identity_bound",
    "inventory_paths_safe",
    "inventory_truth_mutation_isolation_ok",
    "generator_source_evidence_present",
    "generator_source_evidence_matches_source",
    "generator_source_evidence_bound",
    "manifest_schema_evidence_present",
    "manifest_schema_evidence_matches_source",
    "manifest_schema_evidence_bound",
    "qualification_report_bytes_bound",
    "evidence_payload_paths_exact",
    "evidence_payload_hash_bound",
)


def test_closure_required_checks_registered():
    required = required_checks_for(GATE_NAME)
    for check in _NEW_REQUIRED:
        assert check in required, check


def _tamper(tmp_path: Path, mutate) -> Path:
    raw = json.loads(_GATE.read_text(encoding="utf-8"))
    mutate(raw)
    raw["gate_hash"] = ""
    gate = GateArtifact.model_validate(raw)
    path = tmp_path / "gates" / "split-frozen.json"
    write_gate(gate, path)
    return path


def _verify(tmp_path: Path, mutate):
    return verify_split_frozen_gate(REPO_ROOT, _tamper(tmp_path, mutate), require_descends=False)


needs_gate = pytest.mark.skipif(not _GATE.exists(), reason="gate produced in Commit X")


# --------------------------------------------------------------------------- #
# Baseline
# --------------------------------------------------------------------------- #
@needs_gate
def test_committed_gate_verifies_all_new_checks():
    result = verify_split_frozen_gate(REPO_ROOT, _GATE, require_descends=True)
    assert result.ok, result.reasons
    for check in _NEW_REQUIRED:
        assert result.checks.get(check) is True, check


# --------------------------------------------------------------------------- #
# Generator (split package) directory evidence
# --------------------------------------------------------------------------- #
@needs_gate
def test_generator_input_hash_changed_rejected(tmp_path):
    result = _verify(
        tmp_path, lambda r: r["input_hashes"].__setitem__("generator_source_hash", "0" * 64)
    )
    assert not result.ok
    assert any("generator_source_evidence_bound" in x for x in result.reasons)


@needs_gate
def test_generator_evidence_and_input_changed_consistently_rejected(tmp_path):
    x = "1" * 64

    def mut(r):
        for e in r["evidence"]:
            if e["path"] == SPLIT_PACKAGE_DIR:
                e["sha256"] = x
        r["input_hashes"]["generator_source_hash"] = x

    result = _verify(tmp_path, mut)
    assert not result.ok
    # re-derivation from Commit W rejects it even though input==evidence.
    assert any("generator_source_evidence_matches_source" in x for x in result.reasons)


@needs_gate
def test_generator_evidence_missing_rejected(tmp_path):
    def mut(r):
        r["evidence"] = [e for e in r["evidence"] if e["path"] != SPLIT_PACKAGE_DIR]

    result = _verify(tmp_path, mut)
    assert not result.ok
    assert any("generator_source_evidence_present" in x for x in result.reasons)


@needs_gate
def test_generator_evidence_duplicated_rejected(tmp_path):
    def mut(r):
        item = next(e for e in r["evidence"] if e["path"] == SPLIT_PACKAGE_DIR)
        r["evidence"].append(dict(item))

    result = _verify(tmp_path, mut)
    assert not result.ok
    assert any("generator_source_evidence_present" in x for x in result.reasons)


@needs_gate
def test_generator_evidence_wrong_kind_rejected(tmp_path):
    def mut(r):
        for e in r["evidence"]:
            if e["path"] == SPLIT_PACKAGE_DIR:
                e["kind"] = "file"

    result = _verify(tmp_path, mut)
    assert not result.ok
    assert any("generator_source_evidence_present" in x for x in result.reasons)


# --------------------------------------------------------------------------- #
# Manifest schema file evidence
# --------------------------------------------------------------------------- #
@needs_gate
def test_schema_input_hash_changed_rejected(tmp_path):
    result = _verify(
        tmp_path, lambda r: r["input_hashes"].__setitem__("manifest_schema_hash", "0" * 64)
    )
    assert not result.ok
    assert any("manifest_schema_evidence_bound" in x for x in result.reasons)


@needs_gate
def test_schema_evidence_and_input_changed_consistently_rejected(tmp_path):
    x = "2" * 64

    def mut(r):
        for e in r["evidence"]:
            if e["path"] == MANIFEST_SCHEMA_FILE:
                e["sha256"] = x
        r["input_hashes"]["manifest_schema_hash"] = x

    result = _verify(tmp_path, mut)
    assert not result.ok
    assert any("manifest_schema_evidence_matches_source" in x for x in result.reasons)


@needs_gate
def test_schema_evidence_missing_rejected(tmp_path):
    def mut(r):
        r["evidence"] = [e for e in r["evidence"] if e["path"] != MANIFEST_SCHEMA_FILE]

    result = _verify(tmp_path, mut)
    assert not result.ok
    assert any("manifest_schema_evidence_present" in x for x in result.reasons)


# --------------------------------------------------------------------------- #
# Final report bytes + evidence payload
# --------------------------------------------------------------------------- #
@needs_gate
def test_report_hash_changed_rejected(tmp_path):
    result = _verify(
        tmp_path, lambda r: r["input_hashes"].__setitem__("qualification_report_hash", "0" * 64)
    )
    assert not result.ok
    assert any("qualification_report_bytes_bound" in x for x in result.reasons)


@needs_gate
def test_payload_hash_changed_rejected(tmp_path):
    result = _verify(
        tmp_path, lambda r: r["input_hashes"].__setitem__("evidence_payload_hash", "0" * 64)
    )
    assert not result.ok
    assert any("evidence_payload_hash_bound" in x for x in result.reasons)


@needs_gate
def test_report_and_payload_changed_consistently_rejected(tmp_path):
    # Re-point the report hash and recompute the aggregate over the tampered value; the
    # verifier recomputes both from committed bytes and rejects the pair.
    from minos_engine.qualification.layer2_split_runner import (
        EVIDENCE_PAYLOAD,
        evidence_payload_hash,
    )

    fake_report = "0" * 64

    def mut(r):
        ih = r["input_hashes"]
        ih["qualification_report_hash"] = fake_report
        items = []
        for path, kind in EVIDENCE_PAYLOAD:
            if path == FINAL_REPORT_PATH:
                items.append((path, kind.value, fake_report))
            elif path.endswith("dataset_split_v1.json"):
                items.append((path, kind.value, ih["committed_manifest_sha256"]))
            else:
                items.append((path, kind.value, ih["committed_inventory_sha256"]))
        ih["evidence_payload_hash"] = evidence_payload_hash(items)

    result = _verify(tmp_path, mut)
    assert not result.ok
    assert any("qualification_report_bytes_bound" in x for x in result.reasons)


@needs_gate
def test_payload_paths_extra_path_rejected(tmp_path):
    def mut(r):
        paths = json.loads(r["input_hashes"]["evidence_payload_paths"])
        paths.append("reports/LAYER2_L2C_SPLIT_FROZEN_REPORT.md")
        r["input_hashes"]["evidence_payload_paths"] = json.dumps(sorted(paths))

    result = _verify(tmp_path, mut)
    assert not result.ok
    assert any("evidence_payload_paths_exact" in x for x in result.reasons)


@needs_gate
def test_payload_paths_missing_path_rejected(tmp_path):
    def mut(r):
        paths = [
            p for p in json.loads(r["input_hashes"]["evidence_payload_paths"]) if "report" not in p
        ]
        r["input_hashes"]["evidence_payload_paths"] = json.dumps(paths)

    result = _verify(tmp_path, mut)
    assert not result.ok
    assert any("evidence_payload_paths_exact" in x for x in result.reasons)


@needs_gate
def test_payload_including_gate_itself_rejected(tmp_path):
    def mut(r):
        paths = json.loads(r["input_hashes"]["evidence_payload_paths"])
        paths.append("gates/split-frozen.json")
        r["input_hashes"]["evidence_payload_paths"] = json.dumps(sorted(paths))

    result = _verify(tmp_path, mut)
    assert not result.ok
    assert any("evidence_payload_paths_exact" in x for x in result.reasons)


@needs_gate
def test_tampered_manifest_bytes_via_payload_change_rejected(tmp_path):
    # Changing the manifest committed-bytes binding (without touching the frozen file)
    # must break both the manifest-bytes binding and the aggregate payload.
    result = _verify(
        tmp_path, lambda r: r["input_hashes"].__setitem__("committed_manifest_sha256", "0" * 64)
    )
    assert not result.ok
    assert any("committed_manifest_bytes_bound" in x for x in result.reasons)


@needs_gate
def test_tampered_inventory_bytes_binding_rejected(tmp_path):
    result = _verify(
        tmp_path, lambda r: r["input_hashes"].__setitem__("committed_inventory_sha256", "0" * 64)
    )
    assert not result.ok
    assert any("committed_inventory_bytes_bound" in x for x in result.reasons)
