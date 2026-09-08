"""Build a LIVE round from approved TRAIN input-side material only.

The point of a live replay is to prove that a round which is **not** in the frozen fifty-row TRAIN
schedule can still become authoritative, using nothing but what production actually has: the input
bytes' identities, a region, and a platform-issued round id.

What is taken from the TRAIN corpus is input-side only -- the BAM/BAI/reference/FAI content
hashes, the region, and the Layer 1 outputs those inputs produce. Layer 1 mints
``profile_id = canonical_hash({bam_sha256, region, config_hash, profiler_version})[:32]``, which
depends on no schedule, no split and no round id, so the same inputs give the same profile whether
they arrive as a research round or as a live challenge. That is what makes reusing them a replay
rather than a fabrication.

No truth, no mutations, no scores and no outcome is read, and the round's TRAIN result is never
consulted.
"""

from __future__ import annotations

import json
from typing import Any

from minos_engine.common.hashing import canonical_hash
from minos_engine.layer2.live_round_intake import (
    build_live_round_intake,
    live_dataset_id_for,
    verify_live_round_intake,
)
from minos_engine.layer2.round_profile_authority import PROFILE_CORPUS_ROOT

#: A platform-shaped round id: an ISO-8601 timestamp, exactly as `/v2/round-status` issues.
FRESH_LIVE_ROUND_ID = "2026-09-08T12:00:00+00:00"

PROFILE_DOCUMENT = "bam-profile-v1.json"
MANIFEST_DOCUMENT = "profile-manifest-v1.json"
ATTESTATION_DOCUMENT = "input-integrity-attestation-v1.json"
WINDOWS_ARTIFACT = "window-profile-v1.parquet"


def corpus_available(round_id: str) -> bool:
    return (PROFILE_CORPUS_ROOT / round_id / PROFILE_DOCUMENT).is_file()


def _read(round_id: str) -> dict[str, bytes]:
    directory = PROFILE_CORPUS_ROOT / round_id
    return {
        name: (directory / name).read_bytes()
        for name in (PROFILE_DOCUMENT, MANIFEST_DOCUMENT, ATTESTATION_DOCUMENT, WINDOWS_ARTIFACT)
    }


def reissue_attestation(template: dict[str, Any], *, intake: Any) -> dict[str, Any]:
    """Re-issue an attestation bound to a LIVE intake instead of a research registry snapshot.

    Only the *registration* changes -- round id, dataset id, and the identity of the document the
    inputs are registered in. Every input identity is carried over unchanged, which is the whole
    point: these are the same bytes, arriving as a live challenge.
    """
    content = {key: value for key, value in template.items() if key != "attestation_hash"}
    content["round_id"] = str(intake.round_id)
    content["dataset_id"] = intake.dataset_id
    content["chromosome"] = str(intake.chromosome)
    content["registry_snapshot_hash"] = intake.identity
    content["bam_sha256"] = str(intake.bam_sha256)
    content["bai_sha256"] = str(intake.bai_sha256)
    content["reference_sha256"] = str(intake.reference_sha256)
    content["fai_sha256"] = str(intake.fai_sha256)
    content["region_hash"] = str(intake.region_hash)
    content["identity_tuple_hash"] = str(intake.identity_tuple_hash)
    return {**content, "attestation_hash": canonical_hash(dict(sorted(content.items())))}


def build_live_replay(
    source_round_id: str,
    *,
    live_round_id: str = FRESH_LIVE_ROUND_ID,
    intake_override: dict[str, Any] | None = None,
    attestation_override: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """A complete, self-consistent live round: verified intake plus the four L1 artifacts."""
    raw = _read(source_round_id)
    manifest = json.loads(raw[MANIFEST_DOCUMENT])
    template = json.loads(raw[ATTESTATION_DOCUMENT])

    contig = str(manifest["region_contig"])
    start0 = int(manifest["region_start0"])
    end0 = int(manifest["region_end0"])
    content = build_live_round_intake(
        round_id=live_round_id,
        # the platform's own one-based inclusive region string
        region_source=f"{contig}:{start0 + 1}-{end0}",
        bam_sha256=str(template["bam_sha256"]),
        bai_sha256=str(template["bai_sha256"]),
        reference_sha256=str(template["reference_sha256"]),
        fai_sha256=str(template["fai_sha256"]),
    )
    if intake_override:
        content = {**content, **intake_override}
    intake = verify_live_round_intake(content)

    attestation = reissue_attestation(template, intake=intake)
    if attestation_override:
        attestation = {
            **attestation,
            **attestation_override,
        }
        if "attestation_hash" not in attestation_override:
            attestation["attestation_hash"] = canonical_hash(
                {k: v for k, v in sorted(attestation.items()) if k != "attestation_hash"}
            )
    return {
        "intake": intake,
        "intake_content": content,
        "profile_bytes": raw[PROFILE_DOCUMENT],
        "manifest_bytes": raw[MANIFEST_DOCUMENT],
        "attestation_bytes": json.dumps(attestation, sort_keys=True).encode(),
        "attestation": attestation,
        "windows_bytes": raw[WINDOWS_ARTIFACT],
        "dataset_id": live_dataset_id_for(
            chromosome=contig, identity_tuple_hash=str(content["identity_tuple_hash"])
        ),
    }


def live_ownership(replay: dict[str, Any]) -> Any:
    from minos_engine.layer2.live_round_authority import load_verified_live_round_ownership

    return load_verified_live_round_ownership(
        intake=replay["intake"],
        profile_bytes=replay["profile_bytes"],
        manifest_bytes=replay["manifest_bytes"],
        attestation_bytes=replay["attestation_bytes"],
        windows_bytes=replay["windows_bytes"],
    )


def live_request(replay: dict[str, Any], *, authority: Any, mode: Any = None) -> Any:
    """A DecisionRequest for the live round, shaped exactly like the qualification's."""
    from minos_engine.layer2.contracts import ControlMode
    from minos_engine.layer2.safe_controller_qualification import _request_for

    ownership = replay["ownership"]
    owned = ownership.owned(str(replay["intake"].round_id))
    return _request_for(owned, authority=authority, mode=mode or ControlMode.SAFE_BASELINE)
