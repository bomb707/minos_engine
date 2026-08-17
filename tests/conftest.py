"""Shared test fixtures and helpers."""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURES = REPO_ROOT / "tests" / "fixtures"
API_FIXTURES = FIXTURES / "api"
GATK_FIXTURES = FIXTURES / "gatk"
SRC = REPO_ROOT / "src" / "minos_engine"


@pytest.fixture
def valid_round_path() -> Path:
    return API_FIXTURES / "valid_round.json"


@pytest.fixture
def parameter_space_path() -> Path:
    return API_FIXTURES / "gatk_parameter_space.json"


@pytest.fixture
def default_config_path() -> Path:
    return GATK_FIXTURES / "default_config.json"


def make_raw_payload() -> dict[str, Any]:
    """A minimal, self-consistent raw protocol payload for unit tests."""
    h = lambda c: (c * 64)[:64]  # noqa: E731
    return {
        "stale": False,
        "round": {
            "round_id": "2026-08-17T12:00:00.000000+00:00",
            "status": "open",
            "region": "chr19:13000000-23000000",
            "coordinate_system": "one_based_inclusive",
            "deadline_at": "2026-08-17T13:12:00+00:00",
            "time_remaining_seconds": 4200,
            "commit_reveal": {"available": False, "detail": "no commit-reveal"},
        },
        "artifacts": {
            "bam": {
                "uri": "s3://x/input.bam",
                "sha256": h("a"),
                "size_bytes": 100,
                "media_type": "application/x-bam",
            },
            "bai": {
                "uri": "s3://x/input.bam.bai",
                "sha256": h("b"),
                "size_bytes": 10,
                "media_type": "application/x-bai",
            },
            "reference": {
                "uri": "s3://x/chr19.fa",
                "sha256": h("c"),
                "size_bytes": 999,
                "media_type": "text/x-fasta",
            },
        },
        "parameter_space": {
            "caller": "gatk",
            "source": "unit-test",
            "parameters": {
                "min_pruning": {"type": "int", "minimum": 2, "maximum": 10, "default": 2},
                "emit_ref_confidence": {
                    "type": "enum",
                    "enum_values": ["NONE", "GVCF", "BP_RESOLUTION"],
                    "default": "NONE",
                },
            },
        },
        "network_config": {"network": "finney", "api_base_url": "https://api.theminos.ai"},
        "provenance": {
            "minos_upstream_commit": "0f1e2d3c4b5a6978",
            "scorer_hash": h("5"),
            "gatk_image_digest": "broadinstitute/gatk:4.5.0.0",
            "happy_image_digest": "genonet/hap-py@sha256:03ac",
            "reference_sha256": h("c"),
        },
    }


def make_raw_response(payload: dict[str, Any] | None = None):
    from minos_engine.protocol.client import RawProtocolResponse

    p = payload if payload is not None else make_raw_payload()
    return RawProtocolResponse(
        payload=copy.deepcopy(p),
        retrieved_at="2026-08-17T12:00:00+00:00",
        source_endpoints={"round": "https://api.theminos.ai/v2/round-status"},
    )
