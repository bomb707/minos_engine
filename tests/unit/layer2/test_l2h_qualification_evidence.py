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


# ---------------------------------------------------------------------------------------- #
# the current (v3) evidence
# ---------------------------------------------------------------------------------------- #
V3_PATH = "reports/layer2/l2h-safe-controller-qualification-v3.json"
V3_IDENTITY = "a0e8840dbdad6beeca7e7548b500868a860438dbea5673b08b11dae70832ba8e"
V3_FILE_SHA = "90fb7b5f819eee2f414d8b55adfa7bbbd8164eadd874d3354393bb8f093ec7da"
V3_SOURCE_COMMIT = "7d064fe8bc7185bd5c07d16f1fec9dfdd21970b0"
V3_SOURCE_TREE = "799cd5cc493bd79f5ad6e5ecc78033eada5a9f5b"
SAFE_CONFIG = "157d88d1587c13be395c62d60e27d1becdada78fad45e65d883bc1190e51acea"


@pytest.fixture(scope="module")
def current() -> dict[str, Any]:
    return dict(json.loads((repository_root() / V3_PATH).read_bytes()))


def test_the_current_evidence_verifies_and_passes(current: dict[str, Any]) -> None:
    from minos_engine.common.canonical_json import canonical_json_bytes
    from minos_engine.layer2.safe_controller_qualification import (
        qualification_report_identity,
    )

    report = verify_qualification_report(current)
    assert report["ok"] is True
    assert report["status"] == "PASS"
    assert report["capability_scope"] == "SAFE_BASELINE_ONLY"
    assert current["schema_version"] == SAFE_CONTROLLER_QUALIFICATION_SCHEMA
    assert qualification_report_identity(current) == V3_IDENTITY
    raw = (repository_root() / V3_PATH).read_bytes()
    assert canonical_json_bytes(current) == raw
    assert hashlib.sha256(raw).hexdigest() == V3_FILE_SHA


def test_the_current_evidence_names_its_source(current: dict[str, Any]) -> None:
    observation = current["observation"]
    assert observation["source_commit"] == V3_SOURCE_COMMIT
    assert observation["source_tree"] == V3_SOURCE_TREE


def test_the_current_evidence_is_anchored_to_the_accepted_phase_a_identity(
    current: dict[str, Any],
) -> None:
    anchors = current["observation"]["profile_ownership_anchors"]
    assert (
        anchors["phase_a_authority_hash"]
        == "9ad0ba48c80e7b305505fea201e93185deb15ae735338086d05b38afcf4deb3f"
    )
    assert anchors["phase_a_accepted_source_commit"] == ("9395c116e22c52777441d76200acd96a738417bf")
    assert anchors["phase_a_accepted_source_tree"] == ("fe83142845574a7ae28f7a236e959b56474ed997")
    assert (
        anchors["train_schedule_manifest_sha256"]
        == "694a8993ef64f72ca3705442c1bc070c0288d46e08e00e39bab1155d4415d454"
    )
    assert (
        anchors["registry_snapshot_hash"]
        == "3e60aa65aeed8969e29ebeef83024f6fa2285a13c155d7d6dc0c601d1e94f675"
    )
    assert current["observation"]["profile_corpus_identity"] == (
        "9cc53b5d28c8a8da34c25095362c09d8cb1fb57533ff0a0b3e1fdf7000970b03"
    )


def test_the_current_evidence_observed_complete_isolation(current: dict[str, Any]) -> None:
    observation = current["observation"]
    assert observation["guarded_file_apis"] == ["builtins.open", "io.open", "os.open"]
    assert observation["forbidden_sealed_path_open_attempts"] == 0
    assert observation["test_identity_authority_open_attempts"] == 0
    assert observation["validation_identity_authority_open_attempts"] == 0
    assert observation["attempted_sealed_authorities"] == []
    # DERIVED from those counters, not authored
    assert current["test_accessed"] is False
    assert current["validation_read"] is False


def test_the_current_campaign_counts(current: dict[str, Any]) -> None:
    observation = current["observation"]
    assert observation["admitted_partition"] == "train"
    assert observation["profile_count"] == 50
    assert observation["materialized_round_count"] == 50
    assert observation["decision_count"] == 200
    assert observation["requested_mode_counts"] == {
        "BOUNDED": 50,
        "FULL_CONTEXTUAL": 50,
        "REFINEMENT": 50,
        "SAFE_BASELINE": 50,
    }
    assert observation["actual_mode_counts"] == {"SAFE_BASELINE": 200}
    assert observation["fallback_reason_counts"] == {"NONE": 50, "SAFE_BASELINE_FORCED": 150}
    assert observation["selected_config_distribution"] == {SAFE_CONFIG: 200}
    assert observation["invalid_config_count"] == 0
    assert observation["model_load_count"] == 0
    assert observation["candidate_generation_count"] == 0
    assert observation["parameter_mutation_count"] == 0


def test_the_current_evidence_supersedes_both_predecessors(current: dict[str, Any]) -> None:
    schemas = {entry["schema"] for entry in current["supersedes"]}
    assert schemas == {
        "l2h-safe-controller-qualification-v1",
        "l2h-safe-controller-qualification-v2",
    }
    assert "not invalid science" in current["supersession_note"]


def test_the_current_evidence_claims_no_gate_and_no_activation(current: dict[str, Any]) -> None:
    assert current["gate_issued"] is False
    assert current["service_activated"] is False
    observation = current["observation"]
    assert observation["models_qualified_status"] == "HOLD_NO_TRAIN_PROMOTABLE_CONTEXTUAL_MODEL"
    assert observation["models_qualified_gate_present"] is False
    assert observation["select_config_public_boundary_blocked"] is True


def test_the_future_gate_requires_the_corrected_checks() -> None:
    from minos_engine.gates.required_checks import required_checks_for

    required = required_checks_for("SAFE-CONTROLLER-FROZEN")
    for corrected in (
        "sealed_authorities_never_opened",
        "train_ownership_anchored_to_accepted_authority",
        "publication_identity_is_derived_not_declared",
    ):
        assert corrected in required
    # and the superseded reports do not even carry them all
    v1 = json.loads((repository_root() / HISTORICAL["v1"]["path"]).read_bytes())
    assert not required <= set(v1["checks"])
