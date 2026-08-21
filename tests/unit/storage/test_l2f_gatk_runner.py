"""F5-B GATK invocation, runners and byte-level VCF validation — behavioral tests.

No real GATK process is ever started: the production runner is exercised only through its
executable-pinning and environment guards, and every execution path uses the deterministic
FakeGatkRunner.
"""

from __future__ import annotations

import hashlib
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
from minos_engine.storage.l2f_gatk_runner import (
    ENV_GATK_EXECUTABLE,
    ENV_GATK_EXECUTABLE_SHA256,
    ENV_GATK_VERSION,
    FakeGatkRunner,
    SubprocessGatkRunner,
    build_logical_invocation,
    region_token,
    render_execution_argv,
    validate_vcf_bytes,
)

_H = {c: c * 64 for c in "0123456789abcdef"}


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


_CONFIG = {
    "min_pruning": 2,
    "dont_use_soft_clipped_bases": False,
    "pcr_indel_model": "CONSERVATIVE",
}


# --------------------------------------------------------------------------- #
# tokenized argv
# --------------------------------------------------------------------------- #
def test_argv_is_tokenized_with_separate_flag_and_value_tokens() -> None:
    argv = render_execution_argv(
        effective_config=_CONFIG,
        inputs=_inputs(),
        reference_path="/data/ref.fa",
        bam_path="/data/in.bam",
        output_path="/work/out.vcf",
    )
    assert argv[0] == "HaplotypeCaller"
    for flag, value in (("-R", "/data/ref.fa"), ("-I", "/data/in.bam"), ("-O", "/work/out.vcf")):
        i = argv.index(flag)
        assert argv[i + 1] == value  # value is its OWN token
    assert argv[argv.index("-L") + 1] == "chr18:101-200"
    # every rendered flag/value is a separate token; nothing is concatenated.
    assert not any(" " in tok and tok.startswith("--") for tok in argv)


def test_bool_int_float_and_enum_render_as_separate_tokens() -> None:
    argv = render_execution_argv(
        effective_config={
            "min_pruning": 2,
            "active_probability_threshold": 0.002,
            "dont_use_soft_clipped_bases": False,
            "pcr_indel_model": "CONSERVATIVE",
        },
        inputs=_inputs(),
        reference_path="r",
        bam_path="b",
        output_path="o",
    )
    for value in ("2", "0.002", "false", "CONSERVATIVE"):
        assert value in argv, value


def test_spaces_and_shell_metacharacters_remain_inert_argv_data() -> None:
    nasty = "/data/a b; rm -rf / && echo $(whoami) `id` | cat > x"
    argv = render_execution_argv(
        effective_config=_CONFIG,
        inputs=_inputs(),
        reference_path=nasty,
        bam_path="b",
        output_path="o",
    )
    # the entire hostile string is ONE argv token; no shell string is ever built.
    assert nasty in argv
    assert sum(1 for tok in argv if tok == nasty) == 1
    assert not any(isinstance(tok, str) and "\x00" in tok for tok in argv)


def test_logical_argv_uses_stable_placeholders_and_is_host_independent() -> None:
    a = build_logical_invocation(
        effective_config=_CONFIG,
        inputs=_inputs(),
        gatk_executable_sha256=_H["b"],
        gatk_version="4.5.0.0",
    )
    assert ARGV_REFERENCE_PLACEHOLDER in a.logical_argv
    assert ARGV_BAM_PLACEHOLDER in a.logical_argv
    assert ARGV_OUTPUT_PLACEHOLDER in a.logical_argv
    # no host path leaks into the logical argv or its hash
    assert not any(tok.startswith("/") for tok in a.logical_argv)
    b = build_logical_invocation(
        effective_config=_CONFIG,
        inputs=_inputs(),
        gatk_executable_sha256=_H["b"],
        gatk_version="4.5.0.0",
    )
    assert a.argv_hash() == b.argv_hash()
    # region and executable identity DO change it
    c = build_logical_invocation(
        effective_config=_CONFIG,
        inputs=_inputs(region_start0=500, region_end0_exclusive=600),
        gatk_executable_sha256=_H["b"],
        gatk_version="4.5.0.0",
    )
    assert c.argv_hash() != a.argv_hash()
    d = build_logical_invocation(
        effective_config=_CONFIG,
        inputs=_inputs(),
        gatk_executable_sha256=_H["0"],
        gatk_version="4.5.0.0",
    )
    assert d.argv_hash() != a.argv_hash()


def test_region_token_is_one_based_inclusive() -> None:
    assert region_token(_inputs(region_start0=0, region_end0_exclusive=10)) == "chr18:1-10"


# --------------------------------------------------------------------------- #
# fake runner + VCF byte validation
# --------------------------------------------------------------------------- #
def _work(tmp_path: Path) -> Path:
    work = tmp_path / "job"
    work.mkdir()
    return work


def test_fake_runner_writes_real_deterministic_vcf_bytes(tmp_path: Path) -> None:
    work = _work(tmp_path)
    vcf = work / "out.vcf"
    runner = FakeGatkRunner()
    outcome = runner.run(argv=("HaplotypeCaller",), work_dir=work, vcf_path=vcf, inputs=_inputs())
    assert outcome.exit_code == 0 and outcome.vcf_size_bytes > 0
    raw = vcf.read_bytes()
    assert hashlib.sha256(raw).hexdigest() == outcome.vcf_sha256  # never a runner-claimed hash
    assert raw.startswith(b"##fileformat=VCFv4.2\n")

    # deterministic across runs
    vcf2 = work / "out2.vcf"
    second = runner.run(argv=("HaplotypeCaller",), work_dir=work, vcf_path=vcf2, inputs=_inputs())
    assert second.vcf_sha256 == outcome.vcf_sha256


