"""The corrected GATK runtime boundary: explicit interpreter, pinned JVM, runtime identity.

A Phase-A campaign was lost because production launched the GATK script through its
``#!/usr/bin/env python`` shebang and the worker's ``PATH`` had no ``python``: ``env`` exited 127,
GATK never parsed an argument, and five candidates were recorded as having failed. Every control
here exists so that cannot recur, and so that if it ever does the ledger says so.

No GATK, no JVM: the fixtures are real processes started through the real boundary, and the
"launcher" is a small Python script — which is exactly what the production launcher is.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
from tests.gatk_runtime import PYTHON_INTERPRETER, runtime_kwargs

from minos_engine.experiments.execution_contract import (
    GatkExecutionError,
    GatkNonzeroExitError,
    GatkRuntimeIdentityError,
)
from minos_engine.experiments.execution_environment import (
    CHILD_ENVIRONMENT_POLICY_VERSION,
    EXECUTION_ENVIRONMENT_DOMAIN,
    GatkExecutionEnvironment,
    compute_execution_environment_hash,
)
from minos_engine.storage.l2f_gatk_runner import (
    CHILD_ENV_ALLOWLIST,
    ENV_GATK_EXECUTABLE,
    ENV_GATK_EXECUTABLE_SHA256,
    ENV_GATK_PYTHON,
    ENV_GATK_PYTHON_SHA256,
    ENV_GATK_VERSION,
    ENV_JAVA_HOME,
    SubprocessGatkRunner,
)

_VERSION = "4.5.0.0"
#: a stand-in launcher that behaves like the real one: a PYTHON script that prints the GATK
#: version banner. It carries NO shebang at all, which is the point — production supplies the
#: interpreter, so a launcher that could not start on its own still runs.
_LAUNCHER = (
    "import sys\n"
    "if '--version' in sys.argv:\n"
    "    print('The Genome Analysis Toolkit (GATK) v4.5.0.0')\n"
    "    sys.exit(0)\n"
    "sys.exit(0)\n"
)


def _bundle(tmp_path: Path, body: str = _LAUNCHER) -> Path:
    """A launcher plus the local JAR beside it — a launcher alone is not a bundle."""
    launcher = tmp_path / "gatk"
    launcher.write_text(body, encoding="utf-8")
    launcher.chmod(0o755)
    (tmp_path / f"gatk-package-{_VERSION}-local.jar").write_bytes(b"fixture-jar")
    return launcher


def _runner(tmp_path: Path, **over: Any) -> SubprocessGatkRunner:
    launcher = over.pop("launcher", None) or _bundle(tmp_path)
    kwargs: dict[str, Any] = {
        "executable": launcher,
        "expected_sha256": hashlib.sha256(launcher.read_bytes()).hexdigest(),
        "expected_version": _VERSION,
        "local_jar": launcher.parent / f"gatk-package-{_VERSION}-local.jar",
        **runtime_kwargs(tmp_path),
    }
    kwargs.update(over)
    return SubprocessGatkRunner(**kwargs)


# --------------------------------------------------------------------------- #
# THE regression: a PATH with no `python` must not stop production
# --------------------------------------------------------------------------- #
def test_a_path_without_python_still_runs_gatk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The exact diagnosed defect, inverted into a control.

    The child PATH is emptied of every ``python``, which is the condition under which the old
    boundary produced ``/usr/bin/env: 'python': No such file or directory`` and exit 127. Because
    the interpreter is now supplied explicitly, the identical bundle reports its version instead.
    """
    empty_bin = tmp_path / "no-python-bin"
    empty_bin.mkdir()
    monkeypatch.setenv("PATH", str(empty_bin))
    assert _which_python(str(empty_bin)) is None, "the fixture PATH must provide no python"

    observed = _runner(tmp_path).observe_version()

    assert observed == _VERSION


def _which_python(path: str) -> str | None:
    import shutil

    return shutil.which("python", path=path)


def test_the_process_argv_begins_with_the_explicit_interpreter(tmp_path: Path) -> None:
    """Structural, at the argv the boundary actually builds: interpreter first, launcher second."""
    runner = _runner(tmp_path)
    argv = runner._launch_argv("HaplotypeCaller", "-R", "ref.fa")

    assert argv[0] == str(PYTHON_INTERPRETER)
    assert argv[1] == str(runner.executable)
    assert argv[2:] == ["HaplotypeCaller", "-R", "ref.fa"]
    assert Path(argv[0]).is_absolute() and Path(argv[1]).is_absolute()


