"""F3-C2 bounded job enqueue — real-PostgreSQL behavioral tests.

Covers bounded creation, idempotent + overlapping replay, concurrency, alternate unique
conflicts, status/claim preservation, missing-graph and wrong-identity/revision guards,
transaction rollback, legacy/F3-C1 immutability, and non-75 derived job counts. Uses the
dedicated ``isolated_pg_base_url`` cluster (see the plan-store suite for the CI-isolation
rationale); the real operational store is never touched.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Engine, text

from minos_engine.experiments.plan import iter_logical_jobs
from minos_engine.storage import l2f_job_enqueue as EN
from minos_engine.storage.database import OperationalDatabaseIdentityError
from minos_engine.storage.l2f_job_enqueue import (
    JobEnqueueRangeError,
    JobGraphMissingError,
    enqueue_accepted_experiment_jobs,
)
from minos_engine.storage.l2f_plan_store import (
    ImmutableMetadataConflictError,
    PlanRevisionError,
    _persist_experiment_plan_with_trust,
)
from tests.integration.layer2_db.conftest import alembic_upgrade, scratch_database
from tests.integration.layer2_db.l2f_plan_seed import seed_upstream_for_plan
from tests.integration.layer2_db.test_l2f_plan_store import (
    _ACCEPTED_PLAN,
    _CS,
    _SNAPSHOT_A,
    _SNAPSHOT_B,
    _artifact_files,
    _count,
    _engine,
    _graph_counts,
    _provisioned_root,
    _publisher,
    _synthetic_plan,
)

_HEAD = "0006_l2f_experiment_plan"
_PREV = "0005_l2e_feature_view"
_OP_DB = "minos_engine_db"


def _persist(engine: Engine, plan: Any, root: Path) -> None:
    _persist_experiment_plan_with_trust(engine, plan, _CS, publisher=_publisher(root))


def _enqueue(engine: Engine, plan: Any, *, start: int, count: int) -> Any:
    return EN._enqueue_experiment_jobs_with_trust(engine, plan, _CS, start=start, count=count)


def _job_keys(engine: Engine, plan_hash: str) -> list[str]:
    with engine.connect() as c:
        rows = c.execute(
            text(
                "SELECT j.job_key FROM experiments.l2f_experiment_jobs j "
                "JOIN experiments.l2f_experiment_plans p ON p.id = j.plan_id "
                "WHERE p.plan_hash = :h ORDER BY j.job_key"
            ),
            {"h": plan_hash},
        ).all()
    return sorted(r[0] for r in rows)


def _plan_id(engine: Engine, plan_hash: str) -> str:
    with engine.connect() as c:
        return str(
            c.execute(
                text("SELECT id FROM experiments.l2f_experiment_plans WHERE plan_hash=:h"),
                {"h": plan_hash},
            ).scalar_one()
        )


def _expected_keys(plan: Any, start: int, count: int) -> list[str]:
    jobs = list(iter_logical_jobs(plan))
    return sorted(jobs[k].job_key for k in range(start, start + count))


# --------------------------------------------------------------------------- #
# bounded creation + idempotent / overlapping replay
# --------------------------------------------------------------------------- #
def test_first_bounded_range_creates_exact_range(isolated_pg_base_url: str, tmp_path: Path) -> None:
    plan = _synthetic_plan(_SNAPSHOT_A)
    with scratch_database(isolated_pg_base_url, "minos_l2f_synth") as url:
        alembic_upgrade(url, _HEAD)
        engine = _engine(url)
        try:
            with engine.connect() as conn, conn.begin():
                seed_upstream_for_plan(conn, plan)
            _persist(engine, plan, _provisioned_root(tmp_path))
            r = _enqueue(engine, plan, start=0, count=5)
            assert (r.requested_start, r.requested_count) == (0, 5)
            assert r.created_count == 5 and r.existing_count == 0 and r.total_jobs_for_plan == 5
            assert r.plan_hash == plan.plan_hash
            assert _job_keys(engine, plan.plan_hash) == _expected_keys(plan, 0, 5)
            # all newly enqueued jobs are PENDING with no claim metadata.
            assert (
                _count(
                    engine,
                    "SELECT count(*) FROM experiments.l2f_experiment_jobs "
                    "WHERE status='PENDING' AND claimed_by IS NULL AND claimed_at IS NULL",
                )
                == 5
            )
        finally:
            engine.dispose()


def test_sequential_replay_creates_zero_new(isolated_pg_base_url: str, tmp_path: Path) -> None:
    plan = _synthetic_plan(_SNAPSHOT_A)
    with scratch_database(isolated_pg_base_url, "minos_l2f_synth") as url:
        alembic_upgrade(url, _HEAD)
        engine = _engine(url)
        try:
            with engine.connect() as conn, conn.begin():
                seed_upstream_for_plan(conn, plan)
            _persist(engine, plan, _provisioned_root(tmp_path))
            first = _enqueue(engine, plan, start=0, count=6)
            second = _enqueue(engine, plan, start=0, count=6)
            assert first.created_count == 6
            assert second.created_count == 0 and second.existing_count == 6
            assert second.total_jobs_for_plan == 6
        finally:
            engine.dispose()


def test_partial_overlap_creates_only_missing(isolated_pg_base_url: str, tmp_path: Path) -> None:
    plan = _synthetic_plan(_SNAPSHOT_A)
    with scratch_database(isolated_pg_base_url, "minos_l2f_synth") as url:
        alembic_upgrade(url, _HEAD)
        engine = _engine(url)
        try:
            with engine.connect() as conn, conn.begin():
                seed_upstream_for_plan(conn, plan)
            _persist(engine, plan, _provisioned_root(tmp_path))
            _enqueue(engine, plan, start=0, count=5)
            r = _enqueue(engine, plan, start=3, count=5)  # indices 3..7 (3,4 exist; 5,6,7 new)
            assert r.created_count == 3 and r.existing_count == 2
            assert r.total_jobs_for_plan == 8
            assert _job_keys(engine, plan.plan_hash) == _expected_keys(plan, 0, 8)
        finally:
            engine.dispose()


# --------------------------------------------------------------------------- #
# concurrency
# --------------------------------------------------------------------------- #
def _race(url: str, plan: Any, root: Path, ranges: list[tuple[int, int]]) -> list[Any]:
    def _run(start: int, count: int) -> Any:
        eng = _engine(url)
        try:
            return _enqueue(eng, plan, start=start, count=count)
        finally:
            eng.dispose()

    with ThreadPoolExecutor(max_workers=2) as pool:
        futs = [pool.submit(_run, s, c) for s, c in ranges]
        return [f.result() for f in futs]


def test_two_engines_same_range_one_final_set(isolated_pg_base_url: str, tmp_path: Path) -> None:
    plan = _synthetic_plan(_SNAPSHOT_A)
    with scratch_database(isolated_pg_base_url, "minos_l2f_synth") as url:
        alembic_upgrade(url, _HEAD)
        engine = _engine(url)
        try:
            with engine.connect() as conn, conn.begin():
                seed_upstream_for_plan(conn, plan)
            _persist(engine, plan, _provisioned_root(tmp_path))
            res = _race(url, plan, tmp_path, [(0, 10), (0, 10)])
            assert sum(r.created_count for r in res) == 10  # exactly one creator per job
            assert all(r.total_jobs_for_plan == 10 for r in res)
            assert _job_keys(engine, plan.plan_hash) == _expected_keys(plan, 0, 10)
        finally:
            engine.dispose()


def test_two_engines_overlapping_ranges_exact_union(
    isolated_pg_base_url: str, tmp_path: Path
) -> None:
    plan = _synthetic_plan(_SNAPSHOT_A)
    with scratch_database(isolated_pg_base_url, "minos_l2f_synth") as url:
        alembic_upgrade(url, _HEAD)
        engine = _engine(url)
        try:
            with engine.connect() as conn, conn.begin():
                seed_upstream_for_plan(conn, plan)
            _persist(engine, plan, _provisioned_root(tmp_path))
            res = _race(url, plan, tmp_path, [(0, 10), (5, 10)])  # union = indices 0..14
            assert sum(r.created_count for r in res) == 15
            assert _job_keys(engine, plan.plan_hash) == _expected_keys(plan, 0, 15)
        finally:
            engine.dispose()


# --------------------------------------------------------------------------- #
# alternate unique conflicts (typed)
# --------------------------------------------------------------------------- #
def _member_config_ids(engine: Engine, plan_id: str) -> tuple[list[str], list[str]]:
    with engine.connect() as c:
        members = [
            str(r[0])
            for r in c.execute(
                text(
                    "SELECT id FROM experiments.l2f_experiment_plan_members "
                    "WHERE plan_id=:p ORDER BY member_index"
                ),
                {"p": plan_id},
            ).all()
        ]
        configs = [
            str(r[0])
            for r in c.execute(
                text(
                    "SELECT id FROM experiments.l2f_experiment_plan_configs "
                    "WHERE plan_id=:p ORDER BY config_index"
                ),
                {"p": plan_id},
            ).all()
        ]
    return members, configs


def _insert_raw_job(
    engine: Engine, plan_id: str, member_id: str, config_id: str, job_key: str
) -> None:
    with engine.connect() as c, c.begin():
        c.execute(text("SET ROLE minos_admin"))
        c.execute(
            text(
                "INSERT INTO experiments.l2f_experiment_jobs "
                "(plan_id, plan_member_id, plan_config_id, job_key, status) "
                "VALUES (:p,:m,:c,:k,'PENDING')"
            ),
            {"p": plan_id, "m": member_id, "c": config_id, "k": job_key},
        )


def test_same_job_key_different_identity_conflict(
    isolated_pg_base_url: str, tmp_path: Path
) -> None:
    plan = _synthetic_plan(_SNAPSHOT_A)
    with scratch_database(isolated_pg_base_url, "minos_l2f_synth") as url:
        alembic_upgrade(url, _HEAD)
        engine = _engine(url)
        try:
            with engine.connect() as conn, conn.begin():
                seed_upstream_for_plan(conn, plan)
            _persist(engine, plan, _provisioned_root(tmp_path))
            pid = _plan_id(engine, plan.plan_hash)
            members, configs = _member_config_ids(engine, pid)
            jobs = list(iter_logical_jobs(plan))
            # pre-insert a row carrying job[0]'s key but a DIFFERENT (member,config) identity.
            _insert_raw_job(engine, pid, members[1], configs[1], jobs[0].job_key)
            with pytest.raises(ImmutableMetadataConflictError):
                _enqueue(engine, plan, start=0, count=1)
        finally:
            engine.dispose()


def test_same_identity_different_job_key_conflict(
    isolated_pg_base_url: str, tmp_path: Path
) -> None:
    plan = _synthetic_plan(_SNAPSHOT_A)
    with scratch_database(isolated_pg_base_url, "minos_l2f_synth") as url:
        alembic_upgrade(url, _HEAD)
        engine = _engine(url)
        try:
            with engine.connect() as conn, conn.begin():
                seed_upstream_for_plan(conn, plan)
            _persist(engine, plan, _provisioned_root(tmp_path))
            pid = _plan_id(engine, plan.plan_hash)
            members, configs = _member_config_ids(engine, pid)
            # pre-insert job[0]'s (member,config) identity but a WRONG job_key.
            _insert_raw_job(engine, pid, members[0], configs[0], "f" * 64)
            with pytest.raises(ImmutableMetadataConflictError):
                _enqueue(engine, plan, start=0, count=1)
        finally:
            engine.dispose()


# --------------------------------------------------------------------------- #
# status/claim preservation + rollback + immutability
# --------------------------------------------------------------------------- #
def test_existing_status_and_claim_preserved_on_replay(
    isolated_pg_base_url: str, tmp_path: Path
) -> None:
    plan = _synthetic_plan(_SNAPSHOT_A)
    with scratch_database(isolated_pg_base_url, "minos_l2f_synth") as url:
        alembic_upgrade(url, _HEAD)
        engine = _engine(url)
        try:
            with engine.connect() as conn, conn.begin():
                seed_upstream_for_plan(conn, plan)
            _persist(engine, plan, _provisioned_root(tmp_path))
            _enqueue(engine, plan, start=0, count=3)
            jobs = list(iter_logical_jobs(plan))
            # advance one job's mutable status/claim metadata (F4's job, done here only to prove
            # enqueue never resets it).
            with engine.connect() as c, c.begin():
                c.execute(text("SET ROLE minos_admin"))
                c.execute(
                    text(
                        "UPDATE experiments.l2f_experiment_jobs SET status='CLAIMED', "
                        "claimed_by='worker-1', claimed_at=now() WHERE job_key=:k"
                    ),
                    {"k": jobs[0].job_key},
                )
            r = _enqueue(engine, plan, start=0, count=3)
            assert r.created_count == 0 and r.existing_count == 3
            with engine.connect() as c:
                row = c.execute(
                    text(
                        "SELECT status, claimed_by FROM experiments.l2f_experiment_jobs "
                        "WHERE job_key=:k"
                    ),
                    {"k": jobs[0].job_key},
                ).one()
            assert row[0] == "CLAIMED" and row[1] == "worker-1"  # untouched by the replay
        finally:
            engine.dispose()


def test_transaction_rollback_leaves_no_partial_jobs(
    isolated_pg_base_url: str, tmp_path: Path
) -> None:
    plan = _synthetic_plan(_SNAPSHOT_A)
    with scratch_database(isolated_pg_base_url, "minos_l2f_synth") as url:
        alembic_upgrade(url, _HEAD)
        engine = _engine(url)
        try:
            with engine.connect() as conn, conn.begin():
                seed_upstream_for_plan(conn, plan)
            _persist(engine, plan, _provisioned_root(tmp_path))
            pid = _plan_id(engine, plan.plan_hash)
            members, configs = _member_config_ids(engine, pid)
            jobs = list(iter_logical_jobs(plan))
            # poison index 2 with a conflicting (same identity, wrong job_key) row so the enqueue
            # of [0,5) raises AFTER inserting 0 and 1 — which must then roll back.
            mi, ci = jobs[2].member_index, jobs[2].config_index
            _insert_raw_job(engine, pid, members[mi], configs[ci], "e" * 64)
            with pytest.raises(ImmutableMetadataConflictError):
                _enqueue(engine, plan, start=0, count=5)
            # only the pre-existing poisoned row survives; jobs 0 and 1 were rolled back.
            assert _graph_counts(engine)["l2f_experiment_jobs"] == 1
        finally:
            engine.dispose()


def test_legacy_and_f3c1_rows_and_files_unchanged(
    isolated_pg_base_url: str, tmp_path: Path
) -> None:
    plan = _synthetic_plan(_SNAPSHOT_A)
    with scratch_database(isolated_pg_base_url, "minos_l2f_synth") as url:
        alembic_upgrade(url, _HEAD)
        engine = _engine(url)
        try:
            with engine.connect() as conn, conn.begin():
                seed_upstream_for_plan(conn, plan)
            root = _provisioned_root(tmp_path)
            _persist(engine, plan, root)
            legacy_before = {
                t: _count(engine, f"SELECT count(*) FROM {t}")  # noqa: S608
                for t in ("profiling.profiles", "experiments.jobs", "experiments.results")
            }
            graph_before = _graph_counts(engine)
            files_before = {f.name: f.read_bytes() for f in _artifact_files(root)}

            _enqueue(engine, plan, start=0, count=7)

            for t, n in legacy_before.items():
                assert _count(engine, f"SELECT count(*) FROM {t}") == n  # noqa: S608
            after = _graph_counts(engine)
            for tbl in (
                "l2f_experiment_plans",
                "l2f_experiment_plan_members",
                "l2f_config_payloads",
            ):
                assert after[tbl] == graph_before[tbl]  # F3-C1 rows untouched
            assert {f.name: f.read_bytes() for f in _artifact_files(root)} == files_before
        finally:
            engine.dispose()


# --------------------------------------------------------------------------- #
# missing graph / range bounds / non-75 derivation
# --------------------------------------------------------------------------- #
def test_missing_graph_fails_before_job_insertion(
    isolated_pg_base_url: str, tmp_path: Path
) -> None:
    plan = _synthetic_plan(_SNAPSHOT_A)
    with scratch_database(isolated_pg_base_url, "minos_l2f_synth") as url:
        alembic_upgrade(url, _HEAD)  # upstream + plan graph NOT persisted
        engine = _engine(url)
        try:
            with engine.connect() as conn, conn.begin():
                seed_upstream_for_plan(conn, plan)  # upstream only; no F3-C1 plan graph
            with pytest.raises(JobGraphMissingError):
                _enqueue(engine, plan, start=0, count=3)
            assert _graph_counts(engine)["l2f_experiment_jobs"] == 0
        finally:
            engine.dispose()


def test_range_exceeding_logical_job_count_fails(isolated_pg_base_url: str, tmp_path: Path) -> None:
    plan = _synthetic_plan(_SNAPSHOT_A)  # 4 train * 41 = 164 logical jobs
    assert plan.logical_job_count == 164
    with scratch_database(isolated_pg_base_url, "minos_l2f_synth") as url:
        alembic_upgrade(url, _HEAD)
        engine = _engine(url)
        try:
            with engine.connect() as conn, conn.begin():
                seed_upstream_for_plan(conn, plan)
            _persist(engine, plan, _provisioned_root(tmp_path))
            with pytest.raises(JobEnqueueRangeError):
                _enqueue(engine, plan, start=161, count=4)  # 165 > 164
            assert _graph_counts(engine)["l2f_experiment_jobs"] == 0
        finally:
            engine.dispose()


@pytest.mark.parametrize(("spec", "train"), [(_SNAPSHOT_A, 4), (_SNAPSHOT_B, 2)])
def test_non75_synthetic_job_counts(
    isolated_pg_base_url: str, tmp_path: Path, spec: list[tuple[str, str, str]], train: int
) -> None:
    plan = _synthetic_plan(spec)
    ljc = train * len(_CS.configs)
    assert plan.logical_job_count == ljc
    with scratch_database(isolated_pg_base_url, "minos_l2f_synth") as url:
        alembic_upgrade(url, _HEAD)
        engine = _engine(url)
        try:
            with engine.connect() as conn, conn.begin():
                seed_upstream_for_plan(conn, plan)
            _persist(engine, plan, _provisioned_root(tmp_path))
            # the final bounded slice is exactly derivable from the plan (no magic constants).
            r = _enqueue(engine, plan, start=ljc - 3, count=3)
            assert r.created_count == 3 and r.total_jobs_for_plan == 3
            assert _job_keys(engine, plan.plan_hash) == _expected_keys(plan, ljc - 3, 3)
        finally:
            engine.dispose()


# --------------------------------------------------------------------------- #
# production entry point: identity + revision guards + end-to-end success
# --------------------------------------------------------------------------- #
def _install_build_sentinel(monkeypatch: pytest.MonkeyPatch) -> dict[str, int]:
    calls = {"plan": 0, "candidates": 0}
    orig_plan = EN._build_accepted_plan
    orig_cs = EN._build_accepted_candidate_set

    def _p() -> Any:
        calls["plan"] += 1
        return orig_plan()

    def _c() -> Any:
        calls["candidates"] += 1
        return orig_cs()

    monkeypatch.setattr(EN, "_build_accepted_plan", _p)
    monkeypatch.setattr(EN, "_build_accepted_candidate_set", _c)
    return calls


def test_wrong_database_identity_fails_before_plan_construction(
    isolated_pg_base_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    with scratch_database(isolated_pg_base_url, "not_the_operational_store") as url:
        alembic_upgrade(url, _HEAD)
        monkeypatch.setenv("MINOS_DATABASE_URL", url)
        calls = _install_build_sentinel(monkeypatch)
        with pytest.raises(OperationalDatabaseIdentityError):
            enqueue_accepted_experiment_jobs(start=0, count=1)
        assert calls == {"plan": 0, "candidates": 0}


def test_revision_0005_fails_before_plan_construction(
    isolated_pg_base_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    with scratch_database(isolated_pg_base_url, _OP_DB) as url:
        alembic_upgrade(url, _PREV)  # 0005, not 0006
        monkeypatch.setenv("MINOS_DATABASE_URL", url)
        calls = _install_build_sentinel(monkeypatch)
        with pytest.raises(PlanRevisionError):
            enqueue_accepted_experiment_jobs(start=0, count=1)
        assert calls == {"plan": 0, "candidates": 0}


def test_accepted_production_enqueue_end_to_end(
    isolated_pg_base_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with scratch_database(isolated_pg_base_url, _OP_DB) as url:
        alembic_upgrade(url, _HEAD)
        engine = _engine(url)
        try:
            with engine.connect() as conn, conn.begin():
                seed_upstream_for_plan(conn, _ACCEPTED_PLAN)
            root = _provisioned_root(tmp_path)
            monkeypatch.setenv("MINOS_DATABASE_URL", url)
            monkeypatch.setenv("MINOS_L2F_CONFIG_ARTIFACT_ROOT", str(root))
            # persist the accepted F3-C1 graph via its production entry point.
            from minos_engine.storage.l2f_plan_store import persist_accepted_experiment_plan

            persist_accepted_experiment_plan()
            r = enqueue_accepted_experiment_jobs(start=0, count=4)
            assert r.plan_hash == _ACCEPTED_PLAN.plan_hash
            assert r.created_count == 4 and r.total_jobs_for_plan == 4
            assert r.requested_count == 4
            # bounded: cannot exceed the accepted plan's logical_job_count in one call anyway
            # (that is enforced by the batch cap), and no enqueue-all path exists.
            assert _job_keys(engine, _ACCEPTED_PLAN.plan_hash) == _expected_keys(
                _ACCEPTED_PLAN, 0, 4
            )
        finally:
            engine.dispose()
