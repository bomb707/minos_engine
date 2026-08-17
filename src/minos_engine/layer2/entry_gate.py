"""Hardened Layer 2 entry gate (L1-READY), fail-closed.

Layer 2 begins only after the committed ``l1-ready.json`` verifies against the
repository-owned accepted identities in :mod:`minos_engine.layer2.prerequisites`
AND the full accepted git history is proven. The public request
(:class:`EntryGateRequest`) carries **only** runtime paths/context: callers cannot
select the expected gate hash, source commit/tree, schema hash, profiler hash, or
version — those are pinned constants, not inputs (extra fields are forbidden, so a
caller attempting to override an accepted identity is rejected at construction).

The verifier proves 34 numbered invariants (see :func:`verify_l2_entry_gate`),
returning deterministic, machine-readable reason codes. Any missing git object,
shallow/incomplete history, or divergent/sibling/rewritten/unrelated history fails
closed. This does not unblock ``Layer2Service.select_config`` (still
:class:`StageNotReadyError`); it only proves the L2-A prerequisite.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from minos_engine.common.errors import GateError
from minos_engine.gates.contracts import GateArtifact, GateStatus
from minos_engine.gates.required_checks import required_checks_for
from minos_engine.gates.verifier import require_gate_pass, verify_gate_integrity
from minos_engine.qualification import git_tree as G
from minos_engine.qualification.evidence import sha256_file
from minos_engine.schema_registry import validate_against

from .contracts import AcceptedPrerequisiteIdentity
from .prerequisites import ACCEPTED

__all__ = [
    "EntryGateRequest",
    "EntryGateResult",
    "CHECK_ORDER",
    "verify_l2_entry_gate",
    "require_l2_entry_gate",
]

_L1_GATE_NAME = "L1-READY"
_CANONICAL_GATE = "gates/l1-ready.json"
_CANONICAL_REPORT = "reports/LAYER1_QUALIFICATION_REPORT.md"

#: Deterministic ordering of the 34 invariants the entry gate proves.
CHECK_ORDER: tuple[str, ...] = (
    "l1_ready_present",  # 1
    "gate_parses",  # 2
    "gate_name_l1_ready",  # 3
    "status_pass",  # 4
    "canonical_hash_valid",  # 5
    "gate_hash_accepted",  # 6
    "required_checks_present",  # 7
    "mandatory_checks_true",  # 8
    "evidence_verified",  # 9
    "qualification_report_matches",  # 10
    "layer1_schema_hash_accepted",  # 11
    "profiler_config_hash_accepted",  # 12
    "profiler_version_accepted",  # 13
    "qualified_source_commit_accepted",  # 14
    "qualified_source_tree_accepted",  # 15
    "qualified_commit_present",  # 16
    "qualified_tree_matches_git",  # 17
    "artifact_commit_present",  # 18
    "artifact_tree_matches_git",  # 19
    "artifact_proper_descends_source",  # 20
    "head_descends_artifact",  # 21
    "v2_framework_descends_artifact",  # 22
    "v2_evidence_descends_framework",  # 23
    "owner_descends_v2_evidence",  # 24
    "head_descends_owner",  # 25
    "git_objects_present",  # 26
    "git_history_complete",  # 27
    "history_not_divergent",  # 28
    "evidence_nonempty",  # 29
    "evidence_paths_unique",  # 30
    "evidence_paths_within_repo",  # 31
    "evidence_no_symlink_escape",  # 32
    "gate_fields_wellformed",  # 33
    "reason_codes_machine_readable",  # 34
)


class EntryGateRequest(BaseModel):
    """Runtime locator context only — never accepted identities.

    The verifier reads only the canonical repository paths ``gates/l1-ready.json``
    and ``reports/LAYER1_QUALIFICATION_REPORT.md`` under ``repo_root``. Optional
    overrides remain for testability but are constrained: an override must be a
    repo-relative path that resolves within ``repo_root`` and equals the canonical
    path (no absolute paths, no ``..`` escapes, no symlink escapes). An external
    gate/report is rejected before its bytes are read, even if those bytes carry an
    accepted hash.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    repo_root: str = Field(min_length=1)
    head_ref: str = "HEAD"
    l1_ready_path: str | None = None
    qualification_report_path: str | None = None


class EntryGateResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    ok: bool
    checks: dict[str, bool]
    reasons: tuple[str, ...] = ()


