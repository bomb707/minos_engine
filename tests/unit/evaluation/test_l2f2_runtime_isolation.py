"""L2-F2-A runtime isolation — container containment and per-attempt output isolation.

The Docker controls run against a REAL process boundary: a fake ``docker`` executable is placed
on ``PATH`` and records the exact argv it was invoked with. Nothing is mocked, so these prove the
argv and the control flow the production runner actually uses — not a description of them.

No hap.py image is pulled, no container is started, no real Docker daemon is required.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from minos_engine.evaluation.happy_runner import (
    CONTAINER_NAME_PREFIX,
    FakeHappyRunner,
    HappyContainmentError,
    HappyExecutionError,
    HappyTimeoutError,
    SubprocessDockerHappyRunner,
    build_happy_argv,
    new_container_name,
)

_IMAGE = "genonet/hap-py@sha256:" + "0" * 64


def _fake_docker(tmp_path: Path, *, run_body: str, inspect_exit: int = 1) -> Path:
    """Install a fake ``docker`` on PATH that logs every invocation's argv."""
    bin_dir = tmp_path / "fakebin"
    bin_dir.mkdir(exist_ok=True)
    log = tmp_path / "docker.log"
    script = bin_dir / "docker"
    script.write_text(
        "#!/bin/sh\n"
        f'printf "%s\\n" "$*" >> "{log}"\n'
        'case "$1" in\n'
        f"  run) {run_body} ;;\n"
        "  rm) exit 0 ;;\n"
        f"  inspect) exit {inspect_exit} ;;\n"
        "esac\n",
        encoding="utf-8",
    )
    script.chmod(0o755)
    return log


def _invocations(log: Path) -> list[list[str]]:
    if not log.exists():
        return []
    return [line.split() for line in log.read_text(encoding="utf-8").splitlines() if line.strip()]


def _run(tmp_path: Path, runner: SubprocessDockerHappyRunner) -> None:
    work = tmp_path / "work"
    work.mkdir(exist_ok=True)
    runner.run(
        truth_vcf=tmp_path / "t" / "truth.vcf.gz",
        query_vcf=tmp_path / "q" / "query.vcf",
        reference=tmp_path / "r" / "chr18.fa",
        region_bed=tmp_path / "b" / "regions.bed",
        output_prefix=work / "happy",
        work_dir=work,
    )


@pytest.fixture
def on_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PATH", f"{tmp_path / 'fakebin'}:{os.environ.get('PATH', '')}")


# --------------------------------------------------------------------------- #
# CONTAINER IDENTITY
# --------------------------------------------------------------------------- #
def test_every_invocation_gets_its_own_container_identity() -> None:
    names = {new_container_name() for _ in range(64)}
    assert len(names) == 64
    for name in names:
        assert name.startswith(CONTAINER_NAME_PREFIX)
        assert "/" not in name and " " not in name


def test_the_production_argv_names_the_container(tmp_path: Path) -> None:
    name = new_container_name()
    argv = build_happy_argv(
        image=_IMAGE,
        truth_vcf=tmp_path / "t" / "truth.vcf.gz",
        query_vcf=tmp_path / "q" / "query.vcf",
        reference=tmp_path / "r" / "chr18.fa",
        region_bed=tmp_path / "b" / "regions.bed",
        output_prefix=Path("out"),
        work_dir=tmp_path,
        container_name=name,
    )
    assert "--name" in argv
    assert argv[argv.index("--name") + 1] == name
    # still network-isolated and digest-pinned
    assert "--network" in argv and argv[argv.index("--network") + 1] == "none"


def test_an_unsafe_container_name_is_refused(tmp_path: Path) -> None:
    for bad in ("evil/name", "docker-compose-thing", "minos-happy-a/b"):
        with pytest.raises(HappyExecutionError, match="unsafe hap.py container name"):
            build_happy_argv(
                image=_IMAGE,
                truth_vcf=tmp_path / "t" / "truth.vcf.gz",
                query_vcf=tmp_path / "q" / "query.vcf",
                reference=tmp_path / "r" / "chr18.fa",
                region_bed=tmp_path / "b" / "regions.bed",
                output_prefix=Path("out"),
                work_dir=tmp_path,
                container_name=bad,
            )


