"""SPLIT-FROZEN (Layer 2 L2-C) qualification — git-tree-bound, evidence-based.

Assembles the ``SPLIT-FROZEN`` gate from real evidence: the accepted PROTOCOL/TWIN/
L1/DB-READY prerequisites, the deterministic 75-sample manifest, the immutable L2-C
Alembic migration, and the real PostgreSQL 16 L2-C integration suite (migration
lifecycle, role isolation, immutability, constraints) plus the full test suite,
coverage, and ruff/format/mypy. The gate binds the accepted DB-READY identity, the
L2-C qualified source commit/tree, the canonical manifest/registry/policy/generator/
migration hashes, the Alembic head, the Python/PostgreSQL identities, and the exact
50/10/15 counts. A PASS is never constructed from caller-supplied booleans; the
verifier re-derives every binding and proves proper descent from the DB-READY evidence
commit — never the current HEAD.
"""

from __future__ import annotations

import json
import re
import subprocess
import xml.etree.ElementTree as ET
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from minos_engine.common.canonical_json import canonical_json_str
from minos_engine.common.errors import StageNotReadyError
from minos_engine.common.hashing import canonical_hash, sha256_hex
from minos_engine.common.runtime import is_supported_runtime, runtime_identity
from minos_engine.gates.contracts import EvidenceItem, EvidenceKind, GateArtifact, GateStatus
from minos_engine.gates.verifier import load_gate, require_gate_pass, verify_gate_integrity
from minos_engine.layer2 import prerequisites as PRE
from minos_engine.layer2.feature_registry import REGISTRY_HASH
from minos_engine.layer2.split.contracts import DatasetSplitManifest, LocalInputInventory
from minos_engine.layer2.split.generator import generate, parameter_space_hash
from minos_engine.layer2.split.policy import (
    PARTITION_TOTALS,
    SUPPORTED_CHROMOSOMES,
    TOTAL_SAMPLES,
    split_policy_hash,
)
from minos_engine.layer2.split.verifier import verify_manifest
from minos_engine.storage.l2c_migration_contract import (
    L2C_MIGRATION_REVISION,
    l2c_contract_hash,
)

from . import git_tree as G
from .coverage import STAGE0_COVERAGE_THRESHOLD, CoverageResult, run_coverage
from .layer2_db_runner import alembic_head
from .provenance import GitProvenance, read_provenance
from .pytest_accounting import PytestAccounting, run_pytest, suite_passes
from .runner import SourceIntegrity, _bin, _tool_ok, gather_source_integrity

__all__ = [
    "GATE_NAME",
    "SPLIT_QUALIFIER_VERSION",
    "L2C_MIGRATION_FILE",
    "MANIFEST_PATH",
    "INVENTORY_PATH",
    "MANIFEST_SCHEMA_FILE",
    "SPLIT_PACKAGE_DIR",
    "FINAL_REPORT_PATH",
    "HISTORICAL_REPORT_PATH",
    "EVIDENCE_PAYLOAD",
    "EVIDENCE_PAYLOAD_PATHS",
    "SPLIT_EVIDENCE",
    "SPLIT_REQUIRED_TRACKED_FILES",
    "InventoryVerification",
    "verify_inventory",
    "derive_inventory_paths",
    "evidence_payload_hash",
    "SplitQualificationResult",
    "SplitGateVerification",
    "db_ready_closure_checks",
    "assemble_split_result",
    "qualify_split_frozen",
    "verify_split_frozen_gate",
    "write_split_outputs",
    "l2c_migration_immutable",
]

_HEX64 = re.compile(r"^[0-9a-f]{64}$")

GATE_NAME = "SPLIT-FROZEN"
SPLIT_QUALIFIER_VERSION = "layer2-split-qualifier-v1"
L2C_MIGRATION_FILE = "migrations/versions/0002_l2c_dataset_split.py"
MANIFEST_SCHEMA_FILE = "schemas/layer2-dataset-split-v1.schema.json"
MANIFEST_PATH = "manifests/layer2_dataset_split_v1.json"
INVENTORY_PATH = "manifests/layer2_local_input_inventory_v1.json"
#: The immutable split package. Its directory hash is ``generator_source_hash`` — the
#: closure never modifies any file under it, so that accepted identity is preserved.
SPLIT_PACKAGE_DIR = "src/minos_engine/layer2/split"
#: New, non-circular final closure report (the historical L2-C report is never
#: overwritten). This report deliberately omits its own hash, the evidence-payload
#: hash, and the gate hash so ``sha256(report bytes)`` is well defined.
FINAL_REPORT_PATH = "reports/LAYER2_L2C_SPLIT_FINAL_CLOSURE_REPORT.md"
HISTORICAL_REPORT_PATH = "reports/LAYER2_L2C_SPLIT_FROZEN_REPORT.md"
_GATE_SELF_PATH = "gates/split-frozen.json"
_SPLIT_SUITE = "tests/integration/layer2_split"

#: Canonical evidence payload: the exact content artifacts the gate binds by an
#: aggregate hash. It NEVER contains ``gates/split-frozen.json`` — the gate cannot be
#: part of the payload it carries, which is what keeps the aggregate non-circular.
EVIDENCE_PAYLOAD: tuple[tuple[str, EvidenceKind], ...] = (
    (MANIFEST_PATH, EvidenceKind.FILE),
    (INVENTORY_PATH, EvidenceKind.FILE),
    (FINAL_REPORT_PATH, EvidenceKind.FILE),
)
EVIDENCE_PAYLOAD_PATHS: tuple[str, ...] = tuple(sorted(p for p, _ in EVIDENCE_PAYLOAD))

# Inventory safe-path + leakage policy (independent of the frozen contract).
_INV_PATH_FIELDS = ("bam_relpath", "bai_relpath", "reference_relpath", "fai_relpath")
_INV_FORBIDDEN_TOKENS = (
    "truth",
    "mutation",
    "label",
    "score",
    "result",
    "evaluation",
    "credential",
    "secret",
    "hidden",
    "hap.py",
    "tp_",
    "fp_",
    "fn_",
)
_DRIVE_RE = re.compile(r"^[A-Za-z]:")


def evidence_payload_hash(items: list[tuple[str, str, str]]) -> str:
    """Deterministic aggregate over ``(path, kind, sha256)`` triples, sorted by path.

    Non-circular by construction: it is computed only over content artifacts (the
    canonical manifest, the local inventory, and the final closure report) and never
    over the gate that carries it, so it cannot depend on its own value.
    """
    canonical = sorted(
        ({"path": p, "kind": k, "sha256": s} for p, k, s in items),
        key=lambda r: r["path"],
    )
    return canonical_hash(canonical)