def test_the_launcher_shebang_is_never_consulted(tmp_path: Path) -> None:
    """A launcher whose shebang names a nonexistent interpreter still runs.

    Under the old policy this is precisely the 127 case; here the shebang is inert text because
    the interpreter is an argv token.
    """
    launcher = _bundle(tmp_path, "#!/nonexistent/python-does-not-exist\n" + _LAUNCHER)

    assert _runner(tmp_path, launcher=launcher).observe_version() == _VERSION


def test_the_boundary_starts_no_process_it_cannot_name(tmp_path: Path) -> None:
    """Nothing this boundary EXECUTES is located by name.

    ``shutil.which`` appears exactly once, and not to choose anything: Broad's launcher builds its
    own ``["java", ...]``, so the runner predicts what that bare token would resolve to in the
    exact child environment and refuses unless it is the JVM already pinned. The distinction that
    matters is between *discovering* an executable and *proving* which one upstream will pick, so
    this control now pins the single permitted call site rather than banning the name.
    """
    import ast

    source = Path("src/minos_engine/storage/l2f_gatk_runner.py").read_text("utf-8")
    tree = ast.parse(source)
    called: set[str] = set()
    which_sites: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            target = node.func
            name = target.attr if isinstance(target, ast.Attribute) else getattr(target, "id", "")
            called.add(name)
    for function in ast.walk(tree):
        if isinstance(function, ast.FunctionDef):
            for node in ast.walk(function):
                if isinstance(node, ast.Call) and getattr(node.func, "attr", "") == "which":
                    which_sites.append(function.name)

    for forbidden in ("find_executable", "executable"):
        assert forbidden not in called, f"the runner discovers an executable via {forbidden!r}"
    assert which_sites == ["_verify_java_dispatch"], (
        f"shutil.which is reachable from {which_sites}; the ONLY permitted use is proving the "
        "launcher's bare-java dispatch against the pinned JVM"
    )
    # and that one use must end in an equality against the pinned binary, never in a launch.
    verifier = next(
        f
        for f in ast.walk(tree)
        if isinstance(f, ast.FunctionDef) and f.name == "_verify_java_dispatch"
    )
    body = ast.get_source_segment(source, verifier) or ""
    assert "java_binary" in body and "_stable_sha256" in body
    assert "Popen" not in body and "subprocess" not in body
    assert "sys" not in source.split("import")[0]


# --------------------------------------------------------------------------- #
# provisioning is mandatory
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("missing", [ENV_GATK_PYTHON, ENV_GATK_PYTHON_SHA256, ENV_JAVA_HOME])
def test_from_env_refuses_an_incompletely_provisioned_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, missing: str
) -> None:
    launcher = _bundle(tmp_path)
    monkeypatch.setenv(ENV_GATK_EXECUTABLE, str(launcher))
    monkeypatch.setenv(
        ENV_GATK_EXECUTABLE_SHA256, hashlib.sha256(launcher.read_bytes()).hexdigest()
    )
    monkeypatch.setenv(ENV_GATK_VERSION, _VERSION)
    monkeypatch.setenv(ENV_GATK_PYTHON, str(PYTHON_INTERPRETER))
    monkeypatch.setenv(ENV_GATK_PYTHON_SHA256, "a" * 64)
    monkeypatch.setenv(ENV_JAVA_HOME, str(tmp_path / "jdk"))
    monkeypatch.delenv(missing, raising=False)

    with pytest.raises(GatkExecutionError, match="provisioned"):
        SubprocessGatkRunner.from_env()


def test_a_mismatched_interpreter_digest_is_refused(tmp_path: Path) -> None:
    runner = _runner(tmp_path, expected_python_sha256="b" * 64)

    with pytest.raises(GatkRuntimeIdentityError, match="sha256"):
        runner.observe_version()


def test_a_missing_or_symlinked_interpreter_is_refused(tmp_path: Path) -> None:
    absent = _runner(tmp_path, launcher_python=tmp_path / "no-such-python")
    with pytest.raises(GatkRuntimeIdentityError, match="not a regular file"):
        absent.observe_version()

    link = tmp_path / "python-link"
    link.symlink_to(PYTHON_INTERPRETER)
    with pytest.raises(GatkRuntimeIdentityError, match="symlink"):
        _runner(tmp_path, launcher_python=link).observe_version()


def test_a_missing_java_home_is_refused(tmp_path: Path) -> None:
    with pytest.raises(GatkRuntimeIdentityError, match="JAVA_HOME"):
        _runner(tmp_path, java_home=tmp_path / "no-such-jdk")._verify_java()

    empty = tmp_path / "empty-jdk"
    (empty / "bin").mkdir(parents=True)
    with pytest.raises(GatkRuntimeIdentityError, match="does not exist"):
        _runner(tmp_path, java_home=empty)._verify_java()


