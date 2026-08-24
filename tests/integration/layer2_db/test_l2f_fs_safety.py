"""F6 filesystem-safety corrective — descriptor-bound cleanup and output acquisition.

Two check/use races are closed here and both are exercised deterministically:

* cleanup previously resolved the attempt PATHNAME again after its last identity check, so a
  replacement installed in that window could be recursively deleted;
* output validation, hashing and publication previously observed the pathname several times, so
  they could describe different objects.

Every execution uses the deterministic FakeGatkRunner; no GATK process is ever started.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import os
from pathlib import Path
from typing import Any

import pytest

from minos_engine.experiments.execution_contract import GatkOutputError
from minos_engine.storage import l2f_execution as EX
from minos_engine.storage.l2f_execution import (
    OUTPUT_VCF_NAME,
    AttemptWorkspace,
    _create_attempt_dir,
    _remove_attempt_dir,
    acquire_produced_output,
    assert_no_stranded_jobs,
)
from minos_engine.storage.l2f_gatk_runner import FakeGatkRunner
from tests.integration.layer2_db.test_l2f_execution_corrective import env as _env_fixture

env = _env_fixture

_RESULTS = "experiments.l2f_execution_results"
_FAILURES = "experiments.l2f_execution_failures"

_VALID = (
    b"##fileformat=VCFv4.2\n"
    b"#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\tFORMAT\tSAMPLE\n"
    b"chr18\t150\t.\tA\tG\t50.0\tPASS\t.\tGT\t0/1\n"
)


def _inputs() -> Any:
    from minos_engine.experiments.execution_contract import ExecutionInput

    h = {c: c * 64 for c in "0123456789abcdef"}
    return ExecutionInput(
        dataset_id="d1",
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


def _work(tmp_path: Path) -> Path:
    root = tmp_path / "work"
    root.mkdir()
    return root


def _counts(env: Any) -> tuple[int, int]:
    from sqlalchemy import text

    with env.engine.connect() as c:
        r = int(c.execute(text(f"SELECT count(*) FROM {_RESULTS}")).scalar_one())  # noqa: S608
        f = int(c.execute(text(f"SELECT count(*) FROM {_FAILURES}")).scalar_one())  # noqa: S608
    return r, f


# --------------------------------------------------------------------------- #
# B — the cleanup check/use race
# --------------------------------------------------------------------------- #
def test_a_replacement_installed_after_the_last_identity_check_survives(tmp_path: Path) -> None:
    """THE race this corrective closes.

    The replacement is installed at the attempt pathname *exactly* after the final identity
    validation returns and before any deletion begins. A pathname-based ``rmtree`` would then
    traverse the replacement and destroy the victim; descriptor-relative cleanup cannot, because
    it never re-resolves the pathname.
    """
    root = _work(tmp_path)
    workspace = _create_attempt_dir(root, job_id="job-race", attempt_id="aaa")
    (workspace.path / "ours.txt").write_bytes(b"ours")
    path = workspace.path
    real_still_ours = AttemptWorkspace.still_ours
    swapped = {"done": False}

    def _swap_after_validation(self: AttemptWorkspace) -> bool:
        verdict = real_still_ours(self)
        if verdict and not swapped["done"]:
            swapped["done"] = True
            # ...the instant after the identity check passed, replace the pathname.
            os.unlink(path / "ours.txt")
            os.unlink(path / self.sentinel)
            os.rmdir(path)
            path.mkdir(mode=0o700)
            (path / "victim.txt").write_bytes(b"precious")
        return verdict

    AttemptWorkspace.still_ours = _swap_after_validation  # type: ignore[method-assign]
    try:
        _remove_attempt_dir(workspace)
    finally:
        AttemptWorkspace.still_ours = real_still_ours  # type: ignore[method-assign]

    assert swapped["done"], "the race window was never entered"
    assert path.is_dir(), "the replacement directory was deleted"
    assert (path / "victim.txt").read_bytes() == b"precious"


def test_a_replacement_symlink_installed_in_the_race_window_survives(tmp_path: Path) -> None:
    root = _work(tmp_path)
    victim = tmp_path / "victim"
    victim.mkdir()
    (victim / "data").write_bytes(b"precious")
    workspace = _create_attempt_dir(root, job_id="job-link", attempt_id="bbb")
    path = workspace.path
    real_still_ours = AttemptWorkspace.still_ours
    swapped = {"done": False}

    def _swap(self: AttemptWorkspace) -> bool:
        verdict = real_still_ours(self)
        if verdict and not swapped["done"]:
            swapped["done"] = True
            os.unlink(path / self.sentinel)
            os.rmdir(path)
            path.symlink_to(victim, target_is_directory=True)
        return verdict

    AttemptWorkspace.still_ours = _swap  # type: ignore[method-assign]
    try:
        _remove_attempt_dir(workspace)
    finally:
        AttemptWorkspace.still_ours = real_still_ours  # type: ignore[method-assign]

    assert swapped["done"]
    assert (victim / "data").read_bytes() == b"precious"


def test_genuine_cleanup_still_removes_everything(tmp_path: Path) -> None:
    """The control: an untouched attempt directory, including nested children, is removed."""
    root = _work(tmp_path)
    workspace = _create_attempt_dir(root, job_id="job-ok", attempt_id="ccc")
    (workspace.path / "a.txt").write_bytes(b"a")
    nested = workspace.path / "sub" / "deeper"
    nested.mkdir(parents=True)
    (nested / "b.txt").write_bytes(b"b")
    _remove_attempt_dir(workspace)
    assert not workspace.path.exists()
    assert list(root.iterdir()) == []


def test_cleanup_leaves_a_pre_swapped_directory_alone(tmp_path: Path) -> None:
    """Retained: a replacement installed BEFORE cleanup is refused by the identity check."""
    import shutil

    root = _work(tmp_path)
    workspace = _create_attempt_dir(root, job_id="job-pre", attempt_id="ddd")
    path = workspace.path
    shutil.rmtree(path)
    path.mkdir()
    (path / "victim").write_bytes(b"precious")
    _remove_attempt_dir(workspace)
    assert (path / "victim").read_bytes() == b"precious"


def test_cleanup_survives_inode_number_reuse(tmp_path: Path) -> None:
    """Retained: a replacement that happens to reuse the inode number lacks the sentinel."""
    import shutil

    root = _work(tmp_path)
    workspace = _create_attempt_dir(root, job_id="job-reuse", attempt_id="eee")
    path = workspace.path
    shutil.rmtree(path)
    path.mkdir()
    (path / "victim").write_bytes(b"precious")
    info = os.lstat(path)
    if (info.st_dev, info.st_ino) == (workspace.st_dev, workspace.st_ino):
        assert not workspace.still_ours(), "the sentinel must defeat inode-number reuse"
    _remove_attempt_dir(workspace)
    assert (path / "victim").read_bytes() == b"precious"


def test_descriptors_are_closed_exactly_once_on_every_path(tmp_path: Path) -> None:
    root = _work(tmp_path)
    workspace = _create_attempt_dir(root, job_id="job-fd", attempt_id="fff")
    assert workspace.dir_fd is not None and workspace.parent_fd is not None
    dir_fd, parent_fd = workspace.dir_fd, workspace.parent_fd
    _remove_attempt_dir(workspace)
    assert workspace.dir_fd is None and workspace.parent_fd is None
    for fd in (dir_fd, parent_fd):
        with pytest.raises(OSError):
            os.fstat(fd)
    workspace.close()  # idempotent: a second close must not raise or double-close
    _remove_attempt_dir(workspace)


def test_no_descriptor_leak_across_many_attempts(tmp_path: Path) -> None:
    root = _work(tmp_path)
    before = len(os.listdir("/proc/self/fd"))
    for i in range(40):
        workspace = _create_attempt_dir(root, job_id="leak", attempt_id=f"{i:03d}")
        (workspace.path / "x").write_bytes(b"x")
        _remove_attempt_dir(workspace)
    after = len(os.listdir("/proc/self/fd"))
    assert after <= before + 2, (before, after)


# --------------------------------------------------------------------------- #
# C — descriptor-bound output acquisition
# --------------------------------------------------------------------------- #
def _acquire(workspace: AttemptWorkspace) -> Any:
    return acquire_produced_output(workspace, _inputs())


def test_a_valid_output_is_acquired_once_and_binds_its_own_identity(tmp_path: Path) -> None:
    root = _work(tmp_path)
    workspace = _create_attempt_dir(root, job_id="j", attempt_id="ok")
    (workspace.path / OUTPUT_VCF_NAME).write_bytes(_VALID)
    acquired = _acquire(workspace)
    assert acquired.payload == _VALID
    assert acquired.sha256 == hashlib.sha256(_VALID).hexdigest()
    assert acquired.size_bytes == len(_VALID)


def test_a_fifo_output_is_rejected_promptly(tmp_path: Path) -> None:
    """A FIFO must fail immediately — never block the worker waiting for a writer."""
    root = _work(tmp_path)
    workspace = _create_attempt_dir(root, job_id="j", attempt_id="fifo")
    os.mkfifo(workspace.path / OUTPUT_VCF_NAME)
    with pytest.raises(GatkOutputError):
        _acquire(workspace)


def test_a_symlinked_output_is_rejected_by_the_kernel(tmp_path: Path) -> None:
    root = _work(tmp_path)
    workspace = _create_attempt_dir(root, job_id="j", attempt_id="link")
    target = tmp_path / "target.vcf"
    target.write_bytes(_VALID)
    (workspace.path / OUTPUT_VCF_NAME).symlink_to(target)
    with pytest.raises(GatkOutputError):
        _acquire(workspace)


def test_a_hard_linked_output_is_rejected(tmp_path: Path) -> None:
    root = _work(tmp_path)
    workspace = _create_attempt_dir(root, job_id="j", attempt_id="hard")
    out = workspace.path / OUTPUT_VCF_NAME
    out.write_bytes(_VALID)
    os.link(out, workspace.path / "copy.vcf")
    with pytest.raises(GatkOutputError, match="links"):
        _acquire(workspace)


def test_a_replaced_attempt_directory_is_rejected(tmp_path: Path) -> None:
    import shutil

    root = _work(tmp_path)
    workspace = _create_attempt_dir(root, job_id="j", attempt_id="swap")
    (workspace.path / OUTPUT_VCF_NAME).write_bytes(_VALID)
    shutil.rmtree(workspace.path)
    workspace.path.mkdir()
    (workspace.path / OUTPUT_VCF_NAME).write_bytes(_VALID)
    with pytest.raises(GatkOutputError):
        _acquire(workspace)


def test_a_mutation_during_descriptor_bound_reading_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The post-read ``fstat`` catches a file that grew while it was being read."""
    root = _work(tmp_path)
    workspace = _create_attempt_dir(root, job_id="j", attempt_id="mutate")
    out = workspace.path / OUTPUT_VCF_NAME
    out.write_bytes(_VALID)
    real_read = EX._read_fd_bytes

    def _read_then_mutate(fd: int) -> bytes:
        payload = real_read(fd)
        with out.open("ab") as fh:  # the file grows between the read and the post-read fstat
            fh.write(b"##mutated=true\n")
        return payload

    monkeypatch.setattr(EX, "_read_fd_bytes", _read_then_mutate)
    with pytest.raises(GatkOutputError, match="changed size"):
        _acquire(workspace)


