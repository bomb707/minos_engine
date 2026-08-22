"""E2: exact-byte extraction, four-way binding, verified-manifest snapshots, verifier.

All profile documents are SYNTHETIC, generated from the accepted ``bam-profile-v1``
JSON schema itself (no corpus access, no fixed 75/50/10/15 anywhere). Snapshots are
derived through the verified member-manifest boundary; two non-75 snapshots with uneven
chr18–chr22 membership prove assignments are consumed verbatim.
"""

from __future__ import annotations

import hashlib
import json
from functools import lru_cache
from typing import Any

import pytest

from minos_engine.common.hashing import canonical_hash
from minos_engine.layer2.features.contracts import (
    AUTHORITATIVE_COLUMNS,
    FROZEN_FEATURE_SET_HASH,
    canonical_feature_set,
    matrix_hash,
    vector_hash,
)
from minos_engine.layer2.features.errors import (
    FeatureValuesHashMismatchError,
    ForbiddenPartitionError,
    InvalidMemberManifestError,
    InvalidProfileDocumentError,
    MatrixAssemblyError,
    MemberManifestHashMismatchError,
    MissingFeatureError,
    ProfileArtifactHashMismatchError,
    ProfilerIdentityMismatchError,
    RegistrySnapshotMismatchError,
    SnapshotHashMismatchError,
    SnapshotIdentityMismatchError,
)
from minos_engine.layer2.features.extraction import (
    FrozenSnapshot,
    ManifestTrustBundle,
    SnapshotMember,
    assemble_matrix,
    build_feature_vector,
    build_partition_matrix,
    extract_profile_features,
    load_accepted_epoch1_member_manifest,
    load_member_manifest_with_trust,
    verify_accepted_epoch1_matrix_payload,
    verify_matrix,
    verify_matrix_payload_with_trust,
)
from minos_engine.layer2.ingest.contracts import (
    canonical_feature_values_hash,
    extract_eligible_feature_values,
)
from minos_engine.layer2.ingest.validation import l1_feature_values_hash_from_document
from minos_engine.layer2.prerequisites import (
    PROFILE_SNAPSHOT_1_HASH,
    PROFILER_CONFIG_HASH,
    PROFILER_VERSION,
)
from minos_engine.schema_registry import load_schema, validate_against
from tests.conftest import REPO_ROOT

_SPLIT_H = hashlib.sha256(b"synthetic-split-manifest").hexdigest()
_REG_H = hashlib.sha256(b"synthetic-registry-snapshot").hexdigest()


def _bundle_for(manifest_bytes: bytes) -> ManifestTrustBundle:
    """Explicit TEST-ONLY trust bundle for synthetic manifests, derived from the
    manifest's own declared identities (the sanctioned non-production boundary)."""
    raw = json.loads(manifest_bytes)
    return ManifestTrustBundle(
        epoch=raw["epoch"],
        member_manifest_hash=raw["member_manifest_hash"],
        snapshot_hash=raw["snapshot_hash"],
        split_manifest_hash=raw["split_manifest_hash"],
        registry_snapshot_hash=raw["registry_snapshot_hash"],
    )


# --------------------------------------------------------------------------- #
# synthetic profile documents, generated from the accepted schema itself
# --------------------------------------------------------------------------- #
def _gen(schema: dict[str, Any], root: dict[str, Any] | None = None) -> Any:
    """Produce a schema-conforming instance (fills ALL declared properties)."""
    root = root if root is not None else schema
    if "$ref" in schema:
        node: Any = root
        for part in schema["$ref"].lstrip("#/").split("/"):
            node = node[part]
        return _gen(node, root)
    if "const" in schema:
        return schema["const"]
    if "enum" in schema:
        return schema["enum"][0]
    if "anyOf" in schema:
        return _gen(schema["anyOf"][0], root)
    kind = schema.get("type")
    if isinstance(kind, list):
        non_null = [k for k in kind if k != "null"]
        kind = non_null[0] if non_null else "null"
    if kind == "object" or "properties" in schema:
        return {k: _gen(v, root) for k, v in schema.get("properties", {}).items()}
    if kind == "array":
        n = int(schema.get("minItems", 0))
        item = schema.get("items", {"type": "string"})
        return [_gen(item, root) for _ in range(n)]
    if kind == "string":
        return "x" * max(1, int(schema.get("minLength", 1)))
    if kind in ("number", "integer"):
        value: float = 1
        if "minimum" in schema:
            value = schema["minimum"]
        if "exclusiveMinimum" in schema:
            value = schema["exclusiveMinimum"] + 1
        if "maximum" in schema:
            value = min(value, schema["maximum"])
        if "exclusiveMaximum" in schema and value >= schema["exclusiveMaximum"]:
            value = (schema.get("minimum", 0) + schema["exclusiveMaximum"]) / 2
        return int(value) if kind == "integer" else float(value)
    if kind == "boolean":
        return False
    if kind == "null":
        return None
    return "x"


@lru_cache(maxsize=1)
def _template_json() -> str:
    return json.dumps(_gen(load_schema("bam-profile-v1")))


def _set_path(doc: dict[str, Any], path: str, value: Any) -> None:
    node = doc
    parts = path.split(".")
    for part in parts[:-1]:
        node = node.setdefault(part, {})
    node[parts[-1]] = value


def _del_path(doc: dict[str, Any], path: str) -> None:
    node = doc
    parts = path.split(".")
    for part in parts[:-1]:
        node = node[part]
    del node[parts[-1]]


