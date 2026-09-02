"""Which partition each execution phase may byte-verify, proved as a full matrix.

The shared byte verifier used to be hard-coded to TRAIN. That contradicted ``0021``, which
restricted the Phase-D resolver to VALIDATION on purpose: a correct Phase-D job resolved out of
the database and was then refused by the verifier it was handed to. The repair is not "accept
both everywhere" — the phases ask different scientific questions and each executes exactly one
partition — so the matrix below is the actual contract, and every cell of it is asserted.

TEST appears in the refusal column of every row and in the acceptance column of none. It is not
excluded by a check that could be relaxed: it is not a member of the set an expected partition
may be drawn from.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import pytest

from minos_engine.common.errors import MinosEngineError
from minos_engine.storage.l2f2_runner import _EXECUTED_PARTITION_BY_PHASE
from minos_engine.storage.l2f_execution_inputs import (
    DatasetRoot,
    verify_execution_input,
)

_PARTITIONS = ("train", "validation", "test")


def _provisioned(root: Path, *, round_id: str, chromosome: str) -> dict[str, Any]:
    """A complete, byte-consistent input set, so only the PARTITION can decide the outcome."""
    practice = root / "practice" / f"round_{round_id}"
    reference = root / "reference" / chromosome
    practice.mkdir(parents=True, exist_ok=True)
    reference.mkdir(parents=True, exist_ok=True)

    # round-distinct bytes, as real BAMs are: identical filler would make a substituted round
    # hash the same as the real one and quietly pass.
    payloads = {
        practice / "input.bam": f"bam-bytes-{round_id}\n".encode(),
        practice / "input.bam.bai": f"bai-bytes-{round_id}\n".encode(),
        reference / f"{chromosome}.fa": b">seq\nACGT\n",
        reference / f"{chromosome}.fa.fai": b"fai-bytes\n",
    }
    for path, payload in payloads.items():
        path.write_bytes(payload)
    (reference / f"{chromosome}.dict").write_bytes(
        f"@HD\tVN:1.6\n@SQ\tSN:{chromosome}\tLN:4\n".encode()
    )

    def sha(path: Path) -> str:
        return hashlib.sha256(path.read_bytes()).hexdigest()

    return {
        "dataset_id": f"minos-{chromosome}-{round_id}",
        "round_id": round_id,
        "chromosome": chromosome,
        "region_hash": "a" * 64,
        "region_start0": 0,
        "region_end0_exclusive": 4,
        "bam_size_bytes": len(payloads[practice / "input.bam"]),
        "bam_sha256": sha(practice / "input.bam"),
        "bai_sha256": sha(practice / "input.bam.bai"),
        "reference_sha256": sha(reference / f"{chromosome}.fa"),
        "fai_sha256": sha(reference / f"{chromosome}.fa.fai"),
        "profile_id": f"profile-{round_id}",
        "content_hash": "b" * 64,
        "feature_values_hash": "c" * 64,
        "member_index": 0,
    }


@pytest.fixture
def member(tmp_path: Path) -> Any:
    root = tmp_path / "datasets"
    row = _provisioned(root, round_id="0123456789abcdef", chromosome="chr18")
    return row, DatasetRoot(root=root)


# --------------------------------------------------------------------------------------------
# the matrix
# --------------------------------------------------------------------------------------------
@pytest.mark.parametrize("expected", ["train", "validation"])
@pytest.mark.parametrize("actual", _PARTITIONS)
def test_a_member_is_verified_only_for_its_own_partition(
    member: Any, expected: str, actual: str
) -> None:
    row, root = member
    row = {**row, "partition": actual}
    if actual == expected:
        inputs, paths = verify_execution_input(row, root=root, expected_partition=expected)
        assert inputs.dataset_id == row["dataset_id"]
        assert paths.bam.is_file()
        return
    with pytest.raises(MinosEngineError, match="accepts .* members only"):
        verify_execution_input(row, root=root, expected_partition=expected)


@pytest.mark.parametrize("forbidden", ["test", "TRAIN", "", "all", "validation ", "train\n"])
def test_no_phase_may_ask_for_a_partition_outside_the_executable_set(
    member: Any, forbidden: str
) -> None:
    """Runtime-validated, not merely typed: a Literal annotation refuses nothing at run time."""
    row, root = member
    with pytest.raises(MinosEngineError, match="not an executable L2-F partition"):
        verify_execution_input(
            {**row, "partition": forbidden}, root=root, expected_partition=forbidden
        )


def test_test_can_never_be_verified_by_any_phase(member: Any) -> None:
    """The decisive one: no phase in the policy admits TEST, from either direction."""
    row, root = member
    for phase, expected in _EXECUTED_PARTITION_BY_PHASE.items():
        assert expected != "test", phase
        with pytest.raises(MinosEngineError, match="accepts .* members only"):
            verify_execution_input(
                {**row, "partition": "test"}, root=root, expected_partition=expected
            )


# --------------------------------------------------------------------------------------------
# the phase policy itself
# --------------------------------------------------------------------------------------------
def test_the_phase_partition_policy_is_exact_and_total() -> None:
    assert _EXECUTED_PARTITION_BY_PHASE == {
        "PHASE_A": "train",
        "PHASE_B": "train",
        "PHASE_C": "train",
        "PHASE_D": "validation",
    }
    assert "test" not in set(_EXECUTED_PARTITION_BY_PHASE.values())


def test_the_policy_covers_exactly_the_phases_that_have_a_resolver() -> None:
    """A phase with a resolver but no partition policy would fail closed; prove none exists."""
    from minos_engine.storage.l2f2_runner import _RESOLVE_SQL_BY_PHASE

    assert set(_RESOLVE_SQL_BY_PHASE) == set(_EXECUTED_PARTITION_BY_PHASE)


def test_an_unknown_phase_has_no_partition_policy() -> None:
    for phase in ("PHASE_E", "phase_d", "", "PHASE_TEST"):
        assert _EXECUTED_PARTITION_BY_PHASE.get(phase) is None


# --------------------------------------------------------------------------------------------
# no public execution entry has a partition argument
# --------------------------------------------------------------------------------------------
def test_no_public_execution_entry_accepts_a_partition() -> None:
    """Partition is phase authority, not user input — so there is nowhere to assert one."""
    import inspect

    from minos_engine.storage import l2f2_runner

    for name in (
        "execute_next_l2f2_phase_a_job",
        "execute_next_l2f2_phase_b_job",
        "execute_next_l2f2_phase_c_job",
        "execute_next_l2f2_phase_d_job",
    ):
        entry = getattr(l2f2_runner, name)
        assert "partition" not in inspect.signature(entry).parameters, name


def test_the_historical_train_resolver_pins_train_and_says_so() -> None:
    """``resolve_accepted_execution_input`` is the pre-L2-F2 operational path. It stays TRAIN."""
    import inspect

    from minos_engine.storage.l2f_execution_inputs import resolve_accepted_execution_input

    assert "partition" not in inspect.signature(resolve_accepted_execution_input).parameters
    source = inspect.getsource(resolve_accepted_execution_input)
    assert "expected_partition=_TRAIN" in source


# --------------------------------------------------------------------------------------------
# input-byte readiness negatives — the same core the real preflight uses
# --------------------------------------------------------------------------------------------
def _verify(row: dict[str, Any], root: DatasetRoot) -> Any:
    return verify_execution_input(row, root=root, expected_partition="validation")


@pytest.fixture
def validation_member(tmp_path: Path) -> Any:
    root = tmp_path / "datasets"
    row = _provisioned(root, round_id="0f87c91fe3033486", chromosome="chr22")
    row["partition"] = "validation"
    return row, DatasetRoot(root=root), root


@pytest.mark.parametrize(
    "relative",
    [
        "practice/round_0f87c91fe3033486/input.bam",
        "practice/round_0f87c91fe3033486/input.bam.bai",
        "reference/chr22/chr22.fa",
        "reference/chr22/chr22.fa.fai",
        "reference/chr22/chr22.dict",
    ],
)
def test_a_missing_input_file_is_refused(validation_member: Any, relative: str) -> None:
    row, dataset_root, root = validation_member
    (root / relative).unlink()
    with pytest.raises(MinosEngineError):
        _verify(row, dataset_root)


@pytest.mark.parametrize(
    "digest",
    ["bam_sha256", "bai_sha256", "reference_sha256", "fai_sha256"],
)
def test_a_digest_mismatch_is_refused(validation_member: Any, digest: str) -> None:
    row, dataset_root, _root = validation_member
    with pytest.raises(MinosEngineError, match=digest):
        _verify({**row, digest: "f" * 64}, dataset_root)


def test_a_bam_size_mismatch_is_refused(validation_member: Any) -> None:
    row, dataset_root, _root = validation_member
    with pytest.raises(MinosEngineError):
        _verify({**row, "bam_size_bytes": row["bam_size_bytes"] + 1}, dataset_root)


def test_a_dictionary_for_another_chromosome_is_refused(validation_member: Any) -> None:
    row, dataset_root, root = validation_member
    dictionary = root / "reference" / "chr22" / "chr22.dict"
    dictionary.write_bytes(b"@HD\tVN:1.6\n@SQ\tSN:chr21\tLN:4\n")
    with pytest.raises(MinosEngineError):
        _verify(row, dataset_root)


@pytest.mark.parametrize(
    "relative",
    [
        "practice/round_0f87c91fe3033486/input.bam",
        "reference/chr22/chr22.fa",
        "reference/chr22/chr22.dict",
    ],
)
def test_an_input_that_is_a_symlink_is_refused(
    validation_member: Any, tmp_path: Path, relative: str
) -> None:
    """A symlink planted at the expected name cannot redirect the read."""
    row, dataset_root, root = validation_member
    target = root / relative
    elsewhere = tmp_path / "elsewhere.bin"
    elsewhere.write_bytes(target.read_bytes())
    target.unlink()
    target.symlink_to(elsewhere)
    with pytest.raises(MinosEngineError):
        _verify(row, dataset_root)


def test_another_members_round_cannot_substitute(validation_member: Any) -> None:
    """Pointing a member at a different round's directory fails on digests, not silently."""
    row, dataset_root, root = validation_member
    _provisioned(root, round_id="9f860ef634f13ea2", chromosome="chr22")
    with pytest.raises(MinosEngineError, match="sha256"):
        _verify({**row, "round_id": "9f860ef634f13ea2"}, dataset_root)