# --------------------------------------------------------------------------- #
# the runtime identity
# --------------------------------------------------------------------------- #
def test_the_environment_is_derived_by_measurement_and_hashes_deterministically(
    tmp_path: Path,
) -> None:
    runner = _runner(tmp_path)
    environment = runner.execution_environment()

    assert environment.gatk_version == _VERSION
    assert environment.child_environment_policy_version == CHILD_ENVIRONMENT_POLICY_VERSION
    assert (
        environment.launcher_python_sha256
        == hashlib.sha256(PYTHON_INTERPRETER.read_bytes()).hexdigest()
    )
    assert re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+.*", environment.launcher_python_version)
    assert environment.java_version == "17.0.11"
    # deterministic, and the same measurement twice gives the same identity.
    assert environment.environment_hash() == runner.execution_environment().environment_hash()
    assert re.fullmatch(r"[0-9a-f]{64}", environment.environment_hash())


@pytest.mark.parametrize(
    "field",
    [
        "gatk_launcher_sha256",
        "gatk_runtime_bundle_sha256",
        "gatk_version",
        "launcher_python_sha256",
        "launcher_python_version",
        "java_sha256",
        "java_version",
    ],
)
def test_every_runtime_component_moves_the_identity(field: str) -> None:
    """If a component could change without moving the hash, the identity would be decorative."""
    base = GatkExecutionEnvironment(
        gatk_launcher_sha256="0" * 64,
        gatk_runtime_bundle_sha256="1" * 64,
        gatk_version="4.5.0.0",
        launcher_python_sha256="2" * 64,
        launcher_python_version="3.12.3",
        java_sha256="3" * 64,
        java_version="17.0.11",
    )
    replacement = "9" * 64 if field.endswith("sha256") else "changed"
    moved = base.model_copy(update={field: replacement})

    assert moved.environment_hash() != base.environment_hash()


def test_the_identity_excludes_every_host_specific_fact() -> None:
    """Two hosts with the same runtime must agree; the same host with a different one must not."""
    fields = set(GatkExecutionEnvironment.model_fields)
    for forbidden in ("path", "hostname", "pid", "worker", "timestamp", "database", "uri", "home"):
        assert not any(forbidden in name for name in fields), forbidden
    assert EXECUTION_ENVIRONMENT_DOMAIN.startswith("minos:l2f-gatk-execution-environment:")


def test_the_preflight_proves_the_worker_can_run_gatk(tmp_path: Path) -> None:
    environment = _runner(tmp_path).preflight()

    assert environment.gatk_version == _VERSION
    assert environment.environment_hash()


def test_the_preflight_refuses_a_bundle_reporting_another_version(tmp_path: Path) -> None:
    launcher = _bundle(
        tmp_path,
        "import sys\nprint('The Genome Analysis Toolkit (GATK) v4.4.0.0')\nsys.exit(0)\n",
    )

    with pytest.raises(GatkRuntimeIdentityError, match="4.4.0.0"):
        _runner(tmp_path, launcher=launcher).preflight()


def test_the_child_environment_is_still_the_allowlist_only(tmp_path: Path) -> None:
    """The explicit interpreter did not widen what the child inherits."""
    runner = _runner(tmp_path)
    assert set(runner._child_env()) <= set(CHILD_ENV_ALLOWLIST)
    for override in ("GATK_LOCAL_JAR", "GATK_SPARK_JAR"):
        assert override not in CHILD_ENV_ALLOWLIST


# --------------------------------------------------------------------------- #
# structured failure evidence
# --------------------------------------------------------------------------- #
def test_a_nonzero_exit_carries_its_evidence_on_the_exception(tmp_path: Path) -> None:
    """The whole point: 127 must be readable WITHOUT re-running the job."""
    launcher = _bundle(
        tmp_path,
        "import sys\nsys.stderr.write(\"/usr/bin/env: 'python': not found\\n\")\nsys.exit(127)\n",
    )
    runner = _runner(tmp_path, launcher=launcher)
    work = tmp_path / "work"
    work.mkdir()

    with pytest.raises(GatkNonzeroExitError) as excinfo:
        runner.run(
            argv=("HaplotypeCaller",),
            work_dir=work,
            vcf_path=work / "out.vcf",
            inputs=_inputs(),
            expected_runtime_bundle_sha256=runner.runtime_bundle_sha256(),
        )

    error = excinfo.value
    assert error.exit_code == 127
    assert error.stderr_sha256 == hashlib.sha256((work / "gatk.stderr").read_bytes()).hexdigest()
    assert error.stdout_sha256 == hashlib.sha256((work / "gatk.stdout").read_bytes()).hexdigest()
    assert error.runtime_ms >= 0
    # and the evidence is on ATTRIBUTES, never recovered by parsing the message.
    assert "127" in str(error)