def make_doc(
    profile_id: str,
    *,
    seed: int = 0,
    overrides: dict[str, Any] | None = None,
    deletes: tuple[str, ...] = (),
    validate: bool = True,
) -> dict[str, Any]:
    doc: dict[str, Any] = json.loads(_template_json())
    doc["schema_version"] = "bam-profile-v1"
    doc["profile_id"] = profile_id
    doc["status"] = "COMPLETE"
    # distinct, kind-valid value per column and per member (FRACTION stays in [0,1]).
    for i, column in enumerate(canonical_feature_set().columns):
        _set_path(doc, column.path, 0.001 * i + 0.000001 * seed)
    for path, value in (overrides or {}).items():
        _set_path(doc, path, value)
    for path in deletes:
        _del_path(doc, path)
    if validate:
        validate_against("bam-profile-v1", doc)
    return doc


def _member_dict(
    dataset_id: str,
    partition: str,
    *,
    seed: int = 0,
    chromosome: str = "chr18",
    doc: dict[str, Any] | None = None,
    compute_hashes: bool = True,
) -> tuple[dict[str, Any], bytes]:
    """A FULL manifest-member row (all provenance fields) plus its profile payload."""
    profile_id = hashlib.md5(dataset_id.encode(), usedforsecurity=False).hexdigest()
    if doc is None:
        doc = make_doc(profile_id, seed=seed)
    payload = json.dumps(doc).encode("utf-8")
    if compute_hashes:
        fvh = canonical_feature_values_hash(extract_eligible_feature_values(doc))
        l1: str | None = l1_feature_values_hash_from_document(doc)
    else:
        fvh, l1 = "0" * 64, None
    row = {
        "dataset_id": dataset_id,
        "round_id": hashlib.md5(f"round:{dataset_id}".encode(), usedforsecurity=False).hexdigest(),
        "chromosome": chromosome,
        "partition": partition,
        "identity_tuple_hash": hashlib.sha256(f"identity:{dataset_id}".encode()).hexdigest(),
        "content_hash": hashlib.sha256(f"content:{dataset_id}".encode()).hexdigest(),
        "feature_values_hash": fvh,
        "l1_feature_values_hash": l1 if l1 is not None else "0" * 64,
        "profile_id": doc.get("profile_id", profile_id),
        "profile_sha256": hashlib.sha256(payload).hexdigest(),
        "profile_manifest_sha256": hashlib.sha256(f"pm:{dataset_id}".encode()).hexdigest(),
        "windows_sha256": hashlib.sha256(f"win:{dataset_id}".encode()).hexdigest(),
        "attestation_hash": hashlib.sha256(f"att:{dataset_id}".encode()).hexdigest(),
        "m5_status": "ABSENT",
        "integrity_degraded": True,
        "profile_status": "COMPLETE",
        "profiler_version": PROFILER_VERSION,
        "profiler_config_hash": PROFILER_CONFIG_HASH,
        "registry_snapshot_hash": _REG_H,
    }
    return row, payload


def make_member(
    dataset_id: str,
    partition: str,
    *,
    seed: int = 0,
    chromosome: str = "chr18",
    doc: dict[str, Any] | None = None,
    compute_hashes: bool = True,
) -> tuple[SnapshotMember, bytes]:
    row, payload = _member_dict(
        dataset_id,
        partition,
        seed=seed,
        chromosome=chromosome,
        doc=doc,
        compute_hashes=compute_hashes,
    )
    if not compute_hashes:
        row["l1_feature_values_hash"] = None  # skip the L1 chain for invalid docs
    return SnapshotMember(**row), payload


def _freeze_hash(epoch: int, rows: list[dict[str, Any]]) -> str:
    return canonical_hash(
        {
            "epoch": epoch,
            "split_manifest_hash": _SPLIT_H,
            "registry_snapshot_hash": _REG_H,
            "members": [
                {
                    "dataset_id": r["dataset_id"],
                    "partition": r["partition"],
                    "content_hash": r["content_hash"],
                    "feature_values_hash": r["feature_values_hash"],
                }
                for r in sorted(rows, key=lambda r: str(r["dataset_id"]))
            ],
        }
    )


def make_manifest_bytes(
    spec: list[tuple[str, str, str]], *, epoch: int = 1
) -> tuple[bytes, dict[str, bytes]]:
    rows: list[dict[str, Any]] = []
    payloads: dict[str, bytes] = {}
    for i, (dataset_id, chromosome, partition) in enumerate(spec):
        row, payload = _member_dict(dataset_id, partition, seed=i, chromosome=chromosome)
        rows.append(row)
        payloads[dataset_id] = payload
    rows.sort(key=lambda r: str(r["dataset_id"]))
    content: dict[str, Any] = {
        "schema_version": "profile-snapshot-members-v1",
        "epoch": epoch,
        "split_manifest_hash": _SPLIT_H,
        "registry_snapshot_hash": _REG_H,
        "snapshot_hash": _freeze_hash(epoch, rows),
        "member_count": len(rows),
        "members": rows,
    }
    manifest = {**content, "member_manifest_hash": canonical_hash(content)}
    return json.dumps(manifest).encode("utf-8"), payloads


def make_snapshot(
    spec: list[tuple[str, str, str]], *, epoch: int = 1
) -> tuple[FrozenSnapshot, dict[str, bytes], bytes]:
    """Derive every synthetic snapshot through the VERIFIED manifest boundary."""
    manifest_bytes, payloads = make_manifest_bytes(spec, epoch=epoch)
    snapshot = load_member_manifest_with_trust(manifest_bytes, _bundle_for(manifest_bytes))
    return snapshot, payloads, manifest_bytes


#: Snapshot A: 9 members, uneven chromosomes (4×chr18, 3×chr20, 2×chr21),
#: partitions train=4 / validation=2 / test=3 — nothing resembling 50/10/15.
_SPEC_A = [
    ("ds-a18-01", "chr18", "train"),
    ("ds-a18-02", "chr18", "train"),
    ("ds-a18-03", "chr18", "train"),
    ("ds-a18-04", "chr18", "validation"),
    ("ds-a20-05", "chr20", "train"),
    ("ds-a20-06", "chr20", "validation"),
    ("ds-a20-07", "chr20", "test"),
    ("ds-a21-08", "chr21", "test"),
    ("ds-a21-09", "chr21", "test"),
]

