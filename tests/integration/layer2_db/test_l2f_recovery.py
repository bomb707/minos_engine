"""F6 execution recovery — no job is ever silently stranded in CLAIMED or RUNNING.

Every row of the recovery contract is exercised against real PostgreSQL with the deterministic
FakeGatkRunner. Scratch databases only; no GATK process is ever started.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from minos_engine.experiments.execution_contract import (
    ConfigArtifactError,
    GatkExecutionError,
    GatkInvocationError,
    InputResolutionError,
)
from minos_engine.storage import l2f_execution as EX
from minos_engine.storage import l2f_job_claim as JC
from minos_engine.storage.l2f_execution import (
    AmbiguousExecutionCommitError,
    AmbiguousRecoveryCommitError,
    AmbiguousStartCommitError,
    ExecutionRecordedFailureError,
    ExecutionRecoveryError,
    ExecutionWorkspaceError,
    PostCommitWrapperError,
    PreTerminalExecutionError,
    assert_no_stranded_jobs,
    find_nonterminal_jobs,
)
from minos_engine.storage.l2f_gatk_runner import FakeGatkRunner
from minos_engine.storage.l2f_job_claim import AmbiguousClaimCommitError
from tests.integration.layer2_db.test_l2f_execution_corrective import env as _env_fixture

#: the F5 corrective environment fixture, reused verbatim (scratch database, provisioned inputs).
env = _env_fixture

_JOBS = "experiments.l2f_experiment_jobs"
_RESULTS = "experiments.l2f_execution_results"
_FAILURES = "experiments.l2f_execution_failures"


def _statuses(env: Any) -> dict[str, int]:
    from sqlalchemy import text

    with env.engine.connect() as c:
        rows = c.execute(
            text(f"SELECT status, count(*) FROM {_JOBS} GROUP BY status")  # noqa: S608
        ).all()
    return {str(r[0]): int(r[1]) for r in rows}


def _counts(env: Any) -> tuple[int, int]:
    from sqlalchemy import text

    with env.engine.connect() as c:
        r = int(c.execute(text(f"SELECT count(*) FROM {_RESULTS}")).scalar_one())  # noqa: S608
        f = int(c.execute(text(f"SELECT count(*) FROM {_FAILURES}")).scalar_one())  # noqa: S608
    return r, f


# --------------------------------------------------------------------------- #
# the final-state assertion helper itself
# --------------------------------------------------------------------------- #
def test_the_state_helper_detects_a_genuinely_stranded_job(env: Any) -> None:
    """Control: a job deliberately left CLAIMED IS reported, so the helper can actually fail."""
    assert find_nonterminal_jobs(env.engine, env.plan.plan_hash) == ()
    assert_no_stranded_jobs(env.engine, env.plan.plan_hash)

    claimed = JC._claim_next_job_with_trust(env.engine, env.plan, worker_id="w-strand")
    assert claimed is not None
    stranded = find_nonterminal_jobs(env.engine, env.plan.plan_hash)
    assert stranded == ((claimed.job_id, "CLAIMED"),)
    with pytest.raises(ExecutionRecoveryError):
        assert_no_stranded_jobs(env.engine, env.plan.plan_hash)


# --------------------------------------------------------------------------- #
# row 1 — preparation fails while CLAIMED -> PENDING
# --------------------------------------------------------------------------- #
def test_missing_input_recovers_to_pending(env: Any, tmp_path: Path) -> None:
    (tmp_path / "datasets" / "practice" / "round_r0" / "input.bam").unlink()
    with pytest.raises(PreTerminalExecutionError) as excinfo:
        env.run()
    assert excinfo.value.recovered_to == "PENDING"
    assert isinstance(excinfo.value.__cause__, InputResolutionError)
    assert _statuses(env) == {"PENDING": 4}
    assert _counts(env) == (0, 0)
    assert_no_stranded_jobs(env.engine, env.plan.plan_hash)


def test_an_unexpected_preparation_error_still_recovers_to_pending(
    env: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Even an error type the F5 code never anticipated must not strand the claim."""

    def _boom(*_a: Any, **_k: Any) -> Any:
        raise ZeroDivisionError("an entirely unexpected preparation failure")

    monkeypatch.setattr(EX, "_prepare", _boom)
    with pytest.raises(PreTerminalExecutionError) as excinfo:
        env.run()
    assert isinstance(excinfo.value.__cause__, ZeroDivisionError)
    assert _statuses(env) == {"PENDING": 4}
    assert_no_stranded_jobs(env.engine, env.plan.plan_hash)


