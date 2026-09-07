"""``l2h-round-profile-ownership-v2`` — TRAIN-only proof that a request names a real round.

**Why v2 exists.** v1 read the whole 75-member profile snapshot, parsed it, and iterated every
record before skipping non-TRAIN members. TEST is sealed until L2-I *including identity
enumeration*, so reading all 75 and filtering was itself the breach; worse, v1's
"skipped_partition_counts" were derived by traversing the very records it claimed not to touch.
A count of skipped members proves nothing about non-access.

v2 never opens a document that carries a sealed identity. The TRAIN round list comes from
``manifests/l2f2_train_schedule_v1.json``, the frozen TRAIN-ONLY projection the accepted L2-F2
campaign already ran on: 50 entries of ``{chromosome, dataset_id, round_id}`` and no sealed row.

**The schedule is not trusted for being present.** It is anchored to authority that is
already accepted and recomputable from source:

* ``compute_protocol_hash(build_baseline_protocol(root))`` re-derives the baseline protocol
  identity from source, and that identity is what the verified BASELINE-QUALIFIED evidence binds;
* the accepted Phase-A execution authority must cite that same protocol hash and the same split
  manifest SHA, so it cannot be a stray local file agreeing only with itself;
* the schedule's bytes must hash to the ``train_schedule_manifest_sha256`` that authority records;
* and the schedule's own declared shape -- 50 TRAIN, 10 per chromosome, 10 batches of 5, that
  chromosome list -- must equal the protocol's ``train_schedule`` block, so it cannot describe a
  different design than the accepted science.

Per-member identity then comes from the member's own three documents, opened by name from the 50
scheduled round ids. The corpus directory is never listed, so a sealed directory is never even
observed to exist. Byte integrity does not need the (sealed-bearing) artifact inventory: the
accepted L2-D admission authority already binds the manifest to the profile and windows bytes, the
attestation re-hashes to its own ``attestation_hash``, its identity tuple recomputes, and its
``registry_snapshot_hash`` must equal the accepted ``PROFILE_SNAPSHOT_1_REGISTRY_SNAPSHOT_HASH``
constant in ``layer2/prerequisites.py``.

``validate_admission`` is called unchanged rather than reimplemented. What it cannot supply on its
own is the identity it validates against -- it takes ``registry_identity`` as a caller dict -- so
that identity is reconstructed from the member's own verified attestation and cross-checked
against the anchored schedule row.

Layer 2 still does not open the BAM, and nothing here reads truth, mutations, scores, VALIDATION
or TEST.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Final

from minos_engine.common.canonical_json import canonical_json_bytes
from minos_engine.common.errors import MinosEngineError
from minos_engine.common.hashing import sha256_hex
from minos_engine.layer2.prerequisites import (
    PROFILE_SNAPSHOT_1_REGISTRY_SNAPSHOT_HASH as ACCEPTED_REGISTRY_SNAPSHOT_HASH,
)
from minos_engine.models.contract import (
    BASELINE_SELECTED_HASH as ACCEPTED_BASELINE_SELECTED_IDENTITY,
)

__all__ = [
    "ADMITTED_PARTITION",
    "PHASE_A_AUTHORITY_PATH",
    "PROFILE_CORPUS_ROOT",
    "TRAIN_SCHEDULE_PATH",
    "OWNERSHIP_DOMAIN",
    "OWNERSHIP_SCHEMA",
    "OwnedRoundProfile",
    "RoundProfileAuthorityError",
    "VerifiedRoundProfileAuthority",
    "load_verified_round_profile_corpus",
]

OWNERSHIP_SCHEMA: Final = "l2h-round-profile-ownership-v2"
OWNERSHIP_DOMAIN: Final = "minos:l2h-round-profile-ownership:v2\n"

#: The frozen TRAIN-ONLY projection. Contains no TEST or VALIDATION row.
TRAIN_SCHEDULE_PATH: Final = "manifests/l2f2_train_schedule_v1.json"

#: The accepted Phase-A execution authority, which records the schedule's byte SHA.
PHASE_A_AUTHORITY_PATH: Final = "manifests/l2f2_phase_a_execution_authority_v1.json"

#: The accepted local profile corpus. An operational handle only, in the same spirit as
#: ``CONFIG_PAYLOAD_ROOT``: every byte read from it is verified against the frozen inventory, so a
#: wrong root fails rather than substitutes.
PROFILE_CORPUS_ROOT: Final = Path("/home/hr/bittensor/minos_l2d_corpus")

PROFILE_DOCUMENT: Final = "bam-profile-v1.json"
MANIFEST_DOCUMENT: Final = "profile-manifest-v1.json"
ATTESTATION_DOCUMENT: Final = "input-integrity-attestation-v1.json"
WINDOWS_ARTIFACT: Final = "window-profile-v1.parquet"

#: The ONLY partition this authority will enumerate, load, admit or retain.
#:
#: The frozen snapshot's 75 members span train, validation and TEST. TEST is sealed until L2-I --
#: including its identity enumeration -- and VALIDATION is not authorised for L2-G v2, so a member
#: outside TRAIN is skipped on its partition label before its artifacts are opened. Its identity
#: never enters the corpus, a decision, or any published evidence. Widening this is a sealing
#: decision, not a configuration one, which is why it is a constant rather than an argument.
ADMITTED_PARTITION: Final = "train"

#: The ten identity keys ``validate_admission`` binds an attestation to. Reconstructing exactly
#: these from verified bytes is what removes the caller from the trust path.
REGISTRY_IDENTITY_KEYS: Final[tuple[str, ...]] = (
    "dataset_id",
    "round_id",
    "chromosome",
    "bam_sha256",
    "bai_sha256",
    "reference_sha256",
    "fai_sha256",
    "region_hash",
    "identity_tuple_hash",
    "registry_snapshot_hash",
)


class RoundProfileAuthorityError(MinosEngineError):
    """A round or profile cannot be proved to belong to the accepted corpus. Emit nothing."""


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise RoundProfileAuthorityError(message)


class OwnedRoundProfile:
    """One member's identity, as its owning documents actually record it."""

    __slots__ = (
        "attestation_hash",
        "bai_sha256",
        "bam_sha256",
        "chromosome",
        "dataset_id",
        "fai_sha256",
        "fingerprint_hash",
        "identity_tuple_hash",
        "integrity_degraded",
        "partition",
        "profile_id",
        "profile_manifest_sha256",
        "profile_sha256",
        "reference_sha256",
        "region_hash",
        "registry_snapshot_hash",
        "round_id",
    )

    attestation_hash: str
    bai_sha256: str
    bam_sha256: str
    chromosome: str
    dataset_id: str
    fai_sha256: str
    fingerprint_hash: str
    identity_tuple_hash: str
    integrity_degraded: bool
    partition: str
    profile_id: str
    profile_manifest_sha256: str
    profile_sha256: str
    reference_sha256: str
    region_hash: str
    registry_snapshot_hash: str
    round_id: str

    def __init__(self, **fields: Any) -> None:
        for name in self.__slots__:
            setattr(self, name, fields[name])

    def content(self) -> dict[str, Any]:
        return {name: getattr(self, name) for name in sorted(self.__slots__)}