#: Snapshot B: 6 members (5×chr19, 1×chr22), train=1 / validation=3 / test=2 —
#: a grandfathered allocation no percentage rule would ever produce.
_SPEC_B = [
    ("ds-b19-01", "chr19", "train"),
    ("ds-b19-02", "chr19", "validation"),
    ("ds-b19-03", "chr19", "validation"),
    ("ds-b19-04", "chr19", "validation"),
    ("ds-b19-05", "chr19", "test"),
    ("ds-b22-06", "chr22", "test"),
]


@pytest.fixture(scope="module")
def snap_a() -> tuple[FrozenSnapshot, dict[str, bytes], bytes]:
    return make_snapshot(_SPEC_A)


@pytest.fixture(scope="module")
def snap_b() -> tuple[FrozenSnapshot, dict[str, bytes], bytes]:
    return make_snapshot(_SPEC_B, epoch=2)


def _provider(payloads: dict[str, bytes]):
    return lambda member: payloads[member.dataset_id]


def _core_member(dataset_id: str, partition: str) -> SnapshotMember:
    return SnapshotMember(
        dataset_id=dataset_id,
        profile_id=hashlib.md5(dataset_id.encode(), usedforsecurity=False).hexdigest(),
        partition=partition,  # type: ignore[arg-type]
        content_hash=hashlib.sha256(f"c:{dataset_id}".encode()).hexdigest(),
        feature_values_hash=hashlib.sha256(f"f:{dataset_id}".encode()).hexdigest(),
        profile_sha256=hashlib.sha256(f"p:{dataset_id}".encode()).hexdigest(),
    )


# --------------------------------------------------------------------------- #
# exact-byte extraction boundary
# --------------------------------------------------------------------------- #
def test_exact_byte_profile_sha_mismatch() -> None:
    member, payload = make_member("ds-x-01", "train")
    with pytest.raises(ProfileArtifactHashMismatchError):
        extract_profile_features(payload + b" ", member)  # ONE byte appended
    # the hash is always recomputed from received bytes — a member whose recorded
    # hash disagrees with the exact bytes rejects even a well-formed payload.
    tampered = member.model_copy(update={"profile_sha256": "0" * 64})
    with pytest.raises(ProfileArtifactHashMismatchError):
        extract_profile_features(payload, tampered)


def test_duplicate_json_key_rejected() -> None:
    member, payload = make_member("ds-x-02", "train")
    bad = b'{"schema_version": "bam-profile-v1", ' + payload[1:]  # duplicates the key
    rebound = member.model_copy(update={"profile_sha256": hashlib.sha256(bad).hexdigest()})
    with pytest.raises(InvalidProfileDocumentError, match="duplicate JSON key"):
        extract_profile_features(bad, rebound)


def test_non_utf8_and_invalid_json_rejected() -> None:
    member, _ = make_member("ds-x-03", "train")
    for bad in (b"\xff\xfe\x00broken", b"{not json"):
        rebound = member.model_copy(update={"profile_sha256": hashlib.sha256(bad).hexdigest()})
        with pytest.raises(InvalidProfileDocumentError):
            extract_profile_features(bad, rebound)


def test_schema_invalid_profile_rejected() -> None:
    profile_id = hashlib.md5(b"ds-x-04", usedforsecurity=False).hexdigest()
    doc = make_doc(profile_id, validate=False)
    del doc["coverage"]  # drop a required section
    member, payload = make_member("ds-x-04", "train", doc=doc, compute_hashes=False)
    with pytest.raises(InvalidProfileDocumentError):
        extract_profile_features(payload, member)


@pytest.mark.parametrize(
    ("name", "mutate", "expected"),
    [
        ("nan", {"value": float("nan")}, MissingFeatureError),
        ("infinity", {"value": float("inf")}, MissingFeatureError),
        ("null", {"value": None}, (InvalidProfileDocumentError, MissingFeatureError)),
        ("missing", {"delete": True}, (InvalidProfileDocumentError, MissingFeatureError)),
        (
            "fraction_out_of_range",
            {"value": 1.5, "fraction": True},
            (InvalidProfileDocumentError, MissingFeatureError),
        ),
    ],
)
def test_invalid_feature_values_rejected(name, mutate, expected) -> None:
    columns = canonical_feature_set().columns
    if mutate.get("fraction"):
        target = next(c.path for c in columns if c.value_kind == "FRACTION")
    else:
        target = next(c.path for c in columns if c.value_kind == "REAL")
    profile_id = hashlib.md5(f"ds-bad-{name}".encode(), usedforsecurity=False).hexdigest()
    if mutate.get("delete"):
        doc = make_doc(profile_id, deletes=(target,), validate=False)
    else:
        doc = make_doc(profile_id, overrides={target: mutate["value"]}, validate=False)
    member, payload = make_member(f"ds-bad-{name}", "train", doc=doc, compute_hashes=False)
    with pytest.raises(expected):
        extract_profile_features(payload, member)


def test_all_129_paths_extracted_in_exact_manifest_order() -> None:
    member, payload = make_member("ds-x-05", "train", seed=7)
    result = extract_profile_features(payload, member)
    columns = canonical_feature_set().columns
    assert len(result.values) == 129 == len(columns)
    assert [c.path for c in columns] == list(AUTHORITATIVE_COLUMNS)
    expected = tuple(0.001 * i + 0.000001 * 7 for i in range(129))
    assert result.values == expected  # manifest (sorted-path) order, exact values
    assert result.feature_values_hash == member.feature_values_hash


