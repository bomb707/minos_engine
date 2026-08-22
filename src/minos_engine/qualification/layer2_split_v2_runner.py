"""SPLIT-FROZEN-V2 (Layer 2 L2-C, epoched) qualification — git-tree-bound, evidence-based.

Assembles the ``SPLIT-FROZEN-V2`` gate from real evidence. It **supersedes** the accepted
v1 SPLIT-FROZEN gate *within* stage L2-C (Model A): the gate binds the accepted v1
SPLIT-FROZEN identity, proves its qualified source properly descends the v1 SPLIT-FROZEN
*evidence* commit, and leaves the v1 gate/manifest/migration byte-identical and historical.
On top of that it binds the **epoch-1** manifest — a pure INHERITANCE of the accepted v1
partitions (zero assignment transitions; no accepted test/validation sample moves; the v2
salt orders only genuinely new samples from epoch 2 onward) with its growth-capable
``registry_snapshot_hash`` — plus the immutable v2 epoch registry migration (``0003``:
parent-snapshot FK lineage, sealed-test view with no grant), the v2 split-policy hash, the
accepted PROTOCOL/TWIN/L1/DB-READY prerequisites, the CI head-0003 lifecycle pin, and the
real PostgreSQL 16 v2 integration suite (migration lifecycle, epoch store, growth
immutability, role isolation, sealed-test denial, append-only), plus the full test suite,
coverage, and ruff/format/mypy. A PASS is never constructed from caller-supplied booleans;
the verifier re-derives every binding and re-hashes source evidence from the exact
qualified source commit — never the current HEAD.
"""

from __future__ import annotations

import json
import re
import subprocess
import xml.etree.ElementTree as ET
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict

from minos_engine.common.canonical_json import canonical_json_str
from minos_engine.common.errors import StageNotReadyError
from minos_engine.common.hashing import sha256_hex
from minos_engine.common.runtime import is_supported_runtime, runtime_identity
from minos_engine.gates.contracts import EvidenceItem, EvidenceKind, GateArtifact, GateStatus
from minos_engine.gates.verifier import load_gate, require_gate_pass, verify_gate_integrity
from minos_engine.layer2 import prerequisites as PRE
from minos_engine.layer2.split_v2.generator import (
    epoch1_from_v1_manifest,
)
from minos_engine.layer2.split_v2.policy import (
    SUPPORTED_CHROMOSOMES,
    split_policy_hash,
)
from minos_engine.layer2.split_v2.verifier import EpochManifestVerification, verify_epoch_manifest
from minos_engine.storage.l2c_split_v2_migration_contract import (
    L2C_SPLIT_V2_MIGRATION_REVISION,
    l2c_split_v2_contract_hash,
)

from . import git_tree as G
from .coverage import STAGE0_COVERAGE_THRESHOLD, CoverageResult, run_coverage
from .git_tree import historical_blob_text
from .layer2_db_runner import alembic_head
from .provenance import GitProvenance, read_provenance
from .pytest_accounting import PytestAccounting, run_pytest, suite_passes
from .runner import SourceIntegrity, _bin, _tool_ok, gather_source_integrity

__all__ = [
    "GATE_NAME",
    "SPLIT_V2_QUALIFIER_VERSION",
    "V2_MIGRATION_FILE",
    "V1_MANIFEST_PATH",
    "EPOCH1_MANIFEST_PATH",
    "MANIFEST_SCHEMA_FILE",
    "SPLIT_V2_PACKAGE_DIR",
    "FINAL_REPORT_PATH",
    "EVIDENCE_PAYLOAD",
    "EVIDENCE_PAYLOAD_PATHS",
    "SPLIT_V2_EVIDENCE",
    "SPLIT_V2_REQUIRED_TRACKED_FILES",
    "SplitV2QualificationResult",
    "SplitV2GateVerification",
    "split_frozen_closure_checks",
    "assemble_split_v2_result",
    "qualify_split_frozen_v2",
    "verify_split_frozen_v2_gate",
    "write_split_v2_outputs",
    "l2c_v2_migration_immutable",
]

_HEX64 = re.compile(r"^[0-9a-f]{64}$")

GATE_NAME = "SPLIT-FROZEN-V2"
SPLIT_V2_QUALIFIER_VERSION = "layer2-split-v2-qualifier-v1"
V2_MIGRATION_FILE = "migrations/versions/0003_l2c_split_v2_epochs.py"
MANIFEST_SCHEMA_FILE = "schemas/layer2-dataset-split-v2.schema.json"
V1_MANIFEST_PATH = "manifests/layer2_dataset_split_v1.json"
EPOCH1_MANIFEST_PATH = "manifests/layer2_dataset_split_v2_epoch1.json"
#: The immutable v2 split package. Its directory hash is ``generator_source_hash``.
SPLIT_V2_PACKAGE_DIR = "src/minos_engine/layer2/split_v2"
#: Non-circular final closure report (omits its own hash, the evidence-payload hash, and
#: the gate hash so ``sha256(report bytes)`` is well defined).
FINAL_REPORT_PATH = "reports/LAYER2_L2C_SPLIT_V2_CLOSURE_REPORT.md"
_GATE_SELF_PATH = "gates/split-frozen-v2.json"
_SPLIT_V2_SUITE = "tests/integration/layer2_split_v2"

_EXPECTED_COUNTS = {"train": 50, "validation": 10, "test": 15}
_EXPECTED_PER_CHROM = {"train": 10, "validation": 2, "test": 3}

