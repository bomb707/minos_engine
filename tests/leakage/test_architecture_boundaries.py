"""Architecture / import-boundary guards enforced statically over the source tree."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from tests.conftest import SRC


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    mods: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            mods.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            mods.add(node.module)
    return mods


def _package_files(package: str) -> list[Path]:
    return sorted((SRC / package).rglob("*.py"))


def _all_source_files() -> list[Path]:
    return sorted(SRC.rglob("*.py"))


def _violations(files: list[Path], forbidden: tuple[str, ...]) -> dict[str, set[str]]:
    out: dict[str, set[str]] = {}
    for f in files:
        bad = {m for m in _imports(f) for tok in forbidden if tok in m}
        if bad:
            out[str(f.relative_to(SRC))] = bad
    return out


def test_layer1_cannot_import_eval_truth_or_layer2():
    forbidden = (
        "layer2",
        "twin",
        "evaluation",
        "truth",
        "mutation",
        "happy",
        "scoring",
        "retrieval",
    )
    assert _violations(_package_files("layer1"), forbidden) == {}


def test_layer2_cannot_import_bam_readers_or_intake():
    forbidden = ("pysam", "minos_engine.intake")
    assert _violations(_package_files("layer2"), forbidden) == {}


def test_layer2_forbidden_imports_expanded():
    # Layer 2 (L2-A) may consume only typed Layer 1 *contracts*, gate/qualification
    # verification, and common utilities. It must not touch BAM/BAI readers, Layer 1
    # file-opening logic, intake, truth/eval/scoring/mutation packages, or any
    # database/network dependency (no PostgreSQL/SQLAlchemy/Alembic in L2-A).
    forbidden = (
        "pysam",
        "minos_engine.intake",
        "layer1.adapters",
        "layer1.pysam_adapter",
        "layer1.scan",
        "layer1.pileup",
        "layer1.coverage",
        "layer1.reference_profile",
        "layer1.orchestrator",
        "layer1.sampling",
        "layer1.serializer",
        "layer1.service",
        "happy",
        "hap_py",
        "scoring",
        "evaluator",
        "mutation",
        "retrieval",
        "sqlalchemy",
        "alembic",
        "psycopg",
        "asyncpg",
        "requests",
        "httpx",
        "urllib",
    )
    assert _violations(_package_files("layer2"), forbidden) == {}


def test_layer2_domain_modules_do_not_open_files_or_parse_env():
    # Pure Layer 2 domain modules (contracts, feature registry, service) must not
    # open files or read the environment. The entry gate is the artifact verifier
    # and legitimately reads the gate/report it is asked to verify.
    domain = [
        SRC / "layer2" / "contracts.py",
        SRC / "layer2" / "feature_registry.py",
        SRC / "layer2" / "service.py",
        SRC / "layer2" / "prerequisites.py",
    ]
    banned = ("open(", "read_text", "read_bytes", "os.environ", "os.getenv", "getenv(")
    for f in domain:
        text = f.read_text(encoding="utf-8")
        for token in banned:
            assert token not in text, f"{f} uses {token}"


def test_layer2_never_parses_environment():
    for f in _package_files("layer2"):
        text = f.read_text(encoding="utf-8")
        assert "os.environ" not in text and "os.getenv" not in text, f


def test_layer2_service_remains_blocked():
    import pytest

    from minos_engine.common.errors import StageNotReadyError
    from minos_engine.layer2.service import Layer2Service

    with pytest.raises(StageNotReadyError):
        Layer2Service().select_config(None)  # type: ignore[arg-type]


def test_layer2_consumes_only_typed_layer1_contracts():
    # If any layer2 file imports from minos_engine.layer1, it must be the contracts
    # module only (typed profile types), never the profiler/file-opening internals.
    for f in _package_files("layer2"):
        for mod in _imports(f):
            if mod.startswith("minos_engine.layer1"):
                assert mod == "minos_engine.layer1.contracts", f"{f} imports {mod}"


def test_live_package_cannot_import_evaluator_happy_or_scoring():
    # Scoped to the live/production packages. The Validator Twin + tool adapters
    # are the OFFLINE evaluation namespace (Overall spec §6) and legitimately
    # include hap.py/scoring adapters; a Twin architecture test proves production
    # never imports the Twin/tools.
    live_packages = ("protocol", "callers", "layer1", "layer2", "intake", "manifests", "common")
    live_files = [f for pkg in live_packages for f in (SRC / pkg).rglob("*.py")]
    forbidden = ("happy", "hap_py", "scoring", "evaluator", "genomics_config")
    assert _violations(live_files, forbidden) == {}


def test_production_code_never_imports_tests():
    # Production qualification/engine code must not depend on test helpers.
    offenders: dict[str, set[str]] = {}
    for f in _all_source_files():
        bad = {m for m in _imports(f) if m == "tests" or m.startswith("tests.") or m == "conftest"}
        if bad:
            offenders[str(f.relative_to(SRC))] = bad
    assert offenders == {}, offenders


def test_domain_modules_do_not_parse_cli_args():
    # Only cli/* may import argparse (Overall spec §6: domain has no argparse).
    for f in _all_source_files():
        if f.parent.name == "cli":
            continue
        assert "argparse" not in _imports(f), f"{f} imports argparse outside cli/"


def test_single_config_emission_interface():
    # Exactly one production CONFIG-emission entry point (Layer2Service.select_config).
    definers = []
    for f in _all_source_files():
        tree = ast.parse(f.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "select_config":
                definers.append(str(f.relative_to(SRC)))
    assert definers == ["layer2/service.py"], definers


@pytest.mark.parametrize("caller", ["deepvariant", "bcftools", "freebayes"])
def test_disabled_callers_cannot_be_active(caller):
    from minos_engine.common.errors import PolicyViolationError
    from minos_engine.settings import RuntimePolicy

    with pytest.raises(PolicyViolationError):
        RuntimePolicy(active=caller)


def test_submission_and_parameter_space_reject_non_gatk():
    from pydantic import ValidationError

    from minos_engine.callers.contracts import ParameterSpaceSnapshot
    from minos_engine.protocol.submission_contract import SubmissionEnvelope

    with pytest.raises(ValidationError):
        SubmissionEnvelope(tool="deepvariant", version="1", gatk_options={})
    with pytest.raises(ValidationError):
        ParameterSpaceSnapshot(
            caller="bcftools",
            parameters={},
            source="x",
            retrieved_at="2026-08-17T00:00:00+00:00",
            parameter_space_hash="a" * 64,
            stale=False,
        )


# --------------------------------------------------------------------------- #
# experiments package: pure domain only (storage -> experiments, never the reverse)
# --------------------------------------------------------------------------- #
def _called_names(path: Path) -> set[str]:
    """Every callee name in the module (bare ``f()`` and attribute ``x.f()`` forms)."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name):
                names.add(func.id)
            elif isinstance(func, ast.Attribute):
                names.add(func.attr)
    return names