def test_window_fields_completely_excluded() -> None:
    from minos_engine.layer2.feature_registry import production_eligible_fields, record_for

    window_paths = [
        p
        for p in production_eligible_fields()
        if (r := record_for(p)) is not None and r.source_schema == "window-profile-v1"
    ]
    assert len(window_paths) == 12
    assert set(window_paths).isdisjoint(AUTHORITATIVE_COLUMNS)
    # mutating a non-eligible document field changes the bytes but never the
    # extracted values or the canonical feature-values hash.
    base_member, base_payload = make_member("ds-x-06", "train", seed=3)
    profile_id = base_member.profile_id
    changed_doc = make_doc(profile_id, seed=3, overrides={"warnings": ["w" * 5]})
    changed_member, changed_payload = make_member("ds-x-06", "train", doc=changed_doc)
    assert changed_payload != base_payload
    a = extract_profile_features(base_payload, base_member)
    b = extract_profile_features(changed_payload, changed_member)
    assert a.values == b.values
    assert a.feature_values_hash == b.feature_values_hash


# --------------------------------------------------------------------------- #
# four-way binding + typed failures
# --------------------------------------------------------------------------- #
def test_profile_identity_mismatch_rejected() -> None:
    member, payload = make_member("ds-x-07", "train")
    rebound = member.model_copy(update={"profile_id": "e" * 32})
    with pytest.raises(SnapshotIdentityMismatchError, match="profile_id"):
        extract_profile_features(payload, rebound)


def test_l1_identity_chain_mismatch_rejected() -> None:
    member, payload = make_member("ds-x-08", "train")
    rebound = member.model_copy(update={"l1_feature_values_hash": "0" * 64})
    with pytest.raises(SnapshotIdentityMismatchError, match="L1"):
        extract_profile_features(payload, rebound)


def test_feature_values_hash_mismatch_rejected() -> None:
    member, payload = make_member("ds-x-09", "train")
    rebound = member.model_copy(update={"feature_values_hash": "0" * 64})
    with pytest.raises(FeatureValuesHashMismatchError):
        build_feature_vector(payload, rebound, epoch=1, snapshot_hash="d" * 64)


def test_vector_binds_all_identities() -> None:
    member, payload = make_member("ds-x-10", "validation", seed=11)
    vector = build_feature_vector(payload, member, epoch=3, snapshot_hash="d" * 64)
    assert vector.epoch == 3
    assert vector.dataset_id == member.dataset_id
    assert vector.profile_id == member.profile_id
    assert vector.content_hash == member.content_hash
    assert vector.feature_values_hash == member.feature_values_hash
    assert vector.partition == "validation"
    assert vector.snapshot_hash == "d" * 64
    assert vector.feature_set_hash == FROZEN_FEATURE_SET_HASH
    assert vector.value_count == 129
    assert vector_hash(vector) == vector.vector_hash


def test_test_member_rejected_before_bytes_are_touched() -> None:
    member, _ = make_member("ds-x-11", "test")
    # b"" would fail the byte-hash check if it were reached; the partition
    # rejection must come FIRST.
    with pytest.raises(ForbiddenPartitionError):
        build_feature_vector(b"", member, epoch=1, snapshot_hash="d" * 64)


# --------------------------------------------------------------------------- #
# FrozenSnapshot independent self-binding (freeze formula)
# --------------------------------------------------------------------------- #
def test_snapshot_hash_is_recomputed_and_supplied_mismatch_rejected() -> None:
    members = (_core_member("ds-s-01", "train"), _core_member("ds-s-02", "validation"))
    snapshot = FrozenSnapshot(
        epoch=1, split_manifest_hash=_SPLIT_H, registry_snapshot_hash=_REG_H, members=members
    )
    # the auto-filled hash IS the freeze formula over the core member fields.
    expected = canonical_hash(
        {
            "epoch": 1,
            "split_manifest_hash": _SPLIT_H,
            "registry_snapshot_hash": _REG_H,
            "members": [
                {
                    "dataset_id": m.dataset_id,
                    "partition": m.partition,
                    "content_hash": m.content_hash,
                    "feature_values_hash": m.feature_values_hash,
                }
                for m in sorted(members, key=lambda m: m.dataset_id)
            ],
        }
    )
    assert snapshot.snapshot_hash == expected
    with pytest.raises(SnapshotHashMismatchError):
        FrozenSnapshot(
            epoch=1,
            split_manifest_hash=_SPLIT_H,
            registry_snapshot_hash=_REG_H,
            members=members,
            snapshot_hash="0" * 64,
        )


def test_snapshot_tampering_with_retained_hash_rejected() -> None:
    members = (_core_member("ds-s-01", "train"), _core_member("ds-s-02", "validation"))
    snapshot = FrozenSnapshot(
        epoch=1, split_manifest_hash=_SPLIT_H, registry_snapshot_hash=_REG_H, members=members
    )
    old_hash = snapshot.snapshot_hash
    member_tampers = (
        {"partition": "validation"},
        {"dataset_id": "ds-zz-99"},
        {"content_hash": "0" * 64},
        {"feature_values_hash": "0" * 64},
    )
    for update in member_tampers:
        tampered = (members[0].model_copy(update=update), members[1])
        with pytest.raises(SnapshotHashMismatchError):
            FrozenSnapshot(
                epoch=1,
                split_manifest_hash=_SPLIT_H,
                registry_snapshot_hash=_REG_H,
                members=tampered,
                snapshot_hash=old_hash,
            )
    top_tampers = (
        {"epoch": 2},
        {"split_manifest_hash": "e" * 64},
        {"registry_snapshot_hash": "e" * 64},
    )
    for update in top_tampers:
        kwargs: dict[str, Any] = {
            "epoch": 1,
            "split_manifest_hash": _SPLIT_H,
            "registry_snapshot_hash": _REG_H,
            "members": members,
            "snapshot_hash": old_hash,
        }
        kwargs.update(update)
        with pytest.raises(SnapshotHashMismatchError):
            FrozenSnapshot(**kwargs)


