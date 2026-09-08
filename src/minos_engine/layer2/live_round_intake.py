"""``l2h-live-round-intake-v1`` — what LIVE challenge the engine is servicing.

**The upstream authority is not ours.** A live SN107 round is created by the platform, and the
miner learns about it from ``POST /v2/round-status`` (see ``minos_subnet/utils/platform_client.py``
and ``neurons/miner.py``), which returns ``round_id``, ``status``, ``region``,
``bam_presigned_url``, ``bam_index_presigned_url`` and timing. So ``round_id`` is **not invented
here**: it is the platform's, taken verbatim, and it is stable and replayable because the platform
reissues the same id for the same round.

Two consequences of that upstream shape matter:

* The platform's ``round_id`` is an **ISO-8601 timestamp** (``minos_subnet``'s own
  ``validate_round_id`` parses it with ``datetime.fromisoformat`` and caps it at 40 characters),
  not the 16-hex research round id the frozen TRAIN corpus uses. A live round identity is
  therefore a timestamp *as a matter of upstream fact*. That is not the same thing as putting
  wall-clock metadata into a scientific identity: what is excluded here is anything describing
  *this run* -- retrieval time, host, pid, path, URL -- and none of that appears. The round id is
  excluded from nothing precisely because two different rounds may present the same inputs, and
  without it a decision for one round could be replayed as a decision for another.
* The platform hands over **URLs, not content hashes**. Nothing upstream states what the BAM's
  bytes are. Identity is therefore computed locally from the downloaded bytes, which is what the
  intake producer already does, and it is those computed identities -- never a URL -- that this
  document binds.

**What this is not.** It is not an authorization list. A live round is admissible because its
inputs authenticate, not because it appears in some roster. Nothing here reads the TRAIN schedule,
the research split, VALIDATION or TEST.

``parameter_space_hash`` is deliberately absent. It defines what the controller may *do*, not what
the round *is*, and the controller already checks the request's parameter space against the
accepted live space. Binding it here would make the intake identity change when the parameter
space moved even though the inputs had not, and would break the profile binding for no reason.
"""

from __future__ import annotations

from typing import Any, Final

from minos_engine.common.canonical_json import canonical_json_bytes
from minos_engine.common.errors import MinosEngineError
from minos_engine.common.hashing import canonical_hash, sha256_hex

__all__ = [
    "LIVE_INTAKE_DOMAIN",
    "LIVE_INTAKE_FIELDS",
    "LIVE_INTAKE_SCHEMA",
    "LiveRoundIntakeError",
    "VerifiedLiveRoundIntake",
    "build_live_round_intake",
    "live_dataset_id_for",
    "live_round_intake_identity",
    "verify_live_round_intake",
]

LIVE_INTAKE_SCHEMA: Final = "l2h-live-round-intake-v1"
LIVE_INTAKE_DOMAIN: Final = "minos:l2h-live-round-intake:v1\n"

#: The CLOSED content set. An unknown key is a refusal, not an ignored extra: a document that
#: carries fields this version does not understand has not been understood.
LIVE_INTAKE_FIELDS: Final[tuple[str, ...]] = (
    "bai_sha256",
    "bam_sha256",
    "chromosome",
    "fai_sha256",
    "identity_tuple_hash",
    "reference_sha256",
    "region_coordinate_system",
    "region_end0_exclusive",
    "region_hash",
    "region_length_bp",
    "region_source",
    "region_start0",
    "round_id",
    "schema_version",
)

_HEX64: Final = frozenset("0123456789abcdef")
#: The chromosomes this engine profiles. A live round outside them is refused rather than guessed.
SUPPORTED_CONTIGS: Final[tuple[str, ...]] = ("chr18", "chr19", "chr20", "chr21", "chr22")


class LiveRoundIntakeError(MinosEngineError):
    """A live round could not be authenticated. No profile, no ownership, no decision."""


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise LiveRoundIntakeError(message)


def _hex64(value: Any, name: str) -> str:
    text = str(value)
    _require(
        len(text) == 64 and set(text) <= _HEX64,
        f"{name} must be 64 lowercase hex characters, got {value!r}",
    )
    return text


def _valid_round_id(value: Any) -> str:
    """The shared round-identifier rule, raised as a live-intake refusal."""
    from minos_engine.common.genomic_region import validate_round_identifier

    try:
        return validate_round_identifier(value)
    except ValueError as error:
        raise LiveRoundIntakeError(
            f"{error}; the platform issues the round id and this engine does not invent its own"
        ) from None


def live_dataset_id_for(*, chromosome: str, identity_tuple_hash: str) -> str:
    """A content-addressed dataset identity for a live round.

    A live round has no registered research dataset behind it -- there is no
    ``catalog.dataset_registry`` row, no split allocation, no epoch. What it does have is an exact
    input set, so the dataset identity is derived from that and named ``live-`` so it can never be
    mistaken for a registry-backed research dataset id such as ``minos-chr21-0279a3b8042f848b``.
    """
    return f"live-{chromosome}-{identity_tuple_hash[:16]}"


def live_round_intake_identity(content: dict[str, Any]) -> str:
    """Domain-separated identity over the canonical intake content."""
    return sha256_hex(LIVE_INTAKE_DOMAIN.encode("utf-8") + canonical_json_bytes(content))


