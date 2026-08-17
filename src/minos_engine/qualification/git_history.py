"""Read-only Git-history preflight for the committed gates.

Qualification intentionally verifies *historical* commits, trees, and blobs. A
shallow clone lacks those objects, and the failure must be diagnosed precisely —
"the qualified commit is absent" is NOT the same as "an evidence file is
untracked". This preflight distinguishes the cases before any evidence
enumeration, using distinct reason codes, and fails closed.
"""

from __future__ import annotations

import json
from enum import Enum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict

from . import git_tree as G

__all__ = [
    "HistoryReason",
    "HistoryFinding",
    "HistoryCheckResult",
    "check_gate_history",
    "check_repository_history",
]


class HistoryReason(str, Enum):
    GIT_HISTORY_INCOMPLETE = "GIT_HISTORY_INCOMPLETE"
    QUALIFIED_COMMIT_UNAVAILABLE = "QUALIFIED_COMMIT_UNAVAILABLE"
    QUALIFIED_TREE_UNAVAILABLE = "QUALIFIED_TREE_UNAVAILABLE"
    QUALIFIED_TREE_MISMATCH = "QUALIFIED_TREE_MISMATCH"
    EVIDENCE_PATH_MISSING = "EVIDENCE_PATH_MISSING"
    EVIDENCE_HASH_MISMATCH = "EVIDENCE_HASH_MISMATCH"
    GATE_UNREADABLE = "GATE_UNREADABLE"
    NOT_A_GIT_REPO = "NOT_A_GIT_REPO"


class HistoryFinding(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    gate: str
    code: HistoryReason
    detail: str


class HistoryCheckResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    ok: bool
    shallow: bool
    findings: tuple[HistoryFinding, ...] = ()


def _load_gate_json(gate_path: Path) -> dict[str, Any]:
    data: dict[str, Any] = json.loads(gate_path.read_text(encoding="utf-8"))
    return data


def check_gate_history(root: Path, gate_path: Path) -> list[HistoryFinding]:
    """Verify the historical objects a gate depends on exist and are consistent."""
    name = gate_path.name
    findings: list[HistoryFinding] = []
    try:
        gate = _load_gate_json(gate_path)
    except Exception as exc:  # noqa: BLE001 - any read/parse failure fails closed
        return [HistoryFinding(gate=name, code=HistoryReason.GATE_UNREADABLE, detail=str(exc))]

    commit = gate.get("qualified_source_git_sha") or ""
    tree = gate.get("qualified_source_tree_sha") or ""

    commit_present = G.object_exists(root, commit)
    if not commit_present:
        findings.append(
            HistoryFinding(
                gate=name,
                code=HistoryReason.QUALIFIED_COMMIT_UNAVAILABLE,
                detail=f"qualified commit {commit} is not present locally",
            )
        )
    if not G.object_exists(root, tree):
        findings.append(
            HistoryFinding(
                gate=name,
                code=HistoryReason.QUALIFIED_TREE_UNAVAILABLE,
                detail=f"qualified tree {tree} is not present locally",
            )
        )
    if commit_present and G.commit_tree_sha(root, commit) != tree:
        findings.append(
            HistoryFinding(
                gate=name,
                code=HistoryReason.QUALIFIED_TREE_MISMATCH,
                detail=f"commit {commit} tree != qualified tree {tree}",
            )
        )

    # Only enumerate/hash evidence once the qualified commit is present.
    if commit_present:
        for item in gate.get("evidence", []):
            path = item.get("path")
            recorded = item.get("sha256")
            if not path or recorded is None:
                continue
            try:
                if not G.list_tree(root, commit, path):
                    findings.append(
                        HistoryFinding(
                            gate=name,
                            code=HistoryReason.EVIDENCE_PATH_MISSING,
                            detail=f"{path} absent from qualified commit",
                        )
                    )
                    continue
                actual = G.hash_git_path(root, path, commit)
            except G.GitUnavailableError as exc:
                findings.append(
                    HistoryFinding(
                        gate=name, code=HistoryReason.EVIDENCE_PATH_MISSING, detail=f"{path}: {exc}"
                    )
                )
                continue
            if actual != recorded:
                findings.append(
                    HistoryFinding(
                        gate=name,
                        code=HistoryReason.EVIDENCE_HASH_MISMATCH,
                        detail=f"{path} hash != recorded",
                    )
                )
    return findings


def check_repository_history(
    root: Path, *, protocol_gate: Path, twin_gate: Path
) -> HistoryCheckResult:
    """Verify both gates' historical objects; fail closed on a shallow clone."""
    root = root.resolve()
    if not G.is_git_repo(root):
        return HistoryCheckResult(
            ok=False,
            shallow=False,
            findings=(
                HistoryFinding(gate="<repo>", code=HistoryReason.NOT_A_GIT_REPO, detail=str(root)),
            ),
        )
    shallow = G.is_shallow(root)
    findings: list[HistoryFinding] = []
    for gate_path in (protocol_gate, twin_gate):
        if gate_path.exists():
            findings.extend(check_gate_history(root, gate_path))
        else:
            findings.append(
                HistoryFinding(
                    gate=gate_path.name,
                    code=HistoryReason.GATE_UNREADABLE,
                    detail="gate file not found",
                )
            )
    # When history is missing AND the repo is shallow, surface the true cause.
    if findings and shallow:
        findings.insert(
            0,
            HistoryFinding(
                gate="<repo>",
                code=HistoryReason.GIT_HISTORY_INCOMPLETE,
                detail="shallow clone: required historical objects are absent — "
                "check out full history (fetch-depth: 0)",
            ),
        )
    return HistoryCheckResult(ok=not findings, shallow=shallow, findings=tuple(findings))
