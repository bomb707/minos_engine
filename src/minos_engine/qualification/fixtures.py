"""Deterministic qualification fixtures (production, not test-only).

Production qualification code must never import ``tests.*``. Any deterministic
payload a qualification check needs is built here. Tests may import these
helpers; the reverse is forbidden (architecture test enforces it).
"""

from __future__ import annotations

import copy
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from minos_engine.protocol.client import RawProtocolResponse


def _h(char: str) -> str:
    return (char * 64)[:64]


def valid_raw_payload() -> dict[str, Any]:
    """A minimal, self-consistent raw protocol payload (all required identities)."""
    return {
        "stale": False,
        "round": {
            "round_id": "2026-08-17T12:00:00.000000+00:00",
            "status": "open",
            "region": "chr19:13000000-23000000",
            "coordinate_system": "one_based_inclusive",
            "deadline_at": "2026-08-17T13:12:00+00:00",
            "time_remaining_seconds": 4200,
            "commit_reveal": {
                "available": False,
                "detail": "owner-reported but not yet verified through the integrated protocol source",
            },
        },
        "artifacts": {
            "bam": {
                "uri": "s3://x/input.bam",
                "sha256": _h("a"),
                "size_bytes": 100,
                "media_type": "application/x-bam",
            },
            "bai": {
                "uri": "s3://x/input.bam.bai",
                "sha256": _h("b"),
                "size_bytes": 10,
                "media_type": "application/x-bai",
            },
            "reference": {
                "uri": "s3://x/chr19.fa",
                "sha256": _h("c"),
                "size_bytes": 999,
                "media_type": "text/x-fasta",
            },
        },
        "parameter_space": {
            "caller": "gatk",
            "source": "qualification-fixture",
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
            "scorer_hash": _h("5"),
            "gatk_image_digest": "broadinstitute/gatk:4.5.0.0",
            "happy_image_digest": "genonet/hap-py@sha256:03ac",
            "reference_sha256": _h("c"),
        },
    }


def payload_missing_identity(identity: str = "scorer_hash") -> dict[str, Any]:
    """A payload with one required provenance identity removed (fail-closed probe)."""
    payload = copy.deepcopy(valid_raw_payload())
    payload["provenance"].pop(identity, None)
    return payload


def raw_response(payload: dict[str, Any] | None = None) -> RawProtocolResponse:
    """Wrap a payload in a ``RawProtocolResponse`` with a fixed retrieval time."""
    from minos_engine.protocol.client import RawProtocolResponse

    p = payload if payload is not None else valid_raw_payload()
    return RawProtocolResponse(
        payload=copy.deepcopy(p),
        retrieved_at="2026-08-17T12:00:00+00:00",
        source_endpoints={"round": "https://api.theminos.ai/v2/round-status"},
    )
