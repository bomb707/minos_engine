"""Git-ancestry verification for two-commit qualification (fail-closed).

The non-mutating gate checks must hold not only at the original artifact commit
but at any later repository commit that genuinely descends from the accepted
artifact history. This proves the required history using Git objects
(``git merge-base --is-ancestor``), never from filenames, timestamps, commit
messages, or a caller-provided boolean.

Invariants (Phase 2):
  1. the qualified source commit exists;
  2. its tree equals the tree recorded by the gate;
  3. the accepted artifact commit is a *proper* descendant of the qualified
     source commit (``artifact_commit`` given), i.e. it descends from — and is not
     equal to — the source;
  4. the checked repository HEAD descends from (or equals) the accepted artifact
     commit;
  5. sibling, divergent, rewritten, or unrelated history is rejected (the
     ancestry probes return False);
  6. missing commit/tree objects and shallow-history ambiguity fail closed with a
     precise reason.

When no artifact commit is pinned (a freshly-generated gate, e.g. L1-READY),
``artifact_commit=None`` reduces the check to "HEAD is a proper descendant of the
qualified source commit" — the strongest invariant derivable without an
externally-accepted artifact identity.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, ConfigDict

from . import git_tree as G

__all__ = ["AncestryResult", "verify_commit_ancestry"]


class AncestryResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    ok: bool
    checks: dict[str, bool]
    reasons: tuple[str, ...] = ()


def _shallow_suffix(root: Path) -> str:
    return " (shallow/incomplete history)" if G.is_shallow(root) else ""


def verify_commit_ancestry(
    root: Path,
    *,
    qualified_source: str,
    expected_tree: str,
    artifact_commit: str | None,
    head_ref: str = "HEAD",
) -> AncestryResult:
    """Verify the Git-ancestry invariants of a two-commit qualification."""
    root = root.resolve()
    checks: dict[str, bool] = {}
    reasons: list[str] = []

    if not G.is_git_repo(root):
        return AncestryResult(ok=False, checks={"is_git_repo": False}, reasons=("NOT_A_GIT_REPO",))

    # (1) qualified source commit exists
    src_present = G.object_exists(root, qualified_source)
    checks["qualified_source_present"] = src_present
    if not src_present:
        reasons.append(
            f"QUALIFIED_COMMIT_UNAVAILABLE: qualified source {qualified_source} "
            f"is absent locally{_shallow_suffix(root)}"
        )

    # (2) its tree equals the tree recorded by the gate
    tree_match = src_present and G.commit_tree_sha(root, qualified_source) == expected_tree
    checks["qualified_tree_matches"] = tree_match
    if src_present and not tree_match:
        reasons.append(
            "QUALIFIED_TREE_MISMATCH: qualified source commit tree does not equal the gate's tree"
        )

    head_resolved = G.object_exists(root, head_ref)
    checks["head_present"] = head_resolved
    if not head_resolved:
        reasons.append(f"HEAD_UNAVAILABLE: {head_ref} could not be resolved{_shallow_suffix(root)}")

    if artifact_commit is not None:
        # (3) accepted artifact commit exists and is a proper descendant of source
        art_present = G.object_exists(root, artifact_commit)
        checks["artifact_commit_present"] = art_present
        if not art_present:
            reasons.append(
                f"ARTIFACT_COMMIT_UNAVAILABLE: accepted artifact commit {artifact_commit} "
                f"is absent locally{_shallow_suffix(root)}"
            )
        proper = bool(
            src_present
            and art_present
            and G.is_ancestor(root, qualified_source, artifact_commit)
            and not G.is_ancestor(root, artifact_commit, qualified_source)
        )
        checks["commit_b_descends_a"] = proper
        if src_present and art_present and not proper:
            reasons.append(
                "ARTIFACT_NOT_PROPER_DESCENDANT_OF_SOURCE: accepted artifact commit does "
                "not properly descend from the qualified source commit"
            )
        # (4) HEAD descends from (or equals) the accepted artifact commit
        head_ok = bool(
            art_present and head_resolved and G.is_ancestor(root, artifact_commit, head_ref)
        )
        checks["head_descends_artifact"] = head_ok
        if art_present and head_resolved and not head_ok:
            reasons.append(
                "HEAD_NOT_DESCENDANT_OF_ARTIFACT: repository HEAD does not descend from the "
                "accepted artifact commit (divergent, sibling, or unrelated history)"
            )
    else:
        # No pinned artifact: HEAD must be a *proper* descendant of the source.
        proper_head = bool(
            src_present
            and head_resolved
            and G.is_ancestor(root, qualified_source, head_ref)
            and not G.is_ancestor(root, head_ref, qualified_source)
        )
        checks["commit_b_descends_a"] = proper_head
        if src_present and head_resolved and not proper_head:
            reasons.append(
                "HEAD_NOT_PROPER_DESCENDANT_OF_SOURCE: repository HEAD does not properly "
                "descend from the qualified source commit (divergent or unrelated history)"
            )

    return AncestryResult(
        ok=all(checks.values()),
        checks=checks,
        reasons=tuple(dict.fromkeys(reasons)),
    )
