"""Build immutable RoundProtocolSnapshot / RoundContext from a raw payload.

This is the only place raw protocol data becomes a parsed, validated, immutable
contract. It fails closed: any missing required identity or incomplete legal
parameter state raises a typed error and no snapshot is produced.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from minos_engine.common.errors import SnapshotIncompleteError
from minos_engine.intake.artifact_identity import (
    VerificationStrength,
    build_artifact_identity,
)
from minos_engine.intake.contracts import Region

from .contracts import (
    CommitRevealState,
    RoundContext,
    RoundProtocolSnapshot,
    RoundStatus,
)
from .parameter_ranges import parse_parameter_space

if TYPE_CHECKING:
    from .client import RawProtocolResponse

__all__ = ["build_snapshot", "build_round_context"]


def _require(payload: dict[str, Any], key: str) -> Any:
    if key not in payload:
        raise SnapshotIncompleteError(f"protocol payload missing required section {key!r}")
    return payload[key]


def _require_str(section: dict[str, Any], key: str, where: str) -> str:
    value = section.get(key)
    if not isinstance(value, str) or not value.strip():
        raise SnapshotIncompleteError(f"missing required identity {where}.{key}")
    return value.strip()


def _round_status(value: Any) -> RoundStatus:
    try:
        return RoundStatus(value)
    except ValueError:
        return RoundStatus.UNKNOWN


def build_snapshot(raw: RawProtocolResponse) -> RoundProtocolSnapshot:
    payload = raw.payload
    round_section = _require(payload, "round")
    provenance = _require(payload, "provenance")
    parameter_space_raw = _require(payload, "parameter_space")
    network_config_raw = payload.get("network_config", {})

    stale = bool(payload.get("stale", False))

    region = Region.from_source(
        _require_str(round_section, "region", "round"),
        round_section.get("coordinate_system", "one_based_inclusive"),
        verified=False,
    )

    parameter_space = parse_parameter_space(
        parameter_space_raw, retrieved_at=raw.retrieved_at, stale=stale
    )

    commit_reveal_raw = round_section.get("commit_reveal") or {}
    commit_reveal = CommitRevealState(
        available=bool(commit_reveal_raw.get("available", False)),
        enabled=commit_reveal_raw.get("enabled"),
        phase=commit_reveal_raw.get("phase"),
        detail=commit_reveal_raw.get("detail"),
    )

    return RoundProtocolSnapshot(
        retrieved_at=raw.retrieved_at,
        round_id=_require_str(round_section, "round_id", "round"),
        round_status=_round_status(round_section.get("status")),
        exact_region=region,
        deadline_at=_require_str(round_section, "deadline_at", "round"),
        commit_reveal_state=commit_reveal,
        parameter_ranges_raw=parameter_space_raw,
        parameter_space_hash=parameter_space.parameter_space_hash,
        network_config_raw=network_config_raw,
        minos_upstream_commit=_require_str(provenance, "minos_upstream_commit", "provenance"),
        scorer_hash=_require_str(provenance, "scorer_hash", "provenance"),
        gatk_image_digest=_require_str(provenance, "gatk_image_digest", "provenance"),
        happy_image_digest=_require_str(provenance, "happy_image_digest", "provenance"),
        reference_sha256=_require_str(provenance, "reference_sha256", "provenance"),
        source_endpoints=dict(raw.source_endpoints),
        stale=stale,
    )


def _artifact(section: dict[str, Any], name: str, observed_at: str) -> Any:
    return build_artifact_identity(
        uri=_require_str(section, "uri", name),
        sha256=_require_str(section, "sha256", name),
        size_bytes=int(section["size_bytes"]),
        media_type=_require_str(section, "media_type", name),
        observed_at=section.get("observed_at", observed_at),
        strength=VerificationStrength.DECLARED,
    )


def build_round_context(raw: RawProtocolResponse, snapshot: RoundProtocolSnapshot) -> RoundContext:
    payload = raw.payload
    artifacts = _require(payload, "artifacts")
    round_section = _require(payload, "round")
    time_remaining = float(round_section.get("time_remaining_seconds", 0.0))
    return RoundContext(
        round_id=snapshot.round_id,
        status=snapshot.round_status,
        exact_region=snapshot.exact_region,
        time_remaining_seconds=time_remaining,
        bam_artifact=_artifact(_require(artifacts, "bam"), "bam", raw.retrieved_at),
        bai_artifact=_artifact(_require(artifacts, "bai"), "bai", raw.retrieved_at),
        reference_artifact=_artifact(
            _require(artifacts, "reference"), "reference", raw.retrieved_at
        ),
        protocol_snapshot_id=snapshot.snapshot_id,
    )