def test_an_empty_output_is_rejected(tmp_path: Path) -> None:
    root = _work(tmp_path)
    workspace = _create_attempt_dir(root, job_id="j", attempt_id="empty")
    (workspace.path / OUTPUT_VCF_NAME).write_bytes(b"")
    with pytest.raises(GatkOutputError, match="empty"):
        _acquire(workspace)


def test_a_structurally_invalid_output_is_rejected(tmp_path: Path) -> None:
    root = _work(tmp_path)
    workspace = _create_attempt_dir(root, job_id="j", attempt_id="bad")
    (workspace.path / OUTPUT_VCF_NAME).write_bytes(b"not a vcf\n")
    with pytest.raises(GatkOutputError):
        _acquire(workspace)


# --------------------------------------------------------------------------- #
# C — end-to-end: a swap between the runner step and acquisition is durably FAILED
# --------------------------------------------------------------------------- #
@dataclasses.dataclass(frozen=True)
class _SwappingRunner(FakeGatkRunner):
    """Produces a genuine VCF, then swaps the output the instant the runner returns."""

    mode: str = "regular"

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
        vcf_path.unlink()
        if self.mode == "fifo":
            os.mkfifo(vcf_path)
        elif self.mode == "symlink":
            # OUTSIDE the work root, so the work-root emptiness assertion stays meaningful
            target = work_dir.parent.parent / "elsewhere.vcf"
            target.write_bytes(_VALID)
            vcf_path.symlink_to(target)
        elif self.mode == "regular":
            vcf_path.write_bytes(
                b"##fileformat=VCFv4.2\n"
                b"#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\tFORMAT\tSAMPLE\n"
                b"chr18\t151\t.\tC\tT\t60.0\tPASS\t.\tGT\t1/1\n"
            )
        return outcome