def test_verification_opens_no_truth_file(validation_member: Any) -> None:
    """Instrumented: input readiness touches BAM/BAI/reference/FAI/dict and nothing else."""
    import builtins
    import os

    row, dataset_root, root = validation_member
    opened: list[str] = []
    real_open, real_os_open = builtins.open, os.open
    builtins.open = lambda f, *a, **k: (opened.append(str(f)), real_open(f, *a, **k))[1]
    os.open = lambda p, *a, **k: (opened.append(str(p)), real_os_open(p, *a, **k))[1]
    try:
        _verify(row, dataset_root)
    finally:
        builtins.open, os.open = real_open, real_os_open

    assert opened, "the spy recorded nothing; it is not instrumenting the verifier"
    for path in opened:
        assert "truth" not in path
        assert "mutations" not in path
    names = {Path(p).name for p in opened if str(root) in p}
    assert names <= {"input.bam", "input.bam.bai", "chr22.fa", "chr22.fa.fai", "chr22.dict"}


# --------------------------------------------------------------------------------------------
# scratch-root portability
# --------------------------------------------------------------------------------------------
def test_scratch_root_falls_back_when_there_is_no_minos_filesystem(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """A machine with no MINOS physical root must still be able to run these suites.

    The canonical root exists to keep the operator's filesystem tidy. On a runner that has no
    such root there is nothing to keep tidy, and asserting the path unconditionally made every
    scratch fixture fail at setup. The root is discovered; this proves the discovery, with the
    canonical root pointed somewhere that does not exist.
    """
    from tests import minos_scratch

    monkeypatch.setattr(minos_scratch, "CANONICAL_MINOS_ROOT", tmp_path / "absent")
    fallback = tmp_path / "fallback"
    fallback.mkdir()

    scratch, effective_root = minos_scratch.minos_scratch_root("probe_", fallback=fallback)
    assert effective_root == fallback.resolve()
    assert scratch.is_dir()
    assert scratch.resolve().is_relative_to(fallback.resolve())
    assert not scratch.resolve().is_relative_to(Path(__file__).resolve().parents[3])


def test_scratch_root_uses_the_canonical_root_when_the_machine_has_one(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """Where a MINOS filesystem does exist, scratch state still belongs inside it."""
    from tests import minos_scratch

    canonical = tmp_path / "bittensor"
    canonical.mkdir()
    monkeypatch.setattr(minos_scratch, "CANONICAL_MINOS_ROOT", canonical)
    unused_fallback = tmp_path / "fallback"
    unused_fallback.mkdir()

    scratch, effective_root = minos_scratch.minos_scratch_root("probe_", fallback=unused_fallback)
    assert effective_root == canonical
    assert scratch.resolve().is_relative_to(canonical.resolve())
    assert not scratch.resolve().is_relative_to(unused_fallback.resolve())


def test_no_test_support_module_hard_codes_the_operator_minos_root() -> None:
    """The regression guard: one constant, in one place, discovered rather than assumed."""
    import ast

    tests_root = Path(__file__).resolve().parents[2]
    # assembled from parts so this guard does not match itself.
    operator_root = "/" + "/".join(("home", "hr", "bittensor"))
    offenders: list[str] = []
    for path in sorted(tests_root.rglob("*.py")):
        if path.name == "minos_scratch.py" or "fixtures" in path.parts:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and node.value.startswith(operator_root)
                and "corpus" not in node.value
            ):
                offenders.append(f"{path.relative_to(tests_root)}:{node.lineno}")
    assert offenders == [], offenders