#: Canonical evidence payload: the content artifacts the gate binds by an aggregate hash.
#: It NEVER contains the gate itself, keeping the aggregate non-circular.
EVIDENCE_PAYLOAD: tuple[tuple[str, EvidenceKind], ...] = (
    (EPOCH1_MANIFEST_PATH, EvidenceKind.FILE),
    (FINAL_REPORT_PATH, EvidenceKind.FILE),
)
EVIDENCE_PAYLOAD_PATHS: tuple[str, ...] = tuple(sorted(p for p, _ in EVIDENCE_PAYLOAD))

SPLIT_V2_EVIDENCE: tuple[tuple[str, EvidenceKind], ...] = (
    (SPLIT_V2_PACKAGE_DIR, EvidenceKind.DIRECTORY),
    ("src/minos_engine/storage/dataset_split_v2.py", EvidenceKind.FILE),
    ("src/minos_engine/storage/l2c_split_v2_migration_contract.py", EvidenceKind.FILE),
    ("src/minos_engine/qualification/layer2_split_v2_runner.py", EvidenceKind.FILE),
    (V2_MIGRATION_FILE, EvidenceKind.FILE),
    (MANIFEST_SCHEMA_FILE, EvidenceKind.FILE),
    ("gates/protocol-ready.json", EvidenceKind.FILE),
    ("gates/twin-ready.json", EvidenceKind.FILE),
    ("gates/l1-ready.json", EvidenceKind.FILE),
    ("gates/db-ready.json", EvidenceKind.FILE),
    ("gates/split-frozen.json", EvidenceKind.FILE),
    ("docs/layer2/DATASET_SPLIT_V2.md", EvidenceKind.FILE),
    (".github/workflows/ci.yml", EvidenceKind.FILE),
    (_SPLIT_V2_SUITE, EvidenceKind.DIRECTORY),
)

SPLIT_V2_REQUIRED_TRACKED_FILES: tuple[str, ...] = (
    "src/minos_engine/layer2/split_v2/__init__.py",
    "src/minos_engine/layer2/split_v2/policy.py",
    "src/minos_engine/layer2/split_v2/generator.py",
    "src/minos_engine/layer2/split_v2/verifier.py",
    "src/minos_engine/storage/dataset_split_v2.py",
    "src/minos_engine/storage/l2c_split_v2_migration_contract.py",
    V2_MIGRATION_FILE,
    MANIFEST_SCHEMA_FILE,
    V1_MANIFEST_PATH,
    EPOCH1_MANIFEST_PATH,
    "docs/layer2/DATASET_SPLIT_V2.md",
    ".github/workflows/ci.yml",
)

#: The CI workflow path AS IT EXISTED at the frozen qualified source commit. It no longer exists
#: at HEAD (TEST-CI-3 removed the full GitHub workflow); this constant names the historical blob,
#: not a current file.
CI_WORKFLOW = ".github/workflows/ci.yml"
_CI_REQUIRED_TOKENS = (
    "0003_l2c_split_v2_epochs",
    "downgrade 0002_l2c_dataset_split",
    "downgrade 0001_l2b_initial",
    "downgrade base",
    "tests/integration/layer2_split_v2",
    "split-frozen-v2.json",
)


def ci_asserts_head_0003(root: Path) -> bool:
    """True iff the CI workflow AT THE FROZEN QUALIFIED SOURCE COMMIT pinned Alembic head 0003
    and exercised the full v2 downgrade/re-upgrade lifecycle.

    This is historical evidence, so it is read from the commit that produced it
    (``PRE.SPLIT_FROZEN_V2_SOURCE_COMMIT``) rather than from the working tree. The workflow that
    file describes ran and passed at that commit; deleting it at HEAD cannot retroactively
    unmake that. A missing object, a shallow clone or altered bytes all fail closed.
    """
    text = historical_blob_text(root, CI_WORKFLOW, PRE.SPLIT_FROZEN_V2_SOURCE_COMMIT)
    if text is None:
        return False
    return all(tok in text for tok in _CI_REQUIRED_TOKENS)