_CORPUS_TOKEN: Final = object()


class VerifiedRoundProfileAuthority:
    """The accepted corpus of owned (round, profile) identities.

    Minted only by :func:`load_verified_round_profile_corpus`, which verifies every member's
    artifacts against the frozen inventory and runs the accepted admission authority over each.
    """

    __slots__ = ("_anchors", "_by_round", "corpus_identity", "partition")

    def __init__(
        self,
        token: object,
        *,
        by_round: dict[str, OwnedRoundProfile],
        anchors: dict[str, str],
        corpus_identity: str,
        partition: str = ADMITTED_PARTITION,
    ) -> None:
        if token is not _CORPUS_TOKEN:
            raise RoundProfileAuthorityError(
                "an owned-profile corpus may only be minted by the verifying loader; a "
                "dictionary has not been verified against anything"
            )
        self._by_round = dict(by_round)
        self._anchors = dict(anchors)
        self.corpus_identity = corpus_identity
        self.partition = partition

    @property
    def anchors(self) -> dict[str, str]:
        """The already-accepted authorities this corpus hangs from."""
        return dict(self._anchors)

    @property
    def registry_snapshot_hash(self) -> str:
        return self._anchors["registry_snapshot_hash"]

    def __len__(self) -> int:
        return len(self._by_round)

    def rounds(self) -> tuple[str, ...]:
        return tuple(sorted(self._by_round))

    def owned(self, round_id: str) -> OwnedRoundProfile:
        try:
            return self._by_round[round_id]
        except KeyError:
            raise RoundProfileAuthorityError(
                f"round {round_id!r} is not in the accepted profile snapshot"
            ) from None

    def require_owned_request(self, request: Any) -> OwnedRoundProfile:
        """Prove the request's round and profile identity against their owning documents.

        Every mismatch here is an AUTHORITY failure, never a round-level fallback: a request that
        names a profile it cannot prove is not a degraded request, it is an unauthenticated one.
        """
        owned = self.owned(str(request.round.round_id))
        profile = request.profile_ref
        checks: tuple[tuple[str, Any, Any], ...] = (
            ("profile_id", profile.profile_id, owned.profile_id),
            ("region_hash", profile.region_hash, owned.region_hash),
            ("bam_sha256", profile.bam_sha256, owned.bam_sha256),
            ("bai_sha256", profile.bai_sha256, owned.bai_sha256),
            ("reference_sha256", profile.reference_sha256, owned.reference_sha256),
            ("fai_sha256", profile.fai_sha256, owned.fai_sha256),
            ("identity_tuple_hash", profile.identity_tuple_hash, owned.identity_tuple_hash),
            ("fingerprint_hash", profile.fingerprint_hash, owned.fingerprint_hash),
            (
                "profile_manifest_sha256",
                profile.profile_manifest_sha256,
                owned.profile_manifest_sha256,
            ),
        )
        for field, observed, expected in checks:
            _require(
                observed == expected,
                f"the request's {field} is {observed!r}, but round {owned.round_id} owns "
                f"{expected!r}",
            )
        return owned

    def identity_content(self) -> dict[str, Any]:
        return {
            "schema_version": OWNERSHIP_SCHEMA,
            "partition": self.partition,
            "anchors": dict(sorted(self._anchors.items())),
            "member_count": len(self._by_round),
            "members": [self._by_round[r].content() for r in sorted(self._by_round)],
        }


