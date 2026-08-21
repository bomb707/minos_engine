"""F6 execution security — argv/environment injection, CONFIG parameter contract, VCF boundary.

Pure/local only: no database, no network, and never a real GATK process. The only subprocesses
started here are tiny repo-authored POSIX shell fixtures pinned by SHA-256, exactly as the
production runner requires.
"""

from __future__ import annotations

import hashlib
import os
import shutil
from pathlib import Path
from typing import Any

import pytest

from minos_engine.experiments.execution_contract import (
    ARGV_BAM_PLACEHOLDER,
    ARGV_OUTPUT_PLACEHOLDER,
    ARGV_REFERENCE_PLACEHOLDER,
    ExecutionInput,
    GatkExecutionError,
    GatkOutputError,
    GatkTimeoutError,
)
from minos_engine.experiments.gatk_live_space import canonicalize_live_gatk_config
from minos_engine.storage.l2f_execution import (
    ExecutionWorkspaceError,
    _create_attempt_dir,
    reject_symlinked_components,
    verify_produced_output,
)
from minos_engine.storage.l2f_gatk_runner import (
    _ENV_ALLOWLIST,
    MAX_CAPTURED_STREAM_BYTES,
    FakeGatkRunner,
    SubprocessGatkRunner,
    build_logical_invocation,
    region_token,
    render_execution_argv,
    validate_vcf_bytes,
)

_H = {c: c * 64 for c in "0123456789abcdef"}

#: environment variables that must NEVER reach a GATK child process.
FORBIDDEN_CHILD_ENV = (
    "PYTHONPATH",
    "PYTHONHOME",
    "PYTHONSTARTUP",
    "LD_PRELOAD",
    "LD_LIBRARY_PATH",
    "DYLD_INSERT_LIBRARIES",
    "MINOS_DATABASE_URL",
    "DATABASE_URL",
    "PGPASSWORD",
    "AWS_SECRET_ACCESS_KEY",
    "AWS_SESSION_TOKEN",
    "GOOGLE_APPLICATION_CREDENTIALS",
    "ANTHROPIC_API_KEY",
    "OPENAI_API_KEY",
    "MINOS_TRUTH_ROOT",
    "MINOS_SCORING_ROOT",
    "MINOS_HAPPY_PATH",
)

#: payloads that must remain inert data, never a command, a redirect or an extra flag.
INJECTION_PAYLOADS = (
    "; touch OWNED",
    "&& touch OWNED",
    "| touch OWNED",
    "$(touch OWNED)",
    "`touch OWNED`",
    "> /tmp/OWNED",
    "< /etc/passwd",
    "a b\tc\nd",
    "'quoted'",
    '"double"',
    "--another-flag",
    "-L chr19:1-2",
    "../../etc/passwd",
    "\\x00null",
)


def _inputs(**over: Any) -> ExecutionInput:
    base: dict[str, Any] = {
        "dataset_id": "minos-chr18-0001",
        "round_id": "r1",
        "chromosome": "chr18",
        "profile_id": "p1",
        "content_hash": _H["1"],
        "feature_values_hash": _H["2"],
        "bam_sha256": _H["3"],
        "bai_sha256": _H["4"],
        "reference_sha256": _H["5"],
        "fai_sha256": _H["6"],
        "dictionary_sha256": _H["7"],
        "bam_size_bytes": 1024,
        "region_hash": _H["8"],
        "region_start0": 100,
        "region_end0_exclusive": 200,
    }
    base.update(over)
    return ExecutionInput(**base)


def _accepted_config() -> dict[str, Any]:
    return dict(canonicalize_live_gatk_config({}).effective_config)


def _script(tmp_path: Path, name: str, body: str) -> Path:
    path = tmp_path / name
    path.write_text(body, encoding="utf-8")
    path.chmod(0o700)
    return path.resolve()


def _runner_for(script: Path, **over: Any) -> SubprocessGatkRunner:
    return SubprocessGatkRunner(
        executable=script,
        expected_sha256=hashlib.sha256(script.read_bytes()).hexdigest(),
        expected_version="test-fixture",
        **over,
    )