# --------------------------------------------------------------------------- #
# TIMEOUT CONTAINMENT
# --------------------------------------------------------------------------- #
@pytest.mark.usefixtures("on_path")
def test_a_timeout_explicitly_removes_this_invocations_container(tmp_path: Path) -> None:
    """subprocess only kills the Docker CLIENT; the container must be stopped explicitly."""
    log = _fake_docker(tmp_path, run_body="sleep 30")
    runner = SubprocessDockerHappyRunner(image=_IMAGE, timeout_seconds=1)

    with pytest.raises(HappyTimeoutError, match="container was removed"):
        _run(tmp_path, runner)

    calls = _invocations(log)
    assert [c[0] for c in calls] == ["run", "rm", "inspect"]
    started = calls[0][calls[0].index("--name") + 1]
    # cleanup targets EXACTLY the container this invocation started
    assert calls[1] == ["rm", "--force", started]
    assert calls[2] == ["inspect", "--type", "container", started]


@pytest.mark.usefixtures("on_path")
def test_cleanup_can_never_reach_another_invocations_container(tmp_path: Path) -> None:
    log = _fake_docker(tmp_path, run_body="sleep 30")
    runner = SubprocessDockerHappyRunner(image=_IMAGE, timeout_seconds=1)

    for _attempt in range(2):
        with pytest.raises(HappyTimeoutError):
            _run(tmp_path, runner)

    calls = _invocations(log)
    first_name = calls[0][calls[0].index("--name") + 1]
    second_name = calls[3][calls[3].index("--name") + 1]
    assert first_name != second_name
    assert calls[1][-1] == first_name and calls[2][-1] == first_name
    assert calls[4][-1] == second_name and calls[5][-1] == second_name


@pytest.mark.usefixtures("on_path")
def test_a_container_that_survives_removal_is_a_containment_failure(tmp_path: Path) -> None:
    """If ``inspect`` still finds it, the runner must NOT claim a clean timeout."""
    log = _fake_docker(tmp_path, run_body="sleep 30", inspect_exit=0)
    runner = SubprocessDockerHappyRunner(image=_IMAGE, timeout_seconds=1)

    with pytest.raises(HappyContainmentError, match="still exists after forced removal"):
        _run(tmp_path, runner)

    assert [c[0] for c in _invocations(log)] == ["run", "rm", "inspect"]


@pytest.mark.usefixtures("on_path")
def test_a_hanging_cleanup_is_a_containment_failure_not_a_timeout(tmp_path: Path) -> None:
    """Cleanup is itself bounded: an unbounded ``docker rm`` would restore the original hang."""
    bin_dir = tmp_path / "fakebin"
    bin_dir.mkdir(exist_ok=True)
    script = bin_dir / "docker"
    script.write_text("#!/bin/sh\nsleep 30\n", encoding="utf-8")
    script.chmod(0o755)
    runner = SubprocessDockerHappyRunner(image=_IMAGE, timeout_seconds=1, cleanup_timeout_seconds=1)

    with pytest.raises(HappyContainmentError, match="exceeded"):
        _run(tmp_path, runner)


@pytest.mark.usefixtures("on_path")
def test_cleanup_never_invokes_a_shell(tmp_path: Path) -> None:
    """A shell-quoted container name would be an injection surface; argv is fixed."""
    import subprocess

    log = _fake_docker(tmp_path, run_body="sleep 30")
    seen: list[dict[str, object]] = []
    real = subprocess.run

    def _record(*args: object, **kwargs: object) -> object:
        seen.append(dict(kwargs))
        return real(*args, **kwargs)  # type: ignore[arg-type]

    runner = SubprocessDockerHappyRunner(image=_IMAGE, timeout_seconds=1)
    subprocess.run = _record  # type: ignore[assignment]
    try:
        with pytest.raises(HappyTimeoutError):
            _run(tmp_path, runner)
    finally:
        subprocess.run = real  # type: ignore[assignment]

    assert len(seen) == 3, "run + rm + inspect"
    for call in seen:
        assert call.get("shell", False) is False
        assert call.get("timeout") is not None, "every docker call is bounded"
    assert _invocations(log)[1][0] == "rm"