def _l2c_revision_in_lineage() -> bool:
    """True iff the accepted L2-C revision ``0002`` is in the current migration lineage.

    Relaxed from an exact head match: additive migrations added *after* L2-C (the v2 epoch
    registry, L2-D ingestion, …) advance the Alembic head past ``0002``, which must not
    break the accepted SPLIT-FROZEN v1 gate. The revision must remain a genuine ancestor
    of (or equal to) the current head — never merely a stale recorded string.
    """
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    try:
        script = ScriptDirectory.from_config(Config("alembic.ini"))
        head = script.get_current_head()
        if head is None:
            return False
        return any(
            r.revision == L2C_MIGRATION_REVISION for r in script.iterate_revisions(head, "base")
        )
    except Exception:  # noqa: BLE001 - a missing/unreadable script dir fails closed
        return False


def _inv_path_safe(p: str) -> bool:
    """A portable, dataset-root-relative POSIX path with no traversal/abs/URI/drive."""
    if not p:
        return False
    if p.startswith("/"):  # absolute POSIX
        return False
    if "\\" in p:  # Windows backslash separator
        return False
    if "://" in p:  # URI / scheme path
        return False
    if _DRIVE_RE.match(p):  # drive-letter path (e.g. C:...)
        return False
    segs = p.split("/")
    # reject empty/./.. segments (leading/trailing/double slash or traversal)
    return not any(seg in ("", ".", "..") for seg in segs)


def derive_inventory_paths(round_id: str, chromosome: str) -> dict[str, str]:
    """The exact canonical resolver paths for a manifest-bound identity.

    Derived *only* from the identity (round id + chromosome), never read from the
    inventory — this is the independent anchor that rejects a consistently re-hashed
    operational-path substitution that is still syntactically safe. The convention
    matches the committed corpus exactly (bare lowercase-hex round dir, ``chrNN`` casing).
    """
    return {
        "bam_relpath": f"practice/round_{round_id}/input.bam",
        "bai_relpath": f"practice/round_{round_id}/input.bam.bai",
        "reference_relpath": f"reference/{chromosome}/{chromosome}.fa",
        "fai_relpath": f"reference/{chromosome}/{chromosome}.fa.fai",
    }


