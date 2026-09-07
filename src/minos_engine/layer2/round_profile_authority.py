"""``l2h-round-profile-ownership-v1`` — proving a decision request names a real round and profile.

Before this module the safe controller proved only that a ``Layer1ProfileReference`` was
*internally* consistent: its ``identity_tuple_hash`` recomputed from its own four file hashes and
region hash. That is not ownership. A caller could invent a round id, a profile id, a region and
four plausible-looking digests, satisfy the tuple by construction, and receive a production
decision for a profile that never existed.

Ownership is established from committed, frozen, gate-backed documents:

* ``manifests/profile_snapshot_epoch1_members.json`` — the PROFILE-SNAPSHOT-FROZEN-1 membership,
  which owns the ``round_id`` / ``dataset_id`` / ``profile_id`` / ``identity_tuple_hash`` /
  ``profile_manifest_sha256`` binding for each member;
* ``manifests/profile_snapshot_epoch1_artifact_inventory.json`` — the byte SHA-256 and size of
  each member's four on-disk artifacts;
* the member's own ``bam-profile-v1``, ``profile-manifest-v1`` and
  ``input-integrity-attestation-v1`` documents, each read from disk and required to hash to the
  inventory's recorded bytes before it is parsed as anything.

The admission logic itself is NOT reimplemented here. ``layer2.ingest.validation.validate_admission``
is the accepted L2-D authority for whether a profile, its manifest, its attestation and a registry
identity are mutually consistent, and it is called unchanged. What this module adds is the piece it
cannot supply on its own: ``validate_admission`` takes ``registry_identity`` as a plain dict from
its caller, so on its own it proves consistency with whatever identity the caller passed. Here the
registry identity is *reconstructed* from the member's own verified attestation bytes and then
cross-checked against the frozen membership row, so there is no caller-supplied identity anywhere
in the chain.

Layer 2 still does not open the BAM. Nothing here reads truth, mutations, scores, VALIDATION or
TEST: the profile document is consumed only for its identity sections and eligible feature values,
exactly as L2-D already does.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Final

from minos_engine.common.canonical_json import canonical_json_bytes
from minos_engine.common.errors import MinosEngineError
from minos_engine.common.hashing import sha256_hex

__all__ = [
    "ADMITTED_PARTITION",
    "ARTIFACT_INVENTORY_PATH",
    "PROFILE_CORPUS_ROOT",
    "OWNERSHIP_DOMAIN",
    "OWNERSHIP_SCHEMA",
    "SNAPSHOT_MEMBERS_PATH",
    "OwnedRoundProfile",
    "RoundProfileAuthorityError",
    "VerifiedRoundProfileAuthority",
    "load_verified_round_profile_corpus",
]

OWNERSHIP_SCHEMA: Final = "l2h-round-profile-ownership-v1"
OWNERSHIP_DOMAIN: Final = "minos:l2h-round-profile-ownership:v1\n"

SNAPSHOT_MEMBERS_PATH: Final = "manifests/profile_snapshot_epoch1_members.json"
ARTIFACT_INVENTORY_PATH: Final = "manifests/profile_snapshot_epoch1_artifact_inventory.json"

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

    __slots__ = (
        "_by_round",
        "corpus_identity",
        "partition",
        "registry_snapshot_hash",
        "skipped_partition_counts",
        "snapshot_hash",
    )

    def __init__(
        self,
        token: object,
        *,
        by_round: dict[str, OwnedRoundProfile],
        snapshot_hash: str,
        registry_snapshot_hash: str,
        corpus_identity: str,
        partition: str = ADMITTED_PARTITION,
        skipped_partition_counts: dict[str, int] | None = None,
    ) -> None:
        if token is not _CORPUS_TOKEN:
            raise RoundProfileAuthorityError(
                "an owned-profile corpus may only be minted by the verifying loader; a "
                "dictionary has not been verified against anything"
            )
        self._by_round = dict(by_round)
        self.snapshot_hash = snapshot_hash
        self.registry_snapshot_hash = registry_snapshot_hash
        self.corpus_identity = corpus_identity
        self.partition = partition
        self.skipped_partition_counts = dict(skipped_partition_counts or {})

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
            "snapshot_hash": self.snapshot_hash,
            "registry_snapshot_hash": self.registry_snapshot_hash,
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


def load_verified_round_profile_corpus(
    *, root: Any = None, corpus_root: Any = None
) -> VerifiedRoundProfileAuthority:
    """Verify the frozen snapshot, every member's artifacts, and every member's admission."""
    from minos_engine.layer2.ingest.validation import validate_admission
    from minos_engine.layer2.safe_controller_policy import _resolve_root

    base = _resolve_root(root)
    corpus = Path(corpus_root) if corpus_root is not None else PROFILE_CORPUS_ROOT

    members_path = base / SNAPSHOT_MEMBERS_PATH
    inventory_path = base / ARTIFACT_INVENTORY_PATH
    _require(members_path.is_file(), f"the frozen snapshot membership is missing: {members_path}")
    _require(
        inventory_path.is_file(), f"the frozen artifact inventory is missing: {inventory_path}"
    )
    membership = json.loads(members_path.read_bytes())
    inventory = json.loads(inventory_path.read_bytes())

    members = list(membership["members"])
    _require(
        len(members) == int(membership["member_count"]),
        "the frozen snapshot's member_count disagrees with its own member list",
    )
    snapshot_hash = str(membership["snapshot_hash"])
    registry_snapshot_hash = str(membership["registry_snapshot_hash"])

    by_round_inventory = {str(e["round_id"]): e for e in inventory["entries"]}
    _require(
        len(by_round_inventory) == len(inventory["entries"]),
        "the frozen artifact inventory repeats a round",
    )

    owned: dict[str, OwnedRoundProfile] = {}
    skipped: dict[str, int] = {}
    for member in members:
        partition = str(member["partition"])
        if partition != ADMITTED_PARTITION:
            # skipped BEFORE any artifact is opened: a sealed partition is not read, not admitted,
            # and not retained. Only the count survives, so the seal is auditable without
            # enumerating what it covers.
            skipped[partition] = skipped.get(partition, 0) + 1
            continue
        round_id = str(member["round_id"])
        _require(round_id not in owned, f"the frozen snapshot repeats round {round_id}")
        _require(
            str(member["registry_snapshot_hash"]) == registry_snapshot_hash,
            f"{round_id} cites a different registry snapshot than its own membership",
        )
        entry = by_round_inventory.get(round_id)
        _require(entry is not None, f"{round_id} has no entry in the frozen artifact inventory")
        assert entry is not None
        _require(
            str(entry["dataset_id"]) == str(member["dataset_id"]),
            f"{round_id}: the inventory and the membership disagree on the dataset",
        )
        artifacts = entry["artifacts"]
        directory = corpus / round_id

        raw = {
            name: _read_exact(
                directory / name,
                expected_sha=str(artifacts[name]["sha256"]),
                expected_size=int(artifacts[name]["size_bytes"]),
            )
            for name in (
                PROFILE_DOCUMENT,
                MANIFEST_DOCUMENT,
                ATTESTATION_DOCUMENT,
                WINDOWS_ARTIFACT,
            )
        }
        profile_document = json.loads(raw[PROFILE_DOCUMENT])
        manifest_document = json.loads(raw[MANIFEST_DOCUMENT])
        attestation = json.loads(raw[ATTESTATION_DOCUMENT])

        # the membership owns these bindings; the bytes must agree with what it recorded
        _require(
            hashlib.sha256(raw[MANIFEST_DOCUMENT]).hexdigest()
            == str(member["profile_manifest_sha256"]),
            f"{round_id}: the manifest bytes are not the ones the snapshot recorded",
        )
        _require(
            hashlib.sha256(raw[PROFILE_DOCUMENT]).hexdigest() == str(member["profile_sha256"]),
            f"{round_id}: the profile bytes are not the ones the snapshot recorded",
        )
        _require(
            str(profile_document["profile_id"]) == str(member["profile_id"]),
            f"{round_id}: the profile document names another profile than the snapshot",
        )

        # RECONSTRUCTED from the verified attestation, never supplied by a caller
        registry_identity = {key: attestation[key] for key in REGISTRY_IDENTITY_KEYS}
        _require(
            registry_identity["round_id"] == round_id
            and registry_identity["dataset_id"] == str(member["dataset_id"])
            and registry_identity["identity_tuple_hash"] == str(member["identity_tuple_hash"])
            and registry_identity["registry_snapshot_hash"] == registry_snapshot_hash
            and registry_identity["chromosome"] == str(member["chromosome"]),
            f"{round_id}: the attestation's identity disagrees with the frozen membership",
        )

        # THE accepted L2-D admission authority, unchanged and not reimplemented
        decision = validate_admission(
            profile_document=profile_document,
            manifest_document=manifest_document,
            attestation=attestation,
            registry_identity=registry_identity,
            profile_artifact_sha256=str(member["profile_sha256"]),
            windows_artifact_sha256=str(member["windows_sha256"]),
        )
        _require(
            decision.admissible,
            f"{round_id} is not admissible: {'; '.join(decision.reasons) or 'no reason given'}",
        )
        _require(
            decision.feature_values_hash == str(member["feature_values_hash"]),
            f"{round_id}: admission recomputes a different feature-values identity",
        )

        owned[round_id] = OwnedRoundProfile(
            round_id=round_id,
            dataset_id=str(member["dataset_id"]),
            chromosome=str(member["chromosome"]),
            partition=str(member["partition"]),
            profile_id=str(member["profile_id"]),
            profile_sha256=str(member["profile_sha256"]),
            profile_manifest_sha256=str(member["profile_manifest_sha256"]),
            fingerprint_hash=str(manifest_document["fingerprint_hash"]),
            attestation_hash=str(attestation["attestation_hash"]),
            registry_snapshot_hash=registry_snapshot_hash,
            identity_tuple_hash=str(member["identity_tuple_hash"]),
            region_hash=str(attestation["region_hash"]),
            bam_sha256=str(attestation["bam_sha256"]),
            bai_sha256=str(attestation["bai_sha256"]),
            reference_sha256=str(attestation["reference_sha256"]),
            fai_sha256=str(attestation["fai_sha256"]),
            integrity_degraded=bool(member["integrity_degraded"]),
        )

    _require(bool(owned), f"the frozen snapshot has no {ADMITTED_PARTITION} member")
    identity = sha256_hex(
        OWNERSHIP_DOMAIN.encode("utf-8")
        + canonical_json_bytes(
            {
                "schema_version": OWNERSHIP_SCHEMA,
                "partition": ADMITTED_PARTITION,
                "snapshot_hash": snapshot_hash,
                "registry_snapshot_hash": registry_snapshot_hash,
                "member_count": len(owned),
                "skipped_partition_counts": dict(sorted(skipped.items())),
                "members": [owned[r].content() for r in sorted(owned)],
            }
        )
    )
    return VerifiedRoundProfileAuthority(
        _CORPUS_TOKEN,
        by_round=owned,
        snapshot_hash=snapshot_hash,
        registry_snapshot_hash=registry_snapshot_hash,
        corpus_identity=identity,
        partition=ADMITTED_PARTITION,
        skipped_partition_counts=skipped,
    )
