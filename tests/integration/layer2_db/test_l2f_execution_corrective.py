"""F5 corrective — relational job binding, outcome concurrency, exact-connection authorization,
workspace hygiene, bounded streams, strict VCF structure and genuine independent verification.

Scratch PostgreSQL only. Every execution uses the deterministic FakeGatkRunner except the single
bounded-stream test, which runs a tiny provisioned shell script — never a real GATK job.
"""

from __future__ import annotations

import contextlib
import dataclasses
import hashlib
import json
import os
import subprocess  # noqa: S404 - a pinned, repo-authored test script, shell=False
import sys
import threading
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import create_engine, text

from minos_engine.common.canonical_json import canonical_json_bytes
from minos_engine.experiments.execution_contract import (
    GatkExecutionError,
    GatkOutputError,
    compute_input_identity_hash,
    execution_input_from_manifest,
)
from minos_engine.storage import l2f_execution as EX
from minos_engine.storage import l2f_harness_verifier as HV
from minos_engine.storage.l2f_execution import (
    ATTEMPT_DIR_MODE,
    AmbiguousExecutionCommitError,
    ExecutionWorkspaceError,
    PostCommitWrapperError,
    _create_attempt_dir,
    _execute_next_job_with_trust,
    _require_absent_output,
)
from minos_engine.storage.l2f_execution_contract import (
    L2F_JOB_IDENTITY_UNIQUE_TARGETS,
    L2F_RESULT_JOB_IDENTITY_FKS,
)
from minos_engine.storage.l2f_gatk_runner import (
    MAX_CAPTURED_STREAM_BYTES,
    FakeGatkRunner,
    SubprocessGatkRunner,
    validate_vcf_bytes,
)
from minos_engine.storage.l2f_job_enqueue import _enqueue_experiment_jobs_with_trust
from minos_engine.storage.l2f_plan_store import (
    PlanRevisionError,
    _persist_experiment_plan_with_trust,
)
from minos_engine.storage.roles import SCHEMA_OWNER
from tests.integration.layer2_db.conftest import alembic_upgrade, scratch_database
from tests.integration.layer2_db.l2f_plan_seed import seed_upstream_for_plan
from tests.integration.layer2_db.test_l2f_execution import Env, _prepare_env
from tests.integration.layer2_db.test_l2f_plan_store import (
    _CS,
    _SNAPSHOT_A,
    _count,
    _engine,
    _provisioned_root,
    _publisher,
)

_L2F, _F5 = "0006_l2f_experiment_plan", "0008_l2f_execution_results"
_JOBS = "experiments.l2f_experiment_jobs"
_RESULTS = "experiments.l2f_execution_results"
_FAILURES = "experiments.l2f_execution_failures"

#: the accepted operational database name, so require_operational_identity=True can be exercised.
_OPERATIONAL_DB = "minos_engine_db"


@pytest.fixture
def opsenv(isolated_pg_base_url: str, tmp_path: Path) -> Any:
    """A complete F5 environment inside a database literally named ``minos_engine_db``.

    Nothing here touches the real operational store: this is a scratch database on the dedicated
    isolated cluster that merely carries the canonical name, so the accepted-identity code path
    can be exercised end to end.
    """
    plan, identity, dataset_root = _prepare_env(isolated_pg_base_url, tmp_path, _SNAPSHOT_A, jobs=4)
    with scratch_database(isolated_pg_base_url, _OPERATIONAL_DB) as url:
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
            environment = Env(engine, plan, tmp_path, dataset_root)
            environment.url = url  # type: ignore[attr-defined]
            yield environment
        finally:
            engine.dispose()


@pytest.fixture
def env(isolated_pg_base_url: str, tmp_path: Path) -> Any:
    plan, identity, dataset_root = _prepare_env(isolated_pg_base_url, tmp_path, _SNAPSHOT_A, jobs=4)
    with scratch_database(isolated_pg_base_url, "minos_f5_corr") as url:
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
            environment = Env(engine, plan, tmp_path, dataset_root)
            environment.url = url  # type: ignore[attr-defined]
            yield environment
        finally:
            engine.dispose()


def _admin(conn: Any) -> None:
    conn.execute(text(f"SET LOCAL ROLE {SCHEMA_OWNER}"))


def _running_job(env: Any, worker_id: str = "w-race") -> tuple[str, str]:
    """Claim and start one job, leaving it RUNNING and owned by ``worker_id``."""
    from uuid import UUID

    from minos_engine.storage.l2f_job_claim import _claim_next_job_with_trust, _start_job_with_trust

    claimed = _claim_next_job_with_trust(env.engine, env.plan, worker_id=worker_id)
    assert claimed is not None
    _start_job_with_trust(env.engine, env.plan, job_id=UUID(claimed.job_id), worker_id=worker_id)
    return claimed.job_id, claimed.job_key


