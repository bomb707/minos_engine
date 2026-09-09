"""The shared region/round-id rules, held to the contracts they were extracted from.

Layer 2 may not import ``minos_engine.intake``, so the region rule is stated in ``common`` rather
than reached for across the boundary. That duplication is only safe if it cannot drift, which is
what these tests are for -- the same discipline ``layer2/ingest/validation.py`` applies to its pin
of Layer 1's identity-section list.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from minos_engine.common.genomic_region import (
    MAX_ROUND_ID_LENGTH,
    ONE_BASED_INCLUSIVE,
    ZERO_BASED_HALF_OPEN,
    is_upstream_round_id,
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


# --------------------------------------------------------------------------- #
# parity with the official upstream rule
# --------------------------------------------------------------------------- #
#: Transcribed from ``minos_subnet/templates/tool_params.py::validate_round_id``. Each case names
#: the upstream branch it exercises, so a change on either side shows up as a named failure.
UPSTREAM_CASES: list[tuple[str, bool, str]] = [
    ("2026-01-21T12:00:00.000000+00:00", True, "the documented example"),
    ("2026-01-21T12:00:00+00:00", True, "offset, no microseconds"),
    ("2026-01-21T12:00:00Z", True, "trailing Z, parsed directly on 3.11+"),
    ("2026-01-21T12:00:00+00:00Z", True, "legacy: offset AND trailing Z, stripped and retried"),
    ("2026-01-21T12:00:00.000000+05:30", True, "a non-UTC offset is still timezone-aware"),
    ("2026-01-21T12:00:00", False, "NAIVE: upstream requires tzinfo"),
    ("2026-01-21T12:00:00.000000", False, "naive with microseconds"),
    ("2026-01-21", False, "date only, naive"),
    ("", False, "empty"),
    ("not-a-timestamp", False, "not a timestamp at all"),
    ("2026-13-45T99:00:00+00:00", False, "structurally invalid"),
    ("2026-01-21T12:00:00+00:00ZZ", False, "only ONE trailing Z is stripped"),
    ("x" * 41, False, "over the 40-character cap"),
]


@pytest.mark.parametrize("round_id,expected,why", UPSTREAM_CASES)
def test_the_live_branch_matches_the_official_upstream_rule(
    round_id: str, expected: bool, why: str
):
    """``is_upstream_round_id`` is upstream's rule, transcribed. Hold it to the table."""
    assert is_upstream_round_id(round_id) is expected, why


@pytest.mark.parametrize("round_id,expected,why", UPSTREAM_CASES)
def test_the_engine_accepts_exactly_those_and_never_more(round_id: str, expected: bool, why: str):
    """The engine may be stricter, never laxer: it must not accept what upstream rejects."""
    try:
        validate_round_identifier(round_id)
        accepted = True
    except ValueError:
        accepted = False
    if not expected:
        assert not accepted, f"the engine accepted something upstream rejects: {why}"
    else:
        assert accepted, why


def test_the_naive_timestamp_regression_is_closed():
    """The exact defect: a timestamp with no timezone used to pass and must not."""
    assert is_upstream_round_id("2026-01-21T12:00:00") is False
    with pytest.raises(ValueError, match="timezone-aware"):
        validate_round_identifier("2026-01-21T12:00:00")


@pytest.mark.parametrize(
    "round_id,why",
    [
        (
            "2026-01-21 12:00:00+00:00",
            "space separator: fromisoformat allows it, this engine does not",
        ),
        ("../2026-01-21T12:00:00+00:00", "traversal fragment"),
        ("2026-01-21T12:00:00+00:00\n", "trailing newline"),
    ],
)
def test_the_engine_is_deliberately_stricter_than_upstream_on_path_safety(round_id: str, why: str):
    """Documented divergence: strictness only ever removes values, never admits one."""
    with pytest.raises(ValueError):
        validate_round_identifier(round_id)


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


#: The official implementation, when this checkout sits beside the subnet. Located relative to the
#: repository rather than at an operator-specific absolute path, and overridable, so the test is
#: portable and simply skips where the subnet is not checked out.
UPSTREAM_TOOL_PARAMS_ENV = "MINOS_SUBNET_ROOT"


def _upstream_tool_params() -> Path:
    import os

    configured = os.environ.get(UPSTREAM_TOOL_PARAMS_ENV)
    root = Path(configured) if configured else REPO_ROOT.parent / "minos_subnet"
    return root / "templates" / "tool_params.py"


def _load_upstream():
    import importlib.util

    path = _upstream_tool_params()
    if not path.is_file():
        return None
    spec = importlib.util.spec_from_file_location("upstream_tool_params", path)
    if spec is None or spec.loader is None:
        return None
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except Exception:
        return None
    return getattr(module, "validate_round_id", None)


@pytest.mark.parametrize("round_id,expected,why", UPSTREAM_CASES)
def test_parity_against_the_real_upstream_implementation(round_id: str, expected: bool, why: str):
    """Ask the official rule directly, rather than only trusting the transcription."""
    upstream = _load_upstream()
    if upstream is None:
        pytest.skip("the minos_subnet checkout is not available beside this repository")
    assert bool(upstream(round_id)["valid"]) is is_upstream_round_id(round_id), why
    assert bool(upstream(round_id)["valid"]) is expected, why


def test_the_engine_never_accepts_a_round_id_upstream_would_reject():
    """The one-way guarantee, checked across the whole table plus the path-safety cases."""
    upstream = _load_upstream()
    if upstream is None:
        pytest.skip("the minos_subnet checkout is not available beside this repository")
    extra = [
        "2026-01-21 12:00:00+00:00",
        "../2026-01-21T12:00:00+00:00",
        "2026-01-21T12:00:00+00:00\n",
        "0279a3b8042f848b",
    ]
    for round_id in [case[0] for case in UPSTREAM_CASES] + extra:
        try:
            validate_round_identifier(round_id)
            accepted = True
        except ValueError:
            accepted = False
        if accepted and not bool(upstream(round_id)["valid"]):
            # the ONLY permitted case: the frozen research hex shape, which predates the platform
            assert set(round_id) <= set("0123456789abcdef"), (
                f"the engine accepts {round_id!r}, which upstream rejects"
            )
