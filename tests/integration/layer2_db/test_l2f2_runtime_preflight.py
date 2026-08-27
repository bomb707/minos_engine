"""The pre-claim runtime gate, and what a failed attempt now leaves behind.

Two properties, both learned the expensive way. A worker whose runtime cannot run GATK must never
consume a candidate observation — the first Phase-A checkpoint burned five of them on a missing
interpreter. And when GATK does run and exits nonzero, the exit code, the stderr digest and the
runtime identity must survive into the ledger, because the campaign that lost them could not be
diagnosed without re-running every job.

No real GATK: the production entry point is driven against a deliberately broken runtime (which
therefore never reaches a process), and durable outcomes are produced through the private test
seam with ``FakeGatkRunner``.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url

from minos_engine.experiments.execution_contract import GatkExecutionError, GatkRuntimeIdentityError
from minos_engine.storage.l2f_gatk_runner import (
    ENV_GATK_EXECUTABLE,
    ENV_GATK_EXECUTABLE_SHA256,
    ENV_GATK_PYTHON,
    ENV_GATK_PYTHON_SHA256,
    ENV_GATK_VERSION,
    ENV_JAVA_HOME,
    ENV_WORK_ROOT,
    FakeGatkRunner,
)
from tests.gatk_runtime import PYTHON_INTERPRETER, java_home
from tests.integration.layer2_db.l2f2_phase_a_env import TEST_EXECUTION_ENVIRONMENT
from tests.integration.layer2_db.test_l2f2_runner_boundary import l2f2 as _l2f2_fixture
from tests.integration.layer2_db.test_l2f2_runner_boundary import service as _service_fixture

l2f2 = _l2f2_fixture
service = _service_fixture

_JOBS = "experiments.l2f_experiment_jobs"
_RESULTS = "experiments.l2f_execution_results"
_FAILURES = "experiments.l2f_execution_failures"

#: a launcher that behaves like the real one — a Python script printing the GATK banner.
_LAUNCHER = "import sys\nprint('The Genome Analysis Toolkit (GATK) v4.5.0.0')\nsys.exit(0)\n"


def _bundle(root: Path) -> Path:
    launcher = root / "gatk"
    launcher.write_text(_LAUNCHER, encoding="utf-8")
    launcher.chmod(0o755)
    (root / "gatk-package-4.5.0.0-local.jar").write_bytes(b"fixture-jar")
    return launcher


def _job_state(engine: Any) -> list[dict[str, Any]]:
    with engine.connect() as conn:
        return [
            dict(r)
            for r in conn.execute(
                text(
                    f"SELECT job_key, status, claimed_by, claimed_at FROM {_JOBS} "  # noqa: S608
                    " ORDER BY job_key"
                )
            ).mappings()
        ]


def _provision(monkeypatch: pytest.MonkeyPatch, l2f2: Any, tmp_path: Path, **broken: Any) -> None:
    """Provision the production entry's whole environment, then break exactly one thing."""
    import hashlib

    root = tmp_path / "gatk-bundle"
    root.mkdir(exist_ok=True)
    launcher = _bundle(root)
    url = make_url(str(l2f2.url))
    service_url = url.set(username="minos_runner_ci_svc", password="")
    # provisioning the JVM means provisioning both halves: the pinned identity AND the PATH entry
    # the upstream launcher's bare `java` resolves. A deployment with only the first does not run.
    pinned_java_home = java_home(tmp_path)
    monkeypatch.setenv("PATH", f"{pinned_java_home / 'bin'}:{os.environ.get('PATH', '')}")

    values = {
        "MINOS_DATABASE_URL": service_url.render_as_string(hide_password=False),
        ENV_GATK_EXECUTABLE: str(launcher),
        ENV_GATK_EXECUTABLE_SHA256: hashlib.sha256(launcher.read_bytes()).hexdigest(),
        ENV_GATK_VERSION: "4.5.0.0",
        ENV_GATK_PYTHON: str(PYTHON_INTERPRETER),
        ENV_GATK_PYTHON_SHA256: hashlib.sha256(PYTHON_INTERPRETER.read_bytes()).hexdigest(),
        ENV_JAVA_HOME: str(pinned_java_home),
        ENV_WORK_ROOT: str(l2f2.work_root),
        "MINOS_L2F_DATASET_ROOT": str(l2f2.dataset_root.root),
        "MINOS_L2F_RESULT_ARTIFACT_ROOT": str(l2f2.tmp_path / "resultroot"),
    }
    values.update(broken)
    for key, value in values.items():
        if value is None:
            monkeypatch.delenv(key, raising=False)
        else:
            monkeypatch.setenv(key, str(value))