@pytest.mark.parametrize("mode", ["fifo", "symlink", "regular"])
def test_an_output_swapped_after_the_runner_is_durably_failed(env: Any, mode: str) -> None:
    """FIFO, symlink and different-regular-file swaps all fail promptly and end terminal."""
    result = env.run(runner=_SwappingRunner(mode=mode))
    assert result is not None and result.status == "FAILED"
    assert result.failure_code == "GATK_OUTPUT_INVALID"
    assert _counts(env) == (0, 1)
    assert env.artifacts() == []  # nothing was published from a substituted object
    assert list(env.work_root.iterdir()) == []
    assert_no_stranded_jobs(env.engine, env.plan.plan_hash)


class _DirectorySwappingRunner(FakeGatkRunner):
    """Replaces the whole attempt directory the instant the runner returns."""

    def run(
        self,
        *,
        argv: Any,
        work_dir: Any,
        vcf_path: Any,
        inputs: Any,
        expected_runtime_bundle_sha256: str = "",
    ) -> Any:
        import shutil

        outcome = super().run(
            argv=argv,
            work_dir=work_dir,
            vcf_path=vcf_path,
            inputs=inputs,
            expected_runtime_bundle_sha256=expected_runtime_bundle_sha256,
        )
        payload = vcf_path.read_bytes()
        shutil.rmtree(work_dir)
        work_dir.mkdir()
        (work_dir / OUTPUT_VCF_NAME).write_bytes(payload)
        return outcome


