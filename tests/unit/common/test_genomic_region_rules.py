"""The shared region/round-id rules, held to the contracts they were extracted from.

Layer 2 may not import ``minos_engine.intake``, so the region rule is stated in ``common`` rather
than reached for across the boundary. That duplication is only safe if it cannot drift, which is
what these tests are for -- the same discipline ``layer2/ingest/validation.py`` applies to its pin
of Layer 1's identity-section list.
"""

from __future__ import annotations

import json

import pytest

from minos_engine.common.genomic_region import (
    MAX_ROUND_ID_LENGTH,
    ONE_BASED_INCLUSIVE,
    ZERO_BASED_HALF_OPEN,
    normalize_region,
    validate_round_identifier,
)
from minos_engine.intake.contracts import Region
from minos_engine.layer2.round_profile_authority import TRAIN_SCHEDULE_PATH
from tests.conftest import REPO_ROOT

REGIONS = [
    "chr18:1-80373285",
    "chr19:13000000-23000000",
    "chr20:45000000-50000000",
    "chr21:1-46709983",
    "chr22:10510000-50818468",
]


@pytest.mark.parametrize("source", REGIONS)
@pytest.mark.parametrize("convention", [ONE_BASED_INCLUSIVE, ZERO_BASED_HALF_OPEN])
def test_the_shared_rule_agrees_with_the_intake_region_contract(source: str, convention: str):
    contig, start0, end0 = normalize_region(source, convention)
    region = Region.from_source(source, convention)
    assert (contig, start0, end0) == (region.contig, region.start0, region.end0_exclusive)
    assert end0 - start0 == region.length_bp


@pytest.mark.parametrize(
    "source", ["chr20", "chr20:", "chr20:10-", "chr20:1M-2M", "", "chr20:20-10"]
)
def test_an_ambiguous_region_is_a_hard_failure(source: str):
    with pytest.raises(ValueError):
        normalize_region(source, ONE_BASED_INCLUSIVE)


def test_an_unknown_coordinate_convention_is_refused():
    with pytest.raises(ValueError, match="unknown coordinate convention"):
        normalize_region("chr20:1-100", "guess")


def test_a_one_based_start_of_zero_underflows_rather_than_clamping():
    with pytest.raises(ValueError, match="underflow"):
        normalize_region("chr20:0-100", ONE_BASED_INCLUSIVE)


@pytest.mark.parametrize(
    "round_id",
    [
        "0279a3b8042f848b",
        "2026-01-21T12:00:00.000000+00:00",
        "2026-09-08T12:00:00+00:00",
    ],
)
def test_both_shapes_the_engine_actually_meets_are_accepted(round_id: str):
    assert validate_round_identifier(round_id) == round_id


@pytest.mark.parametrize(
    "round_id",
    [
        "",
        "   ",
        "NOT-A-ROUND",
        "../escape",
        "a/b",
        "a\\b",
        "with space",
        "trailing ",
        "x" * (MAX_ROUND_ID_LENGTH + 1),
        "2026-01-21T12:00:00+00:00\n",
    ],
)
def test_anything_else_is_refused(round_id: str):
    with pytest.raises(ValueError):
        validate_round_identifier(round_id)


def test_the_widened_rule_still_accepts_every_frozen_train_round():
    """Hex is a strict subset of what is accepted, so no existing artifact changes."""
    schedule = json.loads((REPO_ROOT / TRAIN_SCHEDULE_PATH).read_bytes())
    rounds = [str(row["round_id"]) for batch in schedule["batches"] for row in batch]
    assert len(rounds) == 50
    for round_id in rounds:
        assert validate_round_identifier(round_id) == round_id


def test_the_attestation_contract_accepts_both_shapes_too():
    from pydantic import ValidationError

    from minos_engine.layer2.ingest.contracts import InputIntegrityAttestation

    base = {
        "generator": "test",
        "generator_version": "1",
        "dataset_id": "d",
        "chromosome": "chr20",
        "registry_snapshot_hash": "a" * 64,
        "bam_sha256": "b" * 64,
        "bai_sha256": "c" * 64,
        "reference_sha256": "d" * 64,
        "fai_sha256": "e" * 64,
        "region_hash": "f" * 64,
        "identity_tuple_hash": "0" * 64,
        "computed_reference_m5": "1" * 32,
        "m5_status": "ABSENT",
    }
    for round_id in ("0279a3b8042f848b", "2026-09-08T12:00:00+00:00"):
        assert InputIntegrityAttestation(**base, round_id=round_id).round_id == round_id
    with pytest.raises(ValidationError):
        InputIntegrityAttestation(**base, round_id="../nope")