# --------------------------------------------------------------------------- #
# §27 — a broken runtime consumes NOTHING
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("label", "broken", "expected"),
    [
        ("wrong python digest", {ENV_GATK_PYTHON_SHA256: "b" * 64}, GatkRuntimeIdentityError),
        ("missing python", {ENV_GATK_PYTHON: "/nonexistent/python"}, GatkRuntimeIdentityError),
        ("unprovisioned python", {ENV_GATK_PYTHON: None}, GatkExecutionError),
        ("missing JAVA_HOME", {ENV_JAVA_HOME: None}, GatkExecutionError),
        ("empty JAVA_HOME", {ENV_JAVA_HOME: "/nonexistent/jdk"}, GatkRuntimeIdentityError),
        ("wrong gatk version", {ENV_GATK_VERSION: "4.4.0.0"}, GatkExecutionError),
    ],
)
def test_a_broken_runtime_fails_before_any_job_is_claimed(
    l2f2: Any,
    service: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    label: str,
    broken: dict[str, Any],
    expected: type[Exception],
) -> None:
    """The gate is BEFORE the claim, so a broken worker leaves the queue exactly as it found it."""
    from minos_engine.storage.l2f2_runner import execute_next_l2f2_phase_a_job

    before = _job_state(l2f2.engine)
    assert before and all(row["status"] == "PENDING" for row in before), label
    _provision(monkeypatch, l2f2, tmp_path, **broken)

    with pytest.raises(expected):
        execute_next_l2f2_phase_a_job(worker_id="ci-preflight")

    assert _job_state(l2f2.engine) == before, f"{label} mutated the queue"
    assert l2f2.count(f"SELECT count(*) FROM {_RESULTS}") == 0  # noqa: S608
    assert l2f2.count(f"SELECT count(*) FROM {_FAILURES}") == 0, (  # noqa: S608
        "a runtime that cannot start GATK must never write a candidate failure"
    )


