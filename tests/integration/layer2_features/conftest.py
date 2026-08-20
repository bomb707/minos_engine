"""Real PostgreSQL 16 fixtures for L2-E (feature view) integration tests.

Provisions a session database at the 0005 head and seeds TWO fully synthetic,
non-75, uneven-chromosome snapshots (epoch 1: 9 members 4/2/3; epoch 2: 6 members
1/3/2 with the mandatory parent chain) end-to-end: dataset registry, split epoch
allocations, accepted bam_profiles rows whose EXACT profile artifact bytes live on
the test filesystem and are registered in ``catalog.artifacts``, and the frozen
profile snapshots — everything the E3 builder verifies against.

Profile documents are generated from the accepted ``bam-profile-v1`` JSON schema
itself (no corpus access, no fixed counts). Snapshot manifests use the accepted
freeze/member-manifest formulas so the explicitly TEST-ONLY trust-bundle boundary can
load them; the production accepted-epoch-1 boundary rejects them by construction.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Engine, create_engine, text

from minos_engine.common.hashing import canonical_hash
from minos_engine.layer2.features.contracts import canonical_feature_set
from minos_engine.layer2.features.extraction import ManifestTrustBundle
from minos_engine.layer2.ingest.contracts import (
    canonical_feature_values_hash,
    extract_eligible_feature_values,
)
from minos_engine.layer2.ingest.validation import l1_feature_values_hash_from_document
from minos_engine.layer2.prerequisites import PROFILER_CONFIG_HASH, PROFILER_VERSION
from minos_engine.schema_registry import load_schema, validate_against
from minos_engine.storage.database import normalize_database_url
from tests.integration.layer2_db.conftest import (
    alembic_upgrade,
    pg_base_url,  # noqa: F401 - session fixture re-export
    scratch_database,
)

# --------------------------------------------------------------------------- #
# synthetic profile documents (accepted-schema-driven; no corpus)
# --------------------------------------------------------------------------- #


def _gen(schema: dict[str, Any], root: dict[str, Any] | None = None) -> Any:
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
        return [_gen(schema.get("items", {"type": "string"}), root) for _ in range(n)]
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


def make_profile_doc(profile_id: str, seed: int) -> dict[str, Any]:
    doc: dict[str, Any] = json.loads(_template_json())
    doc["schema_version"] = "bam-profile-v1"
    doc["profile_id"] = profile_id
    doc["status"] = "COMPLETE"
    for i, column in enumerate(canonical_feature_set().columns):
        _set_path(doc, column.path, 0.001 * i + 0.000001 * seed)
    validate_against("bam-profile-v1", doc)
    return doc


def _h(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


# --------------------------------------------------------------------------- #
# synthetic snapshots: manifest bytes + trust bundle + payload files
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class SyntheticSnapshot:
    epoch: int
    spec: tuple[tuple[str, str, str], ...]  # (dataset_id, chromosome, partition)
    manifest_bytes: bytes
    trust: ManifestTrustBundle
    split_manifest_hash: str
    registry_snapshot_hash: str
    snapshot_hash: str
    members: tuple[dict[str, Any], ...]
    documents: dict[str, dict[str, Any]]
    payload_paths: dict[str, Path]


_SPEC_A: tuple[tuple[str, str, str], ...] = (
    ("ds-a18-01", "chr18", "train"),
    ("ds-a18-02", "chr18", "train"),
    ("ds-a18-03", "chr18", "train"),
    ("ds-a18-04", "chr18", "validation"),
    ("ds-a20-05", "chr20", "train"),
    ("ds-a20-06", "chr20", "validation"),
    ("ds-a20-07", "chr20", "test"),
    ("ds-a21-08", "chr21", "test"),
    ("ds-a21-09", "chr21", "test"),
)

_SPEC_B: tuple[tuple[str, str, str], ...] = (
    ("ds-b19-01", "chr19", "train"),
    ("ds-b19-02", "chr19", "validation"),
    ("ds-b19-03", "chr19", "validation"),
    ("ds-b19-04", "chr19", "validation"),
    ("ds-b19-05", "chr19", "test"),
    ("ds-b22-06", "chr22", "test"),
)


def build_synthetic_snapshot(
    spec: tuple[tuple[str, str, str], ...], *, epoch: int, payload_dir: Path
) -> SyntheticSnapshot:
    split_manifest_hash = _h(f"synthetic-split:{epoch}")
    registry_snapshot_hash = _h(f"synthetic-registry:{epoch}")
    payload_dir.mkdir(parents=True, exist_ok=True)
    members: list[dict[str, Any]] = []
    documents: dict[str, dict[str, Any]] = {}
    payload_paths: dict[str, Path] = {}
    for i, (dataset_id, chromosome, partition) in enumerate(spec):
        profile_id = hashlib.md5(
            f"{epoch}:{dataset_id}".encode(), usedforsecurity=False
        ).hexdigest()
        doc = make_profile_doc(profile_id, seed=epoch * 1000 + i)
        payload = json.dumps(doc).encode("utf-8")
        path = payload_dir / f"{dataset_id}.bam-profile-v1.json"
        path.write_bytes(payload)
        members.append(
            {
                "dataset_id": dataset_id,
                "round_id": hashlib.md5(
                    f"round:{epoch}:{dataset_id}".encode(), usedforsecurity=False
                ).hexdigest(),
                "chromosome": chromosome,
                "partition": partition,
                "identity_tuple_hash": _h(f"identity:{epoch}:{dataset_id}"),
                "content_hash": _h(f"content:{epoch}:{dataset_id}"),
                "feature_values_hash": canonical_feature_values_hash(
                    extract_eligible_feature_values(doc)
                ),
                "l1_feature_values_hash": l1_feature_values_hash_from_document(doc),
                "profile_id": profile_id,
                "profile_sha256": hashlib.sha256(payload).hexdigest(),
                "profile_manifest_sha256": _h(f"pm:{epoch}:{dataset_id}"),
                "windows_sha256": _h(f"win:{epoch}:{dataset_id}"),
                "attestation_hash": _h(f"att:{epoch}:{dataset_id}"),
                "m5_status": "ABSENT",
                "integrity_degraded": True,
                "profile_status": "COMPLETE",
                "profiler_version": PROFILER_VERSION,
                "profiler_config_hash": PROFILER_CONFIG_HASH,
                "registry_snapshot_hash": registry_snapshot_hash,
            }
        )
        documents[dataset_id] = doc
        payload_paths[dataset_id] = path
    members.sort(key=lambda m: str(m["dataset_id"]))
    snapshot_hash = canonical_hash(
        {
            "epoch": epoch,
            "split_manifest_hash": split_manifest_hash,
            "registry_snapshot_hash": registry_snapshot_hash,
            "members": [
                {
                    "dataset_id": m["dataset_id"],
                    "partition": m["partition"],
                    "content_hash": m["content_hash"],
                    "feature_values_hash": m["feature_values_hash"],
                }
                for m in members
            ],
        }
    )
    content: dict[str, Any] = {
        "schema_version": "profile-snapshot-members-v1",
        "epoch": epoch,
        "split_manifest_hash": split_manifest_hash,
        "registry_snapshot_hash": registry_snapshot_hash,
        "snapshot_hash": snapshot_hash,
        "member_count": len(members),
        "members": members,
    }
    manifest = {**content, "member_manifest_hash": canonical_hash(content)}
    manifest_bytes = json.dumps(manifest).encode("utf-8")
    trust = ManifestTrustBundle(
        epoch=epoch,
        member_manifest_hash=manifest["member_manifest_hash"],
        snapshot_hash=snapshot_hash,
        split_manifest_hash=split_manifest_hash,
        registry_snapshot_hash=registry_snapshot_hash,
    )
    return SyntheticSnapshot(
        epoch=epoch,
        spec=spec,
        manifest_bytes=manifest_bytes,
        trust=trust,
        split_manifest_hash=split_manifest_hash,
        registry_snapshot_hash=registry_snapshot_hash,
        snapshot_hash=snapshot_hash,
        members=tuple(members),
        documents=documents,
        payload_paths=payload_paths,
    )


# --------------------------------------------------------------------------- #
# database seeding (owner-run inserts against the 0005 head)
# --------------------------------------------------------------------------- #
def seed_snapshot(
    engine: Engine, snap: SyntheticSnapshot, *, parent: SyntheticSnapshot | None = None
) -> None:
    counts = {"train": 0, "validation": 0, "test": 0}
    for m in snap.members:
        counts[str(m["partition"])] += 1
    with engine.begin() as conn:
        parent_row = None
        if snap.epoch > 1:
            assert parent is not None, "epoch > 1 needs a parent snapshot"
            parent_row = (
                conn.execute(
                    text("SELECT id FROM catalog.split_snapshots WHERE epoch = :e"),
                    {"e": parent.epoch},
                )
                .mappings()
                .one()
            )
        split_snapshot_id = conn.execute(
            text(
                "INSERT INTO catalog.split_snapshots (epoch, salt, split_policy_version, "
                " policy_hash, manifest_hash, registry_snapshot_hash, "
                " ancestor_v1_dataset_registry_hash, parent_snapshot_id, parent_epoch, "
                " parent_manifest_hash, parent_registry_snapshot_hash, transition_count, "
                " sample_count, count_train, count_validation, count_test) "
                "VALUES (:e, 'synthetic', 'split-policy-v2', :ph, :mh, :rh, :ah, :pid, "
                " :pe, :pmh, :prh, 0, :n, :ct, :cv, :cx) RETURNING id"
            ),
            {
                "e": snap.epoch,
                "ph": _h(f"policy:{snap.epoch}"),
                "mh": snap.split_manifest_hash,
                "rh": snap.registry_snapshot_hash,
                "ah": _h(f"ancestor:{snap.epoch}"),
                "pid": str(parent_row["id"]) if parent_row else None,
                "pe": parent.epoch if parent is not None and snap.epoch > 1 else None,
                "pmh": parent.split_manifest_hash if parent and snap.epoch > 1 else None,
                "prh": parent.registry_snapshot_hash if parent and snap.epoch > 1 else None,
                "n": len(snap.members),
                "ct": counts["train"],
                "cv": counts["validation"],
                "cx": counts["test"],
            },
        ).scalar_one()

        profile_snapshot_id = conn.execute(
            text(
                "INSERT INTO profiling.profile_snapshots (epoch, split_snapshot_id, "
                " split_manifest_hash, registry_snapshot_hash, member_count, snapshot_hash) "
                "VALUES (:e, :sid, :smh, :rh, :n, :sh) RETURNING id"
            ),
            {
                "e": snap.epoch,
                "sid": str(split_snapshot_id),
                "smh": snap.split_manifest_hash,
                "rh": snap.registry_snapshot_hash,
                "n": len(snap.members),
                "sh": snap.snapshot_hash,
            },
        ).scalar_one()

        for m in snap.members:
            dataset_id = str(m["dataset_id"])
            registry_id = conn.execute(
                text(
                    "INSERT INTO catalog.dataset_registry (dataset_id, round_id, chromosome, "
                    " region_source, region_start0, region_end0_exclusive, region_length_bp, "
                    " region_coordinate_system, region_hash, bam_sha256, bai_sha256, "
                    " reference_sha256, fai_sha256, bam_size_bytes, parameter_space_hash, "
                    " feature_registry_hash, identity_tuple_hash, manifest_hash, "
                    " split_algorithm_version, split_salt, allocation_digest) "
                    "VALUES (:d, :r, :c, 'synthetic', 0, 1000, 1000, 'zero_based_half_open', "
                    " :rgh, :bam, :bai, :ref, :fai, 1000, :psh, :frh, :ith, :mh, 'v1', 's', "
                    " :ad) RETURNING id"
                ),
                {
                    "d": dataset_id,
                    "r": str(m["round_id"]),
                    "c": str(m["chromosome"]),
                    "rgh": _h(f"region:{snap.epoch}:{dataset_id}"),
                    "bam": _h(f"bam:{snap.epoch}:{dataset_id}"),
                    "bai": _h(f"bai:{snap.epoch}:{dataset_id}"),
                    "ref": _h(f"ref:{snap.epoch}:{dataset_id}"),
                    "fai": _h(f"fai:{snap.epoch}:{dataset_id}"),
                    "psh": _h("param-space"),
                    "frh": _h("feature-registry"),
                    "ith": str(m["identity_tuple_hash"]),
                    "mh": _h(f"registry-manifest:{snap.epoch}"),
                    "ad": _h(f"alloc:{snap.epoch}:{dataset_id}"),
                },
            ).scalar_one()

            conn.execute(
                text(
                    "INSERT INTO catalog.split_epoch_allocations (snapshot_id, "
                    " dataset_registry_id, partition, origin_epoch, assignment_source) "
                    "VALUES (:s, :d, :p, :e, 'v1-inherited')"
                ),
                {
                    "s": str(split_snapshot_id),
                    "d": str(registry_id),
                    "p": str(m["partition"]),
                    "e": snap.epoch,
                },
            )

            payload_path = snap.payload_paths[dataset_id]
            payload = payload_path.read_bytes()
            artifact_ids: dict[str, str] = {}
            for kind, uri, sha in (
                ("l2d:profile-json", str(payload_path), str(m["profile_sha256"])),
                (
                    "l2d:profile-manifest-json",
                    f"{payload_path}.manifest",
                    str(m["profile_manifest_sha256"]),
                ),
                ("l2d:windows-parquet", f"{payload_path}.windows", str(m["windows_sha256"])),
            ):
                artifact_ids[kind] = str(
                    conn.execute(
                        text(
                            "INSERT INTO catalog.artifacts (uri, sha256, size_bytes, "
                            " media_type, provenance) VALUES (:u, :h, :s, 'application/json', "
                            " :k) RETURNING id"
                        ),
                        {"u": uri, "h": sha, "s": len(payload), "k": kind},
                    ).scalar_one()
                )

            bam_profile_id = conn.execute(
                text(
                    "INSERT INTO profiling.bam_profiles (dataset_registry_id, profile_id, "
                    " bam_sha256, bai_sha256, reference_sha256, fai_sha256, region_hash, "
                    " identity_tuple_hash, m5_status, integrity_degraded, attestation_hash, "
                    " registry_snapshot_hash, profile_status, profiler_version, "
                    " profiler_config_hash, windows_row_count, feature_values_hash, "
                    " l1_feature_values_hash, eligible_value_count, profile_document, "
                    " profile_sha256, profile_manifest_sha256, windows_sha256, "
                    " profile_artifact_id, profile_manifest_artifact_id, windows_artifact_id, "
                    " ingestion_key, content_hash) "
                    "VALUES (:reg, :pid, :bam, :bai, :ref, :fai, :rgh, :ith, 'ABSENT', TRUE, "
                    " :att, :rsh, 'COMPLETE', :pv, :pch, 10, :fvh, :l1h, 129, "
                    " CAST(:doc AS jsonb), :psha, :msha, :wsha, :pa, :ma, :wa, :ik, :ch) "
                    "RETURNING id"
                ),
                {
                    "reg": str(registry_id),
                    "pid": str(m["profile_id"]),
                    "bam": _h(f"bam:{snap.epoch}:{dataset_id}"),
                    "bai": _h(f"bai:{snap.epoch}:{dataset_id}"),
                    "ref": _h(f"ref:{snap.epoch}:{dataset_id}"),
                    "fai": _h(f"fai:{snap.epoch}:{dataset_id}"),
                    "rgh": _h(f"region:{snap.epoch}:{dataset_id}"),
                    "ith": str(m["identity_tuple_hash"]),
                    "att": str(m["attestation_hash"]),
                    "rsh": str(m["registry_snapshot_hash"]),
                    "pv": str(m["profiler_version"]),
                    "pch": str(m["profiler_config_hash"]),
                    "fvh": str(m["feature_values_hash"]),
                    "l1h": str(m["l1_feature_values_hash"]),
                    "doc": json.dumps(snap.documents[dataset_id]),
                    "psha": str(m["profile_sha256"]),
                    "msha": str(m["profile_manifest_sha256"]),
                    "wsha": str(m["windows_sha256"]),
                    "pa": artifact_ids["l2d:profile-json"],
                    "ma": artifact_ids["l2d:profile-manifest-json"],
                    "wa": artifact_ids["l2d:windows-parquet"],
                    "ik": canonical_hash(
                        {
                            "identity_tuple_hash": str(m["identity_tuple_hash"]),
                            "profile_id": str(m["profile_id"]),
                        }
                    ),
                    "ch": str(m["content_hash"]),
                },
            ).scalar_one()

            conn.execute(
                text(
                    "INSERT INTO profiling.profile_snapshot_members (profile_snapshot_id, "
                    " bam_profile_id, dataset_registry_id, partition, feature_values_hash) "
                    "VALUES (:s, :b, :d, :p, :f)"
                ),
                {
                    "s": str(profile_snapshot_id),
                    "b": str(bam_profile_id),
                    "d": str(registry_id),
                    "p": str(m["partition"]),
                    "f": str(m["feature_values_hash"]),
                },
            )


# --------------------------------------------------------------------------- #
# session fixtures
# --------------------------------------------------------------------------- #
@pytest.fixture(scope="session")
def l2e_db_url(pg_base_url: str) -> Any:  # noqa: F811 - fixture name shadows import
    with scratch_database(pg_base_url, "minos_l2e_features") as url:
        alembic_upgrade(url, "head")
        yield url


@pytest.fixture(scope="session")
def l2e_engine(l2e_db_url: str) -> Any:
    engine = create_engine(normalize_database_url(l2e_db_url))
    yield engine
    engine.dispose()


@pytest.fixture(scope="session")
def snap_a(l2e_engine: Engine, tmp_path_factory: pytest.TempPathFactory) -> SyntheticSnapshot:
    snap = build_synthetic_snapshot(
        _SPEC_A, epoch=1, payload_dir=tmp_path_factory.mktemp("l2e_profiles_a")
    )
    seed_snapshot(l2e_engine, snap)
    return snap


@pytest.fixture(scope="session")
def snap_b(
    l2e_engine: Engine, snap_a: SyntheticSnapshot, tmp_path_factory: pytest.TempPathFactory
) -> SyntheticSnapshot:
    snap = build_synthetic_snapshot(
        _SPEC_B, epoch=2, payload_dir=tmp_path_factory.mktemp("l2e_profiles_b")
    )
    seed_snapshot(l2e_engine, snap, parent=snap_a)
    return snap


@pytest.fixture(scope="session")
def artifact_root(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("l2e_artifacts")
    for partition in ("train", "validation"):
        (root / "l2e" / partition).mkdir(parents=True, exist_ok=True)
        (root / "l2e" / partition).chmod(0o700)
    return root


#: Small chained snapshots for rollback/concurrency behavior (parent chain mandatory).
_EXTRA_SPECS: dict[int, tuple[tuple[str, str, str], ...]] = {
    3: (
        ("ds-e3-01", "chr18", "train"),
        ("ds-e3-02", "chr19", "train"),
        ("ds-e3-03", "chr20", "test"),
    ),
    4: (("ds-e4-01", "chr20", "train"), ("ds-e4-02", "chr21", "train")),
    5: (("ds-e5-01", "chr21", "train"), ("ds-e5-02", "chr22", "train")),
    6: (("ds-e6-01", "chr18", "train"), ("ds-e6-02", "chr19", "train")),
}


@pytest.fixture(scope="session")
def extra_snaps(
    l2e_engine: Engine,
    snap_b: SyntheticSnapshot,
    tmp_path_factory: pytest.TempPathFactory,
) -> dict[int, SyntheticSnapshot]:
    snaps: dict[int, SyntheticSnapshot] = {}
    parent = snap_b
    for epoch in (3, 4, 5, 6):
        snap = build_synthetic_snapshot(
            _EXTRA_SPECS[epoch],
            epoch=epoch,
            payload_dir=tmp_path_factory.mktemp(f"l2e_profiles_e{epoch}"),
        )
        seed_snapshot(l2e_engine, snap, parent=parent)
        snaps[epoch] = snap
        parent = snap
    return snaps


@pytest.fixture(scope="session")
def built(
    l2e_engine: Engine,
    snap_a: SyntheticSnapshot,
    snap_b: SyntheticSnapshot,
    artifact_root: Path,
) -> dict[tuple[str, str], Any]:
    """The four uneven synthetic matrices, built once through the TEST-ONLY trust
    boundary (a: 4 train / 2 validation; b: 1 train / 3 validation — verbatim)."""
    from minos_engine.storage.feature_matrix import build_feature_matrix_with_trust

    results: dict[tuple[str, str], Any] = {}
    for snap, name in ((snap_a, "a"), (snap_b, "b")):
        for partition in ("train", "validation"):
            results[(name, partition)] = build_feature_matrix_with_trust(
                l2e_engine,
                snap.manifest_bytes,
                snap.trust,
                partition,
                artifact_root=artifact_root,
            )
    return results