# --------------------------------------------------------------------------- #
# verified member-manifest boundary (provenance binding)
# --------------------------------------------------------------------------- #
def test_committed_epoch1_manifest_verifies_through_accepted_boundary() -> None:
    data = (REPO_ROOT / "manifests" / "profile_snapshot_epoch1_members.json").read_bytes()
    # the ACCEPTED loader carries every trust anchor itself — nothing is passed here.
    snapshot = load_accepted_epoch1_member_manifest(data)
    # the derived snapshot reproduces the ACCEPTED frozen snapshot identity.
    assert snapshot.snapshot_hash == PROFILE_SNAPSHOT_1_HASH
    assert snapshot.epoch == 1
    assert all(
        m.chromosome in ("chr18", "chr19", "chr20", "chr21", "chr22") for m in snapshot.members
    )
    assert all(
        m.profiler_version == PROFILER_VERSION and m.profiler_config_hash == PROFILER_CONFIG_HASH
        for m in snapshot.members
    )


def test_manifest_duplicate_key_and_structure_rejected(snap_a) -> None:
    _, _, manifest_bytes = snap_a
    trust = _bundle_for(manifest_bytes)
    dup = b'{"schema_version": "profile-snapshot-members-v1", ' + manifest_bytes[1:]
    with pytest.raises(InvalidMemberManifestError, match="duplicate JSON key"):
        load_member_manifest_with_trust(dup, trust)
    raw = json.loads(manifest_bytes)
    del raw["members"][0]["profile_sha256"]  # structural: a required field is gone
    with pytest.raises(InvalidMemberManifestError):
        load_member_manifest_with_trust(json.dumps(raw).encode(), trust)


def test_manifest_hash_binds_profile_sha256_and_all_provenance(snap_a) -> None:
    _, _, manifest_bytes = snap_a
    trust = _bundle_for(manifest_bytes)
    raw = json.loads(manifest_bytes)
    raw["members"][0]["profile_sha256"] = "0" * 64
    # 1) swapped profile_sha256 with the old manifest hash: rejected.
    with pytest.raises(MemberManifestHashMismatchError):
        load_member_manifest_with_trust(json.dumps(raw).encode(), trust)
    # 2) even a self-consistent re-hash cannot reproduce the anchored manifest hash —
    #    the MANDATORY trust bundle detects the swap (snapshot_hash alone would not,
    #    which is exactly why provenance must come from the verified manifest).
    content = {k: v for k, v in raw.items() if k != "member_manifest_hash"}
    raw["member_manifest_hash"] = canonical_hash(content)
    assert raw["member_manifest_hash"] != trust.member_manifest_hash
    with pytest.raises(MemberManifestHashMismatchError):
        load_member_manifest_with_trust(json.dumps(raw).encode(), trust)


def test_manifest_snapshot_hash_recomputed_independently(snap_a) -> None:
    _, _, manifest_bytes = snap_a
    raw = json.loads(manifest_bytes)
    raw["snapshot_hash"] = "0" * 64
    content = {k: v for k, v in raw.items() if k != "member_manifest_hash"}
    raw["member_manifest_hash"] = canonical_hash(content)  # internally consistent
    tampered = json.dumps(raw).encode()
    # even with a trust bundle DERIVED FROM the tampered manifest itself, the
    # independent freeze-formula recompute rejects the declared snapshot_hash.
    with pytest.raises(SnapshotHashMismatchError):
        load_member_manifest_with_trust(tampered, _bundle_for(tampered))


def test_manifest_registry_and_profiler_bindings(snap_a) -> None:
    _, _, manifest_bytes = snap_a

    def _rebind(raw: dict[str, Any]) -> bytes:
        content = {k: v for k, v in raw.items() if k != "member_manifest_hash"}
        raw["member_manifest_hash"] = canonical_hash(content)
        return json.dumps(raw).encode()

    raw = json.loads(manifest_bytes)
    raw["members"][0]["registry_snapshot_hash"] = "0" * 64
    tampered = _rebind(raw)
    with pytest.raises(RegistrySnapshotMismatchError):
        load_member_manifest_with_trust(tampered, _bundle_for(tampered))

    raw = json.loads(manifest_bytes)
    raw["members"][0]["profiler_config_hash"] = "0" * 64
    tampered = _rebind(raw)
    with pytest.raises(ProfilerIdentityMismatchError):
        load_member_manifest_with_trust(tampered, _bundle_for(tampered))

    raw = json.loads(manifest_bytes)
    raw["members"][0]["profiler_version"] = "other-profiler"
    tampered = _rebind(raw)
    with pytest.raises(ProfilerIdentityMismatchError):
        load_member_manifest_with_trust(tampered, _bundle_for(tampered))

    wrong_registry = _bundle_for(manifest_bytes).model_copy(
        update={"registry_snapshot_hash": "0" * 64}
    )
    with pytest.raises(RegistrySnapshotMismatchError):
        load_member_manifest_with_trust(manifest_bytes, wrong_registry)


# --------------------------------------------------------------------------- #
# snapshot-derived assembly (verbatim, uneven, no percentages)
# --------------------------------------------------------------------------- #
def test_two_uneven_snapshots_build_exact_membership(snap_a, snap_b) -> None:
    for (snapshot, payloads, manifest_bytes), train_n, val_n in ((snap_a, 4, 2), (snap_b, 1, 3)):
        for partition, expected_n in (("train", train_n), ("validation", val_n)):
            build = build_partition_matrix(snapshot, partition, _provider(payloads))
            expected_ids = sorted(
                m.dataset_id for m in snapshot.members if m.partition == partition
            )
            assert [m.dataset_id for m in build.matrix.members] == expected_ids
            assert build.matrix.row_count == expected_n  # snapshot-derived, never 50/10/15
            assert build.matrix.column_count == 129
            assert build.matrix.epoch == snapshot.epoch
            assert build.matrix.snapshot_hash == snapshot.snapshot_hash
            result = verify_matrix_payload_with_trust(
                build.matrix,
                manifest_bytes,
                build.vectors,
                _provider(payloads),
                _bundle_for(manifest_bytes),
            )
            assert result.ok, result.failed()


