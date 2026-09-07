"""The superseded qualification artifacts, kept as history and refused as qualification.

Both recorded real runs of the controller they were written against, so neither is deleted or
rewritten and neither is described as invalid science. What neither can do is authorize anything:

* v1 read, parsed and traversed the whole 75-member profile snapshot before skipping non-TRAIN
  members, so TEST identities were enumerated -- and its ``skipped_partition_counts`` were derived
  by traversing the very records they claimed were untouched;
* v2 fixed the enumeration but authenticated the TRAIN-schedule anchor incompletely: the Phase-A
  authority's own identity was recorded rather than verified against an already-accepted one, so
  a coherent rewrite of the authority and the schedule together survived the authority layer. Its
  access instrumentation also covered only ``io.open``, and only the ownership load rather than
  the whole run.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

import pytest

from minos_engine.layer2.safe_controller_qualification import (
    SAFE_CONTROLLER_QUALIFICATION_SCHEMA,
    SUPERSEDES,
    SUPERSESSION_NOTE,
    SafeControllerQualificationError,
    verify_qualification_report,
)
from minos_engine.qualification.l2f_accepted_identities import repository_root

HISTORICAL = {
    "v1": {
        "path": "reports/layer2/l2h-safe-controller-qualification-v1.json",
        "schema": "l2h-safe-controller-qualification-v1",
        "identity": "7d305bcd7c35c82389259ec1d88058ff9202ce454a0364d15e4b864345aaf821",
        "file_sha": "b0a3a16d2a411d18e708ef6add63a6832778e51cfb8454874763a0f5c8604453",
    },
    "v2": {
        "path": "reports/layer2/l2h-safe-controller-qualification-v2.json",
        "schema": "l2h-safe-controller-qualification-v2",
        "identity": "8408630ffb130afeb22bf08dc47b78f3be5bbeef3dc102c2c3b5265f3431d286",
        "file_sha": "483f77c63938331b4930439a244e8db8992049d7a4fab75faf76629ed5975e9d",
    },
}


@pytest.mark.parametrize("version", sorted(HISTORICAL))
def test_the_superseded_artifact_is_preserved_unchanged(version: str) -> None:
    """Superseded is not deleted. The run happened and the record of it stands."""
    entry = HISTORICAL[version]
    raw = (repository_root() / entry["path"]).read_bytes()
    assert hashlib.sha256(raw).hexdigest() == entry["file_sha"]


@pytest.mark.parametrize("version", sorted(HISTORICAL))
def test_a_superseded_artifact_cannot_qualify_anything(version: str) -> None:
    entry = HISTORICAL[version]
    document: dict[str, Any] = json.loads((repository_root() / entry["path"]).read_bytes())
    assert document["schema_version"] == entry["schema"]
    assert document["schema_version"] != SAFE_CONTROLLER_QUALIFICATION_SCHEMA
    with pytest.raises(SafeControllerQualificationError, match="unexpected qualification schema"):
        verify_qualification_report(document)


def test_source_records_why_each_predecessor_is_superseded() -> None:
    entries = {e["schema"]: e for e in SUPERSEDES}
    assert set(entries) == {HISTORICAL[v]["schema"] for v in HISTORICAL}
    for version, expected in HISTORICAL.items():
        entry = entries[expected["schema"]]
        assert entry["identity"] == expected["identity"], version
        assert entry["status"] == "HISTORICAL_EVIDENCE_NOT_VALID_FOR_QUALIFICATION"
    assert "enumerated" in entries[HISTORICAL["v1"]["schema"]]["reason"]
    assert "incompletely" in entries[HISTORICAL["v2"]["schema"]]["reason"]
    assert "not invalid science" in SUPERSESSION_NOTE


def test_the_v1_isolation_claim_is_the_one_that_failed() -> None:
    """The specific claim that did not match the implementation, named so it is not repeated."""
    v1 = json.loads((repository_root() / HISTORICAL["v1"]["path"]).read_bytes())
    assert v1["checks"]["sealed_partitions_never_enumerated"] is True
    assert v1["observation"]["skipped_partition_counts"] == {"test": 15, "validation": 10}


def test_the_v2_anchor_claim_is_the_one_that_failed() -> None:
    """v2 recorded the Phase-A authority hash it read; it never checked it was the accepted one."""
    v2 = json.loads((repository_root() / HISTORICAL["v2"]["path"]).read_bytes())
    anchors = v2["observation"]["profile_ownership_anchors"]
    assert "phase_a_authority_hash" in anchors
    assert "phase_a_accepted_source_commit" not in anchors
    # and its guard covered only one API and only the ownership load
    assert "guarded_file_apis" not in v2["observation"]


def test_no_gate_artifact_accompanies_the_superseded_evidence() -> None:
    for name in (
        "safe-controller-frozen.json",
        "controller-frozen.json",
        "models-qualified.json",
    ):
        assert not (repository_root() / "gates" / name).exists()