def test_fake_runner_nonzero_exit_and_timeout_are_typed(tmp_path: Path) -> None:
    work = _work(tmp_path)
    with pytest.raises(GatkExecutionError):
        FakeGatkRunner(exit_code=3).run(
            argv=(), work_dir=work, vcf_path=work / "o.vcf", inputs=_inputs()
        )
    with pytest.raises(GatkTimeoutError):
        FakeGatkRunner(raise_timeout=True).run(
            argv=(), work_dir=work, vcf_path=work / "o.vcf", inputs=_inputs()
        )


def test_missing_output_is_rejected(tmp_path: Path) -> None:
    work = _work(tmp_path)
    with pytest.raises(GatkOutputError):
        FakeGatkRunner(write_output=False).run(
            argv=(), work_dir=work, vcf_path=work / "absent.vcf", inputs=_inputs()
        )


@pytest.mark.parametrize(
    ("label", "payload"),
    [
        ("empty", b""),
        ("no fileformat header", b"#CHROM\tPOS\n"),
        ("wrong fileformat", b"##fileformat=BCFv2.2\n#CHROM\tPOS\n"),
        (
            "two #CHROM headers",
            b"##fileformat=VCFv4.2\n#CHROM\tPOS\tID\n#CHROM\tPOS\tID\n",
        ),
        ("no #CHROM header", b"##fileformat=VCFv4.2\n##contig=<ID=chr18>\n"),
    ],
)
def test_malformed_vcf_is_rejected(tmp_path: Path, label: str, payload: bytes) -> None:
    work = _work(tmp_path)
    vcf = work / "o.vcf"
    with pytest.raises(GatkOutputError):
        FakeGatkRunner(override_bytes=payload).run(
            argv=(), work_dir=work, vcf_path=vcf, inputs=_inputs()
        )


def test_wrong_chromosome_record_is_rejected(tmp_path: Path) -> None:
    work = _work(tmp_path)
    payload = (
        b"##fileformat=VCFv4.2\n"
        b"#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\n"
        b"chr19\t101\t.\tA\tG\t50\tPASS\t.\n"
    )
    with pytest.raises(GatkOutputError):
        FakeGatkRunner(override_bytes=payload).run(
            argv=(), work_dir=work, vcf_path=work / "o.vcf", inputs=_inputs()
        )


def test_variant_free_region_is_accepted(tmp_path: Path) -> None:
    """A header-only VCF is legitimate: a region may contain no variants."""
    work = _work(tmp_path)
    payload = b"##fileformat=VCFv4.2\n#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\n"
    outcome = FakeGatkRunner(override_bytes=payload).run(
        argv=(), work_dir=work, vcf_path=work / "o.vcf", inputs=_inputs()
    )
    assert outcome.vcf_sha256 == hashlib.sha256(payload).hexdigest()


def test_symlinked_output_is_rejected(tmp_path: Path) -> None:
    work = _work(tmp_path)
    real = tmp_path / "elsewhere.vcf"
    real.write_bytes(b"##fileformat=VCFv4.2\n#CHROM\tPOS\n")
    link = work / "out.vcf"
    link.symlink_to(real)
    with pytest.raises(GatkOutputError):
        validate_vcf_bytes(link, work_dir=work, inputs=_inputs())


def test_output_outside_the_work_directory_is_rejected(tmp_path: Path) -> None:
    work = _work(tmp_path)
    outside = tmp_path / "outside.vcf"
    outside.write_bytes(b"##fileformat=VCFv4.2\n#CHROM\tPOS\n")
    with pytest.raises(GatkOutputError):
        validate_vcf_bytes(outside, work_dir=work, inputs=_inputs())


# --------------------------------------------------------------------------- #
# production runner guards (no GATK process is started)
# --------------------------------------------------------------------------- #
def test_subprocess_runner_requires_full_provisioning(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in (ENV_GATK_EXECUTABLE, ENV_GATK_EXECUTABLE_SHA256, ENV_GATK_VERSION):
        monkeypatch.delenv(var, raising=False)
    with pytest.raises(GatkExecutionError):
        SubprocessGatkRunner.from_env()


def test_subprocess_runner_rejects_relative_symlink_and_wrong_hash(tmp_path: Path) -> None:
    exe = tmp_path / "gatk"
    exe.write_bytes(b"#!/bin/sh\nexit 0\n")
    exe.chmod(0o755)
    real_sha = hashlib.sha256(exe.read_bytes()).hexdigest()

    # relative path -> rejected (no PATH-based discovery is ever attempted)
    with pytest.raises(GatkExecutionError):
        SubprocessGatkRunner(
            executable=Path("gatk"), expected_sha256=real_sha, expected_version="4.5.0.0"
        )._verify_executable()

    # symlink -> rejected
    link = tmp_path / "gatk-link"
    link.symlink_to(exe)
    with pytest.raises(GatkExecutionError):
        SubprocessGatkRunner(
            executable=link, expected_sha256=real_sha, expected_version="4.5.0.0"
        )._verify_executable()

    # wrong pinned hash -> rejected
    with pytest.raises(GatkExecutionError):
        SubprocessGatkRunner(
            executable=exe, expected_sha256="0" * 64, expected_version="4.5.0.0"
        )._verify_executable()

    # the correctly pinned executable verifies
    SubprocessGatkRunner(
        executable=exe, expected_sha256=real_sha, expected_version="4.5.0.0"
    )._verify_executable()


def test_subprocess_runner_never_uses_a_shell() -> None:
    """shell=False is structural: the runner passes a list argv and pins an absolute executable."""
    import inspect

    src = inspect.getsource(SubprocessGatkRunner.run)
    assert "shell=False" in src
    assert "shell=True" not in src
    assert "os.system" not in src and "check_output" not in src