def test_grandfathered_assignments_consumed_unchanged(snap_b) -> None:
    snapshot, payloads, _ = snap_b
    # validation(3) > train(1): an allocation no percentage policy would produce —
    # consumed verbatim, no reallocation, no split arithmetic.
    train = build_partition_matrix(snapshot, "train", _provider(payloads)).matrix
    validation = build_partition_matrix(snapshot, "validation", _provider(payloads)).matrix
    assert [m.dataset_id for m in train.members] == ["ds-b19-01"]
    assert [m.dataset_id for m in validation.members] == ["ds-b19-02", "ds-b19-03", "ds-b19-04"]


def test_empty_partition_yields_zero_row_matrix() -> None:
    snapshot, payloads, _ = make_snapshot(
        [("ds-c-01", "chr18", "train"), ("ds-c-02", "chr20", "train")], epoch=5
    )
    build = build_partition_matrix(snapshot, "validation", _provider(payloads))
    assert build.matrix.row_count == 0 and build.matrix.members == ()


def test_test_partition_rejected_before_payload_access(snap_a) -> None:
    snapshot, _, _ = snap_a
    calls: list[str] = []

    def sentinel(member: SnapshotMember) -> bytes:
        calls.append(member.dataset_id)
        raise AssertionError("payload provider must never be invoked for a test request")

    with pytest.raises(ForbiddenPartitionError):
        build_partition_matrix(snapshot, "test", sentinel)
    assert calls == []  # proven untouched
    with pytest.raises(ForbiddenPartitionError):
        snapshot.members_for("test")
    with pytest.raises(ForbiddenPartitionError):
        assemble_matrix(snapshot, "test", [])


def test_provider_invoked_only_for_requested_partition(snap_a) -> None:
    snapshot, payloads, _ = snap_a
    train_ids = {m.dataset_id for m in snapshot.members if m.partition == "train"}
    calls: list[str] = []

    def strict(member: SnapshotMember) -> bytes:
        calls.append(member.dataset_id)
        assert member.dataset_id in train_ids, "non-train payload requested"
        return payloads[member.dataset_id]

    build_partition_matrix(snapshot, "train", strict)
    assert sorted(calls) == sorted(train_ids)


def test_assemble_missing_extra_duplicate_rejected(snap_a) -> None:
    snapshot, payloads, _ = snap_a
    build = build_partition_matrix(snapshot, "train", _provider(payloads))
    vectors = list(build.vectors)
    with pytest.raises(MatrixAssemblyError, match="missing"):
        assemble_matrix(snapshot, "train", vectors[1:])
    with pytest.raises(MatrixAssemblyError, match="duplicate"):
        assemble_matrix(snapshot, "train", vectors + [vectors[0]])
    val_member = next(m for m in snapshot.members if m.partition == "validation")
    val_vector = build_feature_vector(
        payloads[val_member.dataset_id],
        val_member,
        epoch=snapshot.epoch,
        snapshot_hash=snapshot.snapshot_hash,
    )
    with pytest.raises(MatrixAssemblyError, match="extra"):
        assemble_matrix(snapshot, "train", vectors + [val_vector])


def test_assemble_rejects_wrong_bindings(snap_a) -> None:
    snapshot, payloads, _ = snap_a
    members = snapshot.members_for("train")
    good = [
        build_feature_vector(
            payloads[m.dataset_id], m, epoch=snapshot.epoch, snapshot_hash=snapshot.snapshot_hash
        )
        for m in members
    ]
    wrong_epoch = [
        build_feature_vector(
            payloads[m.dataset_id], m, epoch=99, snapshot_hash=snapshot.snapshot_hash
        )
        for m in members
    ]
    with pytest.raises(SnapshotIdentityMismatchError, match="epoch/snapshot"):
        assemble_matrix(snapshot, "train", wrong_epoch)
    wrong_snapshot = [
        build_feature_vector(
            payloads[m.dataset_id], m, epoch=snapshot.epoch, snapshot_hash="e" * 64
        )
        for m in members
    ]
    with pytest.raises(SnapshotIdentityMismatchError, match="epoch/snapshot"):
        assemble_matrix(snapshot, "train", wrong_snapshot)
    # tampered partition / registry / feature-set bindings (model_copy bypasses
    # contract validation deliberately, to prove assembly re-checks).
    with pytest.raises(SnapshotIdentityMismatchError, match="partition"):
        assemble_matrix(
            snapshot,
            "train",
            [good[0].model_copy(update={"partition": "validation"})] + good[1:],
        )
    with pytest.raises(SnapshotIdentityMismatchError, match="registry"):
        assemble_matrix(
            snapshot, "train", [good[0].model_copy(update={"registry_hash": "0" * 64})] + good[1:]
        )
    with pytest.raises(SnapshotIdentityMismatchError, match="feature_set"):
        assemble_matrix(
            snapshot,
            "train",
            [good[0].model_copy(update={"feature_set_hash": "0" * 64})] + good[1:],
        )
    with pytest.raises(SnapshotIdentityMismatchError, match="content_hash"):
        assemble_matrix(
            snapshot, "train", [good[0].model_copy(update={"content_hash": "0" * 64})] + good[1:]
        )


def test_cross_partition_isolation(snap_a) -> None:
    snapshot, payloads, _ = snap_a
    train = build_partition_matrix(snapshot, "train", _provider(payloads)).matrix
    validation = build_partition_matrix(snapshot, "validation", _provider(payloads)).matrix
    train_ids = {m.dataset_id for m in train.members}
    val_ids = {m.dataset_id for m in validation.members}
    test_ids = {m.dataset_id for m in snapshot.members if m.partition == "test"}
    assert train_ids.isdisjoint(val_ids)
    assert train_ids.isdisjoint(test_ids) and val_ids.isdisjoint(test_ids)
    assert train_ids == {m.dataset_id for m in snapshot.members if m.partition == "train"}
    assert val_ids == {m.dataset_id for m in snapshot.members if m.partition == "validation"}