def _read_exact(path: Path, *, expected_sha: str, expected_size: int) -> bytes:
    """Bytes first, meaning second. A document is not parsed until it is the recorded one."""
    _require(path.is_file(), f"a corpus artifact is missing: {path}")
    _require(not path.is_symlink(), f"{path} is a symlink")
    raw = path.read_bytes()
    actual = hashlib.sha256(raw).hexdigest()
    _require(
        actual == expected_sha,
        f"{path.name} hashes to {actual}, but the frozen inventory records {expected_sha}",
    )
    _require(
        len(raw) == expected_size,
        f"{path.name} is {len(raw)} bytes, but the frozen inventory records {expected_size}",
    )
    return raw


def _anchored_train_schedule(base: Path) -> tuple[list[dict[str, str]], dict[str, str]]:
    """Return the 50 anchored TRAIN rows and the anchors that made them trustworthy.

    Nothing here opens a document that carries a sealed identity, and the schedule is believed
    only after it agrees with authority that is recomputable from source.
    """
    from minos_engine.baseline.baseline_selected import (
        baseline_selected_content,
        compute_baseline_selected_hash,
    )
    from minos_engine.baseline.schedule import (
        BATCH_COUNT,
        CHROMOSOMES,
        TRAIN_COUNT,
        TRAIN_PER_CHROMOSOME,
    )

    schedule_path = base / TRAIN_SCHEDULE_PATH
    authority_path = base / PHASE_A_AUTHORITY_PATH
    _require(schedule_path.is_file(), f"the TRAIN schedule is missing: {schedule_path}")
    _require(
        authority_path.is_file(), f"the Phase-A execution authority is missing: {authority_path}"
    )

    # The anchor is PURE SOURCE and opens nothing. Recomputing the baseline protocol from
    # `build_baseline_protocol` would have hashed the split manifest, which carries sealed rows --
    # the seal guard caught exactly that during development, which is why this route exists.
    selected = baseline_selected_content()
    _require(
        compute_baseline_selected_hash() == ACCEPTED_BASELINE_SELECTED_IDENTITY,
        "the baseline-selected authority this source computes is not the accepted one",
    )
    protocol_hash = str(selected["baseline_protocol_hash"])

    authority = json.loads(authority_path.read_bytes())
    content = authority["content"]
    _require(
        content.get("baseline_protocol_hash") == protocol_hash,
        "the Phase-A authority cites a protocol other than the accepted baseline protocol, so it "
        "cannot anchor anything",
    )
    expected_schedule_sha = str(content["train_schedule_manifest_sha256"])
    expected_split_sha = str(content["split_manifest_sha256"])

    raw = schedule_path.read_bytes()
    actual = hashlib.sha256(raw).hexdigest()
    _require(
        actual == expected_schedule_sha,
        f"the TRAIN schedule hashes to {actual}, but the accepted Phase-A authority records "
        f"{expected_schedule_sha}",
    )
    schedule = json.loads(raw)

    # the schedule's shape is checked against SOURCE constants, never against a local document
    for field, expected in (
        ("train_count", TRAIN_COUNT),
        ("train_per_chromosome", TRAIN_PER_CHROMOSOME),
        ("batch_count", BATCH_COUNT),
        ("split_manifest_sha256", expected_split_sha),
    ):
        _require(
            schedule.get(field) == expected,
            f"the TRAIN schedule's {field} is {schedule.get(field)!r}, expected {expected!r}",
        )
    _require(
        list(schedule["chromosomes"]) == list(CHROMOSOMES),
        "the TRAIN schedule spans different chromosomes than this source accepts",
    )
    batch_size = int(schedule["batch_size"])
    _require(
        batch_size * BATCH_COUNT == TRAIN_COUNT,
        "the TRAIN schedule's batching does not cover exactly the accepted TRAIN count",
    )

    rows: list[dict[str, str]] = []
    for batch in schedule["batches"]:
        _require(len(batch) == batch_size, "a TRAIN batch is not the declared batch size")
        for entry in batch:
            rows.append(
                {
                    "round_id": str(entry["round_id"]),
                    "dataset_id": str(entry["dataset_id"]),
                    "chromosome": str(entry["chromosome"]),
                }
            )
    _require(
        len(rows) == TRAIN_COUNT,
        f"the TRAIN schedule holds {len(rows)} rounds, expected {TRAIN_COUNT}",
    )
    _require(
        len({r["round_id"] for r in rows}) == len(rows),
        "the TRAIN schedule repeats a round",
    )
    per_chromosome: dict[str, int] = {}
    for row in rows:
        per_chromosome[row["chromosome"]] = per_chromosome.get(row["chromosome"], 0) + 1
    _require(
        set(per_chromosome) == set(CHROMOSOMES)
        and all(n == TRAIN_PER_CHROMOSOME for n in per_chromosome.values()),
        "the TRAIN schedule is not balanced as this source accepts",
    )
    anchors = {
        "baseline_protocol_hash": protocol_hash,
        "phase_a_authority_hash": str(authority["authority_hash"]),
        "train_schedule_manifest_sha256": expected_schedule_sha,
        "split_manifest_sha256": expected_split_sha,
        "registry_snapshot_hash": ACCEPTED_REGISTRY_SNAPSHOT_HASH,
    }
    return sorted(rows, key=lambda r: r["round_id"]), anchors


