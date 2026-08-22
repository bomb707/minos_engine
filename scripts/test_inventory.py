"""TEST-CI-1 test inventory and redundancy analysis (read-only).

Classifies every ``test_*.py`` file into an execution tier, records what each protects, and
detects redundancy by AST and reference analysis — never by filename similarity.

Subcommands:

``inventory``
    Emit ``reports/testing/MINOS_TEST_INVENTORY.json``: one record per test file plus the
    redundancy findings and the tier totals.

``duplicates``
    Print exact duplicate test bodies (normalized AST dumps), grouped.

``unused``
    Print fixtures and helper modules that no test references.

``verify``
    Re-derive the inventory from the working tree and require it to match the committed report,
    so the report cannot silently drift from the suite it describes.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import re
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
TESTS = REPO_ROOT / "tests"
INVENTORY_PATH = REPO_ROOT / "reports" / "testing" / "MINOS_TEST_INVENTORY.json"

TIERS = ("fast", "full", "manual_privileged", "retire_after_dbv2")
DECISIONS = ("keep", "merge", "remove", "manual")

#: directories whose tests need a real PostgreSQL server (service container or bundled pgserver).
_PG_DIRS = ("tests/integration/",)
#: import names that imply a PostgreSQL requirement even outside those directories.
_PG_IMPORTS = ("sqlalchemy", "psycopg", "pgserver", "alembic")
#: markers that imply the file cannot run on an ordinary hosted runner.
_PRIVILEGED_HINTS = ("os.geteuid() == 0", "requires root", "privileged", "setcap", "chown(")


def _no_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    seen: dict[str, Any] = {}
    for key, value in pairs:
        if key in seen:
            raise ValueError(f"duplicate JSON key {key!r}")
        seen[key] = value
    return seen


def load_strict(path: Path) -> Any:
    """Parse JSON, rejecting duplicate keys anywhere in the document."""
    return json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_no_duplicate_keys)


def _category(rel: str) -> str:
    parts = rel.split("/")
    if parts[1] == "unit":
        return "unit"
    if parts[1] == "leakage":
        return "leakage"
    if parts[1] == "component":
        return "component"
    if parts[1] == "acceptance":
        return "acceptance"
    if parts[1] == "determinism":
        return "determinism"
    if parts[1] == "contracts":
        return "protocol-contract"
    if parts[1] == "integration":
        return "integration"
    return parts[1] if len(parts) > 1 else "other"


def _module_source_targets(tree: ast.Module) -> list[str]:
    out: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            if node.module.startswith("minos_engine"):
                out.add(node.module)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith("minos_engine"):
                    out.add(alias.name)
    return sorted(out)


def _test_functions(tree: ast.Module) -> list[ast.FunctionDef | ast.AsyncFunctionDef]:
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
        and node.name.startswith("test_")
    ]


def _normalized_body(node: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    """AST dump of a test body with the docstring stripped, for exact-duplicate detection."""
    body = list(node.body)
    if (
        body
        and isinstance(body[0], ast.Expr)
        and isinstance(body[0].value, ast.Constant)
        and isinstance(body[0].value.value, str)
    ):
        body = body[1:]
    if not body:
        return ""
    module = ast.Module(body=body, type_ignores=[])
    return ast.dump(module, annotate_fields=False, include_attributes=False)


def _fixtures(tree: ast.Module) -> list[str]:
    out: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        for deco in node.decorator_list:
            text = ast.dump(deco)
            if "fixture" in text:
                out.append(node.name)
                break
    return sorted(set(out))


def _requested_names(tree: ast.Module) -> set[str]:
    """Every name a test file could be requesting: parameters, attributes and plain names."""
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            names.update(a.arg for a in node.args.args)
            names.update(a.arg for a in node.args.kwonlyargs)
        elif isinstance(node, ast.Name):
            names.add(node.id)
        elif isinstance(node, ast.Attribute):
            names.add(node.attr)
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            names.add(node.value)
    return names


#: Explicit per-file decisions. Every non-"keep" entry names the proof and the replacement, so the
#: emitted report is reproducible rather than hand-edited after generation.
ANNOTATIONS: dict[str, tuple[str, str, str | None]] = {
    "tests/acceptance/test_stage_gates.py": (
        "keep",
        "Canonical home of the Layer2Service.select_config blocked proof; six byte-identical "
        "copies elsewhere were consolidated here.",
        None,
    ),
    "tests/integration/layer2_features/test_builder.py": (
        "merge",
        "Byte-identical select_config-blocked body removed: same assertion, same public boundary.",
        "tests/acceptance/test_stage_gates.py::test_layer2_blocked",
    ),
    "tests/unit/layer2/features/test_contracts.py": (
        "merge",
        "Byte-identical select_config-blocked body removed.",
        "tests/acceptance/test_stage_gates.py::test_layer2_blocked",
    ),
    "tests/unit/layer2/features/test_extraction.py": (
        "merge",
        "Byte-identical select_config-blocked body removed.",
        "tests/acceptance/test_stage_gates.py::test_layer2_blocked",
    ),
    "tests/unit/experiments/test_accepted_plan.py": (
        "merge",
        "Byte-identical select_config-blocked body removed; the E4 attack matrix is untouched.",
        "tests/acceptance/test_stage_gates.py::test_layer2_blocked",
    ),
    "tests/unit/layer2/test_e4_production_contract.py": (
        "merge",
        "Byte-identical select_config-blocked body removed.",
        "tests/acceptance/test_stage_gates.py::test_layer2_blocked",
    ),
    "tests/acceptance/layer1/test_l1_ready.py": (
        "merge",
        "test_layer2_service_still_blocked had a byte-identical body to test_layer2_blocked.",
        "tests/acceptance/test_stage_gates.py::test_layer2_blocked",
    ),
    "tests/unit/layer2/test_operational_db_identity.py": (
        "merge",
        "test_missing_database_url_still_fails_closed asserted the same behaviour through the "
        "same boundary as test_storage_config.py::test_missing_env_fails_closed.",
        "tests/unit/layer2/test_storage_config.py::test_missing_env_fails_closed",
    ),
    "tests/integration/layer2_db/test_stepwise_migration_chain.py": (
        "keep",
        "Replaces eight inline workflow steps that ran alembic downgrade plus a Python heredoc "
        "embedded in YAML; executed exactly once by the full tier.",
        None,
    ),
    "tests/unit/tools/test_local_qualification.py": (
        "keep",
        "Proves local full qualification refuses the operational store before any tool starts, "
        "and that its plan schedules the full suite exactly once.",
        None,
    ),
    "tests/unit/tools/test_workflow_policy.py": (
        "keep",
        "Evaluates the fast-tier job condition against synthetic GitHub event payloads, so the "
        "one-run-per-commit policy is proven rather than grepped.",
        None,
    ),
    "tests/unit/tools/test_dbv2_audit.py": (
        "keep",
        "Retained specifically for DB-V2: guards the design-report validator and contract hash.",
        None,
    ),
    "tests/component/test_qualification_runner.py": (
        "keep",
        "Shares a body with the twin runner test but drives a DIFFERENT production _assemble; "
        "distinct runtime boundaries need separate proof.",
        None,
    ),
    "tests/component/twin/test_twin_qualification.py": (
        "keep",
        "Shares a body with the protocol runner test but drives a DIFFERENT production "
        "_assemble; distinct runtime boundaries need separate proof.",
        None,
    ),
}

#: What each area protects, recorded so a future reader knows what a removal would cost.
CONTRACTS: dict[str, str] = {
    "tests/leakage": "truth/scoring leakage isolation and architecture dependency boundaries",
    "tests/integration/layer2_db": (
        "PostgreSQL role separation, migration lifecycle, concurrency, commit ambiguity and "
        "descriptor-bound filesystem safety"
    ),
    "tests/unit/storage/test_l2f_execution_security.py": (
        "argv and environment injection resistance, workspace inode safety"
    ),
    "tests/acceptance": "accepted stage-gate posture and git-bound evidence ancestry",
    "tests/determinism": "deterministic scientific identity",
    "tests/protocol_contract": "protocol contract stability",
    "tests/unit/tools": "DB-V2 design-report integrity, CI workflow trigger policy and local-qualification safety",
}

TIER_COMMANDS: dict[str, str] = {
    "fast": "pytest tests/unit tests/leakage tests/determinism tests/protocol_contract",
    "full": (
        "pytest --junitxml=reports/ci-junit.xml --cov=src/minos_engine --cov-fail-under=90 "
        "--cov-report=term-missing --cov-report=xml:reports/ci-coverage.xml"
    ),
    "manual_privileged": "sudo -E pytest -m privileged  # see docs/testing/TEST_STRATEGY.md",
}


def _contract_for(rel: str) -> str:
    for prefix, text in CONTRACTS.items():
        if rel.startswith(prefix):
            return text
    return "module behaviour"


def _tier_for(rel: str, category: str, needs_pg: bool, privileged: bool) -> str:
    if privileged:
        return "manual_privileged"
    if category in {"unit", "leakage", "determinism", "protocol-contract"} and not needs_pg:
        return "fast"
    return "full"


def scan_file(path: Path) -> dict[str, Any]:
    rel = path.relative_to(REPO_ROOT).as_posix()
    text = path.read_text(encoding="utf-8")
    tree = ast.parse(text)
    functions = _test_functions(tree)
    category = _category(rel)
    imports = _module_source_targets(tree)
    raw_imports = {
        alias.name.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    } | {
        node.module.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
    }
    needs_pg = rel.startswith(_PG_DIRS) or bool(raw_imports & set(_PG_IMPORTS))
    needs_fs = ("tmp_path" in text) or (".write_bytes(" in text) or (".mkdir(" in text)
    needs_posix = ("os.mkfifo" in text) or ("/bin/sh" in text) or ("os.killpg" in text)
    privileged = any(hint in text for hint in _PRIVILEGED_HINTS)
    needs_gate = ("gates/" in text) or ("require-pass" in text) or ("git_bound" in text)
    tier = _tier_for(rel, category, needs_pg, privileged)
    decision, reason, replacement = ANNOTATIONS.get(rel, ("keep", "", None))
    return {
        "path": rel,
        "category": category,
        "test_count": len(functions),
        "line_count": text.count("\n") + 1,
        "dependencies": sorted(raw_imports - {"minos_engine", "tests", "__future__"}),
        "requires_postgres": needs_pg,
        "requires_filesystem": needs_fs,
        "requires_posix": needs_posix,
        "requires_privileged": privileged,
        "requires_gate_evidence": needs_gate,
        "source_modules_covered": imports,
        "contract_protected": _contract_for(rel),
        "recommended_tier": tier,
        "decision": decision,
        "reason": reason,
        "replacement": replacement,
        "_functions": {fn.name: _normalized_body(fn) for fn in functions},
        "_fixtures": _fixtures(tree),
    }


def find_exact_duplicates(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Group test functions whose normalized bodies are byte-identical."""
    by_body: dict[str, list[str]] = defaultdict(list)
    for record in records:
        for name, body in record["_functions"].items():
            if not body:
                continue
            by_body[body].append(f"{record['path']}::{name}")
    groups = []
    for body, members in sorted(by_body.items(), key=lambda kv: -len(kv[1])):
        if len(members) < 2:
            continue
        groups.append(
            {
                "occurrences": len(members),
                "members": sorted(members),
                "distinct_names": sorted({m.rsplit("::", 1)[1] for m in members}),
                # sha256 of the normalized AST dump: stable across processes and machines,
                # unlike Python's per-process randomized str hash().
                "body_digest": hashlib.sha256(body.encode("utf-8")).hexdigest()[:16],
            }
        )
    return groups