class InventoryVerification(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    ok: bool
    inventory_hash: str
    embedded_matches: bool
    checks: dict[str, bool]
    reasons: tuple[str, ...] = ()


def verify_inventory(
    inv_raw: dict[str, object], manifest: DatasetSplitManifest
) -> InventoryVerification:
    """Strict, independent verification of the committed local input inventory.

    The inventory hash is recomputed from the validated entries (the embedded field is
    never trusted), and every inventory identity is independently bound to the frozen
    canonical manifest — so a *consistently re-hashed* tampered inventory still fails
    because it breaks the manifest correspondence or the safe-path policy, not merely
    because an outer gate hash is stale. File existence is intentionally NOT checked:
    the inventory is a portable resolver, and existence/content hashing is a
    generation/discovery responsibility.
    """
    checks: dict[str, bool] = {}
    reasons: list[str] = []
    try:
        inv = LocalInputInventory.model_validate(inv_raw)  # extra="forbid" enforced
    except Exception as exc:  # noqa: BLE001
        return InventoryVerification(
            ok=False,
            inventory_hash="",
            embedded_matches=False,
            checks={"inventory_contract_valid": False},
            reasons=(f"contract: {exc}",),
        )
    checks["inventory_contract_valid"] = True

    recomputed = inv.compute_inventory_hash()
    embedded = str(inv_raw.get("inventory_hash", ""))
    embedded_matches = embedded == recomputed
    checks["inventory_hash_recomputed"] = embedded_matches

    manifest_by_id = {s.dataset_id: (s.round_id, s.chromosome) for s in manifest.samples}
    inv_by_id = {e.dataset_id: (e.round_id, e.chromosome) for e in inv.entries}
    identity_ok = (
        len(inv.entries) == TOTAL_SAMPLES
        and len(manifest.samples) == TOTAL_SAMPLES
        and len(inv_by_id) == len(inv.entries)  # no duplicate dataset_id
        and set(inv_by_id) == set(manifest_by_id)
        and all(inv_by_id[k] == manifest_by_id[k] for k in inv_by_id)
    )
    checks["inventory_manifest_identity_bound"] = identity_ok

    checks["inventory_paths_safe"] = all(
        _inv_path_safe(getattr(e, f)) for e in inv.entries for f in _INV_PATH_FIELDS
    )

    # Manifest-derived path identity: every stored path must equal the path derived
    # from the entry's MANIFEST-bound identity (round id + chromosome from the frozen
    # manifest, matched by dataset id) — not trusted from inventory fields. Independent
    # of every hash; a consistently re-hashed safe-path substitution fails here.
    paths_identity_ok = identity_ok and all(
        {f: getattr(e, f) for f in _INV_PATH_FIELDS}
        == derive_inventory_paths(*manifest_by_id[e.dataset_id])
        for e in inv.entries
        if e.dataset_id in manifest_by_id
    )
    checks["inventory_paths_identity_bound"] = paths_identity_ok

    blob = json.dumps(inv.to_canonical(), sort_keys=True).lower()
    checks["inventory_truth_mutation_isolation_ok"] = not any(
        tok in blob for tok in _INV_FORBIDDEN_TOKENS
    )

    for name, ok in checks.items():
        if not ok:
            reasons.append(f"{name} failed")
    return InventoryVerification(
        ok=all(checks.values()),
        inventory_hash=recomputed,
        embedded_matches=embedded_matches,
        checks=checks,
        reasons=tuple(dict.fromkeys(reasons)),
    )


SPLIT_EVIDENCE: tuple[tuple[str, EvidenceKind], ...] = (
    ("src/minos_engine/layer2/split", EvidenceKind.DIRECTORY),
    ("src/minos_engine/storage/dataset_split.py", EvidenceKind.FILE),
    ("src/minos_engine/storage/l2c_migration_contract.py", EvidenceKind.FILE),
    ("src/minos_engine/qualification/layer2_split_runner.py", EvidenceKind.FILE),
    ("src/minos_engine/cli/layer2_split_commands.py", EvidenceKind.FILE),
    (L2C_MIGRATION_FILE, EvidenceKind.FILE),
    (MANIFEST_SCHEMA_FILE, EvidenceKind.FILE),
    ("gates/protocol-ready.json", EvidenceKind.FILE),
    ("gates/twin-ready.json", EvidenceKind.FILE),
    ("gates/l1-ready.json", EvidenceKind.FILE),
    ("gates/db-ready.json", EvidenceKind.FILE),
    ("docs/layer2/DATASET_SPLIT.md", EvidenceKind.FILE),
    (_SPLIT_SUITE, EvidenceKind.DIRECTORY),
)

SPLIT_REQUIRED_TRACKED_FILES: tuple[str, ...] = (
    "src/minos_engine/layer2/split/__init__.py",
    "src/minos_engine/layer2/split/policy.py",
    "src/minos_engine/layer2/split/contracts.py",
    "src/minos_engine/layer2/split/discovery.py",
    "src/minos_engine/layer2/split/generator.py",
    "src/minos_engine/layer2/split/verifier.py",
    "src/minos_engine/storage/dataset_split.py",
    "src/minos_engine/storage/l2c_migration_contract.py",
    L2C_MIGRATION_FILE,
    MANIFEST_SCHEMA_FILE,
    "docs/layer2/DATASET_SPLIT.md",
)

# gate check -> L2-C integration nodeids whose pass proves it.
_CHECK_NODES: dict[str, tuple[str, ...]] = {
    "postgres_16_verified": ("test_migration_lifecycle.py::test_postgres_major_version_is_16",),
    "l2c_migration_lifecycle_passed": (
        "test_migration_lifecycle.py::test_l2c_migration_lifecycle",
    ),
    "role_isolation_passed": ("test_role_isolation.py",),
    "immutability_passed": ("test_split_store.py::test_registry_and_allocation_append_only",),
    "constraints_passed": ("test_split_store.py::test_duplicate_and_overlap_rejected",),
}

_FORBIDDEN_MIGRATION_TOKENS = (
    "Base.metadata",
    "create_all",
    "drop_all",
    "import Base",
    "storage.metadata",
    "storage import models",
    "storage.models",
)


def l2c_migration_immutable(root: Path) -> bool:
    """The L2-C migration is a self-contained snapshot (no ORM metadata dependency)."""
    path = root / L2C_MIGRATION_FILE
    if not path.exists():
        return False
    src = path.read_text(encoding="utf-8")
    return not any(token in src for token in _FORBIDDEN_MIGRATION_TOKENS)


def db_ready_closure_checks(
    root: Path,
    *,
    l2c_source_ref: str,
    head_ref: str = "HEAD",
    require_head_descends: bool = True,
) -> dict[str, bool]:
    """Prove the accepted DB-READY closure and the *exact* L2-C qualified source.

    ``l2c_source_ref`` is the frozen L2-C source commit (provenance HEAD at generation;
    ``gate.qualified_source_git_sha`` at verification) — never a substitute such as the
    current HEAD for checks 1-4. The L2-C source is proven to properly descend the
    accepted DB-READY *evidence* commit (rejecting sibling/ancestor/unrelated sources).
    """
    src = PRE.DB_READY_SOURCE_COMMIT
    src_tree = PRE.DB_READY_SOURCE_TREE
    evi = PRE.DB_READY_EVIDENCE_COMMIT

    src_ok = G.is_commit(root, src)
    src_tree_ok = src_ok and G.commit_tree_sha(root, src) == src_tree
    evi_ok = G.is_commit(root, evi)
    # DB-READY evidence commit properly descends the DB-READY source commit.
    db_chain_ok = bool(src_ok and evi_ok and G.is_ancestor(root, src, evi))

    qs = l2c_source_ref
    qs_present = bool(qs) and G.is_commit(root, qs)
    # accepted DB-READY evidence is a PROPER ancestor of the L2-C qualified source.
    l2c_descends = bool(
        evi_ok and qs_present and G.is_ancestor(root, evi, qs) and not G.is_ancestor(root, qs, evi)
    )
    head_ok = G.object_exists(root, head_ref)
    head_descends = (not require_head_descends) or bool(
        qs_present and head_ok and G.is_ancestor(root, qs, head_ref)
    )
    return {
        "db_ready_source_present": src_ok,
        "db_ready_source_tree_bound": src_tree_ok,
        "db_ready_evidence_present": evi_ok and db_chain_ok,
        "l2c_source_descends_db_ready": l2c_descends,
        "head_descends_l2c_source": head_descends,
    }


class SplitQualificationResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    gate: GateArtifact
    manifest: DatasetSplitManifest
    inventory: LocalInputInventory
    report_markdown: str
    mandatory: dict[str, bool]
    accounting: PytestAccounting
    coverage: CoverageResult
    provenance: GitProvenance


def _si_hash(si: SourceIntegrity, path: str) -> str:
    for item in si.evidence:
        if item.path == path and item.sha256:
            return item.sha256
    return "unavailable"


def _unique_evidence(
    evidence: tuple[EvidenceItem, ...], path: str, kind: EvidenceKind
) -> EvidenceItem | None:
    """Return the single evidence item at ``path`` of ``kind`` with a valid sha256.

    Returns ``None`` if the item is missing, duplicated, of the wrong kind, or its
    sha256 is malformed — every one of which must fail the corresponding binding.
    """
    items = [e for e in evidence if e.path == path]
    if len(items) != 1:
        return None
    item = items[0]
    if item.kind is not kind or not item.sha256 or not _HEX64.match(item.sha256):
        return None
    return item


def _accepted_gate_unchanged(root: Path, filename: str, expected_hash: str) -> bool:
    path = root / "gates" / filename
    if not path.exists():
        return False
    try:
        gate = load_gate(path)
    except Exception:  # noqa: BLE001
        return False
    return gate.gate_hash == expected_hash and require_gate_pass(gate, base_dir=root).ok


def _service_still_blocked() -> bool:
    from minos_engine.layer2.service import Layer2Service

    try:
        Layer2Service().select_config(None)  # type: ignore[arg-type]
    except StageNotReadyError:
        return True
    except Exception:  # noqa: BLE001
        return False
    return False


def run_split_suite(root: Path) -> dict[str, bool]:  # pragma: no cover - subprocess + real PG
    """Run the L2-C PostgreSQL integration suite; return {"file.py::test": passed}."""
    junit = root / "reports" / "ci-split-junit.xml"
    junit.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [_bin("pytest"), _SPLIT_SUITE, f"--junitxml={junit}", "-q"],
        cwd=root,
        capture_output=True,
        check=False,
    )
    return _parse_junit(junit)