def _work(tmp_path: Path, name: str = "work") -> Path:
    work = tmp_path / name
    work.mkdir()
    return work


# --------------------------------------------------------------------------- #
# E1-E3 — the fixed invocation skeleton is never caller-replaceable
# --------------------------------------------------------------------------- #
FIXED_SKELETON = ("HaplotypeCaller", "-R", "-I", "-L", "-O")


def test_the_fixed_argv_skeleton_always_leads_and_is_tokenized() -> None:
    argv = render_execution_argv(
        effective_config=_accepted_config(),
        inputs=_inputs(),
        reference_path="/data/ref.fa",
        bam_path="/data/in.bam",
        output_path="/work/out.vcf",
    )
    assert argv[:9] == (
        "HaplotypeCaller",
        "-R",
        "/data/ref.fa",
        "-I",
        "/data/in.bam",
        "-L",
        region_token(_inputs()),
        "-O",
        "/work/out.vcf",
    )
    # each fixed token appears EXACTLY once: no config flag can add a second one.
    for token in FIXED_SKELETON:
        assert argv.count(token) == 1


@pytest.mark.parametrize(
    "key", ["R", "I", "L", "O", "input", "output", "reference", "intervals", "tool", "executable"]
)
def test_caller_config_cannot_replace_the_fixed_invocation(key: str) -> None:
    """The CONFIG contract rejects the keys that would redirect input, reference or output."""
    with pytest.raises(Exception):  # noqa: B017 - any typed rejection from the contract
        canonicalize_live_gatk_config({**_accepted_config(), key: "/attacker/path"})


def test_the_logical_argv_uses_placeholders_and_is_host_independent() -> None:
    invocation = build_logical_invocation(
        effective_config=_accepted_config(),
        inputs=_inputs(),
        gatk_executable_sha256=_H["b"],
        gatk_version="4.5.0.0",
    )
    assert ARGV_REFERENCE_PLACEHOLDER in invocation.logical_argv
    assert ARGV_BAM_PLACEHOLDER in invocation.logical_argv
    assert ARGV_OUTPUT_PLACEHOLDER in invocation.logical_argv
    for token in invocation.logical_argv:
        assert not token.startswith("/")  # no host path survives into the logical identity


# --------------------------------------------------------------------------- #
# E4/E5 — the live GATK domain governs every rendered value
# --------------------------------------------------------------------------- #
def test_typed_values_render_as_separate_argv_tokens() -> None:
    config = _accepted_config()
    argv = render_execution_argv(
        effective_config=config,
        inputs=_inputs(),
        reference_path="/r.fa",
        bam_path="/i.bam",
        output_path="/o.vcf",
    )
    flags = argv[9:]
    assert len(flags) % 2 == 0  # strictly [flag, value, flag, value, ...]
    rendered = dict(zip(flags[0::2], flags[1::2], strict=True))
    assert all(f.startswith("-") for f in flags[0::2])
    for value in rendered.values():
        assert isinstance(value, str) and value != ""
    # booleans render explicitly, never as a bare presence flag
    booleans = {k: v for k, v in config.items() if isinstance(v, bool)}
    assert booleans
    assert {"true", "false"} & set(rendered.values())


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("min_pruning", 1),  # below the accepted minimum
        ("min_pruning", 11),  # above the accepted maximum
        ("min_pruning", True),  # bool-as-int confusion
        ("min_pruning", "2"),  # string-as-int confusion
        ("min_pruning", 2.5),  # float-as-int confusion
        ("emit_ref_confidence", "NOT_A_MODE"),  # unsupported enum
        ("emit_ref_confidence", 1),  # int-as-enum confusion
        ("standard_min_confidence_threshold_for_calling", float("nan")),
        ("standard_min_confidence_threshold_for_calling", float("inf")),
        ("standard_min_confidence_threshold_for_calling", -float("inf")),
        ("standard_min_confidence_threshold_for_calling", 1000.0),  # out of range
        ("totally_unknown_parameter", 1),  # unknown key
    ],
)
def test_invalid_parameters_are_rejected_before_any_subprocess(key: str, value: Any) -> None:
    with pytest.raises(Exception):  # noqa: B017 - any typed rejection from the contract
        canonicalize_live_gatk_config({**_accepted_config(), key: value})


