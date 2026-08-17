"""Accepted Layer 2 prerequisite identities are pinned and repository-owned."""

from __future__ import annotations

import json
import re

import pydantic
import pytest

from minos_engine.layer2 import prerequisites as P
from minos_engine.layer2.contracts import AcceptedPrerequisiteIdentity
from tests.conftest import REPO_ROOT

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_OID = re.compile(r"^[0-9a-f]{40}$")


def test_accepted_is_frozen_contract():
    assert isinstance(P.ACCEPTED, AcceptedPrerequisiteIdentity)
    with pytest.raises(pydantic.ValidationError):
        P.ACCEPTED.l1_gate_hash = "0" * 64  # type: ignore[misc]


def test_hashes_are_64_hex():
    for v in (
        P.L1_READY_GATE_HASH,
        P.PROTOCOL_READY_GATE_HASH,
        P.TWIN_READY_GATE_HASH,
        P.LAYER1_SCHEMA_HASH,
        P.PROFILER_CONFIG_HASH,
    ):
        assert _SHA256.match(v)


def test_git_ids_are_40_hex():
    for v in (
        P.QUALIFIED_SOURCE_COMMIT,
        P.QUALIFIED_SOURCE_TREE,
        P.ARTIFACT_COMMIT,
        P.ARTIFACT_TREE,
        P.V2_FRAMEWORK_COMMIT,
        P.V2_EVIDENCE_COMMIT,
        P.OWNER_ACCEPTANCE_COMMIT,
    ):
        assert _OID.match(v)


def test_invalid_shapes_rejected():
    import pydantic

    with pytest.raises(pydantic.ValidationError):
        AcceptedPrerequisiteIdentity(
            l1_gate_hash="tooshort",
            protocol_gate_hash=P.PROTOCOL_READY_GATE_HASH,
            twin_gate_hash=P.TWIN_READY_GATE_HASH,
            layer1_schema_hash=P.LAYER1_SCHEMA_HASH,
            profiler_config_hash=P.PROFILER_CONFIG_HASH,
            profiler_version=P.PROFILER_VERSION,
            qualified_source_commit=P.QUALIFIED_SOURCE_COMMIT,
            qualified_source_tree=P.QUALIFIED_SOURCE_TREE,
            artifact_commit=P.ARTIFACT_COMMIT,
            artifact_tree=P.ARTIFACT_TREE,
            v2_framework_commit=P.V2_FRAMEWORK_COMMIT,
            v2_evidence_commit=P.V2_EVIDENCE_COMMIT,
            owner_commit=P.OWNER_ACCEPTANCE_COMMIT,
        )


def test_uppercase_hash_rejected():
    # Constructing with an uppercase (non-lowercase-hex) hash must fail validation.
    fields = dict(P.ACCEPTED)
    fields["l1_gate_hash"] = P.L1_READY_GATE_HASH.upper()
    with pytest.raises(pydantic.ValidationError):
        AcceptedPrerequisiteIdentity(**fields)


@pytest.mark.skipif(
    not (REPO_ROOT / "gates" / "l1-ready.json").exists(),
    reason="L1-READY gate produced in Commit B",
)
def test_accepted_matches_committed_gate():
    raw = json.loads((REPO_ROOT / "gates" / "l1-ready.json").read_text(encoding="utf-8"))
    assert raw["gate_hash"] == P.L1_READY_GATE_HASH
    assert raw["qualified_source_git_sha"] == P.QUALIFIED_SOURCE_COMMIT
    assert raw["qualified_source_tree_sha"] == P.QUALIFIED_SOURCE_TREE
    assert raw["input_hashes"]["layer1_schema_hash"] == P.LAYER1_SCHEMA_HASH
    assert raw["input_hashes"]["profiler_config_hash"] == P.PROFILER_CONFIG_HASH
    assert raw["input_hashes"]["profiler_version"] == P.PROFILER_VERSION
