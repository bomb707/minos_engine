"""Truth isolation: live engine source never *accesses* truth/locked-test data.

The scan looks at real code — import targets and non-docstring string literals —
not documentation prose (docstrings/comments legitimately describe the boundary,
e.g. "Layer 1 must never import hap.py").
"""

from __future__ import annotations

import ast

from tests.conftest import SRC

# Concrete data-file / identity tokens that would only appear in a real access.
_FORBIDDEN_STRING_TOKENS = (
    "truth.vcf",
    "mutations.vcf",
    ".sdf",
    "confident_regions",
    "hidden_score",
    "leaderboard",
    "final_test",
)

# Import targets that would indicate an evaluation/truth dependency.
_FORBIDDEN_IMPORT_TOKENS = (
    "truth",
    "mutation",
    "scoring",
    "happy",
    "hap_py",
    "evaluator",
    "evaluation",
)


def _docstring_nodes(tree: ast.AST) -> set[int]:
    ids: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            body = getattr(node, "body", [])
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)
            ):
                ids.add(id(body[0].value))
    return ids


def test_no_truth_data_access_in_source():
    # Scoped to the live/production packages. The offline Validator Twin
    # legitimately hosts hap.py/scoring adapters (Overall spec §6); production
    # importing the Twin is separately forbidden by a Twin architecture test.
    live_packages = ("protocol", "callers", "layer1", "layer2", "intake", "manifests", "common")
    offenders: dict[str, list[str]] = {}
    live_files = [f for pkg in live_packages for f in (SRC / pkg).rglob("*.py")]
    for path in live_files:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        docstrings = _docstring_nodes(tree)
        hits: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                if id(node) in docstrings:
                    continue
                low = node.value.lower()
                hits.extend(tok for tok in _FORBIDDEN_STRING_TOKENS if tok in low)
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    hits.extend(t for t in _FORBIDDEN_IMPORT_TOKENS if t in alias.name.lower())
            elif isinstance(node, ast.ImportFrom) and node.module:
                hits.extend(t for t in _FORBIDDEN_IMPORT_TOKENS if t in node.module.lower())
        if hits:
            offenders[str(path.relative_to(SRC))] = sorted(set(hits))
    assert offenders == {}, offenders


def test_truth_isolation_enabled_in_config():
    from minos_engine.settings import Settings

    assert Settings.load().engine.truth_isolation_enabled is True