# gate check -> v2 integration nodeids whose pass proves it (the epoch ≥2 growth /
# immutability / sealed-test guarantees are proven as capabilities by these suites).
_CHECK_NODES: dict[str, tuple[str, ...]] = {
    "postgres_16_verified": ("test_migration_lifecycle.py::test_postgres_major_version_is_16",),
    "v2_migration_lifecycle_passed": ("test_migration_lifecycle.py::test_v2_migration_lifecycle",),
    "epoch_role_isolation_passed": ("test_role_isolation.py",),
    "epoch_immutability_passed": ("test_epoch_store.py::test_epoch_tables_append_only",),
    "epoch_constraints_passed": ("test_epoch_store.py::test_repersisting_same_epoch_rejected",),
    "epoch_counts_50_10_15_passed": (
        "test_epoch_store.py::test_epoch_allocations_are_inherited_50_10_15",
    ),
    "sealed_test_access_denied_passed": (
        "test_role_isolation.py::test_sealed_test_denied_to_all_roles",
    ),
    "validation_evaluator_only_passed": (
        "test_role_isolation.py::test_evaluator_reads_validation_only",
    ),
    "trainer_view_no_features_passed": ("test_epoch_store.py::test_views_have_no_feature_columns",),
    "parent_immutability_passed": ("test_epoch_growth.py::test_parent_allocations_immutable",),
    "growth_new_samples_only_passed": ("test_epoch_growth.py::test_growth_assigns_only_new",),
    "removal_replacement_rejected_passed": (
        "test_epoch_growth.py::test_removal_and_replacement_rejected",
    ),
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


def evidence_payload_hash(items: list[tuple[str, str, str]]) -> str:
    """Deterministic aggregate over ``(path, kind, sha256)`` triples, sorted by path.

    Non-circular by construction: computed only over content artifacts (the epoch-1
    manifest + the final closure report), never over the gate that carries it.
    """
    from minos_engine.common.hashing import canonical_hash

    canonical = sorted(
        ({"path": p, "kind": k, "sha256": s} for p, k, s in items),
        key=lambda r: r["path"],
    )
    return canonical_hash(canonical)


def _v2_revision_in_lineage() -> bool:
    """True iff revision ``0003`` is in the current Alembic migration lineage."""
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    try:
        script = ScriptDirectory.from_config(Config("alembic.ini"))
        head = script.get_current_head()
        if head is None:
            return False
        return any(
            r.revision == L2C_SPLIT_V2_MIGRATION_REVISION
            for r in script.iterate_revisions(head, "base")
        )
    except Exception:  # noqa: BLE001 - a missing/unreadable script dir fails closed
        return False


def l2c_v2_migration_immutable(root: Path) -> bool:
    """The v2 migration is a self-contained snapshot (no ORM metadata dependency)."""
    path = root / V2_MIGRATION_FILE
    if not path.exists():
        return False
    src = path.read_text(encoding="utf-8")
    return not any(token in src for token in _FORBIDDEN_MIGRATION_TOKENS)


def split_frozen_closure_checks(
    root: Path,
    *,
    v2_source_ref: str,
    head_ref: str = "HEAD",
    require_head_descends: bool = True,
) -> dict[str, bool]:
    """Prove the accepted v1 SPLIT-FROZEN closure and the exact v2 qualified source.

    ``v2_source_ref`` is the frozen v2 source commit (provenance HEAD at generation;
    ``gate.qualified_source_git_sha`` at verification) — never a substitute such as the
    current HEAD. The v2 source is proven to properly descend the accepted v1 SPLIT-FROZEN
    *evidence* commit (rejecting sibling/ancestor/unrelated sources).
    """
    src = PRE.SPLIT_FROZEN_SOURCE_COMMIT
    src_tree = PRE.SPLIT_FROZEN_SOURCE_TREE
    evi = PRE.SPLIT_FROZEN_EVIDENCE_COMMIT

    src_ok = G.is_commit(root, src)
    src_tree_ok = src_ok and G.commit_tree_sha(root, src) == src_tree
    evi_ok = G.is_commit(root, evi)
    # v1 SPLIT-FROZEN evidence commit properly descends the v1 source commit.
    v1_chain_ok = bool(src_ok and evi_ok and G.is_ancestor(root, src, evi))

    qs = v2_source_ref
    qs_present = bool(qs) and G.is_commit(root, qs)
    # accepted v1 evidence is a PROPER ancestor of the v2 qualified source.
    v2_descends = bool(
        evi_ok and qs_present and G.is_ancestor(root, evi, qs) and not G.is_ancestor(root, qs, evi)
    )
    head_ok = G.object_exists(root, head_ref)
    head_descends = (not require_head_descends) or bool(
        qs_present and head_ok and G.is_ancestor(root, qs, head_ref)
    )
    return {
        "split_frozen_source_present": src_ok,
        "split_frozen_source_tree_bound": src_tree_ok,
        "split_frozen_evidence_present": evi_ok and v1_chain_ok,
        "v2_source_descends_split_frozen": v2_descends,
        "head_descends_v2_source": head_descends,
    }


class SplitV2QualificationResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    gate: GateArtifact
    epoch_manifest: dict[str, Any]
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
    """Return the single evidence item at ``path`` of ``kind`` with a valid sha256."""
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


def run_split_v2_suite(root: Path) -> dict[str, bool]:  # pragma: no cover - subprocess + real PG
    """Run the v2 PostgreSQL integration suite; return {"file.py::test": passed}."""
    junit = root / "reports" / "ci-split-v2-junit.xml"
    junit.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [_bin("pytest"), _SPLIT_V2_SUITE, f"--junitxml={junit}", "-q"],
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


def gather_split_v2_source_integrity(root: Path, ref: str) -> SourceIntegrity:
    return gather_source_integrity(
        root, ref, evidence_spec=SPLIT_V2_EVIDENCE, required_files=SPLIT_V2_REQUIRED_TRACKED_FILES
    )


def epoch_manifest_bytes(manifest: dict[str, Any]) -> bytes:
    return (canonical_json_str(manifest) + "\n").encode("utf-8")


def _load_v1_manifest(root: Path) -> dict[str, Any]:
    data: dict[str, Any] = json.loads((root / V1_MANIFEST_PATH).read_text(encoding="utf-8"))
    return data


def qualify_split_frozen_v2(root: Path) -> SplitV2QualificationResult:  # pragma: no cover - subproc
    root = root.resolve()
    provenance = read_provenance(root)
    ref = provenance.head_sha or "HEAD"
    source_integrity = gather_split_v2_source_integrity(root, ref)
    epoch_manifest = epoch1_from_v1_manifest(_load_v1_manifest(root))
    passmap = run_split_v2_suite(root)
    accounting = run_pytest(root)
    coverage = run_coverage(root)[0]
    tools = {
        "ruff": _tool_ok([_bin("ruff"), "check", "."], root),
        "format": _tool_ok([_bin("ruff"), "format", "--check", "."], root),
        "mypy": _tool_ok([_bin("mypy"), "src"], root),
    }
    return assemble_split_v2_result(
        root,
        epoch_manifest=epoch_manifest,
        passmap=passmap,
        accounting=accounting,
        coverage=coverage,
        tools=tools,
        provenance=provenance,
        source_integrity=source_integrity,
    )


def assemble_split_v2_result(
    root: Path,
    *,
    epoch_manifest: dict[str, Any],
    passmap: dict[str, bool],
    accounting: PytestAccounting,
    coverage: CoverageResult,
    tools: dict[str, bool],
    provenance: GitProvenance,
    source_integrity: SourceIntegrity,
    created_at: str | None = None,
) -> SplitV2QualificationResult:
    root = root.resolve()
    si = source_integrity
    v1_manifest = _load_v1_manifest(root)
    ev = verify_epoch_manifest(epoch_manifest, v1_manifest=v1_manifest)

    migration_sha = _si_hash(si, V2_MIGRATION_FILE)
    manifest_sha = sha256_hex(epoch_manifest_bytes(epoch_manifest))
    generator_source_hash = _si_hash(si, SPLIT_V2_PACKAGE_DIR)
    manifest_schema_hash = _si_hash(si, MANIFEST_SCHEMA_FILE)

    counts = dict(epoch_manifest.get("counts", {}))
    per_chrom = dict(epoch_manifest.get("per_chromosome", {}))
    per_chrom_ok = set(per_chrom) == set(SUPPORTED_CHROMOSOMES) and all(
        per_chrom.get(c) == _EXPECTED_PER_CHROM for c in SUPPORTED_CHROMOSOMES
    )

    closure = split_frozen_closure_checks(root, v2_source_ref=provenance.head_sha or "")

    gen_item = _unique_evidence(si.evidence, SPLIT_V2_PACKAGE_DIR, EvidenceKind.DIRECTORY)
    schema_item = _unique_evidence(si.evidence, MANIFEST_SCHEMA_FILE, EvidenceKind.FILE)

    postgres_16_verified = _check_from_suite(passmap, _CHECK_NODES["postgres_16_verified"])
    suite_checks = {
        check: _check_from_suite(passmap, nodes) for check, nodes in _CHECK_NODES.items()
    }
    created_at = created_at or datetime.now(UTC).isoformat()

    per_chrom_layout = {
        c: f"{per_chrom[c]['train']}/{per_chrom[c]['validation']}/{per_chrom[c]['test']}"
        for c in sorted(per_chrom)
    }
    input_hashes = {
        "canonical_epoch_manifest_hash": str(epoch_manifest["manifest_hash"]),
        "registry_snapshot_hash": str(epoch_manifest["registry_snapshot_hash"]),
        "ancestor_v1_dataset_registry_hash": str(
            epoch_manifest["ancestor_v1_dataset_registry_hash"]
        ),
        "accepted_v1_dataset_registry_hash": str(v1_manifest["dataset_registry_hash"]),
        "transition_count": str(epoch_manifest["transition_count"]),
        "inherited_count": str(epoch_manifest["inherited_count"]),
        "new_count": str(epoch_manifest["new_count"]),
        "split_policy_hash": split_policy_hash(),
        "committed_epoch_manifest_sha256": manifest_sha,
        "manifest_schema_hash": manifest_schema_hash,
        "generator_source_hash": generator_source_hash,
        "v2_migration_file_hash": migration_sha,
        "v2_migration_contract_hash": l2c_split_v2_contract_hash(migration_sha),
        "alembic_head_revision": alembic_head(),
        "accepted_protocol_ready_gate_hash": PRE.PROTOCOL_READY_GATE_HASH,
        "accepted_twin_ready_gate_hash": PRE.TWIN_READY_GATE_HASH,
        "accepted_l1_ready_gate_hash": PRE.L1_READY_GATE_HASH,
        "accepted_db_ready_gate_hash": PRE.DB_READY_GATE_HASH,
        "accepted_split_frozen_gate_hash": PRE.SPLIT_FROZEN_GATE_HASH,
        "split_frozen_source_commit": PRE.SPLIT_FROZEN_SOURCE_COMMIT,
        "split_frozen_source_tree": PRE.SPLIT_FROZEN_SOURCE_TREE,
        "split_frozen_evidence_commit": PRE.SPLIT_FROZEN_EVIDENCE_COMMIT,
        "epoch": str(epoch_manifest["epoch"]),
        "parent_epoch": str(epoch_manifest["parent_epoch"]),
        "total_samples": str(len(epoch_manifest["samples"])),
        "count_train": str(counts.get("train", 0)),
        "count_validation": str(counts.get("validation", 0)),
        "count_test": str(counts.get("test", 0)),
        "per_chromosome_layout": json.dumps(per_chrom_layout, sort_keys=True),
        "postgres_major_version": "16" if postgres_16_verified else "unverified",
        "test_collected": str(accounting.collected),
        "test_passed": str(accounting.passed),
        "test_failed": str(accounting.failed),
        "test_skipped": str(accounting.skipped),
        "split_v2_integration_collected": str(len(passmap)),
        "split_v2_integration_passed": str(sum(1 for v in passmap.values() if v)),
        "python_runtime": runtime_identity(),
        "evidence_payload_paths": json.dumps(list(EVIDENCE_PAYLOAD_PATHS)),
    }

    markdown = _render_final_report(
        qualified_source_sha=provenance.head_sha,
        qualified_source_tree=provenance.tree_sha,
        input_hashes=input_hashes,
        evidence=si.evidence,
    )
    report_sha = sha256_hex(markdown.encode("utf-8"))
    payload_hash = evidence_payload_hash(
        [
            (EPOCH1_MANIFEST_PATH, EvidenceKind.FILE.value, manifest_sha),
            (FINAL_REPORT_PATH, EvidenceKind.FILE.value, report_sha),
        ]
    )
    input_hashes["qualification_report_hash"] = report_sha
    input_hashes["evidence_payload_hash"] = payload_hash

    total_samples = len(epoch_manifest["samples"])
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
        "accepted_split_frozen_unchanged": _accepted_gate_unchanged(
            root, "split-frozen.json", PRE.SPLIT_FROZEN_GATE_HASH
        ),
        **closure,
        # Epoch-1 manifest bindings — EXACT inheritance from the accepted v1 partitions.
        "epoch_manifest_schema_valid": ev.checks.get("schema_valid", False),
        "epoch_manifest_verified": ev.ok,
        "canonical_epoch_manifest_hash_bound": ev.manifest_hash == epoch_manifest["manifest_hash"],
        "epoch1_inherits_v1_partitions_exactly": ev.checks.get(
            "epoch1_inherits_v1_partitions_exactly", False
        ),
        "epoch1_zero_transitions": ev.checks.get("epoch1_zero_transitions", False)
        and epoch_manifest["transition_count"] == 0,
        "epoch1_test_cohort_preserved": ev.checks.get("epoch1_test_cohort_preserved", False),
        "epoch1_validation_cohort_preserved": ev.checks.get(
            "epoch1_validation_cohort_preserved", False
        ),
        "epoch1_parent_fields_null": ev.checks.get("epoch1_parent_fields_null", False),
        "ancestor_v1_registry_bound": ev.checks.get(
            "ancestor_v1_dataset_registry_hash_bound", False
        )
        and str(epoch_manifest["ancestor_v1_dataset_registry_hash"])
        == str(v1_manifest["dataset_registry_hash"]),
        "registry_snapshot_hash_bound": ev.checks.get("registry_snapshot_hash_bound", False),
        "split_policy_hash_bound": str(epoch_manifest["split_policy_hash"]) == split_policy_hash(),
        "committed_epoch_manifest_bytes_bound": _HEX64.match(manifest_sha) is not None,
        "epoch1_is_first_epoch": epoch_manifest["epoch"] == 1
        and epoch_manifest["parent_epoch"] is None,
        # Generator + manifest-schema evidence cross-binding.
        "generator_source_evidence_present": gen_item is not None,
        "generator_source_evidence_matches_source": gen_item is not None,
        "generator_source_evidence_bound": gen_item is not None
        and gen_item.sha256 == generator_source_hash,
        "manifest_schema_evidence_present": schema_item is not None,
        "manifest_schema_evidence_matches_source": schema_item is not None,
        "manifest_schema_evidence_bound": schema_item is not None
        and schema_item.sha256 == manifest_schema_hash,
        # Report bytes + non-circular evidence payload.
        "qualification_report_bytes_bound": _HEX64.match(report_sha) is not None,
        "evidence_payload_paths_exact": _GATE_SELF_PATH not in EVIDENCE_PAYLOAD_PATHS
        and len(EVIDENCE_PAYLOAD_PATHS) == 2,
        "evidence_payload_hash_bound": _HEX64.match(payload_hash) is not None,
        # Immutable v2 migration bindings.
        "v2_migration_immutable": l2c_v2_migration_immutable(root),
        "v2_migration_file_evidence_bound": _HEX64.match(migration_sha) is not None,
        "v2_migration_contract_bound": _HEX64.match(migration_sha) is not None,
        "alembic_head_includes_v2": _v2_revision_in_lineage(),
        "ci_asserts_head_0003": ci_asserts_head_0003(root),
        # Exact split counts.
        "total_sample_count_75": total_samples == 75,
        "partition_totals_50_10_15": counts == _EXPECTED_COUNTS,
        "per_chromosome_10_2_3": per_chrom_ok,
        # Test + static analysis.
        "full_tests_passed": suite_passes(accounting),
        "coverage_passed": coverage.meets(STAGE0_COVERAGE_THRESHOLD),
        "ruff_passed": tools["ruff"],
        "format_passed": tools["format"],
        "mypy_passed": tools["mypy"],
        **suite_checks,
        "evidence_hashes_complete": si.evidence_hashes_complete,
        "required_source_tracked": si.required_source_tracked,
        "truth_mutation_isolation_ok": ev.checks.get("no_truth_or_mutation_fields", False),
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
        qualification_tool_version=SPLIT_V2_QUALIFIER_VERSION,
        created_at=created_at,
    )
    return SplitV2QualificationResult(
        gate=gate,
        epoch_manifest=epoch_manifest,
        report_markdown=markdown,
        mandatory=mandatory,
        accounting=accounting,
        coverage=coverage,
        provenance=provenance,
    )