def build_live_round_intake(
    *,
    round_id: str,
    region_source: str,
    region_coordinate_system: str = "one_based_inclusive",
    bam_sha256: str,
    bai_sha256: str,
    reference_sha256: str,
    fai_sha256: str,
) -> dict[str, Any]:
    """Canonicalize one live round's inputs into the intake document.

    ``region_source`` is the platform's own region string (``chr20:45000000-50000000``) parsed
    once, under an explicit convention, by the shared rule a unit test holds to
    ``intake.contracts.Region`` -- so the coordinate system is stated rather than assumed and an
    ambiguous region is a hard failure. Layer 2 may not import ``intake``, which is why the rule
    lives in ``common`` rather than being reached for across the boundary.
    """
    from minos_engine.common.genomic_region import normalize_region
    from minos_engine.layer2.split.contracts import region_hash_for

    try:
        contig, start0, end0_exclusive = normalize_region(region_source, region_coordinate_system)
    except ValueError as error:
        raise LiveRoundIntakeError(f"the live region is not usable: {error}") from None
    _require(
        contig in SUPPORTED_CONTIGS,
        f"contig {contig!r} is not one this engine profiles {SUPPORTED_CONTIGS}",
    )
    hashes = {
        "bam_sha256": _hex64(bam_sha256, "bam_sha256"),
        "bai_sha256": _hex64(bai_sha256, "bai_sha256"),
        "reference_sha256": _hex64(reference_sha256, "reference_sha256"),
        "fai_sha256": _hex64(fai_sha256, "fai_sha256"),
    }
    region_hash = region_hash_for(contig, start0, end0_exclusive)
    # exactly the tuple the L2-D attestation recomputes, so the two can never disagree silently
    identity_tuple_hash = canonical_hash({**hashes, "region_hash": region_hash})
    return {
        "schema_version": LIVE_INTAKE_SCHEMA,
        "round_id": _valid_round_id(round_id),
        "chromosome": contig,
        "region_source": region_source.strip(),
        "region_coordinate_system": region_coordinate_system,
        "region_start0": start0,
        "region_end0_exclusive": end0_exclusive,
        "region_length_bp": end0_exclusive - start0,
        "region_hash": region_hash,
        "identity_tuple_hash": identity_tuple_hash,
        **hashes,
    }


class VerifiedLiveRoundIntake:
    """Proof that a live round's inputs are internally consistent and canonically identified.

    Minted only by :func:`verify_live_round_intake`. A caller cannot construct one: a dictionary
    that merely has the right keys has been checked against nothing.
    """

    __slots__ = ("_content", "dataset_id", "identity")

    def __init__(self, token: object, *, content: dict[str, Any], identity: str) -> None:
        if token is not _INTAKE_TOKEN:
            raise LiveRoundIntakeError(
                "a live round intake may only be minted by the verifying factory; a dictionary "
                "has not been verified against anything"
            )
        self._content = dict(content)
        self.identity = identity
        self.dataset_id = live_dataset_id_for(
            chromosome=str(content["chromosome"]),
            identity_tuple_hash=str(content["identity_tuple_hash"]),
        )

    def __getattr__(self, name: str) -> Any:
        try:
            return self._content[name]
        except KeyError:
            raise AttributeError(name) from None

    def content(self) -> dict[str, Any]:
        return dict(self._content)

    def registry_identity(self) -> dict[str, Any]:
        """The registered identity an attestation for this round must match.

        For a research round that identity comes from ``catalog.dataset_registry`` and the
        attestation cites the registry snapshot it was matched against. A live round has no
        registry, and the document that registers what its inputs are is this intake -- so the
        intake's own identity takes that role. The field keeps its exact meaning, "the identity of
        the registered input set this attestation was checked against"; only which document
        registers it is different.
        """
        return {
            "dataset_id": self.dataset_id,
            "round_id": str(self._content["round_id"]),
            "chromosome": str(self._content["chromosome"]),
            "bam_sha256": str(self._content["bam_sha256"]),
            "bai_sha256": str(self._content["bai_sha256"]),
            "reference_sha256": str(self._content["reference_sha256"]),
            "fai_sha256": str(self._content["fai_sha256"]),
            "region_hash": str(self._content["region_hash"]),
            "identity_tuple_hash": str(self._content["identity_tuple_hash"]),
            "registry_snapshot_hash": self.identity,
        }


_INTAKE_TOKEN: Final = object()


def verify_live_round_intake(content: Any) -> VerifiedLiveRoundIntake:
    """Re-derive the whole document from its own inputs, then mint the capability.

    Nothing is believed for being present. The region is re-parsed and re-hashed, the identity
    tuple is recomputed, the schema is required to be exactly this version, and the key set must
    be exactly the closed set -- a missing field and an unknown field are both refusals.
    """
    _require(isinstance(content, dict) and bool(content), "a live intake must be a non-empty map")
    assert isinstance(content, dict)
    observed = tuple(sorted(content))
    _require(
        observed == LIVE_INTAKE_FIELDS,
        f"the live intake carries {observed}, not exactly {LIVE_INTAKE_FIELDS}",
    )
    _require(
        content["schema_version"] == LIVE_INTAKE_SCHEMA,
        f"{content['schema_version']!r} is not {LIVE_INTAKE_SCHEMA}",
    )
    rebuilt = build_live_round_intake(
        round_id=content["round_id"],
        region_source=content["region_source"],
        region_coordinate_system=content["region_coordinate_system"],
        bam_sha256=content["bam_sha256"],
        bai_sha256=content["bai_sha256"],
        reference_sha256=content["reference_sha256"],
        fai_sha256=content["fai_sha256"],
    )
    _require(
        canonical_json_bytes(rebuilt) == canonical_json_bytes(content),
        "the live intake does not re-derive from its own inputs; a declared region, region hash "
        "or identity tuple that disagrees with the bytes it describes is not evidence",
    )
    return VerifiedLiveRoundIntake(
        _INTAKE_TOKEN, content=rebuilt, identity=live_round_intake_identity(rebuilt)
    )