def _attribute_names(path: Path) -> set[str]:
    """Every attribute accessed in the module (e.g. the ``environ`` of ``os.environ``)."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}


def _repo_rooted_constants(tree: ast.Module) -> set[str]:
    """Module-level constants transitively derived from ``Path(__file__)``.

    A path built from ``__file__`` is a committed, repository-owned location — it can never be
    redirected by the environment, a caller argument or runtime state.
    """
    rooted: set[str] = set()
    for _ in range(4):  # transitive closure (_REPO_ROOT -> _MANIFEST -> ...)
        for node in tree.body:
            if not isinstance(node, ast.Assign):
                continue
            names = {n.id for n in node.targets if isinstance(n, ast.Name)}
            refs = {n.id for n in ast.walk(node.value) if isinstance(n, ast.Name)}
            uses_file = any(
                isinstance(n, ast.Name) and n.id == "__file__" for n in ast.walk(node.value)
            )
            if uses_file or (refs & rooted):
                rooted |= names
    return rooted


def test_experiments_package_cannot_import_storage_or_database_drivers():
    """The experiments package is a PURE domain layer: it defines the plan/candidate contracts
    that the storage layer consumes. It must never import the storage layer or any database
    driver, or the dependency direction would invert (storage -> experiments only)."""
    forbidden = ("sqlalchemy", "alembic", "psycopg", "minos_engine.storage")
    assert _violations(_package_files("experiments"), forbidden) == {}


def test_experiments_package_performs_no_db_env_or_write_operations():
    """AST guard: no engine creation, SQL text construction, environment parsing, raw ``open()``
    or any write anywhere in the experiments package."""
    banned_calls = {
        "create_db_engine",
        "create_engine",
        "text",  # SQL text construction
        "getenv",
        "open",
        "write_bytes",
        "write_text",
        "mkdir",
        "unlink",
    }
    banned_attributes = {"environ"}
    offenders: dict[str, set[str]] = {}
    for f in _package_files("experiments"):
        bad = (_called_names(f) & banned_calls) | (_attribute_names(f) & banned_attributes)
        if bad:
            offenders[str(f.relative_to(SRC))] = bad
    assert offenders == {}


def test_experiments_reads_only_committed_repo_rooted_artifacts():
    """The only filesystem reads permitted in the domain package are the accepted constructor's
    reads of COMMITTED repository evidence (the epoch-1 member manifest and the E4 train report).
    Each such read must target a module-level constant transitively rooted at ``Path(__file__)``
    — never an environment value, caller argument or runtime-derived path."""
    readers = {"read_text", "read_bytes"}
    offenders: dict[str, set[str]] = {}
    for f in _package_files("experiments"):
        tree = ast.parse(f.read_text(encoding="utf-8"))
        rooted = _repo_rooted_constants(tree)
        bad: set[str] = set()
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in readers
            ):
                receiver = node.func.value
                if not (isinstance(receiver, ast.Name) and receiver.id in rooted):
                    bad.add(ast.unparse(node.func))
        if bad:
            offenders[str(f.relative_to(SRC))] = bad
    assert offenders == {}


def test_dependency_direction_is_storage_to_experiments_only():
    """The one-way dependency must hold in BOTH directions: storage genuinely consumes the pure
    experiments contracts, and no experiments module imports storage."""
    storage_to_experiments = {
        str(f.relative_to(SRC))
        for f in _package_files("storage")
        for mod in _imports(f)
        if mod.startswith("minos_engine.experiments")
    }
    assert storage_to_experiments, "expected storage to consume the pure experiments contracts"

    experiments_to_storage = {
        str(f.relative_to(SRC)): {
            mod for mod in _imports(f) if mod.startswith("minos_engine.storage")
        }
        for f in _package_files("experiments")
    }
    assert {k: v for k, v in experiments_to_storage.items() if v} == {}
