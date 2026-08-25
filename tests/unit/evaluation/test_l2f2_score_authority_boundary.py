"""ONE caller, ONE score authority — enforced structurally, not by convention.

Two rules this repository must not drift away from:

* **The caller is GATK HaplotypeCaller, and only GATK.** No production execution, candidate
  generation, parameter search or CONFIG-emission path may invoke an alternative variant caller.
* **The Minos score is defined by MINOS_SUBNET, not here.** MINOS_ENGINE's historical local
  scorer survives for audits and historical tests, but nothing on the production evaluation path
  may reach it, and no engine-local hap.py / bcftools / RTG command may reappear.

The two rules interact, which is why they live in one file: the pinned upstream scorer
legitimately runs its own internal tooling, and that must never be mistaken for MINOS_ENGINE
gaining a second caller. The oracle is exempt only because it delegates opaquely to MINOS_SUBNET
— it builds no command of its own, which the tests below check at argv level.

Every control here distinguishes *executing* a tool from *mentioning* one. A pinned image digest,
a settings key, an error message and a docstring all legitimately contain the word "bcftools"; a
control that could not tell those from a subprocess call would either be gutted to pass or would
fail on prose. So the caller checks read the AST and look at what actually lands in an argv.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest


def _repo_root() -> Path:
    from minos_engine.qualification.l2f_accepted_identities import repository_root

    return repository_root()


def _src(*parts: str) -> Path:
    return _repo_root() / "src" / "minos_engine" / Path(*parts)


def _executable_source(path: Path) -> str:
    """The file's CODE, with comments and genuine docstrings removed."""
    source = path.read_text(encoding="utf-8")
    lines = source.splitlines()
    drop: set[int] = set()
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        body = getattr(node, "body", [])
        if (
            body
            and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)
        ):
            drop.update(range(body[0].lineno - 1, body[0].end_lineno or body[0].lineno))
    kept = [line for i, line in enumerate(lines) if i not in drop]
    return "\n".join(line.split("#", 1)[0] for line in kept)


def _local_imports(path: Path) -> set[str]:
    out: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.ImportFrom) and node.module:
            out.add(node.module)
        elif isinstance(node, ast.Import):
            out.update(alias.name for alias in node.names)
    return {module for module in out if module.startswith("minos_engine")}


_SUBPROCESS_CALLS = {"run", "Popen", "call", "check_call", "check_output"}


def _executed_argv_literals(path: Path) -> set[str]:
    """Every string constant this module places in a subprocess argv.

    This is the honest form of "does it run X". Naming a tool in a message or a pinned digest is
    not execution, and only argv position distinguishes the two.
    """
    found: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if not isinstance(node, ast.Call):
            continue
        name = getattr(node.func, "attr", None) or getattr(node.func, "id", None)
        if name not in _SUBPROCESS_CALLS or not node.args:
            continue
        argv = node.args[0]
        if isinstance(argv, ast.List | ast.Tuple):
            found.update(
                element.value
                for element in argv.elts
                if isinstance(element, ast.Constant) and isinstance(element.value, str)
            )
        elif isinstance(argv, ast.Constant) and isinstance(argv.value, str):
            found.add(argv.value)
    return found


def _python_files(*parts: str) -> list[Path]:
    root = _src(*parts)
    if root.is_file():
        return [root]
    return sorted(path for path in root.rglob("*.py") if "__pycache__" not in path.parts)


# --------------------------------------------------------------------------- #
# the production score authority is upstream
# --------------------------------------------------------------------------- #
#: MINOS_ENGINE's historical local scorer. Retained for audits; banned from production.
_NON_PRODUCTION_SCORERS = ("happy_runner", "happy_metrics", "minos_score")

#: every module the production evaluation path actually runs through.
_PRODUCTION_EVALUATION = (
    "orchestrator",
    "evaluator",
    "contracts",
    "minos_subnet_oracle",
    "scoring_contract",
    "artifact_publisher",
    "truth_registration",
)


@pytest.mark.parametrize("module", _PRODUCTION_EVALUATION)
def test_no_production_module_imports_the_local_scorer(module: str) -> None:
    imported = _local_imports(_src("evaluation", f"{module}.py"))
    offending = sorted(
        name for name in imported if name.rsplit(".", 1)[-1] in _NON_PRODUCTION_SCORERS
    )
    assert not offending, (
        f"{module} imports the historical local scorer {offending}; production scoring must go "
        "through minos_subnet_oracle only"
    )


def test_the_production_chain_is_transitively_free_of_the_local_scorer() -> None:
    """Not just the direct imports — the whole reachable production closure."""
    seen: set[str] = set()
    frontier = ["minos_engine.evaluation.orchestrator"]
    while frontier:
        name = frontier.pop()
        if name in seen:
            continue
        seen.add(name)
        path = _repo_root() / "src" / "minos_engine" / Path(*name.split(".")[1:]).with_suffix(".py")
        if not path.is_file():
            continue
        frontier.extend(_local_imports(path))
    offending = sorted(
        module for module in seen if module.rsplit(".", 1)[-1] in _NON_PRODUCTION_SCORERS
    )
    assert not offending, f"the production evaluation closure reaches {offending}"
    assert "minos_engine.evaluation.minos_subnet_oracle" in seen, "the oracle is not on the path"


@pytest.mark.parametrize("module", _NON_PRODUCTION_SCORERS)
def test_the_local_scorer_declares_itself_non_production(module: str) -> None:
    text = _src("evaluation", f"{module}.py").read_text(encoding="utf-8")
    assert "NOT PRODUCTION SCORE AUTHORITY" in text, module
    assert "minos_subnet_oracle" in text, module