def test_the_accepted_config_is_the_valid_control() -> None:
    canonical = canonicalize_live_gatk_config(_accepted_config())
    assert canonical.effective_config == _accepted_config()


@pytest.mark.parametrize("payload", INJECTION_PAYLOADS)
def test_injection_payloads_are_rejected_by_the_parameter_contract(payload: str) -> None:
    """No accepted GATK parameter accepts a free-form string, so payloads never reach argv."""
    for key in ("min_pruning", "emit_ref_confidence"):
        with pytest.raises(Exception):  # noqa: B017 - any typed rejection
            canonicalize_live_gatk_config({**_accepted_config(), key: payload})


@pytest.mark.parametrize("payload", INJECTION_PAYLOADS)
def test_injection_payloads_in_host_paths_stay_inert_argv_tokens(payload: str) -> None:
    """Even a hostile PATH value is one argv token — never a shell fragment."""
    argv = render_execution_argv(
        effective_config=_accepted_config(),
        inputs=_inputs(),
        reference_path=payload,
        bam_path="/i.bam",
        output_path="/o.vcf",
    )
    assert argv[2] == payload  # exactly one token, byte-for-byte
    assert argv.count(payload) == 1


# --------------------------------------------------------------------------- #
# E1/E7 — shell=False and the child environment allowlist
# --------------------------------------------------------------------------- #
_ECHO_ENV = "#!/bin/sh\nenv\nexit 5\n"
_ECHO_ARGS = '#!/bin/sh\nprintf "%s\\n" "$@"\nexit 5\n'