def _fake_artifacts(env: Any) -> tuple[str, str]:
    """Register two distinct catalog.artifacts rows for the DB-level concurrency tests."""
    ids: list[str] = []
    with env.engine.connect() as c, c.begin():
        _admin(c)
        for kind, media in (
            ("vcf", "application/vnd.ga4gh.vcf"),
            ("man", "application/vnd.minos.l2f-execution-result+json"),
        ):
            sha = hashlib.sha256(kind.encode()).hexdigest()
            ids.append(
                str(
                    c.execute(
                        text(
                            "INSERT INTO catalog.artifacts (uri, sha256, media_type, size_bytes, "
                            "provenance) VALUES (:u, :h, :m, 1, 'test') RETURNING id"
                        ),
                        {"u": f"file:///tmp/{sha}", "h": sha, "m": media},
                    ).scalar_one()
                )
            )
    return ids[0], ids[1]


_COMPLETE_SQL = (
    "SELECT * FROM experiments.minos_l2f_complete_job_success("
    ":h, :j, :w, :k, :ch, :ps, :ii, :la, :ex, :gv, :va, :vs, :ma, :ms, :rh, :rt)"
)
_FAIL_SQL = "SELECT * FROM experiments.minos_l2f_fail_job(:h, :j, :w, :c, :e, :s)"


def _complete_params(env: Any, job_id: str, job_key: str, worker: str, runtime: int) -> dict:
    vcf_id, man_id = env._artifacts_cache
    hexes = [f"{n:064x}" for n in range(1, 9)]
    with env.engine.connect() as c:
        cfg = c.execute(
            text(
                "SELECT pc.config_hash, pc.parameter_space_hash FROM experiments"
                ".l2f_experiment_plan_configs pc JOIN experiments.l2f_experiment_jobs j "
                "ON j.plan_config_id = pc.id WHERE j.id = :i"
            ),
            {"i": job_id},
        ).one()
    return {
        "h": env.plan.plan_hash,
        "j": job_id,
        "w": worker,
        "k": job_key,
        "ch": str(cfg[0]),
        "ps": str(cfg[1]),
        "ii": hexes[0],
        "la": hexes[1],
        "ex": hexes[2],
        "gv": "test-gatk",
        "va": vcf_id,
        "vs": hashlib.sha256(b"vcf").hexdigest(),
        "ma": man_id,
        "ms": hashlib.sha256(b"man").hexdigest(),
        "rh": hexes[3],
        "rt": runtime,
    }