def test_a_runtime_that_moves_mid_job_is_a_runtime_failure_not_a_gatk_failure(
    tmp_path: Path,
) -> None:
    """Classification by stage: a substituted interpreter is ours, whatever GATK would have done."""
    runner = _runner(tmp_path)
    work = tmp_path / "work-moved"
    work.mkdir()
    expected = runner.execution_environment().environment_hash()
    moved = runner.__class__(
        **{
            **{f: getattr(runner, f) for f in runner.__dataclass_fields__},
            "expected_version": "4.5.0.1",
        }
    )

    with pytest.raises(GatkRuntimeIdentityError, match="execution environment"):
        moved.run(
            argv=("HaplotypeCaller",),
            work_dir=work,
            vcf_path=work / "out.vcf",
            inputs=_inputs(),
            expected_runtime_bundle_sha256=moved.runtime_bundle_sha256(),
            expected_execution_environment_hash=expected,
        )
    assert not (work / "out.vcf").exists()


def _inputs() -> Any:
    from minos_engine.experiments.execution_contract import ExecutionInput

    h = {c: c * 64 for c in "0123456789"}
    return ExecutionInput(
        dataset_id="minos-chr18-0001",
        round_id="r1",
        chromosome="chr18",
        profile_id="p1",
        content_hash=h["1"],
        feature_values_hash=h["2"],
        bam_sha256=h["3"],
        bai_sha256=h["4"],
        reference_sha256=h["5"],
        fai_sha256=h["6"],
        dictionary_sha256=h["7"],
        bam_size_bytes=1024,
        region_hash=h["8"],
        region_start0=100,
        region_end0_exclusive=200,
    )


def test_the_environment_hash_is_domain_separated() -> None:
    """A bare canonical-JSON digest would collide with any other structure of the same shape."""
    environment = GatkExecutionEnvironment(
        gatk_launcher_sha256="0" * 64,
        gatk_runtime_bundle_sha256="1" * 64,
        gatk_version="4.5.0.0",
        launcher_python_sha256="2" * 64,
        launcher_python_version="3.12.3",
        java_sha256="3" * 64,
        java_version="17.0.11",
    )
    import hashlib as _h

    from minos_engine.common.canonical_json import canonical_json_bytes

    undomained = _h.sha256(canonical_json_bytes(environment.model_dump(mode="json"))).hexdigest()
    assert compute_execution_environment_hash(environment) != undomained


def test_python_and_java_probes_never_start_haplotypecaller(tmp_path: Path) -> None:
    """Both preflight probes are non-biological by construction."""
    runner = _runner(tmp_path)
    assert runner.observe_python_version()
    assert runner.observe_java_version() == "17.0.11"
    assert not list(tmp_path.glob("*.vcf"))


def test_the_java_binary_is_resolved_from_java_home_not_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    decoy = tmp_path / "decoy-bin"
    decoy.mkdir()
    (decoy / "java").write_text("#!/bin/sh\nexit 9\n", encoding="utf-8")
    (decoy / "java").chmod(0o755)
    monkeypatch.setenv("PATH", str(decoy))
    runner = _runner(tmp_path)

    assert runner.java_binary == runner.java_home / "bin" / "java"
    assert runner.java_binary.parent.parent == runner.java_home
    assert str(decoy) not in str(runner.java_binary)
    assert runner.observe_java_version() == "17.0.11"


def test_the_subprocess_calls_use_the_launch_argv_helper() -> None:
    """No call site may assemble its own ``[launcher, ...]`` and bypass the interpreter."""
    source = Path("src/minos_engine/storage/l2f_gatk_runner.py").read_text("utf-8")
    assert "str(self.executable), *argv" not in source
    assert 'str(self.executable), "--version"' not in source
    assert source.count("_launch_argv(") >= 3


def test_no_shell_is_involved_anywhere(tmp_path: Path) -> None:
    calls = re.findall(
        r"subprocess\.(run|Popen)\(",
        Path("src/minos_engine/storage/l2f_gatk_runner.py").read_text("utf-8"),
    )
    assert calls, "the module must actually start processes"
    assert "shell=True" not in Path("src/minos_engine/storage/l2f_gatk_runner.py").read_text(
        "utf-8"
    )
    assert os.name == "posix"
    assert subprocess.run  # the module under test uses the same API surface


