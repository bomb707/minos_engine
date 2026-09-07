"""The superseded v1 qualification artifact, kept as history and refused as qualification.

v1 recorded a real run, so it is not deleted or rewritten. What it cannot do is authorize
anything: its ownership loader read, parsed and traversed the whole 75-member profile snapshot
before skipping non-TRAIN members, so TEST identities were enumerated, and its
``skipped_partition_counts`` were derived by traversing the very records they claimed were
untouched. A count of skipped members is not a proof of non-access.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

import pytest

from minos_engine.layer2.safe_controller_qualification import (
    SAFE_CONTROLLER_QUALIFICATION_SCHEMA,
    SUPERSEDES,
    SafeControllerQualificationError,
    verify_qualification_report,
)
from minos_engine.qualification.l2f_accepted_identities import repository_root

V1_PATH = "reports/layer2/l2h-safe-controller-qualification-v1.json"
V1_IDENTITY = "7d305bcd7c35c82389259ec1d88058ff9202ce454a0364d15e4b864345aaf821"
V1_FILE_SHA = "b0a3a16d2a411d18e708ef6add63a6832778e51cfb8454874763a0f5c8604453"


@pytest.fixture(scope="module")
def historical() -> dict[str, Any]:
    return dict(json.loads((repository_root() / V1_PATH).read_bytes()))


def test_the_v1_artifact_is_preserved_unchanged() -> None:
    """Superseded is not deleted. The run happened and the record of it stands."""
    raw = (repository_root() / V1_PATH).read_bytes()
    assert hashlib.sha256(raw).hexdigest() == V1_FILE_SHA


def test_the_v1_artifact_cannot_qualify_anything(historical: dict[str, Any]) -> None:
    assert historical["schema_version"] == "l2h-safe-controller-qualification-v1"
    assert historical["schema_version"] != SAFE_CONTROLLER_QUALIFICATION_SCHEMA
    with pytest.raises(SafeControllerQualificationError, match="unexpected qualification schema"):
        verify_qualification_report(historical)


def test_the_reason_v1_is_superseded_is_recorded_in_source() -> None:
    assert SUPERSEDES["schema"] == "l2h-safe-controller-qualification-v1"
    assert SUPERSEDES["identity"] == V1_IDENTITY
    assert SUPERSEDES["status"] == "HISTORICAL_EVIDENCE_NOT_VALID_FOR_QUALIFICATION"
    assert "enumerated" in SUPERSEDES["reason"]


def test_the_v1_isolation_claim_is_the_one_that_failed(historical: dict[str, Any]) -> None:
    """The specific claim that did not match the implementation, named so it is not repeated."""
    assert historical["checks"]["sealed_partitions_never_enumerated"] is True
    # and it was derived from counts obtained BY traversing the sealed records
    assert historical["observation"]["skipped_partition_counts"] == {"test": 15, "validation": 10}


def test_no_gate_artifact_accompanies_the_superseded_evidence() -> None:
    for name in (
        "safe-controller-frozen.json",
        "controller-frozen.json",
        "models-qualified.json",
    ):
        assert not (repository_root() / "gates" / name).exists()
