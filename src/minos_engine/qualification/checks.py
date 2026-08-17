"""In-process mandatory checks for the PROTOCOL-READY gate.

Subprocess-based checks (pytest, ruff, mypy, coverage) live in their own
modules; these are pure in-process predicates over the source tree and engine
state.
"""

from __future__ import annotations

import ast
from pathlib import Path

from minos_engine.callers.gatk.parameter_registry import REGISTRY
from minos_engine.common.errors import StageNotReadyError
from minos_engine.common.hashing import canonical_hash
from minos_engine.layer1.service import Layer1Service
from minos_engine.layer2.service import Layer2Service
from minos_engine.protocol.snapshot import build_snapshot
from minos_engine.settings import Settings

from .fixtures import payload_missing_identity, raw_response

__all__ = [
    "canonical_determinism",
    "gatk_only_policy",
    "gatk_registry_has_25_params",
    "required_identity_fails_closed",
    "layer1_not_implemented",
    "layer2_blocked",
    "no_truth_or_locked_test_access",
    "six_schemas_present",
    "documentation_complete",
    "ci_workflow_present",
    "specifications_present",
]

_REQUIRED_DOCS = (
    "docs/architecture/OVERVIEW.md",
    "docs/architecture/DEPENDENCY_RULES.md",
    "docs/contracts/PROTOCOL_CONTRACTS.md",
    "docs/contracts/GATK_CONFIG_CONTRACT.md",
    "docs/runbooks/PROTOCOL_SNAPSHOT.md",
    "docs/decisions/ADR-0001-SINGLE-CANONICAL-ENGINE.md",
    "docs/decisions/ADR-0002-GATK-ONLY.md",
    "docs/decisions/ADR-0003-TRUTH-ISOLATION.md",
    "docs/decisions/ADR-0004-STAGE-GATED-DEVELOPMENT.md",
    "README.md",
)

# Built by runtime concatenation so this scanner module does not contain the
# full token literals it searches for (which would otherwise flag itself and the
# truth-isolation leakage test).
_TRUTH_STRING_TOKENS = (
    "truth" + ".vcf",
    "mutations" + ".vcf",
    "hidden" + "_score",
    "leader" + "board",
    "final" + "_test",
)
_TRUTH_IMPORT_TOKENS = (
    "truth",
    "mutation",
    "scoring",
    "happy",
    "hap_py",
    "evaluator",
    "evaluation",
)

_REQUIRED_SPECS = (
    "MINOS_ENGINE_Overall_Exact_Build_Specification_v2.docx",
    "MINOS_ENGINE_Layer1_Exact_Build_Specification_v2.docx",
    "MINOS_ENGINE_Layer2_Exact_Build_Specification_v2.docx",
)


def canonical_determinism() -> bool:
    return canonical_hash({"b": 1, "a": 2}) == canonical_hash({"a": 2, "b": 1})


def gatk_only_policy(settings: Settings | None = None) -> bool:
    s = settings or Settings.load()
    return s.runtime_policy.active == "gatk" and s.runtime_policy.allowed == ("gatk",)


def gatk_registry_has_25_params() -> bool:
    return len(REGISTRY) == 25


def required_identity_fails_closed() -> bool:
    from minos_engine.common.errors import SnapshotIncompleteError

    try:
        build_snapshot(raw_response(payload_missing_identity("scorer_hash")))
    except SnapshotIncompleteError:
        return True
    return False


def layer1_not_implemented() -> bool:
    # Stage marker used by the accepted Stage 0/Stage 1 gates (frozen ``True`` at
    # their commits). Layer 1 is implemented at/after the Layer 1 stage, so the
    # live value is now ``False`` — the service exposes the real profiling path
    # (``profile``) instead of raising ``StageNotReadyError``. The accepted gates
    # are re-verified by re-hashing their committed source and are unaffected.
    return not hasattr(Layer1Service, "profile")


def layer2_blocked() -> bool:
    try:
        Layer2Service().select_config(None)  # type: ignore[arg-type]
    except StageNotReadyError:
        return True
    return False


def _docstring_ids(tree: ast.AST) -> set[int]:
    ids: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Module | ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            body = getattr(node, "body", [])
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)
            ):
                ids.add(id(body[0].value))
    return ids


# The live/production ("live image") packages. The Validator Twin, tool
# adapters, and qualification are the OFFLINE evaluation namespace (Overall spec
# §6: "twin/evaluation exists only in the offline image"); truth/hap.py/scoring
# adapters legitimately live there. The live-path truth scan is therefore scoped
# to these production packages; a separate Twin architecture test proves no
# production package imports the Twin/tools.
_LIVE_PACKAGES = ("protocol", "callers", "layer1", "layer2", "intake", "manifests", "common")


def no_truth_or_locked_test_access(src_dir: Path) -> bool:
    paths: list[Path] = []
    for pkg in _LIVE_PACKAGES:
        paths.extend((src_dir / pkg).rglob("*.py"))
    for path in paths:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        docstrings = _docstring_ids(tree)
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                if id(node) in docstrings:
                    continue
                low = node.value.lower()
                if any(tok in low for tok in _TRUTH_STRING_TOKENS):
                    return False
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if any(t in alias.name.lower() for t in _TRUTH_IMPORT_TOKENS):
                        return False
            elif isinstance(node, ast.ImportFrom) and node.module:
                if any(t in node.module.lower() for t in _TRUTH_IMPORT_TOKENS):
                    return False
    return True


_STAGE0_SCHEMAS = (
    "artifact-identity-v1.schema.json",
    "round-protocol-snapshot-v1.schema.json",
    "round-context-v1.schema.json",
    "parameter-space-snapshot-v1.schema.json",
    "gate-artifact-v1.schema.json",
    "release-manifest-v1.schema.json",
)


def six_schemas_present(root: Path) -> bool:
    # The six required Stage 0 schemas are present. Later stages may add more
    # schemas (e.g. the Twin's), so this checks the required set by name rather
    # than an exact total count.
    return all((root / "schemas" / name).exists() for name in _STAGE0_SCHEMAS)


def documentation_complete(root: Path) -> bool:
    return all((root / rel).exists() for rel in _REQUIRED_DOCS)


def ci_workflow_present(root: Path) -> bool:
    return (root / ".github" / "workflows" / "ci.yml").exists()


def specifications_present(root: Path) -> bool:
    spec_dir = root / "docs" / "specifications"
    manifest = spec_dir / "SPECIFICATION_MANIFEST.json"
    return manifest.exists() and all((spec_dir / name).exists() for name in _REQUIRED_SPECS)
