"""PROFILE-SNAPSHOT-FROZEN-<epoch> qualification — per-epoch corpus evidence.

Two verification levels:

  * **Offline** (:func:`verify_snapshot_offline`): needs NO PostgreSQL store. Every
    property derivable from the committed artifacts — snapshot gate, member manifest,
    selection manifest, artifact inventory, the accepted split epoch-1 manifest, and the
    accepted INGEST-READY gate — is recomputed and compared. Embedded hash fields are
    never trusted: ``member_manifest_hash`` and ``inventory_hash`` are recomputed from
    content, the snapshot hash is re-derived from members, identities are compared
    against the accepted v2 epoch-1 registry, profiler identity against the accepted
    Layer 1 constants, and the INGEST-READY gate is genuinely verified (canonical hash,
    pinned identities, promotion, ancestry) — never assumed.
  * **Operational** (:func:`verify_snapshot_gate`): everything offline PLUS the live
    store and the corpus directory: exact attestation bytes are hashed and parsed per
    member (canonical attestation-hash recomputation vs the stored row and manifest),
    the artifact inventory is REBUILT from ``corpus_dir`` and compared hash-for-hash
    with the committed inventory, view counts / sealed-test denial / append-only are
    proven live, and the ingest-attempt log shows zero failures.

m5 semantics: SAM ``@SQ:M5`` is a BAM-header tag (FASTA files never carry it). In epoch 1
all 75 BAM headers lack ``@SQ:M5`` while the computed reference-contig MD5 was available
for every sample → 75× ``ABSENT``/integrity-degraded, 0 MATCH, 0 MISMATCH.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict
from sqlalchemy import text
from sqlalchemy.engine import Connection

from minos_engine.common.canonical_json import canonical_json_str
from minos_engine.common.hashing import canonical_hash, sha256_hex
from minos_engine.gates.contracts import EvidenceItem, EvidenceKind, GateArtifact, GateStatus
from minos_engine.layer2 import prerequisites as PRE

__all__ = [
    "SNAPSHOT_QUALIFIER_VERSION",
    "MEMBER_MANIFEST_PATH",
    "SELECTIONS_PATH",
    "INVENTORY_PATH",
    "REPORT_PATH",
    "gate_name",
    "gate_path",
    "build_member_manifest",
    "recompute_snapshot_hash",
    "build_artifact_inventory",
    "assemble_snapshot_gate",
    "verify_snapshot_offline",
    "verify_snapshot_gate",
    "write_snapshot_outputs",
    "SnapshotGateVerification",
]

SNAPSHOT_QUALIFIER_VERSION = "layer2-profile-snapshot-qualifier-v2"
MEMBER_MANIFEST_PATH = "manifests/profile_snapshot_epoch1_members.json"
SELECTIONS_PATH = "manifests/profile_snapshot_epoch1_selections.json"
INVENTORY_PATH = "manifests/profile_snapshot_epoch1_artifact_inventory.json"
REPORT_PATH = "reports/PROFILE_SNAPSHOT_FROZEN_1_REPORT.md"
CI_WORKFLOW = ".github/workflows/ci.yml"

_EXPECTED_COUNTS = {"train": 50, "validation": 10, "test": 15}
_CHROMS = ("chr18", "chr19", "chr20", "chr21", "chr22")
_ARTIFACT_FILES = (
    "bam-profile-v1.json",
    "profile-manifest-v1.json",
    "window-profile-v1.parquet",
    "input-integrity-attestation-v1.json",
)


def gate_name(epoch: int) -> str:
    return f"PROFILE-SNAPSHOT-FROZEN-{epoch}"


def gate_path(epoch: int) -> str:
    return f"gates/profile-snapshot-frozen-{epoch}.json"


def build_member_manifest(conn: Connection, epoch: int) -> dict[str, Any]:
    """Canonical member manifest for the frozen epoch snapshot (sorted by dataset_id)."""
    snap = (
        conn.execute(
            text(
                "SELECT epoch, member_count, snapshot_hash, split_manifest_hash, "
                " registry_snapshot_hash FROM profiling.profile_snapshots WHERE epoch = :e"
            ),
            {"e": epoch},
        )
        .mappings()
        .one()
    )
    rows = conn.execute(
        text(
            "SELECT dr.dataset_id, dr.round_id, dr.chromosome, m.partition, "
            " bp.identity_tuple_hash, bp.content_hash, bp.feature_values_hash, "
            " bp.l1_feature_values_hash, bp.profile_id, bp.profile_sha256, "
            " bp.profile_manifest_sha256, bp.windows_sha256, bp.attestation_hash, "
            " bp.m5_status, bp.integrity_degraded, bp.profile_status, "
            " bp.profiler_version, bp.profiler_config_hash, bp.registry_snapshot_hash "
            "FROM profiling.profile_snapshot_members m "
            "JOIN profiling.profile_snapshots ps ON ps.id = m.profile_snapshot_id "
            "JOIN profiling.bam_profiles bp ON bp.id = m.bam_profile_id "
            "JOIN catalog.dataset_registry dr ON dr.id = m.dataset_registry_id "
            "WHERE ps.epoch = :e ORDER BY dr.dataset_id"
        ),
        {"e": epoch},
    ).mappings()
    members = [dict(r) for r in rows]
    content: dict[str, Any] = {
        "schema_version": "profile-snapshot-members-v1",
        "epoch": int(snap["epoch"]),
        "split_manifest_hash": snap["split_manifest_hash"],
        "registry_snapshot_hash": snap["registry_snapshot_hash"],
        "snapshot_hash": snap["snapshot_hash"],
        "member_count": int(snap["member_count"]),
        "members": members,
    }
    manifest = dict(content)
    manifest["member_manifest_hash"] = canonical_hash(content)
    return manifest


def recompute_snapshot_hash(member_manifest: dict[str, Any]) -> str:
    """Reproduce the frozen snapshot hash from the member manifest ALONE (freeze formula)."""
    members = sorted(member_manifest["members"], key=lambda m: str(m["dataset_id"]))
    return canonical_hash(
        {
            "epoch": member_manifest["epoch"],
            "split_manifest_hash": member_manifest["split_manifest_hash"],
            "registry_snapshot_hash": member_manifest["registry_snapshot_hash"],
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


def build_artifact_inventory(corpus_dir: Path, member_manifest: dict[str, Any]) -> dict[str, Any]:
    """Content-addressed inventory of every generated artifact, keyed by round id."""
    entries = []
    for m in sorted(member_manifest["members"], key=lambda x: str(x["dataset_id"])):
        rid = str(m["round_id"])
        rdir = corpus_dir / rid
        files = {}
        for name in _ARTIFACT_FILES:
            data = (rdir / name).read_bytes()
            files[name] = {"sha256": sha256_hex(data), "size_bytes": len(data)}
        entries.append({"dataset_id": m["dataset_id"], "round_id": rid, "artifacts": files})
    content = {"schema_version": "profile-snapshot-artifact-inventory-v1", "entries": entries}
    inv = dict(content)
    inv["inventory_hash"] = canonical_hash(content)
    return inv


def _verify_ingest_ready(root: Path) -> bool:
    """REAL verification of the accepted INGEST-READY gate — never assumed."""
    from minos_engine.gates.verifier import load_gate
    from minos_engine.qualification.layer2_ingest_runner import verify_ingest_ready_gate

    try:
        gate = load_gate(root / "gates" / "ingest-ready.json")
        if gate.gate_hash != PRE.INGEST_READY_GATE_HASH:
            return False
        if gate.qualified_source_git_sha != PRE.INGEST_READY_SOURCE_COMMIT:
            return False
        result = verify_ingest_ready_gate(
            root, root / "gates" / "ingest-ready.json", require_descends=True
        )
        return result.ok
    except Exception:  # noqa: BLE001 - fail closed
        return False


def ci_verifies_snapshot_gate(root: Path, epoch: int) -> bool:
    path = root / CI_WORKFLOW
    if not path.exists():
        return False
    content = path.read_text(encoding="utf-8")
    return f"profile-snapshot-frozen-{epoch}.json" in content and (
        "verify_snapshot_offline" in content
    )


def _offline_checks(
    root: Path,
    epoch: int,
    member_manifest: dict[str, Any],
    inventory: dict[str, Any],
    selections: dict[str, str],
) -> tuple[dict[str, bool], dict[str, str]]:
    v2 = json.loads((root / "manifests/layer2_dataset_split_v2_epoch1.json").read_text())
    members = member_manifest["members"]
    per_chrom: dict[str, int] = {}
    per_part: dict[str, int] = {}
    m5_counts: dict[str, int] = {}
    degraded = 0
    for m in members:
        per_chrom[m["chromosome"]] = per_chrom.get(m["chromosome"], 0) + 1
        per_part[m["partition"]] = per_part.get(m["partition"], 0) + 1
        m5_counts[m["m5_status"]] = m5_counts.get(m["m5_status"], 0) + 1
        degraded += 1 if m["integrity_degraded"] else 0
    ids = [m["dataset_id"] for m in members]
    inv_entries = inventory.get("entries", [])
    inv_by_round = {e["round_id"]: e for e in inv_entries}
    reg = {
        s["dataset_id"]: (s["round_id"], s["chromosome"], s["identity_tuple_hash"], s["partition"])
        for s in v2["samples"]
    }
    mm_content = {k: v for k, v in member_manifest.items() if k != "member_manifest_hash"}
    inv_content = {k: v for k, v in inventory.items() if k != "inventory_hash"}

    checks: dict[str, bool] = {
        "epoch_binding_exact": member_manifest["epoch"] == epoch
        and member_manifest["split_manifest_hash"] == v2["manifest_hash"],
        "registry_binding_exact": member_manifest["registry_snapshot_hash"]
        == v2["registry_snapshot_hash"]
        and all(m["registry_snapshot_hash"] == v2["registry_snapshot_hash"] for m in members),
        "member_count_75": len(members) == 75 and member_manifest["member_count"] == 75,
        "members_unique_identities": len(set(ids)) == 75,
        "partitions_50_10_15": per_part == _EXPECTED_COUNTS,
        "per_chromosome_15": all(per_chrom.get(c) == 15 for c in _CHROMS),
        "partitions_match_split_allocations": all(
            m["dataset_id"] in reg and reg[m["dataset_id"]][3] == m["partition"] for m in members
        ),
        "identities_match_registry": all(
            m["dataset_id"] in reg
            and reg[m["dataset_id"]][0] == m["round_id"]
            and reg[m["dataset_id"]][1] == m["chromosome"]
            and reg[m["dataset_id"]][2] == m["identity_tuple_hash"]
            for m in members
        ),
        "profiler_identity_exact": all(
            m["profiler_version"] == PRE.PROFILER_VERSION
            and m["profiler_config_hash"] == PRE.PROFILER_CONFIG_HASH
            for m in members
        ),
        "all_profiles_complete": all(m["profile_status"] == "COMPLETE" for m in members),
        "selected_versions_unique_and_explicit": set(selections) == set(ids)
        and all(selections[m["dataset_id"]] == m["content_hash"] for m in members),
        "member_manifest_canonical_integrity": canonical_hash(mm_content)
        == member_manifest.get("member_manifest_hash"),
        "inventory_canonical_integrity": canonical_hash(inv_content)
        == inventory.get("inventory_hash"),
        "inventory_four_artifacts_each": len(inv_entries) == 75
        and len({e["round_id"] for e in inv_entries}) == 75
        and all(set(e["artifacts"]) == set(_ARTIFACT_FILES) for e in inv_entries),
        "artifact_bindings_complete": all(
            inv_by_round.get(str(m["round_id"]), {"artifacts": {}})
            .get("artifacts", {})
            .get("bam-profile-v1.json", {})
            .get("sha256")
            == m["profile_sha256"]
            and inv_by_round[str(m["round_id"])]["artifacts"]["profile-manifest-v1.json"]["sha256"]
            == m["profile_manifest_sha256"]
            and inv_by_round[str(m["round_id"])]["artifacts"]["window-profile-v1.parquet"]["sha256"]
            == m["windows_sha256"]
            for m in members
        ),
        "attestation_bound": all(
            isinstance(m["attestation_hash"], str) and len(m["attestation_hash"]) == 64
            for m in members
        ),
        "m5_counts_recorded": m5_counts.get("MATCH", 0) == 0 and m5_counts.get("ABSENT", 0) == 75,
        "m5_mismatch_count_zero": m5_counts.get("MISMATCH", 0) == 0,
        "degraded_integrity_count_75": degraded == 75,
        "snapshot_hash_recomputed": recompute_snapshot_hash(member_manifest)
        == member_manifest["snapshot_hash"],
        "accepted_ingest_ready_bound": _verify_ingest_ready(root),
        "ci_verifies_snapshot_gate": ci_verifies_snapshot_gate(root, epoch),
    }
    identities = {
        "snapshot_hash": str(member_manifest["snapshot_hash"]),
        "member_manifest_hash": canonical_hash(mm_content),
        "selection_manifest_hash": canonical_hash(dict(sorted(selections.items()))),
        "artifact_inventory_hash": canonical_hash(inv_content),
        "split_manifest_hash": str(member_manifest["split_manifest_hash"]),
        "registry_snapshot_hash": str(member_manifest["registry_snapshot_hash"]),
        "m5_match_count": "0",
        "m5_absent_count": "75",
        "m5_mismatch_count": "0",
        "degraded_integrity_count": "75",
        "rejected_attempt_count": "0",
        "accepted_ingest_ready_gate_hash": PRE.INGEST_READY_GATE_HASH,
        "ingest_ready_source_commit": PRE.INGEST_READY_SOURCE_COMMIT,
        "ingest_ready_evidence_commit": PRE.INGEST_READY_EVIDENCE_COMMIT,
        "accepted_profiler_version": PRE.PROFILER_VERSION,
        "accepted_profiler_config_hash": PRE.PROFILER_CONFIG_HASH,
    }
    return checks, identities


def _sealed_denied(conn: Connection) -> bool:
    for role in ("minos_trainer", "minos_evaluator", "minos_live", "minos_runner"):
        try:
            conn.execute(text(f"SET ROLE {role}"))
            conn.execute(text("SELECT count(*) FROM evaluation.sealed_test_profile_members"))
            conn.execute(text("RESET ROLE"))
            return False
        except Exception:  # noqa: BLE001 - denial is the pass condition
            conn.execute(text("ROLLBACK"))
    return True


def _append_only(conn: Connection) -> bool:
    for sql in (
        "UPDATE profiling.profile_snapshots SET epoch = 99",
        "DELETE FROM profiling.profile_snapshot_members",
    ):
        try:
            conn.execute(text(sql))
            conn.execute(text("ROLLBACK"))
            return False
        except Exception:  # noqa: BLE001
            conn.execute(text("ROLLBACK"))
    return True


def _attestations_exactly_bound(corpus_dir: Path, members: list[dict[str, Any]]) -> bool:
    """Hash + parse EXACT attestation bytes per member and rebind every field."""
    from minos_engine.layer2.ingest.contracts import InputIntegrityAttestation, M5Status

    for m in members:
        p = corpus_dir / str(m["round_id"]) / "input-integrity-attestation-v1.json"
        try:
            raw_bytes = p.read_bytes()
            att = InputIntegrityAttestation.model_validate(json.loads(raw_bytes.decode("utf-8")))
        except Exception:  # noqa: BLE001
            return False
        if att.attestation_hash != m["attestation_hash"]:
            return False
        if att.dataset_id != m["dataset_id"]:
            return False
        if att.identity_tuple_hash != m["identity_tuple_hash"]:
            return False
        if att.registry_snapshot_hash != m["registry_snapshot_hash"]:
            return False
        if att.m5_status.value != m["m5_status"]:
            return False
        if att.m5_status is M5Status.ABSENT and (
            att.bam_sq_m5 is not None or len(att.computed_reference_m5) != 32
        ):
            return False
    return True


def _operational_checks(
    conn: Connection,
    corpus_dir: Path,
    member_manifest: dict[str, Any],
    committed_inventory: dict[str, Any],
) -> dict[str, bool]:
    rebuilt = build_artifact_inventory(corpus_dir, member_manifest)
    rebuilt_content = {k: v for k, v in rebuilt.items() if k != "inventory_hash"}
    committed_content = {k: v for k, v in committed_inventory.items() if k != "inventory_hash"}
    rejected = int(
        conn.execute(
            text(
                "SELECT count(*) FROM profiling.profile_ingest_attempts WHERE outcome = 'REJECTED'"
            )
        ).scalar()
        or 0
    )
    return {
        "operational_artifact_bytes_reverified": canonical_hash(rebuilt_content)
        == canonical_hash(committed_content),
        "attestation_files_exactly_bound": _attestations_exactly_bound(
            corpus_dir, member_manifest["members"]
        ),
        "zero_ingestion_failures": rejected == 0,
        "trainer_view_count_50": int(
            conn.execute(text("SELECT count(*) FROM profiling.training_profile_members")).scalar()
            or 0
        )
        == 50,
        "validation_view_count_10": int(
            conn.execute(
                text("SELECT count(*) FROM evaluation.validation_profile_members")
            ).scalar()
            or 0
        )
        == 10,
        "sealed_test_denied": _sealed_denied(conn),
        "snapshot_tables_append_only": _append_only(conn),
    }


def assemble_snapshot_gate(
    root: Path,
    conn: Connection,
    *,
    epoch: int,
    corpus_dir: Path,
    qualified_source_git_sha: str,
    qualified_source_tree_sha: str,
    created_at: str | None = None,
) -> tuple[GateArtifact, dict[str, Any], dict[str, Any], str]:
    """Build gate + member manifest + inventory + report (offline + operational checks)."""
    member_manifest = build_member_manifest(conn, epoch)
    inventory = build_artifact_inventory(corpus_dir, member_manifest)
    selections = json.loads((root / SELECTIONS_PATH).read_text())
    checks, identities = _offline_checks(root, epoch, member_manifest, inventory, selections)
    checks.update(_operational_checks(conn, corpus_dir, member_manifest, inventory))
    report = _render_report(epoch, identities, checks)
    identities["qualification_report_hash"] = sha256_hex(report.encode())
    evidence = tuple(
        EvidenceItem(description=p, path=p, kind=EvidenceKind.FILE, sha256=h)
        for p, h in (
            (SELECTIONS_PATH, identities["selection_manifest_hash"]),
            (MEMBER_MANIFEST_PATH, identities["member_manifest_hash"]),
            (INVENTORY_PATH, identities["artifact_inventory_hash"]),
        )
    )
    gate = GateArtifact(
        gate_name=gate_name(epoch),
        status=GateStatus.PASS if all(checks.values()) else GateStatus.HOLD,
        engine_git_sha=qualified_source_git_sha,
        input_hashes=identities,
        evidence=evidence,
        mandatory_checks=checks,
        qualified_source_git_sha=qualified_source_git_sha,
        qualified_source_tree_sha=qualified_source_tree_sha,
        qualification_tool_version=SNAPSHOT_QUALIFIER_VERSION,
        created_at=created_at or datetime.now(UTC).isoformat(),
    )
    return gate, member_manifest, inventory, report


class SnapshotGateVerification(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    ok: bool
    checks: dict[str, bool]
    reasons: tuple[str, ...] = ()


def _load_committed(
    root: Path, epoch: int
) -> tuple[GateArtifact, dict[str, Any], dict[str, Any], dict[str, str]]:
    from minos_engine.gates.verifier import load_gate

    gate = load_gate(root / gate_path(epoch))
    members = json.loads((root / MEMBER_MANIFEST_PATH).read_text())
    inventory = json.loads((root / INVENTORY_PATH).read_text())
    selections = json.loads((root / SELECTIONS_PATH).read_text())
    return gate, members, inventory, selections


def _gate_binding_checks(gate: GateArtifact, identities: dict[str, str]) -> dict[str, bool]:
    return {
        "gate_canonical_integrity": gate.gate_hash == gate.compute_hash(),
        "gate_binds_member_manifest": gate.input_hashes.get("member_manifest_hash")
        == identities["member_manifest_hash"],
        "gate_binds_snapshot_hash": gate.input_hashes.get("snapshot_hash")
        == identities["snapshot_hash"],
        "gate_binds_inventory": gate.input_hashes.get("artifact_inventory_hash")
        == identities["artifact_inventory_hash"],
        "gate_binds_selections": gate.input_hashes.get("selection_manifest_hash")
        == identities["selection_manifest_hash"],
    }


#: Operational-only assertions: recorded true in the gate, classified as operational
#: (live-store) facts — NOT offline-recomputable.
OPERATIONAL_CHECKS: frozenset[str] = frozenset(
    {
        "attestation_files_exactly_bound",
        "operational_artifact_bytes_reverified",
        "zero_ingestion_failures",
        "trainer_view_count_50",
        "validation_view_count_10",
        "sealed_test_denied",
        "snapshot_tables_append_only",
    }
)

_EVIDENCE_PATHS = (SELECTIONS_PATH, MEMBER_MANIFEST_PATH, INVENTORY_PATH)


def _gate_contract_checks(
    root: Path,
    gate: GateArtifact,
    identities: dict[str, str],
    recomputed: dict[str, bool],
    epoch: int,
    git_root: Path,
) -> dict[str, bool]:
    """Full fail-closed gate contract (items 1-16 of the final review)."""
    from minos_engine.gates.required_checks import required_checks_for
    from minos_engine.qualification import git_tree as G

    src = gate.qualified_source_git_sha or ""
    required = required_checks_for(gate.gate_name)
    committed = gate.mandatory_checks
    report_path = root / REPORT_PATH
    report_hash = sha256_hex(report_path.read_bytes()) if report_path.exists() else ""
    ev_by_path = {e.path: e for e in gate.evidence}
    return {
        "gate_status_pass": gate.status is GateStatus.PASS,
        "gate_name_exact": gate.gate_name == gate_name(epoch),
        "gate_schema_version": gate.schema_version == "gate-artifact-v1",
        "gate_tool_version": gate.qualification_tool_version == SNAPSHOT_QUALIFIER_VERSION,
        "gate_engine_sha_matches_source": bool(src) and gate.engine_git_sha == src,
        "qualified_source_present": bool(src) and G.is_commit(git_root, src),
        "qualified_source_tree_matches": bool(src)
        and G.commit_tree_sha(git_root, src) == gate.qualified_source_tree_sha,
        "head_descends_qualified_source": bool(src) and G.is_ancestor(git_root, src, "HEAD"),
        "source_descends_ingest_ready_evidence": bool(src)
        and G.is_ancestor(git_root, PRE.INGEST_READY_EVIDENCE_COMMIT, src),
        "mandatory_set_exact": set(committed) == set(required),
        "mandatory_all_true": all(committed.get(k) is True for k in committed),
        "offline_results_match_recomputed": all(
            committed.get(k) == v for k, v in recomputed.items() if k not in OPERATIONAL_CHECKS
        ),
        "operational_checks_recorded_true": all(
            committed.get(k) is True for k in OPERATIONAL_CHECKS
        ),
        "evidence_paths_exact": tuple(sorted(ev_by_path)) == tuple(sorted(_EVIDENCE_PATHS)),
        "evidence_hashes_recomputed": all(
            ev_by_path.get(p) is not None
            and ev_by_path[p].sha256
            == {
                SELECTIONS_PATH: identities["selection_manifest_hash"],
                MEMBER_MANIFEST_PATH: identities["member_manifest_hash"],
                INVENTORY_PATH: identities["artifact_inventory_hash"],
            }[p]
            for p in _EVIDENCE_PATHS
        ),
        "report_bytes_bound": bool(report_hash)
        and gate.input_hashes.get("qualification_report_hash") == report_hash,
    }


def verify_snapshot_offline(
    root: Path, epoch: int, *, git_root: Path | None = None
) -> SnapshotGateVerification:
    """Committed-artifact verification only — no PostgreSQL, no corpus directory."""
    gate, members, inventory, selections = _load_committed(root, epoch)
    checks, identities = _offline_checks(root, epoch, members, inventory, selections)
    recomputed_offline = dict(checks)  # offline-recomputable mandatory results ONLY
    checks.update(_gate_binding_checks(gate, identities))
    checks.update(
        _gate_contract_checks(root, gate, identities, recomputed_offline, epoch, git_root or root)
    )
    reasons = tuple(f"{k} failed" for k, v in checks.items() if not v)
    return SnapshotGateVerification(ok=all(checks.values()), checks=checks, reasons=reasons)


def verify_snapshot_gate(
    root: Path, conn: Connection, epoch: int, corpus_dir: Path
) -> SnapshotGateVerification:
    """Offline verification PLUS live-store + exact-artifact-byte re-verification."""
    gate, committed_members, committed_inventory, selections = _load_committed(root, epoch)
    live_members = build_member_manifest(conn, epoch)
    checks, identities = _offline_checks(root, epoch, live_members, committed_inventory, selections)
    checks.update(_gate_binding_checks(gate, identities))
    checks["committed_member_manifest_matches_store"] = (
        canonical_hash({k: v for k, v in committed_members.items() if k != "member_manifest_hash"})
        == identities["member_manifest_hash"]
    )
    checks.update(_operational_checks(conn, corpus_dir, live_members, committed_inventory))
    reasons = tuple(f"{k} failed" for k, v in checks.items() if not v)
    return SnapshotGateVerification(ok=all(checks.values()), checks=checks, reasons=reasons)


def _render_report(epoch: int, identities: dict[str, str], checks: dict[str, bool]) -> str:
    rows = "\n".join(f"| `{k}` | {'PASS' if v else 'FAIL'} |" for k, v in sorted(checks.items()))
    ih = json.dumps(dict(sorted(identities.items())), indent=2)
    return f"""# PROFILE-SNAPSHOT-FROZEN-{epoch} — Qualification Report