def find_unused_fixtures(records: list[dict[str, Any]], conftests: list[Path]) -> list[str]:
    """Fixtures defined in a conftest that no test file or conftest ever requests."""
    requested: set[str] = set()
    for path in sorted(TESTS.rglob("*.py")):
        requested |= _requested_names(ast.parse(path.read_text(encoding="utf-8")))
    unused: list[str] = []
    for conftest in conftests:
        tree = ast.parse(conftest.read_text(encoding="utf-8"))
        for name in _fixtures(tree):
            if name not in requested:
                unused.append(f"{conftest.relative_to(REPO_ROOT).as_posix()}::{name}")
    return sorted(unused)


def find_unused_helpers() -> list[str]:
    """Non-test modules under tests/ that no other module imports."""
    helpers = [
        p
        for p in sorted(TESTS.rglob("*.py"))
        if not p.name.startswith("test_") and p.name not in {"conftest.py", "__init__.py"}
    ]
    corpus = "\n".join(
        p.read_text(encoding="utf-8")
        for p in list(TESTS.rglob("*.py")) + list((REPO_ROOT / "src").rglob("*.py"))
    )
    unused = []
    for helper in helpers:
        stem = helper.stem
        # a helper is used if its module name appears in any import statement anywhere
        if not re.search(rf"\b(import|from)\s+[\w.]*\b{re.escape(stem)}\b", corpus):
            unused.append(helper.relative_to(REPO_ROOT).as_posix())
    return unused


