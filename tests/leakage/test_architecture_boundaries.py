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


# --------------------------------------------------------------------------- #
# F5 execution boundary: GATK only, no truth/scoring, no shell, no legacy tables
# --------------------------------------------------------------------------- #
_F5_MODULES = (
    "storage/l2f_execution.py",
    "storage/l2f_execution_inputs.py",
    "storage/l2f_gatk_runner.py",
    "storage/l2f_result_publisher.py",
    "storage/l2f_execution_config.py",
    "storage/l2f_execution_contract.py",
    "storage/l2f_execution_tables.py",
    "experiments/execution_contract.py",
)


def _code_strings(path: Path) -> set[str]:
    """Every string literal that is NOT a docstring, lowercased.

    F5 docstrings legitimately NAME the things F5 must never touch ("never reads truth...",
    "never touches profiling.profiles"), so a source-text scan would flag its own documentation.
    Only real code literals are inspected.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    docstrings: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            body = getattr(node, "body", [])
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)
            ):
                docstrings.add(id(body[0].value))
    out: set[str] = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and id(node) not in docstrings
        ):
            out.add(node.value.lower())
    return out


def _f5_files() -> list[Path]:
    return [SRC / rel for rel in _F5_MODULES]


def test_f5_modules_all_exist() -> None:
    for path in _f5_files():
        assert path.is_file(), path


def test_f5_never_references_truth_mutation_scoring_or_labels() -> None:
    """F5 executes GATK and records bytes; it never sees evaluation data."""
    banned = (
        "truth",
        "mutation",
        "mutated",
        "happy",
        "hap.py",
        "tp_count",
        "fp_count",
        "fn_count",
        "precision",
        "recall",
        "f1_score",
        "leaderboard",
        "label",
    )
    offenders: dict[str, set[str]] = {}
    for path in _f5_files():
        haystack = " ".join(_code_strings(path)) + " " + " ".join(n.lower() for n in _imports(path))
        hits = {token for token in banned if token in haystack}
        if hits:
            offenders[str(path.relative_to(SRC))] = hits
    assert offenders == {}


def test_f5_never_imports_or_executes_other_callers() -> None:
    for path in _f5_files():
        for mod in _imports(path):
            assert "deepvariant" not in mod.lower(), (path, mod)
            assert "bcftools" not in mod.lower(), (path, mod)
        literals = " ".join(_code_strings(path))
        assert "deepvariant" not in literals, path
        assert "bcftools" not in literals, path


def test_f5_never_invokes_a_shell() -> None:
    """No shell=True, os.system, popen or shell-string construction anywhere in F5."""
    for path in _f5_files():
        names = _called_names(path)
        assert "system" not in names, path
        assert "popen" not in names, path
        assert "eval" not in names and "exec" not in names, path
        body = path.read_text(encoding="utf-8")
        assert "shell=True" not in body, path
        assert "os.system" not in body, path


def test_f5_never_touches_legacy_experiment_tables() -> None:
    legacy = (
        "profiling.profiles",
        "experiments.jobs",
        "experiments.results",
        "catalog.gatk_configs",
    )
    for path in _f5_files():
        literals = " ".join(_code_strings(path))
        for table in legacy:
            assert table not in literals, (path, table)


def test_f5_production_entry_point_takes_only_a_worker_id() -> None:
    """No caller-provided plan, hashes, database, paths, CONFIG, runner or trust bundle."""
    import inspect

    from minos_engine.storage.l2f_execution import execute_next_accepted_job

    sig = inspect.signature(execute_next_accepted_job)
    assert set(sig.parameters) == {"worker_id"}
    assert sig.parameters["worker_id"].kind is inspect.Parameter.KEYWORD_ONLY
    assert sig.parameters["worker_id"].default is inspect.Parameter.empty


def test_f5_trust_boundary_and_fake_runner_are_private() -> None:
    from minos_engine.storage import l2f_execution as EX

    assert "execute_next_accepted_job" in EX.__all__
    assert "_execute_next_job_with_trust" not in EX.__all__
    assert "FakeGatkRunner" not in EX.__all__


def test_f5_result_identity_excludes_raw_process_streams() -> None:
    """Raw stdout/stderr bytes never enter any scientific identity — only a bounded digest."""
    from minos_engine.experiments.execution_contract import ExecutionResultManifest

    fields = set(ExecutionResultManifest.model_fields)
    assert "stderr" not in fields and "stdout" not in fields
    assert not any("stderr" in f or "stdout" in f for f in fields)


# --------------------------------------------------------------------------- #
# F6 — leakage boundary: execution and verification never consume evaluation data
# --------------------------------------------------------------------------- #
#: every evaluation-only concept F5/F6 must never resolve, open, select or emit.
#: Each token is PRECISE on purpose. Broad substrings like "score" or "f1" would collide with
#: legitimate GATK parameter names (``min_base_quality_score``) and with hex digests, producing
#: false positives that would have to be excepted away - which would weaken the guard.
_F6_FORBIDDEN_CONCEPTS = (
    "truth",
    "hap.py",
    "hap_py",
    "happy_",
    "mutation_manifest",
    "mutations",
    "tp_count",
    "fp_count",
    "fn_count",
    "true_positive",
    "false_positive",
    "false_negative",
    "precision",
    "recall",
    "scoring",
    "leaderboard",
    "training_target",
    "train_label",
    "ground_truth",
)

#: the F6 surface = the F5 modules plus the harness verifier.
_F6_MODULES = (*_F5_MODULES, "storage/l2f_harness_verifier.py")


def _f6_files() -> list[Path]:
    return [SRC / rel for rel in _F6_MODULES]


def test_f6_code_literals_never_name_evaluation_concepts() -> None:
    """AST literal scan (docstrings excluded): no evaluation concept is ever addressed in code."""
    from minos_engine.storage.l2f_harness_verifier import _FORBIDDEN_ARTIFACT_TOKENS

    # The ONLY permitted occurrences are the verifier's own DENYLIST — literals that exist
    # precisely to REJECT such material — and the negative check that reports it. They are
    # resolved from the live module, so the exemption can never drift wider than the denylist.
    verifier_denylist = {t.lower() for t in _FORBIDDEN_ARTIFACT_TOKENS} | {
        "no_nontrain_or_truth_data"
    }
    for path in _f6_files():
        allowed = verifier_denylist if path.name == "l2f_harness_verifier.py" else set()
        for literal in _code_strings(path):
            if literal in allowed:
                continue
            for concept in _F6_FORBIDDEN_CONCEPTS:
                assert concept not in literal, (path.name, literal, concept)


def test_f6_sql_never_selects_an_evaluation_column() -> None:
    """Every SQL literal in the F6 surface is inspected for evaluation columns and tables."""
    evaluation_sql = (
        "truth_vcf",
        "truth_bed",
        "mutations",
        "happy_",
        "hap_py",
        "leaderboard",
        "scoring",
        "tp_count",
        "fp_count",
        "fn_count",
    )
    legacy_sql = (
        "experiments.results",
        "experiments.jobs ",
        "profiling.profiles",
        "catalog.gatk_configs",
    )
    for path in _f6_files():
        for literal in _code_strings(path):
            if not any(verb in literal for verb in ("select", "insert", "update", "delete")):
                continue
            for token in evaluation_sql:
                assert token not in literal, (path.name, token, literal[:120])
            for token in legacy_sql:
                if token not in literal:
                    continue
                # A legacy table may be named ONLY inside a count(*) PROOF-OF-ABSENCE probe
                # (the verifier's `legacy_tables_excluded` check). It is never a data source.
                assert path.name == "l2f_harness_verifier.py", (path.name, token)
                assert literal.strip().startswith("select count(*) from "), literal[:120]


def test_f6_only_train_members_are_executable() -> None:
    """The input resolver requires the TRAIN partition; it has no validation/test branch."""
    import minos_engine.storage.l2f_execution_inputs as INPUTS

    literals = _code_strings(SRC / "storage/l2f_execution_inputs.py")
    assert "train" in literals
    assert "validation" not in literals and "test" not in literals
    assert hasattr(INPUTS, "resolve_accepted_execution_input")


def test_f6_resolves_no_truth_or_scoring_path() -> None:
    """Only the two provisioned dataset subtrees are ever addressed by name."""
    literals = _code_strings(SRC / "storage/l2f_execution_inputs.py")
    path_like = {lit for lit in literals if "/" in lit or lit.endswith((".bam", ".fa", ".dict"))}
    for literal in path_like:
        assert not any(c in literal for c in ("truth", "happy", "hap.py", "mutation", "score"))


def test_f6_failure_records_carry_only_bounded_fields() -> None:
    """A failure record is a bounded code plus an optional exit code and digest — nothing else."""
    from minos_engine.experiments.execution_contract import ExecutionFailure

    assert set(ExecutionFailure.model_fields) == {"failure_code", "exit_code", "stderr_sha256"}


def test_f6_failure_codes_are_a_closed_non_scientific_vocabulary() -> None:
    from minos_engine.storage.l2f_execution_contract import L2F_EXECUTION_FAILURE_CODES

    assert len(L2F_EXECUTION_FAILURE_CODES) == 6
    for code in L2F_EXECUTION_FAILURE_CODES:
        assert not any(c in code.lower() for c in _F6_FORBIDDEN_CONCEPTS)


def test_f6_child_environment_allowlist_carries_no_secret_or_evaluation_variable() -> None:
    from minos_engine.storage.l2f_gatk_runner import CHILD_ENV_ALLOWLIST

    assert set(CHILD_ENV_ALLOWLIST) == {
        "PATH",
        "HOME",
        "LANG",
        "LC_ALL",
        "TMPDIR",
        "JAVA_HOME",
        "TZ",
    }
    for name in CHILD_ENV_ALLOWLIST:
        lowered = name.lower()
        assert not any(c in lowered for c in _F6_FORBIDDEN_CONCEPTS)
        assert not any(s in lowered for s in ("url", "dsn", "password", "secret", "token", "key"))


def test_f6_experiments_package_stays_pure() -> None:
    """AST dependency check: the pure domain package imports no storage/db/subprocess module."""
    forbidden_roots = {
        "sqlalchemy",
        "psycopg",
        "alembic",
        "subprocess",
        "socket",
        "urllib",
        "requests",
        "httpx",
    }
    for path in sorted((SRC / "experiments").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert alias.name.split(".")[0] not in forbidden_roots, (path.name, alias.name)
            elif isinstance(node, ast.ImportFrom) and node.module:
                root = node.module.split(".")[0]
                assert root not in forbidden_roots, (path.name, node.module)
                assert not node.module.startswith("minos_engine.storage"), (path.name, node.module)


def test_f6_verifier_never_mutates(monkeypatch: object) -> None:
    """AST check: the verifier module issues no INSERT/UPDATE/DELETE/COPY/TRUNCATE literal."""
    mutating = ("insert into", "update ", "delete from", "truncate", "copy ", "create ", "drop ")
    for literal in _code_strings(SRC / "storage/l2f_harness_verifier.py"):
        stripped = literal.strip().lower()
        for verb in mutating:
            assert not stripped.startswith(verb), (verb, literal[:120])


def test_f6_recovery_never_retries() -> None:
    """No retry/backoff machinery exists anywhere on the F6 surface."""
    for path in _f6_files():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        names = {
            node.name
            for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
        }
        for name in names:
            assert "retry" not in name.lower(), (path.name, name)
            assert "backoff" not in name.lower(), (path.name, name)


def test_f6_cancelled_remains_unreachable() -> None:
    """No F6 code path can ever request the CANCELLED status."""
    from minos_engine.storage.l2f_job_claim import F4_TRANSITIONS

    assert not any("CANCELLED" in s for pair in F4_TRANSITIONS for s in pair)
    for path in _f6_files():
        assert "cancelled" not in _code_strings(path)