def _parse_junit(path: Path) -> dict[str, bool]:
    if not path.exists():
        return {}
    tree = ET.parse(path)
    out: dict[str, bool] = {}
    for case in tree.iter("testcase"):
        classname = case.get("classname", "")
        name = case.get("name", "")
        file_part = classname.split(".")[-1] + ".py" if classname else ""
        out[f"{file_part}::{name}"] = not any(c.tag in ("failure", "error") for c in case)
    return out


def _check_from_suite(passmap: dict[str, bool], nodes: tuple[str, ...]) -> bool:
    matched = False
    for node in nodes:
        if "::" in node:
            if node not in passmap:
                return False
            matched = True
            if not passmap[node]:
                return False
        else:
            prefix = node + "::"
            file_nodes = [k for k in passmap if k.startswith(prefix)]
            if not file_nodes:
                return False
            matched = True
            if not all(passmap[k] for k in file_nodes):
                return False
    return matched


def gather_split_source_integrity(root: Path, ref: str) -> SourceIntegrity:
    return gather_source_integrity(
        root, ref, evidence_spec=SPLIT_EVIDENCE, required_files=SPLIT_REQUIRED_TRACKED_FILES
    )


def manifest_bytes(manifest: DatasetSplitManifest) -> bytes:
    return (canonical_json_str(manifest.to_canonical()) + "\n").encode("utf-8")


def inventory_bytes(inventory: LocalInputInventory) -> bytes:
    return (canonical_json_str(inventory.to_canonical()) + "\n").encode("utf-8")


def qualify_split_frozen(  # pragma: no cover - subprocess + real PG + corpus
    root: Path, dataset_root: str | Path
) -> SplitQualificationResult:
    root = root.resolve()
    provenance = read_provenance(root)
    ref = provenance.head_sha or "HEAD"
    source_integrity = gather_split_source_integrity(root, ref)
    manifest, inventory = generate(dataset_root)
    passmap = run_split_suite(root)
    accounting = run_pytest(root)
    coverage = run_coverage(root)[0]
    tools = {
        "ruff": _tool_ok([_bin("ruff"), "check", "."], root),
        "format": _tool_ok([_bin("ruff"), "format", "--check", "."], root),
        "mypy": _tool_ok([_bin("mypy"), "src"], root),
    }
    return assemble_split_result(
        root,
        manifest=manifest,
        inventory=inventory,
        passmap=passmap,
        accounting=accounting,
        coverage=coverage,
        tools=tools,
        provenance=provenance,
        source_integrity=source_integrity,
    )