def find_permanent_skips() -> list[str]:
    """Modules skipped unconditionally at import time."""
    out = []
    for path in sorted(TESTS.rglob("test_*.py")):
        text = path.read_text(encoding="utf-8")
        if re.search(r"^pytestmark\s*=\s*pytest\.mark\.skip\(", text, re.M):
            out.append(path.relative_to(REPO_ROOT).as_posix())
    return out


def build(records_only: bool = False) -> dict[str, Any]:
    files = sorted(TESTS.rglob("test_*.py"))
    records = [scan_file(p) for p in files]
    conftests = sorted(TESTS.rglob("conftest.py"))
    duplicates = find_exact_duplicates(records)
    payload: dict[str, Any] = {
        "report": "MINOS_TEST_INVENTORY",
        "schema_version": "minos-test-inventory-v1",
        "totals": {
            "test_files": len(records),
            "test_functions": sum(r["test_count"] for r in records),
            "test_lines": sum(r["line_count"] for r in records),
            "conftest_files": len(conftests),
            "fixtures": sum(
                len(_fixtures(ast.parse(c.read_text(encoding="utf-8")))) for c in conftests
            ),
        },
        "tier_totals": {
            tier: {
                "files": sum(1 for r in records if r["recommended_tier"] == tier),
                "tests": sum(r["test_count"] for r in records if r["recommended_tier"] == tier),
            }
            for tier in TIERS
        },
        "redundancy": {
            "exact_duplicate_groups": duplicates,
            "exact_duplicate_functions": sum(g["occurrences"] for g in duplicates),
            "unused_fixtures": find_unused_fixtures(records, conftests),
            "unused_helper_modules": find_unused_helpers(),
            "permanently_skipped_modules": find_permanent_skips(),
        },
        "tier_commands": TIER_COMMANDS,
        "removals": {
            "test_functions_removed": 7,
            "fixtures_removed": 2,
            "files_removed": 0,
            "detail": [
                "6 x byte-identical Layer2Service.select_config blocked assertion (consolidated)",
                "1 x byte-identical database_url() missing-env assertion (consolidated)",
                "2 x tests/conftest.py fixtures no test or conftest ever requested",
            ],
        },
        "files": records,
    }
    if not records_only:
        for record in payload["files"]:
            record.pop("_functions", None)
            record.pop("_fixtures", None)
    return payload


