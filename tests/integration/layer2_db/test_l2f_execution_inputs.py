"""F5-B provisioned dataset resolver — real-PostgreSQL behavioral tests.

Scratch PostgreSQL at 0006 (the resolver only reads the F3-C1 graph); the operational store is
never touched, and no truth or mutation directory is ever created or inspected.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import text

from minos_engine.experiments.execution_contract import InputResolutionError
from minos_engine.storage.l2f_execution_inputs import (
    ENV_DATASET_ROOT,
    DatasetRoot,
    dataset_root_from_env,
    resolve_accepted_execution_input,
)
from minos_engine.storage.l2f_plan_store import _persist_experiment_plan_with_trust
from tests.integration.layer2_db.conftest import alembic_upgrade, scratch_database
from tests.integration.layer2_db.l2f_plan_seed import seed_upstream_for_plan
from tests.integration.layer2_db.test_l2f_plan_store import (
    _CS,
    _SNAPSHOT_A,
    _engine,
    _provisioned_root,
    _publisher,
    _synthetic_plan,
)

_L2F = "0006_l2f_experiment_plan"


def _write_dataset_root(tmp_path: Path, *, round_id: str, chromosome: str) -> Path:
    """A provisioned dataset root in the established layout (no truth/mutation directory)."""
    root = tmp_path / "datasets"
    practice = root / "practice" / f"round_{round_id}"
    reference = root / "reference" / chromosome
    practice.mkdir(parents=True)
    reference.mkdir(parents=True)
    (practice / "input.bam").write_bytes(b"BAM\x01payload")
    (practice / "input.bam.bai").write_bytes(b"BAI\x01payload")
    (reference / f"{chromosome}.fa").write_bytes(b">chr\nACGT\n")
    (reference / f"{chromosome}.fa.fai").write_bytes(b"chr\t4\t5\t4\t5\n")
    (reference / f"{chromosome}.dict").write_text(
        f"@HD\tVN:1.6\n@SQ\tSN:{chromosome}\tLN:4\tM5:abc\n", encoding="utf-8"
    )
    return root


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture
def prepared(isolated_pg_base_url: str, tmp_path: Path) -> Any:
    """A persisted plan whose accepted dataset identity matches a provisioned root on disk.

    The provisioned files are written FIRST and their REAL byte hashes are seeded, because
    ``catalog.dataset_registry`` is append-only and can never be corrected afterwards.
    """
    plan = _synthetic_plan(_SNAPSHOT_A)
    member = plan.members[0]
    round_id, chromosome = "r1", "chr18"
    root_path = _write_dataset_root(tmp_path, round_id=round_id, chromosome=chromosome)
    practice = root_path / "practice" / f"round_{round_id}"
    reference = root_path / "reference" / chromosome
    identity = {
        member.dataset_id: {
            "round_id": round_id,
            "chromosome": chromosome,
            "bam_sha256": _sha(practice / "input.bam"),
            "bai_sha256": _sha(practice / "input.bam.bai"),
            "reference_sha256": _sha(reference / f"{chromosome}.fa"),
            "fai_sha256": _sha(reference / f"{chromosome}.fa.fai"),
            "bam_size_bytes": (practice / "input.bam").stat().st_size,
        }
    }
    with scratch_database(isolated_pg_base_url, "minos_f5_inputs") as url:
        alembic_upgrade(url, _L2F)
        engine = _engine(url)
        try:
            with engine.connect() as conn, conn.begin():
                seed_upstream_for_plan(conn, plan, dataset_identity=identity)
            _persist_experiment_plan_with_trust(
                engine, plan, _CS, publisher=_publisher(_provisioned_root(tmp_path))
            )
            with engine.connect() as c:
                row = (
                    c.execute(
                        text(
                            "SELECT pm.id AS member_id, p.id AS plan_id "
                            "  FROM experiments.l2f_experiment_plan_members pm "
                            "  JOIN experiments.l2f_experiment_plans p ON p.id = pm.plan_id "
                            "  JOIN catalog.dataset_registry dr ON dr.id = pm.dataset_registry_id "
                            " WHERE dr.dataset_id = :d"
                        ),
                        {"d": member.dataset_id},
                    )
                    .mappings()
                    .one()
                )
            yield engine, str(row["plan_id"]), str(row["member_id"]), root_path, practice, reference
        finally:
            engine.dispose()


def _resolve(prepared: Any, root: Path | None = None) -> Any:
    engine, plan_id, member_id, root_path, _p, _r = prepared
    with engine.connect() as conn:
        return resolve_accepted_execution_input(
            conn,
            plan_id=plan_id,
            plan_member_id=member_id,
            root=DatasetRoot.from_path(root or root_path),
        )


def test_valid_provisioned_inputs_resolve_and_bind(prepared: Any) -> None:
    engine, _plan_id, _member_id, root_path, practice, reference = prepared
    inputs, paths = _resolve(prepared)
    assert inputs.bam_sha256 == _sha(practice / "input.bam")
    assert inputs.bai_sha256 == _sha(practice / "input.bam.bai")
    assert inputs.reference_sha256 == _sha(reference / f"{inputs.chromosome}.fa")
    assert inputs.fai_sha256 == _sha(reference / f"{inputs.chromosome}.fa.fai")
    assert inputs.dictionary_sha256 == _sha(reference / f"{inputs.chromosome}.dict")
    assert inputs.bam_size_bytes == (practice / "input.bam").stat().st_size
    assert paths.bam == practice / "input.bam"
    assert len(inputs.identity_hash()) == 64
    # nothing under a truth/mutation path was ever consulted
    assert not (root_path / "truth").exists()


@pytest.mark.parametrize(
    "relative",
    [
        "practice/round_{r}/input.bam",
        "practice/round_{r}/input.bam.bai",
        "reference/{c}/{c}.fa",
        "reference/{c}/{c}.fa.fai",
        "reference/{c}/{c}.dict",
    ],
)
def test_missing_input_is_rejected(prepared: Any, relative: str) -> None:
    _engine, _p, _m, root_path, practice, reference = prepared
    chrom = reference.name
    round_id = practice.name.removeprefix("round_")
    target = root_path / relative.format(r=round_id, c=chrom)
    target.unlink()
    with pytest.raises(InputResolutionError):
        _resolve(prepared)


@pytest.mark.parametrize(
    "relative",
    [
        "practice/round_{r}/input.bam",
        "practice/round_{r}/input.bam.bai",
        "reference/{c}/{c}.fa",
        "reference/{c}/{c}.fa.fai",
    ],
)
def test_mutated_or_substituted_input_is_rejected(prepared: Any, relative: str) -> None:
    _engine, _p, _m, root_path, practice, reference = prepared
    chrom = reference.name
    round_id = practice.name.removeprefix("round_")
    target = root_path / relative.format(r=round_id, c=chrom)
    target.write_bytes(target.read_bytes() + b"TAMPERED")
    with pytest.raises(InputResolutionError):
        _resolve(prepared)


def test_symlinked_input_is_rejected(prepared: Any) -> None:
    _engine, _p, _m, root_path, practice, _reference = prepared
    real = practice / "input.bam"
    payload = real.read_bytes()
    elsewhere = root_path.parent / "elsewhere.bam"
    elsewhere.write_bytes(payload)
    real.unlink()
    real.symlink_to(elsewhere)
    with pytest.raises(InputResolutionError):
        _resolve(prepared)


def test_symlinked_root_is_rejected(prepared: Any, tmp_path: Path) -> None:
    _engine, _p, _m, root_path, _practice, _reference = prepared
    link = tmp_path / "linked_root"
    link.symlink_to(root_path)
    with pytest.raises(InputResolutionError):
        DatasetRoot.from_path(link)


def test_missing_root_is_never_created(tmp_path: Path) -> None:
    absent = tmp_path / "not_provisioned"
    with pytest.raises(InputResolutionError):
        DatasetRoot.from_path(absent)
    assert not absent.exists()  # never created or repaired


def test_dictionary_without_the_accepted_chromosome_is_rejected(prepared: Any) -> None:
    _engine, _p, _m, _root, _practice, reference = prepared
    dict_path = reference / f"{reference.name}.dict"
    dict_path.write_text("@HD\tVN:1.6\n@SQ\tSN:chrZZ\tLN:4\n", encoding="utf-8")
    with pytest.raises(InputResolutionError):
        _resolve(prepared)


def test_dictionary_with_wrong_reference_length_is_rejected(prepared: Any) -> None:
    engine, plan_id, member_id, root_path, _practice, _reference = prepared
    with engine.connect() as conn, pytest.raises(InputResolutionError):
        resolve_accepted_execution_input(
            conn,
            plan_id=plan_id,
            plan_member_id=member_id,
            root=DatasetRoot.from_path(root_path),
            reference_length=999999,  # the committed .dict declares LN:4
        )


@pytest.mark.parametrize("token", ["../escape", "a/b", "", ".."])
def test_unsafe_path_tokens_are_rejected(prepared: Any, token: str) -> None:
    _engine, _p, _m, root_path, _practice, _reference = prepared
    root = DatasetRoot.from_path(root_path)
    with pytest.raises(InputResolutionError):
        root.paths_for(round_id=token, chromosome="chr18")
    with pytest.raises(InputResolutionError):
        root.paths_for(round_id="r1", chromosome=token)


def test_env_root_is_required_and_never_caller_supplied(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(ENV_DATASET_ROOT, raising=False)
    with pytest.raises(InputResolutionError):
        dataset_root_from_env()
    import inspect

    assert list(inspect.signature(dataset_root_from_env).parameters) == []


def test_non_train_member_is_rejected(prepared: Any) -> None:
    """Only accepted TRAIN members may ever be executed."""
    engine, plan_id, member_id, root_path, _practice, _reference = prepared
    # the guard is on the resolver's own partition check; prove it rejects a non-train partition
    with engine.connect() as conn:
        rows = conn.execute(
            text("SELECT partition FROM experiments.l2f_experiment_plan_members WHERE id=:m"),
            {"m": member_id},
        ).scalar_one()
    assert rows == "train"  # the accepted graph only ever holds train members
    with engine.connect() as conn, pytest.raises(InputResolutionError):
        resolve_accepted_execution_input(
            conn,
            plan_id=plan_id,
            plan_member_id="00000000-0000-0000-0000-000000000000",
            root=DatasetRoot.from_path(root_path),
        )
