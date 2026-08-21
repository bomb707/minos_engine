"""F5-C execution orchestration — real-PostgreSQL behavioral tests at 0008.

Every execution uses the deterministic FakeGatkRunner; no GATK process is ever started and no
truth/mutation/hap.py data is ever read. Scratch PostgreSQL only.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Engine, text

from minos_engine.experiments.execution_contract import (
    ConfigArtifactError,
    InputResolutionError,
)
from minos_engine.storage import l2f_execution as EX
from minos_engine.storage import l2f_harness_verifier as HV
from minos_engine.storage import l2f_job_claim as JC
from minos_engine.storage.l2f_execution import (
    AmbiguousExecutionCommitError,
    ExecutionRecordedFailureError,
    PostCommitWrapperError,
    PreTerminalExecutionError,
    _execute_next_job_with_trust,
)
from minos_engine.storage.l2f_execution_inputs import DatasetRoot
from minos_engine.storage.l2f_gatk_runner import FakeGatkRunner
from minos_engine.storage.l2f_job_enqueue import _enqueue_experiment_jobs_with_trust
from minos_engine.storage.l2f_plan_store import _persist_experiment_plan_with_trust
from tests.integration.layer2_db.conftest import alembic_upgrade, scratch_database
from tests.integration.layer2_db.l2f_plan_seed import seed_upstream_for_plan
from tests.integration.layer2_db.test_l2f_plan_store import (
    _CS,
    _SNAPSHOT_A,
    _SNAPSHOT_B,
    _count,
    _engine,
    _provisioned_root,
    _publisher,
    _synthetic_plan,
)

_L2F, _F5 = "0006_l2f_experiment_plan", "0008_l2f_execution_results"
_JOBS = "experiments.l2f_experiment_jobs"
_RESULTS = "experiments.l2f_execution_results"
_FAILURES = "experiments.l2f_execution_failures"


def _sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def _dataset_files(tmp_path: Path, *, round_id: str, chromosome: str) -> Path:
    root = tmp_path / "datasets"
    practice = root / "practice" / f"round_{round_id}"
    reference = root / "reference" / chromosome
    practice.mkdir(parents=True, exist_ok=True)
    reference.mkdir(parents=True, exist_ok=True)
    (practice / "input.bam").write_bytes(b"BAM\x01" + round_id.encode())
    (practice / "input.bam.bai").write_bytes(b"BAI\x01" + round_id.encode())
    (reference / f"{chromosome}.fa").write_bytes(b">" + chromosome.encode() + b"\nACGT\n")
    (reference / f"{chromosome}.fa.fai").write_bytes(chromosome.encode() + b"\t4\t5\t4\t5\n")
    (reference / f"{chromosome}.dict").write_text(
        f"@HD\tVN:1.6\n@SQ\tSN:{chromosome}\tLN:4\n", encoding="utf-8"
    )
    return root


def _result_root(tmp_path: Path) -> Path:
    root = tmp_path / "resultroot"
    root.mkdir(exist_ok=True)
    os.chmod(root, 0o2750)
    return root


def _work_root(tmp_path: Path) -> Path:
    root = tmp_path / "workroot"
    root.mkdir(exist_ok=True)
    return root


class Env:
    """A fully prepared F5 environment: plan graph, jobs, provisioned inputs and publishers."""

    def __init__(self, engine: Engine, plan: Any, tmp_path: Path, dataset_root: Path) -> None:
        from minos_engine.storage.l2f_result_publisher import ResultArtifactPublisher

        self.engine = engine
        self.plan = plan
        self.dataset_root = DatasetRoot.from_path(dataset_root)
        self.result_root = _result_root(tmp_path)
        self.publisher = ResultArtifactPublisher(self.result_root)
        self.work_root = _work_root(tmp_path)

    def run(self, *, worker_id: str = "w-1", runner: FakeGatkRunner | None = None) -> Any:
        return _execute_next_job_with_trust(
            self.engine,
            self.plan,
            worker_id=worker_id,
            runner=runner or FakeGatkRunner(),
            dataset_root=self.dataset_root,
            publisher=self.publisher,
            work_root=self.work_root,
        )

    def status(self, job_id: str) -> str:
        with self.engine.connect() as c:
            return str(
                c.execute(
                    text(f"SELECT status FROM {_JOBS} WHERE id=:i"),
                    {"i": job_id},  # noqa: S608
                ).scalar_one()
            )

    def artifacts(self) -> list[Path]:
        return sorted(self.result_root.iterdir())

    def verify(self) -> Any:
        return HV._verify_experiment_harness_with_trust(self.engine, self.plan, _CS)


def _prepare_env(
    isolated_pg_base_url: str, tmp_path: Path, spec: list[tuple[str, str, str]], *, jobs: int
) -> Any:
    """Persist a plan at 0006, enqueue jobs, upgrade to 0008 and provision matching inputs."""
    plan = _synthetic_plan(spec)
    identity: dict[str, dict[str, Any]] = {}
    root: Path | None = None
    for index, member in enumerate(plan.members):
        round_id, chromosome = f"r{index}", "chr18"
        root = _dataset_files(tmp_path, round_id=round_id, chromosome=chromosome)
        practice = root / "practice" / f"round_{round_id}"
        reference = root / "reference" / chromosome
        identity[member.dataset_id] = {
            "round_id": round_id,
            "chromosome": chromosome,
            "bam_sha256": _sha(practice / "input.bam"),
            "bai_sha256": _sha(practice / "input.bam.bai"),
            "reference_sha256": _sha(reference / f"{chromosome}.fa"),
            "fai_sha256": _sha(reference / f"{chromosome}.fa.fai"),
            "bam_size_bytes": (practice / "input.bam").stat().st_size,
        }
    assert root is not None
    return plan, identity, root


@pytest.fixture
def env(isolated_pg_base_url: str, tmp_path: Path) -> Any:
    plan, identity, dataset_root = _prepare_env(isolated_pg_base_url, tmp_path, _SNAPSHOT_A, jobs=4)
    with scratch_database(isolated_pg_base_url, "minos_f5_exec") as url:
        alembic_upgrade(url, _L2F)
        engine = _engine(url)
        try:
            with engine.connect() as conn, conn.begin():
                seed_upstream_for_plan(conn, plan, dataset_identity=identity)
            _persist_experiment_plan_with_trust(
                engine, plan, _CS, publisher=_publisher(_provisioned_root(tmp_path))
            )
            _enqueue_experiment_jobs_with_trust(engine, plan, _CS, start=0, count=4)
            engine.dispose()
            alembic_upgrade(url, _F5)
            engine = _engine(url)
            yield Env(engine, plan, tmp_path, dataset_root)
        finally:
            engine.dispose()


# --------------------------------------------------------------------------- #
# success path
# --------------------------------------------------------------------------- #
def test_deterministic_success_creates_terminal_row_result_and_artifacts(env: Any) -> None:
    result = env.run()
    assert result is not None and result.status == "SUCCEEDED" and result.replay is False
    assert env.status(result.job_id) == "SUCCEEDED"
    assert _count(env.engine, f"SELECT count(*) FROM {_RESULTS}") == 1  # noqa: S608
    assert _count(env.engine, f"SELECT count(*) FROM {_FAILURES}") == 0  # noqa: S608
    # both artifacts published with distinct extensions and 0o640
    names = [p.name for p in env.artifacts()]
    assert any(n.endswith(".vcf") for n in names)
    assert any(n.endswith(".result.json") for n in names)
    for p in env.artifacts():
        assert oct(p.stat().st_mode)[-3:] == "640"
    # the stored digests are the REAL artifact bytes
    with env.engine.connect() as c:
        row = c.execute(
            text(f"SELECT vcf_sha256, result_manifest_sha256, result_hash FROM {_RESULTS}")  # noqa: S608
        ).one()
    published = {_sha(p) for p in env.artifacts()}
    assert str(row[0]) in published and str(row[1]) in published
    assert result.result_hash == str(row[2])


def test_exact_replay_is_idempotent_and_rewrites_no_files(env: Any) -> None:
    first = env.run()
    before = {p.name: p.read_bytes() for p in env.artifacts()}
    rows_before = _count(env.engine, f"SELECT count(*) FROM {_RESULTS}")  # noqa: S608
    # a terminal job is never reclaimable, so the next dispatch takes a DIFFERENT job
    second = env.run()
    assert second is not None and second.job_id != first.job_id
    assert _count(env.engine, f"SELECT count(*) FROM {_RESULTS}") == rows_before + 1  # noqa: S608
    for name, payload in before.items():
        assert (env.result_root / name).read_bytes() == payload  # never rewritten


def test_terminal_jobs_are_never_reclaimable(env: Any) -> None:
    seen = set()
    for _ in range(4):
        r = env.run()
        assert r is not None and r.job_id not in seen
        seen.add(r.job_id)
    assert env.run() is None  # the queue is exhausted; no terminal job comes back
    assert _count(env.engine, f"SELECT count(*) FROM {_RESULTS}") == 4  # noqa: S608


# --------------------------------------------------------------------------- #
# failure paths
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("runner", "expected"),
    [
        (FakeGatkRunner(exit_code=3), "GATK_NONZERO_EXIT"),
        (FakeGatkRunner(raise_timeout=True), "GATK_TIMEOUT"),
        (FakeGatkRunner(write_output=False), "GATK_OUTPUT_INVALID"),
        (FakeGatkRunner(override_bytes=b""), "GATK_OUTPUT_INVALID"),
        (FakeGatkRunner(override_bytes=b"not a vcf\n"), "GATK_OUTPUT_INVALID"),
        (
            FakeGatkRunner(
                override_bytes=(
                    b"##fileformat=VCFv4.2\n#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\n"
                    b"chr19\t1\t.\tA\tG\t1\tPASS\t.\n"
                )
            ),
            "GATK_OUTPUT_INVALID",
        ),
    ],
)
def test_execution_failure_records_bounded_failure_and_no_success(
    env: Any, runner: FakeGatkRunner, expected: str
) -> None:
    result = env.run(runner=runner)
    assert result is not None and result.status == "FAILED"
    assert result.failure_code == expected
    assert env.status(result.job_id) == "FAILED"
    assert _count(env.engine, f"SELECT count(*) FROM {_FAILURES}") == 1  # noqa: S608
    assert _count(env.engine, f"SELECT count(*) FROM {_RESULTS}") == 0  # noqa: S608
    assert env.artifacts() == []  # no success artifact was published


def test_success_and_failure_are_mutually_exclusive_across_jobs(env: Any) -> None:
    ok = env.run()
    bad = env.run(runner=FakeGatkRunner(exit_code=9))
    assert ok.status == "SUCCEEDED" and bad.status == "FAILED"
    assert ok.job_id != bad.job_id
    with env.engine.connect() as c:
        both = c.execute(
            text(
                f"SELECT count(*) FROM {_RESULTS} r JOIN {_FAILURES} f ON f.job_id = r.job_id"  # noqa: S608
            )
        ).scalar_one()
    assert int(both) == 0


# --------------------------------------------------------------------------- #
# preparation failures release the claim
# --------------------------------------------------------------------------- #
def test_missing_input_releases_the_claim_back_to_pending(env: Any, tmp_path: Path) -> None:
    (tmp_path / "datasets" / "practice" / "round_r0" / "input.bam").unlink()
    # F6: a pre-terminal failure is typed, and the ORIGINAL cause stays chained.
    with pytest.raises(PreTerminalExecutionError) as excinfo:
        env.run()
    assert excinfo.value.recovered_to == "PENDING"
    assert isinstance(excinfo.value.__cause__, InputResolutionError)
    pending = _count(
        env.engine,
        f"SELECT count(*) FROM {_JOBS} WHERE status='PENDING'",  # noqa: S608
    )
    assert pending == 4  # the claim was released; nothing is stuck CLAIMED
    assert _count(env.engine, f"SELECT count(*) FROM {_RESULTS}") == 0  # noqa: S608
    assert _count(env.engine, f"SELECT count(*) FROM {_FAILURES}") == 0  # noqa: S608


def test_mutated_config_artifact_releases_the_claim(env: Any) -> None:
    # tamper with EVERY published CONFIG artifact so whichever job is claimed first is affected.
    with env.engine.connect() as c:
        uris = [
            str(r[0])
            for r in c.execute(
                text(
                    "SELECT a.uri FROM experiments.l2f_config_payloads cp "
                    "JOIN catalog.artifacts a ON a.id = cp.artifact_id"
                )
            ).all()
        ]
    assert uris
    for uri in uris:
        path = Path(uri.removeprefix("file://"))
        os.chmod(path, 0o640)
        path.write_bytes(b'{"tampered": true}')
    with pytest.raises(PreTerminalExecutionError) as excinfo:
        env.run()
    assert excinfo.value.recovered_to == "PENDING"
    assert isinstance(excinfo.value.__cause__, ConfigArtifactError)
    assert _count(env.engine, f"SELECT count(*) FROM {_RESULTS}") == 0  # noqa: S608


# --------------------------------------------------------------------------- #
# transactional guarantees
# --------------------------------------------------------------------------- #
def test_ambiguous_commit_retains_artifacts(env: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    def _raise(_trans: Any) -> None:
        raise AmbiguousExecutionCommitError("simulated ambiguous COMMIT")

    monkeypatch.setattr(EX, "_commit_or_ambiguous", _raise)
    with pytest.raises(AmbiguousExecutionCommitError):
        env.run()
    assert len(env.artifacts()) == 2  # immutable artifacts are RETAINED


def test_post_commit_wrapper_failure_retains_rows_and_artifacts(
    env: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _boom() -> None:
        raise RuntimeError("post-commit wrapper failure")

    monkeypatch.setattr(EX, "_post_commit_hook", _boom)
    # F6: a wrapper failure AFTER a confirmed commit is typed distinctly and never retried.
    with pytest.raises(PostCommitWrapperError) as excinfo:
        env.run()
    assert isinstance(excinfo.value.__cause__, RuntimeError)
    assert _count(env.engine, f"SELECT count(*) FROM {_RESULTS}") == 1  # noqa: S608
    assert len(env.artifacts()) == 2


def test_precommit_rollback_removes_only_newly_created_artifacts(
    env: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _boom(*_a: Any, **_k: Any) -> str:
        raise RuntimeError("injected pre-commit failure")

    monkeypatch.setattr(EX, "_register_artifact", _boom)
    # F6: a NON-ambiguous success-persistence failure drives the job to a durable FAILED outcome
    # instead of stranding it in RUNNING, and preserves the original cause.
    with pytest.raises(ExecutionRecordedFailureError) as excinfo:
        env.run()
    assert isinstance(excinfo.value.__cause__, RuntimeError)
    assert env.artifacts() == []  # every newly created inode was removed
    assert _count(env.engine, f"SELECT count(*) FROM {_RESULTS}") == 0  # noqa: S608
    assert _count(env.engine, f"SELECT count(*) FROM {_FAILURES}") == 1  # noqa: S608
    EX.assert_no_stranded_jobs(env.engine, env.plan.plan_hash)


def test_direct_terminal_update_without_a_record_is_rejected(env: Any) -> None:
    claimed = JC._claim_next_job_with_trust(env.engine, env.plan, worker_id="w-x")
    assert claimed is not None
    import uuid as _uuid

    JC._start_job_with_trust(
        env.engine, env.plan, job_id=_uuid.UUID(claimed.job_id), worker_id="w-x"
    )
    with env.engine.connect() as c, c.begin(), pytest.raises(Exception) as ei:  # noqa: PT011
        c.execute(text("SET LOCAL ROLE minos_admin"))
        c.execute(
            text(f"UPDATE {_JOBS} SET status='SUCCEEDED' WHERE id=:i"),  # noqa: S608
            {"i": claimed.job_id},
        )
    assert getattr(ei.value.orig, "sqlstate", "") == "MN020"
    assert env.status(claimed.job_id) == "RUNNING"


def test_cross_worker_completion_is_rejected(env: Any) -> None:
    claimed = JC._claim_next_job_with_trust(env.engine, env.plan, worker_id="owner")
    assert claimed is not None
    import uuid as _uuid

    JC._start_job_with_trust(
        env.engine, env.plan, job_id=_uuid.UUID(claimed.job_id), worker_id="owner"
    )
    with env.engine.connect() as c, c.begin(), pytest.raises(Exception):  # noqa: B017, PT011
        c.execute(text("SET LOCAL ROLE minos_admin"))
        c.execute(
            text(
                "SELECT * FROM experiments.minos_l2f_fail_job(:h, :j, :w, 'EXECUTION_ERROR', "
                "NULL, NULL)"
            ),
            {"h": env.plan.plan_hash, "j": claimed.job_id, "w": "intruder"},
        )
    assert env.status(claimed.job_id) == "RUNNING"


# --------------------------------------------------------------------------- #
# verification + immutability
# --------------------------------------------------------------------------- #
def test_verifier_accepts_a_mixed_terminal_queue_and_is_non_mutating(env: Any) -> None:
    env.run()
    env.run(runner=FakeGatkRunner(exit_code=4))
    first = env.verify()
    assert first.status == HV.STATUS_PASS, first.failures
    assert first.checks["execution_results_independently_verified"] is True

    def _snapshot() -> Any:
        with env.engine.connect() as c:
            jobs = c.execute(
                text(
                    f"SELECT id, status, claimed_by, claimed_at, updated_at FROM {_JOBS} ORDER BY id"  # noqa: S608
                )
            ).all()
            results = c.execute(
                text(f"SELECT id, result_hash, created_at FROM {_RESULTS} ORDER BY id")  # noqa: S608
            ).all()
        return (
            [tuple(str(v) for v in r) for r in jobs],
            [tuple(str(v) for v in r) for r in results],
            {p.name: p.read_bytes() for p in env.artifacts()},
        )

    before = _snapshot()
    for _ in range(3):
        assert env.verify().status == HV.STATUS_PASS
    assert _snapshot() == before  # rows AND artifact bytes unchanged


def test_verifier_rejects_a_tampered_result_artifact(env: Any) -> None:
    env.run()
    assert env.verify().status == HV.STATUS_PASS
    vcf = next(p for p in env.artifacts() if p.name.endswith(".vcf"))
    os.chmod(vcf, 0o640)
    vcf.write_bytes(b"##fileformat=VCFv4.2\n#CHROM\ttampered\n")
    bad = env.verify()
    assert bad.status == HV.STATUS_FAIL
    assert "execution_results_independently_verified" in bad.failures


def test_legacy_and_f3c_data_unchanged_by_execution(env: Any) -> None:
    legacy_before = {
        t: _count(env.engine, f"SELECT count(*) FROM {t}")  # noqa: S608
        for t in ("profiling.profiles", "experiments.jobs", "experiments.results")
    }
    graph_before = {
        t: _count(env.engine, f"SELECT count(*) FROM experiments.{t}")  # noqa: S608
        for t in (
            "l2f_experiment_plans",
            "l2f_experiment_plan_members",
            "l2f_config_payloads",
            "l2f_experiment_plan_configs",
        )
    }
    env.run()
    env.run(runner=FakeGatkRunner(exit_code=5))
    for t, n in legacy_before.items():
        assert _count(env.engine, f"SELECT count(*) FROM {t}") == n  # noqa: S608
    for t, n in graph_before.items():
        assert _count(env.engine, f"SELECT count(*) FROM experiments.{t}") == n  # noqa: S608


def test_partial_queue_remains_valid(env: Any) -> None:
    env.run()
    r = env.verify()
    assert r.status == HV.STATUS_PASS
    assert r.persisted_job_count == 4  # 1 terminal, 3 still PENDING
    assert r.missing_job_count == env.plan.logical_job_count - 4


# --------------------------------------------------------------------------- #
# non-75 synthetic snapshots through the PRIVATE trust boundary
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(("spec", "train"), [(_SNAPSHOT_A, 4), (_SNAPSHOT_B, 2)])
def test_uneven_non75_snapshots_execute(
    isolated_pg_base_url: str, tmp_path: Path, spec: list[tuple[str, str, str]], train: int
) -> None:
    plan, identity, dataset_root = _prepare_env(isolated_pg_base_url, tmp_path, spec, jobs=2)
    assert plan.train_member_count == train
    with scratch_database(isolated_pg_base_url, "minos_f5_exec") as url:
        alembic_upgrade(url, _L2F)
        engine = _engine(url)
        try:
            with engine.connect() as conn, conn.begin():
                seed_upstream_for_plan(conn, plan, dataset_identity=identity)
            _persist_experiment_plan_with_trust(
                engine, plan, _CS, publisher=_publisher(_provisioned_root(tmp_path))
            )
            _enqueue_experiment_jobs_with_trust(engine, plan, _CS, start=0, count=2)
            engine.dispose()
            alembic_upgrade(url, _F5)
            engine = _engine(url)
            environment = Env(engine, plan, tmp_path, dataset_root)
            a, b = environment.run(), environment.run()
            assert a.status == "SUCCEEDED" and b.status == "SUCCEEDED"
            assert a.result_hash != b.result_hash  # distinct member/config identities
            assert environment.verify().status == HV.STATUS_PASS
        finally:
            engine.dispose()
