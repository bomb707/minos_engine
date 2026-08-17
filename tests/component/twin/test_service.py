"""Test group H — Twin service orchestration and prerequisite gate."""

from __future__ import annotations

import pytest

from minos_engine.common.errors import GateError
from minos_engine.qualification.twin_checks import make_request
from minos_engine.twin.identities import ToolIdentity
from minos_engine.twin.service import TwinService, default_protocol_ready_check
from minos_engine.twin.unavailable import AvailabilityStatus

_H = "a" * 64
_TOOL = ToolIdentity(name="hap.py", version="0.3.14")
_RAW = {"snp": {"tp": 90, "fp": 5, "fn": 10}, "indel": {"tp": 40, "fp": 8, "fn": 12}}


def _replay(service, now="2026-08-17T12:00:00+00:00"):
    return service.replay(
        make_request(),
        _RAW,
        truth_vcf_sha256="e" * 64,
        query_vcf_sha256="f" * 64,
        comparison_tool=_TOOL,
        now_iso=now,
        fixture_hash=_H,
    )


def test_replay_produces_manifest_and_unavailable_score():
    result = _replay(TwinService(protocol_ready_check=lambda: _H))
    assert result.manifest.scorer_status is AvailabilityStatus.UNAVAILABLE
    assert result.manifest.prerequisite_gate_hash == _H
    assert result.plan.plan_hash and result.comparison.content_hash()


def test_replay_is_deterministic_created_at_excluded():
    svc = TwinService(protocol_ready_check=lambda: _H)
    a = _replay(svc, now="2026-08-17T12:00:00+00:00")
    b = _replay(svc, now="2030-01-01T00:00:00+00:00")
    assert a.manifest.manifest_hash == b.manifest.manifest_hash


def test_prerequisite_gate_failure_raises():
    with pytest.raises(GateError):
        _replay(TwinService(protocol_ready_check=lambda: None))


def test_default_protocol_ready_check_reads_repo_gate():
    # The committed Stage 0 PROTOCOL-READY gate authorizes promotion.
    gate_hash = default_protocol_ready_check()
    assert gate_hash and len(gate_hash) == 64