def _all_false(reason: str) -> EntryGateResult:
    checks = dict.fromkeys(CHECK_ORDER, False)
    checks["reason_codes_machine_readable"] = True
    return EntryGateResult(ok=False, checks=checks, reasons=(reason,))


def _resolve_canonical(root: Path, override: str | None, canonical: str) -> tuple[Path | None, str]:
    """Resolve a canonical repo path or a constrained override; fail closed.

    Returns ``(path, "")`` on success or ``(None, CODE)`` when the override is
    absolute, escapes the repo, is non-canonical, or escapes via a symlink.
    """
    if override is None:
        target = root / canonical
    else:
        p = Path(override)
        if p.is_absolute():
            return None, "EXTERNAL_PATH_ABSOLUTE"
        if ".." in p.parts:
            return None, "EXTERNAL_PATH_ESCAPE"
        try:
            rel = (root / p).resolve().relative_to(root.resolve())
        except ValueError:
            return None, "EXTERNAL_PATH_ESCAPE"
        if rel.as_posix() != canonical:
            return None, "EXTERNAL_PATH_NOT_CANONICAL"
        target = root / p
    # Reject a symlink at the canonical location that resolves outside the repo.
    real = Path(os.path.realpath(target))
    root_real = root.resolve()
    if not (real == root_real or real.is_relative_to(root_real)):
        return None, "EXTERNAL_PATH_SYMLINK"
    return target, ""


def _evidence_path_within(root: Path, relpath: str) -> bool:
    p = Path(relpath)
    return not (p.is_absolute() or ".." in p.parts)


def _evidence_no_symlink_escape(root: Path, relpath: str) -> bool:
    full = root / relpath
    try:
        real = full.resolve()
    except OSError:
        return False
    root_real = root.resolve()
    return real == root_real or real.is_relative_to(root_real)


def verify_l2_entry_gate(request: EntryGateRequest) -> EntryGateResult:
    """Verify the L1-READY entry gate against the pinned accepted identities."""
    return _verify_against(request, ACCEPTED)


