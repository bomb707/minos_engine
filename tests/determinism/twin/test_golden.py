"""Determinism — golden content-hash vectors for Twin artifacts.

These pin the canonical hashing of a fixed plan and comparison. A change here
means a semantically relevant change to the contract or hashing — investigate
before updating the golden value.
"""

from __future__ import annotations

from minos_engine.intake.contracts import Region
from minos_engine.tools.happy import parse_raw_result
from minos_engine.twin.comparison import build_comparison_metrics
from minos_engine.twin.execution_plan import build_execution_plan
from minos_engine.twin.fixtures import load_replay_fixture
from minos_engine.twin.identities import ToolIdentity
from tests.conftest import REPO_ROOT

GOLDEN_PLAN_HASH = "d5fcbb70417cba8340d47d4828462f624c254d1a3caf372441808f956dbf51d6"
GOLDEN_COMPARISON_HASH = "d5b32dd1271f9ff7bf9477e62d147a787fd8e3dd4f1c30de366a2185209dd79e"


def test_golden_plan_hash():
    fixture = load_replay_fixture(
        REPO_ROOT / "tests" / "fixtures" / "twin" / "replay" / "valid.json"
    )
    assert build_execution_plan(fixture.request).plan_hash == GOLDEN_PLAN_HASH


def test_golden_comparison_hash():
    region = Region.from_source("chr19:13000000-23000000", "one_based_inclusive")
    payload = {"snp": {"tp": 3, "fp": 1, "fn": 2}, "indel": {"tp": 5, "fp": 5, "fn": 0}}
    cm = build_comparison_metrics(
        round_id="R1",
        region=region,
        reference_sha256="c" * 64,
        raw=parse_raw_result(payload),
        truth_vcf_sha256="e" * 64,
        query_vcf_sha256="f" * 64,
        tool=ToolIdentity(name="hap.py", version="0.3.14"),
        raw_payload=payload,
    )
    assert cm.content_hash() == GOLDEN_COMPARISON_HASH