#: Fields excluded from record equality. Empty on purpose: the report carries no timestamp, no
#: absolute path and no nondeterministic value, so a fresh build must reproduce it exactly.
PROVENANCE_ONLY_FIELDS: frozenset[str] = frozenset()


def verify_inventory(path: Path = INVENTORY_PATH) -> list[str]:
    """Require the committed report to equal a fresh ``build()`` semantically, record by record.

    Totals and path sets are not enough: swapping two records' counts, retiering one file or
    editing a decision all leave the totals identical. Every committed field is therefore
    compared against the freshly derived one. ``verify`` never writes the report.
    """
    problems: list[str] = []
    if not path.is_file():
        return [f"{path} is missing"]
    try:
        committed = load_strict(path)
    except ValueError as exc:
        return [f"{path.name}: {exc}"]

    current = build()

    if committed.get("schema_version") != current["schema_version"]:
        problems.append(
            f"schema_version drifted: {committed.get('schema_version')!r} != "
            f"{current['schema_version']!r}"
        )
    for section in ("totals", "tier_totals", "tier_commands", "removals", "redundancy"):
        if committed.get(section) != current[section]:
            problems.append(f"{section} drifted from a fresh build")

    committed_by_path = {r["path"]: r for r in committed.get("files", [])}
    current_by_path = {r["path"]: r for r in current["files"]}

    for missing in sorted(current_by_path.keys() - committed_by_path.keys()):
        problems.append(f"test file not in the inventory: {missing}")
    for stale in sorted(committed_by_path.keys() - current_by_path.keys()):
        problems.append(f"inventory lists a file that no longer exists: {stale}")

    for rel in sorted(committed_by_path.keys() & current_by_path.keys()):
        want, got = current_by_path[rel], committed_by_path[rel]
        for field in sorted(set(want) | set(got)):
            if field in PROVENANCE_ONLY_FIELDS:
                continue
            if want.get(field) != got.get(field):
                problems.append(
                    f"{rel}: {field} drifted (committed {got.get(field)!r} != "
                    f"derived {want.get(field)!r})"
                )

    for record in committed.get("files", []):
        if record.get("recommended_tier") not in TIERS:
            problems.append(f"{record['path']}: unknown tier {record.get('recommended_tier')!r}")
        if record.get("decision") not in DECISIONS:
            problems.append(f"{record['path']}: unknown decision {record.get('decision')!r}")
        if record.get("decision") != "keep" and not record.get("reason"):
            problems.append(f"{record['path']}: {record['decision']} without a reason")
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="TEST-CI-1 inventory and redundancy analysis")
    sub = parser.add_subparsers(dest="command", required=True)
    inv = sub.add_parser("inventory")
    inv.add_argument("--out", type=Path, default=INVENTORY_PATH)
    sub.add_parser("duplicates")
    sub.add_parser("unused")
    sub.add_parser("verify")
    args = parser.parse_args(argv)

    if args.command == "inventory":
        payload = build()
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_bytes(
            json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False).encode("utf-8")
            + b"\n"
        )
        t = payload["totals"]
        print(
            f"{t['test_files']} files, {t['test_functions']} test functions, "
            f"{t['test_lines']} lines -> {args.out}"
        )
        return 0

    if args.command == "duplicates":
        payload = build(records_only=True)
        for group in payload["redundancy"]["exact_duplicate_groups"]:
            print(f"x{group['occurrences']}  {group['distinct_names']}")
            for member in group["members"]:
                print(f"       {member}")
        return 0

    if args.command == "unused":
        payload = build(records_only=True)
        red = payload["redundancy"]
        print("unused fixtures:", red["unused_fixtures"] or "none")
        print("unused helper modules:", red["unused_helper_modules"] or "none")
        print("permanently skipped:", red["permanently_skipped_modules"] or "none")
        return 0

    # verify
    problems = verify_inventory()
    for problem in problems:
        print(f"FAIL: {problem}", file=sys.stderr)
    print(f"verify: {len(problems)} problem(s)")
    return 1 if problems else 0


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    raise SystemExit(main())
