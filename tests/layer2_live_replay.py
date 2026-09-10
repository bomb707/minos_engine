"""A LIVE round driven end to end through the real authority chain.

Only the **transport** is substituted. A deterministic :class:`FixtureRoundStatusTransport` stands
in for the network, and its payload still goes through::

    observe_fixture_round_status          ->  FixtureRoundStatusReceipt
    accept_fixture_round_downloads        ->  FixtureRoundDownloads  (real files, really hashed)
    observe_fixture_live_round_intake     ->  FixtureLiveRoundIntake
    observe_fixture_live_profile_binding  ->  FixtureLiveProfileBinding
    observe_fixture_live_round_ownership  ->  FixtureRoundProfileAuthority

The parsing, canonicalization and every refusal are the production implementation -- the fixture
scope differs only in which authority token is minted, and a test asserts the two produce
identical parsed content. A fixture therefore cannot obtain the capability the production intake
accepts. Each stage mints a capability of its own domain, and the chain **ends** at
``FixtureRoundProfileAuthority`` -- which the safe controller refuses, because it is not the sealed
production ownership type. A fixture proves the logic; it never becomes the authority.

Everything else is genuine production code: ``tests.layer1_fixtures.build_dataset`` writes a real
BAM, index, reference and FAI; ``Layer1Service.analyze`` is the real profiler; and
``intake.attest_input`` is the real attestation producer, stream-hashing those same files and
matching them against the live intake's registered identity. The live round id is a fresh
timezone-aware timestamp that appears in no schedule.

The one other substitution is the accepted reference table: a synthetic genome cannot hash to the
real GRCh38 contig this engine is qualified against, so the test patches that module constant for
its contig. It is patched, never parameterized — production has no argument through which a
reference identity could be supplied.

No truth, no mutations, no scores.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from minos_engine.layer2.live_round_intake import (
    ACCEPTED_REFERENCE_IDENTITIES,
    ReferenceIdentity,
    live_dataset_id_for,
    observe_fixture_live_round_intake,
)
from minos_engine.protocol.round_status import (
    FixtureRoundStatusTransport,
    accept_fixture_round_downloads,
    observe_fixture_round_status,
)

#: The URLs the fixture round "offers". They are matched by exact string, exactly as production
#: matches the platform's real presigned URLs, and never enter any identity.
FIXTURE_BAM_URL = "https://fixture.invalid/round/input.bam?sig=FIXTURE"
FIXTURE_BAI_URL = "https://fixture.invalid/round/input.bam.bai?sig=FIXTURE"

#: A platform-shaped round id: a timezone-aware ISO-8601 timestamp, as `/v2/round-status` issues.
FRESH_LIVE_ROUND_ID = "2026-09-08T12:00:00+00:00"

#: A supported contig, so nothing is proved by picking one the engine would refuse anyway.
LIVE_CONTIG = "chr18"
CONTIG_LENGTH = 300_000

#: Sentinel for "this key is absent from the payload entirely".
ABSENT = object()


def round_status_payload(
    *, round_id: str = FRESH_LIVE_ROUND_ID, region: str, **override: Any
) -> dict[str, Any]:
    """The shape `POST /v2/round-status` returns, per the subnet client's documented contract.

    ``override`` may replace any key, including ``region``, or remove one with :data:`ABSENT`.
    """
    payload: dict[str, Any] = {
        "has_active_round": True,
        "round_id": round_id,
        "status": "open",
        "region": region,
        "bam_presigned_url": FIXTURE_BAM_URL,
        "bam_index_presigned_url": FIXTURE_BAI_URL,
        "num_mutations": 120,
        "downsampled_coverage": 30,
        "time_remaining_seconds": 1800,
    }
    payload.update(override)
    return {key: value for key, value in payload.items() if value is not ABSENT}


def build_live_dataset(tmp: Path, *, contig: str = LIVE_CONTIG) -> dict[str, Any]:
    """Real files and a real Layer 1 profile for them. No corpus, no schedule, no split."""
    from minos_engine.layer1.config import load_layer1_config
    from minos_engine.layer1.contracts import ProfileRequest, ProfileStatus
    from minos_engine.layer1.service import Layer1Service
    from tests.layer1_fixtures import build_dataset, simple_reads

    dataset = build_dataset(
        tmp,
        simple_reads(CONTIG_LENGTH, n_pairs=40),
        contig=contig,
        contig_len=CONTIG_LENGTH,
    )
    config = load_layer1_config()
    request = ProfileRequest(
        round_id=FRESH_LIVE_ROUND_ID,
        bam_path=str(dataset.bam),
        bai_path=str(dataset.bai),
        reference_path=str(dataset.reference),
        fai_path=str(dataset.fai),
        region_source=f"{contig}:1-{CONTIG_LENGTH}",
        region_coordinate_convention="one_based_inclusive",
        budget_seconds=120,
        cpu_limit=1,
        memory_limit_bytes=1_000_000_000,
        profiler_config_version=config.profiler_config_version,
        profiler_config_hash=config.config_hash,
    )
    result = Layer1Service(require_prerequisite=False).analyze(request, tmp / "artifacts")
    assert result.status is ProfileStatus.COMPLETE, result
    return {
        "contig": contig,
        "region": f"{contig}:1-{CONTIG_LENGTH}",
        "bam": dataset.bam,
        "bai": dataset.bai,
        "reference": dataset.reference,
        "fai": dataset.fai,
        "profile_bytes": Path(result.profile_path).read_bytes(),
        "manifest_bytes": Path(result.manifest_path).read_bytes(),
        "windows_bytes": Path(result.windows_path).read_bytes(),
    }


def accept_synthetic_reference(monkeypatch: Any, dataset: dict[str, Any]) -> ReferenceIdentity:
    """Point the accepted reference table at the synthetic genome, for this contig only.

    The table is a read-only mapping, so this REPLACES the module attribute with a different
    read-only table rather than editing the accepted one. That distinction is the point: a test
    may substitute what the module looks at, and no code -- test or otherwise -- may edit the
    accepted reference authority in place.
    """
    from minos_engine.common.frozen_state import frozen_map
    from minos_engine.common.hashing import sha256_hex
    from minos_engine.intake.attestation import compute_reference_contig_m5
    from minos_engine.layer2 import live_round_intake as intake_module

    reference = ReferenceIdentity(
        contig=dataset["contig"],
        reference_sha256=sha256_hex(Path(dataset["reference"]).read_bytes()),
        fai_sha256=sha256_hex(Path(dataset["fai"]).read_bytes()),
        reference_m5=compute_reference_contig_m5(Path(dataset["reference"]), dataset["contig"])[0],
    )
    substituted = frozen_map({**ACCEPTED_REFERENCE_IDENTITIES, dataset["contig"]: reference})
    monkeypatch.setattr(intake_module, "ACCEPTED_REFERENCE_IDENTITIES", substituted)
    return reference


def build_live_replay(
    dataset: dict[str, Any],
    *,
    live_round_id: str = FRESH_LIVE_ROUND_ID,
    payload_override: dict[str, Any] | None = None,
    attestation_override: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Transport -> receipt -> digests -> intake -> real attestation. Nothing is bypassed."""
    from minos_engine.intake.attestation import attest_input

    payload = round_status_payload(round_id=live_round_id, region=dataset["region"])
    if payload_override is not None:
        payload = {**payload, **payload_override}
        payload = {k: v for k, v in payload.items() if v is not ABSENT}
    receipt = observe_fixture_round_status(FixtureRoundStatusTransport(payload))

    downloads = accept_fixture_round_downloads(
        receipt=receipt,
        bam_source_url=FIXTURE_BAM_URL,
        bam_path=Path(dataset["bam"]).resolve(),
        bai_source_url=FIXTURE_BAI_URL,
        bai_path=Path(dataset["bai"]).resolve(),
    )
    intake = observe_fixture_live_round_intake(receipt=receipt, downloads=downloads)

    registry_record = {
        **intake.registry_identity(),
        "region_start0": int(intake.region_start0),
        "region_end0_exclusive": int(intake.region_end0_exclusive),
    }
    registry_record.pop("registry_snapshot_hash", None)
    attestation = json.loads(
        attest_input(
            bam_path=Path(dataset["bam"]),
            bai_path=Path(dataset["bai"]),
            reference_path=Path(dataset["reference"]),
            fai_path=Path(dataset["fai"]),
            registry_record=registry_record,
            registry_snapshot_hash=intake.identity,
        ).model_dump_json()
    )
    if attestation_override:
        from minos_engine.common.hashing import canonical_hash

        attestation = {**attestation, **attestation_override}
        if "attestation_hash" not in attestation_override:
            attestation["attestation_hash"] = canonical_hash(
                {k: v for k, v in sorted(attestation.items()) if k != "attestation_hash"}
            )
    return {
        "receipt": receipt,
        "payload": payload,
        "downloads": downloads,
        "intake": intake,
        "intake_content": intake.content(),
        "profile_bytes": dataset["profile_bytes"],
        "manifest_bytes": dataset["manifest_bytes"],
        "windows_bytes": dataset["windows_bytes"],
        "attestation": attestation,
        "attestation_bytes": json.dumps(attestation, sort_keys=True).encode(),
        "dataset_id": live_dataset_id_for(
            chromosome=dataset["contig"], identity_tuple_hash=str(intake.identity_tuple_hash)
        ),
        "dataset": dataset,
    }


def live_ownership(replay: dict[str, Any]) -> Any:
    """The FIXTURE terminus. It exercises every shared proof and mints no live authority."""
    from minos_engine.layer2.live_round_authority import observe_fixture_live_round_ownership

    return observe_fixture_live_round_ownership(
        intake=replay["intake"],
        profile_bytes=replay["profile_bytes"],
        manifest_bytes=replay["manifest_bytes"],
        attestation_bytes=replay["attestation_bytes"],
        windows_bytes=replay["windows_bytes"],
    )