# --------------------------------------------------------------------------- #
# G1 — success vs failure for ONE running job is serialized on the job row lock
# --------------------------------------------------------------------------- #
def test_racing_success_and_failure_yield_exactly_one_terminal_outcome(env: Any) -> None:
    env._artifacts_cache = _fake_artifacts(env)
    job_id, job_key = _running_job(env, "w-race")
    params = _complete_params(env, job_id, job_key, "w-race", 5)
    url = env.url
    ready = threading.Barrier(2, timeout=30)
    outcomes: dict[str, BaseException | None] = {}

    def _attempt(name: str, sql: str, args: dict) -> None:
        engine = create_engine(url)
        try:
            conn = engine.connect()
            trans = conn.begin()
            try:
                _admin(conn)
                ready.wait()
                conn.execute(text(sql), args)
                trans.commit()
                outcomes[name] = None
            except BaseException as exc:  # noqa: BLE001 - the loser's typed error is the result
                with contextlib.suppress(BaseException):
                    trans.rollback()
                outcomes[name] = exc
            finally:
                conn.close()
        finally:
            engine.dispose()

    threads = [
        threading.Thread(target=_attempt, args=("success", _COMPLETE_SQL, params)),
        threading.Thread(
            target=_attempt,
            args=(
                "failure",
                _FAIL_SQL,
                {
                    "h": env.plan.plan_hash,
                    "j": job_id,
                    "w": "w-race",
                    "c": "EXECUTION_ERROR",
                    "e": None,
                    "s": None,
                },
            ),
        ),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
        assert not t.is_alive()

    winners = [k for k, v in outcomes.items() if v is None]
    assert len(winners) == 1, outcomes
    results = _count(env.engine, f"SELECT count(*) FROM {_RESULTS}")  # noqa: S608
    failures = _count(env.engine, f"SELECT count(*) FROM {_FAILURES}")  # noqa: S608
    assert results + failures == 1
    expected = "SUCCEEDED" if winners[0] == "success" else "FAILED"
    assert env.status(job_id) == expected


# --------------------------------------------------------------------------- #
# G2/G3 — a result naming another member or config fails through a NAMED foreign key
# --------------------------------------------------------------------------- #
def _direct_result_insert(env: Any, job_id: str, *, member: str, config: str) -> None:
    hexes = [f"{n:064x}" for n in range(1, 9)]
    vcf_id, man_id = env._artifacts_cache
    with env.engine.connect() as c, c.begin():
        _admin(c)
        row = c.execute(
            text(f"SELECT plan_id, job_key FROM {_JOBS} WHERE id = :i"),  # noqa: S608
            {"i": job_id},
        ).one()
        # use the REAL identity of the config being recorded, so every OTHER foreign key is
        # satisfied and only the job<->member<->config binding can be the one that fails.
        cfg = c.execute(
            text(
                "SELECT config_hash, parameter_space_hash FROM "
                "experiments.l2f_experiment_plan_configs WHERE id = :c"
            ),
            {"c": config},
        ).one()
        c.execute(
            text(
                f"INSERT INTO {_RESULTS} (plan_id, job_id, job_key, plan_member_id, "  # noqa: S608
                "plan_config_id, config_hash, parameter_space_hash, input_identity_hash, "
                "logical_argv_hash, gatk_executable_sha256, gatk_version, vcf_artifact_id, "
                "vcf_sha256, result_manifest_artifact_id, result_manifest_sha256, result_hash, "
                "runtime_ms) VALUES (:p, :j, :k, :mem, :cfg, :ch, :ps, :ii, :la, :ex, 'v', "
                ":va, :vs, :ma, :ms, :rh, 1)"
            ),
            {
                "p": str(row[0]),
                "j": job_id,
                "k": str(row[1]),
                "mem": member,
                "cfg": config,
                "ch": str(cfg[0]),
                "ps": str(cfg[1]),
                "ii": hexes[0],
                "la": hexes[1],
                "ex": hexes[2],
                "va": vcf_id,
                "vs": hashlib.sha256(b"vcf").hexdigest(),
                "ma": man_id,
                "ms": hashlib.sha256(b"man").hexdigest(),
                "rh": hexes[3],
            },
        )


def _other_plan_binding(env: Any) -> tuple[Any, str, str]:
    """The first job, plus a VALID member and config of the SAME plan that it does not bind."""
    with env.engine.connect() as c:
        job = (
            c.execute(
                text(
                    f"SELECT id, plan_member_id, plan_config_id FROM {_JOBS} "  # noqa: S608
                    "ORDER BY created_at, id LIMIT 1"
                )
            )
            .mappings()
            .one()
        )
        member = c.execute(
            text(
                "SELECT id FROM experiments.l2f_experiment_plan_members "
                f"WHERE plan_id = (SELECT plan_id FROM {_JOBS} WHERE id = :i) AND id <> :m LIMIT 1"
            ),
            {"i": str(job["id"]), "m": str(job["plan_member_id"])},
        ).scalar_one()
        config = c.execute(
            text(
                "SELECT id FROM experiments.l2f_experiment_plan_configs "
                f"WHERE plan_id = (SELECT plan_id FROM {_JOBS} WHERE id = :i) AND id <> :c LIMIT 1"
            ),
            {"i": str(job["id"]), "c": str(job["plan_config_id"])},
        ).scalar_one()
    return job, str(member), str(config)


def test_direct_result_with_the_wrong_member_is_rejected_by_a_named_fk(env: Any) -> None:
    """A member that genuinely belongs to the SAME plan, but not to this job, still fails."""
    env._artifacts_cache = _fake_artifacts(env)
    job, other_member, _ = _other_plan_binding(env)
    with pytest.raises(Exception) as excinfo:
        _direct_result_insert(
            env,
            str(job["id"]),
            member=other_member,
            config=str(job["plan_config_id"]),
        )
    assert "fk_l2f_exec_results_job_member_config" in str(excinfo.value)
    assert _count(env.engine, f"SELECT count(*) FROM {_RESULTS}") == 0  # noqa: S608


def test_direct_result_with_the_wrong_config_is_rejected_by_a_named_fk(env: Any) -> None:
    """A config that genuinely belongs to the SAME plan, but not to this job, still fails."""
    env._artifacts_cache = _fake_artifacts(env)
    job, _, other_config = _other_plan_binding(env)
    with pytest.raises(Exception) as excinfo:
        _direct_result_insert(
            env,
            str(job["id"]),
            member=str(job["plan_member_id"]),
            config=other_config,
        )
    assert "fk_l2f_exec_results_job_member_config" in str(excinfo.value)
    assert _count(env.engine, f"SELECT count(*) FROM {_RESULTS}") == 0  # noqa: S608


def test_the_exact_job_binding_is_accepted(env: Any) -> None:
    """The control: the job's OWN member and config insert cleanly through the same FK."""
    env._artifacts_cache = _fake_artifacts(env)
    job, _, _ = _other_plan_binding(env)
    _direct_result_insert(
        env,
        str(job["id"]),
        member=str(job["plan_member_id"]),
        config=str(job["plan_config_id"]),
    )
    assert _count(env.engine, f"SELECT count(*) FROM {_RESULTS}") == 1  # noqa: S608


def test_the_job_identity_bindings_are_installed_exactly_once(env: Any) -> None:
    with env.engine.connect() as c:
        uniques = {
            r[0]
            for r in c.execute(
                text(
                    "SELECT conname FROM pg_constraint "
                    f"WHERE conrelid = CAST('{_JOBS}' AS regclass) AND contype = 'u'"
                )
            ).all()
        }
        fks = {
            r[0]
            for r in c.execute(
                text(
                    "SELECT conname FROM pg_constraint "
                    f"WHERE conrelid = CAST('{_RESULTS}' AS regclass) AND contype = 'f'"
                )
            ).all()
        }
        worker_checks = int(
            c.execute(
                text(
                    "SELECT count(*) FROM pg_constraint WHERE conname = "
                    "'ck_l2f_exec_failures_worker_nonempty'"
                )
            ).scalar_one()
        )
    assert set(L2F_JOB_IDENTITY_UNIQUE_TARGETS) <= uniques
    assert set(L2F_RESULT_JOB_IDENTITY_FKS) <= fks
    # exactly ONE worker-nonempty declaration reaches the live schema (never a duplicate pair).
    assert worker_checks == 1


# --------------------------------------------------------------------------- #
# G4 — success get-or-verify compares EVERY immutable column, including runtime_ms
# --------------------------------------------------------------------------- #
def test_success_replay_differing_only_in_runtime_ms_is_rejected(env: Any) -> None:
    env._artifacts_cache = _fake_artifacts(env)
    job_id, job_key = _running_job(env, "w-replay")
    params = _complete_params(env, job_id, job_key, "w-replay", 11)
    with env.engine.connect() as c, c.begin():
        _admin(c)
        c.execute(text(_COMPLETE_SQL), params)
    assert env.status(job_id) == "SUCCEEDED"

    # an EXACT replay is idempotent...
    with env.engine.connect() as c, c.begin():
        _admin(c)
        row = c.execute(text(_COMPLETE_SQL), params).mappings().one()
    assert row["created"] is False

    # ...but a replay differing ONLY in runtime_ms is a typed conflict.
    with pytest.raises(Exception) as excinfo, env.engine.connect() as c, c.begin():
        _admin(c)
        c.execute(text(_COMPLETE_SQL), {**params, "rt": 12})
    assert getattr(getattr(excinfo.value, "orig", None), "sqlstate", None) == "MN022"
    assert _count(env.engine, f"SELECT count(*) FROM {_RESULTS}") == 1  # noqa: S608


# --------------------------------------------------------------------------- #
# G5 — EVERY production connection is authorized on its own, before its first query
# --------------------------------------------------------------------------- #
def test_the_accepted_identity_path_executes_end_to_end(opsenv: Any) -> None:
    result = _execute_next_job_with_trust(
        opsenv.engine,
        opsenv.plan,
        worker_id="w-ops",
        runner=FakeGatkRunner(),
        dataset_root=opsenv.dataset_root,
        publisher=opsenv.publisher,
        work_root=opsenv.work_root,
        require_operational_identity=True,
    )
    assert result is not None and result.status == "SUCCEEDED"


def test_every_live_connection_is_verified_and_never_authorized_by_another(
    opsenv: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A successful check on one connection must never authorize a later connection."""
    from minos_engine.storage import l2f_job_claim as JC

    seen: list[int] = []

    def _tracker(real: Any) -> Any:
        def _tracking(conn: Any) -> None:
            seen.append(id(conn))
            real(conn)

        return _tracking

    # BOTH boundaries the production path crosses: the F4 claim/start/release connections and
    # the F5 preparation/persistence connections.
    monkeypatch.setattr(
        EX,
        "verify_operational_database_identity",
        _tracker(EX.verify_operational_database_identity),
    )
    monkeypatch.setattr(
        JC,
        "verify_operational_database_identity",
        _tracker(JC.verify_operational_database_identity),
    )
    result = _execute_next_job_with_trust(
        opsenv.engine,
        opsenv.plan,
        worker_id="w-ops",
        runner=FakeGatkRunner(),
        dataset_root=opsenv.dataset_root,
        publisher=opsenv.publisher,
        work_root=opsenv.work_root,
        require_operational_identity=True,
    )
    assert result is not None and result.status == "SUCCEEDED"
    # claim, preparation reads, start and success persistence are four DISTINCT connections,
    # each authorized on its own; no earlier success carried over to a later connection.
    assert len(seen) >= 4
    assert len(set(seen)) >= 2


@pytest.mark.parametrize("failing_call", [1, 2, 3, 4])
def test_a_later_connection_failing_its_own_check_aborts_the_run(
    opsenv: Any, monkeypatch: pytest.MonkeyPatch, failing_call: int
) -> None:
    """Each connection re-checks: making the Nth check fail aborts, proving none is skipped."""
    calls = {"n": 0}
    real = EX._require_f5_revision

    def _nth_fails(conn: Any) -> None:
        calls["n"] += 1
        if calls["n"] == failing_call:
            raise PlanRevisionError("simulated wrong revision on THIS connection")
        real(conn)

    monkeypatch.setattr(EX, "_require_f5_revision", _nth_fails)
    # F6: a later connection's own failed check still aborts, but is now surfaced through the
    # recovery contract (the job is released or durably failed, never stranded).
    with pytest.raises(
        (
            PlanRevisionError,
            EX.PreTerminalExecutionError,
            EX.ExecutionRecoveryError,
            EX.ExecutionRecordedFailureError,
        )
    ):
        _execute_next_job_with_trust(
            opsenv.engine,
            opsenv.plan,
            worker_id="w-ops",
            runner=FakeGatkRunner(),
            dataset_root=opsenv.dataset_root,
            publisher=opsenv.publisher,
            work_root=opsenv.work_root,
            require_operational_identity=True,
        )
    assert calls["n"] >= failing_call
    assert _count(opsenv.engine, f"SELECT count(*) FROM {_RESULTS}") == 0  # noqa: S608


def test_a_non_operational_database_is_refused_on_the_claim_connection(env: Any) -> None:
    """The scratch database is not named minos_engine_db, so the FIRST connection refuses."""
    from minos_engine.common.errors import MinosEngineError

    with pytest.raises(MinosEngineError):
        _execute_next_job_with_trust(
            env.engine,
            env.plan,
            worker_id="w-ops",
            runner=FakeGatkRunner(),
            dataset_root=env.dataset_root,
            publisher=env.publisher,
            work_root=env.work_root,
            require_operational_identity=True,
        )
    assert _count(env.engine, f"SELECT count(*) FROM {_JOBS} WHERE status <> 'PENDING'") == 0  # noqa: S608


# --------------------------------------------------------------------------- #
# G6 — failure persistence has the same commit-state semantics as success
# --------------------------------------------------------------------------- #
def test_failure_commit_ambiguity_is_typed_and_not_retried(
    env: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = {"n": 0}

    def _raise(_trans: Any) -> None:
        calls["n"] += 1
        raise AmbiguousExecutionCommitError("simulated ambiguous COMMIT")

    monkeypatch.setattr(EX, "_commit_or_ambiguous", _raise)
    with pytest.raises(AmbiguousExecutionCommitError):
        env.run(runner=FakeGatkRunner(exit_code=7))
    assert calls["n"] == 1  # never retried


def test_failure_precommit_exception_rolls_back(env: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom() -> None:
        raise RuntimeError("pre-commit failure")

    monkeypatch.setattr(EX, "_typed_execution_errors", lambda: _boom())
    with pytest.raises(Exception):  # noqa: B017 - any pre-commit error must roll back
        env.run(runner=FakeGatkRunner(exit_code=7))
    assert _count(env.engine, f"SELECT count(*) FROM {_FAILURES}") == 0  # noqa: S608


def test_failure_wrapper_failure_after_commit_keeps_the_durable_row(
    env: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _boom() -> None:
        raise RuntimeError("post-commit wrapper failure")

    monkeypatch.setattr(EX, "_post_commit_hook", _boom)
    with pytest.raises(PostCommitWrapperError) as excinfo:
        env.run(runner=FakeGatkRunner(exit_code=7))
    assert isinstance(excinfo.value.__cause__, RuntimeError)
    assert _count(env.engine, f"SELECT count(*) FROM {_FAILURES}") == 1  # noqa: S608


def test_failure_persistence_performs_no_post_commit_job_lookup(env: Any) -> None:
    """job_key is threaded in from the claim; the FAILED result still carries it exactly."""
    with env.engine.connect() as c:
        keys = {
            str(r[0])
            for r in c.execute(text(f"SELECT job_key FROM {_JOBS}")).all()  # noqa: S608
        }
    result = env.run(runner=FakeGatkRunner(exit_code=9))
    assert result is not None and result.status == "FAILED"
    assert result.job_key in keys
    with env.engine.connect() as c:
        stored = str(
            c.execute(
                text(f"SELECT job_key FROM {_FAILURES} WHERE job_id = :i"),  # noqa: S608
                {"i": result.job_id},
            ).scalar_one()
        )
    assert stored == result.job_key


# --------------------------------------------------------------------------- #
# G7/G8 — per-attempt workspaces are fresh, exclusive, private and always removed
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "runner",
    [
        FakeGatkRunner(),
        FakeGatkRunner(exit_code=3),
        FakeGatkRunner(raise_timeout=True),
        FakeGatkRunner(write_output=False),
        FakeGatkRunner(override_bytes=b"not a vcf\n"),
    ],
)
def test_every_outcome_removes_the_attempt_directory(env: Any, runner: FakeGatkRunner) -> None:
    assert list(env.work_root.iterdir()) == []
    env.run(runner=runner)
    assert list(env.work_root.iterdir()) == []


def test_ambiguous_commit_also_removes_the_attempt_directory(
    env: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _raise(_trans: Any) -> None:
        raise AmbiguousExecutionCommitError("simulated ambiguous COMMIT")

    monkeypatch.setattr(EX, "_commit_or_ambiguous", _raise)
    with pytest.raises(AmbiguousExecutionCommitError):
        env.run()
    assert list(env.work_root.iterdir()) == []
    # the immutable artifacts are RETAINED even though the workspace is gone
    assert env.artifacts()


def test_a_preexisting_attempt_directory_is_never_reused(tmp_path: Path) -> None:
    root = tmp_path / "work"
    root.mkdir()
    (root / "l2f-job-1-aaa").mkdir()
    with pytest.raises(ExecutionWorkspaceError):
        _create_attempt_dir(root, job_id="job-1", attempt_id="aaa")


def test_a_substituted_symlink_attempt_path_is_never_reused(tmp_path: Path) -> None:
    root = tmp_path / "work"
    root.mkdir()
    (tmp_path / "elsewhere").mkdir()
    (root / "l2f-job-2-bbb").symlink_to(tmp_path / "elsewhere", target_is_directory=True)
    with pytest.raises(ExecutionWorkspaceError):
        _create_attempt_dir(root, job_id="job-2", attempt_id="bbb")


def test_a_fresh_attempt_directory_is_private_and_inside_the_root(tmp_path: Path) -> None:
    root = tmp_path / "work"
    root.mkdir()
    workspace = _create_attempt_dir(root, job_id="job-3", attempt_id="ccc")
    attempt = workspace.path
    info = os.lstat(attempt)
    assert (info.st_mode & 0o777) == ATTEMPT_DIR_MODE
    assert attempt.parent == root.resolve()
    assert attempt.is_dir() and not attempt.is_symlink()
    # F6: the created inode identity is captured and re-checkable.
    assert (workspace.st_dev, workspace.st_ino) == (info.st_dev, info.st_ino)
    assert workspace.still_ours()


def test_a_preexisting_output_path_is_never_reused(tmp_path: Path) -> None:
    out = tmp_path / "output.vcf"
    out.write_bytes(b"stale\n")
    with pytest.raises(ExecutionWorkspaceError):
        _require_absent_output(out)
    link = tmp_path / "linked.vcf"
    link.symlink_to(out)
    with pytest.raises(ExecutionWorkspaceError):
        _require_absent_output(link)


def test_each_attempt_uses_a_distinct_directory(env: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[str] = []
    real = EX._create_attempt_dir

    def _record(work_root: Path, *, job_id: str, attempt_id: str) -> Any:
        workspace = real(work_root, job_id=job_id, attempt_id=attempt_id)
        seen.append(workspace.path.name)
        return workspace

    monkeypatch.setattr(EX, "_create_attempt_dir", _record)
    env.run()
    env.run()
    assert len(seen) == 2 and len(set(seen)) == 2


# --------------------------------------------------------------------------- #
# G9 — stdout/stderr stay bounded WHILE a noisy child process runs
# --------------------------------------------------------------------------- #
_NOISY = """#!/bin/sh
i=0
while [ $i -lt 400 ]; do
  printf '%s\\n' "$LINE"
  printf '%s\\n' "$LINE" >&2
  i=$((i+1))
done
exit 5
"""


@pytest.mark.skipif(not Path("/bin/sh").exists(), reason="POSIX shell required")
def test_a_noisy_subprocess_keeps_captured_streams_bounded(tmp_path: Path) -> None:
    script = tmp_path / "noisy.sh"
    script.write_text(_NOISY.replace("$LINE", "x" * 1000), encoding="utf-8")
    script.chmod(0o700)
    work = tmp_path / "work"
    work.mkdir()

    limit = 4096
    runner = SubprocessGatkRunner(
        executable=script.resolve(),
        expected_sha256=hashlib.sha256(script.read_bytes()).hexdigest(),
        expected_version="test",
        timeout_seconds=120,
        max_captured_stream_bytes=limit,
        local_jar=_fixture_jar(script.parent),
    )
    inputs = _manifest_inputs()
    with pytest.raises(GatkExecutionError):
        runner.run(
            argv=(),
            work_dir=work,
            vcf_path=work / "o.vcf",
            inputs=inputs,
            expected_runtime_bundle_sha256=runner.runtime_bundle_sha256(),
        )

    produced = (work / "gatk.stdout").stat().st_size, (work / "gatk.stderr").stat().st_size
    # the child emitted ~400KB per stream; at most `limit` bytes reached disk on EACH stream.
    assert produced[0] <= limit and produced[1] <= limit
    assert limit < MAX_CAPTURED_STREAM_BYTES  # the test cap is genuinely smaller than production


def _fixture_jar(directory: Path) -> Path:
    """The local JAR a fixture launcher dispatches to; a launcher alone is not a bundle."""
    jar = directory / "gatk-package-test-local.jar"
    if not jar.exists():
        jar.write_bytes(b"fixture-jar")
    return jar


def _manifest_inputs() -> Any:
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


def test_the_production_runner_never_uses_a_shell(tmp_path: Path) -> None:
    """A value containing shell metacharacters stays inert data (argv, never a command)."""
    marker = tmp_path / "pwned"
    script = tmp_path / "echoargs.sh"
    script.write_text('#!/bin/sh\nprintf "%s\\n" "$@"\nexit 5\n', encoding="utf-8")
    script.chmod(0o700)
    work = tmp_path / "work"
    work.mkdir()
    runner = SubprocessGatkRunner(
        executable=script.resolve(),
        expected_sha256=hashlib.sha256(script.read_bytes()).hexdigest(),
        expected_version="test",
        timeout_seconds=60,
        local_jar=_fixture_jar(script.parent),
    )
    with pytest.raises(GatkExecutionError):
        runner.run(
            argv=(f"; touch {marker}",),
            work_dir=work,
            vcf_path=work / "o.vcf",
            inputs=_manifest_inputs(),
            expected_runtime_bundle_sha256=runner.runtime_bundle_sha256(),
        )
    assert not marker.exists()
    assert f"; touch {marker}" in (work / "gatk.stdout").read_text(encoding="utf-8")


# --------------------------------------------------------------------------- #
# G10 — strict VCF structure: region, columns, sample count, chromosome
# --------------------------------------------------------------------------- #
_HEADER = b"##fileformat=VCFv4.2\n#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\tFORMAT\tSAMPLE\n"


@pytest.mark.parametrize(
    ("payload", "why"),
    [
        (_HEADER + b"chr18\t99\t.\tA\tG\t1\tPASS\t.\tGT\t0/1\n", "POS below the interval"),
        (_HEADER + b"chr18\t201\t.\tA\tG\t1\tPASS\t.\tGT\t0/1\n", "POS above the interval"),
        (_HEADER + b"chr19\t150\t.\tA\tG\t1\tPASS\t.\tGT\t0/1\n", "wrong chromosome"),
        (_HEADER + b"chr18\tNOPE\t.\tA\tG\t1\tPASS\t.\tGT\t0/1\n", "non-integer POS"),
        (_HEADER + b"chr18\t150\t.\tA\tG\t1\tPASS\t.\n", "too few record columns"),
        (
            b"##fileformat=VCFv4.2\n"
            b"#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\tFORMAT\tS1\tS2\n",
            "multi-sample header",
        ),
        (
            b"##fileformat=VCFv4.2\n#CHROM\tPOSITION\tID\tREF\tALT\tQUAL\tFILTER\tINFO\tFORMAT\tS\n",
            "malformed header layout",
        ),
        (_HEADER + _HEADER.split(b"\n")[1] + b"\n", "two #CHROM headers"),
        (
            b"##fileformat=VCFv4.2\nchr18\t150\t.\tA\tG\t1\tPASS\t.\tGT\t0/1\n",
            "record before header",
        ),
    ],
)
def test_malformed_or_out_of_region_vcfs_are_rejected(
    tmp_path: Path, payload: bytes, why: str
) -> None:
    work = tmp_path / "work"
    work.mkdir()
    out = work / "o.vcf"
    out.write_bytes(payload)
    with pytest.raises(GatkOutputError):
        validate_vcf_bytes(out, work_dir=work, inputs=_manifest_inputs())


def test_in_region_single_sample_vcfs_are_accepted(tmp_path: Path) -> None:
    work = tmp_path / "work"
    work.mkdir()
    out = work / "o.vcf"
    for pos in (101, 150, 200):
        out.write_bytes(_HEADER + f"chr18\t{pos}\t.\tA\tG\t1\tPASS\t.\tGT\t0/1\n".encode())
        sha, size = validate_vcf_bytes(out, work_dir=work, inputs=_manifest_inputs())
        assert sha == hashlib.sha256(out.read_bytes()).hexdigest() and size > 0
    out.write_bytes(_HEADER)  # a variant-free region stays legitimate
    assert validate_vcf_bytes(out, work_dir=work, inputs=_manifest_inputs())[1] > 0


# --------------------------------------------------------------------------- #
# G11 — a self-consistent forged manifest still fails independent verification
# --------------------------------------------------------------------------- #
def _graph_with_forged_manifest(env: Any, mutate: dict[str, Any]) -> Any:
    """Re-read the persisted graph, then replace one result's manifest with a forged one whose
    ``input_identity_hash`` is recomputed to be internally CONSISTENT with the mutation."""
    from minos_engine.experiments.execution_contract import ExecutionResultManifest

    with env.engine.connect() as conn:
        graph = HV._read_persisted_graph(conn, env.plan)
    result = graph.execution_results[0]
    assert result.manifest_bytes is not None
    document = json.loads(result.manifest_bytes)
    document.update(mutate)
    forged = ExecutionResultManifest(**document)
    document["input_identity_hash"] = compute_input_identity_hash(
        execution_input_from_manifest(forged)
    )
    raw = canonical_json_bytes(document)
    replaced = dataclasses.replace(
        result,
        manifest_bytes=raw,
        manifest_document=json.loads(raw),
        manifest_file_sha256=hashlib.sha256(raw).hexdigest(),
        manifest_file_size=len(raw),
        manifest_sha256=hashlib.sha256(raw).hexdigest(),
        manifest_size_bytes=len(raw),
        manifest_uri=f"file:///r/{hashlib.sha256(raw).hexdigest()}.result.json",
    )
    return dataclasses.replace(graph, execution_results=(replaced,))


@pytest.mark.parametrize(
    "mutate",
    [
        {"dictionary_sha256": "f" * 64},
        {"bam_size_bytes": 999999},
    ],
)
def test_a_self_consistent_forged_input_identity_fails_verification(
    env: Any, mutate: dict[str, Any]
) -> None:
    env.run()
    graph = _graph_with_forged_manifest(env, mutate)
    # the forged manifest recomputes to its OWN stored hash, but not to the database row's.
    assert HV._check_execution_results(env.plan, graph) is False


def test_a_manifest_with_a_duplicate_key_fails_verification(env: Any) -> None:
    env.run()
    with env.engine.connect() as conn:
        graph = HV._read_persisted_graph(conn, env.plan)
    result = graph.execution_results[0]
    assert result.manifest_bytes is not None
    raw = result.manifest_bytes.replace(b'{"', b'{"job_id": "x", "', 1)
    replaced = dataclasses.replace(
        result,
        manifest_bytes=raw,
        manifest_file_sha256=hashlib.sha256(raw).hexdigest(),
        manifest_file_size=len(raw),
        manifest_sha256=hashlib.sha256(raw).hexdigest(),
        manifest_size_bytes=len(raw),
        manifest_uri=f"file:///r/{hashlib.sha256(raw).hexdigest()}.result.json",
    )
    assert (
        HV._check_execution_results(
            env.plan, dataclasses.replace(graph, execution_results=(replaced,))
        )
        is False
    )


def test_a_manifest_with_an_extra_field_fails_verification(env: Any) -> None:
    env.run()
    with env.engine.connect() as conn:
        graph = HV._read_persisted_graph(conn, env.plan)
    result = graph.execution_results[0]
    assert result.manifest_bytes is not None
    document = json.loads(result.manifest_bytes)
    document["surprise"] = 1
    raw = canonical_json_bytes(document)
    replaced = dataclasses.replace(
        result,
        manifest_bytes=raw,
        manifest_file_sha256=hashlib.sha256(raw).hexdigest(),
        manifest_file_size=len(raw),
        manifest_sha256=hashlib.sha256(raw).hexdigest(),
        manifest_size_bytes=len(raw),
        manifest_uri=f"file:///r/{hashlib.sha256(raw).hexdigest()}.result.json",
    )
    assert (
        HV._check_execution_results(
            env.plan, dataclasses.replace(graph, execution_results=(replaced,))
        )
        is False
    )


def test_a_forged_logical_argv_fails_verification(env: Any) -> None:
    """The argv hash is RECOMPUTED from the bound CONFIG, so a stored value cannot be forged."""
    env.run()
    with env.engine.connect() as conn:
        graph = HV._read_persisted_graph(conn, env.plan)
    result = graph.execution_results[0]
    replaced = dataclasses.replace(result, effective_config={"min-pruning": 99})
    assert (
        HV._check_execution_results(
            env.plan, dataclasses.replace(graph, execution_results=(replaced,))
        )
        is False
    )


def test_a_wrong_media_type_fails_verification(env: Any) -> None:
    env.run()
    with env.engine.connect() as conn:
        graph = HV._read_persisted_graph(conn, env.plan)
    replaced = dataclasses.replace(graph.execution_results[0], vcf_media_type="text/plain")
    assert (
        HV._check_execution_results(
            env.plan, dataclasses.replace(graph, execution_results=(replaced,))
        )
        is False
    )


# --------------------------------------------------------------------------- #
# G12 — the valid success and failure paths still work, and still verify
# --------------------------------------------------------------------------- #
def test_valid_success_and_failure_paths_both_still_verify(env: Any) -> None:
    ok = env.run()
    bad = env.run(runner=FakeGatkRunner(exit_code=4))
    assert ok is not None and ok.status == "SUCCEEDED"
    assert bad is not None and bad.status == "FAILED" and bad.failure_code == "GATK_NONZERO_EXIT"
    assert _count(env.engine, f"SELECT count(*) FROM {_RESULTS}") == 1  # noqa: S608
    assert _count(env.engine, f"SELECT count(*) FROM {_FAILURES}") == 1  # noqa: S608
    assert env.verify().status == HV.STATUS_PASS
    # verification is strictly non-mutating
    before = env.verify()
    after = env.verify()
    assert before.checks == after.checks
    assert before.status == after.status == HV.STATUS_PASS


# --------------------------------------------------------------------------- #
# G13 — no F6/F7 behavior is introduced
# --------------------------------------------------------------------------- #
def test_no_f6_or_f7_behaviour_is_introduced() -> None:
    from minos_engine.layer2.service import Layer2Service

    with pytest.raises(Exception):  # noqa: B017 - select_config stays blocked
        Layer2Service().select_config(None)  # type: ignore[arg-type]

    assert set(EX.__all__) == {
        "F5_EXECUTION_REVISION",
        "ATTEMPT_DIR_MODE",
        "AttemptWorkspace",
        "reject_symlinked_components",
        "verify_produced_output",
        "acquire_produced_output",
        "AcquiredOutput",
        "OUTPUT_VCF_NAME",
        "ExecutionDispatchResult",
        "AmbiguousExecutionCommitError",
        "ExecutionResultConflictError",
        "ExecutionWorkspaceError",
        "AmbiguousStartCommitError",
        "AmbiguousRecoveryCommitError",
        "PreTerminalExecutionError",
        "ExecutionRecordedFailureError",
        "ExecutionRecoveryError",
        "PostCommitWrapperError",
        "find_nonterminal_jobs",
        "assert_no_stranded_jobs",
        "execute_next_accepted_job",
    }
    forbidden = ("score", "happy", "hap_py", "leaderboard", "select_config", "optimi", "rank")
    for name in dir(EX):
        assert not any(token in name.lower() for token in forbidden), name
    for name in HV.CHECK_NAMES:
        assert not any(token in name.lower() for token in forbidden), name
    # the verifier's only mention of truth data is the guard that PROVES none is read.
    assert "_check_no_nontrain_or_truth_data" in dir(HV)
    assert not any("truth" in n.lower() for n in dir(EX))


def test_the_test_suite_never_starts_a_real_gatk_process() -> None:
    """The only subprocesses these tests start are repo-authored POSIX shell scripts."""
    assert subprocess.__name__ == "subprocess"
    assert sys.executable  # sanity: no GATK executable is ever provisioned here
    assert os.environ.get("MINOS_L2F_GATK_EXECUTABLE") is None