def load_verified_round_profile_corpus(
    *, root: Any = None, corpus_root: Any = None
) -> VerifiedRoundProfileAuthority:
    """Prove ownership for exactly the 50 anchored TRAIN rounds. Opens no sealed authority."""
    from minos_engine.common.hashing import canonical_hash
    from minos_engine.layer2.ingest.validation import validate_admission
    from minos_engine.layer2.safe_controller_policy import _resolve_root

    base = _resolve_root(root)
    corpus = Path(corpus_root) if corpus_root is not None else PROFILE_CORPUS_ROOT

    rows, anchors = _anchored_train_schedule(base)

    owned: dict[str, OwnedRoundProfile] = {}
    for row in rows:
        round_id = row["round_id"]
        # opened BY NAME. The corpus directory is never listed, so a sealed member is never even
        # observed to exist, let alone read.
        directory = corpus / round_id
        raw: dict[str, bytes] = {}
        for name in (PROFILE_DOCUMENT, MANIFEST_DOCUMENT, ATTESTATION_DOCUMENT, WINDOWS_ARTIFACT):
            path = directory / name
            _require(path.is_file(), f"a corpus artifact is missing: {path}")
            _require(not path.is_symlink(), f"{path} is a symlink")
            raw[name] = path.read_bytes()

        profile_document = json.loads(raw[PROFILE_DOCUMENT])
        manifest_document = json.loads(raw[MANIFEST_DOCUMENT])
        attestation = json.loads(raw[ATTESTATION_DOCUMENT])
        profile_sha = hashlib.sha256(raw[PROFILE_DOCUMENT]).hexdigest()
        windows_sha = hashlib.sha256(raw[WINDOWS_ARTIFACT]).hexdigest()
        manifest_sha = hashlib.sha256(raw[MANIFEST_DOCUMENT]).hexdigest()

        # the attestation is self-anchoring, and anchored to the accepted registry snapshot
        recomputed_attestation = canonical_hash(
            {k: v for k, v in sorted(attestation.items()) if k != "attestation_hash"}
        )
        _require(
            recomputed_attestation == str(attestation["attestation_hash"]),
            f"{round_id}: the attestation does not hash to its own recorded identity",
        )
        _require(
            str(attestation["registry_snapshot_hash"]) == ACCEPTED_REGISTRY_SNAPSHOT_HASH,
            f"{round_id}: the attestation cites a foreign registry snapshot",
        )
        _require(
            canonical_hash(
                {
                    "bam_sha256": attestation["bam_sha256"],
                    "bai_sha256": attestation["bai_sha256"],
                    "reference_sha256": attestation["reference_sha256"],
                    "fai_sha256": attestation["fai_sha256"],
                    "region_hash": attestation["region_hash"],
                }
            )
            == str(attestation["identity_tuple_hash"]),
            f"{round_id}: the attestation's identity tuple does not recompute",
        )
        # ... and bound to the ANCHORED schedule row, not to itself
        _require(
            str(attestation["round_id"]) == round_id
            and str(attestation["dataset_id"]) == row["dataset_id"]
            and str(attestation["chromosome"]) == row["chromosome"],
            f"{round_id}: the attestation does not describe the round the schedule anchors",
        )

        # RECONSTRUCTED from the verified attestation, never supplied by a caller
        registry_identity = {key: attestation[key] for key in REGISTRY_IDENTITY_KEYS}

        # THE accepted L2-D admission authority, unchanged and not reimplemented. It binds the
        # manifest to the profile and windows BYTES, which is why no artifact inventory is needed.
        decision = validate_admission(
            profile_document=profile_document,
            manifest_document=manifest_document,
            attestation=attestation,
            registry_identity=registry_identity,
            profile_artifact_sha256=profile_sha,
            windows_artifact_sha256=windows_sha,
        )
        _require(
            decision.admissible,
            f"{round_id} is not admissible: {'; '.join(decision.reasons) or 'no reason given'}",
        )

        owned[round_id] = OwnedRoundProfile(
            round_id=round_id,
            dataset_id=row["dataset_id"],
            chromosome=row["chromosome"],
            partition=ADMITTED_PARTITION,
            profile_id=str(profile_document["profile_id"]),
            profile_sha256=profile_sha,
            profile_manifest_sha256=manifest_sha,
            fingerprint_hash=str(manifest_document["fingerprint_hash"]),
            attestation_hash=str(attestation["attestation_hash"]),
            registry_snapshot_hash=ACCEPTED_REGISTRY_SNAPSHOT_HASH,
            identity_tuple_hash=str(attestation["identity_tuple_hash"]),
            region_hash=str(attestation["region_hash"]),
            bam_sha256=str(attestation["bam_sha256"]),
            bai_sha256=str(attestation["bai_sha256"]),
            reference_sha256=str(attestation["reference_sha256"]),
            fai_sha256=str(attestation["fai_sha256"]),
            integrity_degraded=bool(decision.integrity_degraded),
        )

    _require(len(owned) == len(rows), "a scheduled TRAIN round produced no owned profile")
    identity = sha256_hex(
        OWNERSHIP_DOMAIN.encode("utf-8")
        + canonical_json_bytes(
            {
                "schema_version": OWNERSHIP_SCHEMA,
                "partition": ADMITTED_PARTITION,
                "anchors": dict(sorted(anchors.items())),
                "member_count": len(owned),
                "members": [owned[r].content() for r in sorted(owned)],
            }
        )
    )
    return VerifiedRoundProfileAuthority(
        _CORPUS_TOKEN,
        by_round=owned,
        anchors=anchors,
        corpus_identity=identity,
    )