def test_the_evaluator_computes_no_score() -> None:
    """The evaluator records an upstream outcome; it must not contain a scoring formula."""
    code = _executable_source(_src("evaluation", "evaluator.py"))
    for banned in ("compute_advanced_score", "decide_admission", "emphasis(", "ratio_penalty"):
        assert banned not in code, banned


def test_the_orchestrator_runs_no_scoring_tool_of_its_own() -> None:
    path = _src("evaluation", "orchestrator.py")
    assert not _executed_argv_literals(path), "the orchestrator starts no process at all"
    code = _executable_source(path)
    for banned in ("parse_happy_outputs", "HappyRunner", "happy_runner"):
        assert banned not in code, banned


def test_the_oracle_builds_no_scientific_command() -> None:
    """The adapter starts ONE kind of process: the bridge. Every scientific command is upstream's."""
    path = _src("evaluation", "minos_subnet_oracle.py")
    argv = _executed_argv_literals(path)
    for banned in ("hap.py", "bcftools", "rtg", "vcfeval", "deepvariant", "freebayes", "docker"):
        assert not any(banned in literal.lower() for literal in argv), (
            f"the oracle constructs a {banned!r} command; that belongs to MINOS_SUBNET"
        )
    # git is provenance only; the interpreter and the bridge path are computed, never literals.
    assert argv <= {"git", "-C", "rev-parse", "HEAD", "status", "--porcelain", "--"}, argv
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Call) and getattr(node.func, "attr", None) in _SUBPROCESS_CALLS:
            assert not [kw for kw in node.keywords if kw.arg == "shell"], "shell= must never appear"


def test_the_bridge_contains_no_scoring_arithmetic() -> None:
    """The bridge calls upstream and reports; it must not reimplement any of it."""
    path = _src("evaluation", "_minos_subnet_bridge.py")
    code = _executable_source(path)
    for banned in ("emphasis", "ratio_penalty", "0.60", "0.15", "0.10", "gamma", "titv", "hethom"):
        assert banned not in code.lower(), banned
    # the score and the admission decision are upstream's own, CALLED rather than reproduced.
    for required in (
        "compute_advanced_score",
        "_valid_round_score",
        "_is_zero_input_advanced_fingerprint",
    ):
        assert required in code, required
    # it runs under a DIFFERENT interpreter, so it may not import this package at all.
    assert not any(name.startswith("minos_engine") for name in _all_imports(path))
    assert not _executed_argv_literals(path), "the bridge starts no process of its own"


def _all_imports(path: Path) -> set[str]:
    out: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.ImportFrom) and node.module:
            out.add(node.module)
        elif isinstance(node, ast.Import):
            out.update(alias.name for alias in node.names)
    return out


# --------------------------------------------------------------------------- #
# the caller is GATK, and only GATK
# --------------------------------------------------------------------------- #
#: an alternative variant CALLER would be a scientific redesign, not an implementation detail.
_ALTERNATIVE_CALLERS = ("deepvariant", "freebayes", "bcftools", "octopus", "strelka")

#: every production surface that decides, generates or executes a caller invocation.
_CALLER_SURFACES = (
    ("callers",),
    ("baseline",),
    ("layer2",),
    ("experiments",),
    ("storage", "l2f2_runner.py"),
    ("storage", "l2f_gatk_runner.py"),
)


@pytest.mark.parametrize("parts", _CALLER_SURFACES, ids=lambda p: "/".join(p))
def test_no_production_caller_surface_executes_an_alternative_caller(
    parts: tuple[str, ...],
) -> None:
    for path in _python_files(*parts):
        argv = _executed_argv_literals(path)
        for banned in _ALTERNATIVE_CALLERS:
            offending = sorted(literal for literal in argv if banned in literal.lower())
            assert not offending, f"{path.relative_to(_repo_root())} executes {offending}"


@pytest.mark.parametrize("parts", _CALLER_SURFACES, ids=lambda p: "/".join(p))
def test_no_production_caller_surface_even_names_a_rival_caller(parts: tuple[str, ...]) -> None:
    """These four have no legitimate reason to appear in caller source at all."""
    for path in _python_files(*parts):
        code = _executable_source(path).lower()
        for banned in ("deepvariant", "freebayes", "octopus", "strelka"):
            assert banned not in code, f"{path.relative_to(_repo_root())} names {banned!r}"


def test_the_gatk_caller_is_still_haplotypecaller() -> None:
    """The positive half: the one caller the search is allowed to use is still the right one."""
    named = [
        path
        for path in _python_files("callers")
        if "HaplotypeCaller" in path.read_text(encoding="utf-8")
    ]
    assert named, "no production caller surface names HaplotypeCaller"


def test_no_engine_module_outside_the_audit_scorer_executes_a_scoring_container() -> None:
    """A bcftools/hap.py runner must not reappear in production source.

    The exemption is narrow and deliberate: the historical local scorer keeps its command builder
    for audit purposes only. The oracle needs no exemption — it builds no such command.
    """
    exempt = {_src("evaluation", f"{name}.py") for name in _NON_PRODUCTION_SCORERS}
    offenders: list[str] = []
    for path in sorted(_src().rglob("*.py")):
        if path in exempt or "__pycache__" in path.parts:
            continue
        argv = _executed_argv_literals(path)
        if any("bcftools" in literal.lower() or "hap.py" in literal.lower() for literal in argv):
            offenders.append(str(path.relative_to(_repo_root())))
    assert not offenders, f"engine-local scoring tool invocations reappeared in {offenders}"