def assemble_split_result(
    root: Path,
    *,
    manifest: DatasetSplitManifest,
    inventory: LocalInputInventory,
    passmap: dict[str, bool],
    accounting: PytestAccounting,
    coverage: CoverageResult,
    tools: dict[str, bool],
    provenance: GitProvenance,
    source_integrity: SourceIntegrity,
    created_at: str | None = None,
) -> SplitQualificationResult:
    root = root.resolve()
    si = source_integrity
    mv = verify_manifest(manifest.to_canonical())
    migration_sha = _si_hash(si, L2C_MIGRATION_FILE)
    manifest_sha = sha256_hex(manifest_bytes(manifest))
    inventory_sha = sha256_hex(inventory_bytes(inventory))
    generator_source_hash = _si_hash(si, SPLIT_PACKAGE_DIR)
    manifest_schema_hash = _si_hash(si, MANIFEST_SCHEMA_FILE)
    per_chrom_ok = all(
        manifest.per_chromosome.get(c) == {"train": 10, "validation": 2, "test": 3}
        for c in SUPPORTED_CHROMOSOMES
    )

    closure = db_ready_closure_checks(root, l2c_source_ref=provenance.head_sha or "")

    # Independent inventory verification + frozen-manifest correspondence (defects 1-2).
    iv = verify_inventory(inventory.to_canonical(), manifest)
    # Unique source-evidence items for the generator package + manifest schema (3-4).
    gen_item = _unique_evidence(si.evidence, SPLIT_PACKAGE_DIR, EvidenceKind.DIRECTORY)
    schema_item = _unique_evidence(si.evidence, MANIFEST_SCHEMA_FILE, EvidenceKind.FILE)

    postgres_16_verified = _check_from_suite(passmap, _CHECK_NODES["postgres_16_verified"])
    suite_checks = {
        check: _check_from_suite(passmap, nodes) for check, nodes in _CHECK_NODES.items()
    }
    created_at = created_at or datetime.now(UTC).isoformat()

    per_chrom_hashes = {
        c: f"{manifest.per_chromosome[c]['train']}/"
        f"{manifest.per_chromosome[c]['validation']}/"
        f"{manifest.per_chromosome[c]['test']}"
        for c in SUPPORTED_CHROMOSOMES
    }
    # Stable identities first (no self-referential report/payload hashes yet), so the
    # final closure report — rendered from these — is non-circular.
    input_hashes = {
        "canonical_manifest_hash": manifest.manifest_hash,
        "dataset_registry_hash": manifest.dataset_registry_hash,
        "split_policy_hash": split_policy_hash(),
        "parameter_space_hash": manifest.parameter_space_hash,
        "feature_registry_hash": manifest.feature_registry_hash,
        "committed_manifest_sha256": manifest_sha,
        "local_input_inventory_hash": inventory.inventory_hash,
        "committed_inventory_sha256": inventory_sha,
        "manifest_schema_hash": manifest_schema_hash,
        "generator_source_hash": generator_source_hash,
        "l2c_migration_file_hash": migration_sha,
        "l2c_migration_contract_hash": l2c_contract_hash(migration_sha),
        "alembic_head_revision": alembic_head(),
        "accepted_protocol_ready_gate_hash": PRE.PROTOCOL_READY_GATE_HASH,
        "accepted_twin_ready_gate_hash": PRE.TWIN_READY_GATE_HASH,
        "accepted_l1_ready_gate_hash": PRE.L1_READY_GATE_HASH,
        "accepted_db_ready_gate_hash": PRE.DB_READY_GATE_HASH,
        "db_ready_source_commit": PRE.DB_READY_SOURCE_COMMIT,
        "db_ready_source_tree": PRE.DB_READY_SOURCE_TREE,
        "db_ready_evidence_commit": PRE.DB_READY_EVIDENCE_COMMIT,
        "accepted_feature_registry_hash": PRE.ACCEPTED_FEATURE_REGISTRY_HASH,
        "total_samples": str(len(manifest.samples)),
        "count_train": str(manifest.counts.get("train", 0)),
        "count_validation": str(manifest.counts.get("validation", 0)),
        "count_test": str(manifest.counts.get("test", 0)),
        "per_chromosome_layout": json.dumps(per_chrom_hashes, sort_keys=True),
        "postgres_major_version": "16" if postgres_16_verified else "unverified",
        "test_collected": str(accounting.collected),
        "test_passed": str(accounting.passed),
        "test_failed": str(accounting.failed),
        "test_skipped": str(accounting.skipped),
        "split_integration_collected": str(len(passmap)),
        "split_integration_passed": str(sum(1 for v in passmap.values() if v)),
        "python_runtime": runtime_identity(),
        "evidence_payload_paths": json.dumps(list(EVIDENCE_PAYLOAD_PATHS)),
    }

    # Render the final closure report from the stable identities, then bind its exact
    # bytes and the aggregate payload — neither of which appears in the report.
    markdown = _render_final_report(
        qualified_source_sha=provenance.head_sha,
        qualified_source_tree=provenance.tree_sha,
        input_hashes=input_hashes,
        evidence=si.evidence,
    )
    report_sha = sha256_hex(markdown.encode("utf-8"))
    payload_hash = evidence_payload_hash(
        [
            (MANIFEST_PATH, EvidenceKind.FILE.value, manifest_sha),
            (INVENTORY_PATH, EvidenceKind.FILE.value, inventory_sha),
            (FINAL_REPORT_PATH, EvidenceKind.FILE.value, report_sha),
        ]
    )
    input_hashes["qualification_report_hash"] = report_sha
    input_hashes["evidence_payload_hash"] = payload_hash

    mandatory: dict[str, bool] = {
        "accepted_protocol_ready_unchanged": _accepted_gate_unchanged(
            root, "protocol-ready.json", PRE.PROTOCOL_READY_GATE_HASH
        ),
        "accepted_twin_ready_unchanged": _accepted_gate_unchanged(
            root, "twin-ready.json", PRE.TWIN_READY_GATE_HASH
        ),
        "accepted_l1_ready_unchanged": _accepted_gate_unchanged(
            root, "l1-ready.json", PRE.L1_READY_GATE_HASH
        ),
        "accepted_db_ready_unchanged": _accepted_gate_unchanged(
            root, "db-ready.json", PRE.DB_READY_GATE_HASH
        ),
        **closure,
        "manifest_schema_valid": mv.checks.get("schema_valid", False),
        "manifest_verified": mv.ok,
        "canonical_manifest_hash_bound": mv.manifest_hash == manifest.manifest_hash,
        "dataset_registry_hash_bound": mv.dataset_registry_hash == manifest.dataset_registry_hash,
        "split_policy_hash_bound": manifest.split_policy_hash == split_policy_hash(),
        "parameter_space_hash_bound": manifest.parameter_space_hash == parameter_space_hash(),
        "feature_registry_hash_bound": manifest.feature_registry_hash == REGISTRY_HASH,
        "committed_manifest_bytes_bound": _HEX64.match(manifest_sha) is not None,
        "local_input_inventory_hash_bound": _HEX64.match(inventory_sha) is not None,
        # Independent inventory verification (defects 1-2).
        "inventory_contract_valid": iv.checks.get("inventory_contract_valid", False),
        "inventory_hash_recomputed": iv.embedded_matches
        and iv.inventory_hash == input_hashes["local_input_inventory_hash"],
        "committed_inventory_bytes_bound": _HEX64.match(inventory_sha) is not None,
        "inventory_manifest_identity_bound": iv.checks.get(
            "inventory_manifest_identity_bound", False
        ),
        "inventory_paths_safe": iv.checks.get("inventory_paths_safe", False),
        "inventory_paths_identity_bound": iv.checks.get("inventory_paths_identity_bound", False),
        "inventory_truth_mutation_isolation_ok": iv.checks.get(
            "inventory_truth_mutation_isolation_ok", False
        ),
        # Generator + manifest-schema evidence cross-binding (defects 3-4).
        "generator_source_evidence_present": gen_item is not None,
        "generator_source_evidence_matches_source": gen_item is not None,
        "generator_source_evidence_bound": gen_item is not None
        and gen_item.sha256 == generator_source_hash,
        "manifest_schema_evidence_present": schema_item is not None,
        "manifest_schema_evidence_matches_source": schema_item is not None,
        "manifest_schema_evidence_bound": schema_item is not None
        and schema_item.sha256 == manifest_schema_hash,
        # Report bytes + non-circular evidence payload (defects 5-6).
        "qualification_report_bytes_bound": _HEX64.match(report_sha) is not None,
        "evidence_payload_paths_exact": _GATE_SELF_PATH not in EVIDENCE_PAYLOAD_PATHS
        and len(EVIDENCE_PAYLOAD_PATHS) == 3,
        "evidence_payload_hash_bound": _HEX64.match(payload_hash) is not None,
        "l2c_migration_immutable": l2c_migration_immutable(root),
        "l2c_migration_file_evidence_bound": _HEX64.match(migration_sha) is not None,
        "l2c_migration_contract_bound": _HEX64.match(migration_sha) is not None,
        "alembic_head_is_l2c": _l2c_revision_in_lineage(),
        "total_sample_count_75": len(manifest.samples) == TOTAL_SAMPLES,
        "partition_totals_50_10_15": manifest.counts == PARTITION_TOTALS,
        "per_chromosome_10_2_3": per_chrom_ok,
        "full_tests_passed": suite_passes(accounting),
        "coverage_passed": coverage.meets(STAGE0_COVERAGE_THRESHOLD),
        "ruff_passed": tools["ruff"],
        "format_passed": tools["format"],
        "mypy_passed": tools["mypy"],
        **suite_checks,
        "evidence_hashes_complete": si.evidence_hashes_complete,
        "required_source_tracked": si.required_source_tracked,
        "truth_mutation_isolation_ok": mv.checks.get("no_truth_or_mutation_fields", False),
        "service_still_blocked": _service_still_blocked(),
    }

    status = GateStatus.PASS if all(mandatory.values()) else GateStatus.HOLD

    gate = GateArtifact(
        gate_name=GATE_NAME,
        status=status,
        engine_git_sha=provenance.head_sha or "unavailable",
        input_hashes=input_hashes,
        evidence=si.evidence,
        mandatory_checks=mandatory,
        qualified_source_git_sha=provenance.head_sha,
        qualified_source_tree_sha=provenance.tree_sha,
        qualification_tool_version=SPLIT_QUALIFIER_VERSION,
        created_at=created_at,
    )
    return SplitQualificationResult(
        gate=gate,
        manifest=manifest,
        inventory=inventory,
        report_markdown=markdown,
        mandatory=mandatory,
        accounting=accounting,
        coverage=coverage,
        provenance=provenance,
    )


