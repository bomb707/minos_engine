"""Gate artifact and release manifest validation + determinism."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from minos_engine.gates.contracts import EvidenceItem, GateArtifact, GateStatus
from minos_engine.manifests.release import ReleaseManifest

_TS1 = "2026-08-17T12:00:00+00:00"
_TS2 = "2027-01-01T09:09:09+00:00"
_H = "a" * 64


def _pass_gate(created_at: str = _TS1) -> GateArtifact:
    return GateArtifact(
        gate_name="TEST",
        status=GateStatus.PASS,
        engine_git_sha="sha",
        mandatory_checks={"a": True, "b": True},
        evidence=(EvidenceItem(description="r", path="reports/r.md", sha256="a" * 64),),
        created_at=created_at,
    )


def test_pass_gate_evidence_requires_sha256():
    with pytest.raises(ValidationError):
        GateArtifact(
            gate_name="TEST",
            status=GateStatus.PASS,
            engine_git_sha="sha",
            mandatory_checks={"a": True},
            evidence=(EvidenceItem(description="r", path="reports/r.md"),),  # no sha256
            created_at=_TS1,
        )


def test_pass_gate_requires_true_checks():
    with pytest.raises(ValidationError):
        GateArtifact(
            gate_name="T",
            status=GateStatus.PASS,
            engine_git_sha="s",
            mandatory_checks={"a": False},
            created_at=_TS1,
        )


def test_pass_gate_requires_at_least_one_check():
    with pytest.raises(ValidationError):
        GateArtifact(
            gate_name="T",
            status=GateStatus.PASS,
            engine_git_sha="s",
            mandatory_checks={},
            created_at=_TS1,
        )


def test_gate_hash_excludes_created_at():
    assert _pass_gate(_TS1).gate_hash == _pass_gate(_TS2).gate_hash


def test_gate_hash_tamper_detected():
    g = _pass_gate()
    with pytest.raises(ValidationError):
        GateArtifact(
            gate_name="TEST",
            status=GateStatus.PASS,
            engine_git_sha="sha",
            mandatory_checks={"a": True, "b": True},
            evidence=(EvidenceItem(description="r", path="reports/r.md", sha256="a" * 64),),
            created_at=_TS1,
            gate_hash="0" * 64,
        )
    assert g.gate_hash == g.compute_hash()


def _manifest(**overrides):
    base = {
        "engine_version": "0.1.0",
        "git_sha": "abc123",
        "engine_config_hash": _H,
        "protocol_contract_hash": _H,
        "gatk_registry_hash": _H,
        "minos_upstream_commit": "deadbeef",
        "scorer_hash": "f" * 64,
        "parameter_space_hash": _H,
        "created_at": _TS1,
    }
    base.update(overrides)
    return ReleaseManifest(**base)


@pytest.mark.parametrize("field", ["git_sha", "scorer_hash", "minos_upstream_commit"])
def test_required_identity_empty_rejected(field):
    with pytest.raises(ValidationError):
        _manifest(**{field: ""})


def test_manifest_content_hash_excludes_created_at():
    assert _manifest().content_hash() == _manifest(created_at=_TS2).content_hash()


def test_manifest_hash_field_shape_enforced():
    with pytest.raises(ValidationError):
        _manifest(engine_config_hash="short")