def test_an_attempt_directory_replaced_before_acquisition_is_durably_failed(env: Any) -> None:
    result = env.run(runner=_DirectorySwappingRunner())
    assert result is not None and result.status == "FAILED"
    assert result.failure_code == "GATK_OUTPUT_INVALID"
    assert _counts(env) == (0, 1)
    assert env.artifacts() == []
    assert_no_stranded_jobs(env.engine, env.plan.plan_hash)


def test_the_published_bytes_are_exactly_the_acquired_bytes(env: Any) -> None:
    """The valid control: validation, digest, size, published VCF and manifest all agree."""
    from sqlalchemy import text

    acquisitions: list[Any] = []
    real = EX.acquire_produced_output

    def _record(workspace: Any, inputs: Any) -> Any:
        acquired = real(workspace, inputs)
        acquisitions.append(acquired)
        return acquired

    EX.acquire_produced_output = _record  # type: ignore[assignment]
    try:
        result = env.run()
    finally:
        EX.acquire_produced_output = real  # type: ignore[assignment]

    assert result is not None and result.status == "SUCCEEDED"
    assert len(acquisitions) == 1  # acquired EXACTLY once
    acquired = acquisitions[0]

    # the dispatch result reports the acquired identity...
    assert result.vcf_sha256 == acquired.sha256

    # ...the immutable row stores it...
    with env.engine.connect() as c:
        row = c.execute(
            text(f"SELECT vcf_sha256, result_manifest_sha256 FROM {_RESULTS}")  # noqa: S608
        ).one()
    assert str(row[0]) == acquired.sha256

    # ...the published VCF artifact is byte-identical to the acquired payload...
    published = {p.name: p.read_bytes() for p in env.artifacts()}
    vcf_name = f"{acquired.sha256}.vcf"
    assert vcf_name in published
    assert published[vcf_name] == acquired.payload
    assert hashlib.sha256(published[vcf_name]).hexdigest() == acquired.sha256

    # ...and the result manifest binds the same digest and size.
    manifest_name = f"{str(row[1])}.result.json"
    document = json.loads(published[manifest_name])
    assert document["vcf_sha256"] == acquired.sha256
    assert document["vcf_size_bytes"] == acquired.size_bytes == len(acquired.payload)