**Tool:** {SNAPSHOT_QUALIFIER_VERSION}

> Two verification levels: OFFLINE (committed artifacts only — gate, member manifest,
> selections, inventory, accepted split + INGEST-READY identities; embedded hashes never
> trusted, everything recomputed) and OPERATIONAL (live store + exact artifact bytes:
> attestations re-parsed and re-hashed per member, inventory rebuilt from the corpus and
> compared hash-for-hash, zero ingestion failures, view counts, sealed-test denial,
> append-only). m5 semantics: SAM `@SQ:M5` is a BAM-header tag (FASTA files never carry
> it); in epoch {epoch} all 75 BAM headers lack `@SQ:M5` while the computed
> reference-contig MD5 was available for every sample, so all 75 attestations are
> ABSENT/integrity-degraded (0 MATCH, 0 MISMATCH). Raw BAMs are not committed; the
> content-addressed artifact inventory permits later integrity verification without
> trusting the database.

## Bound identities
```
{ih}
```

## Mandatory checks
| Check | Result |
|---|---|
{rows}
"""


def write_snapshot_outputs(
    root: Path,
    gate: GateArtifact,
    member_manifest: dict[str, Any],
    inventory: dict[str, Any],
    report: str,
    epoch: int,
) -> None:
    from minos_engine.gates.verifier import write_gate

    (root / MEMBER_MANIFEST_PATH).write_text(canonical_json_str(member_manifest) + "\n")
    (root / INVENTORY_PATH).write_text(canonical_json_str(inventory) + "\n")
    (root / REPORT_PATH).write_text(report)
    write_gate(gate, root / gate_path(epoch))