@pytest.mark.skipif(not Path("/bin/sh").exists(), reason="POSIX shell required")
def test_the_child_environment_is_an_explicit_allowlist(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in FORBIDDEN_CHILD_ENV:
        monkeypatch.setenv(name, f"leaked-{name}")
    script = _script(tmp_path, "envdump.sh", _ECHO_ENV)
    work = _work(tmp_path)
    with pytest.raises(GatkExecutionError):
        _runner_for(script, timeout_seconds=60).run(
            argv=(), work_dir=work, vcf_path=work / "o.vcf", inputs=_inputs()
        )
    dumped = (work / "gatk.stdout").read_text(encoding="utf-8")
    names = {line.split("=", 1)[0] for line in dumped.splitlines() if "=" in line}
    for forbidden in FORBIDDEN_CHILD_ENV:
        assert forbidden not in names, forbidden
        assert f"leaked-{forbidden}" not in dumped
    # PWD/SHLVL/_ are set by the shell fixture ITSELF, not inherited from the parent.
    assert names - {"PWD", "SHLVL", "_"} <= set(_ENV_ALLOWLIST)


@pytest.mark.skipif(not Path("/bin/sh").exists(), reason="POSIX shell required")
@pytest.mark.parametrize("payload", INJECTION_PAYLOADS)
def test_argv_payloads_never_reach_a_shell(tmp_path: Path, payload: str) -> None:
    marker = tmp_path / "OWNED"
    script = _script(tmp_path, "args.sh", _ECHO_ARGS)
    work = _work(tmp_path)
    with pytest.raises(GatkExecutionError):
        _runner_for(script, timeout_seconds=60).run(
            argv=(payload,), work_dir=work, vcf_path=work / "o.vcf", inputs=_inputs()
        )
    assert not marker.exists()
    assert not (work / "OWNED").exists()
    assert payload.splitlines()[0] in (work / "gatk.stdout").read_text(encoding="utf-8")


# --------------------------------------------------------------------------- #
# E2 — the executable is pinned by absolute path and SHA-256
# --------------------------------------------------------------------------- #
def test_a_relative_executable_is_refused(tmp_path: Path) -> None:
    work = _work(tmp_path)
    runner = SubprocessGatkRunner(
        executable=Path("gatk"), expected_sha256=_H["0"], expected_version="v"
    )
    with pytest.raises(GatkExecutionError, match="absolute"):
        runner.run(argv=(), work_dir=work, vcf_path=work / "o.vcf", inputs=_inputs())


def test_a_symlinked_executable_is_refused(tmp_path: Path) -> None:
    real = _script(tmp_path, "real.sh", _ECHO_ARGS)
    link = tmp_path / "link.sh"
    link.symlink_to(real)
    work = _work(tmp_path)
    runner = SubprocessGatkRunner(
        executable=link,
        expected_sha256=hashlib.sha256(real.read_bytes()).hexdigest(),
        expected_version="v",
    )
    with pytest.raises(GatkExecutionError, match="symlink"):
        runner.run(argv=(), work_dir=work, vcf_path=work / "o.vcf", inputs=_inputs())


def test_a_swapped_executable_fails_its_sha256(tmp_path: Path) -> None:
    script = _script(tmp_path, "gatk.sh", _ECHO_ARGS)
    runner = _runner_for(script)
    script.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")  # swapped after pinning
    work = _work(tmp_path)
    with pytest.raises(GatkExecutionError, match="sha256"):
        runner.run(argv=(), work_dir=work, vcf_path=work / "o.vcf", inputs=_inputs())


def test_the_pinned_version_is_provisioned_metadata_not_a_probe(tmp_path: Path) -> None:
    """The runner NEVER probes ``--version``: the version is provisioned metadata BOUND to the
    verified executable SHA-256. This test documents that honestly rather than overclaiming."""
    script = _script(tmp_path, "gatk.sh", _ECHO_ARGS)
    runner = _runner_for(script)
    assert runner.expected_version == "test-fixture"
    work = _work(tmp_path)
    with pytest.raises(GatkExecutionError):
        runner.run(argv=(), work_dir=work, vcf_path=work / "o.vcf", inputs=_inputs())
    # the child was invoked with the EXECUTION argv only - no version probe was ever issued.
    assert "--version" not in (work / "gatk.stdout").read_text(encoding="utf-8")


# --------------------------------------------------------------------------- #
# E8/E9 — bounded streams and process-group timeout
# --------------------------------------------------------------------------- #
_NOISY = """#!/bin/sh
i=0
while [ $i -lt 400 ]; do
  printf '%s\\n' "PAYLOAD"
  printf '%s\\n' "PAYLOAD" >&2
  i=$((i+1))
done
exit 5
"""

_FORKING_SLEEPER = """#!/bin/sh
sleep 300 &
echo $! > child.pid
sleep 300
"""


@pytest.mark.skipif(not Path("/bin/sh").exists(), reason="POSIX shell required")
def test_streams_stay_bounded_while_the_process_runs(tmp_path: Path) -> None:
    script = _script(tmp_path, "noisy.sh", _NOISY.replace("PAYLOAD", "x" * 1000))
    work = _work(tmp_path)
    limit = 2048
    with pytest.raises(GatkExecutionError):
        _runner_for(script, timeout_seconds=120, max_captured_stream_bytes=limit).run(
            argv=(), work_dir=work, vcf_path=work / "o.vcf", inputs=_inputs()
        )
    assert (work / "gatk.stdout").stat().st_size <= limit
    assert (work / "gatk.stderr").stat().st_size <= limit
    assert limit < MAX_CAPTURED_STREAM_BYTES


@pytest.mark.skipif(not Path("/bin/sh").exists(), reason="POSIX shell required")
def test_a_timeout_terminates_the_whole_process_group(tmp_path: Path) -> None:
    script = _script(tmp_path, "sleeper.sh", _FORKING_SLEEPER)
    work = _work(tmp_path)
    with pytest.raises(GatkTimeoutError):
        _runner_for(script, timeout_seconds=2).run(
            argv=(), work_dir=work, vcf_path=work / "o.vcf", inputs=_inputs()
        )
    raw = (work / "child.pid").read_text(encoding="utf-8").strip()
    assert raw
    # the forked grandchild is gone too: the ENTIRE process group was terminated.
    with pytest.raises(ProcessLookupError):
        os.kill(int(raw), 0)


# --------------------------------------------------------------------------- #
# D — workspace and output inode safety
# --------------------------------------------------------------------------- #
def test_a_symlinked_work_root_is_refused(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real, target_is_directory=True)
    with pytest.raises(ExecutionWorkspaceError, match="symlink"):
        _create_attempt_dir(link, job_id="j", attempt_id="a")


def test_a_symlinked_intermediate_component_is_refused(tmp_path: Path) -> None:
    real = tmp_path / "real"
    (real / "inner").mkdir(parents=True)
    link = tmp_path / "mid"
    link.symlink_to(real, target_is_directory=True)
    with pytest.raises(ExecutionWorkspaceError, match="symlink"):
        reject_symlinked_components(link / "inner")


def test_validation_failure_removes_only_the_created_inode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _work(tmp_path)
    sibling = root / "keep-me"
    sibling.mkdir()
    (sibling / "data").write_bytes(b"x")
    monkeypatch.setattr("minos_engine.storage.l2f_execution.ATTEMPT_DIR_MODE", 0o700, raising=True)

    real_lstat = os.lstat
    calls = {"n": 0}

    def _lying_lstat(path: Any, **kw: Any) -> Any:
        info = real_lstat(path, **kw)
        calls["n"] += 1
        return info

    monkeypatch.setattr(os, "lstat", _lying_lstat)
    # force the ownership check to fail by claiming a different effective uid
    monkeypatch.setattr(os, "geteuid", lambda: real_lstat(root).st_uid + 12345)
    with pytest.raises(ExecutionWorkspaceError, match="owned"):
        _create_attempt_dir(root, job_id="j", attempt_id="a")
    # the created inode is gone; the untouched sibling survives intact
    assert not (root / "l2f-j-a").exists()
    assert (sibling / "data").read_bytes() == b"x"


def test_cleanup_never_deletes_a_replacement_directory(tmp_path: Path) -> None:
    from minos_engine.storage.l2f_execution import _remove_attempt_dir

    root = _work(tmp_path)
    workspace = _create_attempt_dir(root, job_id="j", attempt_id="a")
    path = workspace.path
    # an attacker replaces the attempt path with a DIFFERENT directory holding real data
    shutil.rmtree(path)
    path.mkdir()
    (path / "victim").write_bytes(b"precious")
    _remove_attempt_dir(workspace)
    assert (path / "victim").read_bytes() == b"precious"  # the replacement is untouched


def test_cleanup_never_follows_a_replacement_symlink(tmp_path: Path) -> None:
    from minos_engine.storage.l2f_execution import _remove_attempt_dir

    root = _work(tmp_path)
    victim = tmp_path / "victim"
    victim.mkdir()
    (victim / "data").write_bytes(b"precious")
    workspace = _create_attempt_dir(root, job_id="j", attempt_id="a")
    shutil.rmtree(workspace.path)
    workspace.path.symlink_to(victim, target_is_directory=True)
    _remove_attempt_dir(workspace)
    assert (victim / "data").read_bytes() == b"precious"


def test_cleanup_removes_the_real_attempt_directory(tmp_path: Path) -> None:
    from minos_engine.storage.l2f_execution import _remove_attempt_dir

    root = _work(tmp_path)
    workspace = _create_attempt_dir(root, job_id="j", attempt_id="a")
    (workspace.path / "scratch").write_bytes(b"x")
    _remove_attempt_dir(workspace)
    assert not workspace.path.exists()


def test_a_hard_linked_output_is_refused(tmp_path: Path) -> None:
    root = _work(tmp_path)
    workspace = _create_attempt_dir(root, job_id="j", attempt_id="a")
    out = workspace.path / "output.vcf"
    out.write_bytes(b"##fileformat=VCFv4.2\n")
    os.link(out, workspace.path / "hardlink.vcf")
    with pytest.raises(GatkOutputError, match="links"):
        verify_produced_output(out, workspace)


def test_an_output_outside_the_attempt_directory_is_refused(tmp_path: Path) -> None:
    root = _work(tmp_path)
    workspace = _create_attempt_dir(root, job_id="j", attempt_id="a")
    outside = root / "elsewhere.vcf"
    outside.write_bytes(b"##fileformat=VCFv4.2\n")
    with pytest.raises(GatkOutputError):
        verify_produced_output(outside, workspace)


def test_a_non_regular_output_is_refused(tmp_path: Path) -> None:
    root = _work(tmp_path)
    workspace = _create_attempt_dir(root, job_id="j", attempt_id="a")
    fifo = workspace.path / "output.vcf"
    os.mkfifo(fifo)
    with pytest.raises(GatkOutputError, match="regular"):
        verify_produced_output(fifo, workspace)


def test_a_symlinked_output_is_refused(tmp_path: Path) -> None:
    root = _work(tmp_path)
    workspace = _create_attempt_dir(root, job_id="j", attempt_id="a")
    target = tmp_path / "target.vcf"
    target.write_bytes(b"##fileformat=VCFv4.2\n")
    link = workspace.path / "output.vcf"
    link.symlink_to(target)
    with pytest.raises(GatkOutputError, match="symlink"):
        verify_produced_output(link, workspace)


def test_a_replaced_attempt_directory_invalidates_the_output(tmp_path: Path) -> None:
    root = _work(tmp_path)
    workspace = _create_attempt_dir(root, job_id="j", attempt_id="a")
    out = workspace.path / "output.vcf"
    out.write_bytes(b"##fileformat=VCFv4.2\n")
    saved = out.read_bytes()
    shutil.rmtree(workspace.path)
    workspace.path.mkdir()
    (workspace.path / "output.vcf").write_bytes(saved)
    with pytest.raises(GatkOutputError):
        verify_produced_output(workspace.path / "output.vcf", workspace)


def test_a_genuine_output_is_the_valid_control(tmp_path: Path) -> None:
    root = _work(tmp_path)
    workspace = _create_attempt_dir(root, job_id="j", attempt_id="a")
    out = workspace.path / "output.vcf"
    out.write_bytes(b"##fileformat=VCFv4.2\n")
    verify_produced_output(out, workspace)  # must not raise


# --------------------------------------------------------------------------- #
# F (VCF boundary) — the runner-reported hash is never trusted
# --------------------------------------------------------------------------- #
_VALID = (
    b"##fileformat=VCFv4.2\n"
    b"#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\tFORMAT\tSAMPLE\n"
    b"chr18\t150\t.\tA\tG\t50.0\tPASS\t.\tGT\t0/1\n"
)


def test_a_valid_vcf_is_the_control(tmp_path: Path) -> None:
    work = _work(tmp_path)
    out = work / "o.vcf"
    out.write_bytes(_VALID)
    sha, size = validate_vcf_bytes(out, work_dir=work, inputs=_inputs())
    assert sha == hashlib.sha256(_VALID).hexdigest() and size == len(_VALID)


def test_the_validated_digest_is_computed_from_the_actual_bytes(tmp_path: Path) -> None:
    """FakeGatkRunner reports what validate_vcf_bytes measured, never a self-declared hash."""
    work = _work(tmp_path)
    outcome = FakeGatkRunner(override_bytes=_VALID).run(
        argv=(), work_dir=work, vcf_path=work / "o.vcf", inputs=_inputs()
    )
    assert outcome.vcf_sha256 == hashlib.sha256(_VALID).hexdigest()
    assert outcome.vcf_size_bytes == len(_VALID)


def test_a_missing_output_is_refused(tmp_path: Path) -> None:
    work = _work(tmp_path)
    with pytest.raises(GatkOutputError):
        validate_vcf_bytes(work / "absent.vcf", work_dir=work, inputs=_inputs())


def test_an_empty_output_is_refused(tmp_path: Path) -> None:
    work = _work(tmp_path)
    out = work / "o.vcf"
    out.write_bytes(b"")
    with pytest.raises(GatkOutputError):
        validate_vcf_bytes(out, work_dir=work, inputs=_inputs())