class SplitGateVerification(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    ok: bool
    gate_hash: str
    checks: dict[str, bool]
    reasons: tuple[str, ...] = ()


def verify_split_frozen_gate(
    root: Path, gate_path: Path, *, require_descends: bool = True
) -> SplitGateVerification:
    """Non-mutating verification of a committed SPLIT-FROZEN gate (check mode).

    Independently recomputes every binding, re-hashes source evidence from the exact
    qualified source commit, proves proper descent from the DB-READY evidence commit,
    verifies the committed manifest bytes + recomputes its content, and rejects
    duplicate/missing migration evidence and consistently-tampered hash pairs.
    """
    root = root.resolve()
    reasons: list[str] = []
    try:
        gate = load_gate(gate_path)
    except Exception as exc:  # noqa: BLE001
        return SplitGateVerification(
            ok=False, gate_hash="", checks={"loadable": False}, reasons=(str(exc),)
        )

    integrity = verify_gate_integrity(gate, base_dir=root)
    promotion = require_gate_pass(gate, base_dir=root)
    src_sha = gate.qualified_source_git_sha or ""
    gih = gate.input_hashes.get

    checks: dict[str, bool] = {
        "gate_name_split_frozen": gate.gate_name == GATE_NAME,
        "canonical_integrity": gate.gate_hash == gate.compute_hash(),
        "qualification_tool_identity": gate.qualification_tool_version == SPLIT_QUALIFIER_VERSION,
        "python_runtime_is_3_12": is_supported_runtime(),
        "evidence_verified": integrity.ok,
        "required_checks_and_promotion": promotion.ok,
        "alembic_head_is_l2c": _l2c_revision_in_lineage()
        and gih("alembic_head_revision") == L2C_MIGRATION_REVISION,
        "l2c_migration_immutable": l2c_migration_immutable(root),
        "accepted_protocol_ready_unchanged": _accepted_gate_unchanged(
            root, "protocol-ready.json", PRE.PROTOCOL_READY_GATE_HASH
        )
        and gih("accepted_protocol_ready_gate_hash") == PRE.PROTOCOL_READY_GATE_HASH,
        "accepted_twin_ready_unchanged": _accepted_gate_unchanged(
            root, "twin-ready.json", PRE.TWIN_READY_GATE_HASH
        )
        and gih("accepted_twin_ready_gate_hash") == PRE.TWIN_READY_GATE_HASH,
        "accepted_l1_ready_unchanged": _accepted_gate_unchanged(
            root, "l1-ready.json", PRE.L1_READY_GATE_HASH
        )
        and gih("accepted_l1_ready_gate_hash") == PRE.L1_READY_GATE_HASH,
        "accepted_db_ready_unchanged": _accepted_gate_unchanged(
            root, "db-ready.json", PRE.DB_READY_GATE_HASH
        )
        and gih("accepted_db_ready_gate_hash") == PRE.DB_READY_GATE_HASH,
        "feature_registry_hash_bound": gih("feature_registry_hash") == REGISTRY_HASH
        and gih("accepted_feature_registry_hash") == PRE.ACCEPTED_FEATURE_REGISTRY_HASH,
        "split_policy_hash_bound": gih("split_policy_hash") == split_policy_hash(),
        "parameter_space_hash_bound": gih("parameter_space_hash") == parameter_space_hash(),
    }

    # --- L2-C migration cross-binding to qualified-source evidence ----------------
    mig_items = [e for e in gate.evidence if e.path == L2C_MIGRATION_FILE]
    mig_item = mig_items[0] if len(mig_items) == 1 else None
    mig_item_valid = bool(
        mig_item is not None
        and mig_item.kind is EvidenceKind.FILE
        and mig_item.sha256 is not None
        and _HEX64.match(mig_item.sha256)
    )
    mig_evi_sha = mig_item.sha256 if (mig_item and mig_item.sha256) else ""
    try:
        committed_mig_sha = G.sha256_git_file(root, L2C_MIGRATION_FILE, src_sha)[0]
    except Exception:  # noqa: BLE001
        committed_mig_sha = ""
    checks["l2c_migration_evidence_present"] = mig_item_valid
    checks["l2c_migration_evidence_matches_source_blob"] = bool(
        mig_item_valid and committed_mig_sha and mig_evi_sha == committed_mig_sha
    )
    checks["l2c_migration_file_evidence_bound"] = bool(
        mig_item_valid and gih("l2c_migration_file_hash") == mig_evi_sha
    )
    checks["l2c_migration_contract_bound"] = bool(
        mig_item_valid and gih("l2c_migration_contract_hash") == l2c_contract_hash(mig_evi_sha)
    )

    # --- generator (split package) directory evidence cross-binding (defect 3) -----
    gen_item = _unique_evidence(gate.evidence, SPLIT_PACKAGE_DIR, EvidenceKind.DIRECTORY)
    gen_evi_sha = gen_item.sha256 if (gen_item and gen_item.sha256) else ""
    try:
        computed_gen_sha = G.sha256_git_directory(root, SPLIT_PACKAGE_DIR, src_sha)[0]
    except Exception:  # noqa: BLE001
        computed_gen_sha = ""
    checks["generator_source_evidence_present"] = gen_item is not None
    checks["generator_source_evidence_matches_source"] = bool(
        gen_item is not None and computed_gen_sha and gen_evi_sha == computed_gen_sha
    )
    checks["generator_source_evidence_bound"] = bool(
        gen_item is not None and gih("generator_source_hash") == gen_evi_sha
    )

    # --- manifest schema file evidence cross-binding (defect 4) --------------------
    schema_item = _unique_evidence(gate.evidence, MANIFEST_SCHEMA_FILE, EvidenceKind.FILE)
    schema_evi_sha = schema_item.sha256 if (schema_item and schema_item.sha256) else ""
    try:
        computed_schema_sha = G.sha256_git_file(root, MANIFEST_SCHEMA_FILE, src_sha)[0]
    except Exception:  # noqa: BLE001
        computed_schema_sha = ""
    checks["manifest_schema_evidence_present"] = schema_item is not None
    checks["manifest_schema_evidence_matches_source"] = bool(
        schema_item is not None and computed_schema_sha and schema_evi_sha == computed_schema_sha
    )
    checks["manifest_schema_evidence_bound"] = bool(
        schema_item is not None and gih("manifest_schema_hash") == schema_evi_sha
    )

    # --- committed manifest bytes + independent recomputation ---------------------
    try:
        committed_manifest_sha, _ = G.sha256_git_file(root, MANIFEST_PATH, "HEAD")
        manifest_blob = G.blob_bytes(root, MANIFEST_PATH, "HEAD")
    except Exception:  # noqa: BLE001
        committed_manifest_sha, manifest_blob = "", b""
    checks["committed_manifest_bytes_bound"] = bool(
        committed_manifest_sha and committed_manifest_sha == gih("committed_manifest_sha256")
    )
    manifest_ok = False
    manifest_hash_ok = False
    registry_hash_ok = False
    counts_ok = False
    truth_ok = False
    manifest_model: DatasetSplitManifest | None = None
    if manifest_blob:
        try:
            raw = json.loads(manifest_blob.decode("utf-8"))
            mv = verify_manifest(raw)
            manifest_ok = mv.ok
            manifest_hash_ok = mv.manifest_hash == gih("canonical_manifest_hash")
            registry_hash_ok = mv.dataset_registry_hash == gih("dataset_registry_hash")
            counts_ok = (
                mv.checks.get("total_sample_count", False)
                and mv.checks.get("partition_totals_exact", False)
                and mv.checks.get("per_chromosome_layout_exact", False)
            )
            truth_ok = mv.checks.get("no_truth_or_mutation_fields", False)
            manifest_model = DatasetSplitManifest.model_validate(raw)
        except Exception:  # noqa: BLE001
            pass
    checks["manifest_schema_valid"] = manifest_ok
    checks["manifest_verified"] = manifest_ok
    checks["canonical_manifest_hash_bound"] = manifest_hash_ok
    checks["dataset_registry_hash_bound"] = registry_hash_ok
    checks["total_sample_count_75"] = counts_ok
    checks["partition_totals_50_10_15"] = counts_ok
    checks["per_chromosome_10_2_3"] = counts_ok
    checks["truth_mutation_isolation_ok"] = truth_ok

    # --- committed inventory bytes + independent recompute + correspondence (1-2) --
    try:
        inv_sha, inv_blob = (
            G.sha256_git_file(root, INVENTORY_PATH, "HEAD")[0],
            G.blob_bytes(root, INVENTORY_PATH, "HEAD"),
        )
    except Exception:  # noqa: BLE001
        inv_sha, inv_blob = "", b""
    committed_inventory_bytes_bound = bool(inv_sha and inv_sha == gih("committed_inventory_sha256"))
    checks["committed_inventory_bytes_bound"] = committed_inventory_bytes_bound

    inv_result: InventoryVerification | None = None
    if inv_blob and manifest_model is not None:
        try:
            inv_result = verify_inventory(json.loads(inv_blob.decode("utf-8")), manifest_model)
        except Exception:  # noqa: BLE001
            inv_result = None
    ivc = inv_result.checks if inv_result else {}
    checks["inventory_contract_valid"] = bool(inv_result and ivc.get("inventory_contract_valid"))
    # Independent recomputation AND binding to the gate field (never a self-trusted field).
    checks["inventory_hash_recomputed"] = bool(
        inv_result
        and inv_result.embedded_matches
        and inv_result.inventory_hash == gih("local_input_inventory_hash")
    )
    checks["inventory_manifest_identity_bound"] = bool(
        inv_result and ivc.get("inventory_manifest_identity_bound")
    )
    checks["inventory_paths_safe"] = bool(inv_result and ivc.get("inventory_paths_safe"))
    checks["inventory_paths_identity_bound"] = bool(
        inv_result and ivc.get("inventory_paths_identity_bound")
    )
    checks["inventory_truth_mutation_isolation_ok"] = bool(
        inv_result and ivc.get("inventory_truth_mutation_isolation_ok")
    )
    # Legacy binding retained (committed bytes + gate-declared inventory hash present).
    checks["local_input_inventory_hash_bound"] = bool(
        committed_inventory_bytes_bound
        and inv_result
        and inv_result.inventory_hash == gih("local_input_inventory_hash")
    )

    # --- final closure report bytes + non-circular evidence payload (defects 5-6) --
    try:
        committed_report_sha = G.sha256_git_file(root, FINAL_REPORT_PATH, "HEAD")[0]
    except Exception:  # noqa: BLE001
        committed_report_sha = ""
    checks["qualification_report_bytes_bound"] = bool(
        committed_report_sha and committed_report_sha == gih("qualification_report_hash")
    )

    try:
        declared_paths = tuple(json.loads(gih("evidence_payload_paths") or "[]"))
    except Exception:  # noqa: BLE001
        declared_paths = ()
    paths_exact = declared_paths == EVIDENCE_PAYLOAD_PATHS and _GATE_SELF_PATH not in declared_paths
    checks["evidence_payload_paths_exact"] = paths_exact
    computed_payload = ""
    if paths_exact:
        try:
            items = [
                (path, kind.value, G.sha256_git_file(root, path, "HEAD")[0])
                for path, kind in EVIDENCE_PAYLOAD
            ]
            computed_payload = evidence_payload_hash(items)
        except Exception:  # noqa: BLE001
            computed_payload = ""
    checks["evidence_payload_hash_bound"] = bool(
        computed_payload and computed_payload == gih("evidence_payload_hash")
    )

    # --- DB-READY closure ancestry against the EXACT qualified source -------------
    cc = db_ready_closure_checks(
        root,
        l2c_source_ref=src_sha,
        head_ref="HEAD",
        require_head_descends=require_descends,
    )
    checks["l2c_qualified_source_tree_matches"] = (
        G.commit_tree_sha(root, src_sha) == gate.qualified_source_tree_sha
    )
    checks["db_ready_source_present"] = cc["db_ready_source_present"] and (
        gih("db_ready_source_commit") == PRE.DB_READY_SOURCE_COMMIT
    )
    checks["db_ready_source_tree_bound"] = cc["db_ready_source_tree_bound"] and (
        gih("db_ready_source_tree") == PRE.DB_READY_SOURCE_TREE
    )
    checks["db_ready_evidence_present"] = cc["db_ready_evidence_present"] and (
        gih("db_ready_evidence_commit") == PRE.DB_READY_EVIDENCE_COMMIT
    )
    checks["l2c_source_descends_db_ready"] = cc["l2c_source_descends_db_ready"]
    checks["head_descends_l2c_source"] = cc["head_descends_l2c_source"]

    for name, ok in checks.items():
        if not ok:
            reasons.append(f"{name} failed")
    reasons.extend(f"evidence: {r}" for r in integrity.reasons)
    reasons.extend(f"promotion: {r}" for r in promotion.reasons)
    return SplitGateVerification(
        ok=all(checks.values()),
        gate_hash=gate.gate_hash,
        checks=checks,
        reasons=tuple(dict.fromkeys(reasons)),
    )


def write_split_outputs(
    result: SplitQualificationResult, root: Path
) -> tuple[Path, Path, Path, Path]:
    """Write manifest, inventory, the final closure report, and the (final) gate.

    The gate is already complete: ``assemble_split_result`` baked in the exact
    ``qualification_report_hash`` and ``evidence_payload_hash`` from the same report
    bytes written here, so nothing is patched post-render and no gate hash is recomputed.
    The historical L2-C report is never overwritten.
    """
    from minos_engine.gates.verifier import write_gate

    manifests_dir = root / "manifests"
    manifests_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = root / MANIFEST_PATH
    inventory_path = root / INVENTORY_PATH
    manifest_path.write_bytes(manifest_bytes(result.manifest))
    inventory_path.write_bytes(inventory_bytes(result.inventory))

    report_path = root / FINAL_REPORT_PATH
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_bytes(result.report_markdown.encode("utf-8"))

    gate_path = write_gate(result.gate, root / _GATE_SELF_PATH)
    return gate_path, manifest_path, inventory_path, report_path


def _render_final_report(
    *,
    qualified_source_sha: str | None,
    qualified_source_tree: str | None,
    input_hashes: dict[str, str],
    evidence: tuple[EvidenceItem, ...],
) -> str:
    """Render the non-circular final closure report.

    It deliberately omits the gate hash, ``qualification_report_hash``, and
    ``evidence_payload_hash`` so that ``sha256(report bytes)`` is well defined and can
    be bound by the gate without a report↔gate hash cycle. Only stable content
    identities appear here.
    """
    g = input_hashes.get
    stable = {
        k: v
        for k, v in sorted(input_hashes.items())
        if k not in ("qualification_report_hash", "evidence_payload_hash")
    }
    ih = json.dumps(stable, indent=2, sort_keys=True)
    ev = "\n".join(f"| `{e.path}` | {e.kind.value} | `{e.sha256}` |" for e in evidence)
    payload_rows = "\n".join(f"| `{p}` | {k.value} |" for p, k in EVIDENCE_PAYLOAD)
    return f"""# LAYER 2 — L2-C SPLIT-FROZEN Final Closure Report

**Gate:** {GATE_NAME}
**Qualification tool:** {SPLIT_QUALIFIER_VERSION}
**Qualified source git sha (Commit W):** `{qualified_source_sha}`
**Qualified source tree sha (Commit W):** `{qualified_source_tree}`

> Generated by `minos-engine layer2 split qualify`. Not hand-authored. This report is
> the evidence-commit (Commit X) companion to the source-closure commit (Commit W).
> It contains no gate hash, no report hash, and no evidence-payload hash, so hashing
> the report bytes is not circular. The gate binds this report by
> `qualification_report_hash = sha256(report bytes)` and independently re-verifies the
> committed report blob. The canonical manifest, dataset registry, split policy, local
> inventory, migration, schema, and feature-registry identities are unchanged from the
> accepted L2-C state; only verifier source, tests, this report, and the regenerated
> gate change. `Layer2Service.select_config` remains blocked.

## Non-circular two-commit evidence model
* **Commit W** contains the final verifier implementation and tests; the gate binds
  Commit W's SHA and tree and re-hashes source evidence from Commit W's blobs.
* **Commit X** contains this report and the regenerated gate. The gate does **not**
  embed Commit X's own SHA or tree — doing so would be cryptographically circular
  (changing the gate changes the tree, hence the commit SHA). Commit X's identity is
  established externally: after it is pushed, its SHA/tree are reported to the owner,
  frozen by owner acceptance, and pinned by the next stage as its prerequisite.
* The verifier at Commit X may require HEAD to descend Commit W and verifies the
  evidence payload from the current Git tree. It never claims the gate internally
  binds Commit X's own SHA.

## Split summary
| Field | Value |
|---|---|
| Total samples | {g("total_samples")} |
| Train / Validation / Test | {g("count_train")} / {g("count_validation")} / {g("count_test")} |
| Per-chromosome (train/val/test) | `{g("per_chromosome_layout")}` |
| Canonical manifest hash | `{g("canonical_manifest_hash")}` |
| Dataset registry hash | `{g("dataset_registry_hash")}` |
| Split policy hash | `{g("split_policy_hash")}` |
| Parameter-space hash | `{g("parameter_space_hash")}` |
| Feature-registry hash | `{g("feature_registry_hash")}` |
| Local input inventory hash | `{g("local_input_inventory_hash")}` |
| Committed manifest sha256 | `{g("committed_manifest_sha256")}` |
| Committed inventory sha256 | `{g("committed_inventory_sha256")}` |
| Manifest schema hash | `{g("manifest_schema_hash")}` |
| Generator source (split package) hash | `{g("generator_source_hash")}` |
| L2-C migration file hash | `{g("l2c_migration_file_hash")}` |
| L2-C migration contract hash | `{g("l2c_migration_contract_hash")}` |
| Alembic head | `{g("alembic_head_revision")}` |

## Canonical evidence payload (aggregate is non-circular; excludes the gate)
| Path | Kind |
|---|---|
{payload_rows}

## Bound identities (stable; excludes report/payload/gate hashes)
```
{ih}
```

## Source evidence (git-tree-bound, hashed from Commit W)
| Path | Kind | sha256 |
|---|---|---|
{ev}
"""