# --------------------------------------------------------------------------- #
# NORMAL EXIT — no destructive cleanup
# --------------------------------------------------------------------------- #
@pytest.mark.usefixtures("on_path")
def test_a_successful_invocation_issues_no_destructive_cleanup(tmp_path: Path) -> None:
    log = _fake_docker(tmp_path, run_body="exit 0")
    runner = SubprocessDockerHappyRunner(image=_IMAGE, timeout_seconds=30)

    _run(tmp_path, runner)

    assert [c[0] for c in _invocations(log)] == ["run"], "--rm already reaped the container"


@pytest.mark.usefixtures("on_path")
def test_an_ordinary_nonzero_exit_issues_no_destructive_cleanup(tmp_path: Path) -> None:
    log = _fake_docker(tmp_path, run_body="exit 3")
    runner = SubprocessDockerHappyRunner(image=_IMAGE, timeout_seconds=30)

    with pytest.raises(HappyExecutionError, match="exited with code 3"):
        _run(tmp_path, runner)

    assert [c[0] for c in _invocations(log)] == ["run"]


def test_a_start_failure_still_attempts_containment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The client can fail AFTER the daemon created the container, so cleanup is attempted."""
    empty = tmp_path / "emptybin"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))  # no `docker` anywhere -> OSError from exec
    work = tmp_path / "work"
    work.mkdir(exist_ok=True)
    runner = SubprocessDockerHappyRunner(image=_IMAGE, timeout_seconds=5)

    # containment cannot be established without a docker client, so the runner says so rather
    # than reporting a plain start failure and moving on.
    with pytest.raises(HappyContainmentError, match="could not be started"):
        runner.run(
            truth_vcf=tmp_path / "t" / "truth.vcf.gz",
            query_vcf=tmp_path / "q" / "query.vcf",
            reference=tmp_path / "r" / "chr18.fa",
            region_bed=tmp_path / "b" / "regions.bed",
            output_prefix=work / "happy",
            work_dir=work,
        )


# --------------------------------------------------------------------------- #
# ATTEMPT WORKSPACE — the shared audited core, under evaluation's own error
# --------------------------------------------------------------------------- #
def test_the_evaluation_attempt_directory_is_fresh_and_exactly_private(tmp_path: Path) -> None:
    from minos_engine.evaluation.orchestrator import EvaluationWorkspaceError
    from minos_engine.storage.attempt_workspace import (
        create_attempt_workspace,
        remove_attempt_workspace,
    )

    root = tmp_path / "work_root"
    root.mkdir()
    workspace = create_attempt_workspace(root, name="eval-probe", error=EvaluationWorkspaceError)
    try:
        info = workspace.path.lstat()
        assert stat.S_ISDIR(info.st_mode)
        assert not stat.S_ISLNK(info.st_mode)
        assert info.st_uid == os.getuid()
        assert stat.S_IMODE(info.st_mode) == 0o700
        assert workspace.path.parent == root
        # never reused: a second attempt under the same name fails closed
        with pytest.raises(EvaluationWorkspaceError, match="already exists"):
            create_attempt_workspace(root, name="eval-probe", error=EvaluationWorkspaceError)
    finally:
        remove_attempt_workspace(workspace)
    assert not workspace.path.exists()


def test_a_symlinked_work_root_is_refused(tmp_path: Path) -> None:
    from minos_engine.evaluation.orchestrator import EvaluationWorkspaceError
    from minos_engine.storage.attempt_workspace import create_attempt_workspace

    real = tmp_path / "real_root"
    real.mkdir()
    link = tmp_path / "link_root"
    link.symlink_to(real)

    with pytest.raises(EvaluationWorkspaceError, match="symlink"):
        create_attempt_workspace(link, name="eval-probe", error=EvaluationWorkspaceError)


def test_the_fake_runner_writes_only_into_the_work_directory_it_is_given(
    tmp_path: Path,
) -> None:
    work = tmp_path / "attempt"
    work.mkdir()
    runner = FakeHappyRunner(
        written_files={"happy.summary.csv": "Type,Filter\n"},
        written_bytes={"happy.vcf.gz": b"\x1f\x8b"},
    )
    runner.run(
        truth_vcf=tmp_path / "truth.vcf.gz",
        query_vcf=tmp_path / "q.vcf",
        reference=tmp_path / "r.fa",
        region_bed=tmp_path / "b.bed",
        output_prefix=work / "happy",
        work_dir=work,
    )
    assert sorted(p.name for p in work.iterdir()) == ["happy.summary.csv", "happy.vcf.gz"]
    assert not (tmp_path / "happy.summary.csv").exists()
