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


# ---------------------------------------------------------------------------------------- #
# the corrected v2 evidence
# ---------------------------------------------------------------------------------------- #
V2_PATH = "reports/layer2/l2h-safe-controller-qualification-v2.json"
V2_IDENTITY = "8408630ffb130afeb22bf08dc47b78f3be5bbeef3dc102c2c3b5265f3431d286"
V2_FILE_SHA = "483f77c63938331b4930439a244e8db8992049d7a4fab75faf76629ed5975e9d"
V2_SOURCE_COMMIT = "23b360562b317467a542ee722341fc2d0931bfed"


@pytest.fixture(scope="module")
def corrected() -> dict[str, Any]:
    return dict(json.loads((repository_root() / V2_PATH).read_bytes()))


def test_the_corrected_evidence_verifies_and_passes(corrected: dict[str, Any]) -> None:
    from minos_engine.layer2.safe_controller_qualification import (
        qualification_report_identity,
    )

    report = verify_qualification_report(corrected)
    assert report["ok"] is True
    assert report["status"] == "PASS"
    assert report["capability_scope"] == "SAFE_BASELINE_ONLY"
    assert qualification_report_identity(corrected) == V2_IDENTITY
    assert corrected["schema_version"] == SAFE_CONTROLLER_QUALIFICATION_SCHEMA


def test_the_corrected_evidence_is_canonical_bytes(corrected: dict[str, Any]) -> None:
    from minos_engine.common.canonical_json import canonical_json_bytes

    raw = (repository_root() / V2_PATH).read_bytes()
    assert canonical_json_bytes(corrected) == raw
    assert hashlib.sha256(raw).hexdigest() == V2_FILE_SHA


def test_the_corrected_evidence_records_the_superseded_run(corrected: dict[str, Any]) -> None:
    assert corrected["supersedes"] == dict(sorted(SUPERSEDES.items()))
    assert corrected["supersedes"]["identity"] == V1_IDENTITY


def test_the_corrected_campaign_is_train_only_and_sealed(corrected: dict[str, Any]) -> None:
    observation = corrected["observation"]
    assert observation["admitted_partition"] == "train"
    assert observation["profile_count"] == 50
    assert observation["materialized_round_count"] == 50
    assert observation["decision_count"] == 200
    assert observation["forbidden_sealed_path_open_attempts"] == 0
    assert observation["attempted_sealed_authorities"] == []
    assert "skipped_partition_counts" not in observation, (
        "counts derived by traversing sealed records are exactly what v1 got wrong"
    )
    assert observation["actual_mode_counts"] == {"SAFE_BASELINE": 200}
    assert observation["fallback_reason_counts"] == {"NONE": 50, "SAFE_BASELINE_FORCED": 150}
    assert observation["selected_config_distribution"] == {
        "157d88d1587c13be395c62d60e27d1becdada78fad45e65d883bc1190e51acea": 200
    }


def test_the_corrected_evidence_is_anchored(corrected: dict[str, Any]) -> None:
    anchors = corrected["observation"]["profile_ownership_anchors"]
    assert corrected["observation"]["profile_ownership_schema"] == (
        "l2h-round-profile-ownership-v2"
    )
    assert (
        anchors["baseline_protocol_hash"]
        == "c548e190571f5e964560cf30021a520ea8aad6674569fa3202af880d7dff77d1"
    )
    assert (
        anchors["train_schedule_manifest_sha256"]
        == "694a8993ef64f72ca3705442c1bc070c0288d46e08e00e39bab1155d4415d454"
    )
    assert (
        anchors["registry_snapshot_hash"]
        == "3e60aa65aeed8969e29ebeef83024f6fa2285a13c155d7d6dc0c601d1e94f675"
    )


def test_the_corrected_evidence_claims_nothing_it_must_not(corrected: dict[str, Any]) -> None:
    assert corrected["gate_issued"] is False
    assert corrected["service_activated"] is False
    assert corrected["validation_read"] is False
    assert corrected["test_accessed"] is False
    observation = corrected["observation"]
    assert observation["models_qualified_status"] == "HOLD_NO_TRAIN_PROMOTABLE_CONTEXTUAL_MODEL"
    assert observation["models_qualified_gate_present"] is False
    assert observation["model_load_count"] == 0
    assert observation["candidate_generation_count"] == 0
    assert observation["parameter_mutation_count"] == 0
    assert observation["invalid_config_count"] == 0
    assert observation["isolation_violations"] == []
    assert observation["forbidden_calls"] == []