# --------------------------------------------------------------------------- #
# THE second dispatch defect: the launcher starts a BARE `java`
# --------------------------------------------------------------------------- #
_JAVA_STUB = "#!/bin/sh\necho 'openjdk version \"17.0.11\"' 1>&2\n"


def _java_bin(tmp_path: Path, name: str, body: str = _JAVA_STUB) -> Path:
    """A directory containing an executable called ``java``."""
    directory = tmp_path / name
    directory.mkdir(parents=True, exist_ok=True)
    java = directory / "java"
    java.write_text(body, encoding="utf-8")
    java.chmod(0o755)
    return directory


def test_the_pinned_jvm_on_PATH_is_accepted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The accepted deployment shape: bare ``java`` resolves to the JVM that was pinned."""
    runner = _runner(tmp_path)
    monkeypatch.setenv("PATH", str(runner.java_home / "bin"))

    resolved = runner._verify_java_dispatch(runner._child_env())

    assert resolved.resolve() == runner.java_binary.resolve()
    assert runner.preflight().gatk_version == _VERSION


def test_a_PATH_without_java_fails_closed_before_any_launcher_starts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The launcher would fail INSIDE itself with the JVM never started. Refuse first."""
    runner = _runner(tmp_path)
    monkeypatch.setenv("PATH", str(_java_bin(tmp_path, "empty-bin", body="").parent / "nothing"))

    started: list[Any] = []
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: started.append(a) or None)
    with pytest.raises(GatkRuntimeIdentityError, match="resolves no executable named 'java'"):
        runner._verify_java_dispatch(runner._child_env())
    assert started == [], "no process may be started once dispatch cannot be proven"


def test_an_empty_PATH_fails_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    runner = _runner(tmp_path)
    monkeypatch.setenv("PATH", "   ")
    with pytest.raises(GatkRuntimeIdentityError, match="supplies no PATH"):
        runner._verify_java_dispatch(runner._child_env())


def test_a_shadowing_java_earlier_on_PATH_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """PATH ORDER must not be able to choose the JVM. The pinned one is still on the path."""
    runner = _runner(tmp_path)
    shadow = _java_bin(tmp_path, "shadow-bin")
    monkeypatch.setenv("PATH", f"{shadow}:{runner.java_home / 'bin'}")

    with pytest.raises(GatkRuntimeIdentityError, match="would resolve to"):
        runner._verify_java_dispatch(runner._child_env())


def test_a_same_version_impostor_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reporting the right version is not being the right JVM.

    The impostor prints the identical banner the pinned stub prints, so nothing about its
    behaviour distinguishes it. Its bytes do, and that is what the runner compares.
    """
    runner = _runner(tmp_path)
    impostor = _java_bin(tmp_path, "impostor-bin", body=_JAVA_STUB + "# same banner, other bytes\n")
    assert (impostor / "java").read_text().startswith(_JAVA_STUB)
    monkeypatch.setenv("PATH", str(impostor))

    with pytest.raises(GatkRuntimeIdentityError):
        runner._verify_java_dispatch(runner._child_env())


def test_preflight_proves_dispatch_before_it_starts_the_version_probe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A bad PATH must fail before a launcher process exists at all."""
    runner = _runner(tmp_path)
    monkeypatch.setenv("PATH", str(_java_bin(tmp_path, "shadow-bin")))

    launched: list[Any] = []
    monkeypatch.setattr(
        subprocess, "run", lambda *a, **k: launched.append(a) or pytest.fail("a probe started")
    )
    with pytest.raises(GatkRuntimeIdentityError):
        runner.preflight()
    assert launched == []


def test_preflight_verifies_and_probes_with_the_SAME_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify ENV_A then launch with ENV_B is exactly the hole this closes."""
    runner = _runner(tmp_path)
    monkeypatch.setenv("PATH", str(runner.java_home / "bin"))

    verified: list[str] = []
    real_which = shutil.which

    def _recording_which(cmd: str, *, path: str | None = None, **kw: Any) -> str | None:
        if cmd == "java":
            verified.append(path or "")
        return real_which(cmd, path=path, **kw)

    launched: list[str] = []
    real_run = subprocess.run

    def _recording_run(*args: Any, **kwargs: Any) -> Any:
        launched.append(kwargs.get("env", {}).get("PATH", ""))
        return real_run(*args, **kwargs)

    monkeypatch.setattr(shutil, "which", _recording_which)
    monkeypatch.setattr(subprocess, "run", _recording_run)
    runner.preflight()

    assert verified, "dispatch was never verified"
    assert launched, "no probe ran"
    assert set(launched) == {verified[0]}, (
        "the PATH that was proven is not the PATH the launcher received"
    )