def test_a_config_failure_recovers_to_pending(env: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(*_a: Any, **_k: Any) -> Any:
        raise ConfigArtifactError("tampered CONFIG")

    monkeypatch.setattr(EX, "load_accepted_execution_config", _boom)
    with pytest.raises(PreTerminalExecutionError) as excinfo:
        env.run()
    assert isinstance(excinfo.value.__cause__, ConfigArtifactError)
    assert _statuses(env) == {"PENDING": 4}
    assert_no_stranded_jobs(env.engine, env.plan.plan_hash)


# --------------------------------------------------------------------------- #
# row 2 — the release commit is ambiguous
# --------------------------------------------------------------------------- #
def test_an_ambiguous_release_is_typed_and_never_retried(
    env: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    (tmp_path / "datasets" / "practice" / "round_r0" / "input.bam").unlink()
    calls = {"n": 0}
    real = EX._release_job_with_trust

    def _ambiguous(*a: Any, **k: Any) -> Any:
        calls["n"] += 1
        raise AmbiguousClaimCommitError("simulated ambiguous release COMMIT")

    monkeypatch.setattr(EX, "_release_job_with_trust", _ambiguous)
    with pytest.raises(AmbiguousRecoveryCommitError) as excinfo:
        env.run()
    assert isinstance(excinfo.value.__cause__, InputResolutionError)
    assert calls["n"] == 1  # never retried
    assert real is not None


def test_a_failed_release_preserves_both_failures(
    env: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    (tmp_path / "datasets" / "practice" / "round_r0" / "input.bam").unlink()

    def _boom(*_a: Any, **_k: Any) -> Any:
        raise RuntimeError("the release itself failed")

    monkeypatch.setattr(EX, "_release_job_with_trust", _boom)
    with pytest.raises(ExecutionRecoveryError) as excinfo:
        env.run()
    # BOTH statuses survive: the original cause is chained, the recovery cause is attached.
    assert isinstance(excinfo.value.__cause__, InputResolutionError)
    assert isinstance(excinfo.value.recovery_cause, RuntimeError)


# --------------------------------------------------------------------------- #
# row 3 — the start commit is ambiguous: GATK must never run
# --------------------------------------------------------------------------- #
def test_an_ambiguous_start_never_executes_gatk(env: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    ran = {"n": 0}

    class _CountingRunner(FakeGatkRunner):
        def run(self, **kw: Any) -> Any:  # pragma: no cover - must never be reached
            ran["n"] += 1
            return super().run(**kw)

    def _ambiguous(*_a: Any, **_k: Any) -> Any:
        raise AmbiguousClaimCommitError("simulated ambiguous start COMMIT")

    monkeypatch.setattr(EX, "_start_job_with_trust", _ambiguous)
    with pytest.raises(AmbiguousStartCommitError):
        env.run(runner=_CountingRunner())
    assert ran["n"] == 0
    assert _counts(env) == (0, 0)


def test_a_non_ambiguous_start_failure_recovers_to_pending(
    env: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _boom(*_a: Any, **_k: Any) -> Any:
        raise RuntimeError("start refused")

    monkeypatch.setattr(EX, "_start_job_with_trust", _boom)
    with pytest.raises(PreTerminalExecutionError) as excinfo:
        env.run()
    assert isinstance(excinfo.value.__cause__, RuntimeError)
    assert _statuses(env) == {"PENDING": 4}
    assert_no_stranded_jobs(env.engine, env.plan.plan_hash)


# --------------------------------------------------------------------------- #
# rows 4/6 — every non-ambiguous error after RUNNING is durably FAILED
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("runner", "code"),
    [
        (FakeGatkRunner(exit_code=3), "GATK_NONZERO_EXIT"),
        (FakeGatkRunner(raise_timeout=True), "GATK_TIMEOUT"),
        (FakeGatkRunner(write_output=False), "GATK_OUTPUT_INVALID"),
        (FakeGatkRunner(override_bytes=b"not a vcf\n"), "GATK_OUTPUT_INVALID"),
    ],
)
def test_recognized_runner_failures_are_durably_failed(
    env: Any, runner: FakeGatkRunner, code: str
) -> None:
    result = env.run(runner=runner)
    assert result is not None and result.status == "FAILED" and result.failure_code == code
    assert _counts(env) == (0, 1)
    assert_no_stranded_jobs(env.engine, env.plan.plan_hash)


def test_a_workspace_failure_after_running_is_durably_failed(
    env: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Gap C1: attempt-directory creation happens AFTER RUNNING and must never strand the job."""

    def _boom(*_a: Any, **_k: Any) -> Any:
        raise ExecutionWorkspaceError("attempt directory could not be created")

    monkeypatch.setattr(EX, "_create_attempt_dir", _boom)
    with pytest.raises(ExecutionRecordedFailureError) as excinfo:
        env.run()
    assert isinstance(excinfo.value.__cause__, ExecutionWorkspaceError)
    assert excinfo.value.failure_code == "EXECUTION_ERROR"
    assert _counts(env) == (0, 1)
    assert_no_stranded_jobs(env.engine, env.plan.plan_hash)


def test_an_invocation_rendering_failure_is_durably_failed(
    env: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Gap C2: argv rendering happens AFTER RUNNING and must never strand the job."""

    def _boom(*_a: Any, **_k: Any) -> Any:
        raise GatkInvocationError("argv could not be rendered")

    monkeypatch.setattr(EX, "render_execution_argv", _boom)
    with pytest.raises(ExecutionRecordedFailureError) as excinfo:
        env.run()
    assert isinstance(excinfo.value.__cause__, GatkInvocationError)
    assert _counts(env) == (0, 1)
    assert_no_stranded_jobs(env.engine, env.plan.plan_hash)


def test_a_subprocess_startup_failure_is_durably_failed(env: Any) -> None:
    """Gap C3: runner setup / process startup failures end in a durable bounded failure."""

    class _StartupFails(FakeGatkRunner):
        def run(self, **_kw: Any) -> Any:
            raise OSError("the subprocess could not be started")

    result = env.run(runner=_StartupFails())
    assert result is not None and result.status == "FAILED"
    assert result.failure_code == "EXECUTION_ERROR"
    assert _counts(env) == (0, 1)
    assert_no_stranded_jobs(env.engine, env.plan.plan_hash)


def test_an_output_read_failure_is_durably_failed(
    env: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Gap C3: reading the produced output can fail; the job must still end terminal.

    Patches the descriptor-bound acquisition boundary, which is what the production path uses.
    """

    def _boom(*_a: Any, **_k: Any) -> None:
        raise OSError("the produced output could not be read")

    monkeypatch.setattr(EX, "acquire_produced_output", _boom)
    result = env.run()
    assert result is not None and result.status == "FAILED"
    assert _counts(env) == (0, 1)
    assert_no_stranded_jobs(env.engine, env.plan.plan_hash)


def test_a_success_persistence_failure_is_durably_failed(
    env: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Gap C4: a NON-ambiguous success-persistence failure after GATK must not strand the job."""

    def _boom(*_a: Any, **_k: Any) -> Any:
        raise RuntimeError("artifact registration failed")

    monkeypatch.setattr(EX, "_register_artifact", _boom)
    with pytest.raises(ExecutionRecordedFailureError) as excinfo:
        env.run()
    assert isinstance(excinfo.value.__cause__, RuntimeError)
    assert _counts(env) == (0, 1)
    assert env.artifacts() == []  # the rolled-back publication left no inode behind
    assert_no_stranded_jobs(env.engine, env.plan.plan_hash)


def test_a_publisher_failure_is_durably_failed(env: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(*_a: Any, **_k: Any) -> Any:
        raise OSError("the artifact could not be published")

    monkeypatch.setattr(env.publisher, "publish", _boom)
    with pytest.raises(ExecutionRecordedFailureError):
        env.run()
    assert _counts(env) == (0, 1)
    assert_no_stranded_jobs(env.engine, env.plan.plan_hash)


def test_a_mutated_output_between_validation_and_publication_is_durably_failed(
    env: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The runner-reported digest is re-derived from the exact bytes about to be published."""

    class _MutatingRunner(FakeGatkRunner):
        def run(
            self,
            *,
            argv: Any,
            work_dir: Any,
            vcf_path: Any,
            inputs: Any,
            expected_runtime_bundle_sha256: str = "",
        ) -> Any:
            outcome = super().run(
                argv=argv,
                work_dir=work_dir,
                vcf_path=vcf_path,
                inputs=inputs,
                expected_runtime_bundle_sha256=expected_runtime_bundle_sha256,
            )
            vcf_path.write_bytes(vcf_path.read_bytes() + b"##mutated=true\n")
            return outcome

    result = env.run(runner=_MutatingRunner())
    assert result is not None and result.status == "FAILED"
    assert result.failure_code == "GATK_OUTPUT_INVALID"
    assert _counts(env) == (0, 1)
    assert_no_stranded_jobs(env.engine, env.plan.plan_hash)


# --------------------------------------------------------------------------- #
# rows 5/7/8 — terminal commit outcomes
# --------------------------------------------------------------------------- #
def test_a_successful_commit_is_durably_succeeded(env: Any) -> None:
    result = env.run()
    assert result is not None and result.status == "SUCCEEDED"
    assert _counts(env) == (1, 0)
    assert_no_stranded_jobs(env.engine, env.plan.plan_hash)


def test_an_ambiguous_terminal_commit_is_never_a_second_attempt(
    env: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = {"n": 0}

    def _ambiguous(_trans: Any) -> None:
        calls["n"] += 1
        raise AmbiguousExecutionCommitError("simulated ambiguous terminal COMMIT")

    monkeypatch.setattr(EX, "_commit_or_ambiguous", _ambiguous)
    with pytest.raises(AmbiguousExecutionCommitError):
        env.run()
    assert calls["n"] == 1  # exactly one commit attempt; never converted into a second terminal
    assert _counts(env) == (0, 0)
    assert env.artifacts()  # the immutable artifacts are RETAINED
    assert list(env.work_root.iterdir()) == []  # the workspace is still cleaned up


def test_a_wrapper_failure_after_a_confirmed_commit_preserves_everything(
    env: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _boom() -> None:
        raise RuntimeError("wrapper failed after the commit")

    monkeypatch.setattr(EX, "_post_commit_hook", _boom)
    with pytest.raises(PostCommitWrapperError) as excinfo:
        env.run()
    assert isinstance(excinfo.value.__cause__, RuntimeError)
    assert _counts(env) == (1, 0)
    assert len(env.artifacts()) == 2
    assert_no_stranded_jobs(env.engine, env.plan.plan_hash)


def test_an_ambiguous_failure_record_is_typed_and_not_retried(
    env: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = {"n": 0}

    def _ambiguous(_trans: Any) -> None:
        calls["n"] += 1
        raise AmbiguousExecutionCommitError("simulated ambiguous FAILED COMMIT")

    monkeypatch.setattr(EX, "_commit_or_ambiguous", _ambiguous)
    with pytest.raises(AmbiguousExecutionCommitError):
        env.run(runner=FakeGatkRunner(exit_code=5))
    assert calls["n"] == 1


# --------------------------------------------------------------------------- #
# the whole queue survives a storm of handled failures
# --------------------------------------------------------------------------- #
def test_a_mixed_failure_storm_strands_nothing(env: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """Four jobs, four different handled non-ambiguous failure modes, zero stranded rows."""
    outcomes: list[str] = []

    # 1) a recognized runner failure
    outcomes.append(env.run(runner=FakeGatkRunner(exit_code=3)).status)
    # 2) an unexpected workspace failure after RUNNING
    with monkeypatch.context() as m:
        m.setattr(EX, "_create_attempt_dir", _raise(ExecutionWorkspaceError("no workspace")))
        with pytest.raises(ExecutionRecordedFailureError):
            env.run()
    outcomes.append("FAILED")
    # 3) a success-persistence failure after GATK
    with monkeypatch.context() as m:
        m.setattr(EX, "_register_artifact", _raise(RuntimeError("registration failed")))
        with pytest.raises(ExecutionRecordedFailureError):
            env.run()
    outcomes.append("FAILED")
    # 4) a clean success
    outcomes.append(env.run().status)

    assert outcomes == ["FAILED", "FAILED", "FAILED", "SUCCEEDED"]
    assert _counts(env) == (1, 3)
    assert _statuses(env) == {"FAILED": 3, "SUCCEEDED": 1}
    assert find_nonterminal_jobs(env.engine, env.plan.plan_hash) == ()
    assert_no_stranded_jobs(env.engine, env.plan.plan_hash)
    assert list(env.work_root.iterdir()) == []  # every attempt directory was removed
    assert env.verify().status == "PASS"


def _raise(exc: BaseException) -> Any:
    def _boom(*_a: Any, **_k: Any) -> Any:
        raise exc

    return _boom


# --------------------------------------------------------------------------- #
# F7 CLOSURE — an execution-time bundle mismatch is an ORDINARY durable failure
# --------------------------------------------------------------------------- #
class _BundleMismatchRunner:
    """A runner that refuses exactly the way SubprocessGatkRunner refuses a moved bundle.

    The point is the ORCHESTRATION contract, not the hashing: a bundle mismatch raised at the run
    boundary must reach the existing durable typed FAILED path, leave the job terminal rather than
    stranded, and never be retried.
    """

    def __init__(self) -> None:
        self.seen: list[str] = []

    def run(
        self,
        *,
        argv: tuple[str, ...],
        work_dir: Path,
        vcf_path: Path,
        inputs: Any,
        expected_runtime_bundle_sha256: str,
    ) -> Any:
        self.seen.append(expected_runtime_bundle_sha256)
        raise GatkExecutionError(
            f"GATK runtime bundle before execution is {'b' * 64}, but the frozen execution "
            f"identity is {expected_runtime_bundle_sha256}"
        )


def test_an_execution_time_bundle_mismatch_is_durably_failed_and_not_retried(env: Any) -> None:
    runner = _BundleMismatchRunner()
    result = env.run(runner=runner)
    assert result is not None and result.status == "FAILED"
    # it lands in the EXISTING typed classification for a GatkExecutionError; no new failure code
    # and no new recovery path were introduced for the bundle check.
    assert result.failure_code == "GATK_NONZERO_EXIT"
    # exactly one attempt: the durable FAILED outcome IS the result, nothing re-runs it
    assert _counts(env) == (0, 1)
    assert len(runner.seen) == 1
    assert_no_stranded_jobs(env.engine, env.plan.plan_hash)
    # a second dispatch must NOT pick the terminal job back up: it either finds nothing or
    # claims a DIFFERENT pending job. The failed job is never re-executed.
    again = env.run(runner=runner)
    assert again is None or again.job_key != result.job_key
    assert_no_stranded_jobs(env.engine, env.plan.plan_hash)


def test_the_frozen_invocation_bundle_is_what_reaches_the_runner(env: Any) -> None:
    """The value the runner is asked to enforce must BE the one published in the manifest.

    No database column carries the bundle (there is no migration 0009); it is anchored by the
    append-only ``result_hash`` and published in the manifest, so that is what this compares.
    """
    import json

    from minos_engine.storage import l2f_harness_verifier as HV

    seen: list[str] = []

    class _RecordingRunner(FakeGatkRunner):
        def run(
            self,
            *,
            argv: Any,
            work_dir: Any,
            vcf_path: Any,
            inputs: Any,
            expected_runtime_bundle_sha256: str = "",
        ) -> Any:
            seen.append(expected_runtime_bundle_sha256)
            return super().run(
                argv=argv,
                work_dir=work_dir,
                vcf_path=vcf_path,
                inputs=inputs,
                expected_runtime_bundle_sha256=expected_runtime_bundle_sha256,
            )

    result = env.run(runner=_RecordingRunner())
    assert result is not None and result.status == "SUCCEEDED"
    assert len(seen) == 1 and len(seen[0]) == 64
    with env.engine.connect() as conn:
        graph = HV._read_persisted_graph(conn, env.plan)
    document = json.loads(graph.execution_results[0].manifest_bytes)
    assert document["gatk_runtime_bundle_sha256"] == seen[0]