# --------------------------------------------------------------------------- #
# non-mutating verifier: recompute, never repair
# --------------------------------------------------------------------------- #
def test_verifier_passes_logical_and_payload_levels(snap_a) -> None:
    snapshot, payloads, manifest_bytes = snap_a
    build = build_partition_matrix(snapshot, "train", _provider(payloads))
    logical = verify_matrix(build.matrix, snapshot, build.vectors)
    assert logical.ok, logical.failed()
    assert "feature_values_hashes_recomputed_from_vectors" in logical.checks
    assert "vector_values_valid_for_column_kinds" in logical.checks
    assert "feature_values_hashes_recomputed_from_bytes" not in logical.checks
    payload = verify_matrix_payload_with_trust(
        build.matrix,
        manifest_bytes,
        build.vectors,
        _provider(payloads),
        _bundle_for(manifest_bytes),
    )
    assert payload.ok, payload.failed()
    assert payload.checks["member_manifest_verified"]
    assert "feature_values_hashes_recomputed_from_bytes" in payload.checks
    assert "vector_values_match_recomputed_extraction" in payload.checks


def _consistent_forgery(build, index: int, new_value: float):
    """Mutate one value, recompute vector_hash, rebind the MatrixMember, recompute
    matrix_hash — but keep the OLD feature_values_hash."""
    v0 = build.vectors[0]
    values = list(v0.values)
    values[index] = new_value
    forged = v0.model_copy(update={"values": tuple(values)})
    forged = forged.model_copy(update={"vector_hash": vector_hash(forged)})
    members = list(build.matrix.members)
    pos = next(i for i, m in enumerate(members) if m.dataset_id == v0.dataset_id)
    members[pos] = members[pos].model_copy(update={"vector_hash": forged.vector_hash})
    matrix = build.matrix.model_copy(update={"members": tuple(members)})
    matrix = matrix.model_copy(update={"matrix_hash": matrix_hash(matrix)})
    vectors = [forged] + [v for v in build.vectors if v.dataset_id != v0.dataset_id]
    return matrix, vectors


def test_consistent_forgery_fails_specifically_via_fvh_recompute(snap_a) -> None:
    snapshot, payloads, _ = snap_a
    build = build_partition_matrix(snapshot, "train", _provider(payloads))
    real_index = next(
        i for i, c in enumerate(canonical_feature_set().columns) if c.value_kind == "REAL"
    )
    matrix, vectors = _consistent_forgery(build, real_index, 0.777)
    result = verify_matrix(matrix, snapshot, vectors)
    assert not result.ok
    # vector_hash, member binding and matrix_hash were all consistently re-forged —
    # ONLY the value-level recompute catches it.
    assert result.failed() == ("feature_values_hashes_recomputed_from_vectors",)


def test_invalid_fraction_forgery_rejected_logically(snap_a) -> None:
    snapshot, payloads, _ = snap_a
    build = build_partition_matrix(snapshot, "train", _provider(payloads))
    fraction_index = next(
        i for i, c in enumerate(canonical_feature_set().columns) if c.value_kind == "FRACTION"
    )
    matrix, vectors = _consistent_forgery(build, fraction_index, 1.5)
    result = verify_matrix(matrix, snapshot, vectors)
    assert not result.ok
    assert "vector_values_valid_for_column_kinds" in result.failed()
    assert "feature_values_hashes_recomputed_from_vectors" in result.failed()


def test_verifier_detects_vector_tampering(snap_a) -> None:
    snapshot, payloads, _ = snap_a
    build = build_partition_matrix(snapshot, "train", _provider(payloads))
    tampered_values = list(build.vectors[0].values)
    tampered_values[5] = 0.9999
    tampered = [build.vectors[0].model_copy(update={"values": tuple(tampered_values)})] + list(
        build.vectors[1:]
    )
    result = verify_matrix(build.matrix, snapshot, tampered)
    assert not result.ok
    assert "vector_hashes_recomputed_match" in result.failed()
    forged_hash = [build.vectors[0].model_copy(update={"vector_hash": "0" * 64})] + list(
        build.vectors[1:]
    )
    result2 = verify_matrix(build.matrix, snapshot, forged_hash)
    assert not result2.ok
    assert "matrix_members_bind_vector_hashes" in result2.failed()


def test_verifier_detects_matrix_tampering(snap_a) -> None:
    snapshot, payloads, _ = snap_a
    build = build_partition_matrix(snapshot, "train", _provider(payloads))
    matrix = build.matrix
    reordered = matrix.model_copy(update={"members": tuple(reversed(matrix.members))})
    r = verify_matrix(reordered, snapshot, build.vectors)
    assert "members_ordered_and_unique" in r.failed()
    assert "matrix_hash_recomputed_match" in r.failed()
    member_swap = matrix.model_copy(
        update={
            "members": (matrix.members[0].model_copy(update={"vector_hash": "0" * 64}),)
            + matrix.members[1:]
        }
    )
    r2 = verify_matrix(member_swap, snapshot, build.vectors)
    assert "matrix_members_bind_vector_hashes" in r2.failed()
    assert "matrix_hash_recomputed_match" in r2.failed()
    for field, value, check in (
        ("epoch", 42, "epoch_binds_snapshot"),
        ("snapshot_hash", "e" * 64, "snapshot_hash_binds_snapshot"),
        ("registry_hash", "0" * 64, "registry_identity_accepted"),
        ("feature_set_hash", "0" * 64, "feature_set_identity_frozen"),
        ("row_count", 40, "row_count_matches_membership"),
        ("column_count", 128, "column_count_is_frozen_129"),
    ):
        bad = matrix.model_copy(update={field: value})
        result = verify_matrix(bad, snapshot, build.vectors)
        assert check in result.failed(), (field, result.failed())