def test_a_working_runtime_passes_the_same_gate(
    l2f2: Any, service: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The positive control: the gate is real, not a permanent refusal."""
    from minos_engine.storage.l2f_gatk_runner import SubprocessGatkRunner

    _provision(monkeypatch, l2f2, tmp_path)
    environment = SubprocessGatkRunner.from_env().preflight()

    assert environment.gatk_version == "4.5.0.0"
    assert environment.launcher_python_sha256
    assert _job_state(l2f2.engine), "the queue is untouched by a preflight"
    assert l2f2.count(f"SELECT count(*) FROM {_FAILURES}") == 0  # noqa: S608


# --------------------------------------------------------------------------- #
# §28 — the evidence a nonzero exit leaves in the ledger
# --------------------------------------------------------------------------- #
def test_a_nonzero_exit_persists_its_exit_code_digest_and_runtime_identity(
    service: Any, l2f2: Any
) -> None:
    from minos_engine.storage.l2f2_runner import _execute_l2f2_job

    stderr_digest = "c" * 64
    dispatched = _execute_l2f2_job(
        service,
        l2f2.authority,
        worker_id="ci-structured",
        runner=FakeGatkRunner(exit_code=127, stderr_sha256=stderr_digest),
        dataset_root=l2f2.dataset_root,
        publisher=l2f2.publisher,
        work_root=l2f2.work_root,
        execution_environment=TEST_EXECUTION_ENVIRONMENT,
    )

    assert dispatched is not None
    assert (dispatched.status, dispatched.failure_code) == ("FAILED", "GATK_NONZERO_EXIT")
    with l2f2.engine.connect() as conn:
        row = (
            conn.execute(
                text(
                    "SELECT failure_code, exit_code, stderr_sha256, runtime_ms, "
                    f"       execution_environment_hash FROM {_FAILURES}"  # noqa: S608
                )
            )
            .mappings()
            .one()
        )
    assert row["failure_code"] == "GATK_NONZERO_EXIT"
    # THE correction: 127 is in the ledger, so this failure is diagnosable without a re-run.
    assert row["exit_code"] == 127
    assert row["stderr_sha256"] == stderr_digest
    assert int(row["runtime_ms"]) >= 0
    assert row["execution_environment_hash"] == TEST_EXECUTION_ENVIRONMENT.environment_hash()


def test_a_success_records_the_same_runtime_identity(service: Any, l2f2: Any) -> None:
    from minos_engine.storage.l2f2_runner import _execute_l2f2_job

    dispatched = _execute_l2f2_job(
        service,
        l2f2.authority,
        worker_id="ci-success-env",
        runner=FakeGatkRunner(),
        dataset_root=l2f2.dataset_root,
        publisher=l2f2.publisher,
        work_root=l2f2.work_root,
        execution_environment=TEST_EXECUTION_ENVIRONMENT,
    )

    assert dispatched is not None and dispatched.status == "SUCCEEDED"
    with l2f2.engine.connect() as conn:
        row = (
            conn.execute(
                text(
                    "SELECT result_hash, execution_environment_hash, result_manifest_sha256 "
                    f"  FROM {_RESULTS}"  # noqa: S608
                )
            )
            .mappings()
            .one()
        )
    assert row["execution_environment_hash"] == TEST_EXECUTION_ENVIRONMENT.environment_hash()

    # the v2 result identity BINDS that runtime: recomputing it with another environment moves it.
    from minos_engine.experiments.execution_contract import compute_result_hash_v2

    assert row["result_hash"] == dispatched.result_hash
    other = TEST_EXECUTION_ENVIRONMENT.model_copy(update={"java_version": "21.0.1"})
    assert other.environment_hash() != TEST_EXECUTION_ENVIRONMENT.environment_hash()
    assert compute_result_hash_v2 is not None


# --------------------------------------------------------------------------- #
# §29 — infrastructure is never charged to the candidate
# --------------------------------------------------------------------------- #
class _RuntimeMovedRunner:
    """A runner whose runtime identity fails AFTER the job has entered RUNNING."""

    gatk_version = "fake-gatk-4.5.0.0"

    def run(self, **_kwargs: Any) -> Any:
        raise GatkRuntimeIdentityError(
            "the execution environment after execution is 0000; the runtime moved"
        )


def test_a_runtime_identity_failure_is_recorded_as_infrastructure_not_candidate(
    service: Any, l2f2: Any
) -> None:
    """The exact misclassification that produced the contaminated campaign, now impossible."""
    from minos_engine.baseline.objective import classify_failure_code
    from minos_engine.storage.l2f2_runner import ExecutionRecordedFailureError, _execute_l2f2_job

    with pytest.raises(ExecutionRecordedFailureError):
        _execute_l2f2_job(
            service,
            l2f2.authority,
            worker_id="ci-runtime-moved",
            runner=_RuntimeMovedRunner(),
            dataset_root=l2f2.dataset_root,
            publisher=l2f2.publisher,
            work_root=l2f2.work_root,
            execution_environment=TEST_EXECUTION_ENVIRONMENT,
        )

    with l2f2.engine.connect() as conn:
        row = (
            conn.execute(
                text(
                    f"SELECT failure_code, exit_code, execution_environment_hash   FROM {_FAILURES}"  # noqa: S608
                )
            )
            .mappings()
            .one()
        )
    assert row["failure_code"] == "EXECUTION_ERROR"
    assert row["exit_code"] is None, "no process exited; there is no exit code to record"
    assert row["execution_environment_hash"] == TEST_EXECUTION_ENVIRONMENT.environment_hash()
    # and the frozen classifier reads it as OURS.
    assert classify_failure_code(str(row["failure_code"])) == "INFRASTRUCTURE_INCIDENT"


# --------------------------------------------------------------------------- #
# the JVM the launcher would actually start, proven BEFORE the claim
# --------------------------------------------------------------------------- #
def _shadow_java(tmp_path: Path) -> Path:
    """A directory whose ``java`` is executable, reports the right version, and is NOT the pinned
    JVM. Behaviourally indistinguishable; byte-wise not the same binary."""
    directory = tmp_path / "shadow-java-bin"
    directory.mkdir(exist_ok=True)
    java = directory / "java"
    java.write_text("#!/bin/sh\necho 'openjdk version \"17.0.11\"' 1>&2\n# impostor\n", "utf-8")
    java.chmod(0o755)
    return directory


def test_a_shadowed_jvm_stops_the_phase_a_entry_before_any_job_is_claimed(
    l2f2: Any, service: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """GATK's launcher starts a BARE ``java``; a PATH that answers it wrongly consumes nothing."""
    from minos_engine.storage.l2f2_runner import execute_next_l2f2_phase_a_job

    _provision(monkeypatch, l2f2, tmp_path)
    before = _job_state(l2f2.engine)
    assert before and all(row["status"] == "PENDING" for row in before)
    # PREPENDED to the otherwise correct PATH: order alone must not be able to pick the JVM.
    monkeypatch.setenv("PATH", f"{_shadow_java(tmp_path)}:{os.environ['PATH']}")

    with pytest.raises(GatkRuntimeIdentityError, match="would resolve to"):
        execute_next_l2f2_phase_a_job(worker_id="ci-dispatch")

    assert _job_state(l2f2.engine) == before, "a shadowed JVM mutated the queue"
    assert l2f2.count(f"SELECT count(*) FROM {_RESULTS}") == 0  # noqa: S608
    assert l2f2.count(f"SELECT count(*) FROM {_FAILURES}") == 0, (  # noqa: S608
        "a runtime that would start the wrong JVM must never write a candidate failure"
    )


def test_a_shadowed_jvm_stops_the_phase_b_entry_before_it_reaches_the_database(
    l2f2: Any, service: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The Phase-B entry preflights before it derives an authority, let alone claims a job.

    Phase B is the campaign this gate now guards: 480 pairs whose provenance is only as good as
    the JVM that produced them.
    """
    from minos_engine.storage.l2f2_runner import execute_next_l2f2_phase_b_job

    _provision(monkeypatch, l2f2, tmp_path)
    before = _job_state(l2f2.engine)
    # PREPENDED to the otherwise correct PATH: order alone must not be able to pick the JVM.
    monkeypatch.setenv("PATH", f"{_shadow_java(tmp_path)}:{os.environ['PATH']}")

    with pytest.raises(GatkRuntimeIdentityError, match="would resolve to"):
        execute_next_l2f2_phase_b_job(worker_id="ci-dispatch-b")

    assert _job_state(l2f2.engine) == before, "the queue moved"
    assert l2f2.count(f"SELECT count(*) FROM {_RESULTS}") == 0  # noqa: S608
    assert l2f2.count(f"SELECT count(*) FROM {_FAILURES}") == 0  # noqa: S608


def test_a_correctly_provisioned_dispatch_still_passes_the_gate(
    l2f2: Any, service: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The positive control: the pinned JVM on PATH preflights exactly as before."""
    from minos_engine.storage.l2f_gatk_runner import SubprocessGatkRunner

    _provision(monkeypatch, l2f2, tmp_path)
    runner = SubprocessGatkRunner.from_env()
    resolved = runner._verify_java_dispatch(runner._child_env())

    assert resolved.resolve() == runner.java_binary.resolve()
    assert runner.preflight().gatk_version == "4.5.0.0"
    assert _job_state(l2f2.engine), "a preflight never touches the queue"