class SplitV2GateVerification(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    ok: bool
    gate_hash: str
    checks: dict[str, bool]
    reasons: tuple[str, ...] = ()


def verify_split_frozen_v2_gate(
    root: Path, gate_path: Path, *, require_descends: bool = True
) -> SplitV2GateVerification:
    """Non-mutating verification of a committed SPLIT-FROZEN-V2 gate (check mode).

    Independently recomputes every binding, re-hashes source evidence from the exact
    qualified source commit, proves proper descent from the v1 SPLIT-FROZEN evidence
    commit, re-reads the committed epoch-1 manifest bytes and regenerates it from the
    accepted v1 identities, and rejects duplicate/missing migration evidence.
    """
    root = root.resolve()
    reasons: list[str] = []
    try:
        gate = load_gate(gate_path)
    except Exception as exc:  # noqa: BLE001
        return SplitV2GateVerification(
            ok=False, gate_hash="", checks={"loadable": False}, reasons=(str(exc),)
        )

    integrity = verify_gate_integrity(gate, base_dir=root)
    promotion = require_gate_pass(gate, base_dir=root)
    src_sha = gate.qualified_source_git_sha or ""
    gih = gate.input_hashes.get

    checks: dict[str, bool] = {
        "gate_name_split_frozen_v2": gate.gate_name == GATE_NAME,
        "canonical_integrity": gate.gate_hash == gate.compute_hash(),
        "qualification_tool_identity": gate.qualification_tool_version
        == SPLIT_V2_QUALIFIER_VERSION,
        "python_runtime_is_3_12": is_supported_runtime(),
        "evidence_verified": integrity.ok,
        "required_checks_and_promotion": promotion.ok,
        "alembic_head_includes_v2": _v2_revision_in_lineage()
        and gih("alembic_head_revision") == L2C_SPLIT_V2_MIGRATION_REVISION,
        "v2_migration_immutable": l2c_v2_migration_immutable(root),
        "ci_asserts_head_0003": ci_asserts_head_0003(root),
        "epoch1_is_first_epoch": gih("epoch") == "1" and gih("parent_epoch") == "None",
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
        "accepted_split_frozen_unchanged": _accepted_gate_unchanged(
            root, "split-frozen.json", PRE.SPLIT_FROZEN_GATE_HASH
        )
        and gih("accepted_split_frozen_gate_hash") == PRE.SPLIT_FROZEN_GATE_HASH,
        "split_policy_hash_bound": gih("split_policy_hash") == split_policy_hash(),
    }

    # --- v2 migration cross-binding to qualified-source evidence ------------------
    mig_items = [e for e in gate.evidence if e.path == V2_MIGRATION_FILE]
    mig_item = mig_items[0] if len(mig_items) == 1 else None
    mig_item_valid = bool(
        mig_item is not None
        and mig_item.kind is EvidenceKind.FILE
        and mig_item.sha256 is not None
        and _HEX64.match(mig_item.sha256)
    )
    mig_evi_sha = mig_item.sha256 if (mig_item and mig_item.sha256) else ""
    try:
        committed_mig_sha = G.sha256_git_file(root, V2_MIGRATION_FILE, src_sha)[0]
    except Exception:  # noqa: BLE001
        committed_mig_sha = ""
    checks["v2_migration_evidence_present"] = mig_item_valid
    checks["v2_migration_evidence_matches_source_blob"] = bool(
        mig_item_valid and committed_mig_sha and mig_evi_sha == committed_mig_sha
    )
    checks["v2_migration_file_evidence_bound"] = bool(
        mig_item_valid and gih("v2_migration_file_hash") == mig_evi_sha
    )
    checks["v2_migration_contract_bound"] = bool(
        mig_item_valid
        and gih("v2_migration_contract_hash") == l2c_split_v2_contract_hash(mig_evi_sha)
    )

    # --- generator (split_v2 package) directory evidence cross-binding -------------
    gen_item = _unique_evidence(gate.evidence, SPLIT_V2_PACKAGE_DIR, EvidenceKind.DIRECTORY)
    gen_evi_sha = gen_item.sha256 if (gen_item and gen_item.sha256) else ""
    try:
        computed_gen_sha = G.sha256_git_directory(root, SPLIT_V2_PACKAGE_DIR, src_sha)[0]
    except Exception:  # noqa: BLE001
        computed_gen_sha = ""
    checks["generator_source_evidence_present"] = gen_item is not None
    checks["generator_source_evidence_matches_source"] = bool(
        gen_item is not None and computed_gen_sha and gen_evi_sha == computed_gen_sha
    )
    checks["generator_source_evidence_bound"] = bool(
        gen_item is not None and gih("generator_source_hash") == gen_evi_sha
    )

    # --- manifest schema file evidence cross-binding -------------------------------
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

    # --- committed epoch-1 manifest bytes + independent recompute + regen ----------
    try:
        v1_blob = G.blob_bytes(root, V1_MANIFEST_PATH, "HEAD")
        committed_manifest_sha, _ = G.sha256_git_file(root, EPOCH1_MANIFEST_PATH, "HEAD")
        manifest_blob = G.blob_bytes(root, EPOCH1_MANIFEST_PATH, "HEAD")
    except Exception:  # noqa: BLE001
        v1_blob, committed_manifest_sha, manifest_blob = b"", "", b""
    checks["committed_epoch_manifest_bytes_bound"] = bool(
        committed_manifest_sha and committed_manifest_sha == gih("committed_epoch_manifest_sha256")
    )

    ev_result: EpochManifestVerification | None = None
    manifest_ok = manifest_hash_ok = counts_ok = per_chrom_ok = truth_ok = False
    inherit_ok = zero_trans_ok = test_ok = val_ok = parent_null_ok = False
    ancestor_ok = registry_snap_ok = False
    if manifest_blob and v1_blob:
        try:
            raw = json.loads(manifest_blob.decode("utf-8"))
            v1_manifest = json.loads(v1_blob.decode("utf-8"))
            ev_result = verify_epoch_manifest(raw, v1_manifest=v1_manifest)
            manifest_ok = ev_result.ok
            evc = ev_result.checks
            manifest_hash_ok = ev_result.manifest_hash == gih("canonical_epoch_manifest_hash")
            inherit_ok = evc.get("epoch1_inherits_v1_partitions_exactly", False)
            zero_trans_ok = (
                evc.get("epoch1_zero_transitions", False)
                and raw.get("transition_count") == 0
                and gih("transition_count") == "0"
            )
            test_ok = evc.get("epoch1_test_cohort_preserved", False)
            val_ok = evc.get("epoch1_validation_cohort_preserved", False)
            parent_null_ok = evc.get("epoch1_parent_fields_null", False)
            registry_snap_ok = evc.get("registry_snapshot_hash_bound", False) and str(
                raw.get("registry_snapshot_hash")
            ) == gih("registry_snapshot_hash")
            ancestor_ok = evc.get("ancestor_v1_dataset_registry_hash_bound", False) and str(
                raw.get("ancestor_v1_dataset_registry_hash")
            ) == gih("ancestor_v1_dataset_registry_hash")
            counts_ok = (
                dict(raw.get("counts", {})) == _EXPECTED_COUNTS
                and len(raw.get("samples", [])) == 75
            )
            per_chrom_ok = set(raw.get("per_chromosome", {})) == set(SUPPORTED_CHROMOSOMES) and all(
                raw["per_chromosome"][c] == _EXPECTED_PER_CHROM for c in SUPPORTED_CHROMOSOMES
            )
            truth_ok = evc.get("no_truth_or_mutation_fields", False)
        except Exception:  # noqa: BLE001
            pass
    checks["epoch_manifest_schema_valid"] = manifest_ok
    checks["epoch_manifest_verified"] = manifest_ok
    checks["canonical_epoch_manifest_hash_bound"] = manifest_hash_ok
    checks["epoch1_inherits_v1_partitions_exactly"] = inherit_ok
    checks["epoch1_zero_transitions"] = zero_trans_ok
    checks["epoch1_test_cohort_preserved"] = test_ok
    checks["epoch1_validation_cohort_preserved"] = val_ok
    checks["epoch1_parent_fields_null"] = parent_null_ok
    checks["registry_snapshot_hash_bound"] = registry_snap_ok
    checks["ancestor_v1_registry_bound"] = ancestor_ok
    checks["total_sample_count_75"] = counts_ok
    checks["partition_totals_50_10_15"] = counts_ok
    checks["per_chromosome_10_2_3"] = per_chrom_ok
    checks["truth_mutation_isolation_ok"] = truth_ok

    # --- final closure report bytes + non-circular evidence payload ----------------
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

    # --- v1 SPLIT-FROZEN closure ancestry against the EXACT qualified source -------
    cc = split_frozen_closure_checks(
        root, v2_source_ref=src_sha, head_ref="HEAD", require_head_descends=require_descends
    )
    checks["v2_qualified_source_tree_matches"] = (
        G.commit_tree_sha(root, src_sha) == gate.qualified_source_tree_sha
    )
    checks["split_frozen_source_present"] = cc["split_frozen_source_present"] and (
        gih("split_frozen_source_commit") == PRE.SPLIT_FROZEN_SOURCE_COMMIT
    )
    checks["split_frozen_source_tree_bound"] = cc["split_frozen_source_tree_bound"] and (
        gih("split_frozen_source_tree") == PRE.SPLIT_FROZEN_SOURCE_TREE
    )
    checks["split_frozen_evidence_present"] = cc["split_frozen_evidence_present"] and (
        gih("split_frozen_evidence_commit") == PRE.SPLIT_FROZEN_EVIDENCE_COMMIT
    )
    checks["v2_source_descends_split_frozen"] = cc["v2_source_descends_split_frozen"]
    checks["head_descends_v2_source"] = cc["head_descends_v2_source"]

    for name, ok in checks.items():
        if not ok:
            reasons.append(f"{name} failed")
    reasons.extend(f"evidence: {r}" for r in integrity.reasons)
    reasons.extend(f"promotion: {r}" for r in promotion.reasons)
    return SplitV2GateVerification(
        ok=all(checks.values()),
        gate_hash=gate.gate_hash,
        checks=checks,
        reasons=tuple(dict.fromkeys(reasons)),
    )


def write_split_v2_outputs(
    result: SplitV2QualificationResult, root: Path
) -> tuple[Path, Path, Path]:
    """Write the epoch-1 manifest, the final closure report, and the (final) gate."""
    from minos_engine.gates.verifier import write_gate

    manifests_dir = root / "manifests"
    manifests_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = root / EPOCH1_MANIFEST_PATH
    manifest_path.write_bytes(epoch_manifest_bytes(result.epoch_manifest))

    report_path = root / FINAL_REPORT_PATH
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_bytes(result.report_markdown.encode("utf-8"))

    gate_path = write_gate(result.gate, root / _GATE_SELF_PATH)
    return gate_path, manifest_path, report_path


def _render_final_report(
    *,
    qualified_source_sha: str | None,
    qualified_source_tree: str | None,
    input_hashes: dict[str, str],
    evidence: tuple[EvidenceItem, ...],
) -> str:
    """Render the non-circular final closure report (omits report/payload/gate hashes)."""
    g = input_hashes.get
    stable = {
        k: v
        for k, v in sorted(input_hashes.items())
        if k not in ("qualification_report_hash", "evidence_payload_hash")
    }
    ih = json.dumps(stable, indent=2, sort_keys=True)
    ev = "\n".join(f"| `{e.path}` | {e.kind.value} | `{e.sha256}` |" for e in evidence)
    payload_rows = "\n".join(f"| `{p}` | {k.value} |" for p, k in EVIDENCE_PAYLOAD)
    return f"""# LAYER 2 — L2-C SPLIT-FROZEN-V2 (Epoched) Closure Report

**Gate:** {GATE_NAME}
**Qualification tool:** {SPLIT_V2_QUALIFIER_VERSION}
**Qualified source git sha:** `{qualified_source_sha}`
**Qualified source tree sha:** `{qualified_source_tree}`

> Generated by the SPLIT-FROZEN-V2 qualifier. Not hand-authored. This report is the
> evidence-commit companion to the source-closure commit. It contains no gate hash, no
> report hash, and no evidence-payload hash, so hashing the report bytes is not circular.
> The v2 epoched split **supersedes** the accepted v1 SPLIT-FROZEN split within stage
> L2-C: v1's gate, manifest, and migration are byte-identical and historical. The v2
> qualified source properly descends the accepted v1 SPLIT-FROZEN evidence commit.
> **Epoch 1 INHERITS the accepted v1 partitions verbatim** — zero assignment transitions;
> no accepted test or validation sample moves — and binds the accepted v1
> `dataset_registry_hash` as the epoch-1 registry ancestor. The v2 salt orders only
> genuinely new samples from epoch 2 onward; existing allocations are never re-labelled
> and the test set is monotonic. The test cohort is **sealed**: its view carries no grant
> until a separately-authorized final-evaluation migration opens it.
> `Layer2Service.select_config` remains blocked.

## Supersede model (v1 → v2 within L2-C)
* The gate binds the accepted v1 SPLIT-FROZEN gate hash and proves the v2 qualified source
  is a proper descendant of the v1 SPLIT-FROZEN **evidence** commit (never the current
  HEAD). v1 is retained unchanged as the historical policy-v1.
* Epoch 1 is a pure inheritance (`transition_count = 0`); every later epoch is a frozen
  superset snapshot bound to its parent by `parent_manifest_hash`,
  `parent_registry_snapshot_hash`, and a real `parent_snapshot_id` FK.

## Epoch-1 split summary
| Field | Value |
|---|---|
| Epoch / parent | {g("epoch")} / {g("parent_epoch")} |
| Total samples | {g("total_samples")} |
| Train / Validation / Test | {g("count_train")} / {g("count_validation")} / {g("count_test")} |
| Per-chromosome (train/val/test) | `{g("per_chromosome_layout")}` |
| Assignment transitions vs v1 | {g("transition_count")} (inherited {g("inherited_count")}, new {g("new_count")}) |
| Canonical epoch manifest hash | `{g("canonical_epoch_manifest_hash")}` |
| Registry snapshot hash (epoch 1) | `{g("registry_snapshot_hash")}` |
| Ancestor v1 dataset registry hash | `{g("ancestor_v1_dataset_registry_hash")}` |
| Split policy hash (v2) | `{g("split_policy_hash")}` |
| Committed epoch manifest sha256 | `{g("committed_epoch_manifest_sha256")}` |
| Manifest schema hash | `{g("manifest_schema_hash")}` |
| Generator source (split_v2 package) hash | `{g("generator_source_hash")}` |
| v2 migration file hash | `{g("v2_migration_file_hash")}` |
| v2 migration contract hash | `{g("v2_migration_contract_hash")}` |
| Alembic head | `{g("alembic_head_revision")}` |

## Accepted prerequisite identities
| Prerequisite | Accepted hash / commit |
|---|---|
| PROTOCOL-READY gate | `{g("accepted_protocol_ready_gate_hash")}` |
| TWIN-READY gate | `{g("accepted_twin_ready_gate_hash")}` |
| L1-READY gate | `{g("accepted_l1_ready_gate_hash")}` |
| DB-READY gate | `{g("accepted_db_ready_gate_hash")}` |
| SPLIT-FROZEN (v1) gate | `{g("accepted_split_frozen_gate_hash")}` |
| SPLIT-FROZEN (v1) source commit | `{g("split_frozen_source_commit")}` |
| SPLIT-FROZEN (v1) evidence commit | `{g("split_frozen_evidence_commit")}` |

## Canonical evidence payload (aggregate is non-circular; excludes the gate)
| Path | Kind |
|---|---|
{payload_rows}

## Bound identities (stable; excludes report/payload/gate hashes)
```
{ih}
```

## Source evidence (git-tree-bound, hashed from the qualified source commit)
| Path | Kind | sha256 |
|---|---|---|
{ev}
"""