def _verify_against(  # noqa: C901, PLR0912, PLR0915 - one explicit check per invariant
    request: EntryGateRequest, accepted: AcceptedPrerequisiteIdentity
) -> EntryGateResult:
    root = Path(request.repo_root).resolve()
    l1_path, gate_code = _resolve_canonical(root, request.l1_ready_path, _CANONICAL_GATE)
    if l1_path is None:
        return _all_false(gate_code)
    report_path, report_code = _resolve_canonical(
        root, request.qualification_report_path, _CANONICAL_REPORT
    )
    if report_path is None:
        return _all_false(report_code)

    checks: dict[str, bool] = {}
    reasons: list[str] = []

    def record(name: str, ok: bool, code: str) -> bool:
        checks[name] = ok
        if not ok:
            reasons.append(code)
        return ok

    # (1) l1-ready.json exists.
    if not l1_path.exists():
        return _all_false("L1_READY_MISSING")
    checks["l1_ready_present"] = True

    # (2)/(5)/(33) parse + schema + canonical-hash integrity.
    try:
        raw = json.loads(l1_path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return _all_false("GATE_UNPARSEABLE")
    checks["gate_parses"] = True
    try:
        validate_against("gate-artifact-v1", raw)
    except Exception:  # noqa: BLE001
        return _all_false("GATE_SCHEMA_INVALID")
    try:
        gate = GateArtifact.model_validate(raw)
    except Exception as exc:  # noqa: BLE001
        code = "CANONICAL_HASH_INVALID" if "gate_hash" in str(exc) else "GATE_MALFORMED"
        return _all_false(code)
    checks["gate_fields_wellformed"] = True
    record("canonical_hash_valid", gate.gate_hash == gate.compute_hash(), "CANONICAL_HASH_INVALID")

    # (3) gate_name; (4) status; (6) accepted gate hash.
    record("gate_name_l1_ready", gate.gate_name == _L1_GATE_NAME, "GATE_NAME_MISMATCH")
    record("status_pass", gate.status is GateStatus.PASS, "GATE_STATUS_NOT_PASS")
    record("gate_hash_accepted", gate.gate_hash == accepted.l1_gate_hash, "GATE_HASH_NOT_ACCEPTED")

    # (7)/(8) required checks present + all mandatory checks true.
    required = required_checks_for(_L1_GATE_NAME)
    present = set(gate.mandatory_checks)
    record("required_checks_present", required.issubset(present), "REQUIRED_CHECKS_MISSING")
    all_true = all(gate.mandatory_checks.get(n) is True for n in gate.mandatory_checks) and all(
        gate.mandatory_checks.get(n) is True for n in required
    )
    record("mandatory_checks_true", all_true, "MANDATORY_CHECK_FALSE")

    # (9) evidence exists and re-hashes (git-bound); (29)/(30)/(31)/(32) evidence hygiene.
    record("evidence_nonempty", bool(gate.evidence), "EVIDENCE_EMPTY")
    ev_paths = [e.path for e in gate.evidence]
    record("evidence_paths_unique", len(ev_paths) == len(set(ev_paths)), "EVIDENCE_DUPLICATE_PATH")
    record(
        "evidence_paths_within_repo",
        all(_evidence_path_within(root, p) for p in ev_paths),
        "EVIDENCE_PATH_ESCAPE",
    )
    record(
        "evidence_no_symlink_escape",
        all(_evidence_no_symlink_escape(root, p) for p in ev_paths),
        "EVIDENCE_SYMLINK_ESCAPE",
    )
    integrity = verify_gate_integrity(gate, base_dir=root)
    promotion = require_gate_pass(gate, base_dir=root)
    if not record("evidence_verified", integrity.ok, "EVIDENCE_HASH_MISMATCH"):
        reasons.extend(f"EVIDENCE:{r}" for r in integrity.reasons)
    if not promotion.ok:
        reasons.extend(f"PROMOTION:{r}" for r in promotion.reasons)

    # (10) qualification report exists + hash matches the gate.
    ih = gate.input_hashes
    if not report_path.exists():
        record("qualification_report_matches", False, "QUALIFICATION_REPORT_MISSING")
    else:
        actual = sha256_file(report_path)
        record(
            "qualification_report_matches",
            ih.get("qualification_report_hash") == actual,
            "QUALIFICATION_REPORT_HASH_MISMATCH",
        )

    # (11)/(12)/(13) accepted Layer 1 identity bindings.
    record(
        "layer1_schema_hash_accepted",
        ih.get("layer1_schema_hash") == accepted.layer1_schema_hash,
        "LAYER1_SCHEMA_HASH_MISMATCH",
    )
    record(
        "profiler_config_hash_accepted",
        ih.get("profiler_config_hash") == accepted.profiler_config_hash,
        "PROFILER_CONFIG_HASH_MISMATCH",
    )
    record(
        "profiler_version_accepted",
        ih.get("profiler_version") == accepted.profiler_version,
        "PROFILER_VERSION_MISMATCH",
    )

    # (14)/(15) qualified-source identities recorded in the gate equal the accepted.
    record(
        "qualified_source_commit_accepted",
        gate.qualified_source_git_sha == accepted.qualified_source_commit,
        "QUALIFIED_SOURCE_COMMIT_MISMATCH",
    )
    record(
        "qualified_source_tree_accepted",
        gate.qualified_source_tree_sha == accepted.qualified_source_tree,
        "QUALIFIED_SOURCE_TREE_MISMATCH",
    )

    _verify_git_history(root, request.head_ref, accepted, record)

    reasons_out = tuple(dict.fromkeys(reasons))
    checks["reason_codes_machine_readable"] = True
    ok = all(checks.get(name, False) for name in CHECK_ORDER)
    return EntryGateResult(ok=ok, checks=checks, reasons=reasons_out)


def _verify_git_history(  # noqa: C901, PLR0912, PLR0915 - one explicit check per invariant
    root: Path,
    head_ref: str,
    accepted: AcceptedPrerequisiteIdentity,
    record: Callable[[str, bool, str], bool],
) -> None:
    git_keys = (
        "qualified_commit_present",
        "qualified_tree_matches_git",
        "artifact_commit_present",
        "artifact_tree_matches_git",
        "artifact_proper_descends_source",
        "head_descends_artifact",
        "v2_framework_descends_artifact",
        "v2_evidence_descends_framework",
        "owner_descends_v2_evidence",
        "head_descends_owner",
        "git_objects_present",
        "git_history_complete",
        "history_not_divergent",
    )
    if not G.is_git_repo(root):
        for k in git_keys:
            record(k, False, "NOT_A_GIT_REPO")
        return

    shallow = G.is_shallow(root)
    record("git_history_complete", not shallow, "GIT_HISTORY_SHALLOW")

    src = accepted.qualified_source_commit
    art = accepted.artifact_commit
    v2f = accepted.v2_framework_commit
    v2e = accepted.v2_evidence_commit
    own = accepted.owner_commit

    src_ok = G.object_exists(root, src)
    art_ok = G.object_exists(root, art)
    v2f_ok = G.object_exists(root, v2f)
    v2e_ok = G.object_exists(root, v2e)
    own_ok = G.object_exists(root, own)
    head_ok = G.object_exists(root, head_ref)

    record("qualified_commit_present", src_ok, "QUALIFIED_COMMIT_ABSENT")
    record("artifact_commit_present", art_ok, "ARTIFACT_COMMIT_ABSENT")
    all_present = src_ok and art_ok and v2f_ok and v2e_ok and own_ok and head_ok
    if not v2f_ok:
        reasons_code = "V2_FRAMEWORK_ABSENT"
        record("v2_framework_descends_artifact", False, reasons_code)
    if not v2e_ok:
        record("v2_evidence_descends_framework", False, "V2_EVIDENCE_ABSENT")
    if not own_ok:
        record("owner_descends_v2_evidence", False, "OWNER_COMMIT_ABSENT")
    if not head_ok:
        record("head_descends_artifact", False, "HEAD_UNRESOLVED")
    record("git_objects_present", all_present, "GIT_OBJECT_ABSENT")

    # (17)/(19) committed trees match the accepted trees.
    record(
        "qualified_tree_matches_git",
        src_ok and G.commit_tree_sha(root, src) == accepted.qualified_source_tree,
        "QUALIFIED_TREE_MISMATCH",
    )
    record(
        "artifact_tree_matches_git",
        art_ok and G.commit_tree_sha(root, art) == accepted.artifact_tree,
        "ARTIFACT_TREE_MISMATCH",
    )

    # (20) artifact properly descends source.
    proper = bool(
        src_ok and art_ok and G.is_ancestor(root, src, art) and not G.is_ancestor(root, art, src)
    )
    record("artifact_proper_descends_source", proper, "ARTIFACT_NOT_DESCENDANT_OF_SOURCE")

    # (21) HEAD descends artifact.
    record(
        "head_descends_artifact",
        bool(art_ok and head_ok and G.is_ancestor(root, art, head_ref)),
        "HEAD_NOT_DESCENDANT_OF_ARTIFACT",
    )
    # (22) v2 framework descends artifact.
    record(
        "v2_framework_descends_artifact",
        bool(art_ok and v2f_ok and G.is_ancestor(root, art, v2f)),
        "V2_FRAMEWORK_NOT_DESCENDANT_OF_ARTIFACT",
    )
    # (23) v2 evidence descends framework.
    record(
        "v2_evidence_descends_framework",
        bool(v2f_ok and v2e_ok and G.is_ancestor(root, v2f, v2e)),
        "V2_EVIDENCE_NOT_DESCENDANT_OF_FRAMEWORK",
    )
    # (24) owner descends v2 evidence.
    record(
        "owner_descends_v2_evidence",
        bool(v2e_ok and own_ok and G.is_ancestor(root, v2e, own)),
        "OWNER_NOT_DESCENDANT_OF_V2_EVIDENCE",
    )
    # (25) HEAD descends owner.
    record(
        "head_descends_owner",
        bool(own_ok and head_ok and G.is_ancestor(root, own, head_ref)),
        "HEAD_NOT_DESCENDANT_OF_OWNER",
    )

    # (28) aggregate: no divergent/sibling/unrelated/rewritten history anywhere.
    chain_ok = all(
        (
            proper,
            bool(art_ok and head_ok and G.is_ancestor(root, art, head_ref)),
            bool(art_ok and v2f_ok and G.is_ancestor(root, art, v2f)),
            bool(v2f_ok and v2e_ok and G.is_ancestor(root, v2f, v2e)),
            bool(v2e_ok and own_ok and G.is_ancestor(root, v2e, own)),
            bool(own_ok and head_ok and G.is_ancestor(root, own, head_ref)),
        )
    )
    record("history_not_divergent", chain_ok, "HISTORY_DIVERGENT")


def require_l2_entry_gate(request: EntryGateRequest) -> None:
    """Raise :class:`GateError` unless the L2-A entry gate passes."""
    result = verify_l2_entry_gate(request)
    if not result.ok:
        raise GateError("; ".join(result.reasons))