def test_verifier_detects_wrong_snapshot(snap_a, snap_b) -> None:
    snapshot_a, payloads_a, _ = snap_a
    snapshot_b, _, _ = snap_b
    build = build_partition_matrix(snapshot_a, "train", _provider(payloads_a))
    result = verify_matrix(build.matrix, snapshot_b, build.vectors)
    assert not result.ok
    assert "partition_membership_matches_snapshot" in result.failed()


def test_payload_verifier_detects_byte_divergence(snap_a) -> None:
    snapshot, payloads, manifest_bytes = snap_a
    build = build_partition_matrix(snapshot, "train", _provider(payloads))
    corrupted = dict(payloads)
    first_train = build.matrix.members[0].dataset_id
    corrupted[first_train] = corrupted[first_train] + b" "
    result = verify_matrix_payload_with_trust(
        build.matrix,
        manifest_bytes,
        build.vectors,
        _provider(corrupted),
        _bundle_for(manifest_bytes),
    )
    assert not result.ok
    assert "feature_values_hashes_recomputed_from_bytes" in result.failed()
    # swapped payloads: the wrong member's bytes fail the manifest-bound profile_sha256.
    swapped = dict(payloads)
    ids = [m.dataset_id for m in build.matrix.members[:2]]
    swapped[ids[0]], swapped[ids[1]] = swapped[ids[1]], swapped[ids[0]]
    result2 = verify_matrix_payload_with_trust(
        build.matrix,
        manifest_bytes,
        build.vectors,
        _provider(swapped),
        _bundle_for(manifest_bytes),
    )
    assert "feature_values_hashes_recomputed_from_bytes" in result2.failed()


# --------------------------------------------------------------------------- #
# accepted epoch-1 trust anchors (pinned; NO hashes supplied by these tests)
# --------------------------------------------------------------------------- #
def _committed_manifest_bytes() -> bytes:
    return (REPO_ROOT / "manifests" / "profile_snapshot_epoch1_members.json").read_bytes()


def _rehash_consistent(raw: dict[str, Any]) -> bytes:
    """Recompute snapshot_hash (freeze formula) AND member_manifest_hash so the
    forgery is fully self-consistent — only the pinned anchors can reject it."""
    raw["snapshot_hash"] = canonical_hash(
        {
            "epoch": raw["epoch"],
            "split_manifest_hash": raw["split_manifest_hash"],
            "registry_snapshot_hash": raw["registry_snapshot_hash"],
            "members": [
                {
                    "dataset_id": m["dataset_id"],
                    "partition": m["partition"],
                    "content_hash": m["content_hash"],
                    "feature_values_hash": m["feature_values_hash"],
                }
                for m in sorted(raw["members"], key=lambda m: str(m["dataset_id"]))
            ],
        }
    )
    content = {k: v for k, v in raw.items() if k != "member_manifest_hash"}
    raw["member_manifest_hash"] = canonical_hash(content)
    return json.dumps(raw).encode("utf-8")


def test_accepted_loader_rejects_profile_sha256_swap_with_rehash() -> None:
    raw = json.loads(_committed_manifest_bytes())
    raw["members"][0]["profile_sha256"] = "0" * 64
    content = {k: v for k, v in raw.items() if k != "member_manifest_hash"}
    raw["member_manifest_hash"] = canonical_hash(content)  # self-consistent forgery
    with pytest.raises(MemberManifestHashMismatchError):
        load_accepted_epoch1_member_manifest(json.dumps(raw).encode())


def test_accepted_loader_rejects_core_member_forgery_with_full_rehash() -> None:
    raw = json.loads(_committed_manifest_bytes())
    member = raw["members"][0]
    member["partition"] = "validation" if member["partition"] != "validation" else "train"
    forged = _rehash_consistent(raw)  # snapshot_hash AND member_manifest_hash recomputed
    with pytest.raises(MemberManifestHashMismatchError):
        load_accepted_epoch1_member_manifest(forged)


def test_accepted_loader_rejects_substituted_split_and_registry_hashes() -> None:
    raw = json.loads(_committed_manifest_bytes())
    raw["split_manifest_hash"] = "1" * 64
    with pytest.raises(MemberManifestHashMismatchError):
        load_accepted_epoch1_member_manifest(_rehash_consistent(raw))
    raw = json.loads(_committed_manifest_bytes())
    raw["registry_snapshot_hash"] = "2" * 64
    for m in raw["members"]:
        m["registry_snapshot_hash"] = "2" * 64
    with pytest.raises(MemberManifestHashMismatchError):
        load_accepted_epoch1_member_manifest(_rehash_consistent(raw))


def test_accepted_payload_verifier_rejects_before_provider_access(snap_a) -> None:
    snapshot, payloads, _ = snap_a
    build = build_partition_matrix(snapshot, "train", _provider(payloads))
    raw = json.loads(_committed_manifest_bytes())
    raw["members"][0]["profile_sha256"] = "0" * 64
    forged = _rehash_consistent(raw)
    calls: list[str] = []

    def sentinel(member: SnapshotMember) -> bytes:
        calls.append(member.dataset_id)
        raise AssertionError("provider must never run for a rejected manifest")

    with pytest.raises(MemberManifestHashMismatchError):
        verify_accepted_epoch1_matrix_payload(build.matrix, forged, build.vectors, sentinel)
    assert calls == []  # rejected BEFORE any payload access


def test_accepted_loader_rejects_synthetic_manifests(snap_a) -> None:
    # generic uneven synthetic snapshots are supported ONLY through the explicit
    # non-production trust-bundle boundary — the accepted boundary refuses them.
    _, _, manifest_bytes = snap_a
    with pytest.raises(MemberManifestHashMismatchError):
        load_accepted_epoch1_member_manifest(manifest_bytes)
