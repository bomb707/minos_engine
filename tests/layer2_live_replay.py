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
        #: the temp root this dataset was built in, so a production-equivalent miner stand-in has
        #: somewhere of its own to write
        "root": tmp,
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


# --------------------------------------------------------------------------- #
# PRODUCTION-EQUIVALENT SEAM
#
# Everything above this line is the FIXTURE domain: it ends in
# `FixtureRoundProfileAuthority` and can never mint live authority.
#
# What follows drives the REAL production verifiers -- `verify_production_round_status`,
# `download_production_round_inputs`, `verify_live_round_intake`, `verify_live_profile_binding`,
# `own_verified_live_round` -- and therefore produces a genuine sealed
# `VerifiedRoundProfileAuthority(scope="live")`.
#
# The ONLY substitutions are the two official type resolvers, monkeypatched to stand-in classes
# that mirror the upstream contract, plus the accepted reference table (a synthetic genome cannot
# hash to real GRCh38). NO private mint token is imported and no capability is hand-built: every
# seal here was earned by passing the production verifier that owns it. That is what makes this a
# production-EQUIVALENT seam rather than a white-box forgery.
#
# It is still a test seam, and it is not qualification evidence.
# --------------------------------------------------------------------------- #
PRODUCTION_BASE_URL = "https://platform.example"
PRODUCTION_HOTKEY = "5FakeHotkeyAddressForTests"


class StandInOfficialClient:
    """Mirrors ``utils.platform_client.MinerPlatformClient`` where the engine touches it.

    ``_round_status_path`` is a property over the live ``self.demo``, and ``config`` is mutable --
    exactly as upstream, so the client TOCTOU checks are genuinely exercised rather than bypassed.
    """

    def __init__(self, payload: dict[str, Any]) -> None:
        import types

        self._payload = dict(payload)
        self.keypair = types.SimpleNamespace(ss58_address=PRODUCTION_HOTKEY)
        self.config = types.SimpleNamespace(base_url=PRODUCTION_BASE_URL)
        self.demo = False

    @property
    def _round_status_path(self) -> str:
        return "/v2/demo/round-status" if self.demo else "/v2/round-status"

    async def get_round_status(self) -> dict[str, Any]:
        return dict(self._payload)


class StandInOfficialMiner:
    """Mirrors ``neurons.miner.Miner._download_bam``: writes into its own per-round output dir.

    The engine does not reimplement downloading; it hands the round's operational data over and
    hashes exactly what came back. This stand-in does the same thing with local bytes.
    """

    def __init__(self, root: Any, *, bam: Any, bai: Any) -> None:
        self.root = Path(root)
        self._bam = Path(bam)
        self._bai = Path(bai)

    def _download_bam(self, round_data: dict[str, Any], round_id: str) -> str:
        import shutil

        out = self.root / "output" / str(round_id).replace(":", "-")
        out.mkdir(parents=True, exist_ok=True)
        target = out / "input.bam"
        shutil.copyfile(self._bam, target)
        shutil.copyfile(self._bai, Path(str(target) + ".bai"))
        return str(target)


def build_production_live_round(
    monkeypatch: Any,
    dataset: dict[str, Any],
    *,
    live_round_id: str = FRESH_LIVE_ROUND_ID,
    payload_override: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Drive the PRODUCTION chain end to end and return a real live ownership authority.

    receipt -> downloads -> intake -> real L1 attestation -> binding -> ownership, every step
    through the production verifier that owns it.
    """
    import json as _json

    from minos_engine.common.hashing import sha256_hex
    from minos_engine.intake.attestation import attest_input
    from minos_engine.layer2.live_round_authority import verify_live_profile_binding
    from minos_engine.layer2.live_round_intake import verify_live_round_intake
    from minos_engine.layer2.round_profile_authority import own_verified_live_round
    from minos_engine.protocol import round_status as rs

    accept_synthetic_reference(monkeypatch, dataset)

    payload = round_status_payload(round_id=live_round_id, region=dataset["region"])
    # the platform's published BAM digest, which upstream documents as optional; supplying it
    # exercises the authoritative-content branch of the cache-provenance rule
    payload["bam_sha256"] = sha256_hex(Path(dataset["bam"]).read_bytes())
    if payload_override is not None:
        payload = {**payload, **payload_override}
        payload = {k: v for k, v in payload.items() if v is not ABSENT}

    monkeypatch.setattr(rs, "resolve_official_platform_client_type", lambda: StandInOfficialClient)
    monkeypatch.setattr(rs, "resolve_official_miner_type", lambda: StandInOfficialMiner)

    client = StandInOfficialClient(payload)
    transport = rs.production_round_status_transport(client)
    receipt = rs.verify_production_round_status(transport)

    miner = StandInOfficialMiner(
        Path(dataset["root"]) / f"miner-{live_round_id.replace(':', '-')}",
        bam=dataset["bam"],
        bai=dataset["bai"],
    )
    downloads = rs.download_production_round_inputs(receipt=receipt, miner=miner)
    intake = verify_live_round_intake(receipt=receipt, downloads=downloads)

    registry_record = {
        **intake.registry_identity(),
        "region_start0": int(intake.region_start0),
        "region_end0_exclusive": int(intake.region_end0_exclusive),
    }
    registry_record.pop("registry_snapshot_hash", None)
    attestation = _json.loads(
        attest_input(
            bam_path=Path(dataset["bam"]),
            bai_path=Path(dataset["bai"]),
            reference_path=Path(dataset["reference"]),
            fai_path=Path(dataset["fai"]),
            registry_record=registry_record,
            registry_snapshot_hash=intake.identity,
        ).model_dump_json()
    )
    binding = verify_live_profile_binding(
        intake=intake,
        profile_bytes=dataset["profile_bytes"],
        manifest_bytes=dataset["manifest_bytes"],
        attestation_bytes=_json.dumps(attestation, sort_keys=True).encode(),
        windows_bytes=dataset["windows_bytes"],
    )
    return {
        "client": client,
        "receipt": receipt,
        "downloads": downloads,
        "intake": intake,
        "binding": binding,
        "ownership": own_verified_live_round(binding),
        "payload": payload,
        "dataset": dataset,
    }
